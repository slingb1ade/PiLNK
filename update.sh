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

# Step 3.5: Wire in (or update) the SDR self-heal machinery. Idempotent — does
# nothing after the first successful wire-in. Runs under the same passwordless
# sudo this script already uses for the restart below. Never blocks the update.
if [ -f "$PILNK_DIR/bootstrap-selfheal.sh" ]; then
    log "Running self-heal bootstrap..."
    PILNK_DIR="$PILNK_DIR" bash "$PILNK_DIR/bootstrap-selfheal.sh" 2>> "$LOG_FILE" || \
        log "self-heal bootstrap returned non-zero (continuing — non-fatal)"
fi

# Step 3.6: Build the ATC audio engine if this node hasn't got it yet.
#
# WHY THIS EXISTS. pilnkradio is C++ and has to be COMPILED. Everything else in
# PiLNK is Python or HTML, so a git pull is enough — this is the one component
# where "pushed to the fleet" and "working on the fleet" are different things.
# The source, the systemd unit, the udev rules and the dashboard tab have been
# on every node since July, but the tab is hidden until something answers on
# :5656, and nothing does until the binary exists. Net effect: no node except
# the developer's had working ATC audio, and because the tab self-hides there
# were no bug reports to reveal it.
#
# THREE RULES, all load-bearing:
#   DETACHED — a 3-5 minute compile must not sit between the git pull and the
#     service restart below. systemd-run hands it to systemd and returns at once,
#     so the OTA finishes at its normal speed and the build outlives this script.
#   FAIL-SOFT — every failure path here is non-fatal. A node that can't build the
#     audio engine is a node without ATC audio; it is NOT a node without ADS-B.
#     Nothing in this block may ever stop pilnk.service starting.
#   IDEMPOTENT — skipped entirely once the binary exists, so this costs nothing
#     on every subsequent update.
if [ ! -x /usr/local/bin/pilnkradio ] && [ -f "$PILNK_DIR/pilnkradio-install.sh" ]; then
    if command -v systemd-run >/dev/null 2>&1; then
        # Serial comes from the node's own config.json (install.sh records a free
        # dongle there as vhf_serial). Absent that, the engine waits and udev
        # wakes it when a dongle appears — so a node with no radio hardware today
        # is still ready the day its owner buys one.
        VHF_SN=$(python3 -c "import json,sys;print(json.load(open('$PILNK_DIR/config.json')).get('vhf_serial') or '')" 2>/dev/null || true)
        # The build must run as ROOT: the node's sudoers rule only grants
        # `systemctl {restart,start,stop} pilnk` and `daemon-reload`, so the
        # installer's `sudo make install` / `sudo ldconfig` / `sudo tee` would
        # all fail non-interactively as the service user.
        #
        # Root, however, leaves its build trees owned by root inside the user's
        # home — which would block a later manual re-run of the installer if
        # this build fails partway. So hand ownership back afterwards, whatever
        # the outcome, and preserve the real exit code.
        RUN_USER=$(id -un); RUN_GROUP=$(id -gn)
        log "ATC audio engine not present — scheduling detached build (takes several minutes)"
        sudo -n systemd-run \
            --unit=pilnk-audio-build \
            --description="PiLNK ATC audio engine build" \
            --property=Type=oneshot \
            --property=TimeoutStartSec=2700 \
            --setenv=PILNKRADIO_NONINTERACTIVE=1 \
            --setenv=PILNKRADIO_SERIAL="$VHF_SN" \
            --setenv=HOME="$HOME" \
            /bin/bash -c 'bash "$1"; rc=$?; chown -R "$2":"$3" "$HOME/rtl-sdr-blog" "$4/pilnkradio/build" 2>/dev/null || true; exit $rc' \
            _ "$PILNK_DIR/pilnkradio-install.sh" "$RUN_USER" "$RUN_GROUP" "$PILNK_DIR" \
            >> "$LOG_FILE" 2>&1 \
            && log "ATC audio build scheduled (journalctl -u pilnk-audio-build to follow)" \
            || log "could not schedule ATC audio build (continuing — non-fatal)"
    else
        log "systemd-run unavailable — skipping ATC audio build (non-fatal)"
    fi
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
