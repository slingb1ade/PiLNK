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

# Step 1: Fetch the latest refs from GitHub (no working-tree changes yet).
log "Fetching latest from GitHub..."
git fetch origin main 2>> "$LOG_FILE"
FETCH_RESULT=$?

if [ $FETCH_RESULT -ne 0 ]; then
    log "ERROR: git fetch failed (exit code $FETCH_RESULT) — likely a network blip."
    log "Working tree untouched; nothing to restore. Will retry next cycle."
    log "=== OTA UPDATE FAILED — fetch error ==="
    exit 1
fi

# Step 2: Force the working tree to exactly match origin/main.
# reset --hard replaces the old stash+pull+pop dance ON PURPOSE:
#   - No stash => no stash accumulation and no stale-stash pop (the old failure
#     mode: a leftover stash getting popped on a later pull failure, dirtying
#     the tree and looping the node on every subsequent update).
#   - reset --hard ONLY touches TRACKED files. Every per-node file is gitignored
#     (config.json, .secret_key, *.json runtime state, update.log, recordings/,
#     *.bak) so local config and state are preserved untouched.
#   - A node mirrors the official code; local edits to TRACKED files are
#     intentionally discarded (the old stash-and-abandon discarded them too).
log "Resetting working tree to origin/main..."
git reset --hard origin/main 2>> "$LOG_FILE"
RESET_RESULT=$?

if [ $RESET_RESULT -ne 0 ]; then
    log "ERROR: git reset --hard failed (exit code $RESET_RESULT). Tree unchanged."
    log "=== OTA UPDATE FAILED — reset error ==="
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

# Step 4: Restart the PiLNK service to load the new code.
log "Restarting PiLNK service..."

# COMPLETE must be EARNED, not defaulted — that was the whole bug.
#   PID_BEFORE: a genuine restart yields a NEW MainPID. An unchanged pid means
#     the restart no-op'd (the silent failure that used to log a false COMPLETE).
#   sudo -n (non-interactive): fails FAST + visibly if creds aren't there,
#     instead of hanging on a password prompt that never gets answered.
#   RC is captured on the VERY NEXT line — nothing between, or we'd read the
#     wrong command's exit code.
#   is-active is only a SECONDARY sanity confirm — it cannot tell a fresh
#     process from an old one that never died, so it never decides success.
PID_BEFORE=$(systemctl show -p MainPID --value pilnk 2>/dev/null)
sudo -n systemctl restart pilnk 2>> "$LOG_FILE"
RC=$?

# A successful restart usually KILLS this script here (it's a child of the
# pilnk service) and the fresh instance takes over — nothing below runs, which
# is fine. If we DO reach here, the PID comparison is the source of truth.
sleep 5
PID_AFTER=$(systemctl show -p MainPID --value pilnk 2>/dev/null)

if [ "$RC" -eq 0 ] && [ -n "$PID_AFTER" ] && [ "$PID_AFTER" != "0" ] && [ "$PID_AFTER" != "$PID_BEFORE" ]; then
    # EARNED: restart returned 0 AND a NEW MainPID is running.
    if systemctl is-active --quiet pilnk; then
        log "Service restarted successfully (PID $PID_BEFORE -> $PID_AFTER)"
        log "=== OTA UPDATE COMPLETE — v$NEW_VERSION ==="
        exit 0
    fi
    log "ERROR: new PID $PID_AFTER but service not active — check the journal."
    log "Check: sudo journalctl -u pilnk -n 20 --no-pager"
    log "=== OTA UPDATE FAILED — service not active after restart ==="
    exit 2
elif [ "$RC" -ne 0 ]; then
    log "ERROR: 'sudo -n systemctl restart pilnk' failed (exit $RC)."
    log "No passwordless sudo for the restart on this host (Debian Trixie/Bookworm,"
    log "Ubuntu, amd64). New code is on disk (v$NEW_VERSION) but the SERVICE DID NOT"
    log "RESTART — still running the OLD code (PID ${PID_BEFORE:-unknown})."
    log "Fix: run 'sudo systemctl restart pilnk' once, or re-run install.sh to install"
    log "the NOPASSWD sudoers rule so future OTAs restart unattended."
    log "=== OTA UPDATE FAILED — restart blocked (no passwordless sudo) ==="
    exit 4
else
    # RC==0 but MainPID did NOT change → the restart was a no-op. This is the
    # precise silent failure that used to log a false COMPLETE. Refuse it.
    log "ERROR: restart returned 0 but MainPID is unchanged (${PID_BEFORE:-unknown})"
    log "— the service did NOT actually restart. Refusing to log COMPLETE."
    log "Check: sudo journalctl -u pilnk -n 20 --no-pager"
    log "=== OTA UPDATE FAILED — restart no-op (PID unchanged) ==="
    exit 5
fi
