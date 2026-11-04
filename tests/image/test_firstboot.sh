#!/bin/bash
# pilnk-firstboot.sh: resumable first boot. Everything external is faked.
. "$(dirname "$0")/lib.sh"
FB="$REPO/image/firstboot/pilnk-firstboot.sh"
UNIT="$REPO/image/firstboot/pilnk-firstboot.service"

# fakescript PATH 'BODY': a script (not on PATH) that logs "NAME argv" to $CALLS
fakescript() {
  mkdir -p "$(dirname "$1")"
  printf '#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' "$(basename "$1" .sh)" "$2" > "$1"
}

# fresh fake machine. Knobs (files under $ROOT): .grow_rc, .nm_fail (count of
# failing nm-online calls), .hw_rc, .upd_rc, .branch (absent = not a git checkout)
setup() {
  mkroot
  S="$ROOT/var/lib/pilnk-firstboot"; LOG="$ROOT/var/log/pilnk-firstboot.log"
  PD="$ROOT/home/pilnk/pilnk"; mkdir -p "$PD" "$ROOT/proc" "$ROOT/etc"
  printf 'processor\t: 0\nSerial\t\t: 10000000abcd7f3a\nModel\t\t: Raspberry Pi 4\n' > "$ROOT/proc/cpuinfo"
  printf '127.0.0.1\tlocalhost\n127.0.1.1\traspberrypi\n' > "$ROOT/etc/hosts"
  echo 0123456789abcdef0123456789abcdef > "$ROOT/etc/machine-id"
  fakebin findmnt 'echo /dev/mmcblk0p2'
  fakebin growpart 'exit $(cat "$ROOT/.grow_rc" 2>/dev/null || echo 1)'
  fakebin resize2fs 'true'
  fakebin ssh-keygen 'true'
  fakebin hostnamectl 'true'
  fakebin nm-online 'n=$(cat "$ROOT/.nm_fail" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n-1)) > "$ROOT/.nm_fail"; exit 1; fi'
  fakebin reboot '[ -e "$ROOT/var/lib/pilnk-firstboot/grown" ] && echo grown-first >> "$ROOT/.rb"; true'
  fakebin hostname 'true'
  fakebin ip 'n=$(cat "$ROOT/.noroute" 2>/dev/null || echo 0)
    [ -e "$ROOT/var/lib/pilnk-firstboot/network" ] && echo early >> "$ROOT/.early"
    if [ "$n" -gt 0 ]; then echo $((n-1)) > "$ROOT/.noroute"; exit 0; fi
    echo "default via 192.168.1.1 dev wlan0"'
  fakebin runuser 'while [ "$1" != -- ]; do shift; done; shift; VIA_RUNUSER=1 exec "$@"'
  # like real git >= 2.35.2: a repo owned by another user is refused unless run
  # as that user (runuser) or told safe.directory
  fakebin git 'if [ -e "$ROOT/.branch" ] && { [ -n "$VIA_RUNUSER" ] || printf "%s " "$@" | grep -q "safe.directory"; }; then cat "$ROOT/.branch"; else exit 128; fi'
  fakebin sleep 'true'
  fakescript "$PD/update.sh" 'echo "update-flag=$PILNK_UPDATE_NO_RESTART" >> "$CALLS"; exit $(cat "$ROOT/.upd_rc" 2>/dev/null || echo 0)'
  fakescript "$PD/pilnk-hw-setup.sh" 'echo "hw-env root=$PILNK_ROOT ni=$PILNK_HW_NONINTERACTIVE" >> "$CALLS"; exit $(cat "$ROOT/.hw_rc" 2>/dev/null || echo 0)'
  fakescript "$PD/bootstrap-selfheal.sh" 'echo "selfheal-env noblock=$PILNK_SELFHEAL_NO_BLOCK log=$PILNK_SELFHEAL_LOG" >> "$CALLS"'
}
run() {  # run [ENV=val ...]
  : > "$CALLS"
  env PILNK_ROOT="$ROOT" PILNK_DIR="$PD" PILNK_NET_WAIT=1 PILNK_POLL_SLEEP=0 "$@" bash "$FB"; RC=$?
}
# line number of the first CALLS line matching a fixed string (0 = never)
ln() { grep -nF -- "$1" "$CALLS" | head -1 | cut -d: -f1 | grep . || echo 0; }
called() { grep -qF -- "$1" "$CALLS"; }
before() { local a b; a=$(ln "$1"); b=$(ln "$2"); [ "$a" -gt 0 ] && [ "$b" -gt "$a" ]; }
markers() { (cd "$S" 2>/dev/null && ls | grep -vx grown | sort | paste -sd, -); }

