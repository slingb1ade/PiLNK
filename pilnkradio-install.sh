#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  pilnkradio-install.sh — turn a PiLNK node into a radio node
#
#  Enables the v1.3.0 "New-Voices" radio: builds the pilnkradio
#  engine + the REQUIRED rtl-sdr-blog driver fork, generates a
#  per-node config, installs the systemd/udev units and verifies
#  the result. Idempotent — safe to re-run.
#
#  Needs: a PiLNK node on v1.3.0+, a spare RTL-SDR dongle
#  (RTL-SDR Blog V4 recommended) + VHF airband antenna. The
#  build & selftest work with no dongle plugged in; the engine
#  then waits for one.
#
#  Run:   bash ~/pilnk/pilnkradio-install.sh
#  (or the tester one-liner: curl -s https://raw.githubusercontent.com/slingb1ade/PiLNK/main/pilnkradio-install.sh | bash)
# ─────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BOLD='\033[1m'; BLUE='\033[0;34m'; RESET='\033[0m'
ok()   { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
info() { printf "${CYAN}→ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}⚠ %s${RESET}\n" "$1"; }
err()  { printf "${RED}✗ %s${RESET}\n" "$1"; }
step() { printf "\n${BOLD}${BLUE}[ %s ]${RESET}\n" "$1"; }
die()  { err "$1"; exit 1; }

# prompts must survive `curl | bash` (stdin is the pipe) — read from the terminal
TTY=/dev/tty
[ -r "$TTY" ] || TTY=/dev/stdin

[ "$(id -u)" -eq 0 ] && die "Run as a normal user, not root (sudo is used where needed)."
command -v sudo >/dev/null || die "sudo is required."

J=$(nproc 2>/dev/null || echo 2)

# ── [1/8] locate pilnkradio source ─────────────────────────
step "1/8 locate pilnkradio source"
SRC="$HOME/pilnk"
if [ ! -f "$SRC/pilnkradio/main.cpp" ]; then
    warn "~/pilnk has no pilnkradio/ source (node not on v1.3.0+ yet?)"
    info "fetching it from GitHub main instead"
    SRC=$(mktemp -d /tmp/pilnk-src.XXXXXX)
    git clone --depth 1 --branch main https://github.com/slingb1ade/PiLNK "$SRC" \
        || die "clone failed — update the node to v1.3.0+ and re-run"
fi
[ -f "$SRC/pilnkradio/main.cpp" ] || die "pilnkradio source not found in $SRC"
ok "source: $SRC/pilnkradio"

# ── [2/8] build dependencies ───────────────────────────────
step "2/8 build dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential cmake git pkg-config \
    libusb-1.0-0-dev libfftw3-dev curl >/dev/null
ok "deps installed"

# ── [3/8] rtl-sdr-blog driver fork (THE deaf-V4 lesson) ────
# The RTL-SDR Blog V4 REQUIRES this fork. With stock librtlsdr the node
# "works" but is ~10 dB deaf with no error anywhere. Never skip this.
step "3/8 rtl-sdr-blog driver fork"
if [ ! -d "$HOME/rtl-sdr-blog" ]; then
    git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog "$HOME/rtl-sdr-blog"
fi
cmake -B "$HOME/rtl-sdr-blog/build" "$HOME/rtl-sdr-blog" \
    -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON >/dev/null
make -C "$HOME/rtl-sdr-blog/build" -j"$J" >/dev/null
sudo make -C "$HOME/rtl-sdr-blog/build" install >/dev/null
sudo ldconfig
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null
[ -f /usr/local/lib/librtlsdr.so ] || die "fork install missing /usr/local/lib/librtlsdr.so"
ok "driver fork installed to /usr/local"

# ── [4/8] build pilnkradio ─────────────────────────────────
step "4/8 build pilnkradio"
cmake -B "$SRC/pilnkradio/build" "$SRC/pilnkradio" -DRTLSDR_PREFIX=/usr/local >/dev/null
make -C "$SRC/pilnkradio/build" -j"$J" >/dev/null
# hard gate: the binary must link the fork, not stock librtlsdr
if ! ldd "$SRC/pilnkradio/build/pilnkradio" | grep -q '/usr/local/lib/librtlsdr'; then
    ldd "$SRC/pilnkradio/build/pilnkradio" | grep rtlsdr || true
    die "binary linked STOCK librtlsdr — node would be ~10 dB deaf. Aborting."
fi
ok "built and linked against the fork"

# ── [5/8] DSP selftest (no antenna needed) ─────────────────
step "5/8 DSP selftest"
"$SRC/pilnkradio/build/pilnkradio" --selftest || die "selftest FAILED — do not deploy this build"
ok "selftest PASS"
sudo make -C "$SRC/pilnkradio/build" install >/dev/null
ok "installed /usr/local/bin/pilnkradio"

# ── [6/8] dongle serial ────────────────────────────────────
step "6/8 radio dongle"
# every RTL2832/2838 on the bus: "serial<TAB>product"
mapfile -t ALL < <(
    for d in /sys/bus/usb/devices/*/idVendor; do
        dir=${d%/idVendor}
        [ "$(cat "$d" 2>/dev/null)" = "0bda" ] || continue
        case "$(cat "$dir/idProduct" 2>/dev/null)" in 2838|2832) ;; *) continue ;; esac
        printf '%s\t%s\n' "$(cat "$dir/serial" 2>/dev/null || echo '?')" \
                          "$(cat "$dir/product" 2>/dev/null || echo '?')"
    done)
# serials the ADS-B decoder side already claims (best effort)
CLAIMED=$(cat /etc/default/dump1090-fa /etc/default/readsb 2>/dev/null \
          | grep -oE '00[0-9]{6}' | sort -u || true)
CANDIDATES=()
for line in "${ALL[@]:-}"; do
    [ -n "$line" ] || continue
    s=${line%%$'\t'*}; p=${line#*$'\t'}
    if echo "$CLAIMED" | grep -qx "$s"; then
        info "dongle $s ($p) — claimed by the ADS-B decoder, skipping"
    else
        CANDIDATES+=("$s"); info "dongle $s ($p) — available"
    fi
done
DUPES=$(printf '%s\n' "${ALL[@]:-}" | cut -f1 | sort | uniq -d)
if [ -n "$DUPES" ]; then
    warn "TWO dongles share serial(s): $DUPES — the engine pins by serial and cannot tell them apart."
    warn "Fix: unplug the ADS-B dongle, then:  rtl_eeprom -s 00000002   (re-serialize the radio dongle), replug, re-run."
fi
# auto-pick is only safe when the decoder's dongle was positively excluded
# (or no decoder runs here) — otherwise the "available" one may be the
# decoder's own stick published via --device-index instead of a serial.
DECODER_ACTIVE=$(systemctl is-active dump1090-fa readsb 2>/dev/null | grep -cx active || true)
SERIAL=""
if [ ${#CANDIDATES[@]} -eq 1 ] && [ -z "$DUPES" ] \
   && { [ "$DECODER_ACTIVE" -eq 0 ] || [ -n "$CLAIMED" ]; }; then
    SERIAL="${CANDIDATES[0]}"
    ok "radio dongle: $SERIAL"
else
    if [ ${#CANDIDATES[@]} -ge 1 ] && [ "$DECODER_ACTIVE" -gt 0 ] && [ -z "$CLAIMED" ]; then
        warn "an ADS-B decoder is running but its dongle couldn't be identified —"
        warn "confirm which serial is the RADIO dongle (do NOT pick the decoder's)"
    fi
    [ ${#CANDIDATES[@]} -eq 0 ] && warn "no free RTL-SDR dongle found — you can finish now and plug it in later"
    printf "Radio dongle serial [default 00000002]: " > "$TTY"
    read -r SERIAL < "$TTY" || true
    SERIAL=${SERIAL:-00000002}
    info "engine will wait for serial $SERIAL (exits+retries every 5 s until it appears)"
fi

# ── [7/8] per-node config ──────────────────────────────────
step "7/8 config"
CFG=/etc/pilnkradio/config.json
if [ -f "$CFG" ]; then
    ok "existing $CFG kept (delete it and re-run to regenerate)"
else
    FREQ=""
    while [ -z "$FREQ" ]; do
        printf "Local airband frequency to monitor, MHz [default 124.300]: " > "$TTY"
        read -r FREQ < "$TTY" || true
        FREQ=${FREQ:-124.300}
        case "$FREQ" in
            11[89].*|12[0-9].*|13[0-6].*|118|119|12[0-9]|13[0-6]) ;;
            *) warn "airband is 118–137 MHz — got '$FREQ'"; FREQ="" ;;
        esac
    done
    HZ=$(awk -v m="$FREQ" 'BEGIN{printf "%.1f", m*1e6}')
    # dashboard origins allowed to control the radio (audit M3 gate);
    # no-Origin (curl) and localhost are always allowed by the daemon itself
    ORIGINS="\"http://localhost:5000\""
    for ip in $(hostname -I 2>/dev/null); do
        case "$ip" in *:*) ;; *) ORIGINS="$ORIGINS, \"http://$ip:5000\"" ;; esac
    done
    HN=$(hostname 2>/dev/null || true)
    [ -n "$HN" ] && ORIGINS="$ORIGINS, \"http://$HN:5000\", \"http://$HN.local:5000\""
    sudo mkdir -p /etc/pilnkradio
    sudo tee "$CFG" >/dev/null <<EOF
{
    "serial": "$SERIAL",
    "ppm": 0,
    "vfoHz": $HZ,
    "mode": "AM",
    "bandwidthHz": 8000.0,
    "gainIndex": 21,
    "agc": false,
    "squelchEnabled": false,
    "squelchLevel": -50.0,
    "playing": false,
    "port": 5656,
    "allowedOrigins": [$ORIGINS],
    "token": ""
}
EOF
    ok "$CFG written ($FREQ MHz, serial $SERIAL, ppm 0 — calibrate later)"
fi

# ── [8/8] services + verify ────────────────────────────────
step "8/8 services + verify"
sudo cp "$SRC/pilnkradio/pilnkradio.service" /etc/systemd/system/
sudo cp "$SRC/pilnkradio/99-pilnk-v4.rules" /etc/udev/rules.d/
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now pilnkradio >/dev/null 2>&1
UP=""
for _ in 1 2 3 4 5 6 7 8; do
    sleep 1
    if curl -sf --max-time 2 http://localhost:5656/sdr/status >/dev/null; then UP=1; break; fi
done
if [ -n "$UP" ]; then
    ok "engine answering on :5656"
    DRV=$(sudo journalctl -u pilnkradio -n 50 --no-pager 2>/dev/null | grep -F 'driver: loaded' | tail -1 || true)
    if echo "$DRV" | grep -q '/usr/local/lib/librtlsdr'; then
        ok "driver check: fork confirmed (${DRV##*driver: })"
    else
        warn "could not confirm the driver journal line — check: journalctl -u pilnkradio | grep 'driver: loaded'"
        warn "it MUST show /usr/local/lib/librtlsdr… (a /lib/... path = deaf node)"
    fi
else
    if sudo journalctl -u pilnkradio -n 10 --no-pager 2>/dev/null | grep -qiE 'no device|not found|absent'; then
        warn "engine installed but waiting for dongle serial $SERIAL — it will start by itself when plugged in (udev rule active)"
    else
        err "engine not answering on :5656 — inspect: journalctl -u pilnkradio -n 30"
        exit 1
    fi
fi

printf "\n${BOLD}${GREEN}════════ RADIO INSTALL COMPLETE ════════${RESET}\n"
echo "Next:"
echo "  1. Antenna on the RADIO dongle (V4 = the SILVER one), not the ADS-B stick."
echo "  2. Hard-refresh the dashboard — the SDR Audio tab appears by itself"
echo "     (within 5 min; instantly on a page reload)."
echo "  3. Press LISTEN. Playback stays off until you do (operator consent)."
echo "  4. Tune/squelch from the tab. Later: measure the dongle's ppm and set"
echo "     it in $CFG (each crystal differs; 0 is fine to start)."
