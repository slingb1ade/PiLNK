#!/bin/bash
# PiLNK OTA Update Script
# Location: ~/pilnk/update.sh
# Called by app.py when a new version is detected

PILNK_DIR="${PILNK_DIR:-$HOME/pilnk}"
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

# Step 0: Clean up after a power cut in the middle of an earlier git command.
# A cut can leave git's lock files behind (.git/index.lock during a reset,
# .git/refs/remotes/origin/main.lock during a fetch) or a half-written
# .git/index. git then refuses every later fetch and reset, so the node would
# never update again until someone fixed it by hand. If no git process is
# running at all, any lock is a leftover, and the index is only a cache of the
# commit: reset --hard below rebuilds it. With git running, touch nothing.
if pgrep -x git >/dev/null 2>&1; then
    log "A git process is running; leaving .git lock files and the index alone."
else
    while IFS= read -r stale; do
        rm -f -- "$stale" && log "Removed stale lock ${stale#./} (left by an interrupted git command)"
    done < <(find .git -name '*.lock' -type f 2>/dev/null)
    if ! git ls-files >/dev/null 2>&1; then
        rm -f .git/index
        log "The git index was unreadable (a write was cut off); removed it, reset --hard rebuilds it."
    fi
fi

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

# Step 1.5: Is the newest release signed by AJ? (signed-releases spec 5.5)
# pilnk.io serves AJ's signed release statement next to the version; the
# running tree's release-check.sh checks it against the running tree's
# release_signers. release_policy (also from the running tree, so only a signed
# release can change it) decides what the answer does:
#   report  - log + state file only; the update goes ahead exactly as before.
#   enforce - install only the signed commit; refuse anything else (exit 6),
#             leaving the tree as it is. The service keeps running either way.
RELEASE_URL="${PILNK_RELEASE_URL:-https://pilnk.io/api/version.php}"
POLICY=report
if [ "$(head -n 1 "$PILNK_DIR/release_policy" 2>/dev/null | tr -d '[:space:]')" = enforce ]; then
    POLICY=enforce
fi
RESET_TARGET=origin/main
SKIP_RULE28=0
SIG_TOKEN=error; SIG_RUN=none; SIGNED_VERSION=""; SIGNED_COMMIT=""
# SIG_WHY: when SIG_TOKEN is error, the step that failed (26 Nov 2026: one node
# refused every update with a bare "error" and only its owner could say why).
#   tmp      no temp folder
#   fetch:N  curl exit code N (6 = name not found, 7 = refused, 28 = timed out,
#            35 / 60 = TLS or certificate)
#   parse    the reply was not the release JSON
#   check    release-check.sh gave no answer; otherwise release-check.sh's own reason
SIG_WHY=""
REL_TMP=$(mktemp -d 2>/dev/null) || REL_TMP=""
trap '[ -n "$REL_TMP" ] && rm -rf "$REL_TMP"' EXIT
if [ -z "$REL_TMP" ]; then
    SIG_WHY=tmp
else
    # Up to three tries, 5 s apart: one slow reply from pilnk.io must not cost
    # the node its update (26 Nov 2026: a single 15 s timeout did, and the app
    # then waited out its 1-hour cooldown). Worst case adds 40 s, well inside
    # the app's 120 s limit for this script.
    RETRY_DELAY="${PILNK_RELEASE_RETRY_DELAY:-5}"
    for TRY in 1 2 3; do
        CURL_RC=0
        curl -fsS --max-time 15 -H 'User-Agent: PiLNK-OTA/1.0' -o "$REL_TMP/version.json" "$RELEASE_URL" 2>> "$LOG_FILE" \
            || CURL_RC=$?
        [ "$CURL_RC" = 0 ] && break
        [ "$TRY" = 3 ] && break
        log "Release details download failed (curl $CURL_RC), trying again in ${RETRY_DELAY}s"
        sleep "$RETRY_DELAY" 2>/dev/null
    done
    if [ "$CURL_RC" != 0 ]; then
        SIG_WHY="fetch:$CURL_RC"
    elif ! python3 - "$REL_TMP" <<'PY' 2>> "$LOG_FILE"
