#!/bin/bash
# write-build-userdata.sh — put the build-card settings onto a freshly flashed
# Raspberry Pi OS Lite (Trixie) card, because Raspberry Pi Imager left the stock,
# all-commented cloud-init template (seen 6 Nov 2026 with Imager 1.9.6 snap).
#
#   hostname pilnk-build · user pilnk (password you type, hashed here) ·
#   passwordless sudo (as Pi OS gives its first user) · SSH on with password login
#
# Ethernet only: network-config is left exactly as flashed and must not mention Wi-Fi.
# Sterilise later deletes user-data / network-config / meta-data and /var/lib/cloud,
# so the password hash never reaches the image.
#
# Usage (laptop, card's bootfs mounted read-write):
#   bash image/write-build-userdata.sh /media/$USER/bootfs
set -euo pipefail

B=${1:-}
die() { echo "STOP: $*" >&2; exit 1; }
[ -n "$B" ] || die "give the card's bootfs mount point, e.g. /media/$USER/bootfs"
[ -f "$B/cmdline.txt" ] && [ -f "$B/config.txt" ] || die "$B does not look like a Pi boot partition"
[ -f "$B/user-data" ] && [ -f "$B/network-config" ] || die "no cloud-init files on $B: is this a Trixie image?"
[ -w "$B/user-data" ] || die "$B is mounted read-only; mount it read-write first"

# Only ever replace the untouched stock template: never someone's real settings.
# The stock file is comments only (every non-blank line starts with #).
if grep -qvE '^[[:space:]]*(#.*)?$' "$B/user-data"; then
  die "user-data already has settings in it; not overwriting"
fi

# Ethernet only. A live (uncommented) Wi-Fi section would let the build card join Wi-Fi.
if grep -vE '^[[:space:]]*#' "$B/network-config" | grep -qiE 'wifis|wlan|access-points|ssid'; then
  die "network-config has Wi-Fi settings in it; the build card must be Ethernet only"
fi

command -v openssl >/dev/null || die "openssl is missing (sudo apt install openssl)"
echo "Type a throwaway password for user 'pilnk' (asked twice). It is hashed here and not shown."
HASH=$(openssl passwd -6) || die "passwords did not match"
case "$HASH" in '$6$'*) ;; *) die "unexpected hash format" ;; esac

TMP="$B/.user-data.new"
cat > "$TMP" <<EOF
#cloud-config
hostname: pilnk-build
manage_etc_hosts: true
users:
  - name: pilnk
    groups: [adm, dialout, sudo, audio, video, plugdev, users]
    shell: /bin/bash
    lock_passwd: false
    passwd: $HASH
    sudo: ALL=(ALL) NOPASSWD:ALL
ssh_pwauth: true
runcmd:
  - [systemctl, enable, --now, ssh]
EOF
mv "$TMP" "$B/user-data"
sync

echo "--- written (hash hidden):"
grep -vE 'passwd:' "$B/user-data"
echo "--- passwd line present: $(grep -c '^    passwd: \$6\$' "$B/user-data")"
echo "OK: now unmount, put the card in the Pi4, plug in Ethernet, power on."
