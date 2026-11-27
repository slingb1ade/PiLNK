#!/bin/bash
# shellcheck disable=SC2034,SC2016
# rootsync step 8: root's units rendered from the signed copy (Part B spec 5.2
# step 8, 5.3, 5.5; plan Task 3). report = rootsync's own units only; switch =
# every root unit runs from /var/lib/pilnk/current, and there is no way back.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"
. "$(dirname "$0")/rootsync-testlib.sh"

# The files a real release carries for the switch, in the fake GitHub.
add_rootside() {  # POLICY
  ( cd "$ROOT/up" && mkdir -p rootside image/firstboot \
    && for f in pilnk-sdr-recover.service pilnk-dashboard-watchdog.service pilnk-audio-build.service \
                pilnk-rootsync.service pilnk-rootsync.timer sudoers.template rootsync-run; do
         cp "$REPO/rootside/$f" rootside/ || exit 1; done \
    && cp "$REPO/sdr-recover.sh" "$REPO/pilnk-dashboard-watchdog.sh" "$REPO/pilnkradio-install.sh" \
          "$REPO/99-pilnk-sdr.rules" "$REPO/pilnk-sdr-recover.timer" "$REPO/pilnk-radio-watchdog.sh" \
          "$REPO/rootsync.sh" . \
    && cp "$REPO/image/firstboot/pilnk-firstboot.sh" image/firstboot/ \
    && echo "$1" > root_policy && G add -A && G commit -qm "rootside $1" )
}
# A node as it is today: units that run the user's tree.
old_units() {
  U="$P/etc/systemd/system"
  mkdir -p "$U" "$P/etc/udev/rules.d" "$P/etc/sudoers.d"
  for u in pilnk-sdr-recover pilnk-dashboard-watchdog pilnk-audio-build; do
    printf '[Service]\nExecStart=%s/pilnk/%s.sh\n' "$HOME" "$u" > "$U/$u.service"
  done
  echo 'old rule' > "$P/etc/udev/rules.d/99-pilnk-sdr.rules"
  echo 'old sudoers' > "$P/etc/sudoers.d/pilnk-ota"
}
snap() { ( cd "$P/etc" && find . -type f ! -name 'pilnk-rootsync.*' -exec sha256sum {} + | sort ); }
fakes() {
  fakebin visudo 'exit 0'
  fakebin udevadm 'exit 0'
}
runs_current() { grep -Eq '^ExecStart=(/bin/bash )?/var/lib/pilnk/current/' "$P/etc/systemd/system/$1"; }

# --- report: rootsync installs its own timer, nothing else changes
setup; fakes; mksvc auto; old_units; add_rootside report; rel 1.0.1; B=$(snap); RS; A=$(snap)
ok rc_report_mode_units_untouched '[ "$RC" = 0 ] && [ "$B" = "$A" ] && [ -f "$U/pilnk-rootsync.service" ] && [ -f "$U/pilnk-rootsync.timer" ] && st_field switched = False && st_field src = user'
ok rc_report_mode_enables_rootsync_timer 'grep -q "systemctl enable --now pilnk-rootsync.timer" "$CALLS"'

