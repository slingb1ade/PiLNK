#!/bin/bash
# pilnk-sign — sign a PiLNK release on AJ's laptop (signed-releases spec 5.3).
#
#   pilnk-sign --init                           once: create the two release keys
#   pilnk-sign <version> <commit> [--diff]      each release, signed with the YubiKey
#   pilnk-sign <version> <commit> --backup      only if the YubiKey is lost or broken
#
# Install a copy as ~/.local/bin/pilnk-sign, outside the PiLNK folder, and update that
# copy only on purpose. This file holds no secrets and never prints a private key.
#
# Settings (environment, defaults in brackets):
#   PILNK_SIGN_REPO_URL     [https://github.com/slingb1ade/PiLNK.git]
#   PILNK_SIGN_CACHE        [~/.cache/pilnk-sign/repo]   its own clone, never your work tree
#   PILNK_SIGN_VERSION_URL  [https://pilnk.io/api/version.php]
#   PILNK_SIGN_KEY          [~/.ssh/pilnk_release_yk.pub]  YubiKey key, used through ssh-agent
#   PILNK_SIGN_BACKUP_KEY   [~/.ssh/pilnk_release_backup]  never added to any agent
#   PILNK_SIGN_DEST         [aj@66.226.148.29:/var/www/pilnkio/_private/releases/]
#   PILNK_SIGN_SCP          [scp]
#   PILNK_SIGN_FIXTURE      [~/pilnk-sign-fixture]   --init writes its test signature here
#   PILNK_SIGN_PKCS11       [first libykcs11 found]  the YubiKey's PKCS#11 module

NS=pilnk-release
VERSION_RE='^[0-9]+(\.[0-9]+){1,3}$'
ZERO_COMMIT=0000000000000000000000000000000000000000

cfg() {
  REPO_URL="${PILNK_SIGN_REPO_URL:-https://github.com/slingb1ade/PiLNK.git}"
  CACHE="${PILNK_SIGN_CACHE:-$HOME/.cache/pilnk-sign/repo}"
  VERSION_URL="${PILNK_SIGN_VERSION_URL:-https://pilnk.io/api/version.php}"
  KEY="${PILNK_SIGN_KEY:-$HOME/.ssh/pilnk_release_yk.pub}"
  BACKUP_KEY="${PILNK_SIGN_BACKUP_KEY:-$HOME/.ssh/pilnk_release_backup}"
  DEST="${PILNK_SIGN_DEST:-aj@66.226.148.29:/var/www/pilnkio/_private/releases/}"
  SCP="${PILNK_SIGN_SCP:-scp}"
  FIXTURE="${PILNK_SIGN_FIXTURE:-$HOME/pilnk-sign-fixture}"
}

die() { local code="$1"; shift; echo "pilnk-sign: $*" >&2; exit "$code"; }

usage() {
  cat >&2 <<'EOF'
usage: pilnk-sign --init
       pilnk-sign <version> <commit> [--diff] [--backup]
EOF
  exit 2
}

# Files that run as root on every node (spec 5.3 step 4). release-check.sh is added:
# update.sh runs it as root and it decides whether a release is trusted.
is_root_relevant() {
  case "$1" in
    image/firstboot/*|*.service|*.timer|*.rules) return 0 ;;
  esac
  case "${1##*/}" in
    update.sh|release_signers|release_policy|release-check.sh|bootstrap-selfheal.sh|\
    sdr-recover.sh|pilnk-dashboard-watchdog.sh|pilnkradio-install.sh|denoise-install.sh|\
    datalink-install.sh|install.sh|pilnk-hw-setup.sh|pilnk-sign.sh) return 0 ;;
  esac
  return 1
}

ask() {  # ask PROMPT -> true only for exactly "y"
  local a=""
  printf '%s' "$1"
  IFS= read -r a || a=""
  [ -t 0 ] || echo
  [ "$a" = y ]
}

gitc() { git -C "$CACHE" --no-pager "$@"; }

fetch_main() {
  mkdir -p "$(dirname "$CACHE")" || return 1
  if gitc rev-parse --git-dir >/dev/null 2>&1; then
    gitc remote set-url origin "$REPO_URL" && gitc fetch -q --prune origin
  else
    git clone -q --no-checkout "$REPO_URL" "$CACHE"
  fi
}

release_field() {  # release_field JSONFILE FIELD -> the field of .release ("" if none); exit 2 if not JSON
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        r = json.load(f).get('release')
except Exception:
    sys.exit(2)
if isinstance(r, dict) and isinstance(r.get(sys.argv[2]), str):
    sys.stdout.write(r[sys.argv[2]])
PY
}

