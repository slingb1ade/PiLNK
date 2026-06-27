#!/bin/bash
# ╔═══════════════════════════════════════════════════════╗
# ║   PiLNK Installer  v3.1                                ║
# ║   Trixie / readsb / Airspy — CLAIM-SAFE edition        ║
# ║   pilnk.io  |  Built in Auckland NZ                    ║
# ╠═══════════════════════════════════════════════════════╣
# ║  HARDWARE RULE: PiLNK never touches a dongle that      ║
# ║  another decoder/feeder is using (FR24, PiAware,       ║
# ║  RadarBox, dump1090-fa, dump978-fa).                   ║
# ║                                                         ║
# ║  Decoder decision (consume-first):                      ║
# ║    healthy readsb      → consume its aircraft.json     ║
# ║    healthy dump1090-fa → consume its aircraft.json     ║
# ║                          (FlightAware/FR24 untouched)  ║
# ║    Airspy              → airspy_adsb → readsb net-only ║
# ║    free RTL-SDR        → readsb (local rtlsdr)         ║
# ║                                                         ║
# ║  Run AS YOUR USER (not sudo):                           ║
# ║    curl -sSL https://pilnk.io/install.sh | bash         ║
# ╚═══════════════════════════════════════════════════════╝

set -e

# ── Colour helpers ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
info() { printf "${CYAN}→ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}⚠ %s${RESET}\n" "$1"; }
err()  { printf "${RED}✗ %s${RESET}\n" "$1"; }
step() { printf "\n${BOLD}${BLUE}[ %s ]${RESET}\n" "$1"; }

# ── Don't run as root ─────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
  err "Don't run this as root / with sudo."
  err "Run it as your normal user — it'll ask for sudo when it needs it:"
  err "  curl -sSL https://pilnk.io/install.sh | bash"
  exit 1
fi

# ── /dev/tty safety ───────────────────────────────────────
if ! (echo "" > /dev/tty) 2>/dev/null; then
  warn "/dev/tty unavailable. Run via:"
  warn "  curl -sSL https://pilnk.io/install.sh > /tmp/install.sh && bash /tmp/install.sh"
  exit 1
fi

# ── Banner ─────────────────────────────────────────────────
clear
# Braille radar (rendered from the real pilnk.io hero-radar SVG) + PiLNK wordmark.
# 256-colour blues; degrades gracefully to plain text on basic terminals.
RADAR='\033[38;5;39m'   # radar dish/sweep — bright blue
WORD='\033[38;5;111m'   # PiLNK wordmark — periwinkle
printf "\n"
printf "  ${RADAR}⠀⠀⠀⠀⠀⠀⠀⠀⣀⡠⠤⠤⡤⣤⣄⣀⠀⠀⠀⠀⠀⠀⠀⠀${RESET}\n"
printf "  ${RADAR}⠀⠀⠀⠀⢀⡠⠒⠉⠀⠀⠀⠀⣷⣿⣿⣿⣿⣶⣄⡀⠀⠀⠀⠀${RESET}\n"
printf "  ${RADAR}⠀⠀⠀⡰⠋⠀⠀⠀⠀⠀⠀⠈⣿⣿⣿⣿⣿⡿⠇⠙⢆⠀⠀⠀${RESET}\n"
printf "  ${RADAR}⠀⠀⡜⠀⠀⠀⠀⠀⠀⠀⠀⠠⡿⣿⣿⣿⡿⣿⣇⠀⠀⢣⠀⠀${RESET}   ${WORD}${BOLD}██████╗ ██╗██╗     ███╗   ██╗██╗  ██╗${RESET}\n"
printf "  ${RADAR}⠀⡸⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡃⠀⠉⠳⡄⠀⠈⠄⠀⠀⢇⠀${RESET}   ${WORD}${BOLD}██╔══██╗██║██║     ████╗  ██║██║ ██╔╝${RESET}\n"
printf "  ${RADAR}⠀⡇⠀⠀⠀⠀⠀⡀⠀⢀⠀⣠⣄⣑⣀⣀⣀⣀⣀⣀⣀⣀⣸⠀${RESET}   ${WORD}${BOLD}██████╔╝██║██║     ██╔██╗ ██║█████╔╝ ${RESET}\n"
printf "  ${RADAR}⠀⡇⠀⠀⠀⠀⠀⠁⠀⠈⠀⠙⠋⠉⠉⠉⠉⠉⠉⠉⠉⠉⢹⠀${RESET}   ${WORD}${BOLD}██╔═══╝ ██║██║     ██║╚██╗██║██╔═██╗ ${RESET}\n"
printf "  ${RADAR}⠀⢱⠀⠀⠀⠀⠀⠀⣤⡄⠀⠈⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⡎⠀${RESET}   ${WORD}${BOLD}██║     ██║███████╗██║ ╚████║██║  ██╗${RESET}\n"
printf "  ${RADAR}⠀⠀⢣⠀⠀⠀⠀⠀⠉⠀⠀⠐⠂⠀⢠⡄⠀⠀⠀⠀⠀⡜⠀⠀${RESET}   ${WORD}${BOLD}╚═╝     ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝${RESET}\n"
printf "  ${RADAR}⠀⠀⠀⠱⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠁⠀⠀⠀⣠⠎⠀⠀⠀${RESET}\n"
printf "  ${RADAR}⠀⠀⠀⠀⠈⠑⠤⣀⠀⠀⠀⠀⠀⠀⠀⠀⣀⠤⠊⠁⠀⠀⠀⠀${RESET}\n"
printf "  ${RADAR}⠀⠀⠀⠀⠀⠀⠀⠀⠉⠑⠒⠒⠒⠒⠊⠉⠀⠀⠀⠀⠀⠀⠀⠀${RESET}\n"
echo ""
printf "  ${CYAN}Aviation Intelligence Network — v3.1 · Trixie / readsb / Airspy · claim-safe${RESET}\n"
printf "  ${CYAN}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Pre-flight checks ──────────────────────────────────────
step "PRE-FLIGHT CHECKS"

