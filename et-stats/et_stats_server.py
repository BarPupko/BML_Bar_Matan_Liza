#!/usr/bin/env python3
"""Tiny HTTP status server for the ET and Minecraft game servers.

Queries the ET server's UDP "getstatus" protocol directly, and the Minecraft
server via the public mcstatus.io API, exposing both as JSON over HTTP (with
CORS enabled) so a static webpage can fetch them. Also runs a background
poller that accumulates total playtime per player per game, persisted to
disk, and exposed via /playtime.
"""

import gzip
import hmac
import io
import json
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ET_HOST = "127.0.0.1"  # the server runs on this same machine; using the public
                       # IP fails here due to NAT hairpinning on most routers
ET_PORT = 27960
MC_STATUS_URL = "https://api.mcstatus.io/v2/status/java/176.229.38.5:25565"
MC_PLAYERDATA_DIR = Path("/home/plex/minecraft-server/world/playerdata")

# RCON password for the ET server itself (lives outside this repo entirely —
# never committed). Separate from ADMIN_KEY_FILE below, which gates who is
# allowed to trigger these actions from the public website.
RCON_PASSWORD_FILE = Path("/home/plex/Public/etl-jaymod-server/scripts/.rcon_password")
RCON_TIMEOUT = 2.0

# Passphrase the website's admin panel must supply. Kept in a dotfile next to
# this script and listed in .gitignore — must never be hardcoded here, since
# this file is committed to a public GitHub repo.
ADMIN_KEY_FILE = Path(__file__).parent / ".admin_key"

UNSAFE_CONSOLE_CHARS = '";\n\r\\'

SERVER_LOG_FILE = Path("/home/plex/Public/etl-jaymod-server/server.log")
CONSOLE_LOG_TAIL_BYTES = 200_000  # only scan the tail of the log for speed
CONSOLE_LOG_LINE_LIMIT = 40
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
CONSOLE_INTERESTING_RE = re.compile(r"broadcast:|rcon from|say(?:team)?:|\bkick(?:ed)?\b", re.IGNORECASE)
CONSOLE_TIMESTAMP_RE = re.compile(r"^\s*\d+\s+")

# Minecraft server console: the systemd unit wires stdin to this named pipe,
# so commands can be sent by just writing a line to it — no RCON needed.
MC_PIPE_FILE = Path("/home/plex/minecraft-server/mcpipe")
MC_SYSTEMD_UNIT = "minecraft.service"
MC_CONSOLE_LINE_LIMIT = 40
MC_CONSOLE_INTERESTING_RE = re.compile(
    r"<[^>]+>|joined the game|left the game|logged in|kicked|banned|whitelist|difficulty|Set the",
    re.IGNORECASE,
)

# Curated, strictly-whitelisted settings the website is allowed to change —
# never build a command from free-form client input.
MC_ALLOWED_SETTINGS = {
    "difficulty": {"peaceful", "easy", "normal", "hard"},
    "weather": {"clear", "rain", "thunder"},
    "time": {"day", "noon", "night", "midnight"},
    "whitelist": {"on", "off"},
}

MC_BUILTIN_LOCATIONS = {
    "home": (-181.345, 64.00000, 244.259),
    "hell": (-15.583, 60.000000, 24.648),
}
MC_LOCATIONS_FILE = Path(__file__).parent / "mc_locations.json"
MC_LOCATION_NAME_RE = re.compile(r"^[A-Za-z0-9_ -]{1,24}$")
_mc_locations_lock = threading.Lock()

MC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MC_ITEM_RE = re.compile(r"^[a-z0-9_:]{1,64}$")
MC_GAMEMODES = {"survival", "creative", "adventure", "spectator"}

# Confirmed via `dir maps bsp` over RCON — the maps actually installed on
# this server, not a guessed list.
ET_ALLOWED_MAPS = {"battery", "fueldump", "goldrush", "oasis", "radar", "railgun", "braundorf_b4"}

BACKUP_SCRIPT = Path(__file__).parent / "backup.sh"


def is_valid_mc_username(name):
    return bool(MC_USERNAME_RE.match(name or ""))


def load_custom_mc_locations():
    if not MC_LOCATIONS_FILE.exists():
        return {}
    try:
        with open(MC_LOCATIONS_FILE) as f:
            data = json.load(f)
        return {
            name: tuple(coords) for name, coords in data.items()
            if isinstance(coords, list) and len(coords) == 3
        }
    except (OSError, ValueError):
        return {}


def save_custom_mc_locations(locations):
    tmp = MC_LOCATIONS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump({name: list(coords) for name, coords in locations.items()}, f)
    tmp.replace(MC_LOCATIONS_FILE)


