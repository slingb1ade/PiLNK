#!/bin/bash
# ╔═══════════════════════════════════════════════════════╗
# ║  pilnk-hw-setup.sh — PiLNK claim-safe receiver rules   ║
# ╠═══════════════════════════════════════════════════════╣
# ║  The hardware-binding decision lifted out of           ║
# ║  install.sh: which decoder to consume, which SDR is    ║
# ║  ADS-B, which is VHF. One copy of the rules, shared by ║
# ║  the interactive installer (source it) and unattended  ║
# ║  first boot (run it).                                  ║
# ║                                                         ║
# ║  HARDWARE RULE: PiLNK never touches a dongle that      ║
# ║  another decoder/feeder is using.                      ║
# ╚═══════════════════════════════════════════════════════╝
#
# Usage:   pilnk-hw-setup.sh [--plan] [--apply]
#   --plan   print the decision as KEY=VALUE lines, change nothing
#   --apply  decide, then pin readsb, write config.json keys
#            (aircraft_json_path, vhf_serial, dashboard_port), and run
#            pilnkradio-install.sh in config-only mode when a VHF dongle is set
# Exit:    0 bound   2 no receiver found (nothing written)   1 error
#
# Env:     PILNK_ROOT=/              prefix for every system path read/written
#          PILNK_HW_NONINTERACTIVE=1 never prompt; deterministic pick rule
#          PILNK_DIR                 where config.json lives (default: this dir)
#
# Sourced (by install.sh) it only defines the hw_* functions.

PILNK_ROOT="${PILNK_ROOT:-/}"
_R="${PILNK_ROOT%/}"                 # "" for the real root, else the fake root
HW_TTY="$_R/dev/tty"                 # prompts + answers (interactive mode)
HW_NONINTERACTIVE="${PILNK_HW_NONINTERACTIVE:-}"

# Message helpers: install.sh defines coloured ones; standalone, go to stderr
# so stdout carries only the --plan lines.
declare -F ok   >/dev/null || ok()   { printf '✓ %s\n' "$1" >&2; }
declare -F info >/dev/null || info() { printf '→ %s\n' "$1" >&2; }
declare -F warn >/dev/null || warn() { printf '⚠ %s\n' "$1" >&2; }
declare -F err  >/dev/null || err()  { printf '✗ %s\n' "$1" >&2; }
declare -F step >/dev/null || step() { printf '\n[ %s ]\n' "$1" >&2; }