if ! curl -s --max-time 6 https://github.com > /dev/null 2>&1; then
  err "No internet connection to github.com detected."
  err "The app, readsb, and airspy_adsb are all fetched from GitHub."
  exit 1
fi
ok "Internet connection (github.com reachable)"

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
  err "Python 3.8 or higher is required (found $PY_VER)"
  exit 1
fi
ok "Python $PY_VER"

if command -v pip3 &>/dev/null; then
  ok "pip3 available"
else
  info "pip3 not found — will install during setup"
fi

# ── OS Check ───────────────────────────────────────────────
step "OS CHECK"
OS_ID=$(grep ^ID= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')
OS_VER=$(grep ^VERSION_CODENAME= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')
OS_PRETTY=$(grep ^PRETTY_NAME= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)

if echo "$OS_VER" | grep -qi "trixie"; then
  ok "Debian 13 (Trixie) detected — readsb decoder path"
elif echo "$OS_VER" | grep -qi "bookworm"; then
  ok "Debian 12 (Bookworm) detected — readsb decoder path"
elif echo "$OS_VER" | grep -qi "bullseye"; then
  ok "Debian 11 (Bullseye) detected — readsb decoder path"
else
  warn "Detected: ${OS_PRETTY:-Unknown OS}"
  warn "Untested OS — proceeding best-effort with readsb."
  printf "  Continue anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
fi

case "$ARCH" in
  arm64|armhf|armel) ok "Architecture: $ARCH (ARM — Raspberry Pi or compatible)" ;;
  amd64)             ok "Architecture: $ARCH (x86_64)" ;;
  *)                 warn "Architecture: $ARCH (untested — readsb compiles from source, should be fine)" ;;
esac

# ── No questions ───────────────────────────────────────────
step "CONFIGURATION"
echo ""
printf "  No setup questions — PiLNK installs itself.\n"
printf "  After install, this node shows a short ${BOLD}pairing code${RESET}.\n"
printf "  You'll claim it on pilnk.io (Profile) to link it to your account,\n"
printf "  then set its location on the map there. Nothing to type here.\n"
echo ""
printf "  Ready to install? [Y/n] " > /dev/tty
read -r yn < /dev/tty
[[ "$yn" =~ ^[Nn]$ ]] && echo "Cancelled." && exit 0
echo ""

# ── System update ──────────────────────────────────────────
step "SYSTEM UPDATE"
sudo apt-get update -qq || { err "apt-get update failed — check your internet connection"; exit 1; }
ok "Package lists updated"

