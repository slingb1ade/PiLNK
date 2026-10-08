#!/usr/bin/env bash
# PiLNK datalink: start dumpvdl2 from /etc/pilnk-datalink/config.json.
# Run by pilnk-datalink.service. Output goes to 127.0.0.1 only (UDP JSON);
# nothing leaves this node.
set -euo pipefail
CFG="${PILNK_DL_CFG:-/etc/pilnk-datalink/config.json}"
DUMPVDL2="${PILNK_DL_BIN:-/usr/local/bin/dumpvdl2}"

# Validate everything before it reaches a command line: the serial goes to
# --rtlsdr, so it must be a plain serial, never shell or option text.
ARGS="$(python3 - "$CFG" <<'PY'
import json, re, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception as e:
    sys.exit(f"datalink: cannot read config {sys.argv[1]}: {e}")
serial = str(c.get("serial", ""))
if not re.fullmatch(r"[0-9A-Za-z]{1,16}", serial):
    sys.exit(f"datalink: config serial missing or invalid: {serial!r}")
freqs = c.get("freqs_mhz", [136.975])
if not isinstance(freqs, list) or not freqs or not all(isinstance(f, (int, float)) and 118 <= f <= 137 for f in freqs):
    sys.exit(f"datalink: config freqs_mhz must be a list of 118-137 MHz values, got {freqs!r}")
gain = c.get("gain", 40)
if not isinstance(gain, (int, float)) or not 0 <= gain <= 60:
    sys.exit(f"datalink: config gain must be 0-60, got {gain!r}")
port = c.get("udp_port", 5555)
if not isinstance(port, int) or not 1024 <= port <= 65535:
    sys.exit(f"datalink: config udp_port must be 1024-65535, got {port!r}")
print(serial)
print(gain)
print(port)
print(" ".join(str(int(round(f * 1e6))) for f in freqs))
# Optional local message log (decoder research; 7-day cleanup by tmpfiles.d).
# The path goes inside a dumpvdl2 option list split on commas and '=', so only
# a plain absolute path is accepted; anything else just means no log.
log_dir = c.get("log_dir", "")
if log_dir and not (isinstance(log_dir, str) and re.fullmatch(r"/[A-Za-z0-9/_.-]{1,200}", log_dir) and ".." not in log_dir):
    print("datalink: log_dir %r ignored (plain absolute path only) - logging off" % (log_dir,), file=sys.stderr)
    log_dir = ""
print(log_dir)
PY
)" || exit 1
mapfile -t A <<< "$ARGS"
read -r -a HZ <<< "${A[3]}"
LOG_OUT=()
LOGDIR="${A[4]:-}"
if [ -n "$LOGDIR" ]; then
    if [ -d "$LOGDIR" ] && [ -w "$LOGDIR" ]; then
        # local file only, one file per UTC day; nothing leaves the node
        LOG_OUT=(--output "decoded:json:file:path=$LOGDIR/vdl2.json,rotate=daily")
    else
        echo "datalink: log_dir $LOGDIR is missing or not writable - logging off" >&2
    fi
fi
exec "$DUMPVDL2" --rtlsdr "${A[0]}" --gain "${A[1]}" \
    --output "decoded:json:udp:address=127.0.0.1,port=${A[2]}" \
    --msg-filter all,-acars_nodata --utc "${LOG_OUT[@]}" "${HZ[@]}"
