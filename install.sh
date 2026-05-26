#!/bin/bash
# ╔═══════════════════════════════════════════════════════╗
# ║         PiLNK Installer  v2.10                       ║
# ║         pilnk.io  |  Built in Auckland NZ            ║
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

# ── /dev/tty safety ───────────────────────────────────────
if ! (echo "" > /dev/tty) 2>/dev/null; then
  warn "/dev/tty unavailable. Run via:"
  warn "  curl pilnk.io/install.sh > /tmp/install.sh && bash /tmp/install.sh"
  exit 1
fi

# ── Banner ─────────────────────────────────────────────────
clear
printf "${BLUE}"
cat << 'EOF'
  ██████╗ ██╗██╗     ███╗   ██╗██╗  ██╗
  ██╔══██╗██║██║     ████╗  ██║██║ ██╔╝
  ██████╔╝██║██║     ██╔██╗ ██║█████╔╝
  ██╔═══╝ ██║██║     ██║╚██╗██║██╔═██╗
  ██║     ██║███████╗██║ ╚████║██║  ██╗
  ╚═╝     ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝
EOF
printf "${RESET}"
echo ""
printf "  ${CYAN}Aviation Intelligence Network — v2.10${RESET}\n"
printf "  ${CYAN}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Pre-flight checks ──────────────────────────────────────
step "PRE-FLIGHT CHECKS"

# Internet connection
if ! curl -s --max-time 5 https://pilnk.io > /dev/null 2>&1; then
  err "No internet connection detected."
  err "Please check your network and try again."
  exit 1
fi
ok "Internet connection"

# Python version
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
  err "Python 3.8 or higher is required (found $PY_VER)"
  exit 1
fi
ok "Python $PY_VER"

# pip3
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

# Block Trixie (Debian 13) — not currently supported on any architecture
if echo "$OS_VER" | grep -qi "trixie"; then
  echo ""
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  err " UNSUPPORTED OPERATING SYSTEM"
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  warn "Detected: $OS_PRETTY"
  echo ""
  printf "  PiLNK does not currently support Debian Trixie (13).\n"
  printf "  Please use ${GREEN}Bookworm${RESET}:\n"
  printf "    Pi:      Raspberry Pi OS Bookworm — https://www.raspberrypi.com/software/\n"
  printf "    Server:  Debian 12 Bookworm        — https://www.debian.org/distrib/\n"
  echo ""
  err "Installation aborted."
  exit 1
fi

# Warn (don't block) on Ubuntu — most things work, but it's not the
# primary tested target. amd64 users on Ubuntu can opt in at their
# own risk.
if echo "$OS_ID" | grep -qi "ubuntu"; then
  warn "Ubuntu detected — PiLNK is primarily tested on Debian/Raspberry Pi OS."
  warn "Most things should work but you may hit edge cases."
  printf "  Continue anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
fi

# Warn on unknown OS codenames (still allow continue)
if ! echo "$OS_VER" | grep -qi "bookworm\|bullseye"; then
  warn "Detected: ${OS_PRETTY:-Unknown OS}"
  warn "PiLNK is tested on Bookworm and Bullseye."
  printf "  Continue anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
else
  ok "OS: $OS_PRETTY"
fi

# Architecture banner — drives dump1090-fa repo selection later
case "$ARCH" in
  arm64|armhf|armel)
    ok "Architecture: $ARCH (ARM — Raspberry Pi or compatible)"
    ;;
  amd64)
    ok "Architecture: $ARCH (x86_64 server)"
    ;;
  *)
    warn "Architecture: $ARCH (untested — proceeding with best-effort fallback)"
    ;;
esac

