#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PiLNK — SDR Self-Heal  (sdr-recover.sh)
#
# Recovers the ADS-B decoder when an SDR dongle is unplugged / replugged /
# swapped — so a node heals itself instead of the operator re-running the
# installer (which triggers the pairing-flow deadlock).
#
# WHAT IT DOES
#   1. Works out which decoder this node uses (consume readsb / consume
#      dump1090-fa / PiLNK's own readsb / airspy_adsb→readsb).
#   2. Checks whether that decoder is writing a FRESH aircraft.json.
#   3. If stale: detects present hardware, restarts the right decoder, and —
#      for a pinned RTL readsb whose serial has changed (dongle swap) — re-pins
#      /etc/default/readsb to the new free dongle, conservatively.
#
# DESIGN
#   • Runs as ROOT (systemd service/timer + udev). No sudo inside.
#   • NEVER touches the pilnk service (Rule #29). Only the decoder.
#   • Idempotent + safe to run repeatedly. Healthy node → does nothing.
#   • Hardware-agnostic: RTL-SDR *and* Airspy. No region assumptions (Rule #25).
#   • Conservative re-pin: only when exactly ONE free dongle is unambiguous.
#   • COOLDOWN (v1.2.15.2): after a restart, refuse to restart again for
#     COOLDOWN_SECS. A flapping dongle (rapid udev add/remove) can otherwise
#     make us restart the decoder every few seconds, and each restart briefly
#     empties aircraft.json — which strobes the node on the network map. The
#     cooldown gives the decoder room to actually come up and stabilise, so a
#     marginal node degrades gracefully instead of strobing. Does NOT fix the
#     underlying flap (that's hardware) — it stops us amplifying it.
#
# EXIT: always 0 (a watchdog must not fail its unit).
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

LOG="/var/log/pilnk-sdr-recover.log"
FRESH_SECS="${PILNK_FRESH_SECS:-30}"     # aircraft.json older than this = stale
COOLDOWN_SECS="${PILNK_COOLDOWN_SECS:-90}"  # min seconds between restarts
STAMP="/run/pilnk-sdr-recover.stamp"     # last-restart timestamp (tmpfs; clears on reboot)
READSB_DEFAULT="/etc/default/readsb"
DUMP_JSON="/run/dump1090-fa/aircraft.json"
READSB_JSON="/run/readsb/aircraft.json"
AIRSPY_VID="1d50:60a1"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null; }

# ── helpers ──────────────────────────────────────────────────────────────────

svc_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

svc_exists() { systemctl list-unit-files "$1.service" 2>/dev/null | grep -q "$1.service"; }

# Fresh = file exists, non-empty, and modified within FRESH_SECS.
json_fresh() {
  local f="$1"
  [ -s "$f" ] || return 1
  local now mtime age
  now=$(date +%s)
  mtime=$(stat -c %Y "$f" 2>/dev/null) || return 1
  age=$(( now - mtime ))
  [ "$age" -le "$FRESH_SECS" ]
}

# True if we restarted the decoder within the last COOLDOWN_SECS.
in_cooldown() {
  [ -f "$STAMP" ] || return 1
  local now last age
  now=$(date +%s)
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  case "$last" in (*[!0-9]*|'') last=0 ;; esac   # guard against junk in the file
  age=$(( now - last ))
  [ "$age" -lt "$COOLDOWN_SECS" ]
}

stamp_restart() { date +%s > "$STAMP" 2>/dev/null || true; }

# Current RTL serials present on the USB bus (one per line).
rtl_serials() {
  command -v rtl_test >/dev/null 2>&1 || return 0
  timeout 3 rtl_test 2>&1 \
    | grep -oE 'SN: [0-9A-Za-z]+' \
    | awk '{print $2}' | grep . | sort -u
}

airspy_present() { lsusb 2>/dev/null | grep -iqE "airspy|$AIRSPY_VID"; }

# The serial readsb is currently pinned to in /etc/default/readsb, if any.
readsb_pinned_serial() {
  [ -f "$READSB_DEFAULT" ] || return 0
  grep -oE -- '--device [0-9A-Za-z]+' "$READSB_DEFAULT" 2>/dev/null \
    | head -n1 | awk '{print $2}'
}

restart_decoder() {
  local svc="$1"
  log "→ restarting $svc"
  stamp_restart
  systemctl restart "$svc" 2>/dev/null || log "  (restart $svc returned non-zero)"
}

# ── 1. work out which decoder mode this node is in ───────────────────────────
# Priority mirrors the installer's consume-first decision.

DECODER_MODE=""   # consume-readsb | consume-dump | own-readsb | airspy | none
JSON=""

if svc_exists airspy_adsb && svc_active airspy_adsb; then
  DECODER_MODE="airspy"; JSON="$READSB_JSON"
elif svc_exists readsb && svc_active readsb; then
  # readsb is active — could be PiLNK's own (rtlsdr) or a pre-existing feeder
  # we consume. Recovery is identical either way: keep readsb fed.
  DECODER_MODE="readsb"; JSON="$READSB_JSON"
elif svc_exists dump1090-fa && svc_active dump1090-fa; then
  DECODER_MODE="consume-dump"; JSON="$DUMP_JSON"
