#!/bin/bash
# shellcheck disable=SC2034
# release-check.sh: does AJ's signature cover the newest PiLNK release? (spec 5.5)
# Real git, real ssh-keygen (the sandbox runs OpenSSH 8.9, the fleet's floor),
# throwaway keys only.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/sigtest-lib.sh"
CHK="$REPO/release-check.sh"
chk() { out=$(PILNK_DIR="$HOME/pilnk" bash "$CHK" "$@" 2>/dev/null); RC=$?; }
f() { printf '%s\n' "$out" | sed -n "s/^$1=//p"; }
snap() { ( cd "$HOME/pilnk" && "$REALGIT" rev-parse HEAD; "$REALGIT" for-each-ref; sha256sum .git/index; \
           "$REALGIT" ls-files -z | xargs -0 sha256sum ) 2>&1 | sha256sum; }

# --- good signature, node on it
mkroot; mkrepo; release_to 1.0.1; node_pull; chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "good signature, node on it" '[ "$(f ota_sig)" = ok ] && [ "$(f ota_sig_run)" = on_signed ]'
ok "reports the signed version and commit" '[ "$(f signed_version)" = 1.0.1 ] && [ "$(f signed_commit)" = "$(git -C "$ROOT/up" rev-parse HEAD)" ]'
ok "exactly four output lines, exit 0" '[ "$RC" = 0 ] && [ "$(printf "%s\n" "$out" | wc -l)" = 4 ]'
touch -d 2020-01-01 "$HOME/pilnk/app.py"   # stale stat: a plain `git status` would rewrite the index
before=$(snap); chk --fetched "$ROOT/st" "$ROOT/st.sig"; after=$(snap)
ok "read-only: HEAD, refs, index and tracked files unchanged" '[ "$before" = "$after" ]'

# --- no release object
mkroot; mkrepo; : > "$ROOT/st"; : > "$ROOT/st.sig"; chk "$ROOT/st" "$ROOT/st.sig"
ok "no release object (empty files) -> missing/none" '[ "$(f ota_sig)" = missing ] && [ "$(f ota_sig_run)" = none ]'
chk "$ROOT/nope" "$ROOT/nope.sig"
ok "no release object (absent files) -> missing" '[ "$(f ota_sig)" = missing ]'

# --- signatures that must not verify
mkroot; mkrepo; testkey other; release_to 1.0.1 other; node_fetch; chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "unknown key -> bad/none" '[ "$(f ota_sig)" = bad ] && [ "$(f ota_sig_run)" = none ]'
mkroot; mkrepo; release_to 1.0.1; sed -i 's/version 1.0.1/version 1.0.2/' "$ROOT/st"; chk "$ROOT/st" "$ROOT/st.sig"
ok "tampered statement (one byte) -> bad" '[ "$(f ota_sig)" = bad ]'
mkroot; mkrepo; release_to 1.0.1; sign aj "$ROOT/st" other-namespace; chk "$ROOT/st" "$ROOT/st.sig"
ok "wrong namespace -> bad" '[ "$(f ota_sig)" = bad ]'

# --- the running tree's signers, never the incoming one
mkroot; mkrepo; testkey newkey
( cd "$ROOT/up" && signers_line newkey >> release_signers && G commit -qam "add newkey" )
release_to 1.0.1 newkey; node_fetch; chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "verifies against the RUNNING tree signers (key only on origin/main) -> bad" '[ "$(f ota_sig)" = bad ]'

# --- fetched-only checks
mkroot; mkrepo; release_to 1.0.1
statement 1.0.5 "$(git -C "$ROOT/up" rev-parse HEAD)" > "$ROOT/st"; sign aj "$ROOT/st"; node_fetch
chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "statement version != VERSION at commit (--fetched) -> mismatch" '[ "$(f ota_sig)" = mismatch ]'
mkroot; mkrepo
( cd "$ROOT/up" && G checkout -qb side && echo 9.9.9 > VERSION && G commit -qam side && G checkout -q main )
statement 9.9.9 "$(git -C "$ROOT/up" rev-parse side)" > "$ROOT/st"; sign aj "$ROOT/st"; node_fetch
chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "commit not on main (--fetched) -> not_on_main" '[ "$(f ota_sig)" = not_on_main ]'
chk "$ROOT/st" "$ROOT/st.sig"
ok "periodic run (no --fetched) skips the main check -> ok" '[ "$(f ota_sig)" = ok ]'