# ── PiAware Detection ─────────────────────────────────────
PIAWARE_IMAGE=false
if [ -f /etc/piaware.conf ] || dpkg -l piaware &>/dev/null 2>&1; then
  PIAWARE_IMAGE=true
  ok "PiAware detected — will use existing dump1090-fa"
  # Fix stale FlightAware apt mirror (causes 404 errors)
  if ls /etc/apt/sources.list.d/*flightaware* &>/dev/null 2>&1; then
    info "Fixing FlightAware apt mirror..."
    for f in /etc/apt/sources.list.d/*flightaware*; do
      if grep -q "flightaware.com/mirror" "$f" 2>/dev/null; then
        sudo sed -i 's/^deb /#deb /' "$f" 2>/dev/null
      fi
    done
    sudo apt-get update -qq 2>/dev/null || true
    ok "FlightAware mirror fixed"
  fi
fi

# ── Three questions ────────────────────────────────────────
step "CONFIGURATION"
echo ""
printf "  Answer three questions and PiLNK installs itself.\n"
printf "  Your PiLNK Code is on your pilnk.io profile page.\n"
echo ""

# Latitude
printf "${CYAN}  Latitude${RESET}  (e.g. -36.90490 for Auckland): " > /dev/tty
read -r LAT < /dev/tty
if ! echo "$LAT" | grep -qE '^-?[0-9]+\.?[0-9]*$'; then
  err "Invalid latitude. Use decimal format, e.g. -36.90490"
  exit 1
fi

# Longitude
printf "${CYAN}  Longitude${RESET} (e.g. 174.76788 for Auckland): " > /dev/tty
read -r LON < /dev/tty
if ! echo "$LON" | grep -qE '^-?[0-9]+\.?[0-9]*$'; then
  err "Invalid longitude. Use decimal format, e.g. 174.76788"
  exit 1
fi

# PiLNK Code (8 hex characters from profile page)
printf "${CYAN}  PiLNK Code${RESET} (8 characters from pilnk.io → Profile): " > /dev/tty
read -r CODE < /dev/tty
CODE=$(echo "$CODE" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
if echo "$CODE" | grep -qE '^[A-F0-9]{8}$'; then
  ok "PiLNK Code accepted"
elif echo "$CODE" | grep -qE '^[A-Z0-9]{4}-[A-Z0-9]{4}$'; then
  warn "That looks like an old-format code (has a dash)."
  warn "New accounts use 8-character codes without a dash."
  warn "Check your profile page at pilnk.io for your PiLNK Code."
  printf "  Continue with $CODE anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
else
  err "Invalid PiLNK Code."
  err "Your code is 8 characters, e.g. 4E4F196F"
  err "Find it at pilnk.io → Profile"
  exit 1
fi

echo ""
printf "  ${BOLD}Lat:${RESET} $LAT  ${BOLD}Lon:${RESET} $LON  ${BOLD}Code:${RESET} $CODE\n"
echo ""
printf "  Ready to install? [Y/n] " > /dev/tty
read -r yn < /dev/tty
[[ "$yn" =~ ^[Nn]$ ]] && echo "Cancelled." && exit 0

echo ""
printf "  ${GREEN}Starting installation...${RESET}\n"
echo ""
printf "\n  Press ENTER to begin, or Ctrl+C to cancel " > /dev/tty
read -r < /dev/tty
echo ""

# ── System update ──────────────────────────────────────────
step "SYSTEM UPDATE"
sudo apt-get update -qq || { err "apt-get update failed — check your internet connection"; exit 1; }
ok "Package lists updated"

# ── Install ALL system dependencies ───────────────────────
step "SYSTEM DEPENDENCIES"
info "Installing all required system packages..."
sudo apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  git \
  curl \
  wget \
  rtl-sdr \
  librtlsdr-dev \
  sox \
  ffmpeg \
  2>/dev/null || true
ok "System packages installed"

# Blacklist DVB driver so it doesn't grab RTL-SDR dongles
if [ ! -f /etc/modprobe.d/blacklist-rtlsdr.conf ]; then
  echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf > /dev/null
  sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true
  ok "DVB driver blacklisted (prevents dongle conflicts)"
fi

# Install dump1090-fa if not present (skip on PiAware — already installed)
if command -v dump1090-fa &>/dev/null; then
  ok "dump1090-fa already present"
elif [ "$PIAWARE_IMAGE" = true ]; then
  ok "PiAware image — dump1090-fa managed by PiAware"
else
  info "Installing dump1090-fa..."

  # Architecture-aware repo selection. FlightAware's official apt repo
  # only ships ARM packages. amd64 users are served by the well-known
  # abcd567a community repo (long-time FA contributor, recommended in
  # FA's own forum threads for x86_64 installs).
  case "$ARCH" in
    arm64|armhf|armel)
      if ! ls /etc/apt/sources.list.d/*flightaware* &>/dev/null 2>&1; then
        info "Adding FlightAware repository (ARM)..."
        wget -qO /tmp/flightaware-repo.deb "https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb" 2>/dev/null || true
        if [ -f /tmp/flightaware-repo.deb ] && [ -s /tmp/flightaware-repo.deb ]; then
          sudo dpkg -i /tmp/flightaware-repo.deb 2>/dev/null || true
          sudo apt-get update -qq 2>/dev/null || true
          ok "FlightAware repository added"
        fi
      fi
      ;;
    amd64)
      # abcd567a community repo for amd64. Path is per-Debian-version:
      #   Bookworm (Debian 12) → /debian12/
      # Modern keyring location (/etc/apt/keyrings/) per Debian convention.
      # Errors are deliberately not suppressed so download failures
      # (404, network, etc.) surface immediately instead of silently
      # leaving 0-byte files that apt then can't use.
      if [ ! -s /etc/apt/sources.list.d/abcd567a.list ]; then
        info "Adding abcd567a community repository (amd64 / Bookworm)..."
        sudo mkdir -p /etc/apt/keyrings
        if ! sudo wget -O /etc/apt/sources.list.d/abcd567a.list https://abcd567a.github.io/debian12/abcd567a.list; then
          err "Failed to download abcd567a.list"
          sudo rm -f /etc/apt/sources.list.d/abcd567a.list
          exit 1
        fi
        if ! sudo wget -O /etc/apt/keyrings/abcd567a-key.gpg https://abcd567a.github.io/debian12/KEY2.gpg; then
          err "Failed to download abcd567a GPG key"
          sudo rm -f /etc/apt/keyrings/abcd567a-key.gpg
          exit 1
        fi
        sudo apt-get update
        ok "abcd567a repository added"
      fi
      ;;
    *)
      warn "Architecture $ARCH has no known dump1090-fa repo path"
      warn "Will attempt apt install but expect failure"
      ;;
  esac

  # Try apt install — works for both arm and amd64 repo paths above
  if sudo apt-get install -y dump1090-fa 2>/dev/null; then
    ok "dump1090-fa installed via apt"
  else
    # ARM-only fallback: direct .deb download from FlightAware
    case "$ARCH" in
      arm64|armhf|armel)
        info "apt failed — trying direct download from FlightAware..."
        wget -qO /tmp/dump1090-fa.deb "https://flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware-support/dump1090-fa_10.1_${ARCH}.deb" 2>/dev/null || true
        if [ -f /tmp/dump1090-fa.deb ] && [ -s /tmp/dump1090-fa.deb ] && sudo dpkg -i /tmp/dump1090-fa.deb 2>/dev/null; then
          sudo apt-get install -f -y 2>/dev/null || true
          ok "dump1090-fa installed (direct download)"
        else
          warn "dump1090-fa could not be auto-installed"
          warn "Install manually — see https://discussions.flightaware.com/c/adsb"
        fi
        ;;
      amd64)
        warn "dump1090-fa could not be auto-installed for amd64"
        warn "Manual install:"
        warn "  https://github.com/abcd567a/piaware-ubuntu-debian-amd64"
        ;;
      *)
        warn "dump1090-fa could not be auto-installed for $ARCH"
        ;;
    esac
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
    mkdir -p "$PILNK_DIR/templates"
    warn "Could not clone from GitHub — creating minimal structure"
  }
  ok "PiLNK cloned"
fi
cd "$PILNK_DIR"

# ── Python packages ────────────────────────────────────────
step "PYTHON PACKAGES"
info "Installing all required Python packages..."

# Install base packages first
sudo pip3 install \
  flask \
  flask-socketio \
  flask-cors \
  requests \
  numpy \
  python-dotenv \
  --break-system-packages -q 2>/dev/null || \
sudo pip install \
  flask \
  flask-socketio \
  flask-cors \
  requests \
  numpy \
  python-dotenv \
  --break-system-packages -q 2>/dev/null || true
ok "Base Python packages installed"

# faster-whisper removed — ATC transcription disabled until v2.0

# ── Write location to dump1090-fa config ─────────────────
step "LOCATION CONFIGURATION"
DUMP_CONF="/etc/default/dump1090-fa"
if [ -f "$DUMP_CONF" ]; then
  sudo cp "$DUMP_CONF" "${DUMP_CONF}.bak"
  if grep -q "^RECEIVER_LAT=" "$DUMP_CONF"; then
    sudo sed -i "s/^RECEIVER_LAT=.*/RECEIVER_LAT=$LAT/" "$DUMP_CONF"
  else
    echo "RECEIVER_LAT=$LAT" | sudo tee -a "$DUMP_CONF" > /dev/null
  fi
  if grep -q "^RECEIVER_LON=" "$DUMP_CONF"; then
    sudo sed -i "s/^RECEIVER_LON=.*/RECEIVER_LON=$LON/" "$DUMP_CONF"
  else
    echo "RECEIVER_LON=$LON" | sudo tee -a "$DUMP_CONF" > /dev/null
  fi
  ok "Location written to $DUMP_CONF"