# sudo only when needed: not as root, and never against a fake root.
_hw_sudo() {
  if [ -n "$_R" ] || [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

hw_scan_feeders() {
  # ── EXISTING FEEDERS & DECODERS — look before we touch ────
  # CLAIM-SAFE RULE: before assigning any hardware or installing any decoder,
  # find out what is ALREADY running and what it owns. PiLNK coexists; it
  # never displaces a working feeder without explicit operator consent.
  #
  # Verified upstream behaviour (do not re-derive around this):
  #   • wiedehopf readsb-install.sh runs `apt remove -y dump1090-fa` — installing
  #     readsb REMOVES a working dump1090-fa. No readsb-alongside-dump1090-fa.
  #   • readsb-install.sh EXITS on a PiAware SD-card image (piaware-config.txt).
  #   • readsb-install.sh re-points fr24feed/rbfeeder to 127.0.0.1:30005, so
  #     network-mode FR24/RadarBox survive a readsb install; dump1090-fa does not.
  step "EXISTING FEEDERS & DECODERS"

  READSB_HEALTHY=0
  if systemctl is-active --quiet readsb 2>/dev/null && [ -s "$_R/run/readsb/aircraft.json" ]; then
    READSB_HEALTHY=1
    ok "readsb is already running and healthy (/run/readsb/aircraft.json live)"
  fi

  DUMP1090_HEALTHY=0
  if systemctl is-active --quiet dump1090-fa 2>/dev/null && [ -s "$_R/run/dump1090-fa/aircraft.json" ]; then
    DUMP1090_HEALTHY=1
    ok "dump1090-fa is running and healthy (FlightAware/PiAware feeder detected)"
  fi

  PIAWARE_IMAGE=0
  if [ -f "$_R/boot/piaware-config.txt" ] || [ -f "$_R/boot/firmware/piaware-config.txt" ]; then
    PIAWARE_IMAGE=1
    info "PiAware SD-card image detected"
  fi

  # Feeders that may hold a dongle DIRECTLY (no dump1090-fa in between).
  # fr24feed in receiver="dvbt"/sdr mode and rbfeeder in non-network mode own
  # an RTL dongle themselves, usually with NO serial named anywhere we can read.
  # We count these as UNNAMED claims and stay cautious around them.
  UNNAMED_CLAIMS=0
  UNNAMED_REPORT=""
  if systemctl is-active --quiet fr24feed 2>/dev/null; then
    if grep -qiE '^[[:space:]]*receiver[[:space:]]*=[[:space:]]*"?(dvbt|sdr)' "$_R/etc/fr24feed.ini" 2>/dev/null; then
      UNNAMED_CLAIMS=$((UNNAMED_CLAIMS + 1))
      UNNAMED_REPORT="${UNNAMED_REPORT}    fr24feed (FlightRadar24) — DIRECT SDR mode, holding a dongle"$'\n'
      warn "fr24feed is running in direct SDR mode — it owns one RTL dongle itself."
    else
      info "fr24feed is running in network mode (consumes a decoder feed — holds no dongle)."
    fi
  fi
  if systemctl is-active --quiet rbfeeder 2>/dev/null; then
    if grep -qiE '^[[:space:]]*network_mode[[:space:]]*=[[:space:]]*(false|no)' "$_R/etc/rbfeeder.ini" 2>/dev/null; then
      UNNAMED_CLAIMS=$((UNNAMED_CLAIMS + 1))
      UNNAMED_REPORT="${UNNAMED_REPORT}    rbfeeder (RadarBox) — DIRECT SDR mode, holding a dongle"$'\n'
      warn "rbfeeder is running in direct SDR mode — it owns one RTL dongle itself."
    else
      info "rbfeeder is running in network mode (holds no dongle)."
    fi
  fi
  return 0
}

hw_scan_hardware() {
  # ── SDR HARDWARE DETECTION ────────────────────────────────
  # Airspy (Mini or R2) is INVISIBLE to rtl_test (different driver/USB stack), so
  # we detect it separately via lsusb text-match. RTL-SDR is enumerated by rtl_test.
  step "SDR HARDWARE DETECTION"

  AIRSPY_PRESENT=0
  if command -v lsusb &>/dev/null && lsusb | grep -iq airspy; then
    AIRSPY_PRESENT=1
    AIRSPY_DESC=$(lsusb | grep -i airspy | head -n1 | sed 's/^.*ID [0-9a-f:]* //')
    ok "Airspy detected: ${AIRSPY_DESC:-Airspy device}"
  fi

  RTL_PRESENT=0
  RTL_SNS=""
  RTL_COUNT=0
  if command -v rtl_test &>/dev/null; then
    RTL_OUT=$(timeout 3 rtl_test 2>&1 || true)
    RTL_SNS=$(echo "$RTL_OUT" | grep -oE 'SN: [0-9A-Za-z]+' | awk '{print $2}' | grep . | sort -u)
    RTL_COUNT=$(echo "$RTL_SNS" | grep -c . || true)
    if [ "$RTL_COUNT" -gt 0 ]; then
      RTL_PRESENT=1
      ok "RTL-SDR detected: ${RTL_COUNT} dongle(s)"
    fi
  fi
  if [ "$AIRSPY_PRESENT" -eq 0 ] && [ "$RTL_PRESENT" -eq 0 ]; then
    info "No SDR hardware detected (yet)."
  fi
  return 0
}

hw_account_serials() {
  # ── CLAIM-SAFE SERIAL ACCOUNTING ──────────────────────────
  # Enumerate every RTL serial already claimed by an ACTIVE decoder with a
  # named serial in /etc/default, subtract from detected, and only ever assign
  # from the FREE set. readsb is handled separately: if it's healthy we consume
  # its output (its dongle is moot); if we're (re)configuring readsb ourselves,
  # its old serial is ours to reuse — so readsb is NOT in this claims list.
  _decoder_serial() {
    local conf="$1" s=""
    [ -f "$conf" ] || return 0
    s=$(grep -oE '^[A-Za-z_]*SERIAL=[^[:space:]]+' "$conf" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '\042\047')
    if [ -z "$s" ]; then
      s=$(grep -oE '(serial=|--device[ =])[0-9A-Za-z]+' "$conf" 2>/dev/null | head -n1 | sed -E 's/^(serial=|--device[ =])//')
    fi
    printf '%s' "$s" | tr -d '[:space:]'
  }

  CLAIMED_SNS=""
  CLAIMED_REPORT=""
  CLAIMED_COUNT=0
  for pair in "dump1090-fa:$_R/etc/default/dump1090-fa" \
              "dump978-fa:$_R/etc/default/dump978-fa"; do
    svc="${pair%%:*}"; conf="${pair##*:}"
    systemctl is-active --quiet "$svc" 2>/dev/null || continue
    sn=$(_decoder_serial "$conf")
    if [ -n "$sn" ]; then
      CLAIMED_SNS="${CLAIMED_SNS}${sn}"$'\n'
      CLAIMED_REPORT="${CLAIMED_REPORT}    SN ${sn} — in use by ${svc}"$'\n'
      CLAIMED_COUNT=$((CLAIMED_COUNT + 1))
    else
      UNNAMED_CLAIMS=$((UNNAMED_CLAIMS + 1))
      UNNAMED_REPORT="${UNNAMED_REPORT}    ${svc} — active with no named serial, holding a dongle"$'\n'
      warn "${svc} is running without a configured serial — staying cautious about its dongle."
    fi
  done

  FREE_SNS="$RTL_SNS"
  if [ -n "$CLAIMED_SNS" ] && [ -n "$RTL_SNS" ]; then
    FREE_SNS=$(comm -23 <(echo "$RTL_SNS") <(echo "$CLAIMED_SNS" | grep . | sort -u))
  fi
  FREE_COUNT=$(echo "$FREE_SNS" | grep -c . || true)

  if [ "$RTL_PRESENT" -eq 1 ]; then
    info "Dongles — detected: ${RTL_COUNT}, named claims: ${CLAIMED_COUNT}, unnamed claims: ${UNNAMED_CLAIMS}, free: ${FREE_COUNT}"
    [ -n "$CLAIMED_REPORT" ] && printf '%s' "$CLAIMED_REPORT"
    [ -n "$UNNAMED_REPORT" ] && printf '%s' "$UNNAMED_REPORT"
  fi
  return 0
}

# Unattended ADS-B pick from $FREE_SNS: serial containing 1090, else lowest.
_hw_unattended_pick() {
  local p
  p=$(echo "$FREE_SNS" | grep . | LC_ALL=C sort | grep 1090 | head -n1)
  [ -n "$p" ] || p=$(echo "$FREE_SNS" | grep . | LC_ALL=C sort | head -n1)
  printf '%s' "$p"
}

hw_choose_mode() {
  # ── DECODER MODE DECISION (consume-first) ─────────────────
  #   1. healthy readsb       → consume — touch NOTHING
  #   2. healthy dump1090-fa  → consume — touch NOTHING (FR24/PiAware safe)
  #      (unless an Airspy is plugged in — then the operator clearly intends
  #       an upgrade; warn that readsb-install REMOVES dump1090-fa + confirm)
  #   3. Airspy present       → airspy branch (PiAware image → hard stop)
  #   4. free RTL-SDR         → rtlsdr branch (PiAware image → hard stop;
  #                              dongle ONLY from the FREE set)
  #   5. nothing              → install app only, decoder later
  step "DECODER MODE"

  DECODER_MODE=""
  ADSB_SERIAL=""
  VHF_SERIAL=""
  AIRCRAFT_JSON_PATH="/run/readsb/aircraft.json"   # default; consume overrides
  SVC_AFTER="readsb.service"

  if [ "$READSB_HEALTHY" -eq 1 ] && [ "$AIRSPY_PRESENT" -eq 0 ]; then
    DECODER_MODE="consume"
    AIRCRAFT_JSON_PATH="/run/readsb/aircraft.json"
    ok "readsb already feeding — PiLNK will consume its aircraft.json. Installing NO decoder."

  elif [ "$DUMP1090_HEALTHY" -eq 1 ] && [ "$AIRSPY_PRESENT" -eq 0 ]; then
    DECODER_MODE="consume"
    AIRCRAFT_JSON_PATH="/run/dump1090-fa/aircraft.json"
    SVC_AFTER="dump1090-fa.service"
    ok "Healthy dump1090-fa found — PiLNK will read its feed. Installing NO decoder."
    ok "Your FlightAware / FR24 / RadarBox setup stays EXACTLY as it is."

  elif [ "$AIRSPY_PRESENT" -eq 1 ]; then
    DECODER_MODE="airspy"
    if [ "$PIAWARE_IMAGE" -eq 1 ]; then
      err "The Airspy path needs readsb, and readsb will not install on a PiAware"
      err "SD-card image (it refuses, to protect the PiAware config)."
      err "Use a separate non-PiAware box for the Airspy, or reflash with Raspberry Pi OS."
      exit 1
    fi
    if [ "$DUMP1090_HEALTHY" -eq 1 ] && [ -n "$HW_NONINTERACTIVE" ]; then
      # Unattended: nobody can say yes to removing a working dump1090-fa.
      err "An Airspy is plugged in, but a HEALTHY dump1090-fa is also running, and the"
      err "Airspy path would REMOVE it. Unattended, so refusing — nothing was changed."
      exit 1
    fi
    if [ "$DUMP1090_HEALTHY" -eq 1 ]; then
      echo "" >> "$HW_TTY"
      warn "An Airspy is plugged in, but a HEALTHY dump1090-fa is also running."
      warn "The Airspy path installs readsb, which REMOVES dump1090-fa —"
      warn "FlightAware feeding from this box WILL STOP."
      warn "(FR24/RadarBox in network mode are auto re-pointed to readsb and survive.)"
      warn "Best practice: run the Airspy on its own Pi, keep FlightAware on this one."
      printf "  Replace dump1090-fa with readsb and continue? [y/N] " >> "$HW_TTY"
      read -r yn < "$HW_TTY"
      [[ ! "$yn" =~ ^[Yy]$ ]] && { echo "Cancelled — nothing was changed."; exit 1; }
    fi
    ok "Decoder mode: Airspy (airspy_adsb → readsb net-only)"

  elif [ "$RTL_PRESENT" -eq 1 ]; then
    if [ "$PIAWARE_IMAGE" -eq 1 ]; then
      err "readsb will not install on a PiAware SD-card image (it refuses, to"
      err "protect the PiAware config) — and this box has no healthy dump1090-fa"
      err "to consume. Start dump1090-fa first, or use a non-PiAware box."
      exit 1
    fi
    # CLAIM-SAFE pick: PiLNK's readsb only ever takes a FREE dongle.
    if [ "$FREE_COUNT" -eq 0 ]; then
      DECODER_MODE=""
      warn "All ${RTL_COUNT} detected dongle(s) are claimed by other decoders/feeders."
      warn "PiLNK will NOT take a dongle another service is using."
      warn "Add another RTL-SDR and re-run, or free one up. Installing the app only."
    elif [ -n "$HW_NONINTERACTIVE" ]; then
      # UNATTENDED PICK RULE (first boot, nobody to ask). Deterministic, so the
      # same dongles give the same answer on every boot: the free dongle whose
      # serial contains 1090 (the ADS-B convention) is ADS-B; otherwise the
      # lowest free serial is. The VHF dongle is then the next free one (below).
      # Pre-existing claims are still honoured: only the FREE set is ever used.
      DECODER_MODE="rtlsdr"
      ADSB_SERIAL=$(_hw_unattended_pick)
      [ "$UNNAMED_CLAIMS" -gt 0 ] && warn "${UNNAMED_CLAIMS} feeder(s) hold a dongle we can't name; unattended pick used the free set only."
      ok "Decoder mode: RTL-SDR — ADS-B → SN $ADSB_SERIAL (unattended pick)"
    elif [ "$UNNAMED_CLAIMS" -gt 0 ]; then
      # Something (e.g. fr24feed direct mode) holds a dongle we can't name, so
      # the "free" list may still contain the claimed one. A human must pick.
      DECODER_MODE="rtlsdr"
      echo "" >> "$HW_TTY"
      warn "${UNNAMED_CLAIMS} running feeder(s) hold a dongle whose serial can't be read,"
      warn "so one of the dongles listed below may actually be THEIRS."
      warn "Pick the dongle that is FREE for PiLNK — NOT the one your feeder uses:"
      echo "$FREE_SNS" | grep . | nl -s '. SN: ' >> "$HW_TTY"
      printf "  Enter the line number of the FREE dongle for ADS-B (or 0 to skip): " >> "$HW_TTY"
      read -r ADSB_PICK < "$HW_TTY"
      ADSB_PICK=$(printf '%s' "${ADSB_PICK:-0}" | grep -oE '^[0-9]+' || echo 0)
      if [ "$ADSB_PICK" -eq 0 ]; then
        DECODER_MODE=""
        warn "Skipped — installing the app only. Re-run when a free dongle is added."
      else
        ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | sed -n "${ADSB_PICK}p")
        if [ -z "$ADSB_SERIAL" ]; then
          DECODER_MODE=""
          warn "Invalid pick — installing the app only, decoder unconfigured."
        else
          ok "Decoder mode: RTL-SDR — ADS-B → SN $ADSB_SERIAL (operator-confirmed free)"
        fi
      fi
    elif [ "$FREE_COUNT" -eq 1 ]; then
      DECODER_MODE="rtlsdr"
      ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | head -n1)
      ok "Decoder mode: RTL-SDR — single free dongle SN $ADSB_SERIAL → ADS-B"
    else
      DECODER_MODE="rtlsdr"
      echo "" >> "$HW_TTY"
      warn "Found $FREE_COUNT free RTL-SDR dongles:"
      echo "$FREE_SNS" | grep . | nl -s '. SN: ' >> "$HW_TTY"
      printf "  Which line is your ADS-B dongle? [1]: " >> "$HW_TTY"
      read -r ADSB_PICK < "$HW_TTY"
      ADSB_PICK=$(printf '%s' "${ADSB_PICK:-1}" | grep -oE '^[0-9]+' || echo 1)
      ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | sed -n "${ADSB_PICK}p")
      if [ -z "$ADSB_SERIAL" ]; then ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | head -n1); fi
      ok "Decoder mode: RTL-SDR — ADS-B → SN $ADSB_SERIAL"
    fi

  else
    DECODER_MODE=""
    warn "No Airspy and no RTL-SDR detected, and no existing decoder to consume."
    warn "PiLNK will still install; plug in a receiver and re-run."
  fi
  return 0
}

