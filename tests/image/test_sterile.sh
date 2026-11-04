#!/bin/bash
# Task 5: sterile_check (image/sterile-checks.sh), pilnk-sterilise.sh and
# pilnk-image-verify.sh. Everything runs against a fake root under $ROOT; no
# real poweroff / systemctl / mount / losetup is ever reached (PATH fakes log to
# $CALLS, and the scripts only touch the real system when PILNK_ROOT is empty).
source "$(dirname "$0")/lib.sh"
mkroot
unset PILNK_DIR PILNK_ROOT PILNK_BOOT PILNK_DENYLIST

STER="$REPO/image/pilnk-sterilise.sh"
VER="$REPO/image/pilnk-image-verify.sh"
CHK="$REPO/image/sterile-checks.sh"
TEMPLATE="$REPO/image/config.template.json"

for f in "$STER" "$VER" "$CHK"; do
  ok "exists: ${f#"$REPO"/}" "[ -f '$f' ]"
done
[ -f "$CHK" ] && source "$CHK"
type sterile_check >/dev/null 2>&1 || sterile_check() { echo "FAIL missing: sterile-checks.sh not loaded"; return 1; }

# fakes for every command the scripts may only use on a real root
for c in passwd journalctl cloud-init apt-get hostnamectl mount umount losetup udevadm nmcli poweroff; do
  fakebin "$c" 'true'
done

# ---- fixtures --------------------------------------------------------------

# A hand-built, already-sterile fake root (R) + boot partition (B).
make_clean() {
  local R="$1" B="$1/boot/firmware" D="$1/home/pilnk/pilnk" W="$1/etc/systemd/system/multi-user.target.wants"
  mkdir -p "$B" "$D" "$W" "$R/etc/ssh" "$R/etc/cloud" "$R/etc/default" "$R/var/lib/dbus" "$R/var/log" \
           "$R/etc/NetworkManager/system-connections" "$R/etc/netplan" "$R/etc/udev/rules.d" "$R/tmp"
  printf 'root:*:19000:0:99999:7:::\npilnk:!$6$x$y:19000:0:99999:7:::\n' > "$R/etc/shadow"
  : > "$R/etc/machine-id"
  : > "$R/etc/cloud/cloud-init.disabled"
  printf 'RECEIVER_OPTIONS="--gain auto"\n' > "$R/etc/default/readsb"
  cp "$TEMPLATE" "$D/config.json"
  ln -s /etc/systemd/system/pilnk-firstboot.service "$W/pilnk-firstboot.service"
  ln -s /etc/systemd/system/pilnk-wifi-setup.service "$W/pilnk-wifi-setup.service"
}