else
  sudo mkdir -p /etc/default
  printf "RECEIVER_LAT=$LAT\nRECEIVER_LON=$LON\n" | sudo tee "$DUMP_CONF" > /dev/null
  ok "Created $DUMP_CONF with location"
fi

# ── SDR dongle detection + claim-safe assignment ─────────
# HARDWARE RULE: PiLNK only ever touches a dongle that NOTHING else is
# using. We enumerate every serial already claimed by an ACTIVE decoder
# (dump1090-fa, dump978-fa, readsb), subtract those from what rtl_test
# can see, and assign ONLY from the free set:
#   0 free  → assign nothing; tell the operator (never poach a busy one)
#   1 free  → ADS-B, the primary job (VHF left unconfigured)
#   2+ free → ask which is ADS-B (interactive); remaining free → VHF
# No serial is hardcoded as "the ADS-B one" or "the VHF one". Legacy SN
# 00000002 is a last-resort VHF fallback ONLY when present AND free.
step "SDR DONGLE DETECTION"

VHF_SERIAL=""          # set only from the free set; empty = no VHF dongle
ADSB_SERIAL=""         # free dongle chosen for ADS-B (or one dump1090-fa already owns)
LEGACY_SN="00000002"

# Pull a decoder's configured serial out of its /etc/default file.
# Handles RECEIVER_SERIAL="x" and an inline serial=x / --device x in an
# options string. Echoes the serial, or nothing.
_decoder_serial() {
  local conf="$1" s=""
  [ -f "$conf" ] || return 0
  s=$(grep -oE '^[A-Za-z_]*SERIAL=[^[:space:]]+' "$conf" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '\042\047')
  if [ -z "$s" ]; then
    s=$(grep -oE '(serial=|--device[ =])[0-9A-Za-z]+' "$conf" 2>/dev/null | head -n1 | sed -E 's/^(serial=|--device[ =])//')
  fi
  printf '%s' "$s" | tr -d '[:space:]'
}

