#!/bin/bash
# pilnk-hw-setup.sh: claim-safe receiver rules. Everything external is faked.
. "$(dirname "$0")/lib.sh"
HW="$REPO/pilnk-hw-setup.sh"

# plan [env...]: run --plan on the fake root; sets OUT, RC, and field lookups.
plan() {
  OUT=$(env PILNK_ROOT="$ROOT" PILNK_DIR="$ROOT/pilnk" PILNK_HW_NONINTERACTIVE=1 "$@" bash "$HW" --plan 2>/dev/null); RC=$?
}
f() { printf '%s\n' "$OUT" | sed -n "s/^$1=//p"; }

# --- healthy readsb ---
mkroot; active readsb; aircraft readsb
snap() { (cd "$ROOT" && find . -path ./.calls -prune -o -type f -print0 | sort -z | xargs -0 md5sum | md5sum); }
before=$(snap); plan; after=$(snap)
ok "readsb: consume"        '[ "$(f DECODER_MODE)" = consume ]'
ok "readsb: json path"      '[ "$(f AIRCRAFT_JSON_PATH)" = /run/readsb/aircraft.json ]'
ok "readsb: exit 0"         '[ $RC -eq 0 ]'
ok "plan: exactly 5 lines in order" '[ "$(printf "%s\n" "$OUT" | cut -d= -f1 | paste -sd, -)" = "DECODER_MODE,ADSB_SERIAL,VHF_SERIAL,AIRCRAFT_JSON_PATH,SVC_AFTER" ]'
ok "plan changes nothing"   '[ "$before" = "$after" ]'

# --- healthy dump1090-fa ---
mkroot; active dump1090-fa; aircraft dump1090-fa; plan
ok "dump1090-fa: consume"   '[ "$(f DECODER_MODE)" = consume ]'
ok "dump1090-fa: json path" '[ "$(f AIRCRAFT_JSON_PATH)" = /run/dump1090-fa/aircraft.json ]'
ok "dump1090-fa: svc"       '[ "$(f SVC_AFTER)" = dump1090-fa.service ]'

# --- Airspy only ---
mkroot; airspy; plan
ok "airspy: mode"           '[ "$(f DECODER_MODE)" = airspy ]'

# --- one RTL ---
mkroot; rtl 00001090; plan
ok "1 rtl: rtlsdr"          '[ "$(f DECODER_MODE)" = rtlsdr ]'
ok "1 rtl: adsb 1090"       '[ "$(f ADSB_SERIAL)" = 00001090 ]'
ok "1 rtl: vhf empty"       'printf "%s\n" "$OUT" | grep -qx "VHF_SERIAL="'

# --- two RTL, unattended pick rule ---
mkroot; rtl 00000002 00001090; plan
ok "2 rtl (1090): adsb 1090" '[ "$(f ADSB_SERIAL)" = 00001090 ]'
ok "2 rtl (1090): vhf 0002"  '[ "$(f VHF_SERIAL)" = 00000002 ]'
mkroot; rtl 00001090 00000002; plan
ok "2 rtl, other enum order: same answer" '[ "$(f ADSB_SERIAL)/$(f VHF_SERIAL)" = 00001090/00000002 ]'
mkroot; rtl 00000007 00000003; plan
ok "2 rtl (no 1090): adsb lowest" '[ "$(f ADSB_SERIAL)" = 00000003 ]'
ok "2 rtl (no 1090): vhf next"    '[ "$(f VHF_SERIAL)" = 00000007 ]'

# --- claimed dongle is never taken ---
mkroot; active dump1090-fa; echo 'RECEIVER_SERIAL=00000003' > "$ROOT/etc/default/dump1090-fa"; rtl 00000003 00000007; plan
ok "claimed 3, free 7: rtlsdr"      '[ "$(f DECODER_MODE)" = rtlsdr ]'
ok "claimed 3, free 7: adsb 7"      '[ "$(f ADSB_SERIAL)" = 00000007 ]'
ok "claimed 3, free 7: vhf empty"   '[ -z "$(f VHF_SERIAL)" ]'
mkroot; active dump1090-fa; echo 'RECEIVER_SERIAL=00000003' > "$ROOT/etc/default/dump1090-fa"; rtl 00000003; plan
ok "all claimed: none, exit 2"      '[ "$(f DECODER_MODE)" = none ] && [ $RC -eq 2 ]'

