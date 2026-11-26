#!/bin/bash
# shellcheck disable=SC2034
# R2 (enforce) chain proof. Unlike test_update_signed.sh, the node runs ITS OWN
# checked-out update.sh at every step, exactly as a real node does, so this
# proves the hand-over from a report-mode release to the enforce release and
# that an enforce node still moves to the NEXT signed release (not stranded).
#   step 1  node on R1 (report), R2 (enforce) pushed but not signed yet
#   step 2  AJ signs R2                      -> no-op, now enforcing
#   step 3  R3 pushed, not signed            -> stays on R2
#   step 4  AJ signs R3                      -> moves to R3
#   step 5  a 1.0.3 signed by another key    -> refused, tree untouched
#   step 6  1.0.3 signed by AJ               -> moves on again
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"

serve_release() { python3 - "$ROOT/st" "$ROOT/version.json" <<'PY'
import json, sys
st = open(sys.argv[1], 'rb').read().decode(); sig = open(sys.argv[1] + '.sig', 'rb').read().decode()
json.dump({'version': '9.9.9', 'required': False, 'release': {'statement': st, 'sig': sig}}, open(sys.argv[2], 'w'))
PY
}
# the node runs the update.sh it has checked out, like a real node
node_update() { OUT=$(cd "$HOME" && PILNK_UPDATE_NO_RESTART=1 bash "$HOME/pilnk/update.sh" 2>&1); RC=$?; }
ver() { cat "$HOME/pilnk/VERSION"; }
head_() { "$REALGIT" -C "$HOME/pilnk" rev-parse HEAD; }
up_head() { "$REALGIT" -C "$ROOT/up" rev-parse HEAD; }
st() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$HOME/pilnk/release_state.json" "$1" 2>/dev/null; }
treehash() { ( cd "$HOME/pilnk" && "$REALGIT" rev-parse HEAD; "$REALGIT" ls-files -s; "$REALGIT" ls-files -z | xargs -0 sha256sum ) | sha256sum; }

mkroot; mkrepo
fakebin pgrep 'exit 1'
fakebin sudo 'exit 0'
fakebin systemctl 'case "$1" in show) echo 100;; list-unit-files) exit 1;; *) exit 0;; esac'
export PILNK_RELEASE_URL="file://$ROOT/version.json"

# R1: the code under test, report mode, signed
cp "$REPO/update.sh" "$REPO/release-check.sh" "$ROOT/up/"
( cd "$ROOT/up" && printf 'report\n' > release_policy && G add -A && G commit -qm "R1 report" )
statement 1.0.0 "$(up_head)" > "$ROOT/st"; sign aj "$ROOT/st"; serve_release; node_pull
ok "start: node on R1 in report mode" '[ "$(ver)" = 1.0.0 ] && [ "$(cat "$HOME/pilnk/release_policy")" = report ]'

# step 1: R2 = only release_policy -> enforce (+ VERSION); signature still R1's
( cd "$ROOT/up" && printf 'enforce\n' > release_policy && echo 1.0.1 > VERSION && G commit -qam "R2 enforce" )
R2=$(up_head)
node_update
ok "1: report-mode node takes the unsigned R2 as today" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ] && [ "$(head_)" = "$R2" ]'
ok "1: node now carries release_policy=enforce" '[ "$(cat "$HOME/pilnk/release_policy")" = enforce ]'

# step 2: AJ signs R2 -> same version, HEAD on it: clean no-op, enforcing
statement 1.0.1 "$R2" > "$ROOT/st"; sign aj "$ROOT/st"; serve_release
node_update
ok "2: signed R2 -> exit 0, still R2" '[ "$RC" = 0 ] && [ "$(head_)" = "$R2" ]'
ok "2: state mode enforce, ok, on_signed" '[ "$(st mode)" = enforce ] && [ "$(st ota_sig)" = ok ] && [ "$(st ota_sig_run)" = on_signed ]'

# step 3: R3 pushed to GitHub, not signed yet -> enforce node stays on R2
( cd "$ROOT/up" && echo 'print(3)' > app.py && echo 1.0.2 > VERSION && G commit -qam "R3" )
R3=$(up_head)
node_update
ok "3: unsigned R3 not installed, exit 0, still R2" '[ "$RC" = 0 ] && [ "$(head_)" = "$R2" ] && [ "$(ver)" = 1.0.1 ]'

# step 4: AJ signs R3 -> the enforce node moves to it (NOT stranded)
statement 1.0.2 "$R3" > "$ROOT/st"; sign aj "$ROOT/st"; serve_release
node_update
ok "4: signed R3 installed by the enforce node" '[ "$RC" = 0 ] && [ "$(head_)" = "$R3" ] && [ "$(ver)" = 1.0.2 ]'
ok "4: still enforcing after the move" '[ "$(cat "$HOME/pilnk/release_policy")" = enforce ] && [ "$(st mode)" = enforce ]'

# step 5: a release signed by a key not in release_signers is refused
testkey evil
( cd "$ROOT/up" && echo 'print(666)' > app.py && echo 1.0.3 > VERSION && G commit -qam "evil" )
EVIL=$(up_head)
statement 1.0.3 "$EVIL" > "$ROOT/st"; sign evil "$ROOT/st"; serve_release
TREE0=$(treehash); node_update
ok "5: foreign-key release refused (exit 6), tree byte-identical" '[ "$RC" = 6 ] && [ "$(treehash)" = "$TREE0" ] && [ "$(ver)" = 1.0.2 ]'
ok "5: refusal names the bad signature" 'grep -q "SIGNATURE REFUSED: bad" "$HOME/pilnk/update.log"'

# step 6: the same commit signed by AJ goes through (refusal left nothing stuck)
sign aj "$ROOT/st"; serve_release
node_update
ok "6: AJ-signed 1.0.3 installed after a refusal" '[ "$RC" = 0 ] && [ "$(head_)" = "$EVIL" ] && [ "$(ver)" = 1.0.3 ]'

