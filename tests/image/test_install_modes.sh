#!/bin/bash
# install.sh PILNK_IMAGE=1, update.sh PILNK_UPDATE_NO_RESTART=1,
# pilnkradio-install.sh PILNKRADIO_SKIP_CONFIG=1. Everything external is faked;
# nothing reaches the host (sudo, apt, pip, git, wget, curl, systemctl are fakes).
. "$(dirname "$0")/lib.sh"

# ---------- install.sh harness ----------
# run_install [env...]: runs a COPY of install.sh (beside a stub pilnk-hw-setup.sh
# that only logs) under a pty (install.sh insists on /dev/tty). Sets RC, TTYOUT.
run_install() {
  mkroot
  export HOME="$ROOT/home" USER=pilnk TERM=dumb
  mkdir -p "$HOME" "$ROOT/inst" "$ROOT/tee"
  cp "$REPO/install.sh" "$ROOT/inst/install.sh"
  cat > "$ROOT/inst/pilnk-hw-setup.sh" <<'STUB'
hw_decide() { echo "pilnk-hw-setup.sh hw_decide" >> "$CALLS"
  DECODER_MODE=rtlsdr; ADSB_SERIAL=00000001; VHF_SERIAL=""; AIRCRAFT_JSON_PATH=/run/readsb/aircraft.json; SVC_AFTER=readsb.service; }
hw_pin_readsb() { echo "pilnk-hw-setup.sh hw_pin_readsb" >> "$CALLS"; }
hw_dashboard_port() { echo "pilnk-hw-setup.sh hw_dashboard_port" >> "$CALLS"; DASHBOARD_PORT=5000; }
STUB
  fakebin clear ''; fakebin curl ''; fakebin wget ''; fakebin apt-get ''; fakebin pip3 ''; fakebin pip ''
  fakebin hostname 'echo 192.0.2.7'
  fakebin sudo 'case "$1" in tee) mkdir -p "$ROOT/tee"; cat > "$ROOT/tee/$(basename "${@: -1}")";; esac'
  fakebin git 'case "$1" in clone) d="${@: -1}"; mkdir -p "$d"
      (cd "$REPO" && tar cf - --exclude=.git --exclude=.superpowers .) | tar xf - -C "$d"
      for s in pilnkradio-install.sh bootstrap-selfheal.sh; do
        printf "#!/bin/bash\necho \"$s SKIP_CONFIG=\${PILNKRADIO_SKIP_CONFIG:-} NONINTERACTIVE=\${PILNKRADIO_NONINTERACTIVE:-}\" >> \"\$CALLS\"\n" > "$d/$s"; done;; esac'
  export REPO
  TTYOUT=$(printf 'y\n\n\n' | env "$@" python3 -c 'import pty,sys,os
sys.exit(os.waitstatus_to_exitcode(pty.spawn(["bash","'"$ROOT"'/inst/install.sh"])))' 2>&1)
  RC=$?
}
calls() { cat "$CALLS"; }

# ---------- image mode ----------
run_install PILNK_IMAGE=1
ok "image: exit 0"                         '[ $RC -eq 0 ]'
ok "image: no systemctl start/restart/enable/reset-failed of pilnk" '! calls | grep -Eq "systemctl.*(enable|start|restart|reset-failed).* pilnk( |$)"'
ok "image: no pilnk-hw-setup.sh call"      '! calls | grep -q "pilnk-hw-setup"'
ok "image: no bootstrap-selfheal.sh"       '! calls | grep -q "bootstrap-selfheal"'
ok "image: one pilnkradio-install.sh with SKIP_CONFIG=1 + NONINTERACTIVE=1" '[ "$(calls | grep -c "^pilnkradio-install.sh")" = 1 ] && calls | grep -q "^pilnkradio-install.sh SKIP_CONFIG=1 NONINTERACTIVE=1"'
ok "image: config.json byte-equal to template" 'cmp -s "$HOME/pilnk/config.json" "$REPO/image/config.template.json"'
ok "image: template exact contents"        '[ "$(cat "$REPO/image/config.template.json")" = "{\"auto_update\": true, \"aircraft_json_path\": \"/run/readsb/aircraft.json\", \"vhf_serial\": \"\", \"dashboard_port\": 5000}" ]'
ok "image: readsb installed (unpinned) then disabled" 'calls | grep -q "^sudo bash -c" && calls | grep -q "^sudo systemctl disable --now readsb$"'
ok "image: pilnk.service written"          'grep -q "^ExecStart=.*app.py" "$ROOT/tee/pilnk.service"'
ok "image: sudoers rule validated+installed" 'calls | grep -q "^sudo visudo -c" && calls | grep -q "^sudo install .*pilnk-ota"'
ok "image: no Ready-to-install prompt"     '! printf "%s" "$TTYOUT" | grep -q "Ready to install"'

# ---------- normal mode ----------
run_install PILNK_NORMAL=1
ok "normal: exit 0"                        '[ $RC -eq 0 ]'
ok "normal: sources helper, calls hw_decide, hw_pin_readsb, hw_dashboard_port" 'for f in hw_decide hw_pin_readsb hw_dashboard_port; do calls | grep -q "^pilnk-hw-setup.sh $f$" || exit 1; done'
ok "normal: sudo systemctl restart pilnk"  'calls | grep -q "^sudo systemctl restart pilnk$"'
ok "normal: sudo systemctl enable pilnk"   'calls | grep -q "^sudo systemctl enable pilnk"'
ok "normal: bootstrap-selfheal.sh runs"    'calls | grep -q "^bootstrap-selfheal.sh"'
ok "normal: pilnkradio-install.sh without SKIP_CONFIG" 'calls | grep -q "^pilnkradio-install.sh SKIP_CONFIG= NONINTERACTIVE=1"'
ok "normal: readsb is not disabled"        '! calls | grep -q "systemctl disable"'
ok "normal: Ready-to-install prompt shown" 'printf "%s" "$TTYOUT" | grep -q "Ready to install"'

# ---------- update.sh ----------
# run_update VERSION_BEFORE VERSION_AFTER [env...]; sets RC.
run_update() {
  local before="$1" after="$2"; shift 2
  mkroot
  export HOME="$ROOT/home"; P="$HOME/pilnk"; mkdir -p "$P"
  echo "$before" > "$P/VERSION"; echo "$after" > "$ROOT/.newver"
  printf 'from whisper_atc import x\n' > "$P/app.py"
  printf '#!/bin/bash\n' > "$P/bootstrap-selfheal.sh"
  printf '#!/bin/bash\n' > "$P/pilnkradio-install.sh"
  fakebin bash 'exec /bin/bash "$@"'   # log bash invocations (bootstrap-selfheal.sh)
  fakebin git 'case "$1" in reset) cp "$ROOT/.newver" VERSION;; esac'
  fakebin sudo ''
  fakebin sleep ''
  env "$@" bash "$REPO/update.sh" >/dev/null 2>&1; RC=$?
}
run_update 1.0.0 1.0.1 PILNK_UPDATE_NO_RESTART=1
ok "no-restart: exit 0 on update"          '[ $RC -eq 0 ]'
ok "no-restart: fetch and reset still run" 'calls | grep -q "^git fetch origin main" && calls | grep -q "^git reset --hard origin/main"'
ok "no-restart: whisper guard still runs"  '! grep -q "^from whisper_atc" "$P/app.py"'
ok "no-restart: no restart of pilnk"       '! calls | grep -q "systemctl restart pilnk"'
ok "no-restart: no self-heal bootstrap"    '! calls | grep -q "bootstrap-selfheal"'
ok "no-restart: no audio build trigger"    '! calls | grep -q "pilnk-audio-build"'
run_update 1.0.0 1.0.0 PILNK_UPDATE_NO_RESTART=1
ok "no-restart: exit 0 when VERSION unchanged" '[ $RC -eq 0 ]'
ok "no-restart: unchanged still no restart" '! calls | grep -q "systemctl restart pilnk"'
run_update 1.0.0 1.0.1 PILNK_X=1
ok "default: self-heal bootstrap runs"     'calls | grep -q "bootstrap-selfheal"'
ok "default: audio build triggered"        'calls | grep -q "systemctl start --no-block pilnk-audio-build"'
ok "default: sudo -n systemctl restart pilnk" 'calls | grep -q "^sudo -n systemctl restart pilnk$"'
run_update 1.0.0 1.0.0 PILNK_X=1
ok "default: unchanged VERSION still exits 3" '[ $RC -eq 3 ]'
ok "default: unchanged VERSION still no restart" '! calls | grep -q "systemctl restart pilnk"'

# ---------- pilnkradio-install.sh PILNKRADIO_SKIP_CONFIG ----------
# CONFIG_ONLY skips the compile; the engine side is faked.
run_radio() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME/pilnk" "$ROOT/tee"
  fakebin sudo 'case "$1" in tee) cat > "$ROOT/tee/$(basename "${@: -1}")";; esac'
  fakebin curl 'exit 0'; fakebin journalctl ''; fakebin udevadm ''; fakebin hostname 'echo testpi'
  env PILNKRADIO_STATE_FILE="$ROOT/state.json" PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_CONFIG_ONLY=1 \
      PILNKRADIO_SERIAL=00000042 "$@" bash "$REPO/pilnkradio-install.sh" >/dev/null 2>&1; RC=$?
}
run_radio
ok "radio control: config.json written without the flag" '[ $RC -eq 0 ] && [ -s "$ROOT/tee/config.json" ]'
ok "radio CONFIG_ONLY: udev rule installed + reloaded" 'calls | grep -q "^sudo cp .*99-pilnk-v4.rules /etc/udev/rules.d/" && calls | grep -q "^sudo udevadm control --reload"'
run_radio PILNKRADIO_SKIP_CONFIG=1
ok "radio SKIP_CONFIG: exit 0"             '[ $RC -eq 0 ]'
ok "radio SKIP_CONFIG: no config.json written" '[ ! -e "$ROOT/tee/config.json" ] && ! calls | grep -q "mkdir -p /etc/pilnkradio"'
ok "radio SKIP_CONFIG: engine not enabled/started" '! calls | grep -Eq "systemctl (enable|restart|start) pilnkradio"'
ok "radio SKIP_CONFIG: no udev wake rule copied" '! calls | grep -q "99-pilnk-v4.rules"'
ok "radio SKIP_CONFIG: udev not reloaded" '! calls | grep -q "udevadm control"'
ok "radio SKIP_CONFIG: unit still copied + daemon-reload" 'calls | grep -q "^sudo cp .*pilnkradio.service /etc/systemd/system/" && calls | grep -q "systemctl daemon-reload"'

# ---------- the Pi 5 must be able to run the build ----------
ok "pilnkradio/CMakeLists.txt has no -march/-mcpu/-mtune" '! grep -Eq -- "-m(arch|cpu|tune)" "$REPO/pilnkradio/CMakeLists.txt"'
