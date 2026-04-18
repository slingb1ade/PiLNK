#!/bin/bash
# ╔═══════════════════════════════════════════════════════╗
# ║         PiLNK Installer  v2.4                        ║
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
printf "  ${CYAN}Aviation Intelligence Network — v2.4${RESET}\n"
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
if ! command -v pip3 &>/dev/null; then
  err "pip3 not found — installing..."
  sudo apt-get install -y python3-pip -qq || { err "Could not install pip3"; exit 1; }
fi
ok "pip3 available"

# ── OS Check ───────────────────────────────────────────────
step "OS CHECK"
OS_ID=$(grep ^ID= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')
OS_VER=$(grep ^VERSION_CODENAME= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')
OS_PRETTY=$(grep ^PRETTY_NAME= /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')

# Block Trixie
if echo "$OS_VER" | grep -qi "trixie"; then
  echo ""
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  err " UNSUPPORTED OPERATING SYSTEM"
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  warn "Detected: $OS_PRETTY"
  echo ""
  printf "  PiLNK does not currently support Debian Trixie (13).\n"
  printf "  Please use ${GREEN}Raspberry Pi OS Bookworm${RESET} (recommended).\n"
  echo ""
  printf "  Flash a fresh SD card from: ${CYAN}https://www.raspberrypi.com/software/${RESET}\n"
  echo ""
  err "Installation aborted."
  exit 1
fi

# Block Ubuntu
if echo "$OS_ID" | grep -qi "ubuntu"; then
  err "Ubuntu is not supported. Please use Raspberry Pi OS Bookworm."
  exit 1
fi

# Warn on unknown OS
if ! echo "$OS_VER" | grep -qi "bookworm\|bullseye"; then
  warn "Detected: ${OS_PRETTY:-Unknown OS}"
  warn "PiLNK is tested on Raspberry Pi OS Bookworm and Bullseye."
  printf "  Continue anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
else
  ok "OS: $OS_PRETTY"
fi

# ── Three questions ────────────────────────────────────────
step "CONFIGURATION"
echo ""
printf "  Answer three questions and PiLNK installs itself.\n"
printf "  Your verify code is on your pilnk.io profile page.\n"
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

# Verify code — must be the NODE verify code (not user verify code)
printf "${CYAN}  Verify Code${RESET} (from pilnk.io → Profile → My Node section): " > /dev/tty
read -r CODE < /dev/tty
CODE=$(echo "$CODE" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
# Accept both formats: XXXX-XXXX (user code) or 8 hex chars (node code)
if echo "$CODE" | grep -qE '^[A-Z0-9]{8}$'; then
  ok "Verify code accepted (node format)"
elif echo "$CODE" | grep -qE '^[A-Z0-9]{4}-[A-Z0-9]{4}$'; then
  warn "That looks like a USER verify code (has a dash)."
  warn "Your NODE verify code is different — check Profile → My Node."
  warn "It's 8 characters with no dash, e.g. 4E4F196F"
  printf "  Continue with $CODE anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
else
  err "Invalid verify code format."
  err "Node verify code is 8 characters, e.g. 4E4F196F"
  err "Find it at pilnk.io → Profile → My Node section"
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
  libopenblas-dev \
  ffmpeg \
  libavformat-dev \
  libavcodec-dev \
  libavdevice-dev \
  libavutil-dev \
  libavfilter-dev \
  libswscale-dev \
  libswresample-dev \
  sox \
  2>/dev/null || true
ok "System packages installed"

# Install dump1090-fa if not present
if ! command -v dump1090-fa &>/dev/null; then
  info "Installing dump1090-fa..."
  # Try apt first (works if FlightAware repo is configured)
  if sudo apt-get install -y dump1090-fa 2>/dev/null; then
    ok "dump1090-fa installed via apt"
  else
    # Try direct download
    ARCH=$(dpkg --print-architecture)
    wget -qO /tmp/dump1090-fa.deb "https://flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware-support/dump1090-fa_9.0_${ARCH}.deb" 2>/dev/null || true
    if [ -f /tmp/dump1090-fa.deb ] && sudo dpkg -i /tmp/dump1090-fa.deb 2>/dev/null; then
      ok "dump1090-fa installed"
    else
      warn "dump1090-fa could not be auto-installed"
      warn "Install manually: sudo apt-get install dump1090-fa"
      warn "Or visit: https://flightaware.com/adsb/piaware/install"
    fi
  fi
else
  ok "dump1090-fa already present"
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

# Install faster-whisper separately with fallback
# Requires ffmpeg/libav libs to already be installed (done above)
info "Installing faster-whisper (ATC transcription)..."
if sudo pip3 install faster-whisper --break-system-packages -q 2>/dev/null; then
  ok "faster-whisper installed"
elif sudo pip3 install faster-whisper==0.9.0 --break-system-packages -q 2>/dev/null; then
  ok "faster-whisper 0.9.0 installed (compatibility version)"
else
  warn "faster-whisper could not be installed — ATC transcription disabled"
  warn "Run manually later: sudo apt-get install -y ffmpeg libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libavfilter-dev libswscale-dev libswresample-dev && sudo pip3 install faster-whisper --break-system-packages"
fi

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

# ── Write verify code to app.py ───────────────────────────
step "VERIFY CODE"
APP_PY="$PILNK_DIR/app.py"
if [ -f "$APP_PY" ]; then
  if grep -q "NODE_VERIFY_CODE" "$APP_PY"; then
    sed -i "s/NODE_VERIFY_CODE = .*/NODE_VERIFY_CODE = '$CODE'/" "$APP_PY"
    ok "Verify code written to app.py"
  else
    sed -i "1a NODE_VERIFY_CODE = '$CODE'" "$APP_PY"
    ok "Verify code added to app.py"
  fi
else
  warn "app.py not found — it should come from the GitHub clone above"
  warn "Check that https://github.com/slingb1ade/PiLNK is accessible"
  exit 1
fi

# ── Apply ping fixes ───────────────────────────────────────
step "PING FIXES"

# Fix 1: Add lat/lon to ping payload
if grep -q "'node_stats': get_stats_payload()" "$APP_PY" && ! grep -q "'lat': RX_LAT" "$APP_PY"; then
  sed -i "s/'node_stats': get_stats_payload()/'node_stats': get_stats_payload(),\n                'lat': RX_LAT,\n                'lon': RX_LON/" "$APP_PY"
  ok "Ping now sends receiver lat/lon"
fi

# Fix 2: Handle ground altitude (dump1090 sends alt_baro: "ground")
if grep -q "alt = int(ac.get('alt_baro', 0) or ac.get('alt', 0) or 0)" "$APP_PY"; then
  sed -i "s/alt = int(ac.get('alt_baro', 0) or ac.get('alt', 0) or 0)/alt_raw = ac.get('alt_baro', 0) or ac.get('alt', 0) or 0; alt = 0 if alt_raw == 'ground' else int(alt_raw)/" "$APP_PY"
  ok "Fixed ground altitude handling"
fi

# Fix 3: Add User-Agent header (Cloudflare blocks default Python-urllib)
if grep -q "headers={'Content-Type': 'application/json'}" "$APP_PY" && ! grep -q "User-Agent" "$APP_PY"; then
  sed -i "s/headers={'Content-Type': 'application\/json'}/headers={'Content-Type': 'application\/json', 'User-Agent': 'PiLNK\/1.0'}/" "$APP_PY"
  ok "Added User-Agent header for Cloudflare"
fi

# ── Syntax check app.py ────────────────────────────────────
step "SYNTAX CHECK"
if python3 -m py_compile "$APP_PY" 2>/dev/null; then
  ok "app.py syntax OK"
else
  err "app.py has a syntax error — attempting restore from GitHub..."
  cd "$PILNK_DIR" && git checkout app.py 2>/dev/null || true
  # Re-apply verify code after restore
  sed -i "s/NODE_VERIFY_CODE = .*/NODE_VERIFY_CODE = '$CODE'/" "$APP_PY" 2>/dev/null || true
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
printf "  ${BOLD}Verify Code:${RESET}  ${GREEN}$CODE${RESET}\n"
echo ""
printf "  ${CYAN}Open Chrome and go to: http://$PI_IP:5000${RESET}\n"
printf "  ${CYAN}Your node will appear on the PiLNK network map${RESET}\n"
printf "  ${CYAN}once it pings pilnk.io (within 30 seconds).${RESET}\n"
echo ""
printf "  ${BLUE}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
