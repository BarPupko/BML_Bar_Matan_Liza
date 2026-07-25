#!/usr/bin/env bash
# Daily backup of the Minecraft world and the ET server's persistent data
# (admin/level/user/map databases). Run via the backup.timer systemd unit.
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
KEEP=7

MC_DIR="/home/plex/minecraft-server"
MC_PIPE="$MC_DIR/mcpipe"
MC_BACKUP_DIR="/home/plex/backups/minecraft"

ET_DATA_DIR="/home/plex/.etlegacy/jaymod"
ET_BACKUP_DIR="/home/plex/backups/et"

mkdir -p "$MC_BACKUP_DIR" "$ET_BACKUP_DIR"

echo "[$(date)] Starting backup $TIMESTAMP"

# --- Minecraft: pause autosave, force a flush, snapshot, resume ---
if [ -p "$MC_PIPE" ]; then
    echo "save-off" > "$MC_PIPE"
    sleep 1
    echo "save-all flush" > "$MC_PIPE"
    sleep 5
    tar -czf "$MC_BACKUP_DIR/world-$TIMESTAMP.tar.gz" -C "$MC_DIR" world
    echo "save-on" > "$MC_PIPE"
    echo "[$(date)] Minecraft world backed up"
else
    echo "[$(date)] Minecraft pipe not found, skipping" >&2
fi

# --- ET: persistent data is just small db/config files, no special care needed ---
if [ -d "$ET_DATA_DIR" ]; then
    tar -czf "$ET_BACKUP_DIR/jaymod-data-$TIMESTAMP.tar.gz" -C "$ET_DATA_DIR" .
    echo "[$(date)] ET data backed up"
else
    echo "[$(date)] ET data dir not found, skipping" >&2
fi

# --- prune old backups beyond retention ---
for dir in "$MC_BACKUP_DIR" "$ET_BACKUP_DIR"; do
    ls -1t "$dir"/*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
done

echo "[$(date)] Backup complete"