# Set of serials currently CLAIMED by an active decoder (+ a human report).
CLAIMED_SNS=""
CLAIMED_REPORT=""
CLAIMED_COUNT=0
for pair in "dump1090-fa:/etc/default/dump1090-fa" \
            "dump978-fa:/etc/default/dump978-fa" \
            "readsb:/etc/default/readsb"; do
  svc="${pair%%:*}"; conf="${pair##*:}"
  systemctl is-active --quiet "$svc" 2>/dev/null || continue
  sn=$(_decoder_serial "$conf")
  if [ -n "$sn" ]; then
    CLAIMED_SNS="${CLAIMED_SNS}${sn}"$'\n'
    CLAIMED_REPORT="${CLAIMED_REPORT}    SN ${sn} — in use by ${svc}"$'\n'
    CLAIMED_COUNT=$((CLAIMED_COUNT + 1))
    if [ "$svc" = "dump1090-fa" ]; then ADSB_SERIAL="$sn"; fi
  else
    CLAIMED_REPORT="${CLAIMED_REPORT}    (serial not configured) — ${svc} active, holding a dongle"$'\n'
    warn "${svc} is running without a configured serial — can't name the dongle it holds; staying cautious."
  fi
done

if ! command -v rtl_test &>/dev/null; then
  warn "rtl_test not available — cannot enumerate dongles."
  warn "VHF audio left unconfigured; add a dongle and set 'vhf_serial' in ~/pilnk/config.json later."