import json, os, sys
d = sys.argv[1]
rel = json.load(open(os.path.join(d, 'version.json'))).get('release')
def text(k):
    v = rel.get(k) if isinstance(rel, dict) else None
    return v.encode('utf-8') if isinstance(v, str) else b''
# Byte-exact: the signature covers these bytes; no newline translation.
for name in ('statement', 'sig'):
    with open(os.path.join(d, name), 'wb') as f:
        f.write(text(name))
PY
    then
        SIG_WHY=parse
    else
        CHECK_OUT=$(PILNK_DIR="$PILNK_DIR" bash "$PILNK_DIR/release-check.sh" --fetched \
                        "$REL_TMP/statement" "$REL_TMP/sig" 2>> "$LOG_FILE")
        SIG_TOKEN=$(printf '%s\n' "$CHECK_OUT" | sed -n 's/^ota_sig=//p')
        SIG_RUN=$(printf '%s\n' "$CHECK_OUT" | sed -n 's/^ota_sig_run=//p')
        SIGNED_VERSION=$(printf '%s\n' "$CHECK_OUT" | sed -n 's/^signed_version=//p')
        SIGNED_COMMIT=$(printf '%s\n' "$CHECK_OUT" | sed -n 's/^signed_commit=//p')
        SIG_WHY=$(printf '%s\n' "$CHECK_OUT" | sed -n 's/^ota_sig_why=//p')
        [ -n "$SIG_TOKEN" ] || { SIG_TOKEN=error; SIG_RUN=none; SIG_WHY=check; }
    fi
fi
[ "$SIG_TOKEN" = error ] || SIG_WHY=""
# Only a short token ever reaches the state file, the log and the ping.
[[ $SIG_WHY =~ ^[a-z]{1,12}(:[0-9]{1,3})?$ ]] || SIG_WHY=""
# release_state.json: what the app reports in its ping. Written to a temp file
# and renamed, so the app never reads half a file.
python3 - "$PILNK_DIR/release_state.json" "$SIG_TOKEN" "${SIG_RUN:-none}" "$POLICY" \
        "$SIGNED_VERSION" "$SIGNED_COMMIT" "$SIG_WHY" <<'PY' 2>> "$LOG_FILE" || log "could not write release_state.json (continuing)"
import json, os, sys, time
path, tok, run, mode, ver, commit, why = sys.argv[1:8]
tmp = '%s.tmp.%d' % (path, os.getpid())
with open(tmp, 'w') as f:
    json.dump({'ota_sig': tok, 'ota_sig_run': run, 'mode': mode, 'signed_version': ver,
               'signed_commit': commit, 'ota_sig_why': why,
               'checked_at': int(time.time()), 'by': 'update'}, f)
os.replace(tmp, path)
PY
log "Release signature: ota_sig=$SIG_TOKEN ota_sig_run=${SIG_RUN:-none} mode=$POLICY signed=${SIGNED_VERSION:-none}${SIG_WHY:+ why=$SIG_WHY}"

if [ "$POLICY" = enforce ]; then
    refuse() {
        log "SIGNATURE REFUSED: $1 — nothing installed, still v$OLD_VERSION."
        log "=== OTA UPDATE REFUSED — release signature ($1) ==="
        exit 6
    }
    ver_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]; }
    [ "$SIG_TOKEN" = ok ] || refuse "$SIG_TOKEN${SIG_WHY:+ ($SIG_WHY)}"
    ver_lt "$SIGNED_VERSION" "$OLD_VERSION" && refuse downgrade
    if [ "$SIGNED_VERSION" = "$OLD_VERSION" ]; then
        if [ "$(git rev-parse HEAD 2>/dev/null)" = "$SIGNED_COMMIT" ]; then
            log "Already on the signed release v$SIGNED_VERSION — nothing to do."
            log "=== OTA UPDATE COMPLETE (already on the signed release) ==="
            exit 0
        fi
        # Same version, other commit (left over from report mode): move to the
        # signed commit. The code does change, so Rule #28's abort is skipped.
        SKIP_RULE28=1
    fi
    RESET_TARGET="$SIGNED_COMMIT"
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
log "Resetting working tree to $RESET_TARGET..."
git reset --hard "$RESET_TARGET" 2>> "$LOG_FILE"
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
if [ "$OLD_VERSION" = "$NEW_VERSION" ] && [ "${PILNK_UPDATE_NO_RESTART:-}" = 1 ]; then
    # First boot (image): nothing to restart, so an unchanged VERSION is a clean no-op.
    log "VERSION unchanged ($OLD_VERSION) — no-restart mode, nothing to do."
    log "=== OTA UPDATE COMPLETE (no restart) ==="
    exit 0
