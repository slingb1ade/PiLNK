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
#   PILNK_SIGN_KEY          [~/.ssh/pilnk_release_yk]  the YubiKey's FIDO key file (made by --init;
#                           useless without the YubiKey itself) and its .pub beside it
#   PILNK_SIGN_BACKUP_KEY   [~/.ssh/pilnk_release_backup]  never added to any agent
#   PILNK_SIGN_DEST         [first line of ~/.config/pilnk-sign/dest]  where the signed pair is
#                           copied (user@host:/path/). Kept on the laptop, never in this repo:
#                           pilnk.io sits behind Cloudflare and the server address stays private.
#   PILNK_SIGN_SCP          [scp]
#   PILNK_SIGN_FIXTURE      [~/pilnk-sign-fixture]   --init writes its test signature here
#   PILNK_SIGN_RECORD       [${XDG_STATE_HOME:-~/.local/state}/pilnk-sign/signed]
#                           one "version commit" line per release you signed; the review
#                           starts from here, not from what pilnk.io says

NS=pilnk-release
VERSION_RE='^[0-9]+(\.[0-9]+){1,3}$'
ZERO_COMMIT=0000000000000000000000000000000000000000

cfg() {
  REPO_URL="${PILNK_SIGN_REPO_URL:-https://github.com/slingb1ade/PiLNK.git}"
  CACHE="${PILNK_SIGN_CACHE:-$HOME/.cache/pilnk-sign/repo}"
  VERSION_URL="${PILNK_SIGN_VERSION_URL:-https://pilnk.io/api/version.php}"
  KEY="${PILNK_SIGN_KEY:-$HOME/.ssh/pilnk_release_yk}"
  BACKUP_KEY="${PILNK_SIGN_BACKUP_KEY:-$HOME/.ssh/pilnk_release_backup}"
  DEST_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/pilnk-sign/dest"
  DEST="${PILNK_SIGN_DEST:-}"
  if [ -z "$DEST" ] && [ -r "$DEST_FILE" ]; then
    DEST="$(grep -v '^[[:space:]]*#' "$DEST_FILE" | grep -m1 -v '^[[:space:]]*$' | tr -d '[:space:]')"
  fi
  SCP="${PILNK_SIGN_SCP:-scp}"
  FIXTURE="${PILNK_SIGN_FIXTURE:-$HOME/pilnk-sign-fixture}"
  RECORD="${PILNK_SIGN_RECORD:-${XDG_STATE_HOME:-$HOME/.local/state}/pilnk-sign/signed}"
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
# Part B adds what root runs or renders from its own signed copy: rootside/*
# (templates, MANIFEST, rootsync-run), root_policy, rootsync.sh, the pinned
# rtl-sdr-blog commit, and every rootside/MANIFEST script.
is_root_relevant() {
  case "$1" in
    image/firstboot/*|rootside/*|*.service|*.timer|*.rules|pilnkradio/rtl-sdr-blog.commit) return 0 ;;
  esac
  case "${1##*/}" in
    update.sh|release_signers|release_policy|release-check.sh|bootstrap-selfheal.sh|\
    sdr-recover.sh|pilnk-dashboard-watchdog.sh|pilnkradio-install.sh|denoise-install.sh|\
    datalink-install.sh|install.sh|pilnk-hw-setup.sh|pilnk-sign.sh|\
    root_policy|rootsync.sh|hwtier.sh|pilnk-radio-watchdog.sh) return 0 ;;
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

# GIT_LITERAL_PATHSPECS: a file named ':(exclude,glob)**/update.sh' is a file name, never
# an instruction that hides other files from the review (final review I3).
gitc() { GIT_LITERAL_PATHSPECS=1 git -C "$CACHE" --no-pager "$@"; }

