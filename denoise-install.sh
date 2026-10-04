#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  denoise-install.sh — put the live-denoise files where the radio
#  engine looks for them, then restart the engine if anything changed.
#
#  WHY THIS EXISTS (4 Oct 2026). v1.5.29 "Five-By-Five" shipped live
#  denoise in the engine, but the engine only offers it when two files
#  are on the node:
#      /usr/local/lib/pilnk/libdf.so                      (per CPU arch)
#      /usr/local/share/pilnk/DeepFilterNet3_onnx.tar.gz  (the model)
#  Nothing ever delivered them. They were copied by hand onto AJ's own
#  receivers, so denoise worked there and nowhere else — and because the
#  DENOISE button hides itself when the engine says "unavailable", nobody
#  could see it was missing until MME1 asked where the button was.
#
#  The files now travel in the repo (denoise/), so every OTA brings them;
#  this script installs them. It runs as root from pilnk-audio-build.service
#  (the only privileged path the node's sudoers rule allows), either at the
#  end of a full engine build or on its own when update.sh finds the files
#  missing or different.
#
#  FAIL-SOFT: denoise is an extra. Nothing here may break the radio. The
#  engine itself test-loads the library in a child process before using it,
#  so a wrong build can only switch denoise off.
#  IDEMPOTENT: identical files are left alone and the engine is not restarted.
# ─────────────────────────────────────────────────────────────
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${PILNK_DENOISE_SRC:-$HERE/denoise}"
ARCH="${PILNK_DENOISE_ARCH:-$(uname -m)}"
LIB_SRC="$SRC/$ARCH/libdf.so"
MODEL_SRC="$SRC/DeepFilterNet3_onnx.tar.gz"
LIB_DST="${PILNK_DF_LIB_DST:-/usr/local/lib/pilnk/libdf.so}"
MODEL_DST="${PILNK_DF_MODEL_DST:-/usr/local/share/pilnk/DeepFilterNet3_onnx.tar.gz}"
STATE="${PILNK_DENOISE_STATE:-$HERE/denoise_state.json}"
SYSTEMCTL="${PILNK_SYSTEMCTL:-systemctl}"

_state() {   # result, detail — never allowed to fail
    local d
    d="$(printf '%s' "${2:-}" | tr -d '\\"' | tr '\n\r\t' '   ' | cut -c1-160)"
    printf '{"result":"%s","detail":"%s","arch":"%s","ts":%s}\n' \
        "$1" "$d" "$ARCH" "$(date +%s)" > "$STATE" 2>/dev/null || true
    chmod 644 "$STATE" 2>/dev/null || true
}
say() { printf '[denoise] %s\n' "$1"; }

if [ ! -f "$LIB_SRC" ]; then
    say "no build for $ARCH in $SRC — denoise not offered on this machine"
    _state no_build "no libdf.so for $ARCH"
    exit 0
fi
if [ ! -f "$MODEL_SRC" ]; then
    say "model missing from $SRC"
    _state failed "model missing from repo"
    exit 0
fi

# Checksums travel with the files. A truncated or half-pulled file must
# never be installed.
SUMS="$SRC/SHA256SUMS"
if [ ! -f "$SUMS" ]; then
    _state failed "SHA256SUMS missing"; say "SHA256SUMS missing"; exit 0
fi
_want() {   # expected sum for a path relative to $SRC
    local rel="$1" line
    while read -r line; do
        case "$line" in *"  $rel") printf '%s' "${line%% *}"; return 0 ;; esac
    done < "$SUMS"
    return 1
}
for rel in "$ARCH/libdf.so" "DeepFilterNet3_onnx.tar.gz"; do
    want="$(_want "$rel" || true)"
    have="$(sha256sum "$SRC/$rel" 2>/dev/null | cut -d' ' -f1 || true)"
    if [ -z "$want" ] || [ "$want" != "$have" ]; then
        _state failed "checksum mismatch: $rel"
        say "checksum mismatch for $rel — not installing"
        exit 0
    fi
done

CHANGED=0
_place() {   # src dst — atomic replace, only when different
    local src="$1" dst="$2" tmp
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then return 0; fi
    mkdir -p "$(dirname "$dst")" || return 1
    tmp="$dst.new.$$"
    # rename() over the old file: a running engine keeps its mapped copy and
    # the next start gets the new one. Never a half-written file at $dst.
    cp "$src" "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dst" || { rm -f "$tmp"; return 1; }
    CHANGED=1
}
if ! _place "$LIB_SRC" "$LIB_DST"; then
    _state failed "could not write $LIB_DST"; say "could not write $LIB_DST"; exit 0
fi
if ! _place "$MODEL_SRC" "$MODEL_DST"; then
    _state failed "could not write $MODEL_DST"; say "could not write $MODEL_DST"; exit 0
fi

if [ "$CHANGED" -eq 1 ]; then
    say "installed for $ARCH"
    # The engine probes denoise once at start-up, so it needs a restart to
    # notice. Only if it is actually installed as a service.
    if "$SYSTEMCTL" is-enabled pilnkradio >/dev/null 2>&1; then
        "$SYSTEMCTL" restart pilnkradio >/dev/null 2>&1 \
            && say "radio engine restarted" \
            || say "could not restart pilnkradio (denoise appears after its next start)"
    fi
    _state ok "installed"
else
    say "already up to date"
    _state ok "up to date"
fi
exit 0