hw_assign_vhf() {
  # ── VHF audio dongle assignment (claim-safe) ──────────────
  # VHF audio uses a FREE RTL-SDR that is NOT the ADS-B dongle. If an unnamed
  # claim exists (e.g. fr24feed direct mode) we cannot prove the leftover dongle
  # is free, so we leave VHF off rather than risk poaching — the operator can
  # set 'vhf_serial' in ~/pilnk/config.json by hand if they know better.
  if [ "$RTL_PRESENT" -eq 1 ] && [ "$DECODER_MODE" != "" ]; then
    if [ "$UNNAMED_CLAIMS" -gt 0 ]; then
      VHF_CAND=""
      info "Unnamed dongle claims present — VHF audio left off to stay safe."
      info "(Set 'vhf_serial' in ~/pilnk/config.json yourself if you have a spare.)"
    else
      # Unattended consume-readsb: readsb's own dongle (its --device pin) is not
      # free for VHF, or a re-run after a successful bind would hand it over.
      PINNED_SN=""
      if [ -n "$HW_NONINTERACTIVE" ] && [ "$DECODER_MODE" = "consume" ] && [ "$READSB_HEALTHY" -eq 1 ]; then
        PINNED_SN=$(grep -oE -- '--device[ =][0-9A-Za-z]+' "$_R/etc/default/readsb" 2>/dev/null | head -n1 | sed -E 's/^--device[ =]//')
      fi
      VHF_CAND=$(echo "$FREE_SNS" | grep . | grep -v "^${ADSB_SERIAL}$" | { [ -n "$PINNED_SN" ] && grep -v "^${PINNED_SN}$" || cat; } | head -n1)
    fi
    if [ -n "$VHF_CAND" ]; then
      VHF_SERIAL="$VHF_CAND"
      ok "VHF audio → free RTL-SDR SN $VHF_SERIAL"
    elif [ "$UNNAMED_CLAIMS" -eq 0 ]; then
      info "No spare free dongle for VHF audio — audio left off."
    fi
  fi
  return 0
}

