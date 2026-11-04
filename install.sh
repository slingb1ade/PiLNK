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
if [ "${PILNK_IMAGE:-}" != 1 ]; then   # image build: unattended, never asks
  printf "  Ready to install? [Y/n] " > /dev/tty
  read -r yn < /dev/tty
  [[ "$yn" =~ ^[Nn]$ ]] && echo "Cancelled." && exit 0
fi
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

# ── Hardware binding: the claim-safe rules live in pilnk-hw-setup.sh ──
# EXISTING FEEDERS, SDR HARDWARE DETECTION, CLAIM-SAFE SERIAL ACCOUNTING,
# DECODER MODE and VHF audio assignment moved there verbatim, so this
# interactive installer and the unattended first-boot image share ONE copy of
# the rules. Sourced here, it only defines hw_* functions; prompts stay
# interactive (PILNK_HW_NONINTERACTIVE is forced off).
if [ "${PILNK_IMAGE:-}" = 1 ]; then
  # Image build (PILNK_IMAGE=1): no hardware stage. The build Pi has no dongles
  # and PiLNK must never bind or run here; first boot binds the user's hardware.
  # Install readsb (unpinned) and take the dashboard port from the template.
  DECODER_MODE=rtlsdr
  ADSB_SERIAL=""; VHF_SERIAL=""; SVC_AFTER="readsb.service"
  IMAGE_TEMPLATE_REL="image/config.template.json"
  AIRCRAFT_JSON_PATH="/run/readsb/aircraft.json"
  info "Image mode: hardware stage skipped (first boot binds the receivers)"
else
PILNK_HW_NONINTERACTIVE=""
HW_SETUP=""
for _c in "${BASH_SOURCE[0]:+$(dirname "${BASH_SOURCE[0]}")/pilnk-hw-setup.sh}" "$HOME/pilnk/pilnk-hw-setup.sh"; do
  if [ -n "$_c" ] && [ -f "$_c" ]; then HW_SETUP="$_c"; break; fi
done
if [ -z "$HW_SETUP" ]; then
  # curl | bash: nothing alongside us yet — fetch the helper (the repo isn't cloned until later).
  HW_SETUP="$(mktemp /tmp/pilnk-hw-setup.XXXXXX)"
  curl -fsSL --retry 3 --retry-delay 2 --max-time 20 https://raw.githubusercontent.com/slingb1ade/PiLNK/main/pilnk-hw-setup.sh -o "$HW_SETUP" || {
    err "Could not fetch pilnk-hw-setup.sh from GitHub."
    exit 1
  }
fi
# shellcheck source=pilnk-hw-setup.sh
. "$HW_SETUP"
hw_decide
fi  # PILNK_IMAGE

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
  if [ "${PILNK_IMAGE:-}" = 1 ]; then
    # Ships disabled: an enabled, unpinned readsb would grab a dongle at first
    # boot before first boot has bound the hardware.
    sudo systemctl disable --now readsb 2>/dev/null || true
    ok "readsb installed disabled (first boot binds the dongle)"
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
  if [ "${PILNK_IMAGE:-}" != 1 ]; then
    step "PIN RTL-SDR DONGLE"
    hw_pin_readsb
  fi

elif [ "$DECODER_MODE" = "consume" ]; then
  step "CONSUME MODE"
  ok "Using the existing decoder's feed at ${AIRCRAFT_JSON_PATH}"
  ok "No decoder installed, no dongle touched, no existing service modified."
fi

# Decoder health (skip when nothing to check)
if { [ "$DECODER_MODE" = "airspy" ] || [ "$DECODER_MODE" = "rtlsdr" ]; } && [ "${PILNK_IMAGE:-}" != 1 ]; then
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
# Loose upper bounds, not exact pins. Unversioned installs silently partitioned
# the fleet by install DATE: pyModeS 3.6 changed crc_valid from a bool to None
# for Comm-B frames and killed Mode S enrichment on every node installed after
# the change, while older nodes carried on fine with identical code. eventlet's
# presence likewise decides which server flask-socketio uses.
#
# Exact pins would freeze the fleet on 2026 libraries forever and rot. The
# better defence is code that tolerates either version — which is what the
# crc_valid gate and the socketio.run fallback now do — with bounds here only
# to exclude the NEXT major, which is where breaking changes actually live.
#
# update.sh never runs pip, so this affects new installs only and can never
# downgrade a node that is already working.
# powerfloor v1 — hardware tier (spec power-and-hwfloor-spec.md). One line and carry on: no
# prompt, images install headless. A Pi 3 / under-1.5 GiB board gets aircraft-only mode, and the
# ATC audio engine block below skips its build. Exit 3 is caught here: this script runs set -e.
PF_AIRCRAFT_ONLY=0
if [ -f "$PILNK_DIR/hwtier.sh" ]; then
  PF_RC=0
  PF_HW_LINE="$(bash "$PILNK_DIR/hwtier.sh" --audio-ok 2>/dev/null)" || PF_RC=$?
  if [ "$PF_RC" -eq 3 ]; then
    PF_AIRCRAFT_ONLY=1
    echo ""
    warn "$PF_HW_LINE"
    echo ""
  fi
