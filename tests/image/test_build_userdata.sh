#!/bin/bash
# image/write-build-userdata.sh: writes the build card's cloud-init user-data
# when Raspberry Pi Imager left the stock, all-commented template. Fake bootfs,
# fake openssl; never prints the hash.
. "$(dirname "$0")/lib.sh"
mkroot
W="$REPO/image/write-build-userdata.sh"
HASH='$6$saltsalt$fakehashfakehashfakehash'
fakebin openssl "printf '%s\n' '$HASH'"
boot() { # dir -> a stock Trixie bootfs
  mkdir -p "$1"; : > "$1/cmdline.txt"; : > "$1/config.txt"
  printf '#cloud-config\n# hostname: raspberrypi\n\n#users:\n#  - name: pi\n' > "$1/user-data"
  printf '# network:\n#   version: 2\n#   wifis:\n#     wlan0: {}\n' > "$1/network-config"
}
B1="$ROOT/b1"; boot "$B1"
OUT=$(bash "$W" "$B1" < /dev/null 2>&1); RC=$?
ok "stock template: exit 0" "[ '$RC' = 0 ]"
ok "user-data: hostname pilnk-build, user pilnk, ssh on, password login" \
   "grep -qx 'hostname: pilnk-build' '$B1/user-data' && grep -qx '  - name: pilnk' '$B1/user-data' && grep -qx 'ssh_pwauth: true' '$B1/user-data' && grep -q 'enable, --now, ssh' '$B1/user-data'"
ok "user-data holds the sha-512 hash from openssl" "grep -qxF '    passwd: $HASH' '$B1/user-data'"
ok "the hash is never printed" "! grep -qF 'fakehash' <<< \"\$OUT\""
ok "network-config untouched (commented Wi-Fi stays commented)" "grep -q '^#   wifis:' '$B1/network-config' && ! grep -qv '^#' '$B1/network-config'"
ok "no temp file left on the card" "[ -z \"\$(ls -A '$B1' | grep -v -e cmdline.txt -e config.txt -e user-data -e network-config)\" ]"

B2="$ROOT/b2"; boot "$B2"; printf 'hostname: mine\n' >> "$B2/user-data"; cp "$B2/user-data" "$ROOT/b2.orig"
OUT=$(bash "$W" "$B2" < /dev/null 2>&1); RC=$?
ok "user-data with real settings: refuses, file unchanged" "[ '$RC' != 0 ] && cmp -s '$B2/user-data' '$ROOT/b2.orig' && grep -q STOP <<< \"\$OUT\""

B3="$ROOT/b3"; boot "$B3"; printf 'network:\n  wifis:\n    wlan0: {}\n' > "$B3/network-config"; cp "$B3/user-data" "$ROOT/b3.orig"
OUT=$(bash "$W" "$B3" < /dev/null 2>&1); RC=$?
ok "live Wi-Fi in network-config: refuses, user-data unchanged" "[ '$RC' != 0 ] && cmp -s '$B3/user-data' '$ROOT/b3.orig'"

B4="$ROOT/b4"; mkdir -p "$B4"; : > "$B4/user-data"; : > "$B4/network-config"
OUT=$(bash "$W" "$B4" < /dev/null 2>&1); RC=$?
ok "not a boot partition (no cmdline.txt/config.txt): refuses" "[ '$RC' != 0 ] && [ ! -s '$B4/user-data' ]"

OUT=$(bash "$W" < /dev/null 2>&1); RC=$?
ok "no argument: refuses with a usage hint" "[ '$RC' != 0 ] && grep -q bootfs <<< \"\$OUT\""

B5="$ROOT/b5"; boot "$B5"; cp "$B5/user-data" "$ROOT/b5.orig"
fakebin openssl "printf 'garbage\n'"
OUT=$(bash "$W" "$B5" < /dev/null 2>&1); RC=$?
ok "openssl output not a \$6\$ hash: refuses, user-data unchanged" "[ '$RC' != 0 ] && cmp -s '$B5/user-data' '$ROOT/b5.orig'"
fakebin openssl "exit 1"
OUT=$(bash "$W" "$B5" < /dev/null 2>&1); RC=$?
ok "openssl fails (passwords differ): refuses, user-data unchanged" "[ '$RC' != 0 ] && cmp -s '$B5/user-data' '$ROOT/b5.orig'"
