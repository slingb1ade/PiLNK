#!/bin/bash
# pilnk-image-build.sh -- run on the build Pi (as user pilnk, from ~/pilnk).
# Installs everything the sterile image needs WITHOUT running PiLNK:
#   1. PILNK_IMAGE=1 bash install.sh       (packages, engine, no start)
#   2. first-boot script, Wi-Fi setup script + dnsmasq drop-in, both units
#      (installed DISABLED; pilnk-sterilise.sh arms them)
# Then prints the next step. Refuses (exit 1, saying why) unless: user pilnk,
# Trixie, arm64, no RTL-SDR / Airspy plugged in, growpart present, and this
# script lives in $HOME/pilnk (the clone install.sh uses).
#
# Test hooks: PILNK_ROOT (prefix for installed files), PILNK_OS_RELEASE.
set -u

die() { echo "pilnk-image-build: $*" >&2; exit 1; }

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE="$(dirname "$HERE")"
R="${PILNK_ROOT:-}"; R="${R%/}"
OSREL="${PILNK_OS_RELEASE:-/etc/os-release}"

[ "$(id -un)" = pilnk ] || die "run this as user pilnk (you are $(id -un)). The image's user must be pilnk."

# shellcheck disable=SC1090  # path is the OS release file (or a test fixture)
codename="$(. "$OSREL" 2>/dev/null; echo "${VERSION_CODENAME:-}")"
[ "$codename" = trixie ] || die "this must be Raspberry Pi OS Trixie (found '${codename:-unknown}'). Re-flash the build card with Trixie."

arch="$(dpkg --print-architecture 2>/dev/null)"
[ "$arch" = arm64 ] || die "this must be 64-bit (arm64); found '${arch:-unknown}'. Re-flash with Raspberry Pi OS Lite 64-bit."

if command -v rtl_test >/dev/null 2>&1 && timeout 3 rtl_test 2>&1 | grep -qE 'SN: [0-9A-Za-z]+'; then
  die "an RTL-SDR dongle is plugged in. Unplug the Pi4's dongles first."
fi
if command -v lsusb >/dev/null 2>&1 && lsusb 2>/dev/null | grep -qi airspy; then
  die "an Airspy is plugged in. Unplug the Pi4's dongles first."
fi

command -v growpart >/dev/null 2>&1 || die "growpart is missing — run: sudo apt install cloud-guest-utils (first boot can't grow the card without it)"

want="$(cd -P "$HOME/pilnk" 2>/dev/null && pwd)" || want=""
[ -n "$want" ] && [ "$CLONE" = "$want" ] || die "run this from the clone at \$HOME/pilnk (this one is $CLONE): bash ~/pilnk/image/pilnk-image-build.sh"
[ -f "$CLONE/install.sh" ] || die "$CLONE/install.sh not found"

echo "== 1/2 install.sh (image mode; PiLNK is not started)"
PILNK_IMAGE=1 bash "$CLONE/install.sh" || die "install.sh failed (exit $?). Nothing else was installed."

echo "== 2/2 first-boot and Wi-Fi setup files (units stay disabled)"
sudo install -D -m 755 "$CLONE/image/firstboot/pilnk-firstboot.sh" "$R/usr/local/sbin/pilnk-firstboot" || die "install failed: pilnk-firstboot"
sudo install -D -m 644 "$CLONE/image/wifi-setup/pilnk_wifi_setup.py" "$R/usr/local/lib/pilnk/pilnk_wifi_setup.py" || die "install failed: pilnk_wifi_setup.py"
sudo install -D -m 644 "$CLONE/image/wifi-setup/dnsmasq-captive.conf" "$R/usr/local/lib/pilnk/dnsmasq-captive.conf" || die "install failed: dnsmasq-captive.conf"
for u in firstboot/pilnk-firstboot.service wifi-setup/pilnk-wifi-setup.service; do
  sudo install -D -m 644 "$CLONE/image/$u" "$R/etc/systemd/system/$(basename "$u")" || die "install failed: $u"
done
sudo systemctl daemon-reload || die "systemctl daemon-reload failed"

echo
echo "Build done. PiLNK was NOT started and the two image units are NOT enabled."
echo "Next step:  sudo bash image/pilnk-sterilise.sh"
