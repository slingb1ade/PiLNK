#!/bin/bash
# shellcheck disable=SC2034,SC2016
# Root-run scripts leave the PiLNK user's space alone (Part B spec 5.3, plan
# Task 4): the audio build runs from root's signed copy, builds in
# /var/lib/pilnk/build, builds the pinned rtl-sdr-blog commit, reads the
# user's task file and config.json as data only, and writes nothing in the
# user's home. Build tools, git and sudo are faked.
#
# Where unprivileged user namespaces work (unshare -r -m), the build runs as
# uid 0 with a fake fork library bind-mounted over /usr/local/lib, so it gets
# past the fork check to the engine build. Elsewhere those checks print SKIP.
. "$(dirname "$0")/lib.sh"
PIN=$(tr -d '[:space:]' < "$REPO/pilnkradio/rtl-sdr-blog.commit" 2>/dev/null)
USERNS=0
unshare -r -m true 2>/dev/null && USERNS=1
skip() { echo "SKIP: $1 (no user namespace here)"; }

# A signed copy at $CUR, the PiLNK user's home at $UH, root's build area $RB.
rb_setup() {
  mkroot
  CUR="$ROOT/current"; UH="$ROOT/home"; RB="$ROOT/build"; ST="$ROOT/state"
  mkdir -p "$CUR/pilnkradio" "$UH/pilnk" "$RB" "$ST" "$ROOT/roothome" "$ROOT/lib"
  cp "$REPO/pilnkradio-install.sh" "$CUR/"
  printf '#!/bin/bash\necho "DENOISE $*" >> "$CALLS"\n' > "$CUR/denoise-install.sh"
  touch "$CUR/pilnkradio/main.cpp"
  cp "$REPO/pilnkradio/rtl-sdr-blog.commit" "$REPO/pilnkradio/pilnkradio.service" \
     "$REPO/pilnkradio/99-pilnk-v4.rules" "$CUR/pilnkradio/"
  echo '{"dashboard_port": 5050}' > "$UH/pilnk/config.json"
  echo "$PIN" > "$ROOT/.git_head"
  touch "$ROOT/lib/librtlsdr.so"
  fakebin sudo 'case "$1" in tee) cat > /dev/null ;; esac; exit 0'
  for c in apt-get cmake ldconfig chown journalctl udevadm curl; do fakebin "$c" 'exit 0'; done
  fakebin nproc 'echo 2'; fakebin hostname 'echo testpi'
  fakebin ldd 'echo "	librtlsdr.so.0 => /usr/local/lib/librtlsdr.so.0 (0x0)"'
  fakebin make 'd=""; while [ $# -gt 0 ]; do [ "$1" = -C ] && { d="$2"; shift; }; shift; done
case "$d" in */pilnkradio|*/pilnkradio/build) mkdir -p "$d"; printf "#!/bin/bash\nexit 0\n" > "$d/pilnkradio"; chmod +x "$d/pilnkradio" ;; esac; exit 0'
  fakebin git 'case " $* " in *" init "*) mkdir -p "${@: -1}/.git" ;; *" rev-parse HEAD"*) cat "$ROOT/.git_head" ;; esac; exit 0'
}
# run SCRIPT with ENV... ; as uid 0 with the fake fork when a namespace is available
ns_run() {
  if [ "$USERNS" = 1 ]; then
    unshare -r -m bash -c 'mount --bind "$1" /usr/local/lib && shift && exec "$@"' _ "$ROOT/lib" "$@"
  else
    "$@"
  fi
}
# the audio build as the rendered unit runs it (root-build mode)
rb_run() {
  ns_run env HOME="$ROOT/roothome" PILNK_ROOT_BUILD="$RB" PILNKRADIO_STATE_FILE="$ST/audio_build_state.json" \
    PILNK_APP_DIR="$UH/pilnk" PILNK_AUDIO_TASK_FILE="$UH/pilnk/.audio_build_task" \
    PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_SKIP_CONFIG=1 "$@" bash "$CUR/pilnkradio-install.sh" > "$ROOT/out" 2>&1 < /dev/null
  RC=$?
}
step_is() { python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("step") == sys.argv[2] else 1)' "$ST/audio_build_state.json" "$1" 2>/dev/null; }
task() { printf '%s' "$1" > "$UH/pilnk/.audio_build_task"; }