# ── System dependencies ───────────────────────────────────
step "SYSTEM DEPENDENCIES"
info "Installing system packages..."
sudo apt-get install -y \
  python3 python3-pip python3-venv \
  git curl wget usbutils \
  rtl-sdr librtlsdr-dev libusb-1.0-0-dev \
  gcc make ncurses-dev zlib1g-dev pkg-config libc6-dev \
  sox ffmpeg \
  2>/dev/null || true
ok "System packages installed"

# Blacklist the DVB driver stack so the kernel doesn't grab RTL-SDR dongles
# before readsb can. We blacklist the WHOLE stack, not just the top module:
# with two dongles enumerating (e.g. one ADS-B + one airband) the lower
# modules (rtl2832 / dvb_usb_v2) can still race in and cause an intermittent
# "usb_claim_interface error -6" — the dongle drops and re-enumerates, which
# strobes the node. The full set is the community-recommended blacklist.
# Written unconditionally (idempotent) so an EXISTING node that only had the
# single-line blacklist gets upgraded to the full set on its next run/update.
BLCONF=/etc/modprobe.d/blacklist-rtlsdr.conf
sudo tee "$BLCONF" > /dev/null <<'BLEOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2830
blacklist dvb_usb_v2
BLEOF
# Unload any that are currently loaded (ignore if not present / in use).
for m in dvb_usb_rtl28xxu rtl2832_sdr rtl2832 rtl2830 dvb_usb_v2; do
  sudo rmmod "$m" 2>/dev/null || true
done
ok "DVB driver stack blacklisted (prevents dongle conflicts, incl. dual-dongle nodes)"

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
if systemctl is-active --quiet readsb 2>/dev/null && [ -s /run/readsb/aircraft.json ]; then
  READSB_HEALTHY=1
  ok "readsb is already running and healthy (/run/readsb/aircraft.json live)"
fi

DUMP1090_HEALTHY=0
if systemctl is-active --quiet dump1090-fa 2>/dev/null && [ -s /run/dump1090-fa/aircraft.json ]; then
  DUMP1090_HEALTHY=1
  ok "dump1090-fa is running and healthy (FlightAware/PiAware feeder detected)"
fi

PIAWARE_IMAGE=0
if [ -f /boot/piaware-config.txt ] || [ -f /boot/firmware/piaware-config.txt ]; then
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
  if grep -qiE '^[[:space:]]*receiver[[:space:]]*=[[:space:]]*"?(dvbt|sdr)' /etc/fr24feed.ini 2>/dev/null; then
    UNNAMED_CLAIMS=$((UNNAMED_CLAIMS + 1))
    UNNAMED_REPORT="${UNNAMED_REPORT}    fr24feed (FlightRadar24) — DIRECT SDR mode, holding a dongle"$'\n'
    warn "fr24feed is running in direct SDR mode — it owns one RTL dongle itself."
  else
    info "fr24feed is running in network mode (consumes a decoder feed — holds no dongle)."
  fi
fi
if systemctl is-active --quiet rbfeeder 2>/dev/null; then
  if grep -qiE '^[[:space:]]*network_mode[[:space:]]*=[[:space:]]*(false|no)' /etc/rbfeeder.ini 2>/dev/null; then
    UNNAMED_CLAIMS=$((UNNAMED_CLAIMS + 1))
    UNNAMED_REPORT="${UNNAMED_REPORT}    rbfeeder (RadarBox) — DIRECT SDR mode, holding a dongle"$'\n'
    warn "rbfeeder is running in direct SDR mode — it owns one RTL dongle itself."
  else
    info "rbfeeder is running in network mode (holds no dongle)."
  fi