def get_all_mc_locations():
    with _mc_locations_lock:
        custom = load_custom_mc_locations()
    merged = dict(MC_BUILTIN_LOCATIONS)
    merged.update(custom)
    return merged


def add_custom_mc_location(name, x, y, z):
    with _mc_locations_lock:
        custom = load_custom_mc_locations()
        custom[name] = (x, y, z)
        save_custom_mc_locations(custom)


def remove_custom_mc_location(name):
    with _mc_locations_lock:
        custom = load_custom_mc_locations()
        if name in custom:
            del custom[name]
            save_custom_mc_locations(custom)
            return True
        return False

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8099
QUERY_TIMEOUT = 2.0
CACHE_SECONDS = 5

POLL_INTERVAL = 60  # seconds between playtime polls
PLAYTIME_FILE = Path(__file__).parent / "playtime.json"

# Persistent name -> uuid map, built up during polling, so we can look up a
# player's .dat file (for XP/position/last-seen) even when they're offline.
MC_PLAYERS_FILE = Path(__file__).parent / "mc_players.json"
_mc_players_lock = threading.Lock()

COLOR_CODE_RE = re.compile(r"\^(x[0-9A-Fa-f]{6}|.)")
PLAYER_LINE_RE = re.compile(r'^(-?\d+)\s+(-?\d+)\s+"(.*)"$')

_cache = {"data": None, "expires": 0}
_playtime_lock = threading.Lock()
_playtime = {"et": {}, "mc": {}}


def strip_colors(name):
    return COLOR_CODE_RE.sub("", name).strip()


def query_et_server(host, port, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"\xff\xff\xff\xffgetstatus", (host, port))
        data, _ = sock.recvfrom(8192)
    finally:
        sock.close()

    text = data.decode("latin-1", errors="replace")
    prefix = "\xff\xff\xff\xffstatusResponse\n"
    if not text.startswith(prefix):
        raise ValueError("unexpected response from server")
    text = text[len(prefix):]

    lines = text.split("\n")
    info_line = lines[0] if lines else ""
    player_lines = [l for l in lines[1:] if l.strip()]

    parts = info_line.split("\\")[1:]
    info = dict(zip(parts[0::2], parts[1::2]))

    players = []
    for line in player_lines:
        m = PLAYER_LINE_RE.match(line.strip())
        if m:
            score, ping, raw_name = m.groups()
            players.append({
                "name": strip_colors(raw_name),
                "score": int(score),
                "ping": int(ping),
            })

    return {
        "online": True,
        "hostname": strip_colors(info.get("sv_hostname", "")),
        "map": info.get("mapname", ""),
        "players": {
            "online": len(players),
            "max": int(info.get("sv_maxclients", 0)),
            "list": players,
        },
    }


def get_status():
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    try:
        result = query_et_server(ET_HOST, ET_PORT, QUERY_TIMEOUT)
    except (socket.timeout, OSError, ValueError):
        result = {"online": False}

    result["checked_at"] = int(now)
    _cache["data"] = result
    _cache["expires"] = now + CACHE_SECONDS
    return result


