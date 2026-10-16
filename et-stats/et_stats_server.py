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
import http.client
import io
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mc_roster import get_mc_roster

ET_HOST = "127.0.0.1"  # the servers run on this same machine; using the public
                       # IP fails here due to NAT hairpinning on most routers
MC_STATUS_URL = "https://api.mcstatus.io/v2/status/java/77.127.1.215:25565"
MC_PLAYERDATA_DIR = Path("/home/plex/minecraft-server/world/playerdata")

PUBLIC_IP = "77.127.1.215"

# ET servers, keyed by name. Only Jaymod runs now; a NitMod server used to sit
# on the next port up (retired 2026-10-16, its files are kept in
# ~/Public/etl-nitmod-server). Everything ET-related is keyed by these names —
# requests carry a "server" field, and anything that omits it means Jaymod, so
# older cached copies of the page keep working.
#
# rcon_password_file lives outside this repo entirely and is never committed.
# It is separate from ADMIN_KEY_FILE below, which gates who is allowed to
# trigger these actions from the public website.
#
# "features" is the whitelist of gameplay cvars the admin panel may toggle,
# with the exact values allowed for each. Mods do not implement the same set,
# so this is per server rather than global.
ET_ON_OFF = {"0", "1"}

ET_SERVERS = {
    "jaymod": {
        "label": "Jaymod",
        "port": 27960,
        "dir": Path("/home/plex/Public/etl-jaymod-server"),
        "unit": "etlded.service",
        "features": {
            "g_friendlyFire": ET_ON_OFF,
            "g_goomba": {"0", "4"},
            "g_headshot": ET_ON_OFF,
            "g_gravity": {"800", "400", "200"},
        },
    },
}
DEFAULT_ET_SERVER = "jaymod"

for _key, _srv in ET_SERVERS.items():
    _srv["log"] = _srv["dir"] / "server.log"
    _srv["rcon_password_file"] = _srv["dir"] / "scripts" / ".rcon_password"
    _srv["address"] = f"{PUBLIC_IP}:{_srv['port']}"

# Power control for the game server processes. Both run as systemd *user*
# units, so this needs no root — which is the whole reason it can be offered
# from the website at all. Stopping is a real stop: the units are
# Restart=always, but systemd does not bring back something stopped on
# purpose, so it stays down until someone presses Start.
#
# Note this shuts down a game server, not the machine. Powering off the host
# would need sudo, which this account does not have without a password.
ET_POWER_ACTIONS = {"stop", "start", "restart"}

# Minecraft gets the same Start/Stop/Restart through et_power(), as a user
# unit like the ET servers. It used to be a system unit (needs sudo); while
# that one is still active, starting the user unit would launch a second
# server on the same world, so starts are refused until it is retired.
MC_SERVER = {"label": "Minecraft", "unit": "minecraft.service"}
MC_SYSTEM_UNIT = "minecraft.service"
SYSTEMCTL_TIMEOUT = 15

RCON_TIMEOUT = 2.0

# Passphrase the website's admin panel must supply. Kept in a dotfile next to
# this script and listed in .gitignore — must never be hardcoded here, since
# this file is committed to a public GitHub repo.
ADMIN_KEY_FILE = Path(__file__).parent / ".admin_key"

UNSAFE_CONSOLE_CHARS = '";\n\r\\'

CONSOLE_LOG_TAIL_BYTES = 200_000  # only scan the tail of the log for speed
CONSOLE_LOG_LINE_LIMIT = 40
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
CONSOLE_INTERESTING_RE = re.compile(r"broadcast:|rcon from|say(?:team)?:|\bkick(?:ed)?\b", re.IGNORECASE)
CONSOLE_TIMESTAMP_RE = re.compile(r"^\s*\d+\s+")

