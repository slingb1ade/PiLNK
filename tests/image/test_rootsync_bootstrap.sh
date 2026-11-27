#!/bin/bash
# shellcheck disable=SC2034,SC2016
# One-time self-install through the root routes nodes already have (Part B
# spec 5.4, plan Task 5): sdr-recover.sh and pilnkradio-install.sh each carry a
# short block that, run as root with no rootsync yet, runs
# rootsync.sh --install-self from their own folder. No owner command.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"
. "$(dirname "$0")/rootsync-testlib.sh"

ME=$(id -u)
# a user tree as update.sh leaves it: the route scripts and rootsync beside them
mktree() {  # DIR
  mkdir -p "$1/rootside"
  cp "$REPO/rootsync.sh" "$REPO/release-check.sh" "$REPO/sdr-recover.sh" "$REPO/pilnkradio-install.sh" "$1/"
  cp "$REPO/rootside/rootsync-run" "$REPO/rootside/pilnk-rootsync.service" "$REPO/rootside/pilnk-rootsync.timer" "$1/rootside/"
}
IS() {  # rootsync --install-self as uid UID (default: me)
  PILNK_ROOTSYNC_PREFIX="$P" PILNK_ROOTSYNC_UID="${1:-$ME}" PILNK_ROOTSYNC_PINNED_SIGNERS="$ROOT/pinned" \
    bash "$ROOT/tree/rootsync.sh" --install-self > "$ROOT/is.out" 2>&1; RC=$?
}
bt_setup() {
  setup
  rm -rf "$RSB"          # a node before Part B: no /var/lib/pilnk at all
  signers_line aj > "$ROOT/pinned"
  mktree "$ROOT/tree"
}
paths() { for p in "$RSB" "$LIB" "$P/etc/systemd/system/pilnk-rootsync.service" "$P/etc/systemd/system/pilnk-rootsync.timer"; do [ -e "$p" ] && echo "$p"; done; }
mtimes() { find $(paths) -exec stat -c '%n %Y.%y' {} + 2>/dev/null | sort; }

# --- the pinned keys built into rootsync are exactly the repo's release_signers
ok bt_pinned_equals_repo_signers 'sed -n "/<<'"'"'SIGNERS'"'"'\$/,/^SIGNERS\$/p" "$REPO/rootsync.sh" | sed "1d;\$d" | cmp -s - "$REPO/release_signers"'

# --- --install-self: root's layout, pinned trust, rootsync installed, timer on
bt_setup; IS
ok bt_install_self_layout_safe '[ "$RC" = 0 ] && [ -x "$LIB/rootsync" ] && [ -x "$LIB/rootsync-run" ] && [ -f "$LIB/release-check.sh" ] && cmp -s "$RSB/trust/release_signers" "$ROOT/pinned" && [ -f "$P/etc/systemd/system/pilnk-rootsync.timer" ] && [ -z "$(find $(paths) \( ! -uid "$ME" -o \( ! -type l -perm /022 \) \) -print)" ]'
ok bt_install_self_enables_timer 'grep -q "systemctl enable pilnk-rootsync.timer" "$CALLS" && grep -q "systemctl --no-block start pilnk-rootsync" "$CALLS" && [ ! -e "$RSB/current" ]'
B=$(mtimes); : > "$CALLS"; sleep 1; IS; A=$(mtimes)
ok bt_install_self_idempotent '[ "$RC" = 0 ] && [ "$B" = "$A" ] && ! grep -q "systemctl enable" "$CALLS"'

# --- the timer is started now, not just enabled for the next boot (27 Nov hub
# trial: enabled, never started, so a first sync that failed was never retried)
# The start comes after rootsync is in place, so its first run can't miss it.
bt_setup
fakebin systemctl 'case "$1" in is-active) grep -qx "${@: -1}" "$ROOT/.active" 2>/dev/null; exit;; esac
[ "$*" = "--no-block start pilnk-rootsync.timer" ] && [ -x "'"$LIB"'/rootsync" ] && echo "TIMER-AFTER-INSTALL" >> "$CALLS"; exit 0'
IS
ok bt_install_self_starts_timer '[ "$RC" = 0 ] && grep -qx "systemctl --no-block start pilnk-rootsync.timer" "$CALLS" && grep -qx "TIMER-AFTER-INSTALL" "$CALLS"'
# already installed and enabled but the timer stopped: a re-run starts it
: > "$CALLS"; IS
ok bt_install_self_restarts_stopped_timer '[ "$RC" = 0 ] && grep -qx "systemctl --no-block start pilnk-rootsync.timer" "$CALLS" && ! grep -q "systemctl enable" "$CALLS"'
# timer running: nothing started
: > "$CALLS"; active pilnk-rootsync.timer; IS
ok bt_install_self_running_timer_untouched '[ "$RC" = 0 ] && ! grep -q "start pilnk-rootsync" "$CALLS"'
bt_setup; IS 0
ok bt_install_self_not_root_noop '[ "$RC" = 0 ] && [ -z "$(paths)" ]'
bt_setup; mkdir -p "$RSB/trust"; signers_line aj > "$RSB/trust/release_signers"; echo "# rotated later" >> "$RSB/trust/release_signers"; IS
ok bt_install_self_keeps_existing_trust '[ "$RC" = 0 ] && grep -q "rotated later" "$RSB/trust/release_signers"'

