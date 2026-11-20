#!/bin/bash
# shellcheck disable=SC2034
# update.sh with signed releases (spec 5.5, plan Task 3). Real git, real
# ssh-keygen, throwaway keys, a local "GitHub" and a file:// stand-in for
# pilnk.io/api/version.php. Restart-path cases use fake sudo/systemctl only.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"

# setup POLICY: node + up share release-check.sh and release_policy=POLICY
setup() {
  mkroot; mkrepo
  cp "$REPO/release-check.sh" "$ROOT/up/release-check.sh"
  ( cd "$ROOT/up" && printf '%s\n' "$1" > release_policy && G add -A && G commit -qm "policy $1" )
  node_pull
  fakebin pgrep 'exit 1'
  fakebin sudo 'exit 0'
  fakebin systemctl 'case "$1" in show) n=$(cat "$ROOT/.pid" 2>/dev/null || echo 100); echo "$n"; echo $((n+1)) > "$ROOT/.pid";; list-unit-files) exit 1;; *) exit 0;; esac'
  STATE="$HOME/pilnk/release_state.json"; LOGF="$HOME/pilnk/update.log"
  export PILNK_RELEASE_URL="file://$ROOT/version.json"
}
# serve_release [FILE]: version.json carrying $ROOT/st (+ .sig); serve_null: release null
serve_release() { python3 - "${1:-$ROOT/st}" "$ROOT/version.json" <<'PY'
import json, sys
st = open(sys.argv[1], 'rb').read().decode(); sig = open(sys.argv[1] + '.sig', 'rb').read().decode()
json.dump({'version': '9.9.9', 'required': False, 'release': {'statement': st, 'sig': sig}}, open(sys.argv[2], 'w'))
PY
}
serve_null() { echo '{"version":"9.9.9","required":false,"release":null}' > "$ROOT/version.json"; }
run_update() { OUT=$(cd "$HOME" && PILNK_UPDATE_NO_RESTART=1 bash "$REPO/update.sh" 2>&1); RC=$?; }
run_restart() { OUT=$(cd "$HOME" && bash "$REPO/update.sh" 2>&1); RC=$?; }
ver() { cat "$HOME/pilnk/VERSION"; }
head_() { git -C "$HOME/pilnk" rev-parse HEAD; }
st() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$STATE" "$1" 2>/dev/null; }
treehash() { ( cd "$HOME/pilnk" && git rev-parse HEAD; git ls-files -s; git ls-files -z | xargs -0 sha256sum ) | sha256sum; }

# ===== report mode: behaves as today whatever the signature says =====
setup report; release_to 1.0.1; serve_release; run_update
ok "report: good sig, updates to origin/main as today" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'
ok "report: state ok, mode report, by=update" '[ "$(st ota_sig)" = ok ] && [ "$(st mode)" = report ] && [ "$(st by)" = update ]'
ok "report: state carries signed version + commit" '[ "$(st signed_version)" = 1.0.1 ] && [ "$(st signed_commit)" = "$(git -C "$ROOT/up" rev-parse HEAD)" ]'
ok "report: log line names the result" 'grep -q "Release signature: ota_sig=ok ota_sig_run=behind mode=report" "$LOGF"'
setup report; testkey other; release_to 1.0.1 other; serve_release; run_update
ok "report: bad sig still updates, state says bad" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ] && [ "$(st ota_sig)" = bad ]'
setup report; release_to 1.0.1; export PILNK_RELEASE_URL="file://$ROOT/nope.json"; run_update
ok "report: version.php unreachable -> error, still updates" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ] && [ "$(st ota_sig)" = error ] && [ "$(st ota_sig_run)" = none ]'
setup report; ( cd "$ROOT/up" && echo x >> app.py && G commit -qam same-version ); serve_null; run_restart
ok "report: Rule #28 abort still fires (exit 3)" '[ "$RC" = 3 ]'
setup report; release_to 1.0.1; serve_release; : > "$HOME/pilnk/.git/index.lock"; run_update
ok "report: power-cut lock cleanup still runs first" '[ ! -e "$HOME/pilnk/.git/index.lock" ] && [ "$RC" = 0 ] && [ "$(st ota_sig)" = ok ]'
setup report; release_to 1.0.1; serve_release; printf 'enforced\n' > "$HOME/pilnk/release_policy"
( cd "$HOME/pilnk" && G commit -qam garbled ) ; run_update
ok "garbled release_policy counts as report" '[ "$(st mode)" = report ]'

