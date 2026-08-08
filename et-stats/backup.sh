#!/usr/bin/env bash
# Daily backup of the Minecraft world and the ET server's persistent data
# (admin/level/user/map databases). Run via the backup.timer systemd unit.
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
KEEP=7

MC_DIR="/home/plex/minecraft-server"
MC_PIPE="$MC_DIR/mcpipe"
MC_BACKUP_DIR="/home/plex/backups/minecraft"

# One entry per mod: each dedicated server keeps its own admin/level/user/map
# databases under its own fs_game directory.
ET_DATA_DIRS="jaymod nitmod"
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
for mod in $ET_DATA_DIRS; do
    dir="/home/plex/.etlegacy/$mod"
    if [ -d "$dir" ]; then
        tar -czf "$ET_BACKUP_DIR/$mod-data-$TIMESTAMP.tar.gz" -C "$dir" .
        echo "[$(date)] ET $mod data backed up"
    else
        echo "[$(date)] ET data dir $dir not found, skipping" >&2
    fi
done

# --- prune old backups beyond retention ---
# Pruned per archive prefix, not per directory: the ET directory holds one
# archive per mod per run, and a plain directory-wide prune would cut the
# retention window in half for each mod added.
# Callers pass an unquoted glob so it expands here; a pattern that matches
# nothing reaches ls literally, hence the "|| true" to keep pipefail happy.
prune() {
    ls -1t "$@" 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -- || true
}

prune $MC_BACKUP_DIR/world-*.tar.gz
for mod in $ET_DATA_DIRS; do
    prune $ET_BACKUP_DIR/$mod-data-*.tar.gz
done

echo "[$(date)] Backup complete"
