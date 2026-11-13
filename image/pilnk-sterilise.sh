#!/bin/bash
# pilnk-sterilise.sh [--no-poweroff] [--no-zero]
#
# Last step of building the PiLNK card (spec section 3): strips every trace of
# the build (keys, Wi-Fi, logs, AJ's PiLNK identity), arms first boot, wipes free
# space, runs the same rule checks the laptop verifier uses, and powers off only
# when they find nothing.
#
#   --no-poweroff  never power off (tests, dry runs)
#   --no-zero      skip the zero-fill of free space
#
# On the real system it does not run inside your login session: it re-starts
# itself as the transient unit pilnk-sterilise.service (systemd-run) and returns
# at once. The unit does the work and runs the check once. If that finds a
# problem it stops there: your SSH session stays open (SSH is only disabled for
# new logins), nothing is powered off, and /run/pilnk-sterilise.log says what
# failed. Otherwise it closes every login session of pilnk (your SSH session
# ends here), removes the shell histories those shells save as they exit, runs
# the check again and powers off. If that last check fails nobody can log in
# any more, so the result (check ids and paths only) also goes to the screen
# (/dev/console) and to pilnk-sterilise-failed.txt on the boot partition; the
# Pi stays on. Its output goes to /run/pilnk-sterilise.log (RAM only).
#
# Env: PILNK_ROOT  test root (default "": the live system, needs root). "/",
#                  "/." and other spellings of "/" mean the live system.
#      PILNK_BOOT  boot partition (default $PILNK_ROOT/boot/firmware, or
#                  /boot/firmware on the live system)
#      PILNK_DIR   PiLNK checkout (default $PILNK_ROOT/home/pilnk/pilnk)
#      PILNK_ZERO_LIMIT_MB  cap the zero file (tests only)
#      PILNK_CONSOLE  where a failed final check is shown (default /dev/console)
# A test root never re-starts itself, never powers off (as --no-poweroff),
# zero-fills only with PILNK_ZERO_LIMIT_MB set (unlimited it would fill the
# host's disk), and never calls systemctl, loginctl, passwd, cloud-init or
# journalctl. (--stage2 is internal: the detached run.)
#
# Saved Wi-Fi is deleted, never read: NetworkManager / netplan / wpa_supplicant
# files are removed with find -delete and nothing opens them. This script prints
# counts and paths, never file contents.
#
# After this, never boot the card before it is imaged: it would run first boot.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sterile-checks.sh
. "$HERE/sterile-checks.sh"

say() { printf '%s\n' "$*"; }

LOG=/run/pilnk-sterilise.log
FAILED_REPORT=pilnk-sterilise-failed.txt
# Written to the boot partition only after the final check passes; the image
# verifier rejects a card without it (13 Nov, run 4: a stalled sterilise left no
# trace but a dark LED). Fixed text, nothing about the build or the builder.
DONE_MARK=pilnk-sterile.ok
DONE_TEXT="pilnk-sterilise finished: its final check found 0 problems"
ERR_LINES=()      # "FAIL <id>: ..." lines counted in ERRORS, for the reports

# PILNK_ROOT -> "" for the live system (any spelling of "/": "//", "/.",
# "/tmp/..", a symlink to /), else the absolute, resolved fake root.
norm_root() { # path
  local r="$1"
  if [ -n "$r" ]; then
    r="$(realpath -m -- "$r" 2>/dev/null)" || return 1
    [ -n "$r" ] || return 1
    [ "$r" = / ] && r=""
  fi
  printf '%s' "$r"
}

# Delete everything inside DIR (keep DIR); nothing is read.
wipe_dir() { [ -d "$1" ] && find "$1" -mindepth 1 -delete 2>/dev/null; return 0; }

# Delete the named paths and report how many existed.
remove_paths() { # label path...
  local label="$1" n=0 p; shift
  for p in "$@"; do
    if [ -e "$p" ] || [ -L "$p" ]; then rm -rf "$p" 2>/dev/null; n=$((n + 1)); fi
  done
  [ "$n" -gt 0 ] && say "removed: $label ($n)"
  return 0
}