# --- full run, in order ---
setup; run
ok "full: exit 0"                 '[ $RC -eq 0 ]'
ok "full: grow -> keys"           'before growpart ssh-keygen'
ok "full: keys -> hostname"       'before ssh-keygen "hostnamectl set-hostname pilnk-7f3a"'
ok "full: hostname -> nm-online"  'before "hostnamectl set-hostname pilnk-7f3a" nm-online'
ok "full: nm-online -> update"    'before nm-online "update-flag"'
ok "full: update -> hw-setup"     'before update-flag hw-env'
ok "full: hw-setup -> enable readsb"       'before hw-env "systemctl enable readsb"'
ok "full: enable readsb -> selfheal"       'before "systemctl enable readsb" bootstrap-selfheal'
ok "full: selfheal -> enable pilnk -> no-block start"  'before bootstrap-selfheal "systemctl enable pilnk" && before "systemctl enable pilnk" "systemctl --no-block start pilnk"'
ok "full: update runs with NO_RESTART=1"   'called "update-flag=1"'
ok "full: update runs as pilnk user"       'called "runuser -u pilnk --"'
ok "full: hw-setup gets PILNK_ROOT + non-interactive" 'called "hw-env root=$ROOT ni=1"'
ok "full: hw-setup called with --apply"    'called "pilnk-hw-setup --apply"'
ok "full: five markers"           '[ "$(markers)" = "identity,network,receiver,started,update" ]'
ok "full: hosts updated"          'grep -q "^127.0.1.1[[:space:]]pilnk-7f3a" "$ROOT/etc/hosts" && ! grep -q raspberrypi "$ROOT/etc/hosts"'
ok "full: localhost line kept"    'grep -q "^127.0.0.1[[:space:]]localhost" "$ROOT/etc/hosts"'
ok "full: no .no_receiver"        '[ ! -e "$PD/.no_receiver" ]'
ok "full: logged"                 '[ -s "$LOG" ]'
ok "full: disables itself"        'called "systemctl disable pilnk-firstboot"'
ok "full: never starts hotspot"   '! grep -qiE "nmcli|hotspot|wifi-setup" "$CALLS"'
ok "full: never restarts pilnk"   '! grep -qE "restart pilnk" "$CALLS"'

# --- grow needs a reboot: sub-marker before reboot, no second grow ---
setup; echo 0 > "$ROOT/.grow_rc"; run
ok "grow: reboot called"          'called reboot'
ok "grow: grown marker written BEFORE reboot" '[ "$(cat "$ROOT/.rb" 2>/dev/null)" = grown-first ]'
ok "grow: identity not marked yet" '[ ! -e "$S/identity" ]'
ok "grow: stops after reboot (no keys/network)" '! called ssh-keygen && ! called nm-online'
run
ok "grow second run: no growpart" '! called growpart'
ok "grow second run: no reboot"   '! called reboot'
ok "grow second run: identity done" '[ -e "$S/identity" ] && called ssh-keygen'
ok "grow second run: boots on to start" 'called "systemctl --no-block start pilnk"'

# --- grow: nothing to do (growpart NOCHANGE) continues without reboot ---
setup; echo 1 > "$ROOT/.grow_rc"; run
ok "nochange: no reboot, carries on" '! called reboot && [ -e "$S/identity" ] && called resize2fs'
setup; echo 2 > "$ROOT/.grow_rc"; run
ok "growpart failure: logged, not fatal, no reboot" '! called reboot && [ -e "$S/identity" ] && [ -e "$S/started" ]'
setup; fakebin findmnt 'true'; run
ok "no root device found: skip grow, not fatal" '! called growpart && [ -e "$S/identity" ]'

