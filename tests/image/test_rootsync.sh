#!/bin/bash
# shellcheck disable=SC2034,SC2016
# rootsync.sh: root's own signed copy of PiLNK (Part B spec 5.1-5.2, plan Task 1).
# Real git, real ssh-keygen, throwaway keys, a fake root under $P.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"

. "$(dirname "$0")/rootsync-testlib.sh"

# --- a good release becomes current
setup; rel 1.0.1; RS
ok rs_good_release_becomes_current '[ "$RC" = 0 ] && [ "$(cur)" = "signed/1.0.1-${C:0:12}" ] && [ -f "$RSB/signed/1.0.1-${C:0:12}/.complete" ]'
ok rs_status_ok_user '[ "$RC" = 0 ] && st_field src = user && st_field version = 1.0.1 && st_field commit = "$C" && st_field reason = ""'
ok rs_no_git_dir_in_copy '[ -f "$RSB/current/VERSION" ] && [ ! -e "$RSB/current/.git" ]'
ok rs_copy_is_the_signed_tree '[ "$(cat "$RSB/current/VERSION")" = 1.0.1 ] && cmp -s "$RSB/current/release-check.sh" "$REPO/release-check.sh"'

# --- the same release again: one GET, no git
: > "$CALLS"; RS
ok rs_same_release_one_get_no_git '[ "$RC" = 0 ] && [ "$(grep -c "^curl" "$CALLS")" = 1 ] && ! grep -q "^git .*fetch" "$CALLS"'

# --- refusals keep current
OLD=$(cur)
rel 1.0.2; sed -i 's/version 1.0.2/version 1.0.3/' "$ROOT/st"; vp; RS
ok rs_tampered_statement_refused '[ "$RC" = 1 ] && st_field reason = bad && st_field src = error && [ "$(cur)" = "$OLD" ]'
testkey other; rel 1.0.2 other; RS
ok rs_unknown_key_refused '[ "$RC" = 1 ] && st_field reason = bad && [ "$(cur)" = "$OLD" ]'
( cd "$ROOT/up" && G checkout -qb side && echo 9.9.9 > VERSION && G commit -qam side && G checkout -q main )
statement 9.9.9 "$("$REALGIT" -C "$ROOT/up" rev-parse side)" > "$ROOT/st"; sign aj "$ROOT/st"; vp; RS
ok rs_commit_not_on_main '[ "$RC" = 1 ] && st_field reason = not_on_main && [ "$(cur)" = "$OLD" ]'
rel 1.0.2; statement 1.0.5 "$C" > "$ROOT/st"; sign aj "$ROOT/st"; vp; RS
ok rs_version_mismatch '[ "$RC" = 1 ] && st_field reason = mismatch && [ "$(cur)" = "$OLD" ]'

# --- pilnk.io unreachable: nothing on disk changes
before=$(tree_sum); URL="file://$ROOT/nope.json" RS; after=$(tree_sum)
ok rs_fetch_fail_keeps_current '[ "$RC" = 1 ] && st_field reason "~" "^fetch:[0-9]+$" && [ "$before" = "$after" ] && [ "$(cur)" = "$OLD" ]'
ok rs_fetch_fail_keeps_version 'st_field version = 1.0.1 && st_field fails "~" "^[1-9]"'

# --- never backwards
setup; rel 1.0.1; cp "$ROOT/st" "$ROOT/st.101"; cp "$ROOT/st.sig" "$ROOT/st.101.sig"
rel 1.0.2; RS; NEW=$(cur)
cp "$ROOT/st.101" "$ROOT/st"; cp "$ROOT/st.101.sig" "$ROOT/st.sig"; vp; RS
ok rs_never_backwards '[ "$RC" = 1 ] && st_field reason = older && st_field version = 1.0.2 && [ "$(cur)" = "$NEW" ]'

# --- two copies kept, previous points at the one before
setup; rel 1.0.1; RS; FIRST=$(cur); rel 1.0.2; RS; SECOND=$(cur); rel 1.0.3; RS
ok rs_keeps_two_copies '[ "$(ls "$RSB/signed" | wc -l)" = 2 ] && [ "$(readlink "$RSB/previous")" = "$SECOND" ] && [ ! -e "$RSB/$FIRST" ]'

# --- signer rotation takes effect one release late (after a first sync on the
#     pinned keys: a first sync must match them exactly, Task 5)
setup; testkey newkey; rel 1.0.1; RS; FIRST=$(cur)
( cd "$ROOT/up" && signers_line newkey >> release_signers && G commit -qam "add newkey" )
rel 1.0.2 newkey; RS
ok rs_rotation_new_key_cannot_sign_its_own_release '[ "$RC" = 1 ] && st_field reason = bad && [ "$(cur)" = "$FIRST" ]'
rel 1.0.3; RS
ok rs_rotation_release_signed_by_old_key_ok '[ "$RC" = 0 ] && grep -qF "$(cut -d" " -f2 "$ROOT/keys/newkey.pub")" "$RSB/trust/release_signers"'
rel 1.0.4 newkey; RS
ok rs_signer_rotation_one_late '[ "$RC" = 0 ] && st_field version = 1.0.4'