# The UDP "getstatus" response carries no team information, so teams come from
# the console log instead: the engine logs a ClientUserinfoChanged line holding
# the player's sessionTeam every time someone connects or switches sides.
ET_USERINFO_RE = re.compile(r"ClientUserinfoChanged:\s*(\d+)\s+(n\\.*)")
ET_DISCONNECT_RE = re.compile(r"ClientDisconnect:\s*(\d+)")
ET_TEAM_NAMES = {0: "free", 1: "axis", 2: "allies", 3: "spectator"}

# Kills are only ever reported in the console log — the UDP getstatus response
# carries score and ping, nothing else. Format:
#   <ms> Kill: <killer slot> <victim slot> <mod>: <killer> killed <victim> by MOD_X
# The killer is non-greedy so a name containing " killed " can't swallow the
# separator; the victim is anchored by the trailing " by MOD_...".
ET_KILL_RE = re.compile(r"Kill:\s*(-?\d+)\s+(-?\d+)\s+(-?\d+):\s*(.+?) killed (.+?) by (MOD_\w+)")

# Slot 1022 is the world (falling damage, map triggers), not a player.
ET_WORLD_NAMES = {"<world>", "<non-client>"}

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
ET_ALLOWED_MAPS = {
    "battery", "fueldump", "goldrush", "oasis", "radar", "railgun", "braundorf_b4",
    # Added alongside the pk3s in etmain/; all verified to load over RCON.
    "supply", "adlernest", "etl_ice", "etl_warbell",
}

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

# The BlueMap web map (minecraft-map.service) listens on localhost only and is
# reached through this server under /map/, so it rides the same Cloudflare
# tunnel as the stats API rather than needing a second public URL.
MAP_HOST = "127.0.0.1"
MAP_PORT = 8100
MAP_TIMEOUT = 30
MAP_REQUEST_HEADERS = ("Accept", "Accept-Encoding", "If-Modified-Since", "If-None-Match", "Range")
MAP_RESPONSE_HEADERS = ("Content-Type", "Content-Encoding", "Content-Length", "Cache-Control",
                        "Last-Modified", "ETag", "Content-Range", "Accept-Ranges")
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

_cache = {}  # per-ET-server {label: {"data": ..., "expires": ...}}
_playtime_lock = threading.Lock()

# "et"/"mc" hold total seconds per player. "et_seen" is the unix time each ET
# player was last observed online by the poller, and "et_kd" their cumulative
# kills/deaths. "et_log_pos" is how far into server.log the kill parser has
# read, so each poll only scans what was appended since the last one.
_playtime = {"et": {}, "mc": {}, "et_seen": {}, "et_kd": {}, "et_log_pos": {}}


def strip_colors(name):
    return COLOR_CODE_RE.sub("", name).strip()


def resolve_et_server(key):
    """Look up an ET server by name. Anything unrecognised — including a
    missing field from an older cached copy of the page — falls back to
    Jaymod rather than erroring, since that is the server that has always
    been at this endpoint."""
    return ET_SERVERS.get(key or DEFAULT_ET_SERVER, ET_SERVERS[DEFAULT_ET_SERVER])


def is_known_et_server(key):
    return key in ET_SERVERS


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


def get_et_teams(server):
    """Map lowercased player name -> "axis"/"allies"/"spectator" by replaying
    the tail of the console log. The most recent ClientUserinfoChanged for a
    client slot wins, and a disconnect clears the slot so a recycled slot never
    reports the previous occupant's team. Returns {} if the log is unreadable —
    callers treat a missing team as unknown rather than failing."""
    log_file = server["log"]
    if not log_file.exists():
        return {}

    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - CONSOLE_LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return {}

    slots = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = ANSI_ESCAPE_RE.sub("", line)

        gone = ET_DISCONNECT_RE.search(line)
        if gone:
            slots.pop(gone.group(1), None)
            continue

        m = ET_USERINFO_RE.search(line)
        if not m:
            continue

        fields = m.group(2).split("\\")
        info = dict(zip(fields[0::2], fields[1::2]))
        name = strip_colors(info.get("n", "")).lower()
        try:
            team = ET_TEAM_NAMES.get(int(info.get("t", "")))
        except ValueError:
            team = None
        if name and team:
            slots[m.group(1)] = (name, team)

    return {name: team for name, team in slots.values()}


