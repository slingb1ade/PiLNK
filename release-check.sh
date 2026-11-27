#!/bin/bash
# release-check.sh: does AJ's signature cover the newest PiLNK release?
#
#   release-check.sh [--fetched] STATEMENT_FILE SIG_FILE
#
# STATEMENT_FILE / SIG_FILE hold the `release` object's statement and armored
# signature exactly as pilnk.io served them. Prints exactly five lines:
#   ota_sig=ok|missing|bad|not_on_main|mismatch|no_tool|error
#   ota_sig_run=on_signed|behind|ahead|dirty|none
#   signed_version=<V or empty>
#   signed_commit=<40-hex or empty>
#   ota_sig_why=signers|touch   (only with ota_sig=error: which step failed)
# and exits 0. Exit 2 = usage error only.
#
# The signature is checked against release_signers in the tree this node RUNS
# now, never one that came down with a fetch: only a signed release can change
# who may sign. RELEASE_SIGNERS names a different signers file: rootsync.sh
# (root's own signed copy, Part B) passes root's pinned trust/release_signers. --fetched (update.sh, after its git fetch) also checks the
# commit is on origin/main and that its VERSION matches the statement; the
# app's periodic run has fetched nothing, so it skips those two.
#
# Read-only: never writes to the working tree, the index or the refs.
# Spec: pilnk-tasks/signed-releases-spec.md section 5.5.
PILNK_DIR="${PILNK_DIR:-$HOME/pilnk}"
SIGNERS="${RELEASE_SIGNERS:-$PILNK_DIR/release_signers}"
FETCHED=0
if [ "${1:-}" = "--fetched" ]; then FETCHED=1; shift; fi
if [ "$#" -ne 2 ]; then
    echo "usage: release-check.sh [--fetched] STATEMENT_FILE SIG_FILE" >&2
    exit 2
fi
ST="$1"; SIG="$2"
TOKEN=""; RUN="none"; VER=""; COMMIT=""; WHY=""

emit() {
    printf 'ota_sig=%s\nota_sig_run=%s\nsigned_version=%s\nsigned_commit=%s\nota_sig_why=%s\n' \
        "$TOKEN" "$RUN" "$VER" "$COMMIT" "$WHY"
    exit 0
}

# git that never writes: GIT_OPTIONAL_LOCKS=0 stops `status` refreshing the index.
git_ro() { GIT_OPTIONAL_LOCKS=0 git -C "$PILNK_DIR" "$@"; }

# a < b as release numbers (1.6.9 < 1.6.10 < 1.6.10.1)
ver_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]; }

# Exactly the three spec lines, LF only, one trailing newline. Sets VER/COMMIT.
# The commit only ever reaches git after it matched 40 lowercase hex.
parse_statement() {
    local content re
    content=$(cat -- "$ST"; printf x) || return 1
    content=${content%x}
    re=$'^pilnk-release 1\nversion ([0-9]+(\\.[0-9]+){1,3})\ncommit ([0-9a-f]{40})\n$'
    [[ $content =~ $re ]] || return 1
    VER=${BASH_REMATCH[1]}
    COMMIT=${BASH_REMATCH[3]}
}

# FIDO (YubiKey) signatures carry a "user present" flag inside the signed data.
# OpenSSH 8.9 does not check it, so a key made with no-touch-required would pass;
# we check it ourselves (Task 1, Ruling S2). Plain keys (the offline backup) have
# no flag. Exit 0 = fine, 1 = FIDO signature made without a touch, 2 = unreadable.
touch_check() {
    python3 -I - "$SIG" <<'PY'
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
    o = 10                              # magic + version
    for _ in range(4):                  # public key, namespace, reserved, hash
        _, o = field(b, o)
    sig, o = field(b, o)
    if o != len(b):
        sys.exit(2)
    kind, p = field(sig, 0)
    if not kind.startswith(b'sk-'):
        sys.exit(0)
    _, p = field(sig, p)                # the signature proper
    if p + 5 != len(sig):               # flags byte + 32-bit counter
        sys.exit(2)
    sys.exit(0 if sig[p] & 0x01 else 1)
except SystemExit:
    raise
except Exception:
    sys.exit(2)
PY
}

run_state() {
    local head local_ver
    if [ -n "$(git_ro status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        RUN=dirty; return
    fi
    head=$(git_ro rev-parse HEAD 2>/dev/null) || { RUN=none; return; }
    if [ "$head" = "$COMMIT" ]; then RUN=on_signed; return; fi
    local_ver=$(head -n 1 "$PILNK_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$local_ver" ] && ver_lt "$local_ver" "$VER"; then RUN=behind; else RUN=ahead; fi
}

command -v ssh-keygen >/dev/null 2>&1 || { TOKEN=no_tool; emit; }
{ [ -s "$ST" ] && [ -s "$SIG" ]; } || { TOKEN=missing; emit; }
{ [ -f "$SIGNERS" ] && [ -r "$SIGNERS" ]; } || { TOKEN=error; WHY=signers; emit; }
if ! ssh-keygen -Y verify -f "$SIGNERS" -I release -n pilnk-release \
        -s "$SIG" < "$ST" >/dev/null 2>&1; then
    TOKEN=bad; emit
fi
touch_check; tc=$?
case "$tc" in
    0) ;;
    1|2) TOKEN=bad; emit ;;
    *) TOKEN=error; WHY=touch; emit ;;
esac
parse_statement || { TOKEN=not_on_main; VER=""; COMMIT=""; emit; }

TOKEN=ok
if [ "$FETCHED" = 1 ]; then
    if ! git_ro cat-file -e "${COMMIT}^{commit}" 2>/dev/null \
       || ! git_ro merge-base --is-ancestor "$COMMIT" origin/main 2>/dev/null; then
        TOKEN=not_on_main
    elif [ "$(git_ro show "${COMMIT}:VERSION" 2>/dev/null | head -n 1 | tr -d '[:space:]')" != "$VER" ]; then
        TOKEN=mismatch
    fi
fi
run_state
emit
