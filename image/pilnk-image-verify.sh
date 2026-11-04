#!/bin/bash
# pilnk-image-verify.sh IMAGE [--denylist FILE]
# pilnk-image-verify.sh --root DIR --boot DIR --raw FILE [--ext4] [--denylist FILE]
#
# Read-only check of a saved PiLNK card image before it goes to new users
# (spec section 4). Run on the laptop.
#
#   1. Image mode (needs root): attaches IMAGE read-only (losetup -r), mounts the
#      boot partition read-only and the root partition read-only,noload, runs
#      the shared rule checks (sterile-checks.sh), then searches the raw image.
#      Everything is detached on exit, whatever happens.
#   2. Test mode: --root/--boot are an already-extracted tree, --raw the file to
#      search. --ext4 says the raw file is itself an ext4 filesystem, so hits are
#      mapped to paths as in image mode. No mount, no root.
#
# Raw search: ONE pass over the whole image (deleted data in free space, the
# ext4 journal and directory slack included). Only secret-SHAPED data counts:
#   privkey    an OpenSSH key body, or a PEM private-key header followed by a
#              base64 body, also after Proc-Type/DEK-Info headers (an encrypted
#              key); a bare header, as in the ssh binaries, is not a hit
#   psk        a NetworkManager / wpa_supplicant psk= line (at a line start)
#              whose value, quotes aside, is 8-63 printable characters (spaces,
#              $, %, " included) or a 64-hex key -- but not a format string
#              (psk=%s, psk=%.*s ...) or a shell variable (psk=$VAR, ${VAR})
#   tailscale  a Tailscale auth/client/API key
#   deny#N     line N (counting non-blank, non-# lines) of the denylist,
#              case-insensitive
# The denylist is a local file of names, emails, hostnames, node codes and SSIDs;
# default $PILNK_DENYLIST or ~/.config/pilnk/denylist. It is never committed.
#
# Each hit is mapped to where it lies: offset -> partition -> filesystem block
# -> owning inode -> path (debugfs icheck/ncheck on the read-only partition).
#   HIT <id> at <path>
#   HIT <id> at offset <n> (free space | fs metadata | boot partition | ...)
# A denylist hit inside a git-tracked file of the PiLNK clone
# (/home/pilnk/pilnk) that is unchanged from HEAD is code already public on
# GitHub: it prints "SKIP deny#N in <path> (public PiLNK file)" and does not
# fail. Only a regular file counts (not a directory: its blocks hold file
# names, deleted ones included), and only a hit inside the file's size (not in
# the slack after it). "Unchanged" means the blob in HEAD equals
# git hash-object --no-filters of the file; git never runs with the image's
# hooks, filters or system/global config (and never fetches).
# A privkey, psk or tailscale hit (never deny#N) inside a regular file that the
# image's own /var/lib/dpkg/info/*.md5sums lists with the md5 the file has now
# is a stock Debian file (libgnutls embeds its FIPS self-test keys): it prints
# "SKIP <id> in <path> (unmodified Debian package file)" and does not fail.
# Same limits: a regular file only, and only inside its size. Unlisted, a
# different md5, or no readable md5sums -> HIT. With --raw and no --ext4 there
# are no paths, so every hit stays a HIT.
# Every other hit fails. A path that
# itself contains a denylist word is printed as "<path withheld: contains
# deny#N>", and a path under the NetworkManager system-connections, netplan or
# wpa_supplicant directories as just that directory (file names can be SSIDs).
#
# Output NEVER holds the matched bytes, the bytes around them, or a denylist
# entry: the search prints "<id> <offset>" pairs only, and its patterns are
# built from pieces at run time, so this script and its tests (which ship in
# the image) never match themselves.
#
# Exit: 0 clean, 1 findings, 2 usage/setup error.

umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset PILNK_DIR
# shellcheck source=sterile-checks.sh
. "$HERE/sterile-checks.sh"

MAXHITS=50     # HIT lines printed per pattern id; the rest are counted
SCANCAP=5000   # hits per id the search hands over; beyond that they all fail
CLONE=/home/pilnk/pilnk