# --- the task file is one word from a fixed list, read as data
rb_setup; task $'denoise\n'; rb_run
ok rb_task_denoise_word '[ "$RC" = 0 ] && grep -q "^DENOISE" "$CALLS" && ! grep -q "^cmake" "$CALLS"'
rb_setup; task "denoise; touch $ROOT/x"; rb_run
ok rb_task_shell_text_is_full '! grep -q "^DENOISE" "$CALLS" && grep -q "^cmake" "$CALLS" && [ ! -e "$ROOT/x" ]'
rb_setup; printf 'denoise\n' > "$ROOT/real"; ln -s "$ROOT/real" "$UH/pilnk/.audio_build_task"; rb_run
ok rb_task_symlink_is_full '! grep -q "^DENOISE" "$CALLS" && grep -q "^cmake" "$CALLS"'
rb_setup; mkfifo "$UH/pilnk/.audio_build_task"; t0=$(date +%s); rb_run; t1=$(date +%s)
ok rb_task_fifo_no_hang '! grep -q "^DENOISE" "$CALLS" && [ $((t1 - t0)) -lt 3 ]'
rb_setup; { printf 'denoise\n'; head -c 10485760 /dev/zero | tr '\0' x; } > "$UH/pilnk/.audio_build_task"; rb_run
ok rb_task_10mb_is_full '! grep -q "^DENOISE" "$CALLS" && grep -q "^cmake" "$CALLS"'
# a denoise request is acted on once; root never deletes the user's file to say so
rb_setup; task $'denoise\n'; rb_run; : > "$CALLS"; rb_run
ok rb_task_acted_on_once '! grep -q "^DENOISE" "$CALLS" && grep -q "^cmake" "$CALLS" && [ -f "$UH/pilnk/.audio_build_task" ]'
sleep 0.05; task $'denoise\n'; : > "$CALLS"; rb_run
ok rb_task_new_request_acted_on '[ "$RC" = 0 ] && grep -q "^DENOISE" "$CALLS"'

# --- nothing in the user's home is written, chowned or built
rb_setup; mkdir -p "$UH/rtl-sdr-blog/.git"; touch "$ROOT/stamp"; sleep 1; rb_run
ok rb_root_build_writes_no_user_space '[ -z "$(find "$UH" -newer "$ROOT/stamp" -print)" ] && ! grep -q "^chown" "$CALLS" && ! grep -qF "$UH/rtl-sdr-blog" "$CALLS"'
if [ "$USERNS" = 1 ]; then
  ok rb_root_build_out_of_source '[ "$RC" = 0 ] && grep -qx "cmake -S $CUR/pilnkradio -B $RB/pilnkradio -DRTLSDR_PREFIX=/usr/local" "$CALLS" && grep -q "^make -C $RB/pilnkradio" "$CALLS" && grep -q "selftest PASS" "$ROOT/out" && [ ! -e "$CUR/pilnkradio/build" ]'
else skip rb_root_build_out_of_source; fi
ok rb_rtlsdr_built_in_root_build 'grep -qx "cmake -B $RB/rtl-sdr-blog/build $RB/rtl-sdr-blog -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON" "$CALLS"'

# --- rtl-sdr-blog: exactly the pinned commit, checked after the fetch
ok rb_rtlsdr_pinned_commit '[[ $PIN =~ ^[0-9a-f]{40}$ ]] && grep -qE "fetch -q --depth 1 origin $PIN\$" "$CALLS" && grep -q "core.hooksPath=/dev/null" "$CALLS"'
rb_setup; echo 0123456789abcdef0123456789abcdef01234567 > "$ROOT/.git_head"; rb_run
ok rb_rtlsdr_wrong_head_refused '[ "$RC" != 0 ] && step_is rtlsdr && ! grep -q "^cmake -B $RB/rtl-sdr-blog" "$CALLS"'
rb_setup; echo nothex > "$CUR/pilnkradio/rtl-sdr-blog.commit"; rb_run
ok rb_rtlsdr_bad_pin_refused '[ "$RC" != 0 ] && step_is rtlsdr && ! grep -q " fetch " "$CALLS"'

# --- source is the script's own folder; no /tmp clone of main, ever
rb_setup; rm "$CUR/pilnkradio/main.cpp"; rb_run
ok rb_no_tmp_clone_of_main '[ "$RC" != 0 ] && step_is source && ! grep -q "clone.*--branch main" "$CALLS"'

# --- old mode (user-run install, or a node that has not switched): as before, plus the pin
rb_setup
cp "$REPO/pilnkradio-install.sh" "$UH/pilnk/"; mkdir -p "$UH/pilnk/pilnkradio"; touch "$UH/pilnk/pilnkradio/main.cpp"
cp "$REPO/pilnkradio/rtl-sdr-blog.commit" "$UH/pilnk/pilnkradio/"
ns_run env HOME="$UH" PILNKRADIO_STATE_FILE="$ST/audio_build_state.json" PILNKRADIO_NONINTERACTIVE=1 PILNKRADIO_SKIP_CONFIG=1 \
  bash "$UH/pilnk/pilnkradio-install.sh" > "$ROOT/out" 2>&1 < /dev/null; RC=$?
ok rb_old_mode_unchanged 'grep -qx "cmake -B $UH/rtl-sdr-blog/build $UH/rtl-sdr-blog -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON" "$CALLS" && grep -qE "fetch -q --depth 1 origin $PIN\$" "$CALLS" && ! grep -qF "$RB" "$CALLS"'
if [ "$USERNS" = 1 ]; then
  ok rb_old_mode_engine_in_tree '[ "$RC" = 0 ] && grep -qx "chown -R root:root $UH/rtl-sdr-blog" "$CALLS" && grep -qx "cmake -B $UH/pilnk/pilnkradio/build $UH/pilnk/pilnkradio -DRTLSDR_PREFIX=/usr/local" "$CALLS"'
