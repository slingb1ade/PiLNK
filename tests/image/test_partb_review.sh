#!/bin/bash
# shellcheck disable=SC2034,SC2016
# Part B final-review fixes (I-1..I-4): rootsync-run counts by outcome, a new
# rootsync must verify the release it came in, the one-time install is safe to
# interrupt, root's copy leaves out what root never runs and checks free space,
# and a manual-update node's root follows the release its owner installed.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"
. "$(dirname "$0")/rootsync-testlib.sh"
ME=$(id -u)

# ---- I-1: rootsync-run counts by what happened, not by exit code -------------
RUN="$REPO/rootside/rootsync-run"
run_once() { PILNK_ROOTSYNC_PREFIX="$P" bash "$RUN" >/dev/null 2>&1; }
fake_rs() {  # BODY of a fake installed rootsync; rootsync.prev marks that it ran
  mkdir -p "$LIB" "$RSB/state"
  printf '#!/bin/bash\n%s\n' "$1" > "$LIB/rootsync"
  printf '#!/bin/bash\ntouch "%s/PREV_RAN"; exit 0\n' "$ROOT" > "$LIB/rootsync.prev"
  chmod 0755 "$LIB/rootsync" "$LIB/rootsync.prev"
}
# a status written the way rootsync writes it (tmp + rename), then exit 1
st_exit1() { printf 'S=%s/state/status.json; printf %%s "{\\"src\\": \\"user\\", \\"reason\\": \\"%s\\"}" > "$S.t"; mv -f "$S.t" "$S"; exit 1' "$RSB" "$1"; }

setup; fake_rs 'exit 1'
run_once; run_once; run_once; C3=$(cat "$RSB/state/internal_fails"); run_once
ok rv_exit1_without_status_counted '[ "$C3" = 3 ] && [ -e "$ROOT/PREV_RAN" ]'
# an old network failure on file, then a run that dies without writing: counted
setup; fake_rs 'exit 1'; printf '{"src": "user", "reason": "fetch:28"}' > "$RSB/state/status.json"; run_once
ok rv_exit1_stale_fetch_status_counted '[ "$(cat "$RSB/state/internal_fails")" = 1 ]'
setup; fake_rs "$(st_exit1 check)"; run_once
ok rv_exit1_refusal_counted '[ "$(cat "$RSB/state/internal_fails")" = 1 ]'
for r in fetch:28 git older; do
  setup; fake_rs "$(st_exit1 "$r")"; for i in 1 2 3 4 5; do run_once; done
  ok "rv_exit1_external_${r%%:*}_not_counted" '[ ! -e "$ROOT/PREV_RAN" ] && [ "$(cat "$RSB/state/internal_fails")" = 0 ]'
done

# ---- I-1: a new rootsync is installed only if it can verify the release it came in
setup; ( cd "$ROOT/up" && cp "$REPO/rootsync.sh" . && G add -A && G commit -qm "rootsync in the release" )
rel 1.0.1; RS
GOOD=$(sha256sum < "$LIB/rootsync" 2>/dev/null)
( cd "$ROOT/up" && echo '# 1.0.2' >> rootsync.sh \
  && printf '#!/bin/bash\necho ota_sig=bad\nexit 0\n' > release-check.sh && G commit -qam "broken checker" )
rel 1.0.2; RS
ok rv_selftest_needs_a_working_verify '[ -n "$GOOD" ] && [ "$(sha256sum < "$LIB/rootsync")" = "$GOOD" ] && st_field version = 1.0.2 && st_field reason = selftest'

# ---- I-2: the one-time install can be interrupted and is tried again ----------
mktree() {
  mkdir -p "$1/rootside"
  cp "$REPO/rootsync.sh" "$REPO/release-check.sh" "$1/"
  cp "$REPO/rootside/rootsync-run" "$REPO/rootside/pilnk-rootsync.service" "$REPO/rootside/pilnk-rootsync.timer" "$1/rootside/"
}
IS() { PILNK_ROOTSYNC_PREFIX="$P" PILNK_ROOTSYNC_UID="$ME" PILNK_ROOTSYNC_PINNED_SIGNERS="$ROOT/pinned" \
  bash "$ROOT/tree/rootsync.sh" --install-self > "$ROOT/is.out" 2>&1; RC=$?; }
setup; rm -rf "$RSB"; signers_line aj > "$ROOT/pinned"; mktree "$ROOT/tree"
cp "$BIN/systemctl" "$ROOT/systemctl.ok"
fakebin systemctl 'case "$*" in *enable*) exit 1 ;; esac; exit 0'
IS
ok rv_install_interrupted_not_marked_done '[ "$RC" != 0 ] && [ ! -x "$LIB/rootsync" ] && st_field src = user && st_field reason = installing'
cp "$ROOT/systemctl.ok" "$BIN/systemctl"; : > "$CALLS"; IS
ok rv_install_retried_completes '[ "$RC" = 0 ] && [ -x "$LIB/rootsync" ] && grep -q "systemctl enable pilnk-rootsync.timer" "$CALLS"'

# ---- I-3: root's copy leaves out what root never runs, and checks free space ---
setup; ( cd "$ROOT/up" && mkdir -p static tests && head -c 4096 /dev/zero > static/model.glb \
  && echo 'echo t' > tests/t.sh && G add -A && G commit -qm "app-only files" )
rel 1.0.1; RS
ok rv_copy_skips_static_and_tests '[ "$RC" = 0 ] && [ -f "$RSB/current/release-check.sh" ] && [ -f "$RSB/current/VERSION" ] && [ ! -e "$RSB/current/static" ] && [ ! -e "$RSB/current/tests" ]'
setup; rel 1.0.1; RS; OLD=$(cur); rel 1.0.2
fakebin df 'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 100 90 10 90%% /\n"'
RS
ok rv_low_space_refused_before_fetch '[ "$RC" = 1 ] && st_field reason = space && [ "$(cur)" = "$OLD" ] && [ -z "$(find "$RSB/signed" -name "*.tmp")" ] && st_field version = 1.0.1'

# ---- I-4: a manual-update node's root follows the release its owner installed --
setup; mksvc manual; rel 1.0.1; RS --quick; rel 1.0.2; node_pull; RS
ok rv_manual_follows_installed_version '[ "$RC" = 0 ] && st_field version = 1.0.2 && st_field reason = ""'
setup; mksvc manual; rel 1.0.1; RS --quick; rel 1.0.2; RS
ok rv_manual_older_tree_still_holds '[ "$RC" = 0 ] && st_field reason = held && st_field version = 1.0.1'
setup; mksvc manual; rel 1.0.1; RS --quick; rel 1.0.2
echo 1.0.2 > "$ROOT/v.txt"; rm -f "$HOME/pilnk/VERSION"; ln -s "$ROOT/v.txt" "$HOME/pilnk/VERSION"; RS
ok rv_manual_version_symlink_ignored '[ "$RC" = 0 ] && st_field reason = held && st_field version = 1.0.1'