# --- the installed copy does the first sync itself, through rootsync-run (the timer's path)
bt_setup; IS; rel 1.0.1
PILNK_ROOTSYNC_PREFIX="$P" PILNK_RELEASE_URL="file://$ROOT/vp.json" PILNK_ROOTSYNC_GIT_URL="$ROOT/up" \
  PILNK_ROOTSYNC_UID="$ME" PILNK_ROOTSYNC_LOCK_WAIT=1 bash "$LIB/rootsync-run" > "$ROOT/run.out" 2>&1; RC=$?
ok bt_installed_copy_first_sync '[ "$RC" = 0 ] && st_field version = 1.0.1 && [ -f "$RSB/current/.complete" ]'

# --- first sync: the signers in the fetched release must equal root's pinned keys
setup; testkey other
( cd "$ROOT/up" && signers_line other > release_signers && signers_line aj >> release_signers && G commit -qam "other signers" )
rel 1.0.1; RS
ok bt_first_sync_signers_must_match '[ "$RC" = 1 ] && st_field reason = signers && [ ! -e "$RSB/current" ] && [ -z "$(ls "$RSB/signed")" ]'
setup; rel 1.0.1; RS
ok bt_first_sync_signers_equal_ok '[ "$RC" = 0 ] && st_field version = 1.0.1'

# --- the sdr-recover route: as root with no rootsync, it installs it, then recovers as usual
SDR_ENV() { env PILNK_ROOTSYNC_PREFIX="$P" PILNK_ROOTSYNC_UID="$ME" PILNK_ROOTSYNC_PINNED_SIGNERS="$ROOT/pinned" \
  PILNK_SDR_LOG="$ROOT/sdr.log" PILNK_SDR_STAMP="$ROOT/sdr.stamp" PILNK_SDR_PAUSE="$ROOT/sdr.pause" \
  PILNK_READSB_DEFAULT="$ROOT/etc/default/readsb" PILNK_RADIO_CFG="$ROOT/none.json" PILNK_DATALINK_CFG="$ROOT/none.json" "$@"; }
bt_setup; SDR_ENV bash "$ROOT/tree/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
ok bt_sdr_recover_route_installs '[ -x "$LIB/rootsync" ] && grep -q "systemctl enable pilnk-rootsync.timer" "$CALLS" && grep -q "nothing to recover" "$ROOT/sdr.log"'
# rootsync already there: the block does nothing
bt_setup; mkdir -p "$LIB"; printf '#!/bin/bash\nexit 0\n' > "$LIB/rootsync"; chmod 755 "$LIB/rootsync"
printf '#!/bin/bash\necho "INSTALL-SELF $*" >> "$CALLS"\n' > "$ROOT/tree/rootsync.sh"
SDR_ENV bash "$ROOT/tree/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
ok bt_routes_noop_when_installed '! grep -q "INSTALL-SELF" "$CALLS" && grep -q "nothing to recover" "$ROOT/sdr.log"'
# not root: the block does nothing
bt_setup; printf '#!/bin/bash\necho "INSTALL-SELF $*" >> "$CALLS"\n' > "$ROOT/tree/rootsync.sh"
SDR_ENV env PILNK_ROOTSYNC_UID=0 bash "$ROOT/tree/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
ok bt_routes_noop_when_not_root '! grep -q "INSTALL-SELF" "$CALLS"'

# --- the audio-build route: same, and the build still goes ahead
bt_setup; mkdir -p "$ROOT/tree/pilnkradio"; touch "$ROOT/tree/pilnkradio/main.cpp"
for c in sudo apt-get cmake make ldconfig ldd git chown journalctl udevadm curl; do fakebin "$c" 'exit 0'; done
fakebin nproc 'echo 2'
SDR_ENV env HOME="$ROOT/home" PILNKRADIO_STATE_FILE="$ROOT/abs.json" PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_SKIP_CONFIG=1 \
  bash "$ROOT/tree/pilnkradio-install.sh" > "$ROOT/radio.out" 2>&1 < /dev/null
ok bt_audio_route_installs '[ -x "$LIB/rootsync" ] && grep -q "systemctl enable pilnk-rootsync.timer" "$CALLS" && grep -q "1/8 locate pilnkradio source" "$ROOT/radio.out"'

# --- report mode: the node's existing root units are untouched by the bootstrap
bt_setup; U="$P/etc/systemd/system"; mkdir -p "$U"
for u in pilnk-sdr-recover pilnk-dashboard-watchdog pilnk-audio-build; do printf '[Service]\nExecStart=%s/pilnk/%s.sh\n' "$HOME" "$u" > "$U/$u.service"; done
B=$(cd "$U" && sha256sum pilnk-sdr-recover.service pilnk-dashboard-watchdog.service pilnk-audio-build.service)
SDR_ENV bash "$ROOT/tree/sdr-recover.sh" > /dev/null 2>&1 < /dev/null
A=$(cd "$U" && sha256sum pilnk-sdr-recover.service pilnk-dashboard-watchdog.service pilnk-audio-build.service)
ok bt_report_units_untouched '[ -x "$LIB/rootsync" ] && [ "$B" = "$A" ]'
