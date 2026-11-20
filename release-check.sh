#!/bin/bash
# release-check.sh: does AJ's signature cover the newest PiLNK release?
#
#   release-check.sh [--fetched] STATEMENT_FILE SIG_FILE
#
# STATEMENT_FILE / SIG_FILE hold the `release` object's statement and armored
# signature exactly as pilnk.io served them. Prints exactly four lines:
#   ota_sig=ok|missing|bad|not_on_main|mismatch|no_tool|error
#   ota_sig_run=on_signed|behind|ahead|dirty|none
#   signed_version=<V or empty>
#   signed_commit=<40-hex or empty>
# and exits 0. Exit 2 = usage error only.
#
# The signature is checked against release_signers in the tree this node RUNS
# now, never one that came down with a fetch: only a signed release can change
# who may sign. --fetched (update.sh, after its git fetch) also checks the
# commit is on origin/main and that its VERSION matches the statement; the
# app's periodic run has fetched nothing, so it skips those two.
#
# Read-only: never writes to the working tree, the index or the refs.
# Spec: pilnk-tasks/signed-releases-spec.md section 5.5.
PILNK_DIR="${PILNK_DIR:-$HOME/pilnk}"
FETCHED=0
if [ "${1:-}" = "--fetched" ]; then FETCHED=1; shift; fi
if [ "$#" -ne 2 ]; then
    echo "usage: release-check.sh [--fetched] STATEMENT_FILE SIG_FILE" >&2
    exit 2
fi
ST="$1"; SIG="$2"
TOKEN=""; RUN="none"; VER=""; COMMIT=""

emit() {
    printf 'ota_sig=%s\nota_sig_run=%s\nsigned_version=%s\nsigned_commit=%s\n' \
        "$TOKEN" "$RUN" "$VER" "$COMMIT"
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
{ [ -f "$PILNK_DIR/release_signers" ] && [ -r "$PILNK_DIR/release_signers" ]; } || { TOKEN=error; emit; }
if ! ssh-keygen -Y verify -f "$PILNK_DIR/release_signers" -I release -n pilnk-release \
        -s "$SIG" < "$ST" >/dev/null 2>&1; then
    TOKEN=bad; emit
fi
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
