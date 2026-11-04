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
for c in passwd journalctl cloud-init apt-get hostnamectl mount umount losetup udevadm nmcli poweroff \
         systemd-run loginctl; do
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
  # the unit files the enable links point at (pilnk-image-build.sh installs them)
  printf '[Unit]\n' > "$R/etc/systemd/system/pilnk-firstboot.service"
  printf '[Unit]\n' > "$R/etc/systemd/system/pilnk-wifi-setup.service"
}

# Fixtures never spell a pattern the raw search hunts for, so the repo's own
# files stay clean when the verifier scans an image that contains them.
PK=ps; PK+=k
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
nm-connections|printf '%s=hunter2hunter2\n' "$PK" > "$R/etc/NetworkManager/system-connections/PiLNK-Hotspot.nmconnection"
nm-connections|mkdir -p "$R/run/NetworkManager/system-connections"; printf '%s=hunter2hunter2\n' "$PK" > "$R/run/NetworkManager/system-connections/home.nmconnection"
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
logs|mkdir -p "$R/var/lib/wtmpdb"; echo x > "$R/var/lib/wtmpdb/wtmp.db"
zero-file|: > "$R/.pilnk-zero"
zero-file|: > "$B/.pilnk-zero"
tailscale-state|mkdir -p "$R/var/lib/tailscale"; echo x > "$R/var/lib/tailscale/tailscaled.state"
cloud-init-state|mkdir -p "$R/var/lib/cloud/instance"; echo x > "$R/var/lib/cloud/instance/user-data.txt"
netplan-cloud-init|mkdir -p "$R/etc/netplan"; : > "$R/etc/netplan/50-cloud-init.yaml"
readsb-enabled|mkdir -p "$R/etc/systemd/system/default.target.wants"; ln -sfn /lib/systemd/system/readsb.service "$R/etc/systemd/system/default.target.wants/readsb.service"
pilnk-enabled|mkdir -p "$R/etc/systemd/system/graphical.target.wants"; ln -sfn /etc/systemd/system/pilnk.service "$R/etc/systemd/system/graphical.target.wants/pilnk.service"
ssh-enabled|mkdir -p "$R/etc/systemd/system/default.target.wants"; ln -sfn /lib/systemd/system/ssh.service "$R/etc/systemd/system/default.target.wants/ssh.service"
sterilise-failed|echo 'FAIL x: /y' > "$B/pilnk-sterilise-failed.txt"
git-reflog|mkdir -p "$D/.git/logs"; echo x > "$D/.git/logs/HEAD"
git-identity|mkdir -p "$D/.git"; printf '[core]\n\tbare = false\n[user]\n\tname = Someone\n\temail = someone@example.invalid\n' > "$D/.git/config"
wpa-examples|mkdir -p "$R/usr/share/doc/wpasupplicant/examples"; printf 'network={\n\t%s="examplepassphrase"\n}\n' "$PK" > "$R/usr/share/doc/wpasupplicant/examples/wpa_supplicant.conf"
nm-state|mkdir -p "$R/var/lib/NetworkManager"; echo x > "$R/var/lib/NetworkManager/seen-bssids"
nm-state|mkdir -p "$R/var/lib/NetworkManager"; echo x > "$R/var/lib/NetworkManager/timestamps"
nm-state|mkdir -p "$R/var/lib/NetworkManager"; echo x > "$R/var/lib/NetworkManager/NetworkManager.state"
nm-state|mkdir -p "$R/var/lib/NetworkManager"; echo x > "$R/var/lib/NetworkManager/secret_key"
history|mkdir -p "$R/home/pilnk/.local/state"; echo x > "$R/home/pilnk/.local/state/lesshst"
history|mkdir -p "$R/root/.local/state"; echo x > "$R/root/.local/state/lesshst"
history|mkdir -p "$R/home/pilnk/.local/share/nano"; echo x > "$R/home/pilnk/.local/share/nano/search_history"
history|mkdir -p "$R/root/.local/share/nano"; echo x > "$R/root/.local/share/nano/filepos_history"
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
   "! grep -qE '^(systemctl|passwd|journalctl|cloud-init|apt-get|hostnamectl|mount|losetup|udevadm|nmcli|systemd-run|loginctl) ' '$CALLS'"
W="$DIRTY/etc/systemd/system/multi-user.target.wants"
ok "arm: firstboot + wifi-setup enabled, pilnk + readsb not" \
   "[ -L '$W/pilnk-firstboot.service' ] && [ -L '$W/pilnk-wifi-setup.service' ] && [ ! -e '$W/pilnk.service' ] && [ ! -L '$W/pilnk.service' ] && [ ! -L '$W/readsb.service' ]"
ok "machine-id is empty but present" "[ -f '$DIRTY/etc/machine-id' ] && [ ! -s '$DIRTY/etc/machine-id' ]"
ok "hostname set to pilnk" "[ \"\$(cat '$DIRTY/etc/hostname')\" = pilnk ]"
ok "cloud-init disabled marker written" "[ -e '$DIRTY/etc/cloud/cloud-init.disabled' ]"
ok "readsb default file kept, --device gone, other options kept" \
   "grep -q -- '--gain auto' '$DIRTY/etc/default/readsb' && ! grep -q -- '--device' '$DIRTY/etc/default/readsb'"
ok "pilnk shadow entry is locked" "grep -q '^pilnk:!' '$DIRTY/etc/shadow' && grep -q '^root:' '$DIRTY/etc/shadow'"

# Debian's wpasupplicant example configs hold example passphrases: removed, and checked alone
WX="$ROOT/wpax"; make_clean "$WX"
apply_trace "$WX" "$(grep '^wpa-examples|' <<< "$TRACES" | cut -d'|' -f2-)"
ok "fixture: wpasupplicant examples dir seeded" "[ -f '$WX/usr/share/doc/wpasupplicant/examples/wpa_supplicant.conf' ]"
run_check "$WX"
ok "wpa-examples alone: sterile_check names wpa-examples" "[ '$N' = 1 ] && grep -q '^FAIL wpa-examples: ' <<< \"\$OUT\""
PILNK_ROOT="$WX" PILNK_BOOT="$WX/boot/firmware" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1; RCW=$?
ok "sterilise removes the wpasupplicant examples dir; exit 0" "[ '$RCW' = 0 ] && [ ! -e '$WX/usr/share/doc/wpasupplicant/examples' ]"
run_check "$WX"
ok "after sterilise the wpa-examples check passes" "[ '$N' = 0 ]"

# sterilising an already-sterile root is a no-op that still passes
OUTS2=$(PILNK_ROOT="$DIRTY" PILNK_BOOT="$DIRTY/boot/firmware" bash "$STER" --no-poweroff --no-zero 2>&1); RCS2=$?
ok "sterilise is idempotent" "[ '$RCS2' = 0 ]"

# git-ignored runtime files go via git clean -ffdX; tracked files stay
GR="$ROOT/gitroot"; make_clean "$GR"; GD="$GR/home/pilnk/pilnk"
( cd "$GD" && git init -q . && git config user.email t@t && git config user.name t \
  && printf 'junk.cache\nconfig.json\n__ignored_dir/\n' > .gitignore && echo code > app.py \
  && git add .gitignore app.py && git commit -qm init \
  && echo x > junk.cache && mkdir -p __ignored_dir && echo x > __ignored_dir/f ) >/dev/null 2>&1
ok "fixture: the clone has a reflog before sterilise" "[ -s '$GD/.git/logs/HEAD' ]"
PILNK_ROOT="$GR" PILNK_BOOT="$GR/boot/firmware" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1; RCG=$?
ok "git clean removes ignored runtime files, keeps tracked ones" \
   "[ ! -e '$GD/junk.cache' ] && [ ! -e '$GD/__ignored_dir' ] && [ -f '$GD/app.py' ] && cmp -s '$GD/config.json' '$TEMPLATE'"
ok "reflog: expired and .git/logs removed; the clone still works; check passes" \
   "[ '$RCG' = 0 ] && [ ! -e '$GD/.git/logs' ] && git -C '$GD' rev-parse -q --verify HEAD >/dev/null && [ -z \"\$(git -C '$GD' reflog 2>/dev/null)\" ]"