# --- resume ---
setup; mkdir -p "$S"; touch "$S/identity" "$S/network"; run
ok "resume: no identity/network work" '! called growpart && ! called ssh-keygen && ! called hostnamectl && ! called nm-online'
ok "resume: update, receiver, start run" 'called update-flag && called hw-env && called "systemctl --no-block start pilnk"'
ok "resume: all markers"          '[ "$(markers)" = "identity,network,receiver,started,update" ]'

# --- no network: keep polling, never start the hotspot ---
setup; echo 3 > "$ROOT/.nm_fail"; run
ok "no network: polled until online" '[ "$(grep -c "^nm-online" "$CALLS")" -ge 4 ]'
ok "no network: first try honours PILNK_NET_WAIT" 'grep -qE "^nm-online .*-t 1( |$)" "$CALLS"'
ok "no network: no hotspot started by first boot" '! grep -qiE "nmcli|hotspot|wifi-setup" "$CALLS"'
ok "no network: network marker written after success" '[ -e "$S/network" ] && [ "$(cat "$ROOT/.nm_fail")" = 0 ]'

# --- receiver: exit 2 = no dongle ---
setup; echo 2 > "$ROOT/.hw_rc"; run
ok "no rx: exit 0"                '[ $RC -eq 0 ]'
ok "no rx: pilnk still enabled+started" 'called "systemctl --no-block start pilnk"'
ok "no rx: receiver marker NOT written" '[ ! -e "$S/receiver" ] && [ -e "$S/started" ]'
ok "no rx: .no_receiver exists"   '[ -e "$PD/.no_receiver" ]'
ok "no rx: readsb NOT enabled"    '! called "systemctl enable readsb"'
ok "no rx: no self-heal"          '! called bootstrap-selfheal'
ok "no rx: firstboot stays enabled" '! called "systemctl disable pilnk-firstboot"'
run
ok "no rx retry: hw-setup only"   'called hw-env && ! called update-flag && ! called growpart && ! called nm-online && ! called "start pilnk"'
ok "no rx retry: still no marker, flag kept" '[ ! -e "$S/receiver" ] && [ -e "$PD/.no_receiver" ]'
echo 0 > "$ROOT/.hw_rc"; run
ok "dongle plugged: receiver marker"  '[ -e "$S/receiver" ]'
ok "dongle plugged: flag removed"     '[ ! -e "$PD/.no_receiver" ]'
ok "dongle plugged: readsb then selfheal" 'before "systemctl enable readsb" bootstrap-selfheal'
ok "dongle plugged: pilnk not restarted" '! called "start pilnk"'
ok "dongle plugged: now disables itself" 'called "systemctl disable pilnk-firstboot"'

# --- hw-setup error (exit 1): not marked, PiLNK still starts, flag untouched ---
setup; echo 1 > "$ROOT/.hw_rc"; run
ok "hw error: not marked, still starts" '[ ! -e "$S/receiver" ] && [ -e "$S/started" ] && ! called "enable readsb"'

# --- test builds skip the update ---
setup; mkdir "$PD/.git"; echo sterile-image > "$ROOT/.branch"; run
ok "test build: update.sh not run" '! called update-flag'
ok "test build: logged"           'grep -q "test build: update skipped" "$LOG"'
ok "test build: update marker still written" '[ -e "$S/update" ]'
ok "test build: rest proceeds"    'called hw-env && called "--no-block start pilnk"'
setup; mkdir "$PD/.git"; echo main > "$ROOT/.branch"; run
ok "main checkout: update runs"   'called update-flag'
setup; mkdir "$PD/.git"; echo HEAD > "$ROOT/.branch"; run
ok "detached HEAD counts as test build" '! called update-flag'

# --- update failure: retried, then carries on ---
setup; echo 1 > "$ROOT/.upd_rc"; run
ok "update fails: retried 3 times" '[ "$(grep -c "^update-flag\|^update " "$CALLS")" -ge 3 ] && [ "$(grep -c "^update-flag" "$CALLS")" -eq 3 ]'
ok "update fails: still starts PiLNK" '[ -e "$S/started" ] && called "--no-block start pilnk"'