fi

step "PYTHON PACKAGES"
info "Installing Python packages..."
PY_PKGS="flask>=2,<4 flask-socketio>=5,<6 flask-cors>=4,<7 requests>=2,<3 numpy>=1.24,<3 python-dotenv>=1,<2 pyModeS>=2.13,<4"
# shellcheck disable=SC2086
sudo pip3 install $PY_PKGS --break-system-packages -q 2>/dev/null || \
sudo pip install $PY_PKGS --break-system-packages -q 2>/dev/null || \
{ warn "bounded install failed — falling back to unversioned"; \
  sudo pip3 install flask flask-socketio flask-cors requests numpy python-dotenv pyModeS \
    --break-system-packages -q 2>/dev/null || true; }
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
# ── Dashboard port (claim-safe) — probe + rationale live in pilnk-hw-setup.sh ──
if [ "${PILNK_IMAGE:-}" = 1 ]; then
  # Image: config.json is the clean template, byte for byte.
  [ -f "$PILNK_DIR/$IMAGE_TEMPLATE_REL" ] || { err "$IMAGE_TEMPLATE_REL missing from the clone"; exit 1; }
  cp "$PILNK_DIR/$IMAGE_TEMPLATE_REL" "$CONFIG_JSON"
  DASHBOARD_PORT="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["dashboard_port"]))' "$CONFIG_JSON")"
else
hw_dashboard_port

printf '{\n  "auto_update": true,\n  "aircraft_json_path": "%s",\n  "vhf_serial": "%s",\n  "dashboard_port": %s\n}\n' "$AIRCRAFT_JSON_PATH" "$VHF_SERIAL" "$DASHBOARD_PORT" \
  > "$CONFIG_JSON"
fi  # PILNK_IMAGE
ok "config.json written — pairing code will show on first boot (aircraft_json_path → ${AIRCRAFT_JSON_PATH}; vhf_serial → ${VHF_SERIAL:-none}; dashboard_port → ${DASHBOARD_PORT}; auto_update ON)"

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
# Never give up. The default systemd StartLimit (5 starts / 10s) marks the unit
# 'failed' after a brief crash-loop, after which \`systemctl restart\` silently
# no-ops until a human runs \`systemctl reset-failed\` — a documented dead-forever
# trap (one node sat dead after 606 restarts). 0 disables the rate limiter, so a
# node keeps trying to come back on its own, forever. A transient cause (a
# re-seated dongle, a passing resource spike) then self-heals with no visit.
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$PILNK_DIR
Environment=PYTHONUNBUFFERED=1
# app.py logs non-ASCII (✓ → — etc). If the node's locale is C/POSIX rather
# than UTF-8, Python picks ASCII for stdout and dies with UnicodeEncodeError
# at startup. One node hit this twice: the operator fixed it with
# `systemctl set-environment PYTHONIOENCODING=utf-8`, which sets a RUNTIME
# variable on the systemd manager and is lost on reboot — so the node came
# back broken months later with no obvious cause. Setting it in the unit
# makes it persistent. Belt and braces with an explicit UTF-8 locale.
Environment=PYTHONIOENCODING=utf-8
Environment=LC_ALL=C.UTF-8
Environment=LANG=C.UTF-8
ExecStart=/usr/bin/python3 $PILNK_DIR/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
if [ "${PILNK_IMAGE:-}" != 1 ]; then   # image: unit stays disabled; first boot arms it
  sudo systemctl enable pilnk -q
fi

# ── Passwordless restart for unattended OTA ───────────────
# update.sh restarts the service with `sudo -n systemctl restart pilnk` after
# pulling new code. Without a NOPASSWD sudoers rule that non-interactive sudo
# FAILS, so the OTA writes new files but never restarts — the node then runs
# STALE code while reporting the new version (silent drift). We install a
# tightly-scoped rule: this service user may run ONLY the pilnk restart/start/
# stop with no password. Validated with `visudo -c` before install; a malformed
# file is discarded so we can never break sudo on the operator's box.
SUDOERS_FILE="/etc/sudoers.d/pilnk-ota"
SUDOERS_TMP="$(mktemp)"
SYSTEMCTL_BIN="$(command -v systemctl || echo /usr/bin/systemctl)"
cat > "$SUDOERS_TMP" <<SUDOEOF
# PiLNK OTA — allow $USER to restart the pilnk service without a password.
# Created by install.sh. Scoped to the pilnk unit only.
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_BIN restart pilnk, $SYSTEMCTL_BIN start pilnk, $SYSTEMCTL_BIN stop pilnk, $SYSTEMCTL_BIN daemon-reload
SUDOEOF
if sudo visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
  sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
  ok "Passwordless OTA restart enabled (sudoers.d/pilnk-ota)"
else
  warn "Could not validate sudoers rule — skipping (OTA will need a manual restart)"