fi

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
for pair in "dump1090-fa:/etc/default/dump1090-fa" \
            "dump978-fa:/etc/default/dump978-fa"; do
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
  if [ "$DUMP1090_HEALTHY" -eq 1 ]; then
    echo "" > /dev/tty
    warn "An Airspy is plugged in, but a HEALTHY dump1090-fa is also running."
    warn "The Airspy path installs readsb, which REMOVES dump1090-fa —"
    warn "FlightAware feeding from this box WILL STOP."
    warn "(FR24/RadarBox in network mode are auto re-pointed to readsb and survive.)"
    warn "Best practice: run the Airspy on its own Pi, keep FlightAware on this one."
    printf "  Replace dump1090-fa with readsb and continue? [y/N] " > /dev/tty
    read -r yn < /dev/tty
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
  elif [ "$UNNAMED_CLAIMS" -gt 0 ]; then
    # Something (e.g. fr24feed direct mode) holds a dongle we can't name, so
    # the "free" list may still contain the claimed one. A human must pick.
    DECODER_MODE="rtlsdr"
    echo "" > /dev/tty
    warn "${UNNAMED_CLAIMS} running feeder(s) hold a dongle whose serial can't be read,"
    warn "so one of the dongles listed below may actually be THEIRS."
    warn "Pick the dongle that is FREE for PiLNK — NOT the one your feeder uses:"
    echo "$FREE_SNS" | grep . | nl -s '. SN: ' > /dev/tty
    printf "  Enter the line number of the FREE dongle for ADS-B (or 0 to skip): " > /dev/tty
    read -r ADSB_PICK < /dev/tty
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
    echo "" > /dev/tty
    warn "Found $FREE_COUNT free RTL-SDR dongles:"
    echo "$FREE_SNS" | grep . | nl -s '. SN: ' > /dev/tty
    printf "  Which line is your ADS-B dongle? [1]: " > /dev/tty
    read -r ADSB_PICK < /dev/tty
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
    VHF_CAND=$(echo "$FREE_SNS" | grep . | grep -v "^${ADSB_SERIAL}$" | head -n1)
  fi
  if [ -n "$VHF_CAND" ]; then
    VHF_SERIAL="$VHF_CAND"
    ok "VHF audio → free RTL-SDR SN $VHF_SERIAL"
  elif [ "$UNNAMED_CLAIMS" -eq 0 ]; then
    info "No spare free dongle for VHF audio — audio left off."
  fi
fi

# ── Install readsb (wiedehopf) — ONLY when WE provide the decoder ──
if [ "$DECODER_MODE" = "airspy" ] || [ "$DECODER_MODE" = "rtlsdr" ]; then
  step "ADS-B DECODER (readsb)"
  if command -v readsb &>/dev/null; then
    ok "readsb already installed"
  else
    info "Installing readsb — compiles from source, takes a few minutes..."
    sudo bash -c "$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)" || {
      err "readsb install failed."
      err "See https://github.com/wiedehopf/adsb-scripts/wiki/Automatic-installation-for-readsb"
      exit 1
    }
    ok "readsb installed"
  fi
fi

# ── Decoder branch ────────────────────────────────────────
if [ "$DECODER_MODE" = "airspy" ]; then
  step "AIRSPY DECODER (airspy_adsb)"
  # wiedehopf's airspy-conf does the whole job: downloads the right airspy_adsb
  # binary for this arch+libc (Trixie glibc 2.41 → bookworm build), installs a
  # systemd service + udev rules, and RECONFIGURES the readsb we just installed
  # to --net-only consuming airspy_adsb's Beast stream on port 47787.
  info "Installing + configuring airspy_adsb (wiedehopf airspy-conf)..."
  if sudo bash -c "$(wget -O - https://raw.githubusercontent.com/wiedehopf/airspy-conf/master/install.sh)"; then
    ok "airspy_adsb installed; readsb reconfigured to net-only"
  else
    err "airspy-conf failed. readsb is installed but has no Airspy feed yet."
    err "Manual: https://github.com/wiedehopf/airspy-conf"
    warn "Continuing — PiLNK will install but report 0 aircraft until the Airspy feeds readsb."
  fi
  sleep 2
  if systemctl is-active --quiet airspy_adsb 2>/dev/null; then
    ok "airspy_adsb service is running"
  else
    warn "airspy_adsb not active yet — check: sudo systemctl status airspy_adsb"
  fi

elif [ "$DECODER_MODE" = "rtlsdr" ]; then
  step "PIN RTL-SDR DONGLE"
  # Pin readsb to the chosen FREE ADS-B dongle so it can't auto-grab device
  # index 0 — which could be a dongle another feeder owns.
  READSB_DEFAULT="/etc/default/readsb"
  if [ -n "$ADSB_SERIAL" ] && [ -f "$READSB_DEFAULT" ]; then
    if grep -q -- "--device " "$READSB_DEFAULT"; then
      sudo sed -i "s/--device [^ ]*/--device $ADSB_SERIAL/" "$READSB_DEFAULT"
    else
      sudo sed -i "s|RECEIVER_OPTIONS=\"|RECEIVER_OPTIONS=\"--device $ADSB_SERIAL |" "$READSB_DEFAULT"
    fi
    ok "readsb pinned to ADS-B dongle SN $ADSB_SERIAL"
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger 2>/dev/null || true
    sudo systemctl restart readsb 2>/dev/null || true
    sleep 2
  fi

