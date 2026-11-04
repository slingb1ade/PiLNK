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
  fakebin runuser 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"'
  fakebin git 'if [ -e "$ROOT/.branch" ]; then cat "$ROOT/.branch"; else exit 128; fi'
  fakebin sleep 'true'
  fakescript "$PD/update.sh" 'echo "update-flag=$PILNK_UPDATE_NO_RESTART" >> "$CALLS"; exit $(cat "$ROOT/.upd_rc" 2>/dev/null || echo 0)'
  fakescript "$PD/pilnk-hw-setup.sh" 'echo "hw-env root=$PILNK_ROOT ni=$PILNK_HW_NONINTERACTIVE" >> "$CALLS"; exit $(cat "$ROOT/.hw_rc" 2>/dev/null || echo 0)'
  fakescript "$PD/bootstrap-selfheal.sh" 'true'
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
ok "full: selfheal -> enable --now pilnk"  'before bootstrap-selfheal "systemctl enable --now pilnk"'
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
ok "full: never restarts pilnk"   '! grep -qE "(restart|start) pilnk" "$CALLS"'

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
ok "grow second run: boots on to start" 'called "systemctl enable --now pilnk"'

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
ok "resume: update, receiver, start run" 'called update-flag && called hw-env && called "systemctl enable --now pilnk"'
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
ok "no rx: pilnk still enabled+started" 'called "systemctl enable --now pilnk"'
ok "no rx: receiver marker NOT written" '[ ! -e "$S/receiver" ] && [ -e "$S/started" ]'
ok "no rx: .no_receiver exists"   '[ -e "$PD/.no_receiver" ]'
ok "no rx: readsb NOT enabled"    '! called "systemctl enable readsb"'
ok "no rx: no self-heal"          '! called bootstrap-selfheal'
ok "no rx: firstboot stays enabled" '! called "systemctl disable pilnk-firstboot"'
run
ok "no rx retry: hw-setup only"   'called hw-env && ! called update-flag && ! called growpart && ! called nm-online && ! called "enable --now pilnk"'
ok "no rx retry: still no marker, flag kept" '[ ! -e "$S/receiver" ] && [ -e "$PD/.no_receiver" ]'
echo 0 > "$ROOT/.hw_rc"; run
ok "dongle plugged: receiver marker"  '[ -e "$S/receiver" ]'
ok "dongle plugged: flag removed"     '[ ! -e "$PD/.no_receiver" ]'
ok "dongle plugged: readsb then selfheal" 'before "systemctl enable readsb" bootstrap-selfheal'
ok "dongle plugged: pilnk not restarted" '! called "enable --now pilnk"'
ok "dongle plugged: now disables itself" 'called "systemctl disable pilnk-firstboot"'

# --- hw-setup error (exit 1): not marked, PiLNK still starts, flag untouched ---
setup; echo 1 > "$ROOT/.hw_rc"; run
ok "hw error: not marked, still starts" '[ ! -e "$S/receiver" ] && [ -e "$S/started" ] && ! called "enable readsb"'

# --- test builds skip the update ---
setup; echo sterile-image > "$ROOT/.branch"; run
ok "test build: update.sh not run" '! called update-flag'
ok "test build: logged"           'grep -q "test build: update skipped" "$LOG"'
ok "test build: update marker still written" '[ -e "$S/update" ]'
ok "test build: rest proceeds"    'called hw-env && called "enable --now pilnk"'
setup; echo main > "$ROOT/.branch"; run
ok "main checkout: update runs"   'called update-flag'
setup; echo HEAD > "$ROOT/.branch"; run
ok "detached HEAD counts as test build" '! called update-flag'

# --- update failure: retried, then carries on ---
setup; echo 1 > "$ROOT/.upd_rc"; run
ok "update fails: retried 3 times" '[ "$(grep -c "^update-flag\|^update " "$CALLS")" -ge 3 ] && [ "$(grep -c "^update-flag" "$CALLS")" -eq 3 ]'
ok "update fails: still starts PiLNK" '[ -e "$S/started" ] && called "enable --now pilnk"'

# --- hostname suffix ---
setup; sed -i 's/10000000abcd7f3a/10000000ABCD7F3A/' "$ROOT/proc/cpuinfo"; run
ok "hostname: lowercased"         'called "set-hostname pilnk-7f3a"'
setup; printf 'processor\t: 0\n' > "$ROOT/proc/cpuinfo"; echo 0123456789ABCDEF0123456789ABCDEF > "$ROOT/etc/machine-id"; run
ok "hostname: machine-id fallback" 'called "set-hostname pilnk-cdef"'
ok "hostname: fallback logged"    'grep -qi "machine-id" "$LOG"'

# --- unit file ---
ok "unit: Type=oneshot"           'grep -qx "Type=oneshot" "$UNIT"'
ok "unit: Before=pilnk.service"   'grep -qx "Before=pilnk.service" "$UNIT"'
ok "unit: After=NetworkManager.service" 'grep -qx "After=NetworkManager.service" "$UNIT"'
ok "unit: WantedBy"               'grep -qx "WantedBy=multi-user.target" "$UNIT"'
ok "unit: TimeoutStartSec=infinity" 'grep -qx "TimeoutStartSec=infinity" "$UNIT"'
ok "unit: ExecStart installed path" 'grep -qx "ExecStart=/usr/local/sbin/pilnk-firstboot" "$UNIT"'
ok "script: bash -n"              'bash -n "$FB"'
