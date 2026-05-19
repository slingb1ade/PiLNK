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
OLD_VERSION=$(cat VERSION 2>/dev/null || echo 'unknown')
log "Current version: $OLD_VERSION"

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

# ── GUARDRAIL #1 (May 2026): Rule #28 sync-mismatch detector ──
# If git pull succeeded but VERSION didn't change, the remote
# api/version.php reports a newer version than what's actually
# in the GitHub repo. Restarting now would just loop forever —
# every restart re-detects the same "available update" and
# tries again. Abort BEFORE the restart and let app.py's 1-hour
# cooldown kick in. The loop dies on first iteration.
if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    log "ABORT: git pull succeeded but VERSION unchanged (still $OLD_VERSION)."
    log "This usually means api/version.php reports a newer version than"
    log "what's actually been pushed to GitHub (Rule #28 violation)."
    log "Skipping service restart to prevent infinite OTA loop."
    log "Will retry after 1-hour cooldown — by which time the version"
    log "anchor and the git repo should be back in sync."
    log "=== OTA UPDATE ABORTED — Rule #28 sync mismatch ==="
    exit 3
fi

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
