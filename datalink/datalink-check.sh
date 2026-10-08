#!/usr/bin/env bash
# PiLNK datalink start-up self-check (ExecStartPost of pilnk-datalink.service).
# Writes /run/pilnk-datalink/status.json saying which librtlsdr the decoder
# really loaded. The stock Debian driver leaves an RTL-SDR Blog V4 ~10 dB deaf
# with no error anywhere; this turns that silence into a visible driver_ok:false.
# driver_ok: true (fork), false (stock or none), null (dumpvdl2 not exec'd yet).
# Never fails the unit: a wrong driver is reported, not fatal.
PROC="${PILNK_DL_PROC:-/proc}"
STATUS="${PILNK_DL_STATUS:-/run/pilnk-datalink/status.json}"
CFG="${PILNK_DL_CFG:-/etc/pilnk-datalink/config.json}"
sleep "${PILNK_DL_CHECK_WAIT:-3}"
PID="${MAINPID:-}"
[ -n "$PID" ] || PID="$(pidof dumpvdl2 2>/dev/null || true)"; PID="${PID%% *}"
python3 - "$PROC" "$PID" "$STATUS" "$CFG" <<'PY' || true
import json, os, re, sys, time
proc, pid, status, cfg = sys.argv[1:5]
lib, running, started = "", False, False
# The unit's main PID is datalink-run.sh until it execs dumpvdl2; on a slow Pi
# that can take a moment. Only judge the driver once the PID IS dumpvdl2.
tries = int(os.environ.get("PILNK_DL_CHECK_TRIES", "10"))
for i in range(max(tries, 1)):
    try:
        started = os.path.basename(os.readlink(os.path.join(proc, pid, "exe"))) == "dumpvdl2"
    except Exception:
        started = False
    if started:
        break
    if i + 1 < tries:
        time.sleep(1)
try:
    with open(os.path.join(proc, pid, "maps")) as f:
        running = True
        for line in f:
            m = re.search(r"(/\S*librtlsdr\S*)", line)
            if m:
                lib = m.group(1)
                break
except Exception:
    pass
try:
    serial = str(json.load(open(cfg)).get("serial", ""))
except Exception:
    serial = ""
ok = bool(re.match(r"^/usr/local/lib(/[^/]+)?/librtlsdr", lib)) if started or not running else None
os.makedirs(os.path.dirname(status), exist_ok=True)
tmp = status + ".tmp"
with open(tmp, "w") as f:
    json.dump({"running": running, "driver_ok": ok, "lib": lib, "serial": serial, "ts": int(time.time())}, f)
os.replace(tmp, status)
PY
exit 0
