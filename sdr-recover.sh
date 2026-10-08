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
#   • RADIO DONGLE IS NEVER THE ADS-B DONGLE (26 Sep 2026). The serial reserved
#     for the ATC audio engine (/etc/pilnkradio/config.json) is excluded when we
#     count ADS-B dongles and when we re-pin. Before this, a two-dongle node
#     whose ADS-B stick dropped saw "1 RTL present" (the radio) and restarted
#     the decoder anyway; on a pinned readsb node it would have RE-PINNED readsb
#     to the radio dongle. Found by AJ's live unplug test on EpsomPi.
#   • DATALINK DONGLE IS NEVER THE ADS-B DONGLE EITHER (7 Oct 2026). Same rule
#     for the ACARS/VDL2 stick (/etc/pilnk-datalink/config.json): with the ADS-B
#     stick dropped, the old count saw the datalink stick and could re-pin to it.
#   • COOLDOWN WAITS, IT DOESN'T DROP (26 Sep 2026). A replug that lands inside
#     the cooldown used to be logged and discarded, leaving the pickup to the
#     3-min timer. Now the run waits for the cooldown to end, re-checks, and
#     only restarts if still stale. Restarts stay >= COOLDOWN_SECS apart; extra
#     udev/timer starts merge into the waiting oneshot job.
#   • 7 Oct 2026, lessons from re-serializing a new datalink stick on the hub:
#     - MAINTENANCE PAUSE: a valid /run/pilnk-sdr-pause (epoch end, <= 30 min
#       ahead) makes us stand down. Stopping our timer is NOT enough: udev
#       starts us on every plug.
#     - A decoder STOPPED BY HAND (inactive, Result=success) is left alone for
#       30 min, then revived. Crashes are still healed at once.
#     - DUPLICATE SERIALS are reported, and readsb is never re-pinned while two
#       sticks share a serial (a new stick usually ships as 00000001).
#
# EXIT: always 0 (a watchdog must not fail its unit).
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

# Every path below can be overridden by a PILNK_* variable. That exists for the
# test harness only; in production none are set and the literals apply.
LOG="${PILNK_SDR_LOG:-/var/log/pilnk-sdr-recover.log}"
FRESH_SECS="${PILNK_FRESH_SECS:-30}"     # aircraft.json older than this = stale
COOLDOWN_SECS="${PILNK_COOLDOWN_SECS:-90}"  # min seconds between restarts
STAMP="${PILNK_SDR_STAMP:-/run/pilnk-sdr-recover.stamp}"     # last-restart timestamp (tmpfs; clears on reboot)
READSB_DEFAULT="${PILNK_READSB_DEFAULT:-/etc/default/readsb}"
RADIO_CFG="${PILNK_RADIO_CFG:-/etc/pilnkradio/config.json}"   # ATC audio engine — holds the radio dongle's serial
DATALINK_CFG="${PILNK_DATALINK_CFG:-/etc/pilnk-datalink/config.json}"   # ACARS/VDL2 decoder — holds the datalink dongle's serial
DUMP_JSON="${PILNK_DUMP_JSON:-/run/dump1090-fa/aircraft.json}"
READSB_JSON="${PILNK_READSB_JSON:-/run/readsb/aircraft.json}"
PAUSE_FILE="${PILNK_SDR_PAUSE:-/run/pilnk-sdr-pause}"   # maintenance pause (tmpfs; clears on reboot)
PAUSE_MAX_SECS=1800                                      # a pause can never be longer than 30 min
MANUAL_STOP_SECS="${PILNK_MANUAL_STOP_SECS:-1800}"       # leave a hand-stopped decoder alone this long
UPTIME_FILE="${PILNK_UPTIME_FILE:-/proc/uptime}"
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

# Seconds of cooldown left (0 if none). Capped at 100 so a huge override can
# never outlive the service's TimeoutStartSec (150).
cooldown_left() {
  local now last left
  now=$(date +%s)
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  case "$last" in (*[!0-9]*|'') last=0 ;; esac
  left=$(( COOLDOWN_SECS - (now - last) ))
  [ "$left" -lt 0 ] && left=0
  [ "$left" -gt 100 ] && left=100
  echo "$left"
}