usage() { echo "usage: pilnk-image-verify.sh IMAGE [--denylist FILE]" >&2
          echo "       pilnk-image-verify.sh --root DIR --boot DIR --raw FILE [--ext4] [--denylist FILE]" >&2; exit 2; }

IMAGE=""; ROOTDIR=""; BOOTDIR=""; RAW=""; DENY=""; EXT4=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || usage; ROOTDIR="$2"; shift 2 ;;
    --boot) [ $# -ge 2 ] || usage; BOOTDIR="$2"; shift 2 ;;
    --raw) [ $# -ge 2 ] || usage; RAW="$2"; shift 2 ;;
    --ext4) EXT4=1; shift ;;
    --denylist) [ $# -ge 2 ] || usage; DENY="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) [ -z "$IMAGE" ] || usage; IMAGE="$1"; shift ;;
  esac
done
if [ -n "$IMAGE" ]; then
  [ -z "$ROOTDIR$BOOTDIR$RAW" ] && [ "$EXT4" = 0 ] || usage
  [ -f "$IMAGE" ] || { echo "ERROR: image not found" >&2; exit 2; }
else
  [ -n "$ROOTDIR" ] && [ -n "$BOOTDIR" ] && [ -n "$RAW" ] || usage
  [ -d "$ROOTDIR" ] && [ -d "$BOOTDIR" ] && [ -f "$RAW" ] || { echo "ERROR: --root/--boot must be directories and --raw a file" >&2; exit 2; }
fi

TMPD="$(mktemp -d /tmp/pilnk-verify.XXXXXX)" || { echo "ERROR: cannot create temp dir" >&2; exit 2; }
chmod 700 "$TMPD"
LOOP=""; MNT_BOOT=""; MNT_ROOT=""
cleanup() {
  local m
  for m in "$MNT_ROOT" "$MNT_BOOT"; do
    [ -n "$m" ] || continue
    umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || echo "WARNING: could not unmount $m" >&2
  done
  if [ -n "$LOOP" ]; then losetup -d "$LOOP" 2>/dev/null || echo "WARNING: could not detach $LOOP" >&2; fi
  rm -rf --one-file-system "$TMPD" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# -- denylist (copied into a mode-600 file, never echoed) ---------------------
[ -z "$DENY" ] && [ -r "${PILNK_DENYLIST:-$HOME/.config/pilnk/denylist}" ] && DENY="${PILNK_DENYLIST:-$HOME/.config/pilnk/denylist}"
NDENY=0; DENYW=(); DENYLEN=()
: > "$TMPD/deny"
if [ -n "$DENY" ]; then
  [ -r "$DENY" ] || { echo "ERROR: denylist file not readable" >&2; exit 2; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    NDENY=$((NDENY + 1))
    printf '%s\n' "$line" >> "$TMPD/deny"
    DENYW+=("$(LC_ALL=C; printf '%s' "${line,,}")")
    DENYLEN+=("$(printf '%s' "$line" | wc -c)")
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

# -- whole-image search: one pass -------------------------------------------
# tr turns NUL into newline byte for byte, so offsets stay exact and zeroed
# space becomes empty lines. grep -F keeps only lines holding a candidate (plus
# 4 lines after, for PEM bodies) with their byte offsets; awk applies the exact
# rules and prints "<id> <offset>". The lines themselves never leave the pipe.
{ printf '%s\n' "ps""k=" "b3BlbnNzaC1""rZXktdjE" "-----BEG""IN" "ts""key-"; cat "$TMPD/deny"; } > "$TMPD/cand"

# shellcheck disable=SC2016 # awk program, not shell
SCAN_AWK='
BEGIN {
  FS = "\n"
  OSSH = "b3BlbnNzaC1" "rZXktdjE"
  PEMRE = "-----BEG" "IN [A-Z0-9 ]*PRIV" "ATE KEY-----"
  PSK = "ps" "k="
  TSK = "ts" "key-"
  TSKRE = "^(auth|client|api)-[A-Za-z0-9_-]+"
  B64RE = "^[A-Za-z0-9+/]+=*$"
  B64RUN = "^[A-Za-z0-9+/]+"
  HDRRE = "^[A-Za-z][A-Za-z-]*: "
  VARRE = "^[$]([{][^}]*[}]|[A-Za-z_][A-Za-z0-9_]*)$"
  FMTRE = "[%][-+ #0]*([0-9]+|[*])?([.]([0-9]+|[*])?)?(hh|h|ll|l|j|z|t|L)?[sdiuxX]"
  nd = 0
  if (denyfile != "") {
    while ((getline w < denyfile) > 0) { nd++; deny[nd] = tolower(w) }
    close(denyfile)
  }
  prevend = -1; pemoff = -1; pemleft = 0; pemhdr = 0
}
function hit(id, o) {
  if (++cnt[id] <= cap) printf "%s %.0f\n", id, o
}
{
  if (!match($0, /^[0-9]+[:-]/)) { pemoff = -1; prevend = -1; next }
  lo = substr($0, 1, RLENGTH - 1) + 0
  line = substr($0, RLENGTH + 1)
  if (lo != prevend) pemoff = -1
  prevend = lo + length(line) + 1
  s = line; sub(/\r$/, "", s)

  # privkey: the OpenSSH key body (base64 of "openssh-key-v1")
  rest = line; base = lo
  while ((i = index(rest, OSSH)) > 0) { hit("privkey", base + i - 1); base += i; rest = substr(rest, i + 1) }

  # privkey: a PEM header seen on an earlier line, now its body
  if (pemoff >= 0) {
    t = s; sub(/[ \t]+$/, "", t)
    if (length(t) >= 40 && t ~ B64RE) { hit("privkey", pemoff); pemoff = -1 }
    else if (t ~ HDRRE) { pemhdr = 1; if (--pemleft <= 0) pemoff = -1 }
    else if (t == "" && pemhdr) { if (--pemleft <= 0) pemoff = -1 }
    else pemoff = -1
  }
  # privkey: a PEM header ending the line, or followed by an escaped \n + body
  rest = s; base = lo
  while (match(rest, PEMRE)) {
    rs = RSTART; rl = RLENGTH; o = base + rs - 1
    after = substr(rest, rs + rl)
    t = after; sub(/[ \t]+$/, "", t)
    if (t == "") { pemoff = o; pemleft = 4; pemhdr = 0 }
    else if (substr(t, 1, 2) == "\\n" && match(substr(t, 3), B64RUN) && RLENGTH >= 40) hit("privkey", o)
    base += rs + rl - 1; rest = after
  }

  # psk: a whole psk= line holding a real passphrase or a 64-hex key. The line
  # starts at a NUL/newline, or at a 512-byte sector boundary: a file begins on
  # a block boundary, glued to whatever bytes end the block before it.
  rest = s; base = lo
  while ((i = index(rest, PSK)) > 0) {
    o = base + i - 1; a = o - (o % 512)
    if (a < base) a = base
    if (substr(rest, a - base + 1, o - a) ~ /^[ \t]*$/) {
      v = substr(rest, i + 4); sub(/[ \t]+$/, "", v); n = length(v)
      if (n >= 2 && v ~ /^".*"$/) { v = substr(v, 2, n - 2); n -= 2 }   # wpa_supplicant quotes it
      if (n == 64 && v ~ /^[0-9a-fA-F]+$/) hit("psk", o)
      else if (n >= 8 && n <= 63 && v ~ /^[ -~]+$/ && v !~ VARRE && v !~ FMTRE) hit("psk", o)
    }
    base += i; rest = substr(rest, i + 1)
  }

  # tailscale: TSK then auth-, client- or api- and 8+ key characters
  rest = line; base = lo
  while ((i = index(rest, TSK)) > 0) {
    t = substr(rest, i + length(TSK))
    if (match(t, TSKRE) && RLENGTH - index(t, "-") >= 8) hit("tailscale", base + i - 1)
    base += i; rest = substr(rest, i + 1)
  }

  # denylist, case-insensitive
  if (nd) {
    low = tolower(line)
    for (k = 1; k <= nd; k++) {
      rest = low; base = lo
      while ((i = index(rest, deny[k])) > 0) { hit("deny#" k, base + i - 1); base += i; rest = substr(rest, i + 1) }
    }
  }
}
END { for (id in cnt) if (cnt[id] > cap) printf "%s +%.0f\n", id, cnt[id] - cap }
'
LC_ALL=C tr '\000' '\n' < "$RAW" 2>/dev/null \
  | LC_ALL=C grep -a -b -i -F -A 4 -f "$TMPD/cand" 2>/dev/null \
  | LC_ALL=C awk -v denyfile="$TMPD/deny" -v cap="$SCANCAP" "$SCAN_AWK" > "$TMPD/hits"
PS_=("${PIPESTATUS[@]}")
if [ "${PS_[0]}" != 0 ] || [ "${PS_[1]}" -gt 1 ] || [ "${PS_[2]}" != 0 ]; then
  echo "ERROR: the raw search failed (tr/grep/awk exit ${PS_[*]}); the image was NOT searched" >&2
  exit 2
fi

# -- where each hit lies ----------------------------------------------------
FS_DEV=""; FS_START=0; FS_END=0; BOOT_START=-1; BOOT_END=-1; BS=0
part_range() { # /dev/loopNpK -> "start end" in bytes (from sysfs)
  local n s z
  n="$(basename "$1")"
  s=$(cat "/sys/class/block/$n/start" 2>/dev/null) && z=$(cat "/sys/class/block/$n/size" 2>/dev/null) || return 1
  [[ "$s" =~ ^[0-9]+$ && "$z" =~ ^[0-9]+$ ]] || return 1
  echo "$((s * 512)) $(((s + z) * 512))"
}
if [ -n "$IMAGE" ]; then
  if r=$(part_range "${LOOP}p2"); then read -r FS_START FS_END <<< "$r"; FS_DEV="${LOOP}p2"; fi
  if r=$(part_range "${LOOP}p1"); then read -r BOOT_START BOOT_END <<< "$r"; fi
elif [ "$EXT4" = 1 ]; then
  FS_DEV="$RAW"; FS_START=0; FS_END=$(stat -c %s "$RAW")
fi
if [ -n "$FS_DEV" ] && [ -s "$TMPD/hits" ]; then
  BS=$(debugfs -R stats "$FS_DEV" 2>/dev/null | awk -F: '/^Block size:/ { gsub(/[ \t]/, "", $2); print $2; exit }')
  if ! [[ "$BS" =~ ^[0-9]+$ ]] || [ "$BS" -eq 0 ]; then
    echo "NOTE: could not read the root filesystem with debugfs; hits are shown as offsets"
    FS_DEV=""
  fi
fi

declare -A INO_OF PATH_OF USED ITYPE ISIZE IEXT
# debugfs "stat" -> "<inode> <type> <size> <logical>,<phys-first>,<phys-last> ..."
# Only plain "(L[-L]):P[-P]" extents are kept: index blocks (IND, ETB...) and
# uninitialised extents are not file data, so a hit there never maps into it.
# shellcheck disable=SC2016 # awk program, not shell
STAT_AWK='
function flush() { if (ino != "") print ino, type, (size == "" ? "x" : size), ext; ino = "" }
/^Inode: [0-9]+ +Type: / { flush(); ino = $2; type = $4; size = ""; ext = ""; inext = 0; next }
ino == "" { next }
/^User: / { if (match($0, /Size: [0-9]+/)) size = substr($0, RSTART + 6, RLENGTH - 6); next }
/^(EXTENTS|BLOCKS):/ { inext = 1; next }
inext && /^(TOTAL:|debugfs:)/ { inext = 0; next }
inext {
  line = $0
  while (match(line, /\([0-9]+(-[0-9]+)?\):[0-9]+(-[0-9]+)?/)) {
    tok = substr(line, RSTART + 1, RLENGTH - 1); line = substr(line, RSTART + RLENGTH)
    sub(/[)]/, "", tok); split(tok, h, ":"); split(h[1], l, "-"); np = split(h[2], q, "-")
    if (np == 1) q[2] = q[1]
    ext = ext " " l[1] "," q[1] "," q[2]
  }
}
END { flush() }
'
dbg_batch() { # verb file-of-args -> debugfs output (read-only)
  awk -v v="$1" '{ a = a " " $0 } NR % 200 == 0 { print v a; a = "" } END { if (a != "") print v a }' "$2" > "$TMPD/cmd"
  debugfs -f "$TMPD/cmd" "$FS_DEV" 2>/dev/null
}
if [ -n "$FS_DEV" ]; then
  awk -v s="$FS_START" -v e="$FS_END" -v bs="$BS" '$2 !~ /^\+/ && $2 >= s && $2 < e { printf "%.0f\n", int(($2 - s) / bs) }' \
    "$TMPD/hits" | sort -un > "$TMPD/blocks"
  if [ -s "$TMPD/blocks" ]; then
    while IFS=$'\t' read -r b i; do
      [[ "$b" =~ ^[0-9]+$ ]] || continue
      if [[ "$i" =~ ^[0-9]+$ ]]; then INO_OF[$b]=$i; else INO_OF[$b]=0; fi
    done < <(dbg_batch icheck "$TMPD/blocks")
    for b in "${!INO_OF[@]}"; do [ "${INO_OF[$b]}" -ge 11 ] && echo "${INO_OF[$b]}"; done | sort -un > "$TMPD/inodes"
    if [ -s "$TMPD/inodes" ]; then
      while IFS=$'\t' read -r i p; do
        [[ "$i" =~ ^[0-9]+$ ]] || continue
        [ -n "${PATH_OF[$i]:-}" ] || PATH_OF[$i]="/${p#"${p%%[!/]*}"}"
      done < <(dbg_batch ncheck "$TMPD/inodes")
    fi
    # type, size and logical->physical block map of every hit file (for SKIP)
    for i in "${!PATH_OF[@]}"; do echo "stat <$i>"; done > "$TMPD/cmd"
    if [ -s "$TMPD/cmd" ]; then
      while read -r i t z ext; do
        [[ "$i" =~ ^[0-9]+$ && "$z" =~ ^[0-9]+$ ]] || continue
        ITYPE[$i]=$t; ISIZE[$i]=$z; IEXT[$i]=$ext
      done < <(DEBUGFS_PAGER=__none__ PAGER=__none__ debugfs -f "$TMPD/cmd" "$FS_DEV" 2>/dev/null | awk "$STAT_AWK")
    fi
    for b in "${!INO_OF[@]}"; do [ "${INO_OF[$b]}" = 0 ] && echo "$b"; done > "$TMPD/free"
    while IFS= read -r ln; do
      if [[ "$ln" =~ ^Block\ ([0-9]+)\ marked\ in\ use ]]; then USED[${BASH_REMATCH[1]}]=1
      elif [[ "$ln" =~ ^Block\ ([0-9]+)\ not\ in\ use ]]; then USED[${BASH_REMATCH[1]}]=0; fi
    done < <(while IFS= read -r b; do echo "testb $b"; done < "$TMPD/free" > "$TMPD/cmd"; \
             [ -s "$TMPD/cmd" ] && debugfs -f "$TMPD/cmd" "$FS_DEV" 2>/dev/null)
  fi
fi

# where OFFSET lies: sets LOC_PATH (a file or directory) + LOC_INO, or LOC_KIND
locate() {
  local off="$1" b i
  LOC_PATH=""; LOC_KIND=""; LOC_INO=""
  if [ -n "$FS_DEV" ] && [ "$off" -ge "$FS_START" ] && [ "$off" -lt "$FS_END" ]; then
    b=$(((off - FS_START) / BS)); i="${INO_OF[$b]:-}"
    if [ -z "$i" ]; then LOC_KIND="fs metadata"
    elif [ "$i" -ge 11 ]; then
      if [ -n "${PATH_OF[$i]:-}" ]; then LOC_PATH="${PATH_OF[$i]}"; LOC_INO=$i; else LOC_KIND="unlinked file"; fi
    elif [ "$i" -gt 0 ]; then LOC_KIND="fs metadata"            # journal, resize inode...
    elif [ "${USED[$b]:-1}" = 0 ]; then LOC_KIND="free space"
    else LOC_KIND="fs metadata"; fi                             # bitmaps, inode tables, superblocks
  elif [ -n "$IMAGE" ] && [ "$off" -ge "$BOOT_START" ] && [ "$off" -lt "$BOOT_END" ]; then LOC_KIND="boot partition"
  elif [ -n "$IMAGE" ] && [ -n "$FS_DEV" ]; then LOC_KIND="outside partitions"
  fi
}

# a path is printed only if it holds no denylist word (names can be SSIDs);
# in the saved-network directories only the directory is named
NETDIRS=(/etc/NetworkManager/system-connections /run/NetworkManager/system-connections /etc/netplan /etc/wpa_supplicant)
show_path() {
  local p="${1//[[:cntrl:]]/?}" low k d
  for d in "${NETDIRS[@]}"; do
    if [[ "$p" == "$d/"* ]]; then p="$d"; break; fi
  done
  low="$(LC_ALL=C; printf '%s' "${p,,}")"
  for k in "${!DENYW[@]}"; do
    if [[ "$low" == *"${DENYW[$k]}"* ]]; then printf '<path withheld: contains deny#%d>' "$((k + 1))"; return; fi
  done
  printf '%s' "$p"
}

# the LEN bytes at OFFSET lie inside the data of inode INO (below its i_size)
in_file() { # offset inode len
  local b w e l1 p1 p2 fo
  b=$((($1 - FS_START) / BS)); w=$((($1 - FS_START) % BS))
  for e in ${IEXT[$2]:-}; do
    IFS=, read -r l1 p1 p2 <<< "$e"
    if [ "$b" -ge "$p1" ] && [ "$b" -le "$p2" ]; then
      fo=$(((l1 + b - p1) * BS + w))
      [ $((fo + $3)) -le "${ISIZE[$2]:-0}" ]; return
    fi
  done
  return 1
}

# the file at PATH is in HEAD of the PiLNK clone, byte for byte. git reads the
# image's repo but never runs its hooks, filters, fsmonitor or diff drivers,
# never reads the system or global (laptop) config, and never fetches (a
# partial clone's missing objects are not lazy-fetched through the image's
# remote: GIT_NO_LAZY_FETCH, and GIT_ALLOW_PROTOCOL=none for older git).
declare -A PUBLIC
same_as_head() { # path
  local p="$1" rel repo="$ROOTDIR$CLONE" head blob
  [ -n "${PUBLIC[$p]:-}" ] && { [ "${PUBLIC[$p]}" = 1 ]; return; }
  rel="${p#"$CLONE/"}"; PUBLIC[$p]=0
  if [ -d "$repo/.git" ] && [ ! -L "$repo/.git" ] && command -v git >/dev/null 2>&1; then
    local g=(env -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES
             GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0
             GIT_NO_LAZY_FETCH=1 GIT_ALLOW_PROTOCOL=none
             git --git-dir="$repo/.git" -c 'safe.directory=*' -c core.hooksPath=/dev/null -c core.fsmonitor=)
    head=$("${g[@]}" rev-parse --verify -q "HEAD:$rel" 2>/dev/null) || head=""
    blob=$("${g[@]}" hash-object --no-filters -- "$ROOTDIR$p" 2>/dev/null) || blob=""
    if [ -n "$head" ] && [ "$head" = "$blob" ]; then PUBLIC[$p]=1; fi
  fi
  [ "${PUBLIC[$p]}" = 1 ]
}

# a denylist hit of LEN bytes at OFFSET is public PiLNK code: inside a regular
# file (not a directory or symlink) of the clone, within its size, and that
# file is unchanged from HEAD
is_public() { # path inode offset len
  local p="$1" f="$ROOTDIR$1"
  [[ "$p" == "$CLONE/"* ]] && [ -n "$2" ] || return 1
  [ -f "$f" ] && [ ! -L "$f" ] && [ "${ITYPE[$2]:-}" = regular ] || return 1
  in_file "$3" "$2" "$4" || return 1
  same_as_head "$p"
}

# the file at PATH is a Debian package file exactly as shipped: the image's own
# /var/lib/dpkg/info/*.md5sums ("<md5>  <path without leading />" lines; a
# multiarch package's file is "<name>:<arch>.md5sums") lists it with the md5
# the file has now. No md5sums, unreadable, unlisted or different -> no.
declare -A PKGOK
MD5IDX=""
pkg_unmodified() { # path
  local p="$1" d c sum
  [ -n "${PKGOK[$p]:-}" ] && { [ "${PKGOK[$p]}" = 1 ]; return; }
  PKGOK[$p]=0
  if [ -z "$MD5IDX" ]; then
    MD5IDX="$TMPD/md5sums"; : > "$MD5IDX"; d="$ROOTDIR"
    for c in var lib dpkg info; do d="$d/$c"; if [ ! -d "$d" ] || [ -L "$d" ]; then d=""; break; fi; done
    [ -n "$d" ] && find "$d" -maxdepth 1 -type f -name '*.md5sums' -print0 2>/dev/null \
      | xargs -0 -r cat -- 2>/dev/null >> "$MD5IDX"
  fi
  sum=$(md5sum < "$ROOTDIR$p" 2>/dev/null) || return 1
  sum="${sum%% *}"
  [[ "$sum" =~ ^[0-9a-f]{32}$ ]] || return 1
  R="${p#/}" S="$sum" awk 'substr($0, 33, 2) == "  " && substr($0, 35) == ENVIRON["R"] && substr($0, 1, 32) == ENVIRON["S"] { f = 1; exit }
                           END { exit !f }' "$MD5IDX" || return 1
  PKGOK[$p]=1
}

# a secret-shaped hit (id privkey, psk or tailscale; never deny#N) of at least
# LEN bytes at OFFSET lies in the data of a regular file (not a directory or
# symlink, not the slack past its size) that is an unmodified package file.
# libgnutls, for one, embeds its FIPS self-test private keys.
declare -A SECLEN=([privkey]=19 [psk]=12 [tailscale]=19)  # shortest match per id
is_pkgfile() { # path inode offset len
  local f="$ROOTDIR$1"
  [ -n "$2" ] && [ -f "$f" ] && [ ! -L "$f" ] && [ "${ITYPE[$2]:-}" = regular ] || return 1
  in_file "$3" "$2" "$4" || return 1
  pkg_unmodified "$1"
}

declare -A NHIT SEEN
while read -r id off; do
  if [[ "$off" == +* ]]; then
    printf 'HIT %s: %s more offsets not examined\n' "$id" "${off#+}"; FOUND=1; continue
  fi
  locate "$off"
  if [ -n "$LOC_PATH" ]; then
    if [[ "$id" == deny#* ]] && is_public "$LOC_PATH" "$LOC_INO" "$off" "${DENYLEN[${id#deny#} - 1]:-0}"; then
      line="SKIP $id in $(show_path "$LOC_PATH") (public PiLNK file)"
      [ -n "${SEEN[$line]:-}" ] || { SEEN[$line]=1; echo "$line"; }
      continue
    fi
    if [ -n "${SECLEN[$id]:-}" ] && is_pkgfile "$LOC_PATH" "$LOC_INO" "$off" "${SECLEN[$id]}"; then
      line="SKIP $id in $(show_path "$LOC_PATH") (unmodified Debian package file)"
      [ -n "${SEEN[$line]:-}" ] || { SEEN[$line]=1; echo "$line"; }
      continue
    fi
    line="HIT $id at $(show_path "$LOC_PATH")"
  elif [ -n "$LOC_KIND" ]; then line="HIT $id at offset $off ($LOC_KIND)"
  else line="HIT $id at offset $off"; fi
  FOUND=1
  [ -n "${SEEN[$line]:-}" ] && continue
  SEEN[$line]=1
  NHIT[$id]=$(( ${NHIT[$id]:-0} + 1 ))
  [ "${NHIT[$id]}" -le "$MAXHITS" ] && echo "$line"
done < "$TMPD/hits"
for id in "${!NHIT[@]}"; do
  if [ "${NHIT[$id]}" -gt "$MAXHITS" ]; then printf 'HIT %s: %d more not listed\n' "$id" "$((NHIT[$id] - MAXHITS))"; fi
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
