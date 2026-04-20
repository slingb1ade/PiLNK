#!/bin/bash
# PiLNK OTA Update Script
# Location: ~/pilnk/update.sh
# Called by app.py when a new version is detected

PILNK_DIR="$HOME/pilnk"
LOG_FILE="$PILNK_DIR/update.log"
BACKUP_BRANCH="pre-update-backup"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "[PILNK-OTA] $1"
}

cd "$PILNK_DIR" || { log "ERROR: Cannot cd to $PILNK_DIR"; exit 1; }

log "=== OTA UPDATE STARTED ==="
log "Current version: $(cat VERSION 2>/dev/null || echo 'unknown')"

# Step 1: Stash any local changes (config.json is gitignored so it's safe)
log "Stashing local changes..."
git stash 2>> "$LOG_FILE"

# Step 2: Pull latest from GitHub
log "Pulling latest from GitHub..."
git pull origin main 2>> "$LOG_FILE"
PULL_RESULT=$?

if [ $PULL_RESULT -ne 0 ]; then
    log "ERROR: git pull failed (exit code $PULL_RESULT)"
    log "Attempting git stash pop to restore..."
    git stash pop 2>> "$LOG_FILE"
    log "=== OTA UPDATE FAILED ==="
    exit 1
fi

NEW_VERSION=$(cat VERSION 2>/dev/null || echo 'unknown')
log "Updated to version: $NEW_VERSION"

# Step 3: Comment out whisper import (safety — in case it got uncommented)
if grep -q "^from whisper_atc" app.py 2>/dev/null; then
    sed -i 's/^from whisper_atc/# from whisper_atc/' app.py
    log "Whisper import commented out (safety)"
fi

# Step 4: Restart the PiLNK service
log "Restarting PiLNK service..."
sudo systemctl restart pilnk 2>> "$LOG_FILE"

# Step 5: Wait and verify service is running
sleep 5
if systemctl is-active --quiet pilnk; then
    log "Service restarted successfully"
    log "=== OTA UPDATE COMPLETE — v$NEW_VERSION ==="
    exit 0
else
    log "WARNING: Service may not have started cleanly"
    log "Check: sudo journalctl -u pilnk -n 20 --no-pager"
    log "=== OTA UPDATE COMPLETE (with warnings) ==="
    exit 2
fi