# One line per trace: "check-id|shell snippet" (R, B, D, W are set when it runs).
# Several lines may share an id; the loop below runs each one alone.
TRACES=$(cat <<'EOF'
ssh-hostkeys|mkdir -p "$R/etc/ssh"; : > "$R/etc/ssh/ssh_host_ed25519_key"
ssh-hostkeys|mkdir -p "$R/etc/ssh"; : > "$R/etc/ssh/ssh_host_rsa_key.pub"
authorized-keys|mkdir -p "$R/home/pilnk/.ssh"; echo k > "$R/home/pilnk/.ssh/authorized_keys"
authorized-keys|mkdir -p "$R/root/.ssh"; echo k > "$R/root/.ssh/authorized_keys"
ssh-enabled|ln -sfn /lib/systemd/system/ssh.service "$W/ssh.service"
ssh-enabled|mkdir -p "$R/etc/systemd/system/sockets.target.wants"; ln -sfn /lib/systemd/system/ssh.socket "$R/etc/systemd/system/sockets.target.wants/ssh.socket"
ssh-enabled|: > "$B/ssh"
shadow-locked|printf 'root:*:19000:0:99999:7:::\npilnk:$6$x$y:19000:0:99999:7:::\n' > "$R/etc/shadow"
cloud-init-seed|: > "$B/user-data"
cloud-init-seed|: > "$B/network-config"
cloud-init-seed|: > "$B/meta-data"
cloud-init-enabled|rm -f "$R/etc/cloud/cloud-init.disabled"
nm-connections|printf 'psk=hunter2hunter2\n' > "$R/etc/NetworkManager/system-connections/PiLNK-Hotspot.nmconnection"
nm-connections|mkdir -p "$R/run/NetworkManager/system-connections"; printf 'psk=hunter2hunter2\n' > "$R/run/NetworkManager/system-connections/home.nmconnection"
netplan-nm|: > "$R/etc/netplan/90-NM-abc.yaml"
wpa-supplicant|mkdir -p "$R/etc/wpa_supplicant"; : > "$R/etc/wpa_supplicant/wpa_supplicant.conf"
dhcp-lease|mkdir -p "$R/var/lib/NetworkManager"; : > "$R/var/lib/NetworkManager/internal-x-wlan0.lease"
dhcp-lease|mkdir -p "$R/var/lib/dhcp"; : > "$R/var/lib/dhcp/dhclient.eth0.leases"
machine-id|echo 0123456789abcdef0123456789abcdef > "$R/etc/machine-id"
machine-id|mkdir -p "$R/var/lib/dbus"; : > "$R/var/lib/dbus/machine-id"
journal|mkdir -p "$R/var/log/journal/abc"; echo x > "$R/var/log/journal/abc/system.journal"
logs|echo x > "$R/var/log/wtmp"
logs|echo x > "$R/var/log/btmp"
history|echo x > "$R/home/pilnk/.bash_history"
history|echo x > "$R/home/pilnk/.python_history"
history|echo x > "$R/root/.lesshst" 2>/dev/null || { mkdir -p "$R/root"; echo x > "$R/root/.lesshst"; }
home-traces|mkdir -p "$R/home/pilnk/.cache"; echo x > "$R/home/pilnk/.cache/f"
home-traces|echo x > "$R/home/pilnk/.gitconfig"
home-traces|echo x > "$R/home/pilnk/.wget-hsts"
secret-key|echo k > "$D/.secret_key"
update-log|echo x > "$D/update.log"
ota-state|echo '{}' > "$D/ota_state.json"
capsules|mkdir -p "$D/capsules"; echo x > "$D/capsules/x"
history-data|echo '{}' > "$D/history_day.json"
transcripts|mkdir -p "$R/home/pilnk/atc-stt"; echo x > "$R/home/pilnk/atc-stt/atc_transcript.json"
no-receiver|: > "$D/.no_receiver"
config-json|printf '{"auto_update": true, "pending": {"x": 1}, "pilnk_code": "ABC"}\n' > "$D/config.json"
config-json|rm -f "$D/config.json"
readsb-device|printf 'RECEIVER_OPTIONS="--device 0001 --gain auto"\n' > "$R/etc/default/readsb"
pilnkradio-config|mkdir -p "$R/etc/pilnkradio"; echo '{}' > "$R/etc/pilnkradio/config.json"
udev-rule|: > "$R/etc/udev/rules.d/99-pilnk-v4.rules"
udev-rule|printf 'ATTRS{serial}=="0001", SYMLINK+="x"\n' > "$R/etc/udev/rules.d/60-x.rules"
firstboot-enabled|rm -f "$W/pilnk-firstboot.service"
wifi-setup-enabled|rm -f "$W/pilnk-wifi-setup.service"
pilnk-enabled|ln -sfn /etc/systemd/system/pilnk.service "$W/pilnk.service"
readsb-enabled|ln -sfn /lib/systemd/system/readsb.service "$W/readsb.service"
firstboot-markers|mkdir -p "$R/var/lib/pilnk-firstboot"; : > "$R/var/lib/pilnk-firstboot/identity"
firstboot-log|echo x > "$R/var/log/pilnk-firstboot.log"
EOF
)

apply_trace() { # ROOT snippet
  local R="$1" snippet="$2" B D W
  B="$R/boot/firmware"; D="$R/home/pilnk/pilnk"; W="$R/etc/systemd/system/multi-user.target.wants"
  mkdir -p "$B" "$D" "$W"
  eval "$snippet"
}

run_check() { # ROOT -> sets OUT, N
  OUT=$(sterile_check "$1" "$1/boot/firmware" 2>&1); N=$?
}

# ---- 1. the check list on its own -----------------------------------------

CLEAN0="$ROOT/clean0"; make_clean "$CLEAN0"
run_check "$CLEAN0"
ok "hand-built clean root: sterile_check returns 0" "[ '$N' = 0 ] && [ -z '$OUT' ]"