# --- tools and files
mkroot; mkrepo; release_to 1.0.1; node_pull
mkdir -p "$ROOT/nokeygen"; for b in /usr/local/bin/* /usr/bin/* /bin/*; do
  n=$(basename "$b"); [ "$n" = ssh-keygen ] || [ -e "$ROOT/nokeygen/$n" ] || ln -s "$b" "$ROOT/nokeygen/$n"; done
out=$(PATH="$ROOT/nokeygen" PILNK_DIR="$HOME/pilnk" /bin/bash "$CHK" "$ROOT/st" "$ROOT/st.sig" 2>/dev/null)
ok "no ssh-keygen on PATH -> no_tool" '[ "$(f ota_sig)" = no_tool ]'
rm -f "$HOME/pilnk/release_signers"; chk "$ROOT/st" "$ROOT/st.sig"
ok "release_signers missing -> error" '[ "$(f ota_sig)" = error ] && [ "$(f ota_sig_run)" = none ]'

# --- signed but malformed statements (R3): parse failures are not_on_main, git never sees them
mkroot; mkrepo; release_to 1.0.1; node_pull; c=$(git -C "$ROOT/up" rev-parse HEAD)
{ statement 1.0.1 "$c"; echo extra; } > "$ROOT/st"; sign aj "$ROOT/st"; chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "signed but malformed statement (extra line) -> not_on_main" '[ "$(f ota_sig)" = not_on_main ]'
statement 1.0.1 "$c" | sed 's/$/\r/' > "$ROOT/st"; sign aj "$ROOT/st"; chk "$ROOT/st" "$ROOT/st.sig"
ok "signed but CRLF statement -> not_on_main (periodic too)" '[ "$(f ota_sig)" = not_on_main ]'
printf 'pilnk-release 1\nversion 1.0.1\ncommit %s' "$c" > "$ROOT/st"; sign aj "$ROOT/st"; chk "$ROOT/st" "$ROOT/st.sig"
ok "signed but no final newline -> not_on_main" '[ "$(f ota_sig)" = not_on_main ]'
fakebin git 'exec "$REALGIT" "$@"'
printf 'pilnk-release 1\nversion 1.0.1\ncommit --upload-pack=touch%%20/tmp/x\n' > "$ROOT/st"; sign aj "$ROOT/st"
chk --fetched "$ROOT/st" "$ROOT/st.sig"
ok "signed commit like --upload-pack=x never reaches git" '[ "$(f ota_sig)" = not_on_main ] && ! grep -q upload-pack "$CALLS"'

# --- run states
mkroot; mkrepo; release_to 1.0.1; chk "$ROOT/st" "$ROOT/st.sig"
ok "run state behind (node 1.0.0, signed 1.0.1)" '[ "$(f ota_sig)" = ok ] && [ "$(f ota_sig_run)" = behind ]'
node_pull; ( cd "$ROOT/up" && echo 1.0.2 > VERSION && G commit -qam v1.0.2 ); node_pull; chk "$ROOT/st" "$ROOT/st.sig"
ok "run state ahead (newer local commit)" '[ "$(f ota_sig_run)" = ahead ]'
mkroot; mkrepo; release_to 1.0.1; node_pull; echo 'print(2)' > "$HOME/pilnk/app.py"; chk "$ROOT/st" "$ROOT/st.sig"
ok "run state dirty (tracked file edited)" '[ "$(f ota_sig_run)" = dirty ]'
mkroot; mkrepo; release_to 1.6.28; node_pull; release_to 1.6.28.1; chk "$ROOT/st" "$ROOT/st.sig"
ok "4-part versions compare (node 1.6.28, signed 1.6.28.1) -> behind" '[ "$(f ota_sig_run)" = behind ]'
mkroot; mkrepo; release_to 1.6.9; cp "$ROOT/st" "$ROOT/st9"; cp "$ROOT/st.sig" "$ROOT/st9.sig"
( cd "$ROOT/up" && echo 1.6.10 > VERSION && G commit -qam v1.6.10 ); node_pull; chk "$ROOT/st9" "$ROOT/st9.sig"
ok "numeric compare (node 1.6.10, signed 1.6.9) -> ahead" '[ "$(f ota_sig_run)" = ahead ]'

# --- usage
bash "$CHK" >/dev/null 2>&1; RC=$?
ok "no arguments -> exit 2" '[ "$RC" = 2 ]'
