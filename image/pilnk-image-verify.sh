#!/bin/bash
# pilnk-image-verify.sh IMAGE [--denylist FILE]
# pilnk-image-verify.sh --root DIR --boot DIR --raw FILE [--denylist FILE]
#
# Read-only check of a saved PiLNK card image before it goes to new users
# (spec section 4). Run on the laptop.
#
#   1. Image mode (needs root): attaches IMAGE read-only (losetup -r), mounts the
#      boot partition read-only and the root partition read-only,noload, runs
#      the shared rule checks (sterile-checks.sh), then searches the raw image.
#      Everything is detached on exit, whatever happens.
#   2. Test mode: --root/--boot are an already-extracted tree, --raw the file to
#      search. No mount, no root.
#
# Raw search (the whole image, so deleted data in free space is included):
#   privkey   private-key headers
#   psk       "psk="
#   wpa-psk   the wpa-psk key-management string
#   tailscale
#   deny#N    line N (counting non-blank, non-# lines) of the denylist
# The denylist is a local file of names, emails, hostnames, node codes and SSIDs;
# default $PILNK_DENYLIST or ~/.config/pilnk/denylist. It is never committed.
#
# Output: "FAIL <check>: <path>" for rule checks, "HIT <pattern-id> at <byte
# offset>" for the raw search. NEVER the matched text, the bytes around it, or a
# denylist entry: each pattern is searched on its own from a mode-600 temp file,
# and grep's output is cut down to the offset before it reaches the screen.
#
# Exit: 0 clean, 1 findings, 2 usage/setup error.

umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset PILNK_DIR
# shellcheck source=sterile-checks.sh
. "$HERE/sterile-checks.sh"

MAXHITS=50     # offsets printed per pattern; the rest are counted

usage() { echo "usage: pilnk-image-verify.sh IMAGE [--denylist FILE]" >&2
          echo "       pilnk-image-verify.sh --root DIR --boot DIR --raw FILE [--denylist FILE]" >&2; exit 2; }