# --- fr24feed in direct mode: unnamed claim, VHF stays off ---
mkroot; active fr24feed; echo 'receiver="dvbt"' > "$ROOT/etc/fr24feed.ini"; rtl 00000002 00001090; plan
ok "fr24 direct: adsb from free set rule" '[ "$(f ADSB_SERIAL)" = 00001090 ]'
ok "fr24 direct: vhf empty"               '[ -z "$(f VHF_SERIAL)" ] && [ "$(f DECODER_MODE)" = rtlsdr ]'

# --- fr24feed network mode holds nothing ---
mkroot; active fr24feed; echo 'receiver="beast-tcp"' > "$ROOT/etc/fr24feed.ini"; rtl 00000002 00001090; plan
ok "fr24 network: vhf allowed"      '[ "$(f VHF_SERIAL)" = 00000002 ]'

# --- nothing ---
mkroot; plan
ok "nothing: none"                  '[ "$(f DECODER_MODE)" = none ]'
ok "nothing: exit 2"                '[ $RC -eq 2 ]'

# --- Airspy + healthy dump1090-fa: unattended must not remove it ---
mkroot; airspy; active dump1090-fa; aircraft dump1090-fa; plan
ok "airspy+dump1090 unattended: exit 1" '[ $RC -eq 1 ]'

# --- interactive: two free dongles, answer 2 on the (fake) tty ---
mkroot; rtl 00000003 00000007; echo 2 > "$ROOT/dev/tty"
OUT=$(env PILNK_ROOT="$ROOT" PILNK_DIR="$ROOT/pilnk" bash "$HW" --plan 2>/dev/null); RC=$?
ok "interactive: answer 2 -> second line" '[ "$(f ADSB_SERIAL)" = 00000007 ] && [ "$(f VHF_SERIAL)" = 00000003 ]'
ok "interactive: prompt went to tty not stdout" 'grep -q "Which line" "$ROOT/dev/tty" && ! printf "%s" "$OUT" | grep -q "Which line"'

# --- apply ---
apply() {
  OUT=$(env PILNK_ROOT="$ROOT" PILNK_DIR="$ROOT/pilnk" PILNK_HW_NONINTERACTIVE=1 bash "$HW" --apply 2>"$ROOT/.stderr"); RC=$?
}
mkdir_pilnk() { mkdir -p "$ROOT/pilnk"; fakebin_radio; }
fakebin_radio() { printf '#!/bin/bash\necho "RADIO CONFIG_ONLY=${PILNKRADIO_CONFIG_ONLY:-} NONINT=${PILNKRADIO_NONINTERACTIVE:-} SERIAL=${PILNKRADIO_SERIAL:-}" >> "$CALLS"\n' > "$ROOT/pilnk/pilnkradio-install.sh"; }

mkroot; mkdir_pilnk; rtl 00000002 00001090
echo 'RECEIVER_OPTIONS="--device-type rtlsdr --gain auto"' > "$ROOT/etc/default/readsb"
echo '{"pilnk_code":"ABC123","auto_update":false,"aircraft_json_path":"/old"}' > "$ROOT/pilnk/config.json"
apply
ok "apply: exit 0"                  '[ $RC -eq 0 ]'
ok "apply: readsb --device pinned"  'grep -q -- "--device 00001090" "$ROOT/etc/default/readsb"'
ok "apply: readsb options kept"     'grep -q -- "--gain auto" "$ROOT/etc/default/readsb"'
ok "apply: config keeps pilnk_code" 'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"pilnk_code\"]==\"ABC123\" and d[\"auto_update\"] is False" "$ROOT/pilnk/config.json"'
ok "apply: config keys written"     'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"aircraft_json_path\"]==\"/run/readsb/aircraft.json\" and d[\"vhf_serial\"]==\"00000002\" and isinstance(d[\"dashboard_port\"],int)" "$ROOT/pilnk/config.json"'
ok "apply: radio config-only called with VHF serial" 'grep -qx "RADIO CONFIG_ONLY=1 NONINT=1 SERIAL=00000002" "$ROOT/.calls"'
ok "apply: no host systemctl restart on fake root" '! grep -q "^systemctl restart" "$CALLS"'

# existing --device is replaced, not duplicated
mkroot; mkdir_pilnk; rtl 00001090
echo 'RECEIVER_OPTIONS="--device 99 --gain auto"' > "$ROOT/etc/default/readsb"
apply
ok "apply: existing --device replaced" '[ "$(grep -o -- "--device" "$ROOT/etc/default/readsb" | wc -l)" -eq 1 ] && grep -q -- "--device 00001090" "$ROOT/etc/default/readsb"'

