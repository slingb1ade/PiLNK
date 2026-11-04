#!/bin/bash
# Shared helpers for tests/image/test_*.sh. Source this file.
#
#   mkroot            fresh temp fake root in $ROOT, fake-binary dir $BIN (first
#                     on PATH), call log $CALLS; hermetic default fakes for
#                     systemctl / lsusb / rtl_test so nothing reaches the host
#   fakebin NAME 'S'  a fake command on PATH: logs "NAME argv..." to $CALLS,
#                     then runs the shell snippet S
#   ok NAME COND      prints "PASS: NAME" or "FAIL: NAME" (COND is eval'd);
#                     tests/image/run.sh counts these lines
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

_ROOTS=()
_rm_roots() { [ "${#_ROOTS[@]}" -gt 0 ] && rm -rf "${_ROOTS[@]}"; return 0; }
trap _rm_roots EXIT

mkroot() {
  ROOT="$(mktemp -d /tmp/pilnk-test.XXXXXX)"
  _ROOTS+=("$ROOT")
  BIN="$ROOT/.bin"; CALLS="$ROOT/.calls"
  mkdir -p "$BIN" "$ROOT/etc/default" "$ROOT/run" "$ROOT/dev"
  : > "$CALLS"
  ORIG_PATH="${ORIG_PATH:-$PATH}"
  PATH="$BIN:$ORIG_PATH"
  export ROOT BIN CALLS PATH
  # default fakes: no active services, no USB, no dongles
  fakebin systemctl 'case "$1" in is-active) grep -qx "${@: -1}" "$ROOT/.active" 2>/dev/null;; esac'
  fakebin lsusb 'cat "$ROOT/.lsusb" 2>/dev/null; true'
  fakebin rtl_test 'cat "$ROOT/.rtl_out" 2>/dev/null; true'
  : > "$ROOT/.active"
}

fakebin() {
  local name="$1" body="$2"
  printf '#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' "$name" "$body" > "$BIN/$name"
  chmod +x "$BIN/$name"
}

# helpers to describe the fake machine
active()   { printf '%s\n' "$@" >> "$ROOT/.active"; }
rtl()      { local i=0 sn; { echo "Found $# Rafael Micro R820T tuner(s)"; for sn in "$@"; do
               echo "  $i:  Realtek, RTL2838UHIDIR, SN: $sn"; i=$((i+1)); done; } > "$ROOT/.rtl_out"; }
airspy()   { echo "Bus 001 Device 004: ID 1d50:60a1 OpenMoko, Inc. Airspy" > "$ROOT/.lsusb"; }
aircraft() { mkdir -p "$ROOT/run/$1"; echo '{"aircraft":[]}' > "$ROOT/run/$1/aircraft.json"; }

ok() {
  local name="$1" cond="$2"
  if eval "$cond"; then echo "PASS: $name"; else echo "FAIL: $name"; fi
}