DIRTY="$ROOT/dirty"; make_clean "$DIRTY"
while IFS='|' read -r id snippet; do apply_trace "$DIRTY" "$snippet"; done <<< "$TRACES"
# the hostile bits a real card has: a hotspot profile holding a Wi-Fi password
run_check "$DIRTY"
ok "fully dirty root: sterile_check returns >= 1" "[ '$N' -ge 1 ]"
for id in $(cut -d'|' -f1 <<< "$TRACES" | sort -u); do
  ok "dirty root reports check $id" "grep -q '^FAIL $id: ' <<< \"\$OUT\""
done
ok "check output never prints file contents" "! grep -q hunter2 <<< \"\$OUT\""
ok "every check line has the FAIL <check>: <path> shape" \
   "! grep -qvE '^FAIL [a-z0-9-]+: .+' <<< \"\$OUT\""

# ---- 2. sterilise ----------------------------------------------------------

: > "$CALLS"
OUTS=$(PILNK_ROOT="$DIRTY" PILNK_BOOT="$DIRTY/boot/firmware" bash "$STER" --no-poweroff --no-zero 2>&1); RCS=$?
ok "sterilise --no-poweroff --no-zero exits 0 on a dirty root" "[ '$RCS' = 0 ]"
run_check "$DIRTY"
ok "after sterilise: sterile_check returns 0 (${OUT:+$OUT})" "[ '$N' = 0 ]"
ok "after sterilise: config.json byte-equal to the template" "cmp -s '$DIRTY/home/pilnk/pilnk/config.json' '$TEMPLATE'"
ok "sterilise never prints a saved Wi-Fi password" "! grep -q hunter2 <<< \"\$OUTS\""
ok "sterilise says what it removed" "grep -qi 'removed' <<< \"\$OUTS\""
ok "sterilise with --no-poweroff does not power off" "! grep -q '^poweroff' '$CALLS'"
ok "sterilise on a fake root touches no host command" \
   "! grep -qE '^(systemctl|passwd|journalctl|cloud-init|apt-get|hostnamectl|mount|losetup|udevadm|nmcli) ' '$CALLS'"
W="$DIRTY/etc/systemd/system/multi-user.target.wants"
ok "arm: firstboot + wifi-setup enabled, pilnk + readsb not" \
   "[ -L '$W/pilnk-firstboot.service' ] && [ -L '$W/pilnk-wifi-setup.service' ] && [ ! -e '$W/pilnk.service' ] && [ ! -L '$W/pilnk.service' ] && [ ! -L '$W/readsb.service' ]"
ok "machine-id is empty but present" "[ -f '$DIRTY/etc/machine-id' ] && [ ! -s '$DIRTY/etc/machine-id' ]"
ok "hostname set to pilnk" "[ \"\$(cat '$DIRTY/etc/hostname')\" = pilnk ]"
ok "cloud-init disabled marker written" "[ -e '$DIRTY/etc/cloud/cloud-init.disabled' ]"
ok "readsb default file kept, --device gone, other options kept" \
   "grep -q -- '--gain auto' '$DIRTY/etc/default/readsb' && ! grep -q -- '--device' '$DIRTY/etc/default/readsb'"
ok "pilnk shadow entry is locked" "grep -q '^pilnk:!' '$DIRTY/etc/shadow' && grep -q '^root:' '$DIRTY/etc/shadow'"

# sterilising an already-sterile root is a no-op that still passes
OUTS2=$(PILNK_ROOT="$DIRTY" PILNK_BOOT="$DIRTY/boot/firmware" bash "$STER" --no-poweroff --no-zero 2>&1); RCS2=$?
ok "sterilise is idempotent" "[ '$RCS2' = 0 ]"

# git-ignored runtime files go via git clean -ffdX; tracked files stay
GR="$ROOT/gitroot"; make_clean "$GR"; GD="$GR/home/pilnk/pilnk"
( cd "$GD" && git init -q . && git config user.email t@t && git config user.name t \
  && printf 'junk.cache\nconfig.json\n__ignored_dir/\n' > .gitignore && echo code > app.py \
  && git add .gitignore app.py && git commit -qm init \
  && echo x > junk.cache && mkdir -p __ignored_dir && echo x > __ignored_dir/f ) >/dev/null 2>&1
