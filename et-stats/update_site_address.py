#!/usr/bin/env python3
"""Keep the published site pointing at this machine.

Two things the site hardcodes change on their own:
  - the home public IP (dynamic, assigned by the ISP), used for the
    Minecraft/ET join addresses;
  - the Cloudflare quick-tunnel URL for the stats server, which gets a new
    random hostname every time et-stats-tunnel.service restarts.

Run periodically (et-site-address.timer). When either differs from what the
site currently has, the new value is written into every file that carries it,
committed and pushed to GitHub, which redeploys GitHub Pages.

The commit is made in a dedicated git worktree checked out from origin/main,
never in the main working tree, so whatever is being edited there by hand is
never swept into an automated commit. The main tree is then fast-forwarded
if it is clean, or just has the same substitution applied if it is not.
"""

import fcntl
import ipaddress
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKTREE = Path.home() / ".cache" / "et-site-address" / "worktree"
LOCK_FILE = Path.home() / ".cache" / "et-site-address" / "lock"

# Every file that carries the IP or the tunnel URL, relative to the repo root.
SITE_FILES = ["index.html", "et.html", "join.html", "et-stats/et_stats_server.py"]
SERVER_FILE = "et-stats/et_stats_server.py"

IP_SOURCES = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]
TUNNEL_UNIT = "et-stats-tunnel.service"
TUNNEL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
HTTP_TIMEOUT = 10

CURRENT_IP_RE = re.compile(r'^PUBLIC_IP = "([0-9.]+)"', re.M)
CURRENT_TUNNEL_RE = re.compile(r"const STATS_BASE_URL = '([^']+)'")


def log(msg):
    print(msg, flush=True)


def run(*args, cwd=REPO, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "et-site-address/1"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
        return res.status, res.read().decode().strip()


def detect_public_ip():
    """First answer from a fallback list that parses as a public IPv4."""
    for url in IP_SOURCES:
        try:
            _, text = fetch(url)
            ip = ipaddress.IPv4Address(text)
        except (OSError, ValueError):
            continue
        if ip.is_global:
            return str(ip)
    return None


def detect_tunnel_url():
    """The URL the tunnel's *current* run announced, if it actually answers.

    Only log lines from the running invocation count, so a URL from before
    the last restart is never picked up."""
    invocation = run("systemctl", "--user", "show", "-p", "InvocationID", "--value",
                     TUNNEL_UNIT, check=False).stdout.strip()
    if not invocation:
        return None
    logs = run("journalctl", "--user", "--no-pager", "-o", "cat",
               f"_SYSTEMD_INVOCATION_ID={invocation}", check=False).stdout
    urls = TUNNEL_RE.findall(logs)
    if not urls:
        return None
    url = urls[-1]
    try:
        status, _ = fetch(url + "/playtime")
    except OSError:
        return None
    return url if status == 200 else None


def substitute(root, replacements):
    """Apply old->new replacements to the site files under root.
    Returns the list of files that changed."""
    changed = []
    for name in SITE_FILES:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text()
        new = text
        for old, repl in replacements:
            if re.fullmatch(r"[0-9.]+", old):
                # An IP must not match inside a longer number.
                new = re.sub(rf"(?<![0-9.]){re.escape(old)}(?![0-9])", repl, new)
            else:
                new = new.replace(old, repl)
        if new != text:
            path.write_text(new)
            changed.append(name)
    return changed


def prepare_worktree():
    run("git", "fetch", "--quiet", "origin")
    if not (WORKTREE / ".git").exists():
        run("git", "worktree", "prune")
        WORKTREE.parent.mkdir(parents=True, exist_ok=True)
        run("git", "worktree", "add", "--detach", str(WORKTREE), "origin/main")
    run("git", "checkout", "--quiet", "--detach", "origin/main", cwd=WORKTREE)
    run("git", "reset", "--quiet", "--hard", "origin/main", cwd=WORKTREE)
    run("git", "clean", "--quiet", "-fd", cwd=WORKTREE)


def current_values(root):
    server = (root / SERVER_FILE).read_text()
    index = (root / "index.html").read_text()
    ip = CURRENT_IP_RE.search(server)
    tunnel = CURRENT_TUNNEL_RE.search(index)
    return (ip.group(1) if ip else None), (tunnel.group(1) if tunnel else None)


def sync_main_tree(replacements):
    """Bring the hand-edited checkout in line with what was just pushed."""
    dirty = run("git", "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if not dirty and run("git", "merge-base", "--is-ancestor", "HEAD", "origin/main",
                         check=False).returncode == 0:
        run("git", "merge", "--quiet", "--ff-only", "origin/main")
        log("main tree fast-forwarded")
        return
    # Uncommitted work (or diverged history) in the main tree: leave git alone
    # and just make the same edit there. Upstream carries the identical change,
    # so the next merge of origin/main applies cleanly.
    changed = substitute(REPO, replacements)
    log(f"main tree has local changes; applied substitution to {changed or 'nothing'}")


def main():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is in progress")
        return 0

    new_ip = detect_public_ip()
    new_tunnel = detect_tunnel_url()
    if not new_ip:
        log("could not determine public IP; leaving IP as is")
    if not new_tunnel:
        log("tunnel URL unknown or not answering; leaving tunnel URL as is")

    prepare_worktree()
    old_ip, old_tunnel = current_values(WORKTREE)

    replacements = []
    summary = []
    if new_ip and old_ip and new_ip != old_ip:
        replacements.append((old_ip, new_ip))
        summary.append(f"IP {old_ip} -> {new_ip}")
    if new_tunnel and old_tunnel and new_tunnel != old_tunnel:
        replacements.append((old_tunnel, new_tunnel))
        summary.append(f"stats tunnel -> {new_tunnel}")

    if not replacements:
        log(f"up to date (ip={old_ip}, tunnel={old_tunnel})")
        return 0

    changed = substitute(WORKTREE, replacements)
    if not changed:
        log("nothing to change in site files")
        return 0

    message = "Auto-update site address: " + "; ".join(summary)
    run("git", "add", "--", *changed, cwd=WORKTREE)
    run("git", "commit", "--quiet", "-m", message, cwd=WORKTREE)
    run("git", "push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=WORKTREE)
    run("git", "fetch", "--quiet", "origin")
    log(f"pushed: {message} ({', '.join(changed)})")

    server_before = (REPO / SERVER_FILE).read_text()
    sync_main_tree(replacements)
    if (REPO / SERVER_FILE).read_text() != server_before:
        # The stats server reads PUBLIC_IP at import time.
        run("systemctl", "--user", "restart", "et-stats.service", check=False)
        log("restarted et-stats.service")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        log(f"command failed: {' '.join(exc.cmd)}\n{exc.stderr.strip()}")
        sys.exit(1)