elif [ "$DECODER_MODE" = "consume" ]; then
  step "CONSUME MODE"
  ok "Using the existing decoder's feed at ${AIRCRAFT_JSON_PATH}"
  ok "No decoder installed, no dongle touched, no existing service modified."
fi

# Decoder health (skip when nothing to check)
if [ "$DECODER_MODE" = "airspy" ] || [ "$DECODER_MODE" = "rtlsdr" ]; then
  if systemctl is-active --quiet readsb 2>/dev/null; then
    ok "readsb service is running"
  else
    warn "readsb not active yet — check: sudo systemctl status readsb"
  fi
fi

# ── Clone / update PiLNK ──────────────────────────────────
step "PiLNK APPLICATION"
PILNK_DIR="$HOME/pilnk"

if [ -d "$PILNK_DIR/.git" ]; then
  info "Updating existing PiLNK installation..."
  cd "$PILNK_DIR"
  git pull -q origin main 2>/dev/null || git pull -q origin master 2>/dev/null || true
  ok "PiLNK updated"
else
  info "Cloning PiLNK..."
  git clone -q https://github.com/slingb1ade/PiLNK.git "$PILNK_DIR" 2>/dev/null || {
    err "Could not clone PiLNK from GitHub."
    err "Check that https://github.com/slingb1ade/PiLNK is reachable."
    exit 1
  }
  ok "PiLNK cloned"
fi
cd "$PILNK_DIR"
APP_PY="$PILNK_DIR/app.py"
CONFIG_JSON="$PILNK_DIR/config.json"

# ── Python packages ────────────────────────────────────────
step "PYTHON PACKAGES"
info "Installing Python packages..."
sudo pip3 install \
  flask flask-socketio flask-cors requests numpy python-dotenv pyModeS \
  --break-system-packages -q 2>/dev/null || \
sudo pip install \
  flask flask-socketio flask-cors requests numpy python-dotenv pyModeS \
  --break-system-packages -q 2>/dev/null || true
ok "Python packages installed"

# ── Write config.json ─────────────────────────────────────
# The decoder JSON path is a config key (aircraft_json_path, app.py 1.2.10.1+).
# Consume mode points it at the EXISTING decoder's JSON; readsb modes use
# /run/readsb/aircraft.json. No app.py edit → auto_update stays TRUE (OTA).
#
# Phase 2 pairing: NO pilnk_code, lat or lon are written here. On first boot
# app.py sees no code, registers with pilnk.io, and shows a pairing code the
# operator claims from their profile — the real verify_code then flows down and
# app.py writes it here itself. Location is set on pilnk.io and adopted from the
# ping response (see _adopt_server_location() in app.py).
step "NODE CONFIG"
if [ ! -f "$APP_PY" ]; then
  err "app.py not found — the GitHub clone may have failed."
  exit 1
fi
if ! grep -q "aircraft_json_path" "$APP_PY"; then
  warn "This app.py predates the aircraft_json_path key (1.2.10.1)."
  warn "The node may read the dump1090-fa path instead of the configured one. Update PiLNK."
fi
printf '{\n  "auto_update": true,\n  "aircraft_json_path": "%s",\n  "vhf_serial": "%s"\n}\n' "$AIRCRAFT_JSON_PATH" "$VHF_SERIAL" \
  > "$CONFIG_JSON"
ok "config.json written — pairing code will show on first boot (aircraft_json_path → ${AIRCRAFT_JSON_PATH}; vhf_serial → ${VHF_SERIAL:-none}; auto_update ON)"

# Keep config.json + secret out of git (survives any future pull)
GITIGNORE="$PILNK_DIR/.gitignore"
touch "$GITIGNORE"
grep -q "config.json" "$GITIGNORE" 2>/dev/null || echo "config.json" >> "$GITIGNORE"
grep -q ".secret_key" "$GITIGNORE" 2>/dev/null || echo ".secret_key" >> "$GITIGNORE"

# Disable whisper import if present (crashes without faster-whisper)
if grep -q "^from whisper_atc" "$APP_PY"; then
  sed -i 's/^from whisper_atc/# from whisper_atc  # disabled/' "$APP_PY"
  ok "Whisper import disabled (not installed)"