hw_pin_readsb() {
    # Pin readsb to the chosen FREE ADS-B dongle so it can't auto-grab device
    # index 0 — which could be a dongle another feeder owns.
    READSB_DEFAULT="$_R/etc/default/readsb"
    if [ -n "$ADSB_SERIAL" ] && [ -f "$READSB_DEFAULT" ]; then
      if grep -q -- "--device " "$READSB_DEFAULT"; then
        _hw_sudo sed -i "s/--device [^ ]*/--device $ADSB_SERIAL/" "$READSB_DEFAULT"
      else
        _hw_sudo sed -i "s|RECEIVER_OPTIONS=\"|RECEIVER_OPTIONS=\"--device $ADSB_SERIAL |" "$READSB_DEFAULT"
      fi
      ok "readsb pinned to ADS-B dongle SN $ADSB_SERIAL"
      # Only on the real root: a fake PILNK_ROOT must never reach the host.
      if [ -z "$_R" ]; then
        _hw_sudo udevadm control --reload-rules 2>/dev/null || true
        _hw_sudo udevadm trigger 2>/dev/null || true
        _hw_sudo systemctl restart readsb 2>/dev/null || true
        sleep 2
      fi
    fi
  return 0
}

hw_dashboard_port() {
  # ── Dashboard port (claim-safe) ────────────────────────────
  # PiLNK serves its dashboard on 5000. That is NOT always free: a PiAware
  # SD-card image ships piaware-configurator, a Flask app bound to
  # 127.0.0.1:5000. Because a wildcard bind (0.0.0.0) collides with an existing
  # loopback bind on the same port, PiLNK dies with EADDRINUSE and systemd
  # restart-loops it — silently, behind a misleading "check your connection".
  #
  # So: probe, and if 5000 is taken pick the next free port and write it to
  # config.json. It MUST live in config (gitignored) rather than app.py, because
  # update.sh does `git reset --hard` and would revert an app.py edit on the
  # next release — which is exactly how one node went down twice.
  DASHBOARD_PORT=""
  # Definitive test: actually TRY the bind Flask will make. Not a proxy for the
  # question — it IS the question. Parsing `ss` output would miss the subtlety
  # here (something holds 127.0.0.1:5000, and it's the WILDCARD bind that fails),
  # and `ss`/`netstat` aren't guaranteed present on a minimal image — a missing
  # tool would silently report "free" and we'd ship the collision. python3 is
  # guaranteed: it runs app.py.
  port_free() {
    python3 - "$1" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(('0.0.0.0', int(sys.argv[1])))
    sys.exit(0)          # free
except OSError:
    sys.exit(1)          # in use
finally:
    s.close()
PYEOF
  }
  for CAND in 5000 5001 5002 5003 5004 5005; do
    if port_free "$CAND"; then DASHBOARD_PORT="$CAND"; break; fi
  done

  if [ -z "$DASHBOARD_PORT" ]; then
    DASHBOARD_PORT=5000
    err "Ports 5000-5005 are all in use. Free one, or set dashboard_port in"
    err "$HOME/pilnk/config.json by hand, then: sudo systemctl restart pilnk"
  elif [ "$DASHBOARD_PORT" != "5000" ]; then
    # Name the occupant if we can — "port 5000 is in use by piaware-configurator"
    # is actionable; "port 5000 is in use" sends someone hunting.
    PORT_OWNER=$(ss -tlnpH "sport = :5000" 2>/dev/null | grep -oE 'users:\(\("[^"]+' | head -n1 | sed 's/.*(("//')
    warn "Port 5000 is already in use${PORT_OWNER:+ by ${PORT_OWNER}} — using ${DASHBOARD_PORT} instead."
  fi
  return 0
}