# --- hostname suffix ---
setup; sed -i 's/10000000abcd7f3a/10000000ABCD7F3A/' "$ROOT/proc/cpuinfo"; run
ok "hostname: lowercased"         'called "set-hostname pilnk-7f3a"'
setup; printf 'processor\t: 0\n' > "$ROOT/proc/cpuinfo"; echo 0123456789ABCDEF0123456789ABCDEF > "$ROOT/etc/machine-id"; run
ok "hostname: machine-id fallback" 'called "set-hostname pilnk-cdef"'
ok "hostname: fallback logged"    'grep -qi "machine-id" "$LOG"'

# --- C-1: nothing ordered after firstboot may be started synchronously ---
# firstboot is Before=pilnk.service; a blocking start from inside it deadlocks.
nonblock_calls() {  # every systemctl start/restart/--now call lacks --no-block?
  ! grep -E '^systemctl ' "$CALLS" | grep -E ' (start|restart|try-restart|reload-or-restart) | --now' | grep -qv -- '--no-block'
}
setup; run
ok "C-1: start stage = enable, then --no-block start" 'before "systemctl enable pilnk" "systemctl --no-block start pilnk"'
ok "C-1: no blocking start/restart/--now from firstboot" 'nonblock_calls'
ok "C-1: no --now anywhere in firstboot calls" '! grep -E "^systemctl " "$CALLS" | grep -q -- "--now"'
ok "C-1: selfheal told not to block" 'called "selfheal-env noblock=1"'
# selfheal itself: the helper behind every start in bootstrap-selfheal.sh
SH="$REPO/bootstrap-selfheal.sh"
ok "selfheal: no bare 'enable --now' left outside the helper" '[ "$(grep -c "systemctl enable --now" "$SH")" -le 1 ]'
selfheal_helper() {  # source the real helper with priv = passthrough, fake systemctl logging
  eval "$(sed -n '/^svc_enable_start()/,/^}/p' "$SH")"
  priv() { "$@"; }
  svc_enable_start pilnk-dashboard-watchdog.service
}
setup; ( unset PILNK_SELFHEAL_NO_BLOCK; : > "$CALLS"; selfheal_helper )
ok "selfheal default: unchanged 'enable --now X'" 'called "systemctl enable --now pilnk-dashboard-watchdog.service" && ! called "--no-block"'
setup; ( export PILNK_SELFHEAL_NO_BLOCK=1; : > "$CALLS"; selfheal_helper )
ok "selfheal NO_BLOCK: enable then --no-block start, no --now" 'before "systemctl enable pilnk-dashboard-watchdog.service" "systemctl --no-block start pilnk-dashboard-watchdog.service" && ! called "--now"'
ok "selfheal NO_BLOCK: every start is --no-block" 'nonblock_calls'

# --- I-1: online = nm-online ok AND a default route ---
setup; echo 3 > "$ROOT/.noroute"; run
ok "no default route: keeps polling" '[ "$(grep -c "^ip " "$CALLS")" -ge 4 ]'
ok "no default route: asks for the IPv4 default route" 'called "ip -4 route show default"'
ok "no default route: no network marker until a route exists" '[ ! -e "$ROOT/.early" ] && [ -e "$S/network" ]'
ok "no default route: nm-online alone wasn't enough" '[ "$(grep -c "^nm-online" "$CALLS")" -ge 4 ]'

# --- I-2: branch probe works despite git's ownership check; unknown = skip ---
setup; mkdir "$PD/.git"; echo sterile-image > "$ROOT/.branch"; run
ok "branch probe runs as pilnk (runuser ... git -C)" 'called "runuser -u pilnk -- git -C $PD rev-parse --abbrev-ref HEAD"'
setup; mkdir "$PD/.git"; run   # .git present, git cannot say which branch
ok "branch unknown: update skipped" '! called update-flag'
ok "branch unknown: logged" 'grep -q "test build: update skipped (branch unknown)" "$LOG"'
ok "branch unknown: update marker written, rest proceeds" '[ -e "$S/update" ] && called hw-env'

# --- I-3: avahi follows the new hostname ---
setup; run
ok "avahi: no-block try-restart after hostname" 'before "set-hostname pilnk-7f3a" "systemctl --no-block try-restart avahi-daemon"'

