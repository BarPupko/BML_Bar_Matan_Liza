"""Every player who has ever been on the Minecraft server.

The stats server's poller only knows players it happened to see online since
it started tracking. This builds the full list from the sources that go all
the way back:

  - the server logs (every rotated .log.gz plus latest.log), for names,
    UUIDs, join/leave times and session counts since the server was set up;
  - world/stats/<uuid>.json, for all-time playtime/deaths/kills, which also
    covers players from the 2024 world that predates these logs;
  - world/playerdata/<uuid>.dat mtime, as last-seen for those older players;
  - whitelist.json / ops.json, to show who is currently allowed in.

UUIDs that never appear in the logs (2024-era players) are resolved to names
through Mojang's public profile API once and cached on disk.

Connection IPs are read from the logs only to skip them — nothing about where
a player connects from is ever returned.
"""

import gzip
import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

MC_DIR = Path("/home/plex/minecraft-server")
LOG_DIR = MC_DIR / "logs"
WORLD_DIR = MC_DIR / "world"
NAME_CACHE_FILE = Path(__file__).parent / "mc_names_cache.json"

CACHE_SECONDS = 300
TICKS_PER_SECOND = 20

LINE_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] \[[^\]]+\]: (.*)$")
UUID_RE = re.compile(r"^UUID of player (\w{1,16}) is ([0-9a-f-]{36})$")
JOIN_RE = re.compile(r"^(\w{1,16})\[/[^\]]+\] logged in with entity id")
LEAVE_RE = re.compile(r"^(\w{1,16}) left the game$")
STOP_RE = re.compile(r"^Stopping (the )?server$")
LOG_NAME_RE = re.compile(r"^(\d{4}-\d\d-\d\d)-(\d+)\.log\.gz$")

_lock = threading.Lock()
_cache = {"data": None, "expires": 0}


def _log_files():
    """(start date, path) for every log, oldest first, latest.log last."""
    rotated = []
    for path in LOG_DIR.glob("*.log.gz"):
        m = LOG_NAME_RE.match(path.name)
        if m:
            rotated.append((m.group(1), int(m.group(2)), path))
    rotated.sort()
    files = [(datetime.strptime(d, "%Y-%m-%d"), p) for d, _, p in rotated]
    latest = LOG_DIR / "latest.log"
    if latest.exists():
        # latest.log is rolled when the server starts, so it began on the day
        # of the most recent start; its first timestamp pins the date.
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        files.append((mtime.replace(hour=0, minute=0, second=0, microsecond=0), latest))
    return files


def _read_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _scan_logs():
    """{name: {"uuid", "first_seen", "last_seen", "sessions", "log_seconds"}}"""
    players = {}
    files = _log_files()
    for index, (day, path) in enumerate(files):
        lines = _read_lines(path)
        is_latest = index == len(files) - 1 and path.name == "latest.log"
        if is_latest and lines:
            # latest.log can predate its mtime's day if the server ran across
            # midnight; count back the midnights crossed inside the file.
            crossings, prev = 0, None
            for line in lines:
                m = LINE_RE.match(line)
                if m:
                    t = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    if prev and t < prev:
                        crossings += 1
                    prev = t
            day -= timedelta(days=crossings)

        open_sessions = {}
        prev_t = None
        for line in lines:
            m = LINE_RE.match(line)
            if not m:
                continue
            t = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if prev_t and t < prev_t:
                day += timedelta(days=1)
            prev_t = t
            ts = int((day + timedelta(hours=t[0], minutes=t[1], seconds=t[2])).timestamp())
            msg = m.group(4)

            u = UUID_RE.match(msg)
            if u:
                players.setdefault(u.group(1), {})["uuid"] = u.group(2)
                continue
            j = JOIN_RE.match(msg)
            if j:
                p = players.setdefault(j.group(1), {})
                p["sessions"] = p.get("sessions", 0) + 1
                p.setdefault("first_seen", ts)
                p["last_seen"] = ts
                open_sessions[j.group(1)] = ts
                continue
            lv = LEAVE_RE.match(msg)
            if lv and lv.group(1) in open_sessions:
                name = lv.group(1)
                p = players[name]
                p["log_seconds"] = p.get("log_seconds", 0) + ts - open_sessions.pop(name)
                p["last_seen"] = ts
                continue
            if STOP_RE.match(msg):
                for name, start in open_sessions.items():
                    p = players[name]
                    p["log_seconds"] = p.get("log_seconds", 0) + ts - start
                    p["last_seen"] = ts
                open_sessions.clear()

        if is_latest:
            now = int(time.time())
            for name, start in open_sessions.items():
                players[name]["online"] = True
                players[name]["last_seen"] = now
    return {name: p for name, p in players.items() if p.get("sessions")}


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _stats_for(uuid):
    data = _load_json(WORLD_DIR / "stats" / f"{uuid}.json", {})
    custom = data.get("stats", {}).get("minecraft:custom", {})
    if not custom:
        return None
    return {
        "playtime_seconds": custom.get("minecraft:play_time", 0) // TICKS_PER_SECOND,
        "deaths": custom.get("minecraft:deaths", 0),
        "mob_kills": custom.get("minecraft:mob_kills", 0),
    }