PILNK_ROOT="$GR" PILNK_BOOT="$GR/boot/firmware" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1
ok "git clean removes ignored runtime files, keeps tracked ones" \
   "[ ! -e '$GD/junk.cache' ] && [ ! -e '$GD/__ignored_dir' ] && [ -f '$GD/app.py' ] && cmp -s '$GD/config.json' '$TEMPLATE'"

# poweroff only on a clean self-check
P1="$ROOT/p1"; make_clean "$P1"; apply_trace "$P1" 'echo x > "$R/home/pilnk/.bash_history"'
: > "$CALLS"
PILNK_ROOT="$P1" PILNK_BOOT="$P1/boot/firmware" bash "$STER" --no-zero >/dev/null 2>&1; RCP=$?
ok "clean self-check -> exit 0 and exactly one poweroff" "[ '$RCP' = 0 ] && [ \"\$(grep -c '^poweroff' '$CALLS')\" = 1 ]"

if [ "$(id -u)" != 0 ]; then
  P2="$ROOT/p2"; make_clean "$P2"
  mkdir -p "$P2/etc/NetworkManager/system-connections"; echo x > "$P2/etc/NetworkManager/system-connections/stuck"
  chmod 555 "$P2/etc/NetworkManager/system-connections"
  : > "$CALLS"
  OUTP=$(PILNK_ROOT="$P2" PILNK_BOOT="$P2/boot/firmware" bash "$STER" --no-zero 2>&1); RCP2=$?
  chmod 755 "$P2/etc/NetworkManager/system-connections"
  ok "failed self-check -> exit 1, no poweroff, says what it found" \
     "[ '$RCP2' = 1 ] && ! grep -q '^poweroff' '$CALLS' && grep -q '^FAIL nm-connections' <<< \"\$OUTP\""
fi

# zero-fill: runs, reports, and removes its fill file (capped so the test is cheap)
Z="$ROOT/z"; make_clean "$Z"
OUTZ=$(PILNK_ZERO_LIMIT_MB=2 PILNK_ROOT="$Z" PILNK_BOOT="$Z/boot/firmware" bash "$STER" --no-poweroff 2>&1); RCZ=$?
ok "zero-fill: exits 0, reports it, leaves no fill file" \
   "[ '$RCZ' = 0 ] && grep -qi 'zero' <<< \"\$OUTZ\" && [ -z \"\$(find '$Z' -name '.pilnk-zero*')\" ]"

# ---- 3. re-add each trace alone -------------------------------------------

CLEANS="$DIRTY"      # the sterilised root from section 2
i=0
while IFS='|' read -r id snippet; do
  i=$((i+1))
  C="$ROOT/case$i"; cp -a "$CLEANS" "$C"
  apply_trace "$C" "$snippet"
  run_check "$C"
  ok "re-add trace #$i fails check $id" "[ '$N' -ge 1 ] && grep -q '^FAIL $id: ' <<< \"\$OUT\""
  rm -rf "$C"
done <<< "$TRACES"

# ---- 4. verifier: --root/--boot/--raw -------------------------------------

V="$ROOT/vroot"; cp -a "$CLEANS" "$V"
WORD=wombatsentinelx9
DENY="$ROOT/denylist"; printf '%s\n' "$WORD" "second-name" > "$DENY"; chmod 600 "$DENY"
EMPTYRAW="$ROOT/empty.raw"; : > "$EMPTYRAW"
OUTV=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$EMPTYRAW" --denylist "$DENY" 2>&1); RCV=$?
ok "verifier: clean root + empty raw -> exit 0" "[ '$RCV' = 0 ]"

OUTV=$(bash "$VER" --root "$DIRTY" --boot "$DIRTY/boot/firmware" --raw "$EMPTYRAW" 2>&1); RCV=$?
ok "verifier: sterilised root -> exit 0" "[ '$RCV' = 0 ]"

D2="$ROOT/vdirty"; make_clean "$D2"; apply_trace "$D2" "$(grep '^nm-connections' <<< "$TRACES" | head -1 | cut -d'|' -f2-)"
OUTV=$(bash "$VER" --root "$D2" --boot "$D2/boot/firmware" --raw "$EMPTYRAW" 2>&1); RCV=$?
ok "verifier: dirty root -> exit 1 and names the check" "[ '$RCV' = 1 ] && grep -q '^FAIL nm-connections' <<< \"\$OUTV\""
ok "verifier: dirty root output never shows the Wi-Fi password" "! grep -q hunter2 <<< \"\$OUTV\""

