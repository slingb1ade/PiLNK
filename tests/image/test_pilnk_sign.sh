#!/bin/bash
# tools/pilnk-sign.sh — AJ's release signer (signed-releases plan Task 6, spec 5.3).
# Throwaway keys stand in for the YubiKey: a real ssh-agent holds the "YubiKey" key.
# GitHub is a local repo, pilnk.io is a file:// JSON, syd1 is a folder reached by a fake
# scp that also rebuilds the fake version.php answer. No test reaches the network.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/sigtest-lib.sh"
SIGN="$REPO/tools/pilnk-sign.sh"
REAL_KEYGEN="$(command -v ssh-keygen)"; REAL_ADD="$(command -v ssh-add)"
export REAL_KEYGEN REAL_ADD

# one agent + throwaway keys for the whole file
KROOT="$(mktemp -d /tmp/pilnk-sign-keys.XXXXXX)"; _ROOTS+=("$KROOT"); export KROOT
for k in yk backup stranger; do "$REAL_KEYGEN" -q -t ed25519 -N '' -C "$k" -f "$KROOT/$k"; done
"$REAL_KEYGEN" -q -t ecdsa -b 256 -N '' -C yk2 -f "$KROOT/yk2"          # the --init "PIV" key
"$REAL_KEYGEN" -e -m PKCS8 -f "$KROOT/yk2.pub" > "$KROOT/yk2.pem"
eval "$(ssh-agent -s)" >/dev/null
trap '_rm_roots; ssh-agent -k >/dev/null 2>&1' EXIT
"$REAL_ADD" -q "$KROOT/yk" "$KROOT/stranger" 2>/dev/null
AGENT_SOCK="$SSH_AUTH_SOCK"

sl() { printf 'release namespaces="pilnk-release" %s %s\n' "$(cut -d' ' -f1,2 "$KROOT/$1.pub")" "$1"; }

# ssh-keygen stand-in: logs SSH_AUTH_SOCK on -Y sign; fakes -D (PKCS#11 listing);
# gives --init's backup key a test passphrase (or none, when $ROOT/.nopass exists)
keygen_wrapper() {
  fakebin ssh-keygen '
case " $* " in *" -Y sign "*) echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK-}" >> "$CALLS";; esac
if [ "$1" = "-D" ]; then echo "$(cut -d" " -f1,2 "$KROOT/yk2.pub") Public key for Digital Signature"; exit 0; fi
for a in "$@"; do if [ "$a" = "-t" ]; then
  if [ -e "$ROOT/.nopass" ]; then exec "$REAL_KEYGEN" "$@" -N ""; else exec "$REAL_KEYGEN" "$@" -N "test-pass"; fi
fi; done
exec "$REAL_KEYGEN" "$@"'
}

# scenario [FILE] [PREV_VERSION|null] [PREV_COMMIT]: fake GitHub with commits one, two
# (touches FILE, default app.py), v1.0.1; pilnk.io's last signed release = PREV at "one".
scenario() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME" "$ROOT/syd1"
  export SSH_AUTH_SOCK="$AGENT_SOCK"
  local f="${1:-app.py}" prev="${2:-1.0.0}"
  G init -q -b main "$ROOT/up"
  ( cd "$ROOT/up" && echo 1.0.0 > VERSION && echo 'print(1)' > app.py && echo 'echo hi' > update.sh \
    && { sl yk; sl backup; } > release_signers && G add -A && G commit -qm one )
  ONE="$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)"
  ( cd "$ROOT/up" && mkdir -p "$(dirname "$f")" && echo "# changed" >> "$f" && G add -A && G commit -qm two )
  ( cd "$ROOT/up" && echo 1.0.1 > VERSION && G commit -qam v1.0.1 )
  HEADC="$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)"
  statement 1.0.1 "$HEADC" > "$ROOT/expected.txt"
  python3 - "$ROOT/version.json" "$prev" "${3:-$ONE}" <<'PY'
import json, sys
out, v, c = sys.argv[1:4]
rel = None if v == 'null' else {'version': v, 'commit': c,
      'statement': 'pilnk-release 1\nversion %s\ncommit %s\n' % (v, c), 'sig': 'x\n'}
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
         PILNK_SIGN_VERSION_URL="file://$ROOT/version.json" PILNK_SIGN_KEY="$KROOT/yk.pub" \
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
ok "normal signing goes through the agent" 'grep -q "^SSH_AUTH_SOCK=/" "$CALLS" && ! grep -q "^SSH_AUTH_SOCK=$" "$CALLS"'
scenario; ANS='y\n'; OUT="$(printf 'y\n' | (umask 077; bash "$SIGN" 1.0.1 "$HEADC") 2>&1)"; RC=$?
ok "uploaded files are readable by the web server even under umask 077" \
   '[ "$RC" = 0 ] && [ "$(stat -c %a "$ROOT/syd1/1.0.1.txt")" = 644 ] && [ "$(stat -c %a "$ROOT/syd1/1.0.1.sig")" = 644 ]'
