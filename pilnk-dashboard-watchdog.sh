#!/bin/bash
# pilnk-dashboard-watchdog — makes the PiLNK dashboard + ping loop self-healing.
#
# WHY THIS EXISTS. pilnk.service already has Restart=always, so systemd revives a
# process that EXITS. But a process that is still ALIVE yet WEDGED — the Flask
# worker hung, or the ping-loop thread dead while the rest of the process runs on
# — never exits, so systemd never acts, and the node goes dark to the fleet with
# no signal. That is the exact failure class Restart= cannot see, and the reason a
# node can sit "up" locally while it has stopped reporting for hours.
#
# WHAT IT WATCHES. /api/health on the dashboard port (the lightest route on the
# node). Two triggers, BOTH of them LOCAL faults a restart actually fixes:
#   - the endpoint is unreachable for STRIKES_MAX polls  → worker wedged or dead
#   - the endpoint answers but loop_ago_sec is old       → the ping THREAD died
# It deliberately does NOT restart on ping_ok_ago_sec (time since the last
# SUCCESSFUL ping to pilnk.io). That can be stale simply because pilnk.io is down,
# and restarting the node would not help — worse, a single server outage would
# bounce every node in the fleet at once. Watch the loop, not the ping.
#
# Runs as root (the unit sets no User=, same as pilnk-radio-watchdog), so the
# `systemctl restart pilnk` below needs no sudo.

# Resolve the repo this watchdog lives in, so the config path is right whatever
# the node's home is (aj here, pi on most nodes).
PILNK_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -z "$PILNK_DIR" ] && PILNK_DIR="/home/aj/pilnk"
CONFIG="$PILNK_DIR/config.json"

# Dashboard port from config.json (default 5000). NEVER hardcode 5000: a PiAware
# node runs its own service on 5000 and PiLNK is pushed to 5001+ at install, so a
# hardcoded port would watch the wrong thing entirely (the #100 lesson).
read_port() {
    python3 - "$CONFIG" <<'PY' 2>/dev/null || echo 5000
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(int(json.load(f).get("dashboard_port", 5000)))
except Exception:
    print(5000)
PY
}
PORT="$(read_port)"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

POLL=20            # seconds between checks
STRIKES_MAX=3      # consecutive bad polls before a restart (~60s sustained fault)
LOOP_STALE=120     # loop_ago_sec beyond this = ping thread presumed dead (~8 missed 15s loops)
BOOT_GRACE=60      # let a fresh process settle before judging it

echo "pilnk-dashboard-watchdog up — polling $HEALTH_URL every ${POLL}s"
sleep "$BOOT_GRACE"
STRIKES=0

while true; do
    sleep "$POLL"
    BODY="$(curl -s -m 8 "$HEALTH_URL" 2>/dev/null)"

    if [ -z "$BODY" ]; then
        STRIKES=$((STRIKES+1))
        echo "health unreachable on :$PORT (strike $STRIKES/$STRIKES_MAX)"
    else
        # -1 = loop_ago_sec was null (loop not yet stamped — just booted, no strike)
        # -2 = answered but unparseable
        LOOP_AGO="$(printf '%s' "$BODY" | python3 -c 'import json,sys
try:
    v=json.load(sys.stdin).get("loop_ago_sec")
    print(int(v) if v is not None else -1)
except Exception:
    print(-2)' 2>/dev/null || echo -2)"

        if [ "$LOOP_AGO" = "-2" ]; then
            STRIKES=$((STRIKES+1))
            echo "health answered but unparseable (strike $STRIKES/$STRIKES_MAX)"
        elif [ "$LOOP_AGO" -ge 0 ] && [ "$LOOP_AGO" -gt "$LOOP_STALE" ]; then
            STRIKES=$((STRIKES+1))
            echo "ping loop stale ${LOOP_AGO}s > ${LOOP_STALE}s — thread presumed dead (strike $STRIKES/$STRIKES_MAX)"
        else
            STRIKES=0
        fi
    fi

    if [ "$STRIKES" -ge "$STRIKES_MAX" ]; then
        echo "pilnk locally faulted — restarting pilnk.service"
        systemctl restart pilnk
        STRIKES=0
        # Wait for /api/health to answer again before resuming judgement, so we do
        # not immediately re-strike a process that is still starting up.
        for i in $(seq 1 15); do
            sleep 4
            curl -s -m 5 "$HEALTH_URL" >/dev/null 2>&1 && break
        done
        sleep "$BOOT_GRACE"
    fi
done