# --- switch with a good copy: every root unit runs from current
setup; fakes; active pilnk-dashboard-watchdog.service; mksvc auto; old_units; add_rootside switch; rel 1.0.1; RS
ok rc_switch_renders_from_current '[ "$RC" = 0 ] && runs_current pilnk-sdr-recover.service && runs_current pilnk-dashboard-watchdog.service && runs_current pilnk-audio-build.service && st_field switched = True && st_field src = signed && st_field reason = ""'
AB="$U/pilnk-audio-build.service"
ok rc_audio_build_unit_shape 'grep -qx "ExecStartPre=-/usr/local/lib/pilnk/rootsync --quick" "$AB" && grep -qx "ExecStart=/bin/bash /var/lib/pilnk/current/pilnkradio-install.sh" "$AB" && grep -qx "Environment=HOME=/root PILNK_ROOT_BUILD=/var/lib/pilnk/build PILNKRADIO_STATE_FILE=/var/lib/pilnk/state/audio_build_state.json PILNK_APP_DIR=$HOME/pilnk PILNK_AUDIO_TASK_FILE=$HOME/pilnk/.audio_build_task" "$AB" && ! grep -q chown "$AB"'
ok rc_watchdog_gets_app_dir 'grep -qx "Environment=PILNK_APP_DIR=$HOME/pilnk" "$U/pilnk-dashboard-watchdog.service"'
ok rc_udev_and_timer_from_current 'cmp -s "$P/etc/udev/rules.d/99-pilnk-sdr.rules" "$REPO/99-pilnk-sdr.rules" && cmp -s "$U/pilnk-sdr-recover.timer" "$REPO/pilnk-sdr-recover.timer"'
ok rc_sudoers_rendered 'grep -q "^$(id -un) ALL=(root) NOPASSWD:" "$P/etc/sudoers.d/pilnk-ota" && grep -q "start pilnk-rootsync" "$P/etc/sudoers.d/pilnk-ota" && grep -q "visudo -c -f" "$CALLS" && [ "$(stat -c %a "$P/etc/sudoers.d/pilnk-ota")" = 440 ]'
ok rc_switch_reloads_and_restarts 'grep -q "systemctl daemon-reload" "$CALLS" && grep -q "udevadm control --reload-rules" "$CALLS" && grep -q "systemctl --no-block try-restart pilnk-dashboard-watchdog.service" "$CALLS"'
ok rc_no_user_paths_in_root_units '! sed -E "s#PILNK_APP_DIR=[^ ]*##g; s#PILNK_AUDIO_TASK_FILE=[^ ]*##g" "$U"/pilnk-sdr-recover.service "$U"/pilnk-sdr-recover.timer "$U"/pilnk-dashboard-watchdog.service "$AB" "$U"/pilnk-rootsync.service "$U"/pilnk-rootsync.timer "$P/etc/udev/rules.d/99-pilnk-sdr.rules" | grep -qF "$HOME"'
: > "$CALLS"; RS
ok rc_daemon_reload_only_on_change '[ "$RC" = 0 ] && ! grep -q "daemon-reload" "$CALLS" && ! grep -q "udevadm control" "$CALLS" && ! grep -q "try-restart" "$CALLS"'

# --- switch needs a good copy: a release missing a template renders nothing
setup; fakes; mksvc auto; old_units; add_rootside switch
( cd "$ROOT/up" && G rm -q rootside/pilnk-audio-build.service && G commit -qm "no audio template" )
rel 1.0.1; B=$(snap); RS; A=$(snap)
ok rc_switch_needs_good_copy '[ "$B" = "$A" ] && [ ! -e "$U/pilnk-rootsync.timer" ] && st_field switched = False && st_field reason = nocopy'

# --- no usable PiLNK user: nothing written, error:nouser
for kind in missing empty space unknown; do
  setup; fakes; old_units; add_rootside switch
  case "$kind" in
    missing) : ;;
    empty)   mksvc auto; sed -i 's/^User=.*/User=/' "$U/pilnk.service" ;;
    space)   mksvc auto; sed -i 's/^User=.*/User=a b/' "$U/pilnk.service" ;;
    unknown) mksvc auto; sed -i 's/^User=.*/User=nosuchuser/' "$U/pilnk.service" ;;
  esac
  rel 1.0.1; B=$(snap); RS; A=$(snap)
  ok "rc_bad_user_renders_nothing ($kind)" '[ "$B" = "$A" ] && [ ! -e "$U/pilnk-rootsync.timer" ] && st_field switched = False && st_field reason = nouser'
done

# --- sudoers goes through visudo -c first; a failure changes nothing
setup; fakes; fakebin visudo 'exit 1'; mksvc auto; old_units; add_rootside switch; rel 1.0.1; B=$(snap); RS; A=$(snap)
ok rc_sudoers_through_visudo '[ "$(cat "$P/etc/sudoers.d/pilnk-ota")" = "old sudoers" ] && [ "$B" = "$A" ] && st_field reason = sudoers && st_field switched = False'

