#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PiLNK — Self-Heal Bootstrap  (bootstrap-selfheal.sh)
#
# Wires the SDR self-heal machinery into the system, ONCE, idempotently:
#   • /etc/systemd/system/pilnk-sdr-recover.service   (oneshot, runs the brain)
#   • /etc/systemd/system/pilnk-sdr-recover.timer     (safety net, every 3 min)
#   • /etc/udev/rules.d/99-pilnk-sdr.rules            (instant hotplug trigger)
#
# Source files ship in the repo (~/pilnk). This copies them into /etc with the
# real service user substituted, reloads systemd + udev, and enables the timer.
#
# CALLED BY: update.sh (every OTA) and install.sh (new nodes). Safe to run any
# number of times — it only acts when something is missing or has changed.
#
# PRIVILEGE: expects to run under the same passwordless-sudo context update.sh
# already uses for `systemctl restart pilnk`. Uses `sudo -n`; if that's not
# available it logs and exits 0 (never blocks the OTA / install).
#
# EXIT: always 0 — a bootstrap must never break the update that called it.
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

PILNK_DIR="${PILNK_DIR:-$HOME/pilnk}"
LOG="${PILNK_SELFHEAL_LOG:-$PILNK_DIR/update.log}"

SRC_SERVICE="$PILNK_DIR/pilnk-sdr-recover.service"
SRC_TIMER="$PILNK_DIR/pilnk-sdr-recover.timer"
SRC_UDEV="$PILNK_DIR/99-pilnk-sdr.rules"
SRC_SCRIPT="$PILNK_DIR/sdr-recover.sh"

DST_SERVICE="/etc/systemd/system/pilnk-sdr-recover.service"
DST_TIMER="/etc/systemd/system/pilnk-sdr-recover.timer"
DST_UDEV="/etc/udev/rules.d/99-pilnk-sdr.rules"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [selfheal-bootstrap] $1" >> "$LOG" 2>&1; }

# Wrapper: prefer no sudo if already root, else sudo -n (non-interactive).
priv() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

# Confirm we can actually use privilege before trying anything destructive.
if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
  log "no passwordless sudo available — skipping self-heal wiring (will retry next OTA)"
  exit 0
fi

# Source files must exist (they ship in the repo). If a partial checkout means
# one is missing, bail quietly — next OTA with a complete tree will wire it.
for f in "$SRC_SERVICE" "$SRC_TIMER" "$SRC_UDEV" "$SRC_SCRIPT"; do
  if [ ! -f "$f" ]; then
    log "source file missing ($f) — skipping wiring this cycle"
    exit 0
  fi
done

# The brain must be executable.
priv chmod +x "$SRC_SCRIPT" 2>/dev/null || true

CHANGED=0

# Render the service file with the real user + repo path substituted.
RENDERED_SERVICE="$(sed \
  -e "s|/home/PILNK_USER/pilnk/sdr-recover.sh|$SRC_SCRIPT|g" \
  "$SRC_SERVICE")"

# ── install/update a unit only if content differs (idempotent) ──────────────
install_if_changed() {
  local content="$1" dst="$2" label="$3"
  if [ -f "$dst" ] && [ "$content" = "$(cat "$dst" 2>/dev/null)" ]; then
    return 0   # already correct — no-op
  fi
  if printf '%s\n' "$content" | priv tee "$dst" >/dev/null 2>&1; then
    log "wrote $label"; CHANGED=1
  else
    log "FAILED to write $label"
  fi
}

install_if_changed "$RENDERED_SERVICE"        "$DST_SERVICE" "pilnk-sdr-recover.service"
install_if_changed "$(cat "$SRC_TIMER")"      "$DST_TIMER"   "pilnk-sdr-recover.timer"
install_if_changed "$(cat "$SRC_UDEV")"       "$DST_UDEV"    "99-pilnk-sdr.rules"

# ── reload + enable only if we changed something, or the timer isn't active ──
TIMER_ACTIVE=0
systemctl is-active --quiet pilnk-sdr-recover.timer 2>/dev/null && TIMER_ACTIVE=1

if [ "$CHANGED" -eq 1 ] || [ "$TIMER_ACTIVE" -eq 0 ]; then
  priv systemctl daemon-reload 2>/dev/null || true
  if priv systemctl enable --now pilnk-sdr-recover.timer 2>/dev/null; then
    log "enabled + started pilnk-sdr-recover.timer"
  else
    log "could not enable timer (will retry next OTA)"
  fi

  # Reload udev so the hotplug rule is live without a reboot.
  priv udevadm control --reload-rules 2>/dev/null || true
  priv udevadm trigger --subsystem-match=usb 2>/dev/null || true
  log "udev rules reloaded"
fi

if [ "$CHANGED" -eq 1 ]; then
  log "self-heal wiring updated"
else
  log "self-heal already wired — nothing to do"
fi

exit 0