key_in_agent() {  # key_in_agent PUBFILE -> the agent holds that key
  local blob
  blob="$(awk '{print $2; exit}' "$1" 2>/dev/null)"
  [ -n "$blob" ] || return 1
  ssh-add -L 2>/dev/null | awk '{print $2}' | grep -qxF "$blob"
}

pkcs11_module() {
  local m
  if [ -n "${PILNK_SIGN_PKCS11:-}" ]; then
    [ -e "$PILNK_SIGN_PKCS11" ] && echo "$PILNK_SIGN_PKCS11"
    return
  fi
  for m in /usr/lib/x86_64-linux-gnu/libykcs11.so /usr/lib/x86_64-linux-gnu/libykcs11.so.2 \
           /usr/lib/aarch64-linux-gnu/libykcs11.so /usr/lib/aarch64-linux-gnu/libykcs11.so.2 \
           /usr/local/lib/libykcs11.so /usr/local/lib/libykcs11.dylib /opt/homebrew/lib/libykcs11.dylib; do
    if [ -e "$m" ]; then echo "$m"; return 0; fi
  done
  return 1
}

signer_line() {  # signer_line PUBFILE COMMENT -> one release_signers line (namespaces option only)
  printf 'release namespaces="%s" %s %s\n' "$NS" "$(awk '{print $1" "$2; exit}' "$1")" "$2"
}