# --- extract fails (disk full): no .tmp left, current unchanged
setup; rel 1.0.1; RS; OLD=$(cur); rel 1.0.2
fakebin tar 'exit 1'; RS
ok rs_extract_fail_cleans_tmp '[ "$RC" = 1 ] && st_field reason = disk && [ -z "$(ls "$RSB/signed" | grep "\.tmp$")" ] && [ "$(cur)" = "$OLD" ]'
fakebin tar 'exec /usr/bin/tar "$@"'

# --- a MANIFEST script that does not parse blocks the swap; a missing MANIFEST too
( cd "$ROOT/up" && printf 'if then fi (\n' > broken.sh && echo broken.sh >> rootside/MANIFEST && G add -A && G commit -qm broken )
rel 1.0.3; RS
ok rs_syntax_check_blocks_swap '[ "$RC" = 1 ] && st_field reason = syntax && [ "$(cur)" = "$OLD" ]'
( cd "$ROOT/up" && G rm -q rootside/MANIFEST && G commit -qm nomanifest )
rel 1.0.4; RS
ok rs_missing_manifest_refused '[ "$RC" = 1 ] && st_field reason = manifest && [ "$(cur)" = "$OLD" ]'

# --- unsafe ownership/modes: refused before any network or git
setup; rel 1.0.1; RS; chmod o+w "$RSB/state"; : > "$CALLS"; RS
ok rs_unsafe_world_writable_refused '[ "$RC" = 1 ] && st_field reason = unsafe && st_field src = unsafe && ! grep -q "^git" "$CALLS" && ! grep -q "^curl" "$CALLS"'
chmod o-w "$RSB/state"; RS
ok rs_safe_again_recovers '[ "$RC" = 0 ] && st_field src = user'
setup; mkdir -p "$ROOT/elsewhere"; rmdir "$RSB/trust" 2>/dev/null; rm -rf "$RSB"; ln -s "$ROOT/elsewhere" "$RSB"; rel 1.0.1; RS
ok rs_base_symlink_refused '[ "$RC" = 1 ] && ! grep -q "^curl" "$CALLS"'

# --- a second run while one holds the lock exits 0 quickly and does nothing
setup; rel 1.0.1
( flock "$P/run/pilnk-rootsync.lock" sleep 4 ) & HOLD=$!; sleep 0.5
: > "$CALLS"; t0=$(date +%s); RS; t1=$(date +%s)
ok rs_lock_second_run_waits_then_exits '[ "$RC" = 0 ] && [ $((t1 - t0)) -lt 4 ] && ! grep -q "^curl" "$CALLS"'
wait "$HOLD"; RS
ok rs_lock_free_then_syncs '[ "$RC" = 0 ] && [ -n "$(cur)" ]'

# --- usage
RS --bogus
ok rs_usage_exit_2 '[ "$RC" = 2 ]'

# --- the real MANIFEST: every entry exists in this repo and parses
bad=0; while IFS= read -r m; do case "$m" in ''|'#'*) continue;; esac
  [ -f "$REPO/$m" ] && bash -n "$REPO/$m" 2>/dev/null || { echo "  manifest problem: $m"; bad=1; }
done < "$REPO/rootside/MANIFEST" 2>/dev/null || bad=1
ok rs_manifest_entries_exist_and_parse '[ "$bad" = 0 ] && grep -qx sdr-recover.sh "$REPO/rootside/MANIFEST" && grep -qx rootsync.sh "$REPO/rootside/MANIFEST"'

# ============ Task 2: which release root follows, self-update, recovery ============
# --- manual-update node: the timer only checks; the audio build (--quick) moves
setup; mksvc manual; rel 1.0.1; RS --quick; OLD=$(cur); rel 1.0.2; RS
ok rs_manual_node_timer_checks_only '[ "$RC" = 0 ] && st_field reason = held && st_field src = user && st_field version = 1.0.1 && [ "$(cur)" = "$OLD" ]'
RS --quick
ok rs_manual_node_quick_moves '[ "$RC" = 0 ] && st_field version = 1.0.2 && st_field reason = ""'
setup; mksvc auto; rel 1.0.1; RS
ok rs_auto_node_timer_moves '[ "$RC" = 0 ] && st_field version = 1.0.1'

# --- unreadable or hostile config.json counts as auto, and never hangs
for kind in missing fifo huge symlink; do
  setup; mksvc "$kind"; rel 1.0.1; RS --quick; rel 1.0.2; t0=$(date +%s); RS; t1=$(date +%s)
  ok "rs_auto_default_when_unreadable ($kind)" '[ "$RC" = 0 ] && st_field version = 1.0.2 && [ $((t1 - t0)) -lt 6 ]'