def scan_et_kills(server, start_pos):
    """Read server.log from start_pos and tally kills/deaths per player name.

    Returns ({lowercased name: {"kills": n, "deaths": n}}, new_pos). Only whole
    lines are consumed, so a line still being written is picked up next time.
    Logrotate uses copytruncate, so a file shorter than start_pos means the log
    was rotated out from under us and the scan restarts from the beginning."""
    log_file = server["log"]
    if not log_file.exists():
        return {}, start_pos

    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < start_pos:
                start_pos = 0
            f.seek(start_pos)
            data = f.read()
    except OSError:
        return {}, start_pos

    # Trim the trailing partial line and account for it in the new offset.
    cut = data.rfind(b"\n")
    if cut == -1:
        return {}, start_pos
    new_pos = start_pos + cut + 1
    data = data[:cut]

    tally = {}

    def bump(name, field):
        if not name or name in ET_WORLD_NAMES:
            return
        tally.setdefault(name, {"kills": 0, "deaths": 0})[field] += 1

    for line in data.decode("utf-8", errors="replace").splitlines():
        m = ET_KILL_RE.search(ANSI_ESCAPE_RE.sub("", line))
        if not m:
            continue
        killer = strip_colors(m.group(4)).lower()
        victim = strip_colors(m.group(5)).lower()
        bump(victim, "deaths")
        # A player blowing themselves up logs killer == victim; that's a death
        # only, never a kill.
        if killer != victim:
            bump(killer, "kills")

    return tally, new_pos


def merge_et_kd(store, tally):
    for name, counts in tally.items():
        entry = store.setdefault(name, {"kills": 0, "deaths": 0})
        entry["kills"] += counts["kills"]
        entry["deaths"] += counts["deaths"]


def kd_ratio(kills, deaths):
    return round(kills / deaths, 2) if deaths else float(kills)


def et_kd_for(name):
    """Cumulative kills/deaths for one player, keyed case-insensitively."""
    with _playtime_lock:
        entry = _playtime["et_kd"].get(name.strip().lower())
        kills = entry["kills"] if entry else 0
        deaths = entry["deaths"] if entry else 0
    return {"kills": kills, "deaths": deaths, "kd": kd_ratio(kills, deaths)}