# Every RTL serial on the USB bus, one line PER STICK (duplicates kept).
rtl_serials_all() {
  command -v rtl_test >/dev/null 2>&1 || return 0
  timeout 3 rtl_test 2>&1 \
    | grep -oE 'SN: [0-9A-Za-z]+' \
    | awk '{print $2}' | grep . | sort
}

# Current RTL serials present on the USB bus (one per line, de-duplicated).
rtl_serials() { rtl_serials_all | uniq; }

# Serials carried by MORE THAN ONE stick (7 Oct 2026). A new RTL-SDR usually
# ships as 00000001 — the usual ADS-B serial — and rtl_serials' de-duplication
# made two such sticks look like one. A decoder pinned to a shared serial may
# open either stick, and a re-pin to one is a coin toss, so we never re-pin
# while any serial is shared.
dup_serials() { rtl_serials_all | uniq -d; }

# First "serial" value in a JSON config file, if the file exists.
cfg_serial() {
  [ -f "$1" ] || return 0
  grep -oE '"serial"[[:space:]]*:[[:space:]]*"[^"]*"' "$1" 2>/dev/null \
    | head -n1 | sed -E 's/.*"([^"]*)"$/\1/'
}

# Serial the ATC audio engine has reserved for the radio dongle, if any.
radio_serial() { cfg_serial "$RADIO_CFG"; }

# Serial the datalink decoder (ACARS/VDL2) has reserved, if any (7 Oct 2026).
datalink_serial() { cfg_serial "$DATALINK_CFG"; }

# RTL serials that could be the ADS-B dongle: everything present MINUS the
# radio and datalink dongles. Use this, never rtl_serials, for any ADS-B decision.
adsb_rtl_serials() {
  local reserved
  reserved="$( { radio_serial; echo; datalink_serial; echo; } | grep . )"
  if [ -n "$reserved" ]; then
    rtl_serials | grep -vxF -- "$reserved"
  else
    rtl_serials
  fi
}

# For log lines: say which reserved dongles were left out of the count.
radio_note() {
  local radio datalink parts=()
  radio="$(radio_serial)"; datalink="$(datalink_serial)"
  [ -n "$radio" ] && parts+=("radio dongle SN $radio")
  [ -n "$datalink" ] && parts+=("datalink dongle SN $datalink")
  [ ${#parts[@]} -eq 0 ] && return 0
  local IFS=,; local joined="${parts[*]}"
  printf ' (%s not counted)' "${joined//,/, }"
}

# MAINTENANCE PAUSE (7 Oct 2026). Found on the hub while re-serializing a new
# datalink stick: stopping the timer does NOT stop us — udev starts this service
# on every plug — so we restarted dump1090 onto the very stick being worked on.
# A tool doing dongle work (datalink-install.sh) writes one line to PAUSE_FILE:
# the epoch second the pause ends. Junk, past, or more than PAUSE_MAX_SECS
# ahead = no pause, so a crashed tool can never switch self-heal off for long.
# Prints the minutes left and returns 0 while a valid pause is in force.
pause_active() {
  [ -s "$PAUSE_FILE" ] || return 1
  local until now
  until="$(head -c 32 "$PAUSE_FILE" 2>/dev/null | tr -d '[:space:]')"
  # digits only, no leading zero (bash arithmetic would read it as octal), at
  # most 10 digits (an epoch second until the year 2286)
  case "$until" in (*[!0-9]*|''|0*|???????????*) return 1 ;; esac
  now=$(date +%s)
  [ "$until" -gt "$now" ] || return 1
  if [ $(( until - now )) -gt "$PAUSE_MAX_SECS" ]; then
    log "ignoring pause file $PAUSE_FILE: ends more than $((PAUSE_MAX_SECS / 60)) min from now"
    return 1
  fi
  echo $(( (until - now + 59) / 60 ))
}

# RESPECT A MANUAL STOP (7 Oct 2026, AJ's call). On the hub AJ ran
# `systemctl stop dump1090-fa` to free a stick and we started it again 14 s
# later, so rtl_eeprom kept failing with "usb_claim_interface error -6". Anyone
# following a generic RTL-SDR guide hits the same wall. A unit that is inactive
# with Result=success was stopped cleanly (by a person or a tool), not crashed.
# Prints the seconds since that clean stop; prints nothing (fail open, i.e. heal
# as before) for a crash, a unit never started since boot, or unreadable data.
manual_stop_age() {
  local state="" result="" mono="" k v up
  while IFS='=' read -r k v; do
    case "$k" in
      ActiveState) state="$v" ;;
      Result) result="$v" ;;
      InactiveEnterTimestampMonotonic) mono="$v" ;;
    esac
  done < <(systemctl show "$1" -p ActiveState -p Result -p InactiveEnterTimestampMonotonic 2>/dev/null)
  [ "$state" = "inactive" ] && [ "$result" = "success" ] || return 0
  case "$mono" in (*[!0-9]*|''|0) return 0 ;; esac
  up="$(cut -d' ' -f1 "$UPTIME_FILE" 2>/dev/null)"; up="${up%%.*}"
  case "$up" in (*[!0-9]*|'') return 0 ;; esac
  [ "$up" -ge $(( mono / 1000000 )) ] || return 0
  echo $(( up - mono / 1000000 ))
}