# Delete files under DIR matching find args; report the count, never the names.
remove_matching() { # label dir find-args...
  local label="$1" dir="$2" n; shift 2
  [ -d "$dir" ] || return 0
  n=$(find "$dir" "$@" -printf '.' 2>/dev/null | wc -c)
  if [ "$n" -gt 0 ]; then find "$dir" "$@" -delete 2>/dev/null; say "removed: $label ($n)"; fi
  return 0
}

# rm -rf of the wpasupplicant example docs; refuses anything that is not a real directory
remove_wpa_examples() {
  local d="$R/usr/share/doc/wpasupplicant/examples"
  if [ -d "$d" ] && [ ! -L "$d" ]; then
    rm -rf --one-file-system -- "$d" 2>/dev/null
    say "removed: wpasupplicant example configs"
  elif [ -L "$d" ]; then
    rm -f -- "$d"; say "removed: wpasupplicant example configs"
  fi
  return 0
}

wipe_counted() { # label dir
  local n
  [ -d "$2" ] || return 0
  n=$(find "$2" -mindepth 1 -printf '.' 2>/dev/null | wc -c)
  if [ "$n" -gt 0 ]; then wipe_dir "$2"; say "removed: $1 ($n)"; fi
  return 0
}

unit_enable() { # unit...
  local u W="$R/etc/systemd/system/multi-user.target.wants"
  for u in "$@"; do
    if [ "$REAL" = 1 ]; then systemctl enable "$u" >/dev/null 2>&1 || say "WARNING: could not enable $u"
    else mkdir -p "$W"; ln -sfn "/etc/systemd/system/$u" "$W/$u"; fi
  done
}
# On a test root: remove the unit's enable links under every *.wants dir, and
# an alias symlink (sshd.service -> ssh.service) -- never the unit file itself.
unit_disable() { # unit...
  local u f
  for u in "$@"; do
    if [ "$REAL" = 1 ]; then systemctl disable "$u" >/dev/null 2>&1 || true; continue; fi
    for f in "$R"/etc/systemd/system/*.wants/"$u"; do
      if [ -L "$f" ]; then rm -f "$f"; fi
    done
    f="$R/etc/systemd/system/$u"
    if [ -L "$f" ] && [ "$(basename "$(readlink "$f")")" != "$u" ]; then rm -f "$f"; fi
  done
  return 0
}

homes() { local h; for h in "$R"/home/* "$R/root"; do [ -d "$h" ] && printf '%s\n' "$h"; done; }

remove_histories() {
  local h
  while IFS= read -r h; do
    remove_paths "history/cache files in $h" \
      "$h/.bash_history" "$h/.zsh_history" "$h/.sh_history" "$h/.python_history" "$h/.lesshst" \
      "$h/.local/state/lesshst" "$h/.local/share/nano" \
      "$h/.viminfo" "$h/.node_repl_history" "$h/.cache" "$h/.ssh" "$h/.gitconfig" "$h/.wget-hsts"
    # what the build itself leaves: the RUNBOOK's patch folder, a saved build log,
    # and sudo's first-use marker (~/rtl-sdr-blog stays: public driver source the
    # radio installer reuses)
    remove_paths "build leftovers in $h" \
      "$h/sterile-patches" "$h/build.log" "$h/.sudo_as_admin_successful"
  done < <(homes)
}

# wtmp/btmp/lastlog, lastlog2 and wtmpdb (Trixie): SSH logins with source IPs
remove_login_records() {
  remove_paths "login records" "$R/var/log/wtmp" "$R/var/log/btmp" "$R/var/log/lastlog"
  wipe_counted "lastlog" "$R/var/lib/lastlog"
  wipe_counted "wtmpdb" "$R/var/lib/wtmpdb"
}

# cloud-init clean failing is a failure (ERRORS), not a warning
cloud_init_step() {
  if [ "$REAL" = 1 ] && command -v cloud-init >/dev/null 2>&1; then
    if ! cloud-init clean --logs --seed >/dev/null 2>&1; then
      ERR_LINES+=("FAIL cloud-init-clean: cloud-init clean --logs --seed failed")
      say "${ERR_LINES[-1]}"
      ERRORS=$((ERRORS + 1))
    fi
  fi
  remove_paths "cloud-init seed files" "$B/user-data" "$B/network-config" "$B/meta-data"
  # instance data (user-data holds the password hash) and the netplan file it wrote
  remove_paths "cloud-init state" "$R/var/lib/cloud" "$R/etc/netplan/50-cloud-init.yaml"
  mkdir -p "$R/etc/cloud" && : > "$R/etc/cloud/cloud-init.disabled"
  say "disabled: cloud-init"
}

# Saved networks, leases and NetworkManager's state files (seen-bssids,
# timestamps, NetworkManager.state, secret_key ...): deleted, never read.
remove_network() {
  local d
  wipe_counted "saved network connections" "$R/etc/NetworkManager/system-connections"
  wipe_counted "runtime network connections" "$R/run/NetworkManager/system-connections"
  remove_matching "NetworkManager netplan files" "$R/etc/netplan" -maxdepth 1 -name '90-NM-*.yaml'
  remove_matching "wpa_supplicant files" "$R/etc/wpa_supplicant" -maxdepth 1 -name '*.conf'
  remove_paths "boot wpa_supplicant.conf" "$B/wpa_supplicant.conf"
  for d in "$R/var/lib/NetworkManager" "$R/var/lib/dhcp" "$R/var/lib/dhcpcd"; do
    remove_matching "DHCP leases" "$d" -maxdepth 2 -name '*.lease*'
  done
  remove_matching "NetworkManager state" "$R/var/lib/NetworkManager" -mindepth 1 -maxdepth 1 ! -type d
}

# Detached run, after the sessions are closed: stop NetworkManager and
# wpa_supplicant (a lease renewal or their state flush would otherwise write
# to the card through the zero-fill and the final check), then remove
# everything network again, since they write their state files on the way down.
stop_network() {
  if [ "$REAL" = 1 ]; then
    systemctl stop NetworkManager.service wpa_supplicant.service >/dev/null 2>&1 \
      || say "WARNING: could not stop NetworkManager / wpa_supplicant"
  fi
  say "stopped: NetworkManager, wpa_supplicant (no more network writes)"
  remove_network
}

# Close every login session of pilnk. Their shells save history on the way
# out, which is why the histories are removed again afterwards.
end_sessions() {
  local i=0 s
  loginctl terminate-user pilnk >/dev/null 2>&1 || true
  while [ "$i" -lt 30 ]; do
    s=$(loginctl show-user pilnk -p Sessions --value 2>/dev/null) || break
    [ -z "$s" ] && break
    sleep 1; i=$((i + 1))
  done
  say "closed: login sessions of pilnk"
}

# Delete files under /var/log but keep its directories (journald, apt and others
# expect them); the journal's per-machine-id folders go too.
wipe_logs() {
  local n
  [ -d "$R/var/log" ] || return 0
  n=$(find "$R/var/log" -mindepth 1 ! -type d -printf '.' 2>/dev/null | wc -c)
  find "$R/var/log" -mindepth 1 ! -type d -delete 2>/dev/null
  wipe_dir "$R/var/log/journal"
  [ "$n" -gt 0 ] && say "removed: /var/log files ($n, directories kept)"
  return 0
}

# Swap (found on the first hardware run, 5 Nov: a private key in /var/swap).
# The swap file is an allocated file, so the zero-fill never reaches it, and it
# holds whatever RAM was paged out during the build. Turn swap off (zram too:
# rpi-swap's zram writes back into /var/swap through a loop device), write zeros
# over the file where it lies, then delete it. At the next boot
# rpi-resize-swap-file.service (rpi-swap) creates a new, empty one.
remove_swap() {
  local f sz mb dev
  if [ "$REAL" = 1 ]; then
    swapoff -a >/dev/null 2>&1 || say "WARNING: swapoff -a failed"
    systemctl stop rpi-zram-writeback.service 'systemd-zram-setup@*.service' >/dev/null 2>&1 || true
  fi
  for f in "$R/var/swap" "$R/swapfile"; do
    [ -f "$f" ] || continue
    if [ "$REAL" = 1 ]; then
      while IFS=: read -r dev _; do
        [ -n "$dev" ] && losetup -d "$dev" >/dev/null 2>&1
      done < <(losetup -j "$f" 2>/dev/null)
    fi
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    mb=$(( (sz + 1048575) / 1048576 ))
    if ! dd if=/dev/zero of="$f" bs=1M count="$mb" conv=notrunc,fsync status=none 2>/dev/null; then
      say "WARNING: could not overwrite ${f#"$R"} (still in use?)"
      ERR_LINES+=("FAIL swap-zero: ${f#"$R"}"); ERRORS=$((ERRORS + 1))
    fi
    rm -f "$f"
    say "removed: swap file ${f#"$R"} (zeroed first; rpi-swap makes a new one at boot)"
  done
  return 0
}

ZERO_FILE=""
# A problem found on the way: counted in ERRORS, so the final check fails.
note_fail() { # id text
  ERR_LINES+=("FAIL $1: $2"); say "${ERR_LINES[-1]}"; ERRORS=$((ERRORS + 1))
}
# Fill the free space with zeros, then delete the fill file. The only normal
# end is a full disk ("No space left on device", or the test cap); any other
# end means the card stopped part way, and a fill file that survives rm would
# ship. Both fail the final check. dd's message is read, never printed.
zero_fill() { # dir
  local cnt=() ef rc
  [ -d "$1" ] || return 0
  ZERO_FILE="$1/.pilnk-zero"
  [ -n "${PILNK_ZERO_LIMIT_MB:-}" ] && cnt=(count="$PILNK_ZERO_LIMIT_MB")
  ef=$(mktemp -p /run pilnk-zero.XXXXXX 2>/dev/null || mktemp)
  LC_ALL=C dd if=/dev/zero of="$ZERO_FILE" bs=1M "${cnt[@]}" status=none 2>"$ef"; rc=$?
  sync
  rm -f "$ZERO_FILE"
  if [ -e "$ZERO_FILE" ]; then note_fail zero-fill-rm "${ZERO_FILE#"$R"} (the fill file is still there)"; fi
  ZERO_FILE=""
  if [ "$rc" -ne 0 ] && ! grep -q 'No space left on device' "$ef" 2>/dev/null; then
    note_fail zero-fill-short "${1#"$R"} (dd stopped before the disk was full)"
  else
    say "zero-filled: free space on $1"
  fi
  rm -f "$ef"
}
remove_zero_file() { if [ -n "$ZERO_FILE" ]; then rm -f "$ZERO_FILE"; fi; }

# The rule checks plus the errors counted on the way: sets CHECK_OUT (the
# check's FAIL lines) and CHECK_N, and prints the lines.
collect_check() {
  local n
  CHECK_OUT=$(sterile_check "$R" "$B"); n=$?
  CHECK_N=$((n + ${ERRORS:-0}))
  if [ -n "$CHECK_OUT" ]; then say "$CHECK_OUT"; fi
}

# Detached run, before the login sessions are closed: on a problem stop here,
# so AJ's SSH session (SSH is disabled for new logins only) can read the log.
pre_check() {
  say "== pre-check (before closing the login sessions)"
  collect_check
  if [ "$CHECK_N" -ne 0 ]; then
    say "NOT STERILE: $CHECK_N problem(s) above. Login sessions left open, nothing powered off. Fix them and run pilnk-sterilise again. Log: $LOG"
    exit 1
  fi
}

# After the sessions are closed nobody can log in, so a failed final check is
# also put where it can be read without one: the console, and a file on the
# boot partition (read it on the laptop; the check id sterilise-failed stops
# the card shipping with it). Check ids and paths only, as printed above.
report_failure() {
  local con="${PILNK_CONSOLE:-/dev/console}" f="$B/$FAILED_REPORT" text
  text="pilnk-sterilise: NOT STERILE. The Pi stays on. Do not image this card or boot it again."
  if [ "${#ERR_LINES[@]}" -gt 0 ]; then text+=$'\n'"$(printf '%s\n' "${ERR_LINES[@]}")"; fi
  if [ -n "$CHECK_OUT" ]; then text+=$'\n'"$CHECK_OUT"; fi
  text+=$'\n'"NOT STERILE: $CHECK_N problem(s). Full log: $LOG (RAM only)"
  { printf '%s\n' "$text" > "$con"; } 2>/dev/null || say "WARNING: could not write to $con"
  if { printf '%s\n' "$text" > "$f"; } 2>/dev/null; then sync; say "wrote: $f"
  else say "WARNING: could not write $f"; fi
}

# The positive proof the run finished: the verifier rejects a card without it.
write_done_mark() {
  local f="$B/$DONE_MARK"
  if { printf '%s\n' "$DONE_TEXT" > "$f"; } 2>/dev/null; then sync; say "wrote: finished mark $f"
  else say "WARNING: could not write the finished mark $f (the image verifier will reject this card)"; fi
}

# Last step: the check, then power off only when it finds nothing.
final_check() {
  say "== self-check"
  collect_check
  if [ "$CHECK_N" -ne 0 ]; then
    say "NOT STERILE: $CHECK_N problem(s) above. Not powering off."
    if [ "$STAGE2" = 1 ]; then report_failure; fi
    exit 1
  fi
  say "STERILE: 0 problems."
  write_done_mark
  if [ "$POWEROFF" = 1 ]; then
    say "powering off. Do not boot this card again before it is imaged."
    poweroff
  else
    say "not powering off (--no-poweroff, or a fake root): leaving the system up."
  fi
  exit 0
}

# Real root, stage 1: start the detached unit and return.
relaunch_detached() { # args...
  local self env=() v
  self="$HERE/$(basename "${BASH_SOURCE[0]}")"
  for v in PILNK_BOOT PILNK_DIR PILNK_ZERO_LIMIT_MB; do
    if [ -n "${!v:-}" ]; then env+=("--setenv=$v=${!v}"); fi
  done
  if ! systemd-run --unit=pilnk-sterilise --collect --quiet \
         --property=StandardOutput=truncate:"$LOG" --property=StandardError=inherit \
         "${env[@]}" /bin/bash "$self" "$@" --stage2 >/dev/null 2>&1; then
    echo "pilnk-sterilise: could not start pilnk-sterilise.service (systemd-run failed); nothing was changed" >&2
    return 1
  fi
  if [ "$POWEROFF" = 1 ]; then
    say "pilnk-sterilise: now running as pilnk-sterilise.service; your session will close in a minute or two and the Pi will power off when it is done (if your session is still open after a few minutes the check found a problem: read $LOG)"
  else
    say "pilnk-sterilise: now running as pilnk-sterilise.service; your session will close in a minute or two and the Pi stays up (--no-poweroff; if your session is still open after a few minutes the check found a problem: read $LOG)"
  fi
  return 0
}

# Sourced (by the tests): define the steps, run nothing.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then return 0; fi

set -u

POWEROFF=1; ZERO=1; STAGE2=0; ARGS=()
for a in "$@"; do
  case "$a" in
    --no-poweroff) POWEROFF=0; ARGS+=("$a") ;;
    --no-zero) ZERO=0; ARGS+=("$a") ;;
    --stage2) STAGE2=1 ;;
    -h|--help) sed -n '2,/^# After this/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "pilnk-sterilise: unknown option: $a" >&2; exit 2 ;;
  esac
done

if ! R="$(norm_root "${PILNK_ROOT:-}")"; then
  echo "pilnk-sterilise: cannot resolve PILNK_ROOT" >&2
  exit 2
fi
if [ -n "$R" ]; then B="${PILNK_BOOT:-$R/boot/firmware}"; else B="${PILNK_BOOT:-/boot/firmware}"; fi
B="${B%/}"
D="${PILNK_DIR:-$R/home/pilnk/pilnk}"
REAL=0; [ -z "$R" ] && REAL=1
ERRORS=0
FAKE_NOPOWER=0
if [ "$REAL" = 0 ] && [ "$POWEROFF" = 1 ]; then POWEROFF=0; FAKE_NOPOWER=1; fi

if [ "$REAL" = 1 ] && [ "$(id -u)" != 0 ]; then
  echo "pilnk-sterilise: run as root (sudo)" >&2
  exit 2
fi

if [ "$REAL" = 1 ] && [ "$STAGE2" = 0 ]; then
  relaunch_detached "${ARGS[@]}"
  exit $?
fi

trap remove_zero_file EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[ "$STAGE2" = 1 ] && trap '' PIPE

say "== sterilising ${R:-/} (boot $B)"
[ "$FAKE_NOPOWER" = 1 ] && say "fake root: never powers off (as --no-poweroff)"
remove_paths "leftover zero-fill files" "$R/.pilnk-zero" "$B/.pilnk-zero"
remove_paths "previous failure report" "$B/$FAILED_REPORT"
remove_paths "previous finished mark" "$B/$DONE_MARK"

# -- Logins --------------------------------------------------------------
remove_matching "SSH host keys" "$R/etc/ssh" -maxdepth 1 -name 'ssh_host_*'
while IFS= read -r h; do
  remove_matching "authorized_keys in $h" "$h" -maxdepth 3 -name authorized_keys
done < <(homes)
unit_disable ssh.service sshd.service ssh.socket
remove_paths "boot ssh flag" "$B/ssh" "$B/ssh.txt"
say "disabled: SSH"
if [ "$REAL" = 1 ]; then
  passwd -l pilnk >/dev/null 2>&1 || say "WARNING: passwd -l pilnk failed"
elif [ -f "$R/etc/shadow" ]; then
  sed -i -E 's/^(pilnk:)([^!:])/\1!\2/; s/^(pilnk:):/\1!:/' "$R/etc/shadow"
fi
say "locked: pilnk password"

# -- Cloud-init ----------------------------------------------------------
cloud_init_step

# -- Network (deleted, never read) --------------------------------------
remove_network
# Debian ships example configs with example passphrases (harmless docs, but the
# image verifier's raw psk= rule flags them)
remove_wpa_examples
remove_paths "Tailscale state" "$R/var/lib/tailscale"

# -- Identity ------------------------------------------------------------
mkdir -p "$R/etc"
: > "$R/etc/machine-id"
remove_paths "dbus machine-id" "$R/var/lib/dbus/machine-id"
echo pilnk > "$R/etc/hostname"
[ -f "$R/etc/hosts" ] && sed -i -E 's/^(127\.0\.1\.1[[:space:]]+).*/\1pilnk/' "$R/etc/hosts"
say "reset: machine-id (empty), hostname (pilnk)"

# -- Traces --------------------------------------------------------------
remove_histories
if [ "$REAL" = 1 ]; then
  apt-get clean >/dev/null 2>&1 || true
else
  wipe_dir "$R/var/cache/apt/archives"
fi
say "cleaned: apt cache, pip cache"
wipe_counted "/tmp" "$R/tmp"
wipe_counted "/var/tmp" "$R/var/tmp"

# -- PiLNK ---------------------------------------------------------------
# (the ceiling stops git from using a parent repo if $D/.git is broken)
if [ -e "$D/.git" ] && command -v git >/dev/null 2>&1; then
  GIT_CEILING_DIRECTORIES="$(dirname "$D")" git -c "safe.directory=$D" -C "$D" clean -ffdXq >/dev/null 2>&1 \
    && say "removed: git-ignored runtime files (git clean -ffdX)"
  # reflogs name who did what to the clone, and when
  GIT_CEILING_DIRECTORIES="$(dirname "$D")" git -c "safe.directory=$D" -C "$D" reflog expire --expire=now --all >/dev/null 2>&1 || true
fi
remove_paths "git reflogs (.git/logs)" "$D/.git/logs"
# a git identity (user.name / user.email) set while building the clone; "no such
# section" is the normal case and is ignored. Values are never printed.
if [ -f "$D/.git/config" ] && command -v git >/dev/null 2>&1 \
   && grep -qiE '^[[:space:]]*\[user([[:space:]]|\])' "$D/.git/config" 2>/dev/null; then
  git config --file "$D/.git/config" --remove-section user >/dev/null 2>&1 || true
  say "removed: git identity from the clone's .git/config"
fi
remove_paths "PiLNK secrets and runtime state" \
  "$D/.secret_key" "$D/update.log" "$D/ota_state.json" "$D/.no_receiver" "$D/config.json.installer"
remove_matching "history day store" "$D" -maxdepth 1 -name 'history_day.json*'
wipe_counted "flight capsules" "$D/capsules"
wipe_counted "history folder" "$D/history"
wipe_counted "transcripts" "$R/home/pilnk/atc-stt"
mkdir -p "$D"
cp "$STERILE_TEMPLATE" "$D/config.json" && chmod 644 "$D/config.json"
if [ "$(id -u)" = 0 ] && [ -d "$D" ]; then
  chown "$(stat -c '%u:%g' "$D")" "$D/config.json" 2>/dev/null || true
fi
say "reset: config.json (clean template)"
# readsb --device, pilnkradio's serial, serial-pinned udev rules
[ -f "$R/etc/default/readsb" ] && sed -i -E 's/--device[ =][^ "]*[ ]?//g' "$R/etc/default/readsb"
remove_paths "pilnkradio serial config" "$R/etc/pilnkradio/config.json"
remove_paths "radio wake udev rule" "$R/etc/udev/rules.d/99-pilnk-v4.rules"
if [ -d "$R/etc/udev/rules.d" ]; then
  while IFS= read -r -d '' f; do
    grep -qE 'ATTRS?\{serial\}' "$f" 2>/dev/null && remove_paths "serial-pinned udev rule" "$f"
  done < <(find "$R/etc/udev/rules.d" -maxdepth 1 -name '*.rules' -print0 2>/dev/null)
fi
say "cleared: readsb --device, pilnkradio serial, pinned udev rules"
remove_paths "first-boot markers and log" "$R/var/lib/pilnk-firstboot" "$R/var/log/pilnk-firstboot.log"

# -- Arm first boot ------------------------------------------------------
# readsb ships disabled: an unpinned readsb would grab a dongle at first boot.
unit_enable pilnk-firstboot.service pilnk-wifi-setup.service
unit_disable pilnk.service readsb.service
say "armed: first boot (pilnk-firstboot + pilnk-wifi-setup enabled; pilnk + readsb disabled)"

# -- Logs last, so nothing above leaves new ones behind -------------------
if [ "$REAL" = 1 ]; then
  journalctl --rotate >/dev/null 2>&1 || true
  journalctl --vacuum-time=1s >/dev/null 2>&1 || true
  systemctl stop systemd-journald.socket systemd-journald-dev-log.socket \
    systemd-journald-audit.socket systemd-journald.service >/dev/null 2>&1 || true
  wipe_dir /run/log/journal
fi
wipe_logs
remove_login_records

# -- Swap: before the pre-check, which fails on a swap file ----------------
remove_swap

# -- Detached run: check, close the login sessions, remove what they wrote -
if [ "$STAGE2" = 1 ]; then
  # NetworkManager rewrites its state files now and then: clear them right
  # before the pre-check so it does not trip on a write from the last seconds
  remove_matching "NetworkManager state" "$R/var/lib/NetworkManager" -mindepth 1 -maxdepth 1 ! -type d ! -name '*.lease*'
  pre_check
  end_sessions
  if [ "$REAL" = 1 ]; then
    # their stop actions write the shutdown record now, not after the check
    for u in wtmpdb-update-boot.service systemd-update-utmp.service; do
      systemctl stop "$u" >/dev/null 2>&1 || true
    done
  fi
  stop_network
  remove_histories
  remove_login_records
fi

# -- Wipe free space -----------------------------------------------------
if [ "$ZERO" = 0 ]; then
  say "skipped: zero-fill (--no-zero)"
elif [ "$REAL" = 0 ] && [ -z "${PILNK_ZERO_LIMIT_MB:-}" ]; then
  say "skipped: zero-fill (a fake root needs PILNK_ZERO_LIMIT_MB; unlimited it would fill the host's disk)"
else
  [ -d "$B" ] && zero_fill "$B"
  zero_fill "$R/"
fi
sync

# -- Self-check, then power off -------------------------------------------
final_check