def query_mc_status():
    """Returns (online, [{"name": ..., "uuid": ...}, ...]) for the MC server."""
    try:
        req = urllib.request.Request(MC_STATUS_URL, headers={"User-Agent": "Mozilla/5.0 (BML-stats-server)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError):
        return False, []

    if not data.get("online"):
        return False, []

    players = [
        {"name": p["name_clean"], "uuid": p["uuid"]}
        for p in data.get("players", {}).get("list", [])
    ]
    return True, players


def query_mc_names():
    """Returns (online, [player names]) for the Minecraft server, or (False, []) on failure."""
    online, players = query_mc_status()
    return online, [p["name"] for p in players]


# --- Minimal big-endian NBT reader, just enough to pull XpLevel out of a
# player .dat file (gzip-compressed NBT). No external dependency needed.

def _nbt_read_string(f):
    length = struct.unpack(">H", f.read(2))[0]
    return f.read(length).decode("utf-8", errors="replace")


def _nbt_read_payload(f, tag_type):
    if tag_type == 1:
        return struct.unpack(">b", f.read(1))[0]
    if tag_type == 2:
        return struct.unpack(">h", f.read(2))[0]
    if tag_type == 3:
        return struct.unpack(">i", f.read(4))[0]
    if tag_type == 4:
        return struct.unpack(">q", f.read(8))[0]
    if tag_type == 5:
        return struct.unpack(">f", f.read(4))[0]
    if tag_type == 6:
        return struct.unpack(">d", f.read(8))[0]
    if tag_type == 7:
        n = struct.unpack(">i", f.read(4))[0]
        return f.read(n)
    if tag_type == 8:
        return _nbt_read_string(f)
    if tag_type == 9:
        item_type = struct.unpack(">b", f.read(1))[0]
        n = struct.unpack(">i", f.read(4))[0]
        return [_nbt_read_payload(f, item_type) for _ in range(n)]
    if tag_type == 10:
        d = {}
        while True:
            t = struct.unpack(">b", f.read(1))[0]
            if t == 0:
                break
            name = _nbt_read_string(f)
            d[name] = _nbt_read_payload(f, t)
        return d
    if tag_type == 11:
        n = struct.unpack(">i", f.read(4))[0]
        return list(struct.unpack(">%di" % n, f.read(4 * n)))
    if tag_type == 12:
        n = struct.unpack(">i", f.read(4))[0]
        return list(struct.unpack(">%dq" % n, f.read(8 * n)))
    raise ValueError(f"unknown NBT tag type {tag_type}")


def get_mc_xp_level(player_uuid):
    path = MC_PLAYERDATA_DIR / f"{player_uuid}.dat"
    try:
        with gzip.open(path, "rb") as gz:
            data = gz.read()
        stream = io.BytesIO(data)
        struct.unpack(">b", stream.read(1))  # root tag type (compound)
        _nbt_read_string(stream)  # root name (unused)
        root = _nbt_read_payload(stream, 10)
        return int(root.get("XpLevel", 0))
    except (OSError, ValueError, KeyError, struct.error):
        return None


def get_mc_player_snapshot(player_uuid):
    """XP level, last known position, and last-seen time (file mtime, i.e.
    last time this player's data was saved) from their .dat file."""
    path = MC_PLAYERDATA_DIR / f"{player_uuid}.dat"
    try:
        last_seen = int(path.stat().st_mtime)
        with gzip.open(path, "rb") as gz:
            data = gz.read()
        stream = io.BytesIO(data)
        struct.unpack(">b", stream.read(1))
        _nbt_read_string(stream)
        root = _nbt_read_payload(stream, 10)
        xp_level = int(root.get("XpLevel", 0))
        pos_raw = root.get("Pos")
        pos = None
        if isinstance(pos_raw, list) and len(pos_raw) == 3:
            pos = [round(float(v), 1) for v in pos_raw]
        return {"xp_level": xp_level, "pos": pos, "last_seen": last_seen}
    except (OSError, ValueError, KeyError, struct.error):
        return {"xp_level": None, "pos": None, "last_seen": None}


def load_mc_players():
    if not MC_PLAYERS_FILE.exists():
        return {}
    try:
        with open(MC_PLAYERS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_mc_players(mapping):
    tmp = MC_PLAYERS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(mapping, f)
    tmp.replace(MC_PLAYERS_FILE)


def get_mc_player_details(mc_playtime):
    with _mc_players_lock:
        mapping = load_mc_players()
    result = {}
    for name, seconds in mc_playtime.items():
        player_uuid = mapping.get(name)
        snap = get_mc_player_snapshot(player_uuid) if player_uuid else {
            "xp_level": None, "pos": None, "last_seen": None,
        }
        result[name] = {
            "playtime_seconds": seconds,
            "xp_level": snap["xp_level"],
            "pos": snap["pos"],
            "last_seen": snap["last_seen"],
        }
    return result


def load_playtime():
    if PLAYTIME_FILE.exists():
        try:
            with open(PLAYTIME_FILE) as f:
                data = json.load(f)
                return {"et": data.get("et", {}), "mc": data.get("mc", {})}
        except (OSError, ValueError):
            pass
    return {"et": {}, "mc": {}}


def save_playtime(data):
    tmp = PLAYTIME_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(PLAYTIME_FILE)


def poll_loop():
    global _playtime
    with _playtime_lock:
        _playtime = load_playtime()

    while True:
        try:
            et_result = query_et_server(ET_HOST, ET_PORT, QUERY_TIMEOUT)
            et_names = [p["name"] for p in et_result["players"]["list"]]
        except (socket.timeout, OSError, ValueError):
            et_names = []

        _, mc_players = query_mc_status()

        with _playtime_lock:
            for name in et_names:
                _playtime["et"][name] = _playtime["et"].get(name, 0) + POLL_INTERVAL
            for p in mc_players:
                _playtime["mc"][p["name"]] = _playtime["mc"].get(p["name"], 0) + POLL_INTERVAL
            save_playtime(_playtime)

        if mc_players:
            with _mc_players_lock:
                mapping = load_mc_players()
                changed = False
                for p in mc_players:
                    if mapping.get(p["name"]) != p["uuid"]:
                        mapping[p["name"]] = p["uuid"]
                        changed = True
                if changed:
                    save_mc_players(mapping)

        time.sleep(POLL_INTERVAL)


def get_playtime():
    with _playtime_lock:
        et = dict(_playtime["et"])
        mc = dict(_playtime["mc"])
    return {
        "et": et,
        "mc": mc,
        "mc_details": get_mc_player_details(mc),
        "updated_at": int(time.time()),
    }


def get_mc_xp():
    online, players = query_mc_status()
    result = []
    for p in players:
        xp_level = get_mc_xp_level(p["uuid"])
        result.append({"name": p["name"], "xp_level": xp_level})
    return {"online": online, "players": result, "checked_at": int(time.time())}


def is_safe_console_string(s):
    return bool(s) and len(s) <= 100 and not any(c in s for c in UNSAFE_CONSOLE_CHARS)


def check_admin_key(provided):
    try:
        expected = ADMIN_KEY_FILE.read_text().strip()
    except OSError:
        return False
    return bool(provided) and hmac.compare_digest(provided, expected)


def get_console_log(limit=CONSOLE_LOG_LINE_LIMIT):
    """Tail the ET server's console log, keeping only player/admin-relevant
    lines (joins, chat, rcon commands, kicks) and dropping bot gameplay
    telemetry (Item/Kill/Ammo/Userinfo spam etc.)."""
    if not SERVER_LOG_FILE.exists():
        return []

    with open(SERVER_LOG_FILE, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - CONSOLE_LOG_TAIL_BYTES))
        data = f.read()

    text = data.decode("utf-8", errors="replace")
    cleaned = []
    for line in text.splitlines():
        if not CONSOLE_INTERESTING_RE.search(line):
            continue
        line = ANSI_ESCAPE_RE.sub("", line)
        line = strip_colors(line)
        line = line.replace("\\n", "").strip()
        line = CONSOLE_TIMESTAMP_RE.sub("", line)
        if line:
            cleaned.append(line)

    return cleaned[-limit:]


def send_rcon_command(command, timeout=RCON_TIMEOUT):
    password = RCON_PASSWORD_FILE.read_text().strip()
    packet = b"\xff\xff\xff\xffrcon " + password.encode() + b" " + command.encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ET_HOST, ET_PORT))
        chunks = []
        try:
            while True:
                data, _ = sock.recvfrom(65536)
                text = data[4:].decode(errors="replace") if data[:4] == b"\xff\xff\xff\xff" else data.decode(errors="replace")
                chunks.append(text.strip("\n"))
        except socket.timeout:
            pass
        return "\n".join(chunks)
    finally:
        sock.close()