# Everything from GitHub passes through here before it reaches AJ's terminal: control
# characters and bidi overrides are shown as <1B> / <U+202E>, never obeyed (final review I3).
safe_show() {
  python3 -I -c '
import sys
BIDI = set(range(0x200e, 0x2010)) | set(range(0x202a, 0x202f)) | set(range(0x2066, 0x206a))
out = []
for ch in sys.stdin.buffer.read().decode("utf-8", "backslashreplace"):
    o = ord(ch)
    if (o < 0x20 and ch not in "\t\n") or o == 0x7f or 0x80 <= o <= 0x9f:
        out.append("<%02X>" % o)
    elif o in BIDI:
        out.append("<U+%04X>" % o)
    else:
        out.append(ch)
sys.stdout.buffer.write("".join(out).encode("utf-8"))'
}

fetch_main() {
  mkdir -p "$(dirname "$CACHE")" || return 1
  if gitc rev-parse --git-dir >/dev/null 2>&1; then
    gitc remote set-url origin "$REPO_URL" && gitc fetch -q --prune origin
  else
    git clone -q --no-checkout "$REPO_URL" "$CACHE"
  fi
}

release_parts() {  # release_parts JSONFILE DIR -> none | bad | "ok VERSION COMMIT"; exit 2 if not JSON
  # "ok" writes DIR/prev.txt + DIR/prev.sig; VERSION and COMMIT come from the statement itself.
  python3 -I - "$1" "$2" <<'PY'
import json, os, re, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        r = json.load(f).get('release')
except Exception:
    sys.exit(2)
if r is None:
    print('none'); sys.exit(0)
st = r.get('statement') if isinstance(r, dict) else None
sig = r.get('sig') if isinstance(r, dict) else None
m = re.fullmatch(r'pilnk-release 1\nversion ([0-9]+(?:\.[0-9]+){1,3})\ncommit ([0-9a-f]{40})\n', st) \
    if isinstance(st, str) else None
if not m or not isinstance(sig, str) or not sig:
    print('bad'); sys.exit(0)
with open(os.path.join(sys.argv[2], 'prev.txt'), 'wb') as f:
    f.write(st.encode('utf-8'))
with open(os.path.join(sys.argv[2], 'prev.sig'), 'wb') as f:
    f.write(sig.encode('utf-8'))
print('ok', m.group(1), m.group(2))
PY
}

local_signers() {  # local_signers OUT -> signer lines for AJ's OWN public keys (never GitHub's file)
  : > "$1"
  if [ -f "$KEY.pub" ]; then signer_line "$KEY.pub" pilnk-release-yubikey >> "$1"; fi
  if [ -f "$BACKUP_KEY.pub" ]; then signer_line "$BACKUP_KEY.pub" pilnk-release-backup >> "$1"; fi
  return 0
}

last_record() {  # last_record -> "VERSION COMMIT" of the last release AJ signed here ("" if none)
  [ -f "$RECORD" ] || return 0
  grep -E '^[0-9]+(\.[0-9]+){1,3} [0-9a-f]{40}$' "$RECORD" | tail -n 1
}