# ---------------------------------------------------------------------- sign one release
cmd_sign() {
  local ver="" commit="" backup=0 showdiff=0 a
  for a in "$@"; do
    case "$a" in
      --backup) backup=1 ;;
      --diff) showdiff=1 ;;
      -*) usage ;;
      *) if [ -z "$ver" ]; then ver="$a"; elif [ -z "$commit" ]; then commit="$a"; else usage; fi ;;
    esac
  done
  [ -n "$ver" ] && [ -n "$commit" ] || usage
  [[ "$ver" =~ $VERSION_RE ]] || die 2 "version must look like 1.6.28, not '$ver'"
  [[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || die 2 "commit must be a lowercase hex hash, not '$commit'"
  T="$(mktemp -d)" || die 1 "no temp dir"
  trap 'rm -rf "$T"' EXIT

  # 1-2. GitHub main, and the commit + VERSION it must match
  fetch_main || die 1 "could not fetch $REPO_URL into $CACHE"
  local head full atver
  head="$(gitc rev-parse --verify --quiet refs/remotes/origin/main)" || die 1 "no origin/main in $CACHE"
  full="$(gitc rev-parse --verify --quiet "$commit^{commit}" 2>/dev/null)"
  [ "$full" = "$head" ] || die 1 "$commit is not the current origin/main head ($head). Refusing."
  atver="$(gitc show "$head:VERSION" 2>/dev/null | tr -d '[:space:]')"
  [ "$atver" = "$ver" ] || die 1 "VERSION at $head is '$atver', not $ver. Refusing."

  # 3. what changed since the last signed release on pilnk.io
  curl -fsS --max-time 20 -o "$T/before.json" "$VERSION_URL" || die 3 "could not read $VERSION_URL"
  local pv pc base="" rewritten=0
  pv="$(release_field "$T/before.json" version)" || die 3 "$VERSION_URL did not answer with JSON"
  pc="$(release_field "$T/before.json" commit)"
  if [[ "$pv" =~ $VERSION_RE ]]; then
    if [ "$pv" = "$ver" ] || [ "$(printf '%s\n%s\n' "$pv" "$ver" | sort -V | tail -n 1)" != "$ver" ]; then
      die 1 "$ver is not newer than the last signed release $pv. Refusing."
    fi
    if [[ "$pc" =~ ^[0-9a-f]{40}$ ]] && gitc merge-base --is-ancestor "$pc" "$head" 2>/dev/null; then
      base="$pc"
    else
      rewritten=1
    fi
    echo "Last signed release on pilnk.io: $pv ($pc)"
  else
    pv=""
    echo "No signed release on pilnk.io yet."
  fi
  if [ -z "$base" ]; then  # fall back to the previous VERSION bump, else the whole history
    base="$(gitc log --format=%H -n 2 "$head" -- VERSION | sed -n 2p)"
  fi
  local range="$head" from
  from="$base"
  if [ -n "$base" ]; then range="$base..$head"; else from="$(git hash-object -t tree /dev/null)"; fi

  echo "Signing PiLNK $ver = $head"
  [ "$rewritten" = 1 ] && echo "HISTORY CHANGED: the last signed commit $pc is not in main's history."
  echo
  if [ -n "$base" ]; then echo "Commits since ${base:0:12}:"; else echo "Commits (whole history):"; fi
  gitc log --no-color --format='  %h %s' "$range"
  echo
  echo "Files changed:"
  local flagged=() p ad dl
  while IFS=$'\t' read -r ad dl p; do
    [ -n "$p" ] || continue
    printf '  +%s -%s %s\n' "$ad" "$dl" "$p"
    if is_root_relevant "$p"; then flagged+=("$p"); fi
  done < <(gitc diff --no-color --no-renames --numstat "$from" "$head")

  # 4. root-relevant changes (or rewritten history) need a second y
  if [ "${#flagged[@]}" -gt 0 ] || [ "$rewritten" = 1 ]; then
    echo
    if [ "${#flagged[@]}" -gt 0 ]; then
      echo "ROOT-RELEVANT files changed (these run as root on every node):"
      for p in "${flagged[@]}"; do echo "  ! $p"; done
      if [ "$showdiff" = 1 ]; then
        gitc diff --no-color --no-renames "$from" "$head" -- "${flagged[@]}"
      else
        echo "  (run again with --diff to read these changes in full)"
      fi
    fi
    ask "Have you read these changes? [y/N] " || die 1 "Stopped. Nothing signed."
  fi

  # 5. the question
  ask "Sign PiLNK $ver? [y/N] " || die 1 "Stopped. Nothing signed."

  # 6. sign the exact statement (spec 5.2)
  printf 'pilnk-release 1\nversion %s\ncommit %s\n' "$ver" "$head" > "$T/$ver.txt"
  if [ "$backup" = 1 ]; then
    [ -f "$BACKUP_KEY" ] || die 1 "backup key $BACKUP_KEY not found"
    echo "Signing with the BACKUP key (it will ask for its passphrase)."
    SSH_AUTH_SOCK= ssh-keygen -q -Y sign -f "$BACKUP_KEY" -n "$NS" "$T/$ver.txt" >/dev/null \
      || die 1 "signing failed. Nothing uploaded."
  else
    [ -f "$KEY" ] || die 1 "$KEY not found (run pilnk-sign --init first)"
    if ! key_in_agent "$KEY"; then
      die 1 "The YubiKey release key is not in your ssh-agent. Plug the YubiKey in, run
    ssh-add -s $(pkcs11_module || echo /path/to/libykcs11.so)
then run pilnk-sign again. Nothing signed."
    fi
    echo "Touch the YubiKey when it blinks (type its PIN if asked)."
    ssh-keygen -q -Y sign -f "$KEY" -n "$NS" "$T/$ver.txt" >/dev/null \
      || die 1 "signing failed (YubiKey removed, not touched, or wrong PIN). Nothing uploaded."
  fi
  mv "$T/$ver.txt.sig" "$T/$ver.sig" || die 1 "no signature written. Nothing uploaded."

  # 7. check it against release_signers at that commit (catches the wrong key)
  gitc show "$head:release_signers" > "$T/release_signers" 2>/dev/null \
    || die 1 "release_signers is missing at $head. Nothing uploaded."
  ssh-keygen -Y verify -f "$T/release_signers" -I release -n "$NS" -s "$T/$ver.sig" \
    < "$T/$ver.txt" >/dev/null 2>&1 \
    || die 1 "The signature does not verify against release_signers at $head (wrong key?). Nothing uploaded."

  # 8. upload; the web server reads these as group/other, whatever this umask is
  chmod 0644 "$T/$ver.txt" "$T/$ver.sig"
  echo "Uploading $ver.txt and $ver.sig to $DEST"
  "$SCP" -q "$T/$ver.txt" "$T/$ver.sig" "$DEST" || die 3 "upload failed. Nothing published; run again."

  # 9. pilnk.io must now serve exactly these bytes
  curl -fsS --max-time 20 -o "$T/after.json" "$VERSION_URL" \
    || die 3 "uploaded, but could not read $VERSION_URL to confirm."
  python3 - "$T/after.json" "$ver" "$head" "$T/$ver.txt" "$T/$ver.sig" <<'PY' \
    || die 3 "uploaded, but $VERSION_URL does not show PiLNK $ver yet. Check _private/releases on syd1."
import json, sys
j, ver, head, st, sig = sys.argv[1:6]
try:
    r = json.load(open(j, encoding='utf-8')).get('release') or {}
    ok = (r.get('version') == ver and r.get('commit') == head
          and r.get('statement', '').encode() == open(st, 'rb').read()
          and r.get('sig', '').encode() == open(sig, 'rb').read())
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
  echo "done: PiLNK $ver ($head) is signed and published."
}

# ---------------------------------------------------------------------- one-time key set-up
# The YubiKey half. Ruling R10/P1: a PIV key in slot 9c, generated on the YubiKey (it can
# never leave), touch ALWAYS + PIN ONCE (both fixed at generation), used through ssh-agent
# with the PKCS#11 module. If the Task 1 spike picks another mechanism, only this changes.
init_hw_key() {  # init_hw_key MODULE PEM_OUT
  ykman piv keys generate --algorithm ECCP256 --pin-policy ONCE --touch-policy ALWAYS 9c "$2" || return 1
  ykman piv certificates generate --subject "CN=pilnk-release" 9c "$2" || return 1
  ssh-add -s "$1" || return 1
}

cmd_init() {
  local f mod yk type blob
  for f in "$KEY" "$BACKUP_KEY" "$BACKUP_KEY.pub"; do
    [ -e "$f" ] && die 1 "$f already exists. Refusing to overwrite release keys."
  done
  command -v ykman >/dev/null 2>&1 || die 1 "ykman not found (install yubikey-manager)."
  mod="$(pkcs11_module)" || die 1 "no YubiKey PKCS#11 module found (install ykcs11, or set PILNK_SIGN_PKCS11)."
  ykman info || die 1 "No YubiKey found. Plug it in and run again."
  T="$(mktemp -d)" || die 1 "no temp dir"
  trap 'rm -rf "$T"' EXIT
  mkdir -p "$(dirname "$KEY")" "$(dirname "$BACKUP_KEY")" || die 1 "cannot create key folders"

  echo
  echo "1/4 Backup key. Choose a strong passphrase; it is stored with the key, offline."
  ssh-keygen -t ed25519 -a 100 -C pilnk-release-backup -f "$BACKUP_KEY" >/dev/null \
    || die 1 "backup key not created."
  if ssh-keygen -y -P '' -f "$BACKUP_KEY" >/dev/null 2>&1; then
    rm -f "$BACKUP_KEY" "$BACKUP_KEY.pub"
    die 1 "the backup key must have a passphrase. Removed it; run pilnk-sign --init again."
  fi

  echo
  echo "2/4 YubiKey key. ykman asks for the PIV management key and PIN; touch when it blinks."
  init_hw_key "$mod" "$T/yk.pem" || die 1 "the YubiKey key was not created and loaded."
  yk="$(ssh-keygen -i -m PKCS8 -f "$T/yk.pem")" || die 1 "could not read the new YubiKey public key."
  read -r type blob _ <<<"$yk"
  ssh-keygen -D "$mod" 2>/dev/null | awk '{print $2}' | grep -qxF "$blob" \
    || die 1 "the new key is not visible through $mod."
  printf '%s %s pilnk-release-yubikey\n' "$type" "$blob" > "$KEY"

  echo
  echo "3/4 Test signature over a fixed statement. Touch the YubiKey when it blinks."
  mkdir -p "$FIXTURE" || die 1 "cannot create $FIXTURE"
  printf 'pilnk-release 1\nversion 0.0.0\ncommit %s\n' "$ZERO_COMMIT" > "$FIXTURE/statement"
  { signer_line "$KEY" pilnk-release-yubikey; signer_line "$BACKUP_KEY.pub" pilnk-release-backup; } \
    > "$FIXTURE/release_signers"
  key_in_agent "$KEY" || die 1 "the YubiKey key is not in your ssh-agent."
  rm -f "$FIXTURE/statement.sig"
  ssh-keygen -q -Y sign -f "$KEY" -n "$NS" "$FIXTURE/statement" >/dev/null \
    || die 1 "the test signature failed (no touch?)."
  ssh-keygen -Y verify -f "$FIXTURE/release_signers" -I release -n "$NS" \
    -s "$FIXTURE/statement.sig" < "$FIXTURE/statement" >/dev/null 2>&1 \
    || die 1 "the test signature does not verify."

  echo
  echo "4/4 Done. These two lines go in release_signers at the top of the PiLNK repo:"
  echo
  cat "$FIXTURE/release_signers"
  echo
  echo "Test fixture (statement, signature, signers): $FIXTURE"
  echo
  echo "NOW: copy the backup private key $BACKUP_KEY into Bitwarden and one other"
  echo "safe place, then delete it from this laptop:  rm $BACKUP_KEY"
  echo "Keep $BACKUP_KEY.pub. Never add the backup key to ssh-agent."
}

main() {
  set -u
  cfg
  case "${1:-}" in
    --init) [ "$#" -eq 1 ] || usage; cmd_init ;;
    ""|-h|--help) usage ;;
    *) cmd_sign "$@" ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