def get_status(server_key=DEFAULT_ET_SERVER):
    server = resolve_et_server(server_key)
    key = server["label"]
    now = time.time()

    cached = _cache.get(key)
    if cached is not None and now < cached["expires"]:
        return cached["data"]

    try:
        result = query_et_server(ET_HOST, server["port"], QUERY_TIMEOUT)
    except (socket.timeout, OSError, ValueError):
        result = {"online": False}

    if result.get("online"):
        teams = get_et_teams(server)
        for player in result["players"]["list"]:
            player["team"] = teams.get(player["name"].lower())
            player.update(et_kd_for(player["name"]))

    result["server"] = key
    result["address"] = server["address"]
    result["checked_at"] = int(now)
    _cache[key] = {"data": result, "expires": now + CACHE_SECONDS}
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
    # Keys added after the first version of this file are read defensively, so
    # an older playtime.json just starts them empty rather than failing.
    if PLAYTIME_FILE.exists():
        try:
            with open(PLAYTIME_FILE) as f:
                data = json.load(f)
                # et_log_pos was a single int back when there was only one ET
                # server; carry that value over as Jaymod's offset so the
                # backlog isn't re-counted on upgrade.
                log_pos = data.get("et_log_pos", {})
                if isinstance(log_pos, int):
                    log_pos = {DEFAULT_ET_SERVER: log_pos}
                return {
                    "et": data.get("et", {}),
                    "mc": data.get("mc", {}),
                    "et_seen": data.get("et_seen", {}),
                    "et_kd": data.get("et_kd", {}),
                    "et_log_pos": log_pos,
                }
        except (OSError, ValueError):
            pass
    return {"et": {}, "mc": {}, "et_seen": {}, "et_kd": {}, "et_log_pos": {}}


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
        # Playtime and K/D are pooled across both ET servers: it is one
        # community playing the same maps under two mods, and a player's
        # totals shouldn't reset because they hopped from Jaymod to NitMod.
        et_names = []
        kill_tallies = {}
        for key, server in ET_SERVERS.items():
            try:
                et_result = query_et_server(ET_HOST, server["port"], QUERY_TIMEOUT)
                et_names.extend(p["name"] for p in et_result["players"]["list"])
            except (socket.timeout, OSError, ValueError):
                pass

            # Scanning happens outside the lock: on the very first poll this
            # reads the whole backlog of that server.log, which takes a moment.
            with _playtime_lock:
                log_pos = _playtime["et_log_pos"].get(key, 0)
            kill_tallies[key] = scan_et_kills(server, log_pos)

        _, mc_players = query_mc_status()

        now = int(time.time())
        with _playtime_lock:
            # A player on both servers at once is still one person, so their
            # playtime advances by one interval, not two.
            for name in set(et_names):
                _playtime["et"][name] = _playtime["et"].get(name, 0) + POLL_INTERVAL
                _playtime["et_seen"][name] = now
            for p in mc_players:
                _playtime["mc"][p["name"]] = _playtime["mc"].get(p["name"], 0) + POLL_INTERVAL
            for key, (tally, log_pos) in kill_tallies.items():
                merge_et_kd(_playtime["et_kd"], tally)
                _playtime["et_log_pos"][key] = log_pos
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


def get_et_player_details(et_playtime):
    with _playtime_lock:
        seen = dict(_playtime["et_seen"])
        kd = {name: dict(counts) for name, counts in _playtime["et_kd"].items()}

    result = {}
    for name, seconds in et_playtime.items():
        counts = kd.get(name.strip().lower(), {"kills": 0, "deaths": 0})
        result[name] = {
            "playtime_seconds": seconds,
            "kills": counts["kills"],
            "deaths": counts["deaths"],
            "kd": kd_ratio(counts["kills"], counts["deaths"]),
            "last_seen": seen.get(name),
        }
    return result