# --- M-1: readsb enable failure keeps receiver unmarked ---
setup; fakebin systemctl 'case "$*" in "enable readsb") exit 1;; esac'; run
ok "readsb enable fails: receiver not marked" '[ ! -e "$S/receiver" ]'
ok "readsb enable fails: PiLNK still starts" '[ -e "$S/started" ] && called "--no-block start pilnk"'
ok "readsb enable fails: firstboot stays enabled" '! called "systemctl disable pilnk-firstboot"'

# --- M-2: hostnamectl failure/missing -> write /etc/hostname directly ---
setup; fakebin hostnamectl 'exit 127'; run
ok "hostnamectl fails: /etc/hostname written" '[ "$(cat "$ROOT/etc/hostname")" = pilnk-7f3a ]'
ok "hostnamectl fails: hostname command run" 'called "hostname pilnk-7f3a"'
ok "hostnamectl fails: identity still completes" '[ -e "$S/identity" ] && grep -q "^127.0.1.1[[:space:]]pilnk-7f3a" "$ROOT/etc/hosts"'
setup; rm "$ROOT/etc/hosts"; mkdir "$ROOT/etc/hosts"; run
ok "hosts edit fails: identity not marked" '[ ! -e "$S/identity" ] && grep -q "hosts" "$LOG"'

# --- M-3: /dev/root is resolved, or the grow is skipped ---
setup; fakebin findmnt 'echo /dev/root'; echo 'console=serial0 root=PARTUUID=abcd1234-02 rootfstype=ext4' > "$ROOT/proc/cmdline"
fakebin blkid 'case "$*" in *PARTUUID=abcd1234-02*) echo /dev/mmcblk0p2;; esac'; run
ok "/dev/root: resolved via cmdline PARTUUID + blkid" 'called "growpart /dev/mmcblk0 2"'
setup; fakebin findmnt 'echo /dev/root'; echo 'root=PARTUUID=nope' > "$ROOT/proc/cmdline"; fakebin blkid 'true'; run
ok "/dev/root unresolvable: no growpart" '! called growpart'
ok "/dev/root unresolvable: logged, identity not blocked" 'grep -q "grow:" "$LOG" && [ -e "$S/identity" ] && [ -e "$S/started" ]'

# --- M-4: reboot fails after growpart -> online resize2fs, carry on ---
setup; echo 0 > "$ROOT/.grow_rc"; fakebin reboot 'exit 1'; run
ok "reboot fails: resize2fs runs online" 'called "resize2fs /dev/mmcblk0p2"'
ok "reboot fails: first boot continues" '[ -e "$S/identity" ] && [ -e "$S/started" ] && [ -e "$S/grown" ]'
ok "reboot fails: logged" 'grep -q "reboot failed" "$LOG"'

# --- M-7: selfheal logs into the firstboot log ---
setup; run
ok "selfheal log = firstboot log" 'called "selfheal-env noblock=1 log=$LOG"'

# --- unit file ---
ok "unit: Type=oneshot"           'grep -qx "Type=oneshot" "$UNIT"'
ok "unit: Before=pilnk.service"   'grep -qx "Before=pilnk.service" "$UNIT"'
ok "unit: After=NetworkManager.service" 'grep -qx "After=NetworkManager.service" "$UNIT"'
ok "unit: WantedBy"               'grep -qx "WantedBy=multi-user.target" "$UNIT"'
ok "unit: TimeoutStartSec=infinity" 'grep -qx "TimeoutStartSec=infinity" "$UNIT"'
ok "unit: ExecStart installed path" 'grep -qx "ExecStart=/usr/local/sbin/pilnk-firstboot" "$UNIT"'
ok "script: bash -n"              'bash -n "$FB"'

# ======== final review fixes ========
# I1: the unit has no User=, so systemd gives first boot no $HOME; hw-setup's
# radio config (pilnkradio-install.sh, set -u) needs HOME and its state file
setup; fakescript "$PD/pilnk-hw-setup.sh" 'echo "hw-home HOME=${HOME:-UNSET} STATE=${PILNKRADIO_STATE_FILE:-UNSET}" >> "$CALLS"; exit 0'
: > "$CALLS"; env -u HOME PILNK_ROOT="$ROOT" PILNK_DIR="$PD" PILNK_NET_WAIT=1 PILNK_POLL_SLEEP=0 bash "$FB"; RC=$?
ok "I1: HOME unset -> hw-setup gets HOME = the clone's parent (/home/pilnk)" 'called "hw-home HOME=$ROOT/home/pilnk "'
ok "I1: hw-setup gets PILNKRADIO_STATE_FILE in the clone" 'called "STATE=$PD/audio_build_state.json"'