airspy_present() { lsusb 2>/dev/null | grep -iqE "airspy|$AIRSPY_VID"; }

# The serial readsb is currently pinned to in /etc/default/readsb, if any.
readsb_pinned_serial() {
  [ -f "$READSB_DEFAULT" ] || return 0
  grep -oE -- '--device [0-9A-Za-z]+' "$READSB_DEFAULT" 2>/dev/null \
    | head -n1 | awk '{print $2}'
}

DID_RESTART=0
restart_decoder() {
  local svc="$1"
  log "→ restarting $svc"
  DID_RESTART=1
  stamp_restart
  systemctl restart "$svc" 2>/dev/null || log "  (restart $svc returned non-zero)"
}

# ── 1. work out which decoder mode this node is in ───────────────────────────
# Priority mirrors the installer's consume-first decision.

DECODER_MODE=""   # consume-readsb | consume-dump | own-readsb | airspy | none
JSON=""
IDLE_UNIT=""      # set when the decoder unit is installed but not running

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
  if   svc_exists readsb;      then DECODER_MODE="readsb";      JSON="$READSB_JSON"; IDLE_UNIT="readsb"
  elif svc_exists dump1090-fa; then DECODER_MODE="consume-dump"; JSON="$DUMP_JSON"; IDLE_UNIT="dump1090-fa"
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

# ── 2a. stand down? (maintenance pause, or a decoder stopped by hand) ────────
# Checked only once we know we are stale, so a healthy node stays silent. Run
# again after the cooldown wait below: a pause written while we slept must win.
stand_down_check() {
  local pause_min stop_age
  if pause_min="$(pause_active)"; then
    log "STALE but paused for maintenance (${pause_min} min left) — not touching the decoder"
    exit 0
  fi
  [ -n "$IDLE_UNIT" ] || return 0
  stop_age="$(manual_stop_age "$IDLE_UNIT")"
  [ -n "$stop_age" ] || return 0
  if [ "$stop_age" -lt "$MANUAL_STOP_SECS" ]; then
    log "$IDLE_UNIT was stopped by hand $((stop_age / 60)) min ago — leaving it alone until $((MANUAL_STOP_SECS / 60)) min have passed"
    exit 0
  fi
  [ -n "${REVIVE_LOGGED:-}" ] || log "$IDLE_UNIT was stopped by hand $((stop_age / 60)) min ago — reviving"
  REVIVE_LOGGED=1
}
stand_down_check

# ── 2b. cooldown — don't restart-storm a flapping node ───────────────────────
# We're stale. If we ALSO restarted very recently, the decoder may simply still
# be coming up, OR a flapping dongle is firing udev repeatedly. Either way, hold
# off — restarting again now would just re-empty the json and strobe the node.
#
# Hold, don't drop: wait out the rest of the cooldown, then look again. The
# decoder often comes back by itself in that window (its own systemd restart
# picks the replugged dongle up — seen on EpsomPi 26 Sep), and then we do
# nothing. If it is still stale, we restart once, still >= COOLDOWN_SECS after
# the last restart. udev/timer starts arriving meanwhile merge into this job.
if in_cooldown; then
  LEFT="$(cooldown_left)"
  log "STALE but in cooldown — waiting ${LEFT}s for it to end, then re-checking"
  sleep "$LEFT"
  if json_fresh "$JSON"; then
    log "✓ fresh again after the cooldown wait — decoder came back by itself, no restart needed"
    exit 0
  fi
  stand_down_check