# ===== enforce mode =====
setup enforce; release_to 1.0.1; SIGNED=$(git -C "$ROOT/up" rev-parse HEAD); serve_release
( cd "$ROOT/up" && echo 1.0.2 > VERSION && G commit -qam "unsigned 1.0.2" ); run_update
ok "enforce: resets to the SIGNED commit, not origin/main" '[ "$RC" = 0 ] && [ "$(head_)" = "$SIGNED" ] && [ "$(ver)" = 1.0.1 ]'
ok "enforce: state mode enforce" '[ "$(st mode)" = enforce ]'

setup enforce; release_to 1.0.1; serve_release; node_pull; run_restart
ok "enforce: same version + HEAD on it -> exit 0, no restart call" '[ "$RC" = 0 ] && ! grep -q "systemctl restart" "$CALLS"'
setup enforce; release_to 1.0.1; serve_release; node_pull; run_update
ok "enforce: first-boot no-restart path, same version -> exit 0" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'

setup enforce; ( cd "$ROOT/up" && echo 1.0.1 > VERSION && G commit -qam "A 1.0.1 unsigned" ); node_pull
( cd "$ROOT/up" && echo 'print(3)' > app.py && G commit -qam "B 1.0.1" )
statement 1.0.1 "$(git -C "$ROOT/up" rev-parse HEAD)" > "$ROOT/st"; sign aj "$ROOT/st"; SIGNED=$(git -C "$ROOT/up" rev-parse HEAD)
serve_release; run_restart
ok "enforce: same version, different HEAD -> converges, no Rule #28 abort" '[ "$RC" = 0 ] && [ "$(head_)" = "$SIGNED" ] && ! grep -q "Rule #28" "$LOGF"'

setup enforce; release_to 1.0.1; cp "$ROOT/st" "$ROOT/old"; cp "$ROOT/st.sig" "$ROOT/old.sig"
( cd "$ROOT/up" && echo 1.0.2 > VERSION && G commit -qam v1.0.2 ); node_pull; serve_release "$ROOT/old"
TREE0=$(treehash); run_update
ok "enforce: downgrade refused, exit 6" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: downgrade" "$LOGF"'
ok "enforce: downgrade leaves tree byte-identical" '[ "$(treehash)" = "$TREE0" ]'

# each refusal token: exit 6, tree untouched
setup enforce; release_to 1.0.1; serve_null; TREE0=$(treehash); run_update
ok "enforce: missing refused, exit 6, tree byte-identical" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: missing" "$LOGF" && [ "$(treehash)" = "$TREE0" ]'
setup enforce; testkey other; release_to 1.0.1 other; serve_release; TREE0=$(treehash); run_update
ok "enforce: bad refused, exit 6, tree byte-identical" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: bad" "$LOGF" && [ "$(treehash)" = "$TREE0" ]'
setup enforce; ( cd "$ROOT/up" && G checkout -qb side && echo 9.9.9 > VERSION && G commit -qam side && G checkout -q main )
statement 9.9.9 "$(git -C "$ROOT/up" rev-parse side)" > "$ROOT/st"; sign aj "$ROOT/st"; serve_release; TREE0=$(treehash); run_update
ok "enforce: not_on_main refused, exit 6, tree byte-identical" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: not_on_main" "$LOGF" && [ "$(treehash)" = "$TREE0" ]'
setup enforce; release_to 1.0.1; statement 1.0.5 "$(git -C "$ROOT/up" rev-parse HEAD)" > "$ROOT/st"; sign aj "$ROOT/st"
serve_release; TREE0=$(treehash); run_update
ok "enforce: mismatch refused, exit 6, tree byte-identical" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: mismatch" "$LOGF" && [ "$(treehash)" = "$TREE0" ]'
setup enforce; release_to 1.0.1; export PILNK_RELEASE_URL="file://$ROOT/nope.json"; TREE0=$(treehash); run_update
ok "enforce: error (no version.php) refused, exit 6, tree byte-identical" '[ "$RC" = 6 ] && grep -q "SIGNATURE REFUSED: error" "$LOGF" && [ "$(treehash)" = "$TREE0" ]'
ok "enforce: refusal names itself for the app's ota_fail" 'grep -q "SIGNATURE REFUSED" <<<"$OUT"'

# ===== plumbing =====
ok "state file written via an atomic rename" 'grep -q "os.replace" "$REPO/update.sh"'
setup report; release_to 1.0.1; serve_release; git clone -q "$ROOT/up" "$ROOT/elsewhere"
OUT=$(cd "$HOME" && PILNK_DIR="$ROOT/elsewhere" PILNK_UPDATE_NO_RESTART=1 bash "$REPO/update.sh" 2>&1); RC=$?
ok "PILNK_DIR from env is honoured" '[ "$RC" = 0 ] && [ "$(cat "$ROOT/elsewhere/VERSION")" = 1.0.1 ] && [ -f "$ROOT/elsewhere/release_state.json" ]'