else
  # No active decoder service. Pick the json that exists so we can still report,
  # and try to revive whichever decoder unit is installed.
  if   svc_exists readsb;      then DECODER_MODE="readsb";      JSON="$READSB_JSON"
  elif svc_exists dump1090-fa; then DECODER_MODE="consume-dump"; JSON="$DUMP_JSON"
  else DECODER_MODE="none"; fi
fi

if [ "$DECODER_MODE" = "none" ]; then
  log "no decoder service installed — nothing to recover. Exiting."
  exit 0
fi

# ── 2. healthy? then do nothing ──────────────────────────────────────────────

if json_fresh "$JSON"; then
  # Quiet success — no log spam on the happy path (timer runs every few min).
  exit 0
fi

# ── 2b. cooldown — don't restart-storm a flapping node ───────────────────────
# We're stale. If we ALSO restarted very recently, the decoder may simply still
# be coming up, OR a flapping dongle is firing udev repeatedly. Either way, hold
# off — restarting again now would just re-empty the json and strobe the node.
if in_cooldown; then
  log "STALE but in cooldown (<${COOLDOWN_SECS}s since last restart) — holding, letting decoder settle"
  exit 0
fi

log "STALE: decoder=$DECODER_MODE json=$JSON not fresh (>${FRESH_SECS}s or empty) — recovering"

# ── 3. recover ───────────────────────────────────────────────────────────────

case "$DECODER_MODE" in

  airspy)
    # Airspy path: airspy_adsb owns the USB device (no RTL serial), readsb is
    # net-only consuming its Beast stream. If the Airspy is present, bounce
    # airspy_adsb first, then readsb. If absent, nothing we can do but log.
    if airspy_present; then
      log "Airspy present — restarting airspy_adsb then readsb"
      restart_decoder airspy_adsb
      sleep 3
      svc_exists readsb && restart_decoder readsb
    else
      log "Airspy NOT present on USB bus — cannot recover until hardware returns"
    fi
    ;;

  readsb)
    # PiLNK's own readsb (rtlsdr) OR a consumed readsb. Check the serial pin.
    PINNED="$(readsb_pinned_serial)"
    mapfile -t PRESENT < <(rtl_serials)
    NPRESENT=${#PRESENT[@]}

    if [ -n "$PINNED" ]; then
      # Is the pinned dongle still present?
      if printf '%s\n' "${PRESENT[@]}" | grep -qx "$PINNED"; then
        log "pinned dongle SN $PINNED still present — just restarting readsb"
        restart_decoder readsb
      else
        # Dongle swap case. Re-pin ONLY if exactly one free dongle is present
        # (unambiguous). Otherwise leave the pin alone — a multi-dongle node
        # needs human disambiguation, same caution as the installer.
        if [ "$NPRESENT" -eq 1 ]; then
          NEW="${PRESENT[0]}"
          log "pinned SN $PINNED is GONE; exactly one free dongle SN $NEW present — re-pinning"
          if grep -q -- "--device " "$READSB_DEFAULT" 2>/dev/null; then
            sed -i "s/--device [^ ]*/--device $NEW/" "$READSB_DEFAULT"
          else
            sed -i "s|RECEIVER_OPTIONS=\"|RECEIVER_OPTIONS=\"--device $NEW |" "$READSB_DEFAULT"
          fi
          udevadm control --reload-rules 2>/dev/null || true
          udevadm trigger 2>/dev/null || true
          restart_decoder readsb
          log "re-pinned readsb → SN $NEW"
        elif [ "$NPRESENT" -eq 0 ]; then
          log "pinned SN $PINNED gone and NO RTL dongle present — waiting for hardware"
        else
          log "pinned SN $PINNED gone; $NPRESENT dongles present (ambiguous) — NOT re-pinning, restarting readsb as-is"
          restart_decoder readsb
        fi
      fi
    else
      # No explicit pin (readsb auto-selects). If any RTL present, a restart
      # is enough; readsb will grab device 0.
      if [ "$NPRESENT" -ge 1 ]; then
        log "no serial pin; $NPRESENT RTL present — restarting readsb"
        restart_decoder readsb
      else
        log "no serial pin and NO RTL present — waiting for hardware"
      fi
    fi
    ;;

  consume-dump)
    # dump1090-fa owns the dongle directly. We don't manage its serial pin
    # (that's the operator's FlightAware/PiAware setup). Safest recovery is a
    # plain restart if a dongle is present; never re-pin someone else's feeder.
    mapfile -t PRESENT < <(rtl_serials)
    if [ "${#PRESENT[@]}" -ge 1 ]; then
      log "dump1090-fa stale; ${#PRESENT[@]} RTL present — restarting dump1090-fa"
      restart_decoder dump1090-fa
    else
      log "dump1090-fa stale and NO RTL present — waiting for hardware"
    fi
    ;;

esac

# Give the decoder a moment, then report the outcome (don't loop/block).
sleep 4
if json_fresh "$JSON"; then
  log "✓ recovered — $JSON is fresh again"
else
  log "… still not fresh after restart; will retry on next trigger (after cooldown)"
fi

exit 0