# git identity: the clone's .git/config must hold no [user] section
GI="$ROOT/gitid"; make_clean "$GI"; GID="$GI/home/pilnk/pilnk"
( cd "$GID" && git init -q . && git config user.name Someone && git config user.email someone@example.invalid ) >/dev/null 2>&1
ok "fixture: the clone has a [user] section" "grep -q '^\[user\]' '$GID/.git/config'"
run_check "$GI"
ok "git-identity alone: sterile_check names git-identity, never the email" \
   "[ '$N' = 1 ] && grep -q '^FAIL git-identity: /home/pilnk/pilnk/.git/config' <<< \"\$OUT\" && ! grep -q 'example.invalid' <<< \"\$OUT\""
OUTGI=$(PILNK_ROOT="$GI" PILNK_BOOT="$GI/boot/firmware" bash "$STER" --no-poweroff --no-zero 2>&1); RCGI=$?
ok "sterilise removes the [user] section (other config kept); exit 0; email never printed" \
   "[ '$RCGI' = 0 ] && ! grep -q '^\[user\]' '$GID/.git/config' && git -C '$GID' rev-parse --git-dir >/dev/null 2>&1 && grep -q '^\[core\]' '$GID/.git/config' && ! grep -q 'example.invalid' <<< \"\$OUTGI\""
run_check "$GI"
ok "after sterilise the git-identity check passes" "[ '$N' = 0 ]"
git -C "$GID" config user.name Again >/dev/null 2>&1
run_check "$GI"
ok "re-adding a user key alone -> git-identity fails again" "[ '$N' = 1 ] && grep -q '^FAIL git-identity: ' <<< \"\$OUT\""

# a fake root never powers off, even without --no-poweroff
P1="$ROOT/p1"; make_clean "$P1"; apply_trace "$P1" 'echo x > "$R/home/pilnk/.bash_history"'
: > "$CALLS"
OUTP1=$(PILNK_ROOT="$P1" PILNK_BOOT="$P1/boot/firmware" bash "$STER" --no-zero 2>&1); RCP=$?
ok "fake root: clean self-check -> exit 0, no poweroff without --no-poweroff, says why" \
   "[ '$RCP' = 0 ] && ! grep -q '^poweroff' '$CALLS' && grep -qi 'fake root' <<< \"\$OUTP1\""

# the real-root ending, run sourced against a fake tree: poweroff only on a clean check
: > "$CALLS"
( source "$STER"; R="$P1"; B="$P1/boot/firmware"; REAL=1; POWEROFF=1; STAGE2=1; ERRORS=0; final_check ) >/dev/null 2>&1; RCF=$?
ok "real root, clean final check -> exit 0 and exactly one poweroff" "[ '$RCF' = 0 ] && [ \"\$(grep -c '^poweroff' '$CALLS')\" = 1 ]"
P1F="$ROOT/p1f"; make_clean "$P1F"; : > "$P1F/etc/ssh/ssh_host_ed25519_key"
: > "$CALLS"
( source "$STER"; R="$P1F"; B="$P1F/boot/firmware"; REAL=1; POWEROFF=1; STAGE2=0; ERRORS=0; final_check ) >/dev/null 2>&1; RCF=$?
ok "real root, failed final check -> exit 1, no poweroff" "[ '$RCF' = 1 ] && ! grep -q '^poweroff' '$CALLS'"
( source "$STER"; R="$P1"; B="$P1/boot/firmware"; REAL=1; POWEROFF=1; STAGE2=0; ERRORS=1; final_check ) >/dev/null 2>&1; RCF=$?
ok "real root, clean check but a counted error -> exit 1, no poweroff" "[ '$RCF' = 1 ] && ! grep -q '^poweroff' '$CALLS'"

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
   "[ '$RCZ' = 0 ] && grep -qi 'zero-filled' <<< \"\$OUTZ\" && [ -z \"\$(find '$Z' -name '.pilnk-zero*')\" ]"
# on a fake root, zero-fill without PILNK_ZERO_LIMIT_MB would fill the host disk
ZN="$ROOT/zn"; make_clean "$ZN"; ZNB="$ROOT/znbin"; mkdir -p "$ZNB"
printf '#!/bin/bash\necho "dd $*" >> "$CALLS"\nexit 1\n' > "$ZNB/dd"; chmod +x "$ZNB/dd"
: > "$CALLS"
OUTZN=$(PATH="$ZNB:$PATH" PILNK_ROOT="$ZN" bash "$STER" --no-poweroff 2>&1); RCZN=$?
ok "fake root, no PILNK_ZERO_LIMIT_MB -> zero-fill skipped with a note, dd never runs" \
   "[ '$RCZN' = 0 ] && ! grep -q '^dd ' '$CALLS' && grep -q 'PILNK_ZERO_LIMIT_MB' <<< \"\$OUTZN\" && ! grep -q 'zero-filled' <<< \"\$OUTZN\""

# PILNK_ROOT spellings of "/" are the live system (unit test of the normaliser)
for r in / // /. //. /./ /tmp/.. /tmp/../.; do
  got=$( source "$STER"; norm_root "$r" 2>/dev/null ); rcn=$?
  ok "norm_root '$r' -> '' (the live system)" "[ '$rcn' = 0 ] && [ -z '$got' ]"
done
got=$( source "$STER"; norm_root "$ZN/./" 2>/dev/null )
ok "norm_root keeps a real fake root (trailing /. and / dropped)" "[ '$got' = '$ZN' ]"

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

# ---- 5. raw search: secret-shaped patterns only ---------------------------
# Every fixture is assembled at run time from pieces, so this file never holds
# a string the raw search would flag (the repo ships inside the image).

BEG="-----BEG"; BEG+="IN"; FIN="-----E"; FIN+="ND"; PKEY="PRIV"; PKEY+="ATE KEY-----"
TSK=ts; TSK+=key
ossh_gen() { # a real ed25519 key from ssh-keygen
  rm -f "$ROOT/k" "$ROOT/k.pub"
  ssh-keygen -q -t ed25519 -N '' -C t -f "$ROOT/k" >/dev/null 2>&1 && cat "$ROOT/k"
}
ossh_key() { # a real-shaped OpenSSH key built by hand
  printf '%s OPENSSH %s\n' "$BEG" "$PKEY"
  { printf 'openssh-key-v1\0\0\0\0\4none\0\0\0\4none\0\0\0\0\0\0\0\1'; head -c 300 /dev/urandom; } | base64 -w 70
  printf '%s OPENSSH %s\n' "$FIN" "$PKEY"
}
pem_rsa() {
  printf '%s RSA %s\n' "$BEG" "$PKEY"; head -c 600 /dev/urandom | base64 -w 64; printf '%s RSA %s\n' "$FIN" "$PKEY"
}
pem_json() { # a key inside a JSON string, newlines escaped
  printf '{"private_key": "%s %s\\n%s\\n%s %s\\n"}\n' "$BEG" "$PKEY" "$(head -c 90 /dev/urandom | base64 -w 0)" "$FIN" "$PKEY"
}
nm_keyfile() { printf '[wifi-security]\nkey-mgmt=wpa-psk\n%s=hunter2hunter2\n' "$PK"; }
wpa_block()  { printf 'network={\n\tssid="Home"\n\t%s="hunter2hunter2"\n\tkey_mgmt=WPA-PSK\n}\n' "$PK"; }
hex_psk()    { printf '%s=%s\n' "$PK" "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"; }
ts_key()     { printf 'TS_AUTHKEY=%s-auth-kQ7xYz3CNTRL-abcdefGHIJ1234\n' "$TSK"; }
# legitimate strings a real Pi OS image is full of
fmt_psk()    { printf '%s=%%s\0%s=%%s\n' "$PK" "$PK"; }
key_mgmt()   { printf 'key_mgmt=WPA-PSK\0wpa-%s\0WPA%s-PSK\0' 'psk' '2'; }
pem_header() { printf '%s OPENSSH %s\0%s RSA %s\0\0usage: %%s\0' "$BEG" "$PKEY" "$BEG" "$PKEY"; }
ts_word()    { printf 'Tail%s\0/var/lib/tail%s/tail%sd.state\0' scale scale scale; }
psk_var()    { printf '%s=$PSK\n%s=short\n  %s="${1}"\n' "$PK" "$PK" "$PK"; }
# N3: an encrypted traditional PEM key (two header lines and a blank line before the body)
pem_enc() {
  printf '%s RSA %s\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,%s\n\n' "$BEG" "$PKEY" \
    "$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | tr a-f A-F)"
  head -c 600 /dev/urandom | base64 -w 64; printf '%s RSA %s\n' "$FIN" "$PKEY"
}
pem_enc_gen() { # the same, made by openssl
  openssl genrsa -aes128 -traditional -passout pass:testpass 1024 2>/dev/null
}
# N4: NetworkManager keyfile values are written unquoted, any printable character
nm_space()   { printf '[wifi-security]\n%s=%s\n' "$PK" 'my home wifi 2024'; }
nm_dollar()  { printf '[wifi-security]\n%s=%s\n' "$PK" 'pa$$word1234'; }
nm_percent() { printf '[wifi-security]\n%s=%s\n' "$PK" '50%offwifi!'; }
nm_quote()   { printf '[wifi-security]\n%s=%s\n' "$PK" 'abc"defghij'; }
# ...but long format strings and shell variables are not passphrases
psk_fmt_long() { printf '%s=%s\0%s=%s\0%s=%s\n' "$PK" '%s key_mgmt=%s' "$PK" '%.*s (len %d)' "$PK" '"%s" proto=%s'; }
psk_var_long() { printf '%s=%s\n%s=%s\n%s=%s\n%s=%s\n' "$PK" '${WIFI_PASSWORD}' "$PK" '$WIFI_PASSWORD' \
                   "$PK" '"${WIFI_PASSWORD}"' "$PK" '"$WIFI_PASSWORD"'; }