# --- PiLNK OS first boot: rewritten to run from current if it exists, never created
setup; fakes; mksvc auto; old_units; add_rootside switch
mkdir -p "$P/usr/local/sbin"; echo '# old firstboot' > "$P/usr/local/sbin/pilnk-firstboot"; rel 1.0.1; RS
FB="$P/usr/local/sbin/pilnk-firstboot"
ok rc_firstboot_rendered_if_present '[ "$RC" = 0 ] && grep -qxF "ROOT_CODE=\"\${PILNK_ROOT_CODE:-/var/lib/pilnk/current}\"" "$FB" && grep -qF "bash \"\$ROOT_CODE/pilnk-hw-setup.sh\"" "$FB" && grep -qF "bash \"\$ROOT_CODE/bootstrap-selfheal.sh\"" "$FB" && ! grep -qF "bash \"\$PILNK_DIR/pilnk-hw-setup.sh\"" "$FB" && bash -n "$FB" && [ "$(stat -c %a "$FB")" = 755 ]'
setup; fakes; mksvc auto; old_units; add_rootside switch; rel 1.0.1; RS
ok rc_firstboot_not_created '[ "$RC" = 0 ] && [ ! -e "$P/usr/local/sbin/pilnk-firstboot" ]'

# --- no way back: a later release that says report leaves the node switched
setup; fakes; mksvc auto; old_units; add_rootside switch; rel 1.0.1; RS
( cd "$ROOT/up" && echo report > root_policy && G commit -qam "back to report" ); rel 1.0.2
# something rewrites a unit to the user's tree again (an old self-heal, say)
printf '[Service]\nExecStart=%s/pilnk/sdr-recover.sh\n' "$HOME" > "$U/pilnk-sdr-recover.service"; RS
ok rc_no_way_back '[ "$RC" = 0 ] && st_field version = 1.0.2 && st_field switched = True && st_field src = signed && runs_current pilnk-sdr-recover.service && runs_current pilnk-audio-build.service'

# --- bootstrap-selfheal.sh keeps its hands off a switched node
selfheal() {  # run the real bootstrap-selfheal.sh as the user, sudo faked
  PILNK_ROOTSYNC_PREFIX="$P" PILNK_DIR="$HOME/pilnk" PILNK_SELFHEAL_LOG="$ROOT/selfheal.log" \
    bash "$REPO/bootstrap-selfheal.sh" < /dev/null > /dev/null 2>&1
}
sh_setup() {
  setup
  fakebin sudo 'case "$*" in *tee*) cat > /dev/null ;; esac; exit 0'
  for f in pilnk-sdr-recover.service pilnk-sdr-recover.timer 99-pilnk-sdr.rules sdr-recover.sh \
           pilnk-dashboard-watchdog.service pilnk-dashboard-watchdog.sh; do cp "$REPO/$f" "$HOME/pilnk/"; done
  mkdir -p "$RSB/state"
}
sh_setup; echo '{"switched": true, "src": "signed"}' > "$RSB/state/status.json"; selfheal
ok bs_selfheal_skips_when_switched '! grep -q "^sudo" "$CALLS" && grep -q "managed by rootsync" "$ROOT/selfheal.log"'
sh_setup; echo '{"switched": false, "src": "user"}' > "$RSB/state/status.json"; selfheal
ok bs_selfheal_unchanged_when_not 'grep -q "^sudo -n tee /etc/systemd/system/pilnk-sdr-recover.service" "$CALLS" && ! grep -q "managed by rootsync" "$ROOT/selfheal.log"'
sh_setup; selfheal
ok bs_selfheal_unchanged_without_status 'grep -q "^sudo -n tee /etc/systemd/system/pilnk-sdr-recover.service" "$CALLS"'
sh_setup; echo '{"switched": "yes"}' > "$RSB/state/status.json"; selfheal
ok bs_selfheal_only_exact_true 'grep -q "^sudo -n tee /etc/systemd/system/pilnk-sdr-recover.service" "$CALLS"'