not_newer() {  # not_newer NEW OLD -> NEW is not newer than OLD
  [ "$1" = "$2" ] || [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" != "$1" ]
}

# A YubiKey (FIDO) signature carries a signed "user present" flag. OpenSSH 8.9 ignores it, so
# nodes check it themselves (release-check.sh) and so does this tool (Task 1, Ruling S2).
# touch_ok SIGFILE -> 0 touched (or a plain key, e.g. the backup), 1 FIDO without a touch,
# 2 not a readable signature. Same logic as release-check.sh's touch_check.
touch_ok() {
  python3 -I - "$1" <<'PY'
import base64, struct, sys
def field(b, o):
    if o + 4 > len(b):
        raise ValueError
    n = struct.unpack('>I', b[o:o + 4])[0]
    if o + 4 + n > len(b):
        raise ValueError
    return b[o + 4:o + 4 + n], o + 4 + n
try:
    lines = open(sys.argv[1], 'rb').read().decode('ascii').strip().split('\n')
    if lines[0].strip() != '-----BEGIN SSH SIGNATURE-----' or lines[-1].strip() != '-----END SSH SIGNATURE-----':
        sys.exit(2)
    b = base64.b64decode(''.join(l.strip() for l in lines[1:-1]), validate=True)
    if b[:6] != b'SSHSIG':
        sys.exit(2)
    o = 10
    for _ in range(4):
        _, o = field(b, o)
    sig, o = field(b, o)
    if o != len(b):
        sys.exit(2)
    kind, p = field(sig, 0)
    if not kind.startswith(b'sk-'):
        sys.exit(0)
    _, p = field(sig, p)
    if p + 5 != len(sig):
        sys.exit(2)
    sys.exit(0 if sig[p] & 0x01 else 1)
except SystemExit:
    raise
except Exception:
    sys.exit(2)
PY
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
  [ -n "$DEST" ] || die 1 "no upload address. Put user@host:/path/ on one line in $DEST_FILE
(it stays on this laptop, never on GitHub), or set PILNK_SIGN_DEST. Nothing signed."
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

  # 3. what changed since the last signed release. The review starts from AJ's own record
  # of what he signed, else from pilnk.io's release only if its signature checks against his
  # own public keys: a forged pilnk.io answer cannot narrow the review (final review I4).
  curl -fsS --max-time 20 -o "$T/before.json" "$VERSION_URL" || die 3 "could not read $VERSION_URL"
  local rel pv="" pc="" pstate rv="" rc="" bc="" v base="" rewritten=0 extra=0
  rel="$(release_parts "$T/before.json" "$T")" || die 3 "$VERSION_URL did not answer with JSON"
  case "$rel" in
    "ok "*)
      read -r _ pv pc <<<"$rel"
      local_signers "$T/local_signers"
      if [ -s "$T/local_signers" ] && ssh-keygen -Y verify -f "$T/local_signers" -I release -n "$NS" \
           -s "$T/prev.sig" < "$T/prev.txt" >/dev/null 2>&1 && touch_ok "$T/prev.sig"; then
        pstate=verified
      else
        pstate=unverified
      fi ;;
    bad) pstate=unverified ;;
    *) pstate=none ;;
  esac
  read -r rv rc < <(last_record) || true
  for v in "$rv" "$([ "$pstate" = verified ] && echo "$pv")"; do
    [ -n "$v" ] || continue
    not_newer "$ver" "$v" && die 1 "$ver is not newer than the last signed release $v. Refusing."
  done
  case "$pstate" in
    verified) echo "Last signed release on pilnk.io: $pv ($pc), signature checked against your keys." ;;
    unverified)
      echo "UNVERIFIED: pilnk.io shows a release whose signature does not check against your keys."
      echo "  Ignoring it. Someone may have changed pilnk.io; look at _private/releases on syd1."
      extra=1 ;;
    none) echo "No signed release on pilnk.io yet." ;;
  esac
  if [ -n "$rv" ]; then
    echo "Your record: you last signed $rv ($rc)."
    if [ "$pstate" != verified ] || [ "$rv" != "$pv" ] || [ "$rc" != "$pc" ]; then
      echo "WARNING: pilnk.io does not show the release you last signed. Reviewing from your record."
      extra=1
    fi
    bc="$rc"
  elif [ "$pstate" = verified ]; then
    bc="$pc"
  fi
  if [ -n "$bc" ]; then
    if gitc merge-base --is-ancestor "$bc" "$head" 2>/dev/null; then base="$bc"; else rewritten=1; fi
  fi
  if [ -z "$base" ]; then  # fall back to the previous VERSION bump, else the whole history
    base="$(gitc log --format=%H -n 2 "$head" -- VERSION | sed -n 2p)"
  fi
  local range="$head" from
  from="$base"
  if [ -n "$base" ]; then range="$base..$head"; else from="$(git hash-object -t tree /dev/null)"; fi

  echo "Signing PiLNK $ver = $head"
  [ "$rewritten" = 1 ] && echo "HISTORY CHANGED: the last signed commit $bc is not in main's history."
  echo
  if [ -n "$base" ]; then echo "Commits since ${base:0:12}:"; else echo "Commits (whole history):"; fi
  gitc log --no-color --format='  %h %s' "$range" | safe_show
  echo
  echo "Files changed:"
  local flagged=() p ad dl rest listing=""
  while IFS= read -r -d '' rest; do   # -z: "added<TAB>deleted<TAB>path", path taken raw
    ad="${rest%%$'\t'*}"; rest="${rest#*$'\t'}"
    dl="${rest%%$'\t'*}"; p="${rest#*$'\t'}"
    [ -n "$p" ] || continue
    listing+="  +$ad -$dl ${p//$'\n'/<0A>}"$'\n'
    if is_root_relevant "$p"; then flagged+=("$p"); fi
  done < <(gitc diff --no-color --no-renames --numstat -z "$from" "$head")
  printf '%s' "$listing" | safe_show

  # 4. root-relevant changes, rewritten history or an unconfirmed starting point need a second y
  if [ "${#flagged[@]}" -gt 0 ] || [ "$rewritten" = 1 ] || [ "$extra" = 1 ]; then
    echo
    if [ "${#flagged[@]}" -gt 0 ]; then
      echo "ROOT-RELEVANT files changed (these run as root on every node):"
      for p in "${flagged[@]}"; do printf '  ! %s\n' "${p//$'\n'/<0A>}"; done | safe_show
      if [ "$showdiff" = 1 ]; then
        gitc diff --no-color --no-renames "$from" "$head" -- "${flagged[@]}" | safe_show
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
    [ -f "$KEY" ] || die 1 "$KEY not found (run pilnk-sign --init first). Nothing signed."
    echo "Touch the YubiKey when it blinks (within 30 s)."
    ssh-keygen -q -Y sign -f "$KEY" -n "$NS" "$T/$ver.txt" >/dev/null \
      || die 1 "signing failed: YubiKey not plugged in, or not touched within 30 s. Nothing uploaded."
  fi
  mv "$T/$ver.txt.sig" "$T/$ver.sig" || die 1 "no signature written. Nothing uploaded."

  # 7. check it against release_signers at that commit (catches the wrong key)
  gitc show "$head:release_signers" > "$T/release_signers" 2>/dev/null \
    || die 1 "release_signers is missing at $head. Nothing uploaded."
  ssh-keygen -Y verify -f "$T/release_signers" -I release -n "$NS" -s "$T/$ver.sig" \
    < "$T/$ver.txt" >/dev/null 2>&1 \
    || die 1 "The signature does not verify against release_signers at $head (wrong key?). Nothing uploaded."
  touch_ok "$T/$ver.sig" \
    || die 1 "The signature carries no touch flag (a FIDO key made without touch?). Nodes refuse it. Nothing uploaded."

  # 8. upload; the web server reads these as group/other, whatever this umask is
  chmod 0644 "$T/$ver.txt" "$T/$ver.sig"
  echo "Uploading $ver.txt and $ver.sig to $DEST"
  "$SCP" -q "$T/$ver.txt" "$T/$ver.sig" "$DEST" || die 3 "upload failed. Nothing published; run again."

  # 9. pilnk.io must now serve exactly these bytes
  curl -fsS --max-time 20 -o "$T/after.json" "$VERSION_URL" \
    || die 3 "uploaded, but could not read $VERSION_URL to confirm."
  python3 -I - "$T/after.json" "$ver" "$head" "$T/$ver.txt" "$T/$ver.sig" <<'PY' \
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
  { mkdir -p "$(dirname "$RECORD")" && printf '%s %s\n' "$ver" "$head" >> "$RECORD"; } \
    || echo "pilnk-sign: warning: could not add PiLNK $ver to $RECORD" >&2
  echo "done: PiLNK $ver ($head) is signed and published."
}