SHAPES="privkey|ossh_key
privkey|pem_rsa
privkey|pem_json
privkey|pem_enc
psk|nm_keyfile
psk|wpa_block
psk|hex_psk
psk|nm_space
psk|nm_dollar
psk|nm_percent
psk|nm_quote
tailscale|ts_key"
command -v ssh-keygen >/dev/null 2>&1 && SHAPES="privkey|ossh_gen
$SHAPES"
pem_enc_gen | grep -q '^DEK-Info' && SHAPES="privkey|pem_enc_gen
$SHAPES"
LEGIT="fmt_psk key_mgmt pem_header ts_word psk_var psk_fmt_long psk_var_long"

# one fake bin dir per awk, so both mawk (Pi OS, Debian default) and gawk run
AWKS=""
for a in mawk gawk; do
  command -v "$a" >/dev/null 2>&1 || continue
  mkdir -p "$ROOT/awk-$a"; ln -sf "$(command -v "$a")" "$ROOT/awk-$a/awk"; AWKS="$AWKS $a"
done
[ -n "$AWKS" ] || AWKS=" default"

for A in $AWKS; do
  AP="$ROOT/awk-$A:$PATH"
  while IFS='|' read -r id gen; do
    f="$ROOT/shape.raw"; { head -c 100 /dev/zero; "$gen"; head -c 100 /dev/zero; } > "$f"
    out=$(PATH="$AP" bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$f" 2>&1); rc=$?
    ok "raw ($A): $gen is caught as $id with an offset, exit 1" "[ '$rc' = 1 ] && grep -qE '^HIT $id at offset [0-9]+\$' <<< \"\$out\""
    ok "raw ($A): $gen output holds no secret bytes" "! grep -qiE 'hunter2|BEGIN|$TSK|kQ7x|b3Bl|MII|Proc-Type|DEK-Info|home wifi|word1234|offwifi|defghij' <<< \"\$out\""
  done <<< "$SHAPES"
  for gen in $LEGIT; do
    f="$ROOT/legit.raw"; { head -c 100 /dev/zero; "$gen"; head -c 100 /dev/zero; } > "$f"
    out=$(PATH="$AP" bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$f" 2>&1); rc=$?
    ok "raw ($A): legitimate $gen is not a hit" "[ '$rc' = 0 ] && ! grep -q '^HIT' <<< \"\$out\""
  done
  f="$ROOT/off.raw"; { head -c 100 /dev/zero; nm_keyfile; } > "$f"
  want=$((100 + $(printf '[wifi-security]\nkey-mgmt=wpa-psk\n' | wc -c)))
  out=$(PATH="$AP" bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$f" 2>&1)
  ok "raw ($A): the offset is the exact byte where the line starts ($want)" "grep -qx 'HIT psk at offset $want' <<< \"\$out\""

  # the repo's own scripts and tests (which ship in the image) never match
  SELF="$ROOT/self.raw"; : > "$SELF"
  while IFS= read -r -d '' f; do { cat "$f"; printf '\0'; } >> "$SELF"; done \
    < <(find "$REPO/image" "$REPO/tests/image" -type f ! -path '*/__pycache__/*' -print0)
  out=$(PATH="$AP" bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$SELF" 2>&1); rc=$?
  ok "raw ($A): image/ and tests/image/ themselves give zero hits" "[ '$rc' = 0 ] && ! grep -q '^HIT' <<< \"\$out\""
done

# ---- 6. hits mapped to paths on a real ext4 filesystem --------------------

if command -v mkfs.ext4 >/dev/null && command -v debugfs >/dev/null; then
  # 6a. a deleted file in free space; zeroing the free space makes it clean
  IMG="$ROOT/test.img"
  truncate -s 64M "$IMG"
  mkfs.ext4 -q -F "$IMG" >/dev/null 2>&1
  printf 'owner %s\nnetwork={\n%s=hunter2hunter2\n}\n' "$WORD" "$PK" > "$ROOT/leak.txt"
  debugfs -w -R "write $ROOT/leak.txt leak.txt" "$IMG" >/dev/null 2>&1
  debugfs -w -R "rm leak.txt" "$IMG" >/dev/null 2>&1
  ok "fixture: deleted file is gone from the ext4 tree" "! debugfs -R 'ls -l /' '$IMG' 2>/dev/null | grep -q leak.txt"

  OUTR=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$IMG" --ext4 --denylist "$DENY" 2>&1); RCR=$?
  ok "ext4: leftover bytes in free space -> exit 1" "[ '$RCR' = 1 ]"
  ok "ext4: deleted denylist word -> HIT deny#1 at offset N (free space)" "grep -qE '^HIT deny#1 at offset [0-9]+ \\(free space\\)\$' <<< \"\$OUTR\""
  ok "ext4: deleted psk line -> HIT psk at offset N (free space)" "grep -qE '^HIT psk at offset [0-9]+ \\(free space\\)\$' <<< \"\$OUTR\""
  ok "ext4: output never contains the password" "! grep -qi hunter2 <<< \"\$OUTR\""
  ok "ext4: output never contains the denylist words" "! grep -qiE '$WORD|second-name' <<< \"\$OUTR\""
  ok "ext4: no temp files are left behind" "[ -z \"\$(ls -d /tmp/pilnk-verify.* 2>/dev/null)\" ]"
  for off in $(grep -E '^HIT ' <<< "$OUTR" | grep -oE 'offset [0-9]+' | grep -oE '[0-9]+' | sort -un); do
    dd if=/dev/zero of="$IMG" bs=4096 seek=$((off / 4096)) count=1 conv=notrunc status=none
  done
  OUTR2=$(bash "$VER" --root "$V" --boot "$V/boot/firmware" --raw "$IMG" --ext4 --denylist "$DENY" 2>&1); RCR2=$?
  ok "ext4: after the free space is zeroed the verifier is clean" "[ '$RCR2' = 0 ] && ! grep -q '^HIT' <<< \"\$OUTR2\""

  # 6b. live files, fs metadata, and the public-PiLNK-file rule
  T="$ROOT/ext4tree"; make_clean "$T"
  mkdir -p "$T/srv"
  printf '%s=hunter2hunter2\n' "$PK" > "$T/srv/live.conf"
  printf '%s=hunter2hunter2\n' "$PK" > "$T/srv/$WORD-notes.conf"
  GD="$T/home/pilnk/pilnk"
  FMARK="$ROOT/filter-ran"
  ( cd "$GD" && git init -q . && git config user.email t@t && git config user.name t \
    && printf 'by %s\n' "$WORD" > public.txt && echo clean > changed.txt \
    && mkdir -p static lib && echo app > static/app.js && echo lib > lib/a.py && echo hello > tail.txt \
    && git add public.txt changed.txt static/app.js lib/a.py tail.txt && git commit -qm init \
    && printf 'now %s\n' "$WORD" > changed.txt && printf 'mine %s\n' "$WORD" > extra.txt \
    && echo plain > "static/$WORD-notes.txt" \
    && git config filter.x.clean "touch '$FMARK'; cat" && echo '* filter=x' > .git/info/attributes \
    && touch -d '2001-01-01' public.txt && rm -rf .git/logs && git config --remove-section user ) >/dev/null 2>&1
  IDX0=$(sha1sum < "$GD/.git/index")
  IMG2="$ROOT/map.img"; truncate -s 64M "$IMG2"
  mkfs.ext4 -q -F -L "$WORD" -d "$T" "$IMG2" >/dev/null 2>&1
  # N1: a deleted file whose name holds a denylist word, in a tracked directory
  echo plain > "$ROOT/plain.txt"
  printf 'cd /home/pilnk/pilnk/lib\nwrite %s %s-old.txt\nrm %s-old.txt\n' "$ROOT/plain.txt" "$WORD" "$WORD" > "$ROOT/rm.cmd"
  debugfs -w -f "$ROOT/rm.cmd" "$IMG2" >/dev/null 2>&1
  printf '%s="hunter2hunter2"\n' "$PK" > "$ROOT/gone.txt"
  debugfs -w -R "write $ROOT/gone.txt gone.txt" "$IMG2" >/dev/null 2>&1
  debugfs -w -R "rm gone.txt" "$IMG2" >/dev/null 2>&1
  ok "fixture: the deleted name is gone from lib/ but its bytes are still there" \
     "! debugfs -R 'ls /home/pilnk/pilnk/lib' '$IMG2' 2>/dev/null | grep -q '$WORD' && grep -qa '$WORD-old' '$IMG2'"
  # N1: a denylist word in the tail slack (past i_size) of a public tracked file
  BSZ=$(debugfs -R stats "$IMG2" 2>/dev/null | awk -F: '/^Block size:/ { gsub(/[ \t]/, "", $2); print $2 }')
  TBLK=$(debugfs -R 'bmap /home/pilnk/pilnk/tail.txt 0' "$IMG2" 2>/dev/null)
  printf '%s' "$WORD" | dd of="$IMG2" bs=1 seek=$((TBLK * BSZ + 100)) conv=notrunc status=none
  OUTM=$(bash "$VER" --root "$T" --boot "$T/boot/firmware" --raw "$IMG2" --ext4 --denylist "$DENY" 2>&1); RCM=$?
  ok "map: exit 1" "[ '$RCM' = 1 ]"
  ok "map: live file -> HIT psk at its path" "grep -qx 'HIT psk at /srv/live.conf' <<< \"\$OUTM\""
  ok "map: deleted file -> HIT psk at offset N (free space)" "grep -qE '^HIT psk at offset [0-9]+ \\(free space\\)\$' <<< \"\$OUTM\""
  ok "map: superblock label -> HIT deny#1 at offset N (fs metadata)" "grep -qE '^HIT deny#1 at offset [0-9]+ \\(fs metadata\\)\$' <<< \"\$OUTM\""
  ok "map: a name in a directory block -> HIT at the directory" "grep -qx 'HIT deny#1 at /srv' <<< \"\$OUTM\""
  ok "map: a path holding a denylist word is withheld" "grep -qx 'HIT psk at <path withheld: contains deny#1>' <<< \"\$OUTM\""
  ok "map: tracked file unchanged from HEAD -> SKIP, not a failure" \
     "grep -qx 'SKIP deny#1 in /home/pilnk/pilnk/public.txt (public PiLNK file)' <<< \"\$OUTM\" && ! grep -q '^HIT deny#1 at /home/pilnk/pilnk/public.txt' <<< \"\$OUTM\""
  ok "map: tracked file modified since HEAD -> HIT" "grep -qx 'HIT deny#1 at /home/pilnk/pilnk/changed.txt' <<< \"\$OUTM\""
  ok "map: untracked file in the clone -> HIT" "grep -qx 'HIT deny#1 at /home/pilnk/pilnk/extra.txt' <<< \"\$OUTM\""
  ok "N1: untracked file with a denylisted name in a tracked dir -> HIT at the dir, not SKIP" \
     "grep -qx 'HIT deny#1 at /home/pilnk/pilnk/static' <<< \"\$OUTM\" && ! grep -q '^SKIP deny#1 in /home/pilnk/pilnk/static' <<< \"\$OUTM\""
  ok "N1: a denylisted name deleted from a tracked dir (dir slack) -> HIT at the dir, not SKIP" \
     "grep -qx 'HIT deny#1 at /home/pilnk/pilnk/lib' <<< \"\$OUTM\" && ! grep -q '^SKIP deny#1 in /home/pilnk/pilnk/lib' <<< \"\$OUTM\""
  ok "N1: a denylist word in a public file's tail slack (past i_size) -> HIT, not SKIP" \
     "grep -qx 'HIT deny#1 at /home/pilnk/pilnk/tail.txt' <<< \"\$OUTM\" && ! grep -q '^SKIP deny#1 in /home/pilnk/pilnk/tail.txt' <<< \"\$OUTM\""
  ok "N5: the image repo's clean filter never runs" "[ ! -e '$FMARK' ]"
  ok "map: output never contains the password or a denylist word" "! grep -qiE 'hunter2|$WORD|second-name' <<< \"\$OUTM\""
  ok "map: every line has a known shape" \
     "! grep -vE '^(HIT [a-z#0-9-]+ at (/[^ ]*|offset [0-9]+ \\((free space|fs metadata|unlinked file)\\)|<path withheld: contains deny#[0-9]+>)|SKIP deny#[0-9]+ in /.* \\(public PiLNK file\\)|NOT CLEAN: .*)\$' <<< \"\$OUTM\""
  ok "map: the clone's git index is untouched (read-only git)" "[ \"\$(sha1sum < '$GD/.git/index')\" = '$IDX0' ]"

  # only SKIPs left -> clean
  T2="$ROOT/ext4tree2"; make_clean "$T2"; GD2="$T2/home/pilnk/pilnk"
  ( cd "$GD2" && git init -q . && git config user.email t@t && git config user.name t \
    && printf 'by %s\n' "$WORD" > public.txt && git add public.txt && git commit -qm init && rm -rf .git/logs && git config --remove-section user ) >/dev/null 2>&1
  IMG3="$ROOT/map3.img"; truncate -s 64M "$IMG3"; mkfs.ext4 -q -F -d "$T2" "$IMG3" >/dev/null 2>&1
  OUTS3=$(bash "$VER" --root "$T2" --boot "$T2/boot/firmware" --raw "$IMG3" --ext4 --denylist "$DENY" 2>&1); RCS3=$?
  ok "map: an image whose only denylist hits are public PiLNK files is clean" \
     "[ '$RCS3' = 0 ] && grep -q '^SKIP deny#1 in /home/pilnk/pilnk/public.txt' <<< \"\$OUTS3\" && grep -q '^CLEAN' <<< \"\$OUTS3\""

  # N6: a hit in a saved-network file names its directory, never the file
  T4="$ROOT/ext4tree4"; make_clean "$T4"; mkdir -p "$T4/etc/wpa_supplicant"
  printf '%s=hunter2hunter2\n' "$PK" > "$T4/etc/NetworkManager/system-connections/HomeNetAlpha.nmconnection"
  printf '%s=hunter2hunter2\n' "$PK" > "$T4/etc/netplan/90-NM-HomeNetBeta.yaml"
  printf '%s=hunter2hunter2\n' "$PK" > "$T4/etc/wpa_supplicant/HomeNetGamma.conf"
  IMG4="$ROOT/map4.img"; truncate -s 64M "$IMG4"; mkfs.ext4 -q -F -d "$T4" "$IMG4" >/dev/null 2>&1
  OUT4=$(bash "$VER" --root "$T4" --boot "$T4/boot/firmware" --raw "$IMG4" --ext4 2>&1)
  ok "N6: NM / netplan / wpa_supplicant hits name the directory only" \
     "grep -qx 'HIT psk at /etc/NetworkManager/system-connections' <<< \"\$OUT4\" && grep -qx 'HIT psk at /etc/netplan' <<< \"\$OUT4\" && grep -qx 'HIT psk at /etc/wpa_supplicant' <<< \"\$OUT4\""
  ok "N6: no HIT line names a file in those directories" \
     "! grep -E '^HIT .*(HomeNet|at /etc/(NetworkManager/system-connections|netplan|wpa_supplicant)/)' <<< \"\$OUT4\" | grep -q ."

  # N7: a partial clone whose promisor remote runs a command; a tree object is
  # missing, so resolving HEAD:sub/p2.txt would lazy-fetch it through that command
  T7="$ROOT/ext4tree7"; make_clean "$T7"; GD7="$T7/home/pilnk/pilnk"; LMARK="$ROOT/lazy-fetch-ran"
  ( cd "$GD7" && git init -q . && git config user.email t@t && git config user.name t \
    && mkdir sub && echo one > sub/p1.txt && echo two > sub/p2.txt && git add sub && git commit -qm init \
    && st=$(git rev-parse HEAD:sub) && rm -f ".git/objects/${st:0:2}/${st:2}" \
    && printf 'by %s\n' "$WORD" > sub/p2.txt \
    && git config core.repositoryformatversion 1 && git config extensions.partialClone origin \
    && git config remote.origin.url file:///nowhere && git config remote.origin.promisor true \
    && git config remote.origin.uploadpack "touch '$LMARK'; false" && rm -rf .git/logs && git config --remove-section user ) >/dev/null 2>&1
  IMG7="$ROOT/map7.img"; truncate -s 64M "$IMG7"; mkfs.ext4 -q -F -d "$T7" "$IMG7" >/dev/null 2>&1
  OUT7=$(bash "$VER" --root "$T7" --boot "$T7/boot/firmware" --raw "$IMG7" --ext4 --denylist "$DENY" 2>&1); RC7=$?
  ok "N7: the image repo's promisor remote command never runs (no lazy fetch)" "[ ! -e '$LMARK' ]"
  ok "N7: the hit in the partial clone stays a HIT" \
     "[ '$RC7' = 1 ] && grep -qx 'HIT deny#1 at /home/pilnk/pilnk/sub/p2.txt' <<< \"\$OUT7\""

  # dpkg: a secret-shaped hit inside an unmodified Debian package file (libgnutls
  # embeds its FIPS self-test keys) is SKIPped; anything else stays a HIT
  md5of() { md5sum < "$1" | cut -c1-32; }
  T8="$ROOT/ext4tree8"; make_clean "$T8"; L8="$T8/usr/lib/aarch64-linux-gnu"; I8="$T8/var/lib/dpkg/info"
  mkdir -p "$L8" "$I8" "$T8/usr/share/doc/libfake1"
  { printf 'ELF\0\0'; pem_rsa; printf '\0\0'; } > "$L8/libfake.so.1"
  { nm_keyfile; printf '\0'; ts_key; } > "$T8/usr/share/doc/libfake1/example.conf"
  { printf 'ELF\0\0'; pem_rsa; } > "$L8/libedited.so.1"
  { printf 'ELF\0\0'; pem_rsa; } > "$L8/libloose.so.1"
  printf 'ELF\0by %s\0' "$WORD" > "$L8/libdeny.so.1"
  echo x > "$L8/libtail.so.1"
  { printf '%s  usr/lib/aarch64-linux-gnu/libfake.so.1\n' "$(md5of "$L8/libfake.so.1")"
    printf '%s  usr/share/doc/libfake1/example.conf\n' "$(md5of "$T8/usr/share/doc/libfake1/example.conf")"
    printf '%s  usr/lib/aarch64-linux-gnu/libtail.so.1\n' "$(md5of "$L8/libtail.so.1")"
  } > "$I8/libfake1:arm64.md5sums"
  { printf '%s  usr/lib/aarch64-linux-gnu/libedited.so.1\n' 0123456789abcdef0123456789abcdef
    printf '%s  usr/lib/aarch64-linux-gnu/libdeny.so.1\n' "$(md5of "$L8/libdeny.so.1")"
  } > "$I8/libother2.md5sums"
  IMG8="$ROOT/map8.img"; truncate -s 64M "$IMG8"; mkfs.ext4 -q -F -d "$T8" "$IMG8" >/dev/null 2>&1
  # a key in the slack after a listed, unmodified file (past i_size)
  BSZ8=$(debugfs -R stats "$IMG8" 2>/dev/null | awk -F: '/^Block size:/ { gsub(/[ \t]/, "", $2); print $2 }')
  TBLK8=$(debugfs -R 'bmap /usr/lib/aarch64-linux-gnu/libtail.so.1 0' "$IMG8" 2>/dev/null)
  { printf '\n%s RSA %s\n' "$BEG" "$PKEY"; head -c 48 /dev/urandom | base64 -w 64; } \
    | dd of="$IMG8" bs=1 seek=$((TBLK8 * BSZ8 + 16)) conv=notrunc status=none
  OUT8=$(bash "$VER" --root "$T8" --boot "$T8/boot/firmware" --raw "$IMG8" --ext4 --denylist "$DENY" 2>&1); RC8=$?
  ok "dpkg: key in a listed file whose md5 matches (multiarch .md5sums) -> SKIP, not a HIT" \
     "grep -qx 'SKIP privkey in /usr/lib/aarch64-linux-gnu/libfake.so.1 (unmodified Debian package file)' <<< \"\$OUT8\" && ! grep -q '^HIT privkey at /usr/lib/aarch64-linux-gnu/libfake.so.1' <<< \"\$OUT8\""
  ok "dpkg: psk and tailscale ids in a listed unmodified file -> SKIP" \
     "grep -qx 'SKIP psk in /usr/share/doc/libfake1/example.conf (unmodified Debian package file)' <<< \"\$OUT8\" && grep -qx 'SKIP tailscale in /usr/share/doc/libfake1/example.conf (unmodified Debian package file)' <<< \"\$OUT8\""
  ok "dpkg: listed file whose md5 differs -> HIT" "grep -qx 'HIT privkey at /usr/lib/aarch64-linux-gnu/libedited.so.1' <<< \"\$OUT8\""
  ok "dpkg: file not listed in any md5sums -> HIT" "grep -qx 'HIT privkey at /usr/lib/aarch64-linux-gnu/libloose.so.1' <<< \"\$OUT8\""
  ok "dpkg: a denylist word in a listed unmodified file -> HIT, never SKIP" \
     "grep -qx 'HIT deny#1 at /usr/lib/aarch64-linux-gnu/libdeny.so.1' <<< \"\$OUT8\" && ! grep -q '^SKIP deny#1 in /usr/lib' <<< \"\$OUT8\""
  ok "dpkg: a key in the slack after a listed file (past i_size) -> HIT" \
     "grep -qx 'HIT privkey at /usr/lib/aarch64-linux-gnu/libtail.so.1' <<< \"\$OUT8\" && ! grep -q '^SKIP privkey in /usr/lib/aarch64-linux-gnu/libtail' <<< \"\$OUT8\""
  ok "dpkg: exit 1, output holds no key, psk, tailscale or denylist bytes" \
     "[ '$RC8' = 1 ] && ! grep -qiE 'hunter2|BEGIN|$TSK|kQ7x|MII|$WORD' <<< \"\$OUT8\""
  ok "dpkg: every line has a known shape" \
     "! grep -vE '^(HIT [a-z#0-9-]+ at (/[^ ]*|offset [0-9]+ \\((free space|fs metadata|unlinked file)\\))|SKIP (privkey|psk|tailscale) in /[^ ]* \\(unmodified Debian package file\\)|NOT CLEAN: .*)\$' <<< \"\$OUT8\""
  OUT8R=$(bash "$VER" --root "$T8" --boot "$T8/boot/firmware" --raw "$IMG8" 2>&1); RC8R=$?
  ok "dpkg: --raw without path mapping -> the same keys are HITs at offsets, no SKIP" \
     "[ '$RC8R' = 1 ] && grep -qE '^HIT privkey at offset [0-9]+\$' <<< \"\$OUT8R\" && ! grep -q '^SKIP' <<< \"\$OUT8R\""
  # only unmodified-package hits -> clean; unreadable md5sums -> HIT
  T9="$ROOT/ext4tree9"; make_clean "$T9"; mkdir -p "$T9/usr/lib/aarch64-linux-gnu" "$T9/var/lib/dpkg/info"
  cp "$L8/libfake.so.1" "$T9/usr/lib/aarch64-linux-gnu/"
  grep libfake.so "$I8/libfake1:arm64.md5sums" > "$T9/var/lib/dpkg/info/libfake1:arm64.md5sums"
  IMG9="$ROOT/map9.img"; truncate -s 64M "$IMG9"; mkfs.ext4 -q -F -d "$T9" "$IMG9" >/dev/null 2>&1
  OUT9=$(bash "$VER" --root "$T9" --boot "$T9/boot/firmware" --raw "$IMG9" --ext4 2>&1); RC9=$?
  ok "dpkg: an image whose only secret-shaped hits are unmodified package files is clean" \
     "[ '$RC9' = 0 ] && grep -q '^SKIP privkey in /usr/lib/aarch64-linux-gnu/libfake.so.1' <<< \"\$OUT9\" && grep -q '^CLEAN' <<< \"\$OUT9\""
  rm -f "$T9/var/lib/dpkg/info/libfake1:arm64.md5sums"; mkdir "$T9/var/lib/dpkg/info/libfake1:arm64.md5sums"
  OUT9B=$(bash "$VER" --root "$T9" --boot "$T9/boot/firmware" --raw "$IMG9" --ext4 2>&1); RC9B=$?
  ok "dpkg: md5sums missing or unreadable -> HIT" \
     "[ '$RC9B' = 1 ] && grep -qx 'HIT privkey at /usr/lib/aarch64-linux-gnu/libfake.so.1' <<< \"\$OUT9B\""
else
  echo "FAIL: ext4 tools (mkfs.ext4, debugfs) missing; raw-image tests cannot run"
fi

# ---- 7. verifier image mode teardown (M8), with fakes ---------------------

M8B="$ROOT/m8bin"; mkdir -p "$M8B"
fake_in() { printf '#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' "$2" "$3" > "$1/$2"; chmod +x "$1/$2"; }
printf '#!/bin/bash\n[ "$1" = -u ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n' > "$M8B/id"; chmod +x "$M8B/id"
fake_in "$M8B" losetup 'case "$*" in *--show*) echo /dev/loop97;; esac; true'
fake_in "$M8B" mount 'true'
fake_in "$M8B" umount '[ "$1" = -l ]'
fake_in "$M8B" udevadm 'true'
IMGF="$ROOT/m8.img"; head -c 8192 /dev/zero > "$IMGF"
: > "$CALLS"
OUT8=$(PATH="$M8B:$PATH" bash "$VER" "$IMGF" 2>&1); RC8=$?
ok "M8: image mode retries a failed umount with umount -l (root and boot)" \
   "grep -qE '^umount -l .*/root\$' '$CALLS' && grep -qE '^umount -l .*/boot\$' '$CALLS'"
ok "M8: the loop device is detached and the temp dir removed" \
   "grep -q '^losetup -d /dev/loop97' '$CALLS' && [ -z \"\$(ls -d /tmp/pilnk-verify.* 2>/dev/null)\" ]"

# ---- 8. sterilise review fixes ---------------------------------------------

# I2: PILNK_ROOT without PILNK_BOOT means $PILNK_ROOT/boot/firmware, never the host's
I2R="$ROOT/i2"; make_clean "$I2R"; : > "$I2R/boot/firmware/ssh"; : > "$I2R/boot/firmware/user-data"
OUTB=$(PILNK_ROOT="$I2R" bash "$STER" --no-poweroff --no-zero 2>&1); RCB=$?
ok "I2: PILNK_ROOT alone -> boot is \$PILNK_ROOT/boot/firmware" \
   "[ '$RCB' = 0 ] && [ ! -e '$I2R/boot/firmware/ssh' ] && [ ! -e '$I2R/boot/firmware/user-data' ] && grep -qF '(boot $I2R/boot/firmware)' <<< \"\$OUTB\""
ok "I2: a fake-root run never names the host's /boot/firmware" \
   "! grep -qE '(^|[^[:alnum:]_./-])/boot/firmware' <<< \"\$OUTB\""

# M2: an interrupted zero-fill removes its fill file; the check catches a leftover
ZI="$ROOT/zi"; make_clean "$ZI"; DDB="$ROOT/ddbin"; mkdir -p "$DDB"
printf '#!/bin/bash\nfor a; do case $a in of=*) : > "${a#of=}";; esac; done\nkill -TERM $PPID\nsleep 1\n' > "$DDB/dd"; chmod +x "$DDB/dd"
PATH="$DDB:$PATH" PILNK_ZERO_LIMIT_MB=1 PILNK_ROOT="$ZI" bash "$STER" --no-poweroff >/dev/null 2>&1; RCZI=$?
ok "M2: zero-fill killed by TERM -> non-zero exit, no fill file left" \
   "[ '$RCZI' != 0 ] && [ -z \"\$(find '$ZI' -name '.pilnk-zero*')\" ]"

# M3: /var/log keeps its directories
L="$ROOT/m3"; make_clean "$L"
mkdir -p "$L/var/log/apt" "$L/var/log/journal/0123abcd"
echo x > "$L/var/log/apt/history.log"; echo x > "$L/var/log/journal/0123abcd/system.journal"; echo x > "$L/var/log/syslog"
PILNK_ROOT="$L" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1
ok "M3: /var/log files are gone but its directories stay" \
   "[ -d '$L/var/log/apt' ] && [ ! -e '$L/var/log/apt/history.log' ] && [ ! -e '$L/var/log/syslog' ] && [ -d '$L/var/log/journal' ] && [ -z \"\$(ls -A '$L/var/log/journal')\" ]"

# M4: fake-root disable removes .wants links and the sshd alias, never unit files
U="$ROOT/m4"; make_clean "$U"; S="$U/etc/systemd/system"
printf '[Unit]\n' > "$S/pilnk.service"; printf '[Unit]\n' > "$S/readsb.service"
ln -s /etc/systemd/system/pilnk.service "$S/multi-user.target.wants/pilnk.service"
ln -s /lib/systemd/system/ssh.service "$S/sshd.service"
PILNK_ROOT="$U" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1
ok "M4: unit files kept, .wants link and sshd alias removed" \
   "[ -f '$S/pilnk.service' ] && [ -f '$S/readsb.service' ] && [ ! -L '$S/multi-user.target.wants/pilnk.service' ] && [ ! -L '$S/sshd.service' ]"

# M5: an enable link under any *.wants counts
M5R="$ROOT/m5"; make_clean "$M5R"; S="$M5R/etc/systemd/system"; mkdir -p "$S/default.target.wants"
mv "$S/multi-user.target.wants/pilnk-firstboot.service" "$S/default.target.wants/"
run_check "$M5R"
ok "M5: first boot enabled via default.target.wants passes" "[ '$N' = 0 ]"

# M6: a failed cloud-init clean makes sterilise fail (the step run on its own)
CI="$ROOT/m6"; make_clean "$CI"
mkdir -p "$CI/var/lib/cloud/instance"; echo x > "$CI/var/lib/cloud/instance/user-data.txt"
: > "$CI/etc/netplan/50-cloud-init.yaml"
fakebin cloud-init 'exit 1'
OUTCI=$( source "$STER"; R="$CI"; B="$CI/boot/firmware"; REAL=1; ERRORS=0; cloud_init_step; echo "errors=$ERRORS" ) 2>&1
fakebin cloud-init 'true'
ok "M6: failed cloud-init clean -> FAIL cloud-init-clean and one error counted" \
   "grep -q '^FAIL cloud-init-clean' <<< \"\$OUTCI\" && grep -qx 'errors=1' <<< \"\$OUTCI\""
ok "M6: /var/lib/cloud and 50-cloud-init.yaml removed" \
   "[ ! -e '$CI/var/lib/cloud' ] && [ ! -e '$CI/etc/netplan/50-cloud-init.yaml' ]"

# I3 stage 1: on the real root, re-run detached via systemd-run, then exit.
# Every command that could change the host is a logging fake here, and id
# claims root, so even a broken dispatch could not touch the machine.
S1B="$ROOT/s1bin"; mkdir -p "$S1B"
for c in rm find dd cp sed mkdir ln chown chmod sync git passwd journalctl systemctl cloud-init \
         apt-get poweroff loginctl mv touch truncate; do fake_in "$S1B" "$c" 'true'; done
fake_in "$S1B" systemd-run 'true'
cp "$M8B/id" "$S1B/id"
: > "$CALLS"
OUT1=$(PATH="$S1B:$PATH" bash "$STER" --no-zero 2>&1); RC1=$?
ok "I3: real root -> systemd-run --unit=pilnk-sterilise --collect ... --stage2, exit 0" \
   "[ '$RC1' = 0 ] && grep -qE '^systemd-run .*--unit=pilnk-sterilise .*--collect .*/pilnk-sterilise\\.sh --no-zero --stage2\$' '$CALLS'"
ok "I3: stage 1 does nothing else itself" "[ \"\$(grep -vc '^systemd-run ' '$CALLS')\" = 0 ]"
ok "I3: stage 1 prints one line: the session closes and the Pi powers off" \
   "[ \"\$(printf '%s\n' \"\$OUT1\" | wc -l)\" = 1 ] && grep -qi 'session' <<< \"\$OUT1\" && grep -qi 'power' <<< \"\$OUT1\""

# I3 stage 2 (on a fake root): end pilnk's sessions, wait, re-remove histories
# and login records written on logout, then check and power off.
S2R="$ROOT/s2"; make_clean "$S2R"
fakebin loginctl 'case "$1" in
  terminate-user) echo hist > "$FAKE_R/home/pilnk/.bash_history"; mkdir -p "$FAKE_R/var/lib/wtmpdb"; echo x > "$FAKE_R/var/lib/wtmpdb/wtmp.db" ;;
  show-user) n=$(cat "$FAKE_R.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_R.n"; [ "$n" -lt 2 ] && echo c1 ;;
esac; true'
fakebin sleep 'true'
: > "$CALLS"
OUT2=$(FAKE_R="$S2R" PILNK_ROOT="$S2R" bash "$STER" --no-zero --stage2 2>&1); RC2=$?
rm -f "$BIN/sleep"
first() { grep -n "$1" "$CALLS" | head -1 | cut -d: -f1; }
ok "I3: stage 2 -> exit 0, histories and login records written at logout are gone" \
   "[ '$RC2' = 0 ] && [ ! -e '$S2R/home/pilnk/.bash_history' ] && [ -z \"\$(ls -A '$S2R/var/lib/wtmpdb' 2>/dev/null)\" ]"
lineno() { grep -n -- "$2" <<< "$1" | head -1 | cut -d: -f1; }
ok "I3: stage 2 calls terminate-user pilnk, and never powers off a fake root" \
   "grep -q '^loginctl terminate-user pilnk' '$CALLS' && ! grep -q '^poweroff' '$CALLS'"
ok "N2: stage 2 order: pre-check, then close sessions, then the final self-check" \
   "a=\$(lineno \"\$OUT2\" '^== pre-check'); b=\$(lineno \"\$OUT2\" '^closed: login sessions'); c=\$(lineno \"\$OUT2\" '^== self-check'); [ -n \"\$a\" ] && [ -n \"\$b\" ] && [ -n \"\$c\" ] && [ \"\$a\" -lt \"\$b\" ] && [ \"\$b\" -lt \"\$c\" ]"
ok "I3: stage 2 waits until loginctl reports no sessions" "[ \"\$(grep -c '^loginctl show-user pilnk' '$CALLS')\" -ge 3 ]"
ok "I3: stage 2 never re-launches itself" "! grep -q '^systemd-run' '$CALLS'"
: > "$CALLS"
PILNK_ROOT="$S2R" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1
ok "I3: a plain fake-root run stays in-process (no systemd-run, no loginctl)" "! grep -qE '^(systemd-run|loginctl) ' '$CALLS'"

# N2a: the pre-check fails -> exit non-zero, sessions NOT ended, says where the log is
fakebin loginctl 'true'
S2A="$ROOT/s2a"; make_clean "$S2A"; printf '[Unit]\n' > "$S2A/etc/systemd/system/sshd.service"
: > "$CALLS"
OUT2A=$(PILNK_CONSOLE="$ROOT/console-a" PILNK_ROOT="$S2A" bash "$STER" --no-zero --stage2 2>&1); RC2A=$?
ok "N2a: failed pre-check -> exit 1, no terminate-user, no poweroff" \
   "[ '$RC2A' = 1 ] && ! grep -q '^loginctl terminate-user' '$CALLS' && ! grep -q '^poweroff' '$CALLS'"
ok "N2a: says what failed and where the log is" \
   "grep -q '^FAIL ssh-enabled: /etc/systemd/system/sshd.service' <<< \"\$OUT2A\" && grep -qF '/run/pilnk-sterilise.log' <<< \"\$OUT2A\""
ok "N2a: no failure report written (the session is still open)" \
   "[ ! -e '$S2A/boot/firmware/pilnk-sterilise-failed.txt' ] && [ ! -e '$ROOT/console-a' ]"

# N2b: the final check fails after the sessions ended -> console + boot-partition report
fakebin loginctl 'case "$1" in terminate-user) mkdir -p "$FAKE_R/etc/ssh"; echo hunter2 > "$FAKE_R/etc/ssh/ssh_host_ed25519_key" ;; esac; true'
S2B="$ROOT/s2b"; make_clean "$S2B"
: > "$CALLS"
OUT2B=$(FAKE_R="$S2B" PILNK_CONSOLE="$ROOT/console-b" PILNK_ROOT="$S2B" bash "$STER" --no-zero --stage2 2>&1); RC2B=$?
FAILF="$S2B/boot/firmware/pilnk-sterilise-failed.txt"
ok "N2b: failed final check -> exit 1, sessions were ended, no poweroff" \
   "[ '$RC2B' = 1 ] && grep -q '^loginctl terminate-user pilnk' '$CALLS' && ! grep -q '^poweroff' '$CALLS'"
ok "N2b: the result lines go to the console" "grep -qx 'FAIL ssh-hostkeys: /etc/ssh/ssh_host_ed25519_key' '$ROOT/console-b' 2>/dev/null"
ok "N2b: ... and to \$B/pilnk-sterilise-failed.txt" "grep -qx 'FAIL ssh-hostkeys: /etc/ssh/ssh_host_ed25519_key' '$FAILF' 2>/dev/null"
ok "N2b: neither holds file contents" "[ -f '$FAILF' ] && [ -f '$ROOT/console-b' ] && ! grep -q hunter2 '$ROOT/console-b' '$FAILF'"
run_check "$S2B"
ok "N2c: the failure report itself fails the check (sterilise-failed)" \
   "grep -qx 'FAIL sterilise-failed: /boot/firmware/pilnk-sterilise-failed.txt' <<< \"\$OUT\""
rm -f "$S2B/etc/ssh/ssh_host_ed25519_key"; fakebin loginctl 'true'
PILNK_ROOT="$S2B" bash "$STER" --no-zero --stage2 >/dev/null 2>&1; RC2C=$?
ok "N2c: the next run removes an old failure report and passes" "[ '$RC2C' = 0 ] && [ ! -e '$FAILF' ]"

# PILNK_ROOT=/. is the live system: stage 1 (every mutating command a fake, id says 0)
if [ "$(id -u)" != 0 ]; then
  : > "$CALLS"
  OUTDOT=$(PILNK_ROOT=/. PATH="$S1B:$PATH" bash "$STER" --no-zero 2>&1); RCDOT=$?
  ok "PILNK_ROOT=/. -> the live system: only systemd-run (stage 1)" \
     "[ '$RCDOT' = 0 ] && grep -q '^systemd-run .*--stage2\$' '$CALLS' && [ \"\$(grep -vc '^systemd-run ' '$CALLS')\" = 0 ]"
fi

# ---- 9. final review fixes -------------------------------------------------

# C1: wiedehopf's stock /etc/default/readsb carries --device-type, which is not a pin
WIEDEHOPF='RECEIVER_OPTIONS="--device 0 --device-type rtlsdr --gain auto --ppm 0"'
C1="$ROOT/c1"; make_clean "$C1"; printf '%s\n' "$WIEDEHOPF" > "$C1/etc/default/readsb"
PILNK_ROOT="$C1" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1; RCC1=$?
run_check "$C1"
ok "C1: sterilised wiedehopf default passes the check (exit 0, no readsb-device)" \
   "[ '$RCC1' = 0 ] && [ '$N' = 0 ] && ! grep -q '^FAIL readsb-device' <<< \"\$OUT\""
ok "C1: sterilise keeps --device-type rtlsdr and drops --device 0" \
   "grep -q -- '--device-type rtlsdr' '$C1/etc/default/readsb' && ! grep -qE -- '--device[ =]' '$C1/etc/default/readsb'"
for left in '--device 0 ' '--device=0 ' '--device 00000003 '; do
  printf 'RECEIVER_OPTIONS="%s--device-type rtlsdr --gain auto --ppm 0"\n' "$left" > "$C1/etc/default/readsb"
  run_check "$C1"
  ok "C1: a leftover '${left% }' next to --device-type fails readsb-device" \
     "[ '$N' = 1 ] && grep -q '^FAIL readsb-device: /etc/default/readsb' <<< \"\$OUT\""
done
printf 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto --ppm 0"\n' > "$C1/etc/default/readsb"
OUTVC1=$(bash "$VER" --root "$C1" --boot "$C1/boot/firmware" --raw "$EMPTYRAW" 2>&1); RCVC1=$?
ok "C1: the laptop verifier passes a --device-type-only readsb default" "[ '$RCVC1' = 0 ] && ! grep -q readsb-device <<< \"\$OUTVC1\""

# M8: an enable link whose unit file is missing (build step 2/2 skipped) fails
for u in pilnk-firstboot.service pilnk-wifi-setup.service; do
  M8R="$ROOT/m8-$u"; make_clean "$M8R"; rm -f "$M8R/etc/systemd/system/$u"
  run_check "$M8R"
  ok "M8: $u enabled but its unit file is missing -> FAIL unit-file-missing" \
     "[ '$N' = 1 ] && grep -qx 'FAIL unit-file-missing: /etc/systemd/system/$u' <<< \"\$OUT\""
done
M8R="$ROOT/m8-rel"; make_clean "$M8R"; W8="$M8R/etc/systemd/system/multi-user.target.wants"
ln -sfn ../pilnk-firstboot.service "$W8/pilnk-firstboot.service"
run_check "$M8R"
ok "M8: a relative enable link to an existing unit file passes" "[ '$N' = 0 ]"
rm -f "$M8R/etc/systemd/system/pilnk-firstboot.service"; run_check "$M8R"
ok "M8: a relative enable link to a missing unit file fails" "grep -q '^FAIL unit-file-missing: ' <<< \"\$OUT\""

# M3: less (XDG) and nano history, for root and every /home user
M3R="$ROOT/m3x"; make_clean "$M3R"; mkdir -p "$M3R/home/other"
for h in "$M3R/root" "$M3R/home/pilnk" "$M3R/home/other"; do
  mkdir -p "$h/.local/state" "$h/.local/share/nano"; echo x > "$h/.local/state/lesshst"
  echo x > "$h/.local/share/nano/search_history"; echo x > "$h/.lesshst"
done
PILNK_ROOT="$M3R" bash "$STER" --no-poweroff --no-zero >/dev/null 2>&1; RCM3=$?
ok "M3: sterilise removes .lesshst, .local/state/lesshst and .local/share/nano for every home; check passes" \
   "[ '$RCM3' = 0 ] && [ -z \"\$(find '$M3R' \\( -name lesshst -o -name .lesshst -o -path '*/.local/share/nano' \\) -print -quit)\" ]"

# I5: NetworkManager state that can hold BSSIDs/SSIDs is removed (never read) and checked
NM5="$ROOT/i5"; make_clean "$NM5"; NMD="$NM5/var/lib/NetworkManager"; mkdir -p "$NMD"
for f in NetworkManager.state timestamps seen-bssids secret_key NetworkManager-intern.conf; do echo "HomeNetAlpha" > "$NMD/$f"; done
echo x > "$NMD/internal-abc-eth0.lease"
run_check "$NM5"
ok "I5: each NM state file fails nm-state (leases stay dhcp-lease)" \
   "[ \"\$(grep -c '^FAIL nm-state: /var/lib/NetworkManager/' <<< \"\$OUT\")\" = 5 ] && grep -q '^FAIL dhcp-lease: ' <<< \"\$OUT\" && ! grep -q 'nm-state: .*lease' <<< \"\$OUT\""
ok "I5: the check never prints NM state contents" "! grep -q HomeNetAlpha <<< \"\$OUT\""
OUTI5=$(PILNK_ROOT="$NM5" bash "$STER" --no-poweroff --no-zero 2>&1); RCI5=$?
run_check "$NM5"
ok "I5: sterilise removes them; check passes; contents never printed" \
   "[ '$RCI5' = 0 ] && [ '$N' = 0 ] && [ -z \"\$(ls -A '$NMD')\" ] && ! grep -q HomeNetAlpha <<< \"\$OUTI5\""

# I5 stage 2: NetworkManager and wpa_supplicant are stopped after the sessions
# close, and every network removal runs again after the stop (NM writes its
# state files and a renewed lease on the way down)
NMW='mkdir -p "$FAKE_R/var/lib/NetworkManager" "$FAKE_R/etc/NetworkManager/system-connections"
  for f in timestamps seen-bssids NetworkManager.state internal-x-wlan0.lease; do echo HomeNetAlpha > "$FAKE_R/var/lib/NetworkManager/$f"; done
  echo x > "$FAKE_R/etc/NetworkManager/system-connections/home.nmconnection"'
S5="$ROOT/i5s2"; make_clean "$S5"
fakebin systemctl "case \"\$*\" in 'stop NetworkManager.service wpa_supplicant.service') $NMW ;; esac; true"
: > "$CALLS"
OUTS5=$( source "$STER"; R="$S5"; B="$S5/boot/firmware"; D="$S5/home/pilnk/pilnk"; REAL=1; stop_network; echo "rc=$?" ) 2>&1
fakebin systemctl 'case "$1" in is-active) grep -qx "${@: -1}" "$ROOT/.active" 2>/dev/null;; esac'
run_check "$S5"
ok "I5: stop_network (real root) stops NetworkManager + wpa_supplicant" \
   "grep -qx 'systemctl stop NetworkManager.service wpa_supplicant.service' '$CALLS' && grep -qx 'rc=0' <<< \"\$OUTS5\""
ok "I5: files NM writes while stopping are removed afterwards; check passes" "[ '$N' = 0 ]"
ok "I5: stop_network prints no NM contents" "! grep -q HomeNetAlpha <<< \"\$OUTS5\""
# the detached run (fake root): a lease + NM state written while the sessions
# close are gone by the final check
S6="$ROOT/i5s2b"; make_clean "$S6"
fakebin loginctl "case \"\$1\" in terminate-user) $NMW ;; esac; true"
: > "$CALLS"
OUTS6=$(FAKE_R="$S6" PILNK_ROOT="$S6" bash "$STER" --no-zero --stage2 2>&1); RCS6=$?
fakebin loginctl 'true'
ok "I5: stage 2 removes NM state/leases/connections written after the pre-check; exit 0" \
   "[ '$RCS6' = 0 ] && [ -z \"\$(ls -A '$S6/var/lib/NetworkManager')\" ] && [ -z \"\$(ls -A '$S6/etc/NetworkManager/system-connections')\" ]"
ok "I5: stage 2 on a fake root never calls systemctl" "! grep -q '^systemctl ' '$CALLS'"
ok "I5: stage 2 order: sessions closed -> network stopped/removed -> self-check" \
   "a=\$(lineno \"\$OUTS6\" '^closed: login sessions'); b=\$(lineno \"\$OUTS6\" '^stopped: NetworkManager'); c=\$(lineno \"\$OUTS6\" '^== self-check'); [ -n \"\$a\" ] && [ -n \"\$b\" ] && [ -n \"\$c\" ] && [ \"\$a\" -lt \"\$b\" ] && [ \"\$b\" -lt \"\$c\" ]"