fi
if [ "$OLD_VERSION" = "$NEW_VERSION" ] && [ "$SKIP_RULE28" = 1 ]; then
    log "Moved to the signed commit at the same version ($NEW_VERSION) — restarting to load it."
elif [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
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

# PILNK_UPDATE_NO_RESTART=1 (sterile image first boot): the code is updated and
# the whisper guard has run; everything after is the live-node machinery
# (self-heal wiring, audio build trigger, service restart) which first boot
# does itself once it has bound the hardware.
if [ "${PILNK_UPDATE_NO_RESTART:-}" = 1 ]; then
    log "No-restart mode: skipping self-heal, audio build and service restart."
    log "=== OTA UPDATE COMPLETE (no restart) — v$NEW_VERSION ==="
    exit 0
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
#   IDEMPOTENT — skipped unless the binary is missing or out of date, so this
#     costs nothing on the updates that don't touch the engine.
#
# 20 Sep 2026: this used to test ONLY for a missing binary, with the comment
# "skipped entirely once the binary exists". That was right while the question
# was "has this node ever had ATC audio" — and silently wrong the first time we
# changed main.cpp (v1.5.5), because every node that already had a radio pulled
# the new source and kept running the old binary. A condition that tests for
# MISSING cannot notice STALE. The mtime check below closes that: git only
# touches main.cpp's mtime when the pull actually changes it, so a release that
# leaves the engine alone still rebuilds nothing.
#
# Engine 2.3.0 split the DSP out of main.cpp into dsp.hpp, so "the source" is every file the
# engine is built from, not main.cpp alone: a release that changed only dsp.hpp must rebuild too.
# >>> radio-sources
# radio_src_newer DIR BIN: prints the first of DIR/*.cpp, *.hpp, *.h, CMakeLists.txt that is newer
# than BIN and returns 0; returns 1 when none is (or DIR is missing). Backups (main.cpp.pre*.bak)
# and the build/ directory do not count.
radio_src_newer() {
    local dir="$1" bin="$2" f
    [ -d "$dir" ] || return 1
    for f in "$dir"/*.cpp "$dir"/*.hpp "$dir"/*.h "$dir"/CMakeLists.txt; do
        [ -f "$f" ] || continue
        if [ "$f" -nt "$bin" ]; then basename "$f"; return 0; fi
    done
    return 1
}
# <<< radio-sources
PILNKRADIO_BIN=/usr/local/bin/pilnkradio
RADIO_BUILD_WHY=""
if [ ! -x "$PILNKRADIO_BIN" ]; then
    RADIO_BUILD_WHY="engine not present"
elif RADIO_SRC_FILE=$(radio_src_newer "$PILNK_DIR/pilnkradio" "$PILNKRADIO_BIN"); then
    RADIO_BUILD_WHY="engine source is newer than the installed binary ($RADIO_SRC_FILE)"
fi
# powerfloor v1 — aircraft-only boards (Pi 3, under 1.5 GiB) never build the engine. An engine
# that is already there is left alone: not stopped, not deleted, just never rebuilt. Step 3.7
# below still needs the binary, so such a node keeps getting denoise file updates.
# No hwtier.sh (or any odd answer) means "full": the build behaves exactly as before.
if [ -n "$RADIO_BUILD_WHY" ] && [ -f "$PILNK_DIR/hwtier.sh" ] \
        && [ "$(bash "$PILNK_DIR/hwtier.sh" 2>/dev/null)" = "adsb_only" ]; then
    log "ATC audio: aircraft-only board — engine build skipped"
    RADIO_BUILD_WHY=""
fi
if [ -n "$RADIO_BUILD_WHY" ] && [ -f "$PILNK_DIR/pilnkradio-install.sh" ]; then
    if systemctl list-unit-files pilnk-audio-build.service >/dev/null 2>&1; then
        # `systemctl start` on a Type=oneshot unit BLOCKS until it finishes, which
        # would park the OTA behind a 5-minute compile. --no-block returns at once
        # and lets systemd own the job — the build outlives this script.
        #
        # Uses the dedicated unit, NOT systemd-run: the node's sudoers rule grants
        # `systemctl start pilnk-audio-build` specifically. Granting systemd-run
        # would be granting full root, since it can launch anything.
        log "ATC audio: $RADIO_BUILD_WHY — starting detached build"
        sudo -n systemctl start --no-block pilnk-audio-build 2>> "$LOG_FILE" \
            && log "ATC audio build started (journalctl -u pilnk-audio-build to follow)" \
            || log "could not start ATC audio build (continuing — non-fatal)"
    else
        log "pilnk-audio-build.service not installed yet — audio build deferred to next OTA (non-fatal)"
    fi
fi

# Step 3.7: Live-denoise files for a node that already has the radio engine.
#
# v1.5.29 shipped denoise in the engine but nothing ever delivered the two
# files it needs (libdf.so for this CPU, and the model), so it only worked on
# the developer's hand-set-up receivers. They now travel in the repo
# (denoise/); this puts them in place. A full engine build (step 3.6) already
# does this at its end, so only act when no build was started above.
# Same three rules as 3.6: detached, fail-soft, idempotent (cmp, so a node
# that already matches does nothing at all).
if [ -z "$RADIO_BUILD_WHY" ] && [ -x "$PILNKRADIO_BIN" ] && [ -f "$PILNK_DIR/denoise-install.sh" ]; then
    # The engine's own ELF type, not uname: a 32-bit userland on a 64-bit
    # kernel needs the armhf build (MME1, 4 Oct 2026). One source of truth.
    DN_ARCH="$(bash "$PILNK_DIR/denoise-install.sh" --print-arch 2>/dev/null || uname -m)"
    DN_LIB="$PILNK_DIR/denoise/$DN_ARCH/libdf.so"
    DN_MODEL="$PILNK_DIR/denoise/DeepFilterNet3_onnx.tar.gz"
    if [ -f "$DN_LIB" ] && { ! cmp -s "$DN_LIB" /usr/local/lib/pilnk/libdf.so \
            || ! cmp -s "$DN_MODEL" /usr/local/share/pilnk/DeepFilterNet3_onnx.tar.gz; }; then
        if systemctl list-unit-files pilnk-audio-build.service >/dev/null 2>&1; then
            log "ATC audio: live-denoise files missing or out of date — installing (detached)"
            echo denoise > "$PILNK_DIR/.audio_build_task"
            sudo -n systemctl start --no-block pilnk-audio-build 2>> "$LOG_FILE" \
                && log "denoise install started (journalctl -u pilnk-audio-build to follow)" \
                || { rm -f "$PILNK_DIR/.audio_build_task"; log "could not start denoise install (continuing — non-fatal)"; }
        fi
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
    # A bare restart only fixes THIS update; the next one strands the node again.
    # The bootstrap installs the scoped restart rule first (needs the password once).
    log "Fix (once, as the node's owner):"
    log "  sudo PILNK_DIR=$PILNK_DIR bash $PILNK_DIR/bootstrap-selfheal.sh && sudo systemctl restart pilnk"
    log "That installs the restart rule, so future OTAs restart unattended."
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