IMAGE=""; ROOTDIR=""; BOOTDIR=""; RAW=""; DENY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || usage; ROOTDIR="$2"; shift 2 ;;
    --boot) [ $# -ge 2 ] || usage; BOOTDIR="$2"; shift 2 ;;
    --raw) [ $# -ge 2 ] || usage; RAW="$2"; shift 2 ;;
    --denylist) [ $# -ge 2 ] || usage; DENY="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) [ -z "$IMAGE" ] || usage; IMAGE="$1"; shift ;;
  esac
done
if [ -n "$IMAGE" ]; then
  [ -z "$ROOTDIR$BOOTDIR$RAW" ] || usage
  [ -f "$IMAGE" ] || { echo "ERROR: image not found" >&2; exit 2; }
else
  [ -n "$ROOTDIR" ] && [ -n "$BOOTDIR" ] && [ -n "$RAW" ] || usage
  [ -d "$ROOTDIR" ] && [ -d "$BOOTDIR" ] && [ -f "$RAW" ] || { echo "ERROR: --root/--boot must be directories and --raw a file" >&2; exit 2; }
fi

TMPD="$(mktemp -d /tmp/pilnk-verify.XXXXXX)" || { echo "ERROR: cannot create temp dir" >&2; exit 2; }
chmod 700 "$TMPD"
LOOP=""; MNT_BOOT=""; MNT_ROOT=""
cleanup() {
  [ -n "$MNT_ROOT" ] && umount "$MNT_ROOT" 2>/dev/null
  [ -n "$MNT_BOOT" ] && umount "$MNT_BOOT" 2>/dev/null
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
  rm -rf "$TMPD"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# -- denylist (read only into mode-600 files, never echoed) ---------------
[ -z "$DENY" ] && [ -r "${PILNK_DENYLIST:-$HOME/.config/pilnk/denylist}" ] && DENY="${PILNK_DENYLIST:-$HOME/.config/pilnk/denylist}"
NDENY=0
if [ -n "$DENY" ]; then
  [ -r "$DENY" ] || { echo "ERROR: denylist file not readable" >&2; exit 2; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    NDENY=$((NDENY + 1))
    printf '%s\n' "$line" > "$TMPD/deny.$NDENY"
  done < "$DENY"
fi

# -- image mode: attach and mount read-only -------------------------------
if [ -n "$IMAGE" ]; then
  if [ "$(id -u)" != 0 ]; then
    echo "ERROR: mounting an image needs root (sudo), or pass --root DIR --boot DIR --raw FILE" >&2
    exit 2
  fi
  LOOP="$(losetup -r -f -P --show "$IMAGE" 2>/dev/null)" || { LOOP=""; echo "ERROR: cannot attach image read-only" >&2; exit 2; }
  command -v udevadm >/dev/null 2>&1 && udevadm settle 2>/dev/null
  MNT_BOOT="$TMPD/boot"; MNT_ROOT="$TMPD/root"; mkdir -p "$MNT_BOOT" "$MNT_ROOT"
  if mount -o ro "${LOOP}p1" "$MNT_BOOT" 2>/dev/null; then :; else MNT_BOOT=""; echo "ERROR: cannot mount boot partition read-only" >&2; exit 2; fi
  if mount -o ro,noload "${LOOP}p2" "$MNT_ROOT" 2>/dev/null; then :; else MNT_ROOT=""; echo "ERROR: cannot mount root partition read-only" >&2; exit 2; fi
  ROOTDIR="$MNT_ROOT"; BOOTDIR="$MNT_BOOT"; RAW="$IMAGE"
fi

FOUND=0

# -- rule checks (same list the sterilise script ends with) ----------------
sterile_check "$ROOTDIR" "$BOOTDIR"
NF=$?
[ "$NF" -gt 0 ] && FOUND=1

# -- whole-image search ------------------------------------------------------
# One grep per pattern so the id is known without printing the pattern. The image
# goes through tr first, turning NUL into newline one byte for one byte: offsets
# stay exact, and zeroed free space becomes tiny lines instead of one multi-GB
# line grep would have to hold in memory. (grep -z reports wrong -b offsets on
# sparse files, so it is not used.) grep's "offset:match" lines go through
# cut so only the offset survives.
raw_hits() { # id patternfile [-i]
  local id="$1" pf="$2" icase="$3" n=0 off
  while IFS= read -r off; do
    n=$((n + 1))
    [ "$n" -le "$MAXHITS" ] && printf 'HIT %s at %s\n' "$id" "$off"
  done < <(tr '\0' '\n' < "$RAW" 2>/dev/null | grep -o -b -a -F ${icase:+"$icase"} -f "$pf" 2>/dev/null | cut -d: -f1)
  if [ "$n" -gt "$MAXHITS" ]; then printf 'HIT %s: %d more offsets not listed\n' "$id" "$((n - MAXHITS))"; fi
  [ "$n" -gt 0 ] && FOUND=1
  return 0
}

# Built from pieces so this script's own text does not contain the strings it
# searches for (the repo, and so this file, ships inside the image).
mkpat() { printf '%s\n' "$2" > "$TMPD/pat.$1"; }
mkpat privkey "-----BEGIN ""PRIVATE KEY"
mkpat privkey2 "-----BEGIN ""RSA PRIVATE KEY"
mkpat privkey3 "-----BEGIN ""OPENSSH PRIVATE KEY"
mkpat privkey4 "-----BEGIN ""EC PRIVATE KEY"
mkpat psk "ps""k="
mkpat wpa-psk "wpa""-psk"
mkpat tailscale "tail""scale"
cat "$TMPD/pat.privkey" "$TMPD/pat.privkey2" "$TMPD/pat.privkey3" "$TMPD/pat.privkey4" > "$TMPD/pat.privkey.all"

raw_hits privkey "$TMPD/pat.privkey.all" ""
raw_hits psk "$TMPD/pat.psk" -i
raw_hits wpa-psk "$TMPD/pat.wpa-psk" -i
raw_hits tailscale "$TMPD/pat.tailscale" -i
i=1
while [ "$i" -le "$NDENY" ]; do
  raw_hits "deny#$i" "$TMPD/deny.$i" -i
  i=$((i + 1))
done

if [ -z "$DENY" ]; then
  echo "NOTE: no denylist given or found; names, emails, hostnames and SSIDs were not searched"
fi
if [ "$FOUND" = 0 ]; then
  echo "CLEAN: no rule failures, no raw-image hits"
  exit 0
fi
echo "NOT CLEAN: do not give this image to anyone"
exit 1