else skip rb_old_mode_engine_in_tree; fi
rb_setup; cp "$REPO/pilnkradio-install.sh" "$UH/pilnk/"; printf '#!/bin/bash\necho "DENOISE $*" >> "$CALLS"\n' > "$UH/pilnk/denoise-install.sh"
task $'denoise\n'
env HOME="$UH" PILNKRADIO_STATE_FILE="$ST/audio_build_state.json" PILNKRADIO_NONINTERACTIVE=1 bash "$UH/pilnk/pilnkradio-install.sh" > "$ROOT/out" 2>&1 < /dev/null
ok rb_old_mode_task_removed 'grep -q "^DENOISE" "$CALLS" && [ ! -e "$UH/pilnk/.audio_build_task" ]'

# --- the dashboard watchdog: port only, from the app folder, as data
WD="$REPO/pilnk-dashboard-watchdog.sh"
wd_port() { PILNK_APP_DIR="$UH/pilnk" timeout 8 bash "$WD" --print-port 2>/dev/null; }
rb_setup; rm "$UH/pilnk/config.json"; mkfifo "$UH/pilnk/config.json"; t0=$(date +%s); P=$(wd_port); t1=$(date +%s)
ok wd_config_fifo_port_5000 '[ "$P" = 5000 ] && [ $((t1 - t0)) -lt 6 ]'
rb_setup; echo '{"dashboard_port": 6000}' > "$ROOT/other.json"; rm "$UH/pilnk/config.json"; ln -s "$ROOT/other.json" "$UH/pilnk/config.json"
ok wd_config_symlink_port_5000 '[ "$(wd_port)" = 5000 ]'
rb_setup
ok wd_config_port_read '[ "$(wd_port)" = 5050 ]'
for bad in '{"dashboard_port": 70000}' '{"dashboard_port": true}' '{"dashboard_port": "$(touch x)"}' 'not json' '[5050]'; do
  printf '%s' "$bad" > "$UH/pilnk/config.json"
  ok "wd_config_bad_port_5000 ($bad)" '[ "$(wd_port)" = 5000 ]'
done
rb_setup; mkdir -p "$ROOT/tree"; cp "$WD" "$ROOT/tree/"; echo '{"dashboard_port": 6060}' > "$ROOT/tree/config.json"
ok wd_old_layout_reads_own_folder '[ "$(timeout 8 bash "$ROOT/tree/pilnk-dashboard-watchdog.sh" --print-port 2>/dev/null)" = 6060 ]'

# --- the same reader inside the installer (the engine's allowed origins)
rb_setup
eval "$(sed -n '/^read_dash_port() {/,/^}/p' "$REPO/pilnkradio-install.sh")"
ok rb_dash_port_reader_valid '[ "$(read_dash_port "$UH/pilnk/config.json")" = 5050 ]'
echo '{"dashboard_port": 6000}' > "$ROOT/other.json"; rm "$UH/pilnk/config.json"; ln -s "$ROOT/other.json" "$UH/pilnk/config.json"
ok rb_dash_port_reader_symlink '[ "$(read_dash_port "$UH/pilnk/config.json")" = 5000 ]'
rm "$UH/pilnk/config.json"; mkfifo "$UH/pilnk/config.json"; t0=$(date +%s); P=$(read_dash_port "$UH/pilnk/config.json"); t1=$(date +%s)
ok rb_dash_port_reader_fifo '[ "$P" = 5000 ] && [ $((t1 - t0)) -lt 3 ]'
ok rb_dash_port_from_app_dir 'grep -q "read_dash_port \"\${PILNK_APP_DIR:-\$HOME/pilnk}/config.json\"" "$REPO/pilnkradio-install.sh"'

# --- denoise-install keeps its state outside the signed copy in root-build mode
rb_setup; cp "$REPO/denoise-install.sh" "$CUR/denoise-install.sh"
PILNK_ROOT_BUILD="$RB" PILNKRADIO_STATE_FILE="$ST/audio_build_state.json" PILNK_DENOISE_SRC="$ROOT/none" \
  PILNK_DENOISE_ARCH=x86_64 bash "$CUR/denoise-install.sh" > /dev/null 2>&1
ok rb_denoise_state_outside_copy '[ -f "$ST/denoise_state.json" ] && [ ! -e "$CUR/denoise_state.json" ]'

# --- every root-run script starts its siblings from its own folder
bad_calls() {
  local f
  while IFS= read -r f; do
    case "$f" in ''|'#'*) continue ;; esac
    grep -nE '(bash|sh|exec|source|\.)[[:space:]]+"?(\$HOME/pilnk|/home/|\$PILNK_DIR|\$\{PILNK_DIR)' "$REPO/$f" \
      | grep -v 'runuser -u' | sed "s|^|$f:|"
  done < "$REPO/rootside/MANIFEST"
}
ok rb_root_callers_use_own_dir '[ -z "$(bad_calls)" ]'
