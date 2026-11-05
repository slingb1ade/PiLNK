#!/bin/bash
# hwtier.sh — PiLNK hardware tier (shell twin of hwtier.py; spec power-and-hwfloor-spec.md).
#   hwtier.sh            prints  full | adsb_only
#   hwtier.sh --label    prints  the board name ("Raspberry Pi 3 Model B Plus", "1 GB Raspberry Pi 4")
#   hwtier.sh --audio-ok exit 0 on full (silent); on adsb_only prints the installer line, exit 3
# Inputs: PILNK_MODEL_FILE (default /proc/device-tree/model), PILNK_MEMINFO (default /proc/meminfo),
#         PILNK_FORCE_FULL=1 forces full.  Never fails hard: unreadable inputs → full.
RAM_FLOOR_KB=1572864
SMALL_RE='Raspberry Pi (3|2|Zero|Compute Module 3|Model )'

model_file="${PILNK_MODEL_FILE:-/proc/device-tree/model}"
meminfo="${PILNK_MEMINFO:-/proc/meminfo}"
model="$( { tr -d '\0' < "$model_file"; } 2>/dev/null )"
mem="$(awk '/^MemTotal:/ {print $2; exit}' "$meminfo" 2>/dev/null)"

small_model() { [ -n "$1" ] && printf '%s' "$1" | grep -Eq "$SMALL_RE"; }
ram_low() { [ -n "$mem" ] && [ "$mem" -lt "$RAM_FLOOR_KB" ] 2>/dev/null; }

tier() {
  if [ "${PILNK_FORCE_FULL:-}" = "1" ]; then echo full; return; fi
  if small_model "$model" || ram_low; then echo adsb_only; else echo full; fi
}

label() {
  local short="${model%% Rev *}"
  if ram_low && ! small_model "$short"; then
    local gb=$(( (mem + 524288) / 1048576 )); [ "$gb" -lt 1 ] && gb=1
    local family="${short%% Model *}"; [ -z "$family" ] && family="computer"
    echo "$gb GB $family"
  else
    echo "${short:-computer}"
  fi
}

case "${1:-}" in
  --label) label ;;
  --audio-ok)
    if [ "$(tier)" = "full" ]; then exit 0; fi
    echo "This is a $(label) — PiLNK will run in aircraft-only mode (map, tracking, stats). ATC audio, live denoise and datalink need a Pi 4 (2 GB+) or Pi 5."
    exit 3 ;;
  *) tier ;;
esac