# single dongle: no VHF -> pilnkradio NOT called; fresh config has auto_update
mkroot; mkdir_pilnk; rtl 00001090; echo 'RECEIVER_OPTIONS=""' > "$ROOT/etc/default/readsb"
apply
ok "apply: no VHF -> pilnkradio not called" '! grep -q "^RADIO" "$CALLS"'
ok "apply: fresh config created"           'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"vhf_serial\"]==\"\" and d[\"auto_update\"] is True" "$ROOT/pilnk/config.json"'

# consume: writes config, no pin, no radio
mkroot; mkdir_pilnk; active readsb; aircraft readsb; echo 'RECEIVER_OPTIONS=""' > "$ROOT/etc/default/readsb"
apply
ok "apply consume: readsb untouched" '! grep -q -- "--device" "$ROOT/etc/default/readsb"'
ok "apply consume: config written"   'grep -q "/run/readsb/aircraft.json" "$ROOT/pilnk/config.json"'

# nothing: exit 2 and nothing written
mkroot; mkdir_pilnk; apply
ok "apply nothing: exit 2"           '[ $RC -eq 2 ]'
ok "apply nothing: no config.json"   '[ ! -e "$ROOT/pilnk/config.json" ]'

# corrupt config is not clobbered
mkroot; mkdir_pilnk; rtl 00001090; echo 'RECEIVER_OPTIONS=""' > "$ROOT/etc/default/readsb"; echo 'not json' > "$ROOT/pilnk/config.json"
apply
ok "apply: corrupt config -> exit 1, file kept" '[ $RC -eq 1 ] && [ "$(cat "$ROOT/pilnk/config.json")" = "not json" ]'

# no args / bad args
mkroot
ok "no args: exit 1" '! bash "$HW" >/dev/null 2>&1'

# --- pilnkradio-install.sh PILNKRADIO_CONFIG_ONLY=1: no compile, no network ---
mkroot
for c in sudo apt-get cmake make git curl wget journalctl sleep udevadm nproc; do fakebin $c 'exit 0'; done
fakebin nproc 'echo 2'
mkdir -p "$ROOT/home/pilnk"
env HOME="$ROOT/home" PILNKRADIO_STATE_FILE="$ROOT/state.json" PILNKRADIO_NONINTERACTIVE=1 \
    PILNKRADIO_CONFIG_ONLY=1 PILNKRADIO_SERIAL=00000002 PILNKRADIO_FREQ=118.7 \
    bash "$REPO/pilnkradio-install.sh" > "$ROOT/.radio_out" 2>&1; RC=$?
ok "config-only: reaches the radio dongle step"  'grep -q "6/8 radio dongle" "$ROOT/.radio_out"'
ok "config-only: skips deps/fork/build"          '! grep -qE "^(apt-get|cmake|make|git|wget) " "$CALLS" && ! grep -q "2/8 build dependencies\|4/8 build" "$ROOT/.radio_out"'
ok "config-only: installs unit + udev rule"      'grep -q "^sudo cp .*pilnkradio.service" "$CALLS" && grep -q "^sudo cp .*99-pilnk-v4.rules" "$CALLS"'
ok "config-only: uses the given serial"          'grep -q "00000002" "$ROOT/.radio_out"'

# --- sourced (as install.sh does): defines functions only, then works under set -e ---
mkroot; rtl 00000003 00000007; echo 2 > "$ROOT/dev/tty"
echo 'RECEIVER_OPTIONS=""' > "$ROOT/etc/default/readsb"
SRCOUT=$(env PILNK_ROOT="$ROOT" bash -c 'set -e; . "$1"; echo SOURCED_QUIET; hw_decide >/dev/null 2>&1; hw_pin_readsb >/dev/null 2>&1; hw_dashboard_port >/dev/null 2>&1
   echo "$DECODER_MODE $ADSB_SERIAL $VHF_SERIAL $DASHBOARD_PORT"' _ "$HW" 2>&1)
ok "sourced: no output / no action on source"   '[ "$(printf "%s\n" "$SRCOUT" | head -1)" = SOURCED_QUIET ]'
ok "sourced: interactive decision + pin + port" 'printf "%s\n" "$SRCOUT" | tail -1 | grep -qE "^rtlsdr 00000007 00000003 50[0-9][0-9]$" && grep -q -- "--device 00000007" "$ROOT/etc/default/readsb"'