# ---------------------------------------------------------------------- one-time key set-up
# Task 1 (20 Nov 2026): AJ's key is a Yubico Security Key (FIDO only, no PIV). The release key is
# an OpenSSH FIDO key, ed25519-sk, non-resident: nothing is stored on the YubiKey, the key file
# is useless without it, and every signature needs a touch (Rulings S1, S2). Everything is made
# in a folder beside the keys and moved into place only once the test signature has passed, so
# a failure part-way leaves nothing behind.
cmd_init() {
  local f
  for f in "$KEY" "$KEY.pub" "$BACKUP_KEY" "$BACKUP_KEY.pub"; do
    [ -e "$f" ] && die 1 "$f already exists. Refusing to overwrite release keys."
  done
  mkdir -p "$(dirname "$KEY")" "$(dirname "$BACKUP_KEY")" || die 1 "cannot create key folders"
  T="$(mktemp -d "$(dirname "$KEY")/.pilnk-init.XXXXXX")" || die 1 "no temp folder"
  trap 'rm -rf "$T"' EXIT

  echo
  echo "1/4 YubiKey key. Touch the YubiKey when it blinks."
  echo "    (ssh-keygen may ask for a passphrase for the key file: Enter for none is fine,"
  echo "     the file is useless without the YubiKey.)"
  ssh-keygen -t ed25519-sk -O application=ssh:pilnk-release -C pilnk-release-yubikey -f "$T/yk" >/dev/null \
    || die 1 "No YubiKey answered. Plug it in, touch it when it blinks, and run again. Nothing created."

  echo
  echo "2/4 Backup key. Choose a strong passphrase; it is stored with the key, offline."
  ssh-keygen -t ed25519 -a 100 -C pilnk-release-backup -f "$T/backup" >/dev/null \
    || die 1 "backup key not created. Nothing kept."
  if ssh-keygen -y -P '' -f "$T/backup" >/dev/null 2>&1; then
    die 1 "the backup key must have a passphrase. Nothing kept; run pilnk-sign --init again."
  fi

  echo
  echo "3/4 Test signature over a fixed statement. Touch the YubiKey when it blinks."
  mkdir -p "$T/fixture"
  printf 'pilnk-release 1\nversion 0.0.0\ncommit %s\n' "$ZERO_COMMIT" > "$T/fixture/statement"
  { signer_line "$T/yk.pub" pilnk-release-yubikey; signer_line "$T/backup.pub" pilnk-release-backup; } \
    > "$T/fixture/release_signers"
  ssh-keygen -q -Y sign -f "$T/yk" -n "$NS" "$T/fixture/statement" >/dev/null \
    || die 1 "the test signature failed (not touched within 30 s?). Nothing kept."
  ssh-keygen -Y verify -f "$T/fixture/release_signers" -I release -n "$NS" \
    -s "$T/fixture/statement.sig" < "$T/fixture/statement" >/dev/null 2>&1 \
    || die 1 "the test signature does not verify. Nothing kept."
  touch_ok "$T/fixture/statement.sig" \
    || die 1 "the test signature carries no touch flag. Nothing kept."

  mv "$T/yk" "$KEY" && mv "$T/yk.pub" "$KEY.pub" \
    && mv "$T/backup" "$BACKUP_KEY" && mv "$T/backup.pub" "$BACKUP_KEY.pub" \
    || die 1 "could not move the keys into place. Check $(dirname "$KEY") and run again."
  mkdir -p "$FIXTURE" && cp "$T/fixture/statement" "$T/fixture/statement.sig" "$T/fixture/release_signers" "$FIXTURE/" \
    || echo "pilnk-sign: warning: could not write the test fixture to $FIXTURE" >&2

  echo
  echo "4/4 Done. These two lines go in release_signers at the top of the PiLNK repo:"
  echo
  cat "$T/fixture/release_signers"
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