else
  RTL_OUT=$(timeout 3 rtl_test 2>&1 || true)
  DETECTED_SNS=$(echo "$RTL_OUT" | grep -oE 'SN: [0-9A-Za-z]+' | awk '{print $2}' | grep . | sort -u)
  DETECTED_COUNT=$(echo "$DETECTED_SNS" | grep -c . || true)

  if [ -n "$CLAIMED_SNS" ]; then
    FREE_SNS=$(comm -23 <(echo "$DETECTED_SNS") <(echo "$CLAIMED_SNS" | grep . | sort -u))
  else
    FREE_SNS="$DETECTED_SNS"
  fi
  FREE_COUNT=$(echo "$FREE_SNS" | grep -c . || true)

  info "Dongles — detected: ${DETECTED_COUNT}, claimed by other decoders: ${CLAIMED_COUNT}, free: ${FREE_COUNT}"
  if [ -n "$CLAIMED_REPORT" ]; then printf '%s' "$CLAIMED_REPORT"; fi

  if [ "$DETECTED_COUNT" -eq 0 ]; then
    warn "No RTL-SDR dongles detected — nothing to assign."
    warn "Plug a dongle in and re-run if you want ADS-B and/or VHF audio."
  elif [ "$FREE_COUNT" -eq 0 ]; then
    warn "All detected dongles are already in use by other decoders — PiLNK will NOT take one."
    if [ -n "$ADSB_SERIAL" ]; then
      ok "ADS-B is already served by dump1090-fa on SN ${ADSB_SERIAL} — leaving it alone."
    else
      warn "PiLNK needs its own FREE dongle for ADS-B. Add another RTL-SDR and re-run, or free one up."
      warn "Continuing install with ADS-B left unconfigured (non-fatal)."
    fi
  elif [ "$FREE_COUNT" -eq 1 ]; then
    ONLY_FREE=$(echo "$FREE_SNS" | grep . | head -n1)
    if [ -n "$ADSB_SERIAL" ]; then
      VHF_SERIAL="$ONLY_FREE"
      ok "ADS-B already on SN ${ADSB_SERIAL} (dump1090-fa); lone free SN ${ONLY_FREE} → VHF audio."
    else
      ADSB_SERIAL="$ONLY_FREE"
      ok "Single free dongle SN ${ONLY_FREE} → ADS-B (primary job). VHF left unconfigured."
      info "Add a second dongle later for VHF audio."
    fi
  else
    if [ -n "$ADSB_SERIAL" ]; then
      VHF_SERIAL=$(echo "$FREE_SNS" | grep . | head -n1)
      ok "ADS-B already on SN ${ADSB_SERIAL}; free SN ${VHF_SERIAL} → VHF audio."
    elif [ -r /dev/tty ]; then
      echo "" > /dev/tty
      warn "${FREE_COUNT} free dongles — which is ADS-B (the primary job)?"
      echo "$FREE_SNS" | grep . | nl -s '. SN: ' > /dev/tty
      printf "  Enter the line number for ADS-B [1]: " > /dev/tty
      read -r ADSB_PICK < /dev/tty
      ADSB_PICK=$(printf '%s' "${ADSB_PICK:-1}" | grep -oE '^[0-9]+' || echo 1)
      ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | sed -n "${ADSB_PICK}p")
      if [ -z "$ADSB_SERIAL" ]; then ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | head -n1); fi
      ok "ADS-B → SN ${ADSB_SERIAL}"
      VHF_SERIAL=$(echo "$FREE_SNS" | grep . | grep -v "^${ADSB_SERIAL}$" | head -n1)
      if [ -n "$VHF_SERIAL" ]; then ok "VHF audio → SN ${VHF_SERIAL}"; fi
    else
      ADSB_SERIAL=$(echo "$FREE_SNS" | grep . | head -n1)
      VHF_SERIAL=$(echo "$FREE_SNS" | grep . | grep -v "^${ADSB_SERIAL}$" | head -n1)
      warn "Non-interactive install: assumed SN ${ADSB_SERIAL} for ADS-B${VHF_SERIAL:+, SN ${VHF_SERIAL} for VHF}."
      warn "Re-run interactively or edit configs to change."
    fi
  fi

  # Last-resort legacy VHF fallback: ONLY if VHF still unset AND the legacy
  # SN is present, free, and not the ADS-B pick. Never an assumption.
  if [ -z "$VHF_SERIAL" ] && [ "$ADSB_SERIAL" != "$LEGACY_SN" ] && echo "$FREE_SNS" | grep -q "^${LEGACY_SN}$"; then
    VHF_SERIAL="$LEGACY_SN"
    info "VHF audio → legacy SN ${LEGACY_SN} (present and free)."
  fi
