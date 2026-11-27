#!/bin/bash
# shellcheck disable=SC2034,SC2016
# Part B end to end (plan Task 8): a node as it is today takes S1 (root_policy
# report) through the sdr-recover route, then S2 (switch). After the switch,
# nothing the PiLNK user writes in their own tree runs as root. A node with no
# root route gets nothing, and a release signed by an unknown key changes nothing.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"
. "$(dirname "$0")/rootsync-testlib.sh"

ME=$(id -u)
U_FILES="sdr-recover.sh pilnk-dashboard-watchdog.sh pilnkradio-install.sh denoise-install.sh rootsync.sh release-check.sh rootside/rootsync-run"

# Everything a real release carries for root: every rootside/MANIFEST path, the
# rootside templates, the udev rule and recover timer, and root_policy.
release_files() {  # POLICY
  ( cd "$ROOT/up" || exit 1
    mkdir -p rootside image/firstboot
    cp "$REPO"/rootside/* rootside/ || exit 1
    grep -Ev '^[[:space:]]*(#|$)' "$REPO/rootside/MANIFEST" | while read -r f; do
      mkdir -p "$(dirname "$f")"; cp "$REPO/$f" "$f" || exit 1
    done
    cp "$REPO/99-pilnk-sdr.rules" "$REPO/pilnk-sdr-recover.timer" .
    echo "$1" > root_policy
    G add -A && G commit -qm "release files ($1)" )
}
policy() { ( cd "$ROOT/up" && echo "$1" > root_policy && G commit -qam "root_policy $1" ); }
# A node as it is today: root units that run the user's tree.
old_units() {
  U="$P/etc/systemd/system"
  mkdir -p "$U" "$P/etc/udev/rules.d" "$P/etc/sudoers.d"
  printf '[Service]\nExecStart=/bin/bash %s/pilnk/sdr-recover.sh\n' "$HOME" > "$U/pilnk-sdr-recover.service"
  printf '[Service]\nExecStart=/bin/bash %s/pilnk/pilnk-dashboard-watchdog.sh\n' "$HOME" > "$U/pilnk-dashboard-watchdog.service"
  printf '[Service]\nExecStart=/bin/bash %s/pilnk/pilnkradio-install.sh\n' "$HOME" > "$U/pilnk-audio-build.service"
  echo 'old rule' > "$P/etc/udev/rules.d/99-pilnk-sdr.rules"
  echo 'old sudoers' > "$P/etc/sudoers.d/pilnk-ota"
}
snap() { ( cd "$P/etc" && find . -type f ! -name 'pilnk-rootsync.*' -exec sha256sum {} + | sort ); }
SDR_ENV() { env PILNK_ROOTSYNC_PREFIX="$P" PILNK_ROOTSYNC_UID="$ME" PILNK_ROOTSYNC_PINNED_SIGNERS="$ROOT/pinned" \
  PILNK_SDR_LOG="$ROOT/sdr.log" PILNK_SDR_STAMP="$ROOT/sdr.stamp" PILNK_SDR_PAUSE="$ROOT/sdr.pause" \
  PILNK_READSB_DEFAULT="$ROOT/etc/default/readsb" PILNK_RADIO_CFG="$ROOT/none.json" PILNK_DATALINK_CFG="$ROOT/none.json" "$@"; }
# rootsync as its timer runs it: the installed rootsync-run, never the user's tree
RUN() {
  PILNK_ROOTSYNC_PREFIX="$P" PILNK_RELEASE_URL="file://$ROOT/vp.json" PILNK_ROOTSYNC_GIT_URL="$ROOT/up" \
  PILNK_ROOTSYNC_UID="$ME" PILNK_ROOTSYNC_LOCK_WAIT=1 bash "$LIB/rootsync-run" "$@" > "$ROOT/run.out" 2>&1; RC=$?
}
# the ping's root fields, as app.py builds them (root_status.ping_fields)
PING() {
  python3 -I -c 'import json, sys; sys.path.insert(0, sys.argv[1]); import root_status as r
print(json.dumps(r.ping_fields(r.load(sys.argv[2]), sys.argv[3], sys.argv[4]), sort_keys=True))' \
    "$REPO" "$P" "$HOME/pilnk" "$RSB/current"
}
# Run a rendered unit the way systemd would, with /var/lib/pilnk, /usr/local/lib/pilnk
# and /root mapped into the test root. Tools that would build or reach out are stubs.
run_unit() {  # UNIT
  local f="$P/etc/systemd/system/$1" envs cmd
  envs=$(sed -n 's/^Environment=//p' "$f" | sed "s#/var/lib/pilnk#$RSB#g; s#HOME=/root#HOME=$ROOT/roothome#" | tr '\n' ' ')
  [ -n "${envs// /}" ] || envs="PILNK_UNIT_RUN=1"
  cmd=$(sed -n 's/^ExecStart=//p' "$f" | sed "s#/var/lib/pilnk#$RSB#g; s#/usr/local/lib/pilnk#$LIB#g")
  mkdir -p "$ROOT/roothome" "$ROOT/.ubin"
  for c in sudo apt-get cmake make ldconfig ldd git chown journalctl udevadm nproc rtl_test; do
    printf '#!/bin/bash\necho "%s $*" >> "%s"\nexit 1\n' "$c" "$CALLS" > "$ROOT/.ubin/$c"; chmod +x "$ROOT/.ubin/$c"
  done
  ( eval "export $envs"; PATH="$ROOT/.ubin:$PATH" SDR_ENV timeout 10 $cmd ) < /dev/null >> "$ROOT/unit.out" 2>&1
  return 0
}
runs_current() { grep -Eq '^ExecStart=(/bin/bash )?/var/lib/pilnk/current/' "$P/etc/systemd/system/$1"; }
# a node as it is today, on S1: user tree = the release, no /var/lib/pilnk yet
chain_setup() {
  setup
  rm -rf "$RSB"
  signers_line aj > "$ROOT/pinned"
  fakebin visudo 'exit 0'; fakebin udevadm 'exit 0'
  mksvc auto; old_units
  release_files report; rel 1.0.1; node_pull
}
inject() {  # make every root-run file in the user's tree touch $ROOT/PWNED first
  local f
  for f in $U_FILES; do sed -i "2i touch \"$ROOT/PWNED\"" "$HOME/pilnk/$f"; done
}

# --- S1 (report): the sdr-recover route installs rootsync; rootsync syncs; nothing switches
chain_setup; B=$(snap)
SDR_ENV bash "$HOME/pilnk/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
RUN; A=$(snap); S1PING=$(PING)
ok chain_s1_reports_user '[ "$RC" = 0 ] && [ -x "$LIB/rootsync" ] && st_field src = user && st_field version = 1.0.1 && st_field switched = False && [ "$S1PING" = "{\"root\": \"user:1.0.1\", \"root_match\": \"y\"}" ] && [ "$B" = "$A" ]'

# --- S2 (switch): the same timer path renders every root unit from current
policy switch; rel 1.0.2; node_pull; RUN; S2PING=$(PING)
ok chain_s2_switches '[ "$RC" = 0 ] && st_field src = signed && st_field version = 1.0.2 && st_field switched = True && [ "$S2PING" = "{\"root\": \"signed:1.0.2\"}" ] && runs_current pilnk-sdr-recover.service && runs_current pilnk-dashboard-watchdog.service && runs_current pilnk-audio-build.service'

# --- red team: the PiLNK user edits every root-run file in their tree, and asks
# for an audio build (denoise). Control first: the edit does fire from the user tree.
inject; echo denoise > "$HOME/pilnk/.audio_build_task"
SDR_ENV bash "$HOME/pilnk/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
CONTROL=$([ -e "$ROOT/PWNED" ] && echo fired); rm -f "$ROOT/PWNED"
N0=$(wc -l < "$ROOT/sdr.log")
for u in pilnk-sdr-recover.service pilnk-dashboard-watchdog.service pilnk-audio-build.service; do run_unit "$u"; done
RUN; RUN --quick
# evidence each unit really ran root's copy: recover logged, watchdog started,
# and the denoise task was acted on from current/ (not the user's denoise-install.sh)
RAN=$([ "$(wc -l < "$ROOT/sdr.log")" -gt "$N0" ] && grep -q "pilnk-dashboard-watchdog up" "$ROOT/unit.out" \
      && grep -qF "[denoise]" "$ROOT/unit.out" && grep -qF "$RSB/current/denoise" "$ROOT/unit.out" && echo yes)
ok chain_redteam_user_edit_ignored '[ "$CONTROL" = fired ] && [ "$RAN" = yes ] && [ ! -e "$ROOT/PWNED" ] && st_field version = 1.0.2'

# --- a node with neither root route: updating the user tree installs nothing for root
chain_setup; policy switch; rel 1.0.2; node_pull; NPING=$(PING)
ok chain_no_route_reports_none '[ ! -e "$LIB" ] && [ ! -e "$RSB" ] && [ ! -e "$P/etc/systemd/system/pilnk-rootsync.timer" ] && [ "$NPING" = "{\"root\": \"none\"}" ] && grep -qx "ExecStart=/bin/bash $HOME/pilnk/sdr-recover.sh" "$P/etc/systemd/system/pilnk-sdr-recover.service"'

# --- S3 signed by an unknown key, after the switch: root keeps S2 and says why
chain_setup; SDR_ENV bash "$HOME/pilnk/sdr-recover.sh" > /dev/null 2>&1 < /dev/null; RUN
policy switch; rel 1.0.2; node_pull; RUN
CUR2=$(cur); B=$(snap); testkey evil; rel 1.0.3 evil; node_pull; RUN; A=$(snap); S3PING=$(PING)
ok chain_bad_s3_keeps_s2 '[ "$RC" = 1 ] && [ "$(cur)" = "$CUR2" ] && [ "$B" = "$A" ] && st_field version = 1.0.2 && st_field switched = True && st_field src = error && [[ "$S3PING" == "{\"root\": \"error:"* ]] && grep -qx "1.0.2" "$RSB/current/VERSION"'
