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
# Env: PILNK_ROOT  test root (default "": the live system, needs root)
#      PILNK_BOOT  boot partition (default /boot/firmware)
#      PILNK_DIR   PiLNK checkout (default $PILNK_ROOT/home/pilnk/pilnk)
#      PILNK_ZERO_LIMIT_MB  cap the zero file (tests only)
#
# Saved Wi-Fi is deleted, never read: NetworkManager / netplan / wpa_supplicant
# files are removed with find -delete and nothing opens them. This script prints
# counts and paths, never file contents.
#
# After this, never boot the card before it is imaged: it would run first boot.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=sterile-checks.sh
. "$HERE/sterile-checks.sh"

POWEROFF=1; ZERO=1
for a in "$@"; do
  case "$a" in
    --no-poweroff) POWEROFF=0 ;;
    --no-zero) ZERO=0 ;;
    -h|--help) sed -n '2,/^# After this/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "pilnk-sterilise: unknown option: $a" >&2; exit 2 ;;
  esac
done

R="${PILNK_ROOT:-}"; R="${R%/}"
B="${PILNK_BOOT:-/boot/firmware}"; B="${B%/}"
D="${PILNK_DIR:-$R/home/pilnk/pilnk}"
REAL=0; [ -z "$R" ] && REAL=1
W="$R/etc/systemd/system/multi-user.target.wants"

if [ "$REAL" = 1 ] && [ "$(id -u)" != 0 ]; then
  echo "pilnk-sterilise: run as root (sudo)" >&2
  exit 2
fi

say() { printf '%s\n' "$*"; }

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

wipe_counted() { # label dir
  local n
  [ -d "$2" ] || return 0
  n=$(find "$2" -mindepth 1 -printf '.' 2>/dev/null | wc -c)
  if [ "$n" -gt 0 ]; then wipe_dir "$2"; say "removed: $1 ($n)"; fi
  return 0
}

unit_enable() { # unit...
  local u
  for u in "$@"; do
    if [ "$REAL" = 1 ]; then systemctl enable "$u" >/dev/null 2>&1 || say "WARNING: could not enable $u"
    else mkdir -p "$W"; ln -sfn "/etc/systemd/system/$u" "$W/$u"; fi
  done
}
unit_disable() { # unit...
  local u
  for u in "$@"; do
    if [ "$REAL" = 1 ]; then systemctl disable "$u" >/dev/null 2>&1 || true
    else rm -f "$W/$u" "$R/etc/systemd/system/sockets.target.wants/$u" "$R/etc/systemd/system/$u"; fi
  done
}

homes() { local h; for h in "$R"/home/* "$R/root"; do [ -d "$h" ] && printf '%s\n' "$h"; done; }

say "== sterilising ${R:-/} (boot $B)"

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
if [ "$REAL" = 1 ] && command -v cloud-init >/dev/null 2>&1; then
  cloud-init clean --logs --seed >/dev/null 2>&1 || say "WARNING: cloud-init clean failed"
fi
remove_paths "cloud-init seed files" "$B/user-data" "$B/network-config" "$B/meta-data"
mkdir -p "$R/etc/cloud" && : > "$R/etc/cloud/cloud-init.disabled"
say "disabled: cloud-init"

# -- Network (deleted, never read) --------------------------------------
wipe_counted "saved network connections" "$R/etc/NetworkManager/system-connections"
wipe_counted "runtime network connections" "$R/run/NetworkManager/system-connections"
remove_matching "NetworkManager netplan files" "$R/etc/netplan" -maxdepth 1 -name '90-NM-*.yaml'
remove_matching "wpa_supplicant files" "$R/etc/wpa_supplicant" -maxdepth 1 -name '*.conf'
remove_paths "boot wpa_supplicant.conf" "$B/wpa_supplicant.conf"
for d in "$R/var/lib/NetworkManager" "$R/var/lib/dhcp" "$R/var/lib/dhcpcd"; do
  remove_matching "DHCP leases" "$d" -maxdepth 2 -name '*.lease*'
done
remove_matching "NetworkManager state" "$R/var/lib/NetworkManager" -maxdepth 1 -type f

# -- Identity ------------------------------------------------------------
mkdir -p "$R/etc"
: > "$R/etc/machine-id"
remove_paths "dbus machine-id" "$R/var/lib/dbus/machine-id"
echo pilnk > "$R/etc/hostname"
[ -f "$R/etc/hosts" ] && sed -i -E 's/^(127\.0\.1\.1[[:space:]]+).*/\1pilnk/' "$R/etc/hosts"
say "reset: machine-id (empty), hostname (pilnk)"

# -- Traces --------------------------------------------------------------
while IFS= read -r h; do
  remove_paths "history/cache files in $h" \
    "$h/.bash_history" "$h/.zsh_history" "$h/.sh_history" "$h/.python_history" "$h/.lesshst" \
    "$h/.viminfo" "$h/.node_repl_history" "$h/.cache" "$h/.ssh" "$h/.gitconfig" "$h/.wget-hsts"
done < <(homes)
if [ "$REAL" = 1 ]; then
  apt-get clean >/dev/null 2>&1 || true
else
  wipe_dir "$R/var/cache/apt/archives"
fi
say "cleaned: apt cache, pip cache"
wipe_counted "/tmp" "$R/tmp"
wipe_counted "/var/tmp" "$R/var/tmp"

# -- PiLNK ---------------------------------------------------------------
if [ -e "$D/.git" ] && command -v git >/dev/null 2>&1; then
  git -c "safe.directory=$D" -C "$D" clean -ffdXq >/dev/null 2>&1 \
    && say "removed: git-ignored runtime files (git clean -ffdX)"
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
wipe_counted "/var/log" "$R/var/log"
wipe_counted "lastlog" "$R/var/lib/lastlog"

# -- Wipe free space -----------------------------------------------------
zero_fill() { # dir
  local f="$1/.pilnk-zero" cnt=()
  [ -d "$1" ] || return 0
  [ -n "${PILNK_ZERO_LIMIT_MB:-}" ] && cnt=(count="$PILNK_ZERO_LIMIT_MB")
  dd if=/dev/zero of="$f" bs=1M "${cnt[@]}" status=none 2>/dev/null || true
  sync
  rm -f "$f"
  say "zero-filled: free space on $1"
}
if [ "$ZERO" = 1 ]; then
  [ -d "$B" ] && zero_fill "$B"
  zero_fill "$R/"
else
  say "skipped: zero-fill (--no-zero)"
fi
sync

# -- Self-check ----------------------------------------------------------
say "== self-check"
sterile_check "$R" "$B"
N=$?
if [ "$N" -ne 0 ]; then
  say "NOT STERILE: $N problem(s) above. Not powering off."
  exit 1
fi
say "STERILE: 0 problems."
if [ "$POWEROFF" = 1 ]; then
  say "powering off. Do not boot this card again before it is imaged."
  poweroff
else
  say "--no-poweroff: leaving the system up."
fi
exit 0