# Run the whole decision (read-only). Sets DECODER_MODE, ADSB_SERIAL,
# VHF_SERIAL, AIRCRAFT_JSON_PATH, SVC_AFTER. May exit 1 (hard stops).
hw_decide() {
  hw_scan_feeders
  hw_scan_hardware
  hw_account_serials
  hw_choose_mode
  hw_assign_vhf
  return 0
}

hw_print_plan() {
  printf 'DECODER_MODE=%s\n' "${DECODER_MODE:-none}"
  printf 'ADSB_SERIAL=%s\n' "$ADSB_SERIAL"
  printf 'VHF_SERIAL=%s\n' "$VHF_SERIAL"
  printf 'AIRCRAFT_JSON_PATH=%s\n' "$AIRCRAFT_JSON_PATH"
  printf 'SVC_AFTER=%s\n' "$SVC_AFTER"
}

# Merge aircraft_json_path / vhf_serial / dashboard_port into config.json,
# keeping every other key (pilnk_code etc). An unreadable existing file is an
# error, never overwritten.
hw_write_config() {
  local cfg="$PILNK_DIR/config.json" rc=0 keep
  keep=$(python3 - "$cfg" <<'PYEOF' 2>/dev/null
import json, sys
try:
    v = json.load(open(sys.argv[1])).get("dashboard_port", "")
    print(v if type(v) is int else "")
except Exception:
    pass
PYEOF
)
  # A re-run must not move a port PiLNK itself is already holding.
  if [ -n "$keep" ]; then DASHBOARD_PORT="$keep"; else hw_dashboard_port; fi
  mkdir -p "$PILNK_DIR"
  python3 - "$cfg" "$AIRCRAFT_JSON_PATH" "$VHF_SERIAL" "$DASHBOARD_PORT" <<'PYEOF' || rc=$?
import json, os, shutil, sys, tempfile
path, ajp, vhf, port = sys.argv[1:5]
if os.path.exists(path):
    try:
        with open(path) as f:
            d = json.load(f)
        assert isinstance(d, dict)
    except Exception:
        sys.stderr.write("config.json exists but is not a JSON object; not touching it\n")
        sys.exit(3)
else:
    d = {"auto_update": True}
d["aircraft_json_path"] = ajp
d["vhf_serial"] = vhf
d["dashboard_port"] = int(port)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".config.json.")
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
if os.path.exists(path):
    shutil.copymode(path, tmp)
    st = os.stat(path)