# the real hw-setup + a radio installer that, like pilnkradio-install.sh, dies on an unset HOME
real_hw() {
  cp "$REPO/pilnk-hw-setup.sh" "$PD/pilnk-hw-setup.sh"
  printf '#!/bin/bash\nset -u\necho "radio HOME=$HOME STATE=$PILNKRADIO_STATE_FILE SERIAL=$PILNKRADIO_SERIAL" >> "$CALLS"\n' > "$PD/pilnkradio-install.sh"
  # systemctl: is-active reads $ROOT/.active; stop removes the unit from it
  # is-enabled reads $ROOT/.enabled, which enable appends to (the image ships readsb disabled)
  fakebin systemctl 'case "$1" in is-active) if grep -qx "${@: -1}" "$ROOT/.active" 2>/dev/null; then echo active; else echo inactive; exit 3; fi;;
    is-enabled) grep -qx "${@: -1}" "$ROOT/.enabled" 2>/dev/null;;
    enable) echo "${@: -1}" >> "$ROOT/.enabled";;
    stop) sed -i "/^${2%.service}\$/d" "$ROOT/.active";; *) true;; esac'
  mkdir -p "$S"; touch "$S/identity" "$S/network" "$S/update"
}
setup; real_hw; rtl 00000002 00001090
echo 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto --ppm 0"' > "$ROOT/etc/default/readsb"
: > "$CALLS"; env -u HOME PILNK_ROOT="$ROOT" PILNK_DIR="$PD" PILNK_NET_WAIT=1 PILNK_POLL_SLEEP=0 bash "$FB"; RC=$?
ok "I1: real hw-setup, HOME unset -> radio config runs with HOME=/home/pilnk and the VHF serial" \
   'called "radio HOME=$ROOT/home/pilnk STATE=$PD/audio_build_state.json SERIAL=00000002"'
ok "I1: receiver bound" '[ -e "$S/receiver" ]'

# I2: no receiver at first boot -> pilnk enabled; dongles plugged in; at the next
# boot pilnk's Wants= has started an unpinned readsb that already writes aircraft.json
setup; real_hw
echo 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto --ppm 0"' > "$ROOT/etc/default/readsb"
run                                            # boot 1: no dongle
ok "I2 boot 1: no receiver -> .no_receiver, pilnk started" '[ -e "$PD/.no_receiver" ] && [ -e "$S/started" ] && [ ! -e "$S/receiver" ]'
rtl 00000003 00001090; active readsb; aircraft readsb   # boot 2: dongles in, readsb up unpinned
run
ok "I2 boot 2: unpinned running readsb is stopped before hw-setup" 'before "systemctl stop readsb" "systemctl enable readsb"'
ok "I2 boot 2: ADS-B pinned to the 1090 dongle (not consumed)" \
   'grep -q -- "--device 00001090 --device-type rtlsdr" "$ROOT/etc/default/readsb" && [ "$(grep -oE -- "--device[ =]" "$ROOT/etc/default/readsb" | wc -l)" -eq 1 ]'
ok "I2 boot 2: VHF is the other dongle, never the ADS-B one" \
   'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"vhf_serial\"]==\"00000003\"" "$PD/config.json" && called "SERIAL=00000003"'
ok "I2 boot 2: receiver marker, flag removed" '[ -e "$S/receiver" ] && [ ! -e "$PD/.no_receiver" ]'
# a readsb that is already pinned (or net-only) is left alone
setup; real_hw; rtl 00001090; active readsb; aircraft readsb
echo 'RECEIVER_OPTIONS="--device 00001090 --gain auto"' > "$ROOT/etc/default/readsb"; run
ok "I2: a pinned running readsb is not stopped" '! called "systemctl stop readsb"'
setup; real_hw; airspy; active readsb; aircraft readsb
echo 'RECEIVER_OPTIONS="--net-only"' > "$ROOT/etc/default/readsb"
fakebin curl 'for a; do o=$a; done; printf "#!/bin/bash\nexit 0\n" > "$o"'; run
ok "I2: a net-only readsb (holds no dongle) is not stopped" '! called "systemctl stop readsb"'