fi

# ── Syntax check app.py ────────────────────────────────────
step "SYNTAX CHECK"
if python3 -m py_compile "$APP_PY" 2>/dev/null; then
  ok "app.py syntax OK"
else
  err "app.py has a syntax error — restoring clean copy from GitHub..."
  git checkout app.py 2>/dev/null || true
  if python3 -m py_compile "$APP_PY" 2>/dev/null; then
    ok "app.py restored"
  else
    err "Could not fix app.py — please report at pilnk.io/forum"
    exit 1
  fi
fi

# ── Systemd service ───────────────────────────────────────
step "SERVICE SETUP"
SERVICE_FILE="/etc/systemd/system/pilnk.service"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=PiLNK — The Open Source ATC Network (Trixie/readsb/Airspy)
After=network.target ${SVC_AFTER}
Wants=${SVC_AFTER}

[Service]
Type=simple
User=$USER
WorkingDirectory=$PILNK_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $PILNK_DIR/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable pilnk -q
sudo systemctl restart pilnk 2>/dev/null || sudo systemctl start pilnk 2>/dev/null || true
sleep 3

if systemctl is-active --quiet pilnk; then
  ok "PiLNK service is running"
else
  warn "Service may still be starting — check: sudo systemctl status pilnk"
  warn "If it fails: sudo journalctl -u pilnk -n 20"
fi

# ── Pi IP ─────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')

# ── Success! ──────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "\n  ${GREEN}${BOLD}🎉 PiLNK installed!${RESET}\n\n"
printf "  ${BOLD}Dashboard:${RESET}    http://$PI_IP:5000\n"
if [ "$DECODER_MODE" = "airspy" ] || [ "$DECODER_MODE" = "rtlsdr" ]; then
printf "  ${BOLD}Decoder map:${RESET}  http://$PI_IP/tar1090   ${CYAN}(readsb's own view)${RESET}\n"
fi
printf "  ${BOLD}Pairing:${RESET}      open the dashboard — a pairing code shows at the top\n"
printf "  ${BOLD}Claim it:${RESET}     pilnk.io → Profile → enter the code to link this node\n"
printf "  ${BOLD}Location:${RESET}     set it on pilnk.io → Profile → your node\n"
case "$DECODER_MODE" in
  airspy)  printf "  ${BOLD}Decoder:${RESET}      Airspy → airspy_adsb → readsb (net-only)\n" ;;
  rtlsdr)  printf "  ${BOLD}Decoder:${RESET}      RTL-SDR → readsb (SN ${ADSB_SERIAL:-?})\n" ;;
  consume) printf "  ${BOLD}Decoder:${RESET}      existing feed → ${AIRCRAFT_JSON_PATH} ${GREEN}(nothing touched)${RESET}\n" ;;
  *)       printf "  ${BOLD}Decoder:${RESET}      ${YELLOW}none configured — plug in a receiver and re-run${RESET}\n" ;;
esac
[ -n "$VHF_SERIAL" ] && printf "  ${BOLD}VHF audio:${RESET}    RTL-SDR SN ${VHF_SERIAL} → PiLNK rtl_fm\n"
echo ""
printf "  ${CYAN}Open Chrome → http://$PI_IP:5000 — aircraft appear within ~30s.${RESET}\n"
echo ""
printf "  ${YELLOW}Notes:${RESET}\n"
case "$DECODER_MODE" in
  consume)
printf "    • PiLNK is CONSUMING your existing decoder's feed — FlightAware/FR24/\n"
printf "      RadarBox were not touched. If that decoder stops, PiLNK shows 0 aircraft.\n"
    ;;
  airspy)
printf "    • Decoder is readsb, fed by airspy_adsb.\n"
printf "    • No aircraft? Check http://$PI_IP/tar1090 and:\n"
printf "      sudo systemctl status airspy_adsb readsb\n"
    ;;
  rtlsdr)
printf "    • Decoder is readsb on its own FREE dongle (SN ${ADSB_SERIAL:-?}).\n"
printf "    • No aircraft? Check http://$PI_IP/tar1090 and: sudo systemctl status readsb\n"
    ;;
  *)
printf "    • No decoder configured yet — plug in an RTL-SDR or Airspy and re-run.\n"
    ;;
esac
printf "    • auto_update is ON (no local app edit — config key handles the path).\n"
echo ""
printf "  ${BLUE}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