ok "verifier: no arguments -> usage error (exit 2)" "bash '$VER' >/dev/null 2>&1; [ \$? = 2 ]"
ok "verifier: unreadable denylist -> exit 2, not a pass" \
   "bash '$VER' --root '$V' --boot '$V/boot/firmware' --raw '$EMPTYRAW' --denylist '$ROOT/nope' >/dev/null 2>&1; [ \$? = 2 ]"

# ---- 5. raw search on a real ext4 image -----------------------------------

if command -v mkfs.ext4 >/dev/null && command -v debugfs >/dev/null; then
  IMG="$ROOT/test.img"
  truncate -s 64M "$IMG"
  mkfs.ext4 -q -F "$IMG" >/dev/null 2>&1
  printf 'owner %s\nnetwork={\npsk=hunter2hunter2\n}\n' "$WORD" > "$ROOT/leak.txt"
  debugfs -w -R "write $ROOT/leak.txt leak.txt" "$IMG" >/dev/null 2>&1
  debugfs -w -R "rm leak.txt" "$IMG" >/dev/null 2>&1
  ok "fixture: deleted file is gone from the ext4 tree" "! debugfs -R 'ls -l /' '$IMG' 2>/dev/null | grep -q leak.txt"

  OUTR=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$IMG" --denylist "$DENY" 2>&1); RCR=$?
  ok "raw: leftover bytes in free space -> exit 1" "[ '$RCR' = 1 ]"
  ok "raw: reports HIT deny#1 with a byte offset" "grep -qE '^HIT deny#1 at [0-9]+\$' <<< \"\$OUTR\""
  ok "raw: reports HIT psk with a byte offset" "grep -qE '^HIT psk at [0-9]+\$' <<< \"\$OUTR\""
  ok "raw: output never contains the password" "! grep -qi hunter2 <<< \"\$OUTR\""
  ok "raw: output never contains the denylist word" "! grep -qi '$WORD' <<< \"\$OUTR\""
  ok "raw: output never contains the second denylist word either" "! grep -qi second-name <<< \"\$OUTR\""
  ok "raw: no temp pattern files are left behind" "[ -z \"\$(ls -d /tmp/pilnk-verify.* 2>/dev/null)\" ]"

  # simulate the zero-fill: blank every 4K block that holds a hit (what filling
  # free space with zeros does to deleted data), then the image must be clean.
  for off in $(grep -E '^HIT ' <<< "$OUTR" | grep -oE '[0-9]+$' | sort -un); do
    dd if=/dev/zero of="$IMG" bs=4096 seek=$((off / 4096)) count=1 conv=notrunc status=none
  done
  OUTR2=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$IMG" --denylist "$DENY" 2>&1); RCR2=$?
  ok "raw: after the free space is zeroed the verifier is clean" "[ '$RCR2' = 0 ] && ! grep -q '^HIT' <<< \"\$OUTR2\""
else
  echo "FAIL: ext4 tools (mkfs.ext4, debugfs) missing; raw-image test cannot run"
fi

# private key header + tailscale + wpa-psk ids (built at run time so this file
# does not itself contain the strings the verifier hunts for)
RAWF="$ROOT/ids.raw"
{ head -c 100 /dev/zero; printf -- '-----BEGIN %s %s-----' OPENSSH 'PRIVATE KEY'; head -c 50 /dev/zero
  printf 'Tail%s' 'scale'; head -c 20 /dev/zero; printf 'wpa%s' '-psk'; } > "$RAWF"
OUTI=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$RAWF" 2>&1); RCI=$?
ok "raw ids: privkey / tailscale / wpa-psk found with offsets, exit 1" \
   "[ '$RCI' = 1 ] && grep -qE '^HIT privkey at [0-9]+\$' <<< \"\$OUTI\" && grep -qE '^HIT tailscale at [0-9]+\$' <<< \"\$OUTI\" && grep -qE '^HIT wpa-psk at [0-9]+\$' <<< \"\$OUTI\""
ok "raw ids: output has no key header text" "! grep -qi 'BEGIN' <<< \"\$OUTI\""