def _resolve_names(uuids):
    """Name for each UUID via Mojang, cached forever on disk (names can
    change, but for an old roster the last-known name is good enough)."""
    cache = _load_json(NAME_CACHE_FILE, {})
    changed = False
    for uuid in uuids:
        if uuid in cache:
            continue
        url = "https://sessionserver.mojang.com/session/minecraft/profile/" + uuid.replace("-", "")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BML-stats-server"})
            with urllib.request.urlopen(req, timeout=5) as res:
                cache[uuid] = json.loads(res.read()).get("name")
        except (OSError, ValueError):
            continue  # try again on the next rebuild
        changed = True
    if changed:
        tmp = NAME_CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(cache, f)
        tmp.replace(NAME_CACHE_FILE)
    return cache


def _build():
    from_logs = _scan_logs()
    by_uuid = {}
    for name, p in from_logs.items():
        uuid = p.get("uuid")
        key = uuid or f"name:{name}"
        entry = by_uuid.setdefault(key, {"uuid": uuid, "name": name})
        # A renamed account shows up under two names with one UUID; keep the
        # most recent name and merge the rest.
        if p.get("last_seen", 0) >= entry.get("last_seen", 0):
            entry["name"] = name
        entry["sessions"] = entry.get("sessions", 0) + p.get("sessions", 0)
        entry["log_seconds"] = entry.get("log_seconds", 0) + p.get("log_seconds", 0)
        entry["first_seen"] = min(entry.get("first_seen", p["first_seen"]), p["first_seen"])
        entry["last_seen"] = max(entry.get("last_seen", 0), p.get("last_seen", 0))
        entry["online"] = entry.get("online", False) or p.get("online", False)

    # Players with saved data but no log history: the 2024 world's players.
    for dat in (WORLD_DIR / "playerdata").glob("*.dat"):
        uuid = dat.stem
        if uuid not in by_uuid:
            by_uuid[uuid] = {
                "uuid": uuid, "name": None, "sessions": None, "log_seconds": None,
                "first_seen": None, "last_seen": int(dat.stat().st_mtime), "online": False,
            }

    names = _resolve_names([u for u, e in by_uuid.items() if e["name"] is None])
    whitelist = {e.get("uuid") for e in _load_json(MC_DIR / "whitelist.json", [])}
    ops = {e.get("uuid") for e in _load_json(MC_DIR / "ops.json", [])}
    banned = {e.get("uuid") for e in _load_json(MC_DIR / "banned-players.json", [])}

    roster = []
    for entry in by_uuid.values():
        uuid = entry["uuid"]
        if entry["name"] is None:
            entry["name"] = names.get(uuid) or uuid[:8]
        stats = _stats_for(uuid) if uuid else None
        roster.append({
            "name": entry["name"],
            "uuid": uuid,
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
            "sessions": entry["sessions"],
            # World stats count every tick ever played, including the 2024
            # world; the log total is only a fallback for players without it.
            "playtime_seconds": stats["playtime_seconds"] if stats else entry["log_seconds"],
            "deaths": stats["deaths"] if stats else None,
            "mob_kills": stats["mob_kills"] if stats else None,
            "online": entry["online"],
            "whitelisted": uuid in whitelist,
            "op": uuid in ops,
            "banned": uuid in banned,
        })
    roster.sort(key=lambda p: (-(p["last_seen"] or 0), p["name"].lower()))
    return roster


def get_mc_roster():
    now = time.time()
    with _lock:
        if _cache["data"] is None or now >= _cache["expires"]:
            _cache["data"] = {"players": _build(), "updated_at": int(now)}
            _cache["expires"] = now + CACHE_SECONDS
        return _cache["data"]
