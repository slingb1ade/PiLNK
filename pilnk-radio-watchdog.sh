#!/bin/bash
# pilnk-radio-watchdog — makes the radio self-healing.
#
# The pilnk_bridge v0.5.1 module exposes real sample-flow rates in
# /sdr/status (audioSps). This watchdog polls it; if the radio claims
# playing but zero samples flow for ~45s (USB dongle drop, driver hang),
# or the bridge stops answering entirely, it restarts sdrpp and resumes
# playback. Pairs with the "Receiver stalled" banner in the dashboard tab.
STATUS_URL="http://127.0.0.1:5656/sdr/status"
PLAY_URL="http://127.0.0.1:5656/sdr/playing"
FAILS=0
RESUME=0
while true; do
    sleep 15
    S=$(curl -s -m 5 "$STATUS_URL" 2>/dev/null)
    if [ -z "$S" ]; then
        FAILS=$((FAILS+1))
        echo "bridge unreachable (strike $FAILS)"
    else
        read -r PLAYING SPS <<< "$(echo "$S" | python3 -c 'import json,sys
d=json.load(sys.stdin)
print(str(d.get("playing")).lower(), int(d.get("audioSps",0)))' 2>/dev/null || echo "? 0")"
        if [ "$PLAYING" = "true" ] && [ "$SPS" -eq 0 ]; then
            FAILS=$((FAILS+1))
            echo "playing but zero sample flow (strike $FAILS)"
        else
            FAILS=0
        fi
        # Consent tracking (M4 fix, audit 2026-07-09). The consent gate / Local
        # Laws requirement means we must NEVER auto-resume a radio the operator
        # has stopped. So: playing WITH flow => consent given (latch on);
        # observed STOPPED => consent withdrawn (latch off). A stall
        # (playing && zero flow) leaves the latch unchanged, so genuine
        # dongle-drop recovery still resumes correctly.
        if [ "$PLAYING" = "true" ] && [ "$SPS" -gt 0 ]; then
            RESUME=1
        elif [ "$PLAYING" = "false" ]; then
            RESUME=0
        fi
    fi
    if [ "$FAILS" -ge 3 ]; then
        echo "radio stalled ~45s — restarting sdrpp"
        systemctl restart sdrpp
        FAILS=0
        # wait for the bridge to come back
        for i in $(seq 1 20); do
            sleep 3
            curl -s -m 3 "$STATUS_URL" >/dev/null 2>&1 && break
        done
        if [ "$RESUME" = "1" ]; then
            sleep 2
            curl -s -m 5 -X POST "$PLAY_URL" -d '{"on":true}' >/dev/null 2>&1
            echo "sdrpp restarted, playback resumed"
        else
            echo "sdrpp restarted (was not playing — leaving stopped)"
        fi
    fi
done
