#!/bin/bash
# Task 6: image/pilnk-image-build.sh. Runs from a copy of the repo placed at
# $HOME/pilnk inside a fake root; id, dpkg, sudo, growpart, rtl_test, lsusb and
# systemctl are PATH fakes, install.sh is a logging stub, files land under
# PILNK_ROOT. Nothing real is installed.
source "$(dirname "$0")/lib.sh"
mkroot

BUILD="$REPO/image/pilnk-image-build.sh"
ok "exists: image/pilnk-image-build.sh" "[ -f '$BUILD' ]"

export HOME="$ROOT/home/pilnk"
export PILNK_ROOT="$ROOT/fs"
export PILNK_OS_RELEASE="$ROOT/os-release"
CLONE="$HOME/pilnk"
mkdir -p "$CLONE" "$PILNK_ROOT"
cp -r "$REPO/image" "$CLONE/image"
cat > "$CLONE/install.sh" <<'EOS'
#!/bin/bash
echo "install.sh PILNK_IMAGE=${PILNK_IMAGE:-unset}" >> "$CALLS"
EOS

good_machine() {
  printf 'VERSION_CODENAME=trixie\nID=debian\n' > "$PILNK_OS_RELEASE"
  fakebin id 'case "$1" in -un) echo pilnk;; *) echo 1000;; esac'
  fakebin dpkg 'echo arm64'
  fakebin growpart 'true'
  fakebin dpkg-query 'case "$*" in *dnsmasq-base*) printf "install ok installed";; *) exit 1;; esac'
  fakebin sudo 'exec "$@"'
  : > "$ROOT/.rtl_out"; : > "$ROOT/.lsusb"
}

# shellcheck disable=SC2034  # OUT is read by eval in ok()
run() { # run [script path]; sets OUT, RC
  : > "$CALLS"
  OUT=$(bash "${1:-$CLONE/image/pilnk-image-build.sh}" 2>&1); RC=$?
}
refused() { # name message-regex
  ok "$1: exit 1" "[ '$RC' = 1 ]"
  ok "$1: message" "grep -qiE '$2' <<<\"\$OUT\""
  ok "$1: install.sh not called" "! grep -q '^install.sh' '$CALLS'"
  ok "$1: nothing installed" "[ -z \"\$(find '$PILNK_ROOT' -type f)\" ]"
}

good_machine; fakebin id 'case "$1" in -un) echo aj;; *) echo 1001;; esac'
run; refused "other user" "pilnk"
good_machine; printf 'VERSION_CODENAME=bookworm\n' > "$PILNK_OS_RELEASE"
run; refused "bookworm" "trixie"
good_machine; fakebin dpkg 'echo armhf'
run; refused "armhf" "arm64"
good_machine; rtl 00000001
run; refused "rtl dongle present" "Unplug the Pi4.s dongles first"
good_machine; airspy
run; refused "airspy present" "Unplug the Pi4.s dongles first"
good_machine; HOME="$ROOT/home/other" run "$REPO/image/pilnk-image-build.sh"; refused "not run from \$HOME/pilnk" "pilnk"
# growpart missing: a PATH holding only the tools the script needs (no growpart)
good_machine; rm -f "$BIN/growpart"
TOOLS="$ROOT/.tools"; mkdir -p "$TOOLS"
for t in bash dirname grep timeout cat env find; do ln -sf "$(PATH="$ORIG_PATH" command -v $t)" "$TOOLS/$t"; done
SAVED_PATH="$PATH"; PATH="$BIN:$TOOLS"
run; refused "growpart missing" "growpart is missing — run: sudo apt install cloud-guest-utils"
PATH="$SAVED_PATH"
# M11: NetworkManager's shared mode (the hotspot) needs dnsmasq-base
good_machine; fakebin dpkg-query 'exit 1'
run; refused "dnsmasq-base missing" "dnsmasq-base is missing — run: sudo apt install dnsmasq-base"
good_machine; fakebin dpkg-query 'printf "deinstall ok config-files"'
run; refused "dnsmasq-base removed (config files only)" "dnsmasq-base is missing"

# ---- happy path ------------------------------------------------------------
good_machine
run
ok "happy: exit 0" "[ '$RC' = 0 ]"
ok "happy: install.sh called once with PILNK_IMAGE=1" "[ \"\$(grep -c '^install.sh' '$CALLS')\" = 1 ] && grep -qx 'install.sh PILNK_IMAGE=1' '$CALLS'"
F="$PILNK_ROOT/usr/local/sbin/pilnk-firstboot"
ok "firstboot script installed" "cmp -s '$F' '$REPO/image/firstboot/pilnk-firstboot.sh'"
ok "firstboot script mode 755" "[ \"\$(stat -c %a '$F')\" = 755 ]"
L="$PILNK_ROOT/usr/local/lib/pilnk"
ok "wifi setup py installed" "cmp -s '$L/pilnk_wifi_setup.py' '$REPO/image/wifi-setup/pilnk_wifi_setup.py'"
ok "dnsmasq drop-in installed" "cmp -s '$L/dnsmasq-captive.conf' '$REPO/image/wifi-setup/dnsmasq-captive.conf'"
U="$PILNK_ROOT/etc/systemd/system"
ok "firstboot unit installed" "cmp -s '$U/pilnk-firstboot.service' '$REPO/image/firstboot/pilnk-firstboot.service'"
ok "wifi-setup unit installed" "cmp -s '$U/pilnk-wifi-setup.service' '$REPO/image/wifi-setup/pilnk-wifi-setup.service'"
ok "daemon-reload called" "grep -q '^systemctl daemon-reload' '$CALLS'"
ok "systemctl enable never called" "! grep -q '^systemctl.* enable' '$CALLS'"
ok "no unit enable symlinks" "[ -z \"\$(find '$U' -type l)\" ]"
ok "pilnk never started/restarted" "! grep -E '^systemctl.* (start|restart|try-restart|reload-or-restart) ' '$CALLS' | grep -q pilnk"
ok "no systemctl start/restart at all" "! grep -qE '^systemctl.* (start|restart|--now)' '$CALLS'"
ok "next step printed" "grep -q 'sudo bash image/pilnk-sterilise.sh' <<<\"\$OUT\""

# idempotent re-run
run
ok "re-run exit 0" "[ '$RC' = 0 ]"

# install.sh failure stops the build before installing the units
rm -rf "$PILNK_ROOT"; mkdir -p "$PILNK_ROOT"
printf '#!/bin/bash\necho "install.sh PILNK_IMAGE=$PILNK_IMAGE" >> "$CALLS"\nexit 3\n' > "$CLONE/install.sh"
run
ok "install.sh failure: exit non-zero" "[ '$RC' != 0 ]"
ok "install.sh failure: units not installed" "[ -z \"\$(find '$PILNK_ROOT' -type f)\" ]"
ok "install.sh failure: no next step" "! grep -q 'pilnk-sterilise.sh' <<<\"\$OUT\""