done

# --- --quick always exits 0 so the build runs from current
setup; rel 1.0.1; URL="file://$ROOT/nope.json" RS --quick
ok rs_quick_always_exits_0 '[ "$RC" = 0 ] && st_field reason "~" "^fetch:"'

# --- self-update: a release's rootsync.sh replaces the installed one only after its --selftest
install_rs() { mkdir -p "$LIB"; install -m 0755 "$REPO/rootsync.sh" "$LIB/rootsync"; }
setup; install_rs
( cd "$ROOT/up" && cp "$REPO/rootsync.sh" rootsync.sh && echo '# new rootsync' >> rootsync.sh \
  && G add -A && G commit -qm "new rootsync" )
OLDSUM=$(sha256sum < "$LIB/rootsync"); rel 1.0.1; RS
ok rs_new_rootsync_adopted_after_selftest '[ "$RC" = 0 ] && grep -q "^# new rootsync" "$LIB/rootsync" && [ "$(sha256sum < "$LIB/rootsync.prev")" = "$OLDSUM" ]'
( cd "$ROOT/up" && printf '#!/bin/bash\n[ "${1:-}" = --selftest ] && exit 1\nexit 0\n' > rootsync.sh && G commit -qam "broken rootsync" )
BEFORE=$(sha256sum < "$LIB/rootsync"); rel 1.0.2; RS
ok rs_broken_new_rootsync_not_adopted '[ "$RC" = 0 ] && st_field version = 1.0.2 && st_field reason = selftest && [ "$(sha256sum < "$LIB/rootsync")" = "$BEFORE" ]'
ok rs_selftest_of_real_script '[ "$(bash -c "PILNK_ROOTSYNC_PREFIX=$P PILNK_ROOTSYNC_UID=$(id -u) bash $REPO/rootsync.sh --selftest >/dev/null 2>&1; echo \$?")" = 0 ]'

# --- rootsync-run: 3 internal failures in a row, then rootsync.prev; network failures don't count
RUN="$REPO/rootside/rootsync-run"
run_once() { PILNK_ROOTSYNC_PREFIX="$P" bash "$RUN" >/dev/null 2>&1; RRC=$?; }
setup; mkdir -p "$LIB" "$RSB/state"
printf '#!/bin/bash\nexit 3\n' > "$LIB/rootsync"; printf '#!/bin/bash\ntouch "%s/PREV_RAN"; exit 0\n' "$ROOT" > "$LIB/rootsync.prev"
chmod 0755 "$LIB/rootsync" "$LIB/rootsync.prev"
run_once; run_once; run_once
ok run_three_internal_fails_counted '[ ! -e "$ROOT/PREV_RAN" ] && [ "$(cat "$RSB/state/internal_fails")" = 3 ]'
run_once
ok run_falls_back_after_three_internal '[ -e "$ROOT/PREV_RAN" ] && [ "$(cat "$RSB/state/internal_fails")" = 0 ]'
printf '#!/bin/bash\nexit 2\n' > "$LIB/rootsync"; rm -f "$ROOT/PREV_RAN"; run_once; run_once; run_once; run_once
ok run_syntax_error_exit_2_counted_too '[ -e "$ROOT/PREV_RAN" ]'
setup; mkdir -p "$LIB" "$RSB/state"
# a network failure as rootsync records it: status rewritten with reason fetch:N, exit 1
printf '#!/bin/bash\nS=%s/state/status.json; echo "{\\"reason\\": \\"fetch:28\\"}" > "$S.t"; mv -f "$S.t" "$S"; exit 1\n' "$RSB" > "$LIB/rootsync"
printf '#!/bin/bash\ntouch "%s/PREV_RAN"\n' "$ROOT" > "$LIB/rootsync.prev"
chmod 0755 "$LIB/rootsync" "$LIB/rootsync.prev"
for i in 1 2 3 4 5; do run_once; done
ok run_network_fail_not_counted '[ ! -e "$ROOT/PREV_RAN" ] && [ "$(cat "$RSB/state/internal_fails" 2>/dev/null || echo 0)" = 0 ]'

# --- an interrupted swap: current points at a folder with no .complete
setup; rel 1.0.1; RS; P1=$(cur); rel 1.0.2; RS; P2=$(cur)
rm -f "$RSB/$P2/.complete"; URL="file://$ROOT/nope.json" RS
ok rs_interrupted_swap_uses_previous '[ "$(cur)" = "$P1" ] && st_field version = 1.0.1'
RS
ok rs_interrupted_swap_heals_on_next_good_run '[ "$RC" = 0 ] && [ "$(cur)" = "$P2" ] && [ -f "$RSB/$P2/.complete" ]'
