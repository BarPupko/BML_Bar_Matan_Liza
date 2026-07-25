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

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8099
QUERY_TIMEOUT = 2.0
CACHE_SECONDS = 5

POLL_INTERVAL = 60  # seconds between playtime polls
PLAYTIME_FILE = Path(__file__).parent / "playtime.json"

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
        with urllib.request.urlopen(MC_STATUS_URL, timeout=5) as resp:
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

        _, mc_names = query_mc_names()

        with _playtime_lock:
            for name in et_names:
                _playtime["et"][name] = _playtime["et"].get(name, 0) + POLL_INTERVAL
            for name in mc_names:
                _playtime["mc"][name] = _playtime["mc"].get(name, 0) + POLL_INTERVAL
            save_playtime(_playtime)

        time.sleep(POLL_INTERVAL)


def get_playtime():
    with _playtime_lock:
        return {
            "et": dict(_playtime["et"]),
            "mc": dict(_playtime["mc"]),
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