def get_playtime():
    with _playtime_lock:
        et = dict(_playtime["et"])
        mc = dict(_playtime["mc"])
    return {
        "et": et,
        "mc": mc,
        "et_details": get_et_player_details(et),
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


def get_console_log(server, limit=CONSOLE_LOG_LINE_LIMIT):
    """Tail the ET server's console log, keeping only player/admin-relevant
    lines (joins, chat, rcon commands, kicks) and dropping bot gameplay
    telemetry (Item/Kill/Ammo/Userinfo spam etc.)."""
    log_file = server["log"]
    if not log_file.exists():
        return []

    with open(log_file, "rb") as f:
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


def et_power(server, action):
    """Start/stop/restart a game server's systemd user unit.

    The unit name comes from the server registry and the action from a fixed
    set, so neither is ever built out of request text; the command is run as an
    argument list with no shell involved. Returns (ok, message)."""
    if action not in ET_POWER_ACTIONS:
        return False, "invalid action"

    try:
        result = subprocess.run(
            ["systemctl", "--user", action, server["unit"]],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "systemctl timed out"
    except OSError as exc:
        return False, f"could not run systemctl: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[0] if detail else f"systemctl exited {result.returncode}"

    return True, f"{server['label']} {action} requested."


def et_unit_active(server):
    """Whether the game server's unit is currently running. Distinct from the
    UDP status probe: a unit can be up while the server is still loading a map
    and not yet answering getstatus."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", server["unit"]],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() == "active"


def mc_system_unit_active():
    try:
        result = subprocess.run(["systemctl", "is-active", MC_SYSTEM_UNIT],
                                capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: err on the side of not starting a second server
    return result.stdout.strip() == "active"


def send_rcon_command(command, server, timeout=RCON_TIMEOUT):
    password = server["rcon_password_file"].read_text().strip()
    packet = b"\xff\xff\xff\xffrcon " + password.encode() + b" " + command.encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ET_HOST, server["port"]))
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
    """Write a console command to the server's stdin pipe.

    Opened non-blocking: with the server stopped nothing holds the pipe's read
    end, and a plain open() would then hang this request thread forever.
    Raises OSError (ENXIO) in that case instead."""
    fd = os.open(MC_PIPE_FILE, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (command + "\n").encode())
    finally:
        os.close(fd)


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

    def _proxy_map(self):
        """Pass a GET through to BlueMap's webserver, streaming the body.
        Map tiles are stored gzip-compressed and served with
        Content-Encoding: gzip, so they are relayed byte-for-byte."""
        conn = http.client.HTTPConnection(MAP_HOST, MAP_PORT, timeout=MAP_TIMEOUT)
        try:
            headers = {h: self.headers[h] for h in MAP_REQUEST_HEADERS if self.headers.get(h)}
            conn.request("GET", self.path[len("/map"):], headers=headers)
            res = conn.getresponse()
            self.send_response(res.status)
            for h in MAP_RESPONSE_HEADERS:
                value = res.getheader(h)
                if value:
                    self.send_header(h, value)
            if res.getheader("Content-Length") is None:
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = res.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            if not self.wfile.closed:
                try:
                    self._send_json({"error": "map unavailable"}, status=502)
                except OSError:
                    pass
        finally:
            conn.close()

    def _query_param(self, name):
        query = urllib.parse.urlparse(self.path).query
        values = urllib.parse.parse_qs(query).get(name)
        return values[0] if values else None

    def do_GET(self):
        if self.path.startswith("/playtime"):
            self._send_json(get_playtime())
        elif self.path.startswith("/mc-xp"):
            self._send_json(get_mc_xp())
        elif self.path.startswith("/servers"):
            self._send_json({
                "servers": [
                    {
                        "key": key,
                        "label": srv["label"],
                        "address": srv["address"],
                        "features": sorted(srv["features"]),
                    }
                    for key, srv in ET_SERVERS.items()
                ],
                "default": DEFAULT_ET_SERVER,
            })
        elif self.path.startswith("/status"):
            self._send_json(get_status(self._query_param("server")))
        elif self.path.startswith("/mc-roster"):
            roster = get_mc_roster()
            players = []
            for player in roster["players"]:
                snap = get_mc_player_snapshot(player["uuid"]) if player["uuid"] else {}
                players.append(dict(player, xp_level=snap.get("xp_level"), pos=snap.get("pos")))
            self._send_json(dict(roster, players=players))
        elif self.path == "/map":
            self.send_response(301)
            self.send_header("Location", "/map/")
            self.end_headers()
        elif self.path.startswith("/map/"):
            self._proxy_map()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        try:
            self._handle_post()
        except OSError as exc:
            # Mostly send_mc_command finding no server on the pipe (ENXIO).
            if self.path.startswith("/admin/mc-") and getattr(exc, "errno", None) == 6:
                self._send_json({"error": "Minecraft server is not running"}, status=503)
            else:
                raise

    def _handle_post(self):
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

        # Every ET action names the server it targets. An unknown name is
        # rejected outright rather than silently falling back, so a typo can
        # never quietly kick people off the wrong server; a missing one still
        # means Jaymod, for older cached copies of the page.
        et_server_key = body.get("server", DEFAULT_ET_SERVER)
        if self.path.startswith("/admin/et-") or self.path.startswith("/admin/console") \
                or self.path.startswith("/admin/say") or self.path.endswith("-password"):
            if not is_known_et_server(et_server_key):
                self._send_json({"error": "unknown server"}, status=400)
                return
        et_server = resolve_et_server(et_server_key)

        if self.path.startswith("/admin/set-password"):
            password = body.get("password", "")
            if not is_safe_console_string(password):
                self._send_json({"error": "invalid password"}, status=400)
                return
            send_rcon_command(f'g_password "{password}"', et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/clear-password"):
            send_rcon_command('g_password ""', et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/say"):
            message = body.get("message", "")
            if not is_safe_console_string(message):
                self._send_json({"error": "invalid message"}, status=400)
                return
            send_rcon_command(f'say "{message}"', et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/console"):
            self._send_json({"lines": get_console_log(et_server)})

        elif self.path.startswith("/admin/et-kick"):
            player = body.get("player", "")
            if not is_safe_console_string(player):
                self._send_json({"error": "invalid player name"}, status=400)
                return
            send_rcon_command(f"kick {player}", et_server)
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
            send_rcon_command(f"kick {player} {minutes}", et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-map"):
            map_name = body.get("map", "")
            if map_name not in ET_ALLOWED_MAPS:
                self._send_json({"error": "invalid map"}, status=400)
                return
            send_rcon_command(f"map {map_name}", et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-shuffle"):
            send_rcon_command("shuffleteams", et_server)
            self._send_json({"ok": True})

        # Checked before the singular route below: "/admin/et-features" also
        # starts with "/admin/et-feature", so the plural has to match first.
        elif self.path.startswith("/admin/et-features"):
            self._send_json({
                "features": {c: sorted(v) for c, v in et_server["features"].items()},
            })

        elif self.path.startswith("/admin/et-feature"):
            # Only cvars this server declares, and only the exact values it
            # declares for them. The name is never taken from the request as
            # free text — it has to be a key that is already in the table.
            cvar = body.get("cvar", "")
            value = str(body.get("value", ""))
            allowed = et_server["features"].get(cvar)
            if allowed is None:
                self._send_json({"error": "unsupported setting on this server"}, status=400)
                return
            if value not in allowed:
                self._send_json({"error": "invalid value"}, status=400)
                return
            send_rcon_command(f"{cvar} {value}", et_server)
            self._send_json({"ok": True})

        elif self.path.startswith("/admin/et-power"):
            action = body.get("action", "")
            if action not in ET_POWER_ACTIONS:
                self._send_json({"error": "invalid action"}, status=400)
                return
            # Warn players before pulling the server out from under them.
            # Best-effort: if the server is already down the rcon just times
            # out, which is fine — the stop still goes ahead.
            if action in ("stop", "restart"):
                try:
                    send_rcon_command(
                        f'say "^1Server {action} requested by an admin."', et_server)
                except OSError:
                    pass
            ok, message = et_power(et_server, action)
            if not ok:
                self._send_json({"error": message}, status=500)
                return
            self._send_json({
                "ok": True,
                "message": message,
                "active": et_unit_active(et_server),
            })

        elif self.path.startswith("/admin/mc-power"):
            action = body.get("action", "")
            if action not in ET_POWER_ACTIONS:
                self._send_json({"error": "invalid action"}, status=400)
                return
            if mc_system_unit_active():
                self._send_json({"error": "Minecraft still runs as a system service; "
                                          "it must be moved to a user service first"}, status=409)
                return
            if action in ("stop", "restart"):
                try:
                    send_mc_command(f"say Server {action} requested by an admin.")
                except OSError:
                    pass  # already down; the stop still goes ahead
            ok, message = et_power(MC_SERVER, action)
            if not ok:
                self._send_json({"error": message}, status=500)
                return
            self._send_json({"ok": True, "message": message, "active": et_unit_active(MC_SERVER)})

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