scenario; export PILNK_SIGN_KEY="$KROOT/stranger.pub"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "self-verify catches a key not in release_signers" '[ "$RC" != 0 ] && grep -q "does not verify" <<<"$OUT" && ! uploaded'
scenario; ANS='y\n'; OUT="$(printf 'y\n' | SSH_AUTH_SOCK= bash "$SIGN" 1.0.1 "$HEADC" 2>&1)"; RC=$?
ok "YubiKey key not in the agent -> stops and says so" '[ "$RC" != 0 ] && grep -q "not in your ssh-agent" <<<"$OUT" && ! uploaded'
scenario; ANS='y\n'; run_sign 1.0.1 "$HEADC" --backup
ok "--backup signs with the backup key, agent unset" '[ "$RC" = 0 ] && grep -q "SSH_AUTH_SOCK=$" "$CALLS"'
scenario; touch "$ROOT/.scp_noupdate"; ANS='y\n'; run_sign 1.0.1 "$HEADC"
ok "pilnk.io not showing the release -> not done, non-zero" '[ "$RC" != 0 ] && ! grep -q "done" <<<"$OUT"'

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

# ---- --init ------------------------------------------------------------------------------
init_scenario() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME"
  export SSH_AUTH_SOCK="$AGENT_SOCK"
  unset PILNK_SIGN_KEY PILNK_SIGN_BACKUP_KEY
  : > "$ROOT/libykcs11.so"; export PILNK_SIGN_PKCS11="$ROOT/libykcs11.so"
  fakebin ykman 'if [ "$1" = info ]; then [ -e "$ROOT/.no_yk" ] && exit 1; echo "Device type: YubiKey 5"; exit 0; fi
if [ "$1 $2 $3" = "piv keys generate" ]; then cp "$KROOT/yk2.pem" "${@: -1}"; fi
exit 0'
  fakebin ssh-add 'if [ "$1" = "-s" ]; then exec "$REAL_ADD" -q "$KROOT/yk2" 2>/dev/null; fi
exec "$REAL_ADD" "$@"'
  keygen_wrapper
}
run_init() { OUT="$(bash "$SIGN" --init </dev/null 2>&1)"; RC=$?; }

init_scenario; run_init
ok "--init succeeds" '[ "$RC" = 0 ]'
ok "--init never prints private key material" '! grep -q "PRIVATE KEY" <<<"$OUT"'
ok "--init prints two namespaces-only signer lines" '[ "$(grep -c "^release namespaces=\"pilnk-release\" " <<<"$OUT")" = 2 ]'
ok "--init runs the hardware steps in order (ykman info, PIV generate, certificate, ssh-add -s, ssh-keygen -D)" '
  [ "$(grep -E "^(ykman info|ykman piv keys generate --algorithm ECCP256 --pin-policy ONCE --touch-policy ALWAYS 9c |ykman piv certificates generate |ssh-add -s |ssh-keygen -D )" "$CALLS" | cut -c1-12 | tr "\n" "|")" = "ykman info|ykman piv ke|ykman piv ce|ssh-add -s /|ssh-keygen -|" ]'
ok "--init writes the YubiKey public key file" '[ "$(cut -d" " -f1,2 "$HOME/.ssh/pilnk_release_yk.pub")" = "$(cut -d" " -f1,2 "$KROOT/yk2.pub")" ]'
ok "--init fixture: exact fixed statement" '[ "$(cat "$HOME/pilnk-sign-fixture/statement")" = "$(printf "pilnk-release 1\nversion 0.0.0\ncommit %s" 0000000000000000000000000000000000000000)" ]'
ok "--init fixture: the YubiKey signature verifies on OpenSSH 8.9" \
   '"$REAL_KEYGEN" -Y verify -f "$HOME/pilnk-sign-fixture/release_signers" -I release -n pilnk-release -s "$HOME/pilnk-sign-fixture/statement.sig" < "$HOME/pilnk-sign-fixture/statement" >/dev/null 2>&1'
ok "--init never loads the backup key into an agent" '! grep -q "ssh-add.*backup" "$CALLS" && ! "$REAL_ADD" -L | grep -qF "$(cut -d" " -f2 "$HOME/.ssh/pilnk_release_backup.pub")"'
ok "--init tells AJ to move the backup key offline and delete it" 'grep -q "Bitwarden" <<<"$OUT" && grep -q "delete" <<<"$OUT"'
run_init
ok "--init refuses to overwrite existing keys" '[ "$RC" != 0 ]'
rm -f "$HOME/.ssh/pilnk_release_backup"; run_init     # AJ did as told: backup private key moved off the laptop
ok "--init after the backup key was moved offline still refuses, YubiKey slot untouched" \
   '[ "$RC" != 0 ] && grep -q "already exists" <<<"$OUT" && [ "$(grep -c "^ykman piv keys generate" "$CALLS")" = 1 ]'

init_scenario; touch "$ROOT/.nopass"; run_init
ok "--init refuses a backup key with no passphrase (removes it, YubiKey untouched)" \
   '[ "$RC" != 0 ] && [ ! -e "$HOME/.ssh/pilnk_release_backup" ] && ! grep -q "^ykman piv" "$CALLS"'
init_scenario; touch "$ROOT/.no_yk"; run_init
ok "--init with no YubiKey stops before creating any key" '[ "$RC" != 0 ] && grep -q "No YubiKey" <<<"$OUT" && [ ! -e "$HOME/.ssh/pilnk_release_backup" ]'