def send_mc_command(command):
    with open(MC_PIPE_FILE, "w") as f:
        f.write(command + "\n")


def mc_setting_command(setting, value):
    allowed_values = MC_ALLOWED_SETTINGS.get(setting)
    if not allowed_values or value not in allowed_values:
        return None
    if setting == "difficulty":
        return f"difficulty {value}"
    if setting == "weather":
        return f"weather {value}"
    if setting == "time":
        return f"time set {value}"
    if setting == "whitelist":
        return f"whitelist {value}"
    return None


def get_mc_console(limit=MC_CONSOLE_LINE_LIMIT):
    """Tail the Minecraft server's journal, keeping only player-relevant
    lines (chat, joins/leaves, admin actions) and dropping the rest."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", MC_SYSTEMD_UNIT, "-n", "300", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    cleaned = [
        line.strip() for line in result.stdout.splitlines()
        if MC_CONSOLE_INTERESTING_RE.search(line)
    ]
    return cleaned[-limit:]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/playtime"):
            self._send_json(get_playtime())
        elif self.path.startswith("/mc-xp"):
            self._send_json(get_mc_xp())
        elif self.path.startswith("/status"):
            self._send_json(get_status())
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            self._send_json({"error": "invalid JSON"}, status=400)
            return

        if not self.path.startswith("/admin/"):
            self._send_json({"error": "not found"}, status=404)
            return

        if not check_admin_key(body.get("admin_key", "")):
            self._send_json({"error": "unauthorized"}, status=403)
            return

        if self.path.startswith("/admin/set-password"):
            password = body.get("password", "")
            if not is_safe_console_string(password):
                self._send_json({"error": "invalid password"}, status=400)
                return
            send_rcon_command(f'g_password "{password}"')
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/clear-password"):
            send_rcon_command('g_password ""')
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/say"):
            message = body.get("message", "")
            if not is_safe_console_string(message):
                self._send_json({"error": "invalid message"}, status=400)
                return
            send_rcon_command(f'say "{message}"')
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/console"):
            self._send_json({"lines": get_console_log()})

        elif self.path.startswith("/admin/et-kick"):
            player = body.get("player", "")
            if not is_safe_console_string(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            send_rcon_command(f"kick {player}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-tempban"):
            player = body.get("player", "")
            minutes = body.get("minutes", 0)
            if not is_safe_console_string(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            if not isinstance(minutes, int) or not (1 <= minutes <= 1440):
                self._send_json({"error": "invalid duration"}, status=400)
                return
            send_rcon_command(f"kick {player} {minutes}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-map"):
            map_name = body.get("map", "")
            if map_name not in ET_ALLOWED_MAPS:
                self._send_json({"error": "invalid map"}, status=400)
                return
            send_rcon_command(f"map {map_name}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-shuffle"):
            send_rcon_command("shuffleteams")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-say"):
            message = body.get("message", "")
            if not is_safe_console_string(message):
                self._send_json({"error": "invalid message"}, status=400)
                return
            send_mc_command(f"say {message}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-setting"):
            command = mc_setting_command(body.get("setting", ""), body.get("value", ""))
            if not command:
                self._send_json({"error": "invalid setting"}, status=400)
                return
            send_mc_command(command)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-console"):
            self._send_json({"lines": get_mc_console()})

        elif self.path.startswith("/admin/mc-teleport"):
            player = body.get("player", "")
            location = body.get("location", "")
            locations = get_all_mc_locations()
            if not is_valid_mc_username(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            if location not in locations:
                self._send_json({"error": "invalid location"}, status=400)
                return
            x, y, z = locations[location]
            send_mc_command(f"tp {player} {x} {y} {z}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-locations-list"):
            locations = get_all_mc_locations()
            self._send_json({
                "locations": [
                    {"name": name, "x": x, "y": y, "z": z, "builtin": name in MC_BUILTIN_LOCATIONS}
                    for name, (x, y, z) in locations.items()
                ]
            })

        elif self.path.startswith("/admin/mc-add-location"):
            name = body.get("name", "")
            try:
                x = float(body.get("x"))
                y = float(body.get("y"))
                z = float(body.get("z"))
            except (TypeError, ValueError):
                self._send_json({"error": "invalid coordinates"}, status=400)
                return
            if not MC_LOCATION_NAME_RE.match(name):
                self._send_json({"error": "invalid name"}, status=400)
                return
            if name.lower() in MC_BUILTIN_LOCATIONS:
                self._send_json({"error": "name already used by a built-in location"}, status=400)
                return
            if not all(-30000000 <= v <= 30000000 for v in (x, y, z)):
                self._send_json({"error": "coordinates out of range"}, status=400)
                return
            add_custom_mc_location(name, x, y, z)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-remove-location"):
            name = body.get("name", "")
            if name in MC_BUILTIN_LOCATIONS:
                self._send_json({"error": "cannot remove a built-in location"}, status=400)
                return
            if not remove_custom_mc_location(name):
                self._send_json({"error": "location not found"}, status=404)
                return
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-op"):
            player = body.get("player", "")
            grant = body.get("grant", True)
            if not is_valid_mc_username(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            send_mc_command(f"{'op' if grant else 'deop'} {player}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-give"):
            player = body.get("player", "")
            item = body.get("item", "")
            count = body.get("count", 1)
            if not is_valid_mc_username(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            if not MC_ITEM_RE.match(item or ""):
                self._send_json({"error": "invalid item"}, status=400)
                return
            if not isinstance(count, int) or not (1 <= count <= 6400):
                self._send_json({"error": "invalid count"}, status=400)
                return
            send_mc_command(f"give {player} {item} {count}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/mc-gamemode"):
            player = body.get("player", "")
            mode = body.get("mode", "")
            if not is_valid_mc_username(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            if mode not in MC_GAMEMODES:
                self._send_json({"error": "invalid gamemode"}, status=400)
                return
            send_mc_command(f"gamemode {mode} {player}")
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/backup-now"):
            if not BACKUP_SCRIPT.exists():
                self._send_json({"error": "backup script not found"}, status=500)
                return
            subprocess.Popen(
                ["bash", str(BACKUP_SCRIPT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._send_json({"ok": True, "message": "Backup started in the background."})

        else:
            self._send_json({"error": "not found"}, status=404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"Stats server listening on http://{LISTEN_HOST}:{LISTEN_PORT}/status and /playtime")
    server.serve_forever()