fi
rm -f "$SUDOERS_TMP"

# Clear any prior failed state FIRST. If a previous install crash-looped, systemd
# hits its start limit and marks the unit failed — after which `restart` silently
# does nothing and a perfectly correct fix still looks broken. That cost a real
# node an extra debugging round: 606 restarts, then reset-failed was the only
# thing standing between it and working. Harmless when there's nothing to reset.
if [ "${PILNK_IMAGE:-}" = 1 ]; then
  ok "Image mode: pilnk.service installed, NOT enabled or started"
else
sudo systemctl reset-failed pilnk 2>/dev/null || true
sudo systemctl restart pilnk 2>/dev/null || sudo systemctl start pilnk 2>/dev/null || true
sleep 3

if systemctl is-active --quiet pilnk; then
  ok "PiLNK service is running"
else
  warn "Service may still be starting — check: sudo systemctl status pilnk"
  warn "If it fails: sudo journalctl -u pilnk -n 20"
fi
fi  # PILNK_IMAGE

# ── Self-heal wiring ──────────────────────────────────────
# bootstrap-selfheal.sh wires the node's self-repair into /etc: the SDR
# recovery timer (every 3 min) + udev hotplug rule (dongle unplug/replug/swap),
# the dashboard watchdog, and the scoped sudo grants. Its header has always said
# it is called by update.sh AND install.sh, but until 26 Sep 2026 install.sh
# never called it: a brand-new node had none of it until its FIRST OTA, so a
# dongle knocked loose in week one needed a human. It runs here, after the
# service is up (the watchdog judges a running dashboard, after a 60 s grace).
# The operator's sudo is still warm, so the bootstrap's `sudo -n` works.
# Idempotent, logs to update.log, and never fatal to the install.
if [ "${PILNK_IMAGE:-}" != 1 ] && [ -f "$PILNK_DIR/bootstrap-selfheal.sh" ]; then
  PILNK_DIR="$PILNK_DIR" bash "$PILNK_DIR/bootstrap-selfheal.sh" 2>/dev/null || true
  if systemctl is-enabled --quiet pilnk-sdr-recover.timer 2>/dev/null; then
    ok "Self-heal wired (receiver unplug/replug recovery, 3-min safety check, dashboard watchdog)"
  else
    warn "Self-heal wiring did not complete — the first automatic update will retry it"
  fi
fi

# ── Pi IP ─────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')

# ── Success! ──────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "\n  ${GREEN}${BOLD}🎉 PiLNK installed!${RESET}\n\n"
printf "  ${BOLD}Dashboard:${RESET}    http://$PI_IP:${DASHBOARD_PORT}\n"
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
if [ -n "$VHF_SERIAL" ]; then
  printf "  ${BOLD}ATC audio:${RESET}    spare RTL-SDR SN ${VHF_SERIAL} reserved ${YELLOW}(engine not installed)${RESET}\n"
  printf "  ${BOLD}          ${RESET}    to enable it:  ${CYAN}bash ~/pilnk/pilnkradio-install.sh${RESET}\n"
fi
echo ""
printf "  ${CYAN}Open Chrome → http://$PI_IP:${DASHBOARD_PORT} — aircraft appear within ~30s.${RESET}\n"
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

# ── ATC audio engine ───────────────────────────────────────
# Installed on EVERY node, not offered. pilnkradio is C++ and must be compiled;
# unlike the rest of PiLNK a git pull doesn't make it work. Leaving it opt-in
# meant the dashboard tab (which self-hides until the engine answers on :5656)
# stayed invisible on every node but the developer's — a shipped feature nobody
# could see, and no bug reports because there was nothing to report.
#
# So: build it here, whether or not a radio dongle is present. A node with no
# spare dongle still gets a working engine that waits, and the udev rule starts
# it the moment one is plugged in. No SSH, no second visit.
#
# Runs in the FOREGROUND here (unlike the OTA path, which detaches): the
# operator is watching a terminal, the compile output is informative, and the
# node is already installed and running by this point. Non-fatal either way.
if [ "${PF_AIRCRAFT_ONLY:-0}" = 1 ]; then
  step "ATC AUDIO ENGINE"
  info "Skipped: aircraft-only board (see the note near the start). PiLNK runs normally."
  echo ""
elif [ -f "$PILNK_DIR/pilnkradio-install.sh" ]; then
  step "ATC AUDIO ENGINE"
  info "Building the live ATC audio engine (~3-5 min). PiLNK itself is already"
  info "installed and running — nothing below can affect your ADS-B feed."
  echo ""
  # Image: build the engine and denoise only; first boot writes the config.
  PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_SERIAL="$VHF_SERIAL" \
    PILNKRADIO_SKIP_CONFIG="$([ "${PILNK_IMAGE:-}" = 1 ] && echo 1)" \
    bash "$PILNK_DIR/pilnkradio-install.sh" \
    || warn "ATC audio engine did not finish — PiLNK is unaffected. Re-run later: bash ~/pilnk/pilnkradio-install.sh"
  echo ""
fi