fi

# Pin dump1090-fa to the chosen ADS-B serial — only when we picked a NEW
# free dongle (it wasn't already serving it) and it is NOT PiAware-managed.
# This makes ADS-B deterministic instead of letting dump1090-fa auto-grab
# device-index 0, which could be a dongle another decoder owns.
if [ -n "$ADSB_SERIAL" ] && [ "$PIAWARE_IMAGE" != true ] && [ -f /etc/default/dump1090-fa ]; then
  CURRENT_ADSB=$(_decoder_serial /etc/default/dump1090-fa)
  if [ "$CURRENT_ADSB" != "$ADSB_SERIAL" ]; then
    if grep -q '^RECEIVER_SERIAL=' /etc/default/dump1090-fa; then
      sudo sed -i "s/^RECEIVER_SERIAL=.*/RECEIVER_SERIAL=\"${ADSB_SERIAL}\"/" /etc/default/dump1090-fa
    else
      echo "RECEIVER_SERIAL=\"${ADSB_SERIAL}\"" | sudo tee -a /etc/default/dump1090-fa > /dev/null
    fi
    ok "Pinned dump1090-fa to ADS-B dongle SN ${ADSB_SERIAL} (takes effect on dump1090-fa restart)."
  fi
fi

# ── Assignment summary ───────────────────────────────────
echo ""
ok "Dongle assignment summary:"
ok "  ADS-B : ${ADSB_SERIAL:-<none — add a free dongle and re-run>}"
ok "  VHF   : ${VHF_SERIAL:-<none assigned>}"
if [ "$CLAIMED_COUNT" -gt 0 ]; then
  ok "  Left ${CLAIMED_COUNT} dongle(s) owned by other decoders untouched (listed above)."
