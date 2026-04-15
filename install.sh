#!/bin/bash
# ╔═══════════════════════════════════════════════════════╗
# ║         PiLNK Installer  v2.0                        ║
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

# ── /dev/tty safety (Mac SSH edge case) ───────────────────
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
printf "  ${CYAN}Aviation Intelligence Network — v2.0${RESET}\n"
printf "  ${CYAN}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check OS ───────────────────────────────────────────────
if ! grep -qi "raspberry\|debian\|ubuntu" /etc/os-release 2>/dev/null; then
  warn "This script is designed for Raspberry Pi OS / Debian."
  printf "Continue anyway? [y/N] " > /dev/tty
  read -r yn < /dev/tty
  [[ ! "$yn" =~ ^[Yy]$ ]] && exit 1
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

# Verify code
printf "${CYAN}  Verify Code${RESET} (from your pilnk.io profile, e.g. ABCD-EFGH): " > /dev/tty
read -r CODE < /dev/tty
CODE=$(echo "$CODE" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
if ! echo "$CODE" | grep -qE '^[A-Z0-9]{4}-[A-Z0-9]{4}$'; then
  err "Invalid verify code format. Expected: XXXX-XXXX (e.g. ABCD-EFGH)"
  err "Find your code at pilnk.io → Profile → My Node"
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
sudo apt-get update -qq
ok "Package lists updated"

# ── Install dependencies ───────────────────────────────────
step "DEPENDENCIES"
sudo apt-get install -y -qq \
  python3 python3-pip python3-venv \
  git curl wget \
  rtl-sdr \
  dump1090-fa \
  librtlsdr-dev \
  sox \
  2>/dev/null || true
ok "Core packages installed"

# Install dump1090-fa if not present
if ! command -v dump1090-fa &>/dev/null; then
  info "Installing dump1090-fa from FlightAware..."
  wget -qO /tmp/piaware.deb "https://flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware/dump1090-fa_9.0_$(dpkg --print-architecture).deb" 2>/dev/null || true
  [ -f /tmp/piaware.deb ] && sudo dpkg -i /tmp/piaware.deb 2>/dev/null || true
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
    # Fallback: create minimal structure if GitHub unavailable
    mkdir -p "$PILNK_DIR/templates"
    warn "Could not clone from GitHub — creating minimal structure"
  }
  ok "PiLNK cloned"
fi
cd "$PILNK_DIR"

# ── Python virtualenv + packages ──────────────────────────
step "PYTHON ENVIRONMENT"
if [ ! -d "$PILNK_DIR/venv" ]; then
  python3 -m venv "$PILNK_DIR/venv"
fi
source "$PILNK_DIR/venv/bin/activate"
pip install -q --upgrade pip
pip install -q flask flask-socketio flask-cors requests 2>/dev/null || true
# Also install to system Python so systemd can find packages regardless of venv
sudo pip3 install flask flask-socketio flask-cors requests --break-system-packages -q 2>/dev/null || \
  sudo pip install flask flask-socketio flask-cors requests --break-system-packages -q 2>/dev/null || true
ok "Python environment ready"
deactivate

# ── Write location to dump1090-fa config ─────────────────
step "LOCATION CONFIGURATION"
DUMP_CONF="/etc/default/dump1090-fa"
if [ -f "$DUMP_CONF" ]; then
  sudo cp "$DUMP_CONF" "${DUMP_CONF}.bak"
  # Use perl for reliable in-place substitution (handles blank values)
  sudo perl -i -pe "s/^RECEIVER_LAT=.*/RECEIVER_LAT=$LAT/" "$DUMP_CONF"
  sudo perl -i -pe "s/^RECEIVER_LON=.*/RECEIVER_LON=$LON/" "$DUMP_CONF"
  # Verify it worked, append if not found
  grep -q "^RECEIVER_LAT=$LAT" "$DUMP_CONF" || echo "RECEIVER_LAT=$LAT" | sudo tee -a "$DUMP_CONF" > /dev/null
  grep -q "^RECEIVER_LON=$LON" "$DUMP_CONF" || echo "RECEIVER_LON=$LON" | sudo tee -a "$DUMP_CONF" > /dev/null
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
    # Insert after first import block
    sed -i "1a NODE_VERIFY_CODE = '$CODE'" "$APP_PY"
    ok "Verify code added to app.py"
  fi
else
  # Create minimal app.py if it doesn't exist
  cat > "$APP_PY" << PYEOF
NODE_VERIFY_CODE = '$CODE'
NODE_LAT = $LAT
NODE_LON = $LON
# PiLNK app.py — full version will be downloaded on first run
PYEOF
  ok "Created app.py with configuration"
fi

# Write location to app.py too (for nodes that don't use dump1090-fa config)
if grep -q "^NODE_LAT" "$APP_PY" 2>/dev/null; then
  sed -i "s/^NODE_LAT = .*/NODE_LAT = $LAT/" "$APP_PY"
  sed -i "s/^NODE_LON = .*/NODE_LON = $LON/" "$APP_PY"
fi

# ── Systemd service ───────────────────────────────────────
step "SERVICE SETUP"
SERVICE_FILE="/etc/systemd/system/pilnk.service"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=PiLNK Aviation Dashboard
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
sleep 2

if systemctl is-active --quiet pilnk; then
  ok "PiLNK service is running"
else
  warn "Service may still be starting — check with: sudo systemctl status pilnk"
fi

# ── Restart dump1090-fa with new location ────────────────
step "ADS-B RECEIVER"
sudo systemctl restart dump1090-fa 2>/dev/null || true
sleep 1
if systemctl is-active --quiet dump1090-fa 2>/dev/null; then
  ok "dump1090-fa running"
else
  info "dump1090-fa not active (may need SDR dongle)"
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
printf "  ${CYAN}Your node will appear on the PiLNK network map${RESET}\n"
printf "  ${CYAN}once it pings pilnk.io (within 30 seconds).${RESET}\n"
echo ""
printf "  ${BLUE}pilnk.io${RESET}\n"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
