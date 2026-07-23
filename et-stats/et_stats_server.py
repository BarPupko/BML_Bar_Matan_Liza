#!/usr/bin/env python3
"""Tiny HTTP status server for a Wolfenstein: Enemy Territory (idtech3) game server.

Queries the ET server's UDP "getstatus" protocol and exposes the parsed
result as JSON over HTTP, with CORS enabled, so a static webpage can fetch it.
"""

import json
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ET_HOST = "127.0.0.1"  # the server runs on this same machine; using the public
                       # IP fails here due to NAT hairpinning on most routers
ET_PORT = 27960
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8099
QUERY_TIMEOUT = 2.0
CACHE_SECONDS = 5

COLOR_CODE_RE = re.compile(r"\^(x[0-9A-Fa-f]{6}|.)")
PLAYER_LINE_RE = re.compile(r'^(-?\d+)\s+(-?\d+)\s+"(.*)"$')

_cache = {"data": None, "expires": 0}


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
        if self.path.startswith("/status"):
            self._send_json(get_status())
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"ET stats server listening on http://{LISTEN_HOST}:{LISTEN_PORT}/status")
    server.serve_forever()