fi


# ── Write PiLNK Code to config.json ───────────────────────
step "PiLNK CODE"
APP_PY="$PILNK_DIR/app.py"
CONFIG_JSON="$PILNK_DIR/config.json"

# Write config.json (gitignored — survives git pull)
# Source of truth for node identity: code, location, OTA preference, VHF dongle serial.
printf '{\n  "pilnk_code": "%s",\n  "lat": %s,\n  "lon": %s,\n  "auto_update": true,\n  "vhf_serial": "%s"\n}\n' "$CODE" "$LAT" "$LON" "$VHF_SERIAL" > "$CONFIG_JSON"
ok "PiLNK Code + location + VHF serial saved to config.json"

# Ensure config.json is gitignored (survives git pull)
GITIGNORE="$PILNK_DIR/.gitignore"
touch "$GITIGNORE"
grep -q "config.json" "$GITIGNORE" 2>/dev/null || echo "config.json" >> "$GITIGNORE"
grep -q ".secret_key" "$GITIGNORE" 2>/dev/null || echo ".secret_key" >> "$GITIGNORE"

# Ensure app.py exists
if [ ! -f "$APP_PY" ]; then
  warn "app.py not found — it should come from the GitHub clone above"
  warn "Check that https://github.com/slingb1ade/PiLNK is accessible"
  exit 1
fi

# Ensure whisper import is commented out (crashes without faster-whisper)
if grep -q "^from whisper_atc" "$APP_PY"; then
  sed -i 's/^from whisper_atc/# from whisper_atc  # disabled until v2.0/' "$APP_PY"
  ok "Whisper import disabled (not installed)"
fi

# ── Syntax check app.py ────────────────────────────────────
step "SYNTAX CHECK"
if python3 -m py_compile "$APP_PY" 2>/dev/null; then
  ok "app.py syntax OK"
else
  err "app.py has a syntax error — attempting restore from GitHub..."
  cd "$PILNK_DIR" && git checkout app.py 2>/dev/null || true
  if python3 -m py_compile "$APP_PY" 2>/dev/null; then
    ok "app.py restored successfully"
  else
    err "Could not fix app.py — please report this at pilnk.io/forum"
    exit 1
  fi
fi

# ── Systemd service ───────────────────────────────────────
step "SERVICE SETUP"
SERVICE_FILE="/etc/systemd/system/pilnk.service"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=PiLNK — The Open Source ATC Network
After=network.target

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
  warn "Service may still be starting — check with: sudo systemctl status pilnk"
  warn "If it fails, run: sudo journalctl -u pilnk -n 20"
fi

# ── Restart dump1090-fa with new location ────────────────
step "ADS-B RECEIVER"
sudo systemctl restart dump1090-fa 2>/dev/null || true
sleep 1
if systemctl is-active --quiet dump1090-fa 2>/dev/null; then
  ok "dump1090-fa running"
else
  info "dump1090-fa not active (may need SDR dongle plugged in)"
fi

# ── Get Pi's IP ───────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')

# ── Success! ──────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "\n  ${GREEN}${BOLD}🎉 PiLNK installed successfully!${RESET}\n\n"
printf "  ${BOLD}Dashboard:${RESET}    http://$PI_IP:5000\n"
printf "  ${BOLD}Location:${RESET}     $LAT, $LON\n"
printf "  ${BOLD}PiLNK Code:${RESET}  ${GREEN}$CODE${RESET}\n"
echo ""
printf "  ${CYAN}Open Chrome and go to: http://$PI_IP:5000${RESET}\n"
printf "  ${CYAN}Your node will appear on the PiLNK network map${RESET}\n"
printf "  ${CYAN}once it pings pilnk.io (within 30 seconds).${RESET}\n"
echo ""
printf "  ${BLUE}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