else:
    os.chmod(tmp, 0o644)
    st = os.stat(os.path.dirname(path) or ".")
if os.geteuid() == 0:
    os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PYEOF
  if [ "$rc" -ne 0 ]; then err "could not write $cfg"; return 1; fi
  ok "config.json: aircraft_json_path → ${AIRCRAFT_JSON_PATH}; vhf_serial → ${VHF_SERIAL:-none}; dashboard_port → ${DASHBOARD_PORT}"
  return 0
}

# VHF engine config (serial check, config, udev, service) — no compile.
hw_run_radio_config() {
  [ -n "$VHF_SERIAL" ] || return 0
  if [ ! -f "$PILNK_DIR/pilnkradio-install.sh" ]; then
    warn "pilnkradio-install.sh not found in $PILNK_DIR — VHF audio not configured"
    return 0
  fi
  PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_CONFIG_ONLY=1 PILNKRADIO_SERIAL="$VHF_SERIAL" \
    bash "$PILNK_DIR/pilnkradio-install.sh" \
    || warn "VHF audio config did not finish — PiLNK is unaffected; it is retried on the next run"
  return 0
}

hw_main() {
  local do_plan=0 do_apply=0 a
  for a in "$@"; do
    case "$a" in
      --plan)  do_plan=1 ;;
      --apply) do_apply=1 ;;
      -h|--help) sed -n '/^# Usage:/,/^# Sourced/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "pilnk-hw-setup.sh: unknown option $a (use --plan or --apply)" >&2; return 1 ;;
    esac
  done
  if [ "$do_plan" -eq 0 ] && [ "$do_apply" -eq 0 ]; then
    echo "usage: pilnk-hw-setup.sh [--plan] [--apply]" >&2; return 1
  fi
  PILNK_DIR="${PILNK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  hw_decide >&2                    # claim reports go to the terminal, never into the plan
  hw_print_plan
  if [ -z "$DECODER_MODE" ]; then
    return 2                       # nothing found/free: nothing written
  fi
  if [ "$do_apply" -eq 1 ]; then
    if [ "$DECODER_MODE" = "rtlsdr" ]; then
      hw_pin_readsb
      # pinned = exactly one --device[ =] option, and it is ours (whole serial;
      # wiedehopf's --device-type is not a pin)
      if ! grep -qE -- "--device[ =]${ADSB_SERIAL}([ \"]|\$)" "$_R/etc/default/readsb" 2>/dev/null \
         || [ "$(grep -oE -- '--device[ =]' "$_R/etc/default/readsb" 2>/dev/null | wc -l)" -ne 1 ]; then
        err "readsb was not pinned to SN $ADSB_SERIAL alone (no RECEIVER_OPTIONS line, or another --device option, in $_R/etc/default/readsb?)"
        return 1
      fi
    fi
    hw_write_config || return 1
    hw_run_radio_config
  fi
  return 0
}

# Run only when executed, not when sourced by install.sh.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -e
  hw_main "$@"
  exit $?
fi