fi

log "STALE: decoder=$DECODER_MODE json=$JSON not fresh (>${FRESH_SECS}s or empty) — recovering"

# ── 3. recover ───────────────────────────────────────────────────────────────

DUPS="$(dup_serials | tr '\n' ' ' | sed 's/ $//')"
if [ -n "$DUPS" ]; then
  log "WARNING: two dongles share SN $DUPS — a decoder pinned to it may open the wrong stick. Fix: plug in only one of them and run rtl_eeprom -s <new serial>, then replug"
fi

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
    # adsb_rtl_serials, not rtl_serials: the radio dongle must never count as a
    # candidate, or a dropped ADS-B stick would get readsb re-pinned to it.
    mapfile -t PRESENT < <(adsb_rtl_serials)
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
        if [ -n "$DUPS" ]; then
          log "pinned SN $PINNED gone and a serial is shared ($DUPS) — NOT re-pinning, restarting readsb as-is"
          restart_decoder readsb
        elif [ "$NPRESENT" -eq 1 ]; then
          NEW="${PRESENT[0]}"
          log "pinned SN $PINNED is GONE; exactly one free dongle SN $NEW present — re-pinning"
          if grep -q -- "--device " "$READSB_DEFAULT" 2>/dev/null; then
            # [^ "] not [^ ]: when --device is the last option the serial is
            # followed by the closing quote, and [^ ]* used to swallow it.
            sed -i "s/--device [^ \"]*/--device $NEW/" "$READSB_DEFAULT"
          else
            sed -i "s|RECEIVER_OPTIONS=\"|RECEIVER_OPTIONS=\"--device $NEW |" "$READSB_DEFAULT"
          fi
          udevadm control --reload-rules 2>/dev/null || true
          udevadm trigger 2>/dev/null || true
          restart_decoder readsb
          log "re-pinned readsb → SN $NEW"
        elif [ "$NPRESENT" -eq 0 ]; then
          log "pinned SN $PINNED gone and NO ADS-B RTL dongle present$(radio_note) — waiting for hardware"
        else
          log "pinned SN $PINNED gone; $NPRESENT dongles present (ambiguous) — NOT re-pinning, restarting readsb as-is"
          restart_decoder readsb
        fi
      fi
    else
      # No explicit pin (readsb auto-selects). If any RTL present, a restart
      # is enough; readsb will grab device 0.
      if [ "$NPRESENT" -ge 1 ]; then
        log "no serial pin; $NPRESENT ADS-B RTL present$(radio_note) — restarting readsb"
        restart_decoder readsb
      else
        log "no serial pin and NO ADS-B RTL present$(radio_note) — waiting for hardware"
      fi
    fi
    ;;

  consume-dump)
    # dump1090-fa owns the dongle directly. We don't manage its serial pin
    # (that's the operator's FlightAware/PiAware setup). Safest recovery is a
    # plain restart if a dongle is present; never re-pin someone else's feeder.
    mapfile -t PRESENT < <(adsb_rtl_serials)
    if [ "${#PRESENT[@]}" -ge 1 ]; then
      log "dump1090-fa stale; ${#PRESENT[@]} ADS-B RTL present$(radio_note) — restarting dump1090-fa"
      restart_decoder dump1090-fa
    else
      log "dump1090-fa stale and NO ADS-B RTL present$(radio_note) — waiting for hardware (the replug will trigger us)"
    fi
    ;;

esac

# Nothing restarted (waiting for hardware) — nothing to report on. Without
# this the log said "still not fresh after restart" when no restart happened.
[ "$DID_RESTART" -eq 1 ] || exit 0

# Give the decoder a moment, then report the outcome (don't loop/block).
sleep 4
if json_fresh "$JSON"; then
  log "✓ recovered — $JSON is fresh again"
else
  log "… still not fresh after restart; will retry on next trigger (after cooldown)"
fi

exit 0