# I3: Airspy -> airspy-conf runs in the receiver stage (after the network stage)
airspy_case() { # curl-body airspy-conf-rc
  setup; real_hw; airspy
  echo 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto --ppm 0"' > "$ROOT/etc/default/readsb"
  fakebin curl "for a; do o=\$a; done; $1"
  AIRSPY_RC=$2
  # like wiedehopf's: readsb is only made --net-only when it is enabled
  printf '#!/bin/bash\necho "airspy-conf ran" >> "$CALLS"\nif systemctl is-enabled readsb; then echo %s > "$PILNK_ROOT/etc/default/readsb"; fi\nexit %s\n' "'RECEIVER_OPTIONS=\"--net-only\"'" "$AIRSPY_RC" > "$ROOT/.airspy-conf"
  run
}
airspy_case 'cp "$ROOT/.airspy-conf" "$o"' 0
ok "I3: Airspy -> airspy-conf downloaded (curl --retry) and run, then readsb enabled" \
   'called "airspy-conf ran" && grep -qE "^curl .*--retry .*airspy-conf" "$CALLS" && before "systemctl enable readsb" "airspy-conf ran"'
ok "N2: airspy-conf made readsb net-only; logged only after the check" 'grep -q -- "--net-only" "$ROOT/etc/default/readsb" && grep -q "readsb is now net-only" "$LOG"'
ok "I3: Airspy bound -> receiver marker, no banner" '[ -e "$S/receiver" ] && [ ! -e "$PD/.no_receiver" ]'
airspy_case 'cp "$ROOT/.airspy-conf" "$o"' 1
ok "I3: airspy-conf fails -> no receiver marker, logged, retried next boot" \
   '[ ! -e "$S/receiver" ] && grep -q "airspy-conf failed" "$LOG" && [ -e "$S/started" ]'
# N2: airspy-conf exits 0 but readsb is still not net-only -> logged, retried next boot
airspy_case 'printf "#!/bin/bash\nexit 0\n" > "$o"' 0
ok "N2: readsb not net-only after airspy-conf -> no marker, logged, no false claim" \
   '[ ! -e "$S/receiver" ] && grep -q "not net-only" "$LOG" && ! grep -q "readsb is now net-only" "$LOG" && [ -e "$S/started" ]'
airspy_case 'exit 22' 0
ok "I3: airspy-conf download fails -> nothing run, no marker, logged" \
   '! called "airspy-conf ran" && [ ! -e "$S/receiver" ] && grep -q "could not download airspy-conf" "$LOG"'
airspy_case ': > "$o"' 0
ok "I3: empty download -> nothing run, no marker" '! called "airspy-conf ran" && [ ! -e "$S/receiver" ]'
setup; real_hw; rtl 00001090; echo 'RECEIVER_OPTIONS=""' > "$ROOT/etc/default/readsb"; fakebin curl 'exit 1'; run
ok "I3: RTL only -> airspy-conf never fetched" '! grep -q "^curl " "$CALLS" && [ -e "$S/receiver" ]'

# N3: an unpinned readsb waiting for its auto-restart (activating) is stopped too
setup; real_hw; rtl 00000004 00001090
echo 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto --ppm 0"' > "$ROOT/etc/default/readsb"
fakebin systemctl 'case "$1" in is-active) if [ "${2:-}" = "--quiet" ]; then exit 3; else echo activating; exit 3; fi;; is-enabled) exit 1;; *) true;; esac'
run
ok "N3: unpinned readsb in 'activating' is stopped before hw-setup" 'before "systemctl stop readsb" "systemctl enable readsb"'
setup; real_hw; rtl 00001090
echo 'RECEIVER_OPTIONS="--device 00001090 --gain auto"' > "$ROOT/etc/default/readsb"
fakebin systemctl 'case "$1" in is-active) echo activating; exit 3;; *) true;; esac'
run
ok "N3: a pinned readsb in 'activating' is not stopped" '! called "systemctl stop readsb"'
