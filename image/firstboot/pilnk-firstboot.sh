#!/bin/bash
# PiLNK first boot: resumable, one function per stage, a marker per stage.
#
#   identity  grow the root fs (reboot if the partition changed), new SSH host
#             keys, hostname pilnk-XXXX, /etc/hosts
#   network   wait for a connection (pilnk-wifi-setup.service owns the hotspot)
#   update    bring the code current once (no restart, no self-heal)
#   receiver  pilnk-hw-setup.sh --apply, then readsb + self-heal
#   started   enable and start pilnk.service
#
# Env: PILNK_ROOT (test root, default ""), PILNK_DIR (default /home/pilnk/pilnk),
#      PILNK_NET_WAIT (seconds, default 90), PILNK_POLL_SLEEP (default 5).
# Installed at /usr/local/sbin/pilnk-firstboot so the update stage's git reset
# cannot rewrite the running script.

R="${PILNK_ROOT:-}"
PILNK_DIR="${PILNK_DIR:-/home/pilnk/pilnk}"
NET_WAIT="${PILNK_NET_WAIT:-90}"
POLL="${PILNK_POLL_SLEEP:-5}"
STATE="$R/var/lib/pilnk-firstboot"
LOG="$R/var/log/pilnk-firstboot.log"

mkdir -p "$STATE" "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Last 4 hex of the Serial line in cpuinfo, lowercased; else of /etc/machine-id.
host_suffix() {
  local s
  s=$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' "$R/proc/cpuinfo" 2>/dev/null | head -1 | tr -cd '0-9a-fA-F')
  if [ -z "$s" ]; then
    s=$(tr -cd '0-9a-fA-F' < "$R/etc/machine-id" 2>/dev/null)
    [ -n "$s" ] && log "no Serial in cpuinfo; hostname suffix from /etc/machine-id" >&2
  fi
  [ -n "$s" ] || return 1
  printf '%s\n' "${s: -4}" | tr 'A-F' 'a-f'
}

# Root partition -> disk + number. Echoes "SRC DISK PARTNUM", or nothing.
root_device() {
  local src part disk
  src=$(findmnt -n -o SOURCE / 2>/dev/null)
  case "$src" in /dev/*) ;; *) return 1 ;; esac
  part=$(printf '%s' "$src" | sed -E 's/.*[^0-9]([0-9]+)$/\1/')
  disk=${src%"$part"}
  case "$disk" in *[0-9]p) disk=${disk%p} ;; esac
  echo "$src $disk $part"
}

grow_root() {
  local dev src disk part rc
  dev=$(root_device) || { log "grow: no root device found; skipping"; touch "$STATE/grown"; return 0; }
  read -r src disk part <<< "$dev"
  if [ ! -e "$STATE/grown" ]; then
    growpart "$disk" "$part"; rc=$?
    case "$rc" in
      0) log "grow: partition grown; rebooting to re-read the table"
         touch "$STATE/grown"       # before reboot: no boot loop, no second grow
         reboot
         exit 0 ;;
      1) log "grow: partition already fills the card" ;;
      *) log "grow: growpart failed (rc=$rc); continuing without it" ;;
    esac
    touch "$STATE/grown"
  fi
  resize2fs "$src" || log "grow: resize2fs failed; continuing"
}

stage_identity() {
  local suf host
  grow_root
  rm -f "$R"/etc/ssh/ssh_host_*
  ssh-keygen -A || { log "identity: ssh-keygen failed"; return 1; }
  suf=$(host_suffix) || { log "identity: no serial and no machine-id"; return 1; }
  host="pilnk-$suf"
  hostnamectl set-hostname "$host" || { log "identity: hostnamectl failed"; return 1; }
  if grep -q '^127\.0\.1\.1' "$R/etc/hosts" 2>/dev/null; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$host/" "$R/etc/hosts"
  else
    printf '127.0.1.1\t%s\n' "$host" >> "$R/etc/hosts"
  fi
  log "identity: hostname $host"
}

# First boot never starts the hotspot: pilnk-wifi-setup.service does, at every
# boot, once there is still no connection after 90 s. We just wait for online.
stage_network() {
  if ! nm-online -q -t "$NET_WAIT"; then
    log "network: not online after ${NET_WAIT}s; waiting for pilnk-wifi-setup"
    until nm-online -q -t 5; do sleep "$POLL"; done
  fi
  log "network: online"
}

stage_update() {
  local branch i
  branch=$(git -C "$PILNK_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -n "$branch" ] && [ "$branch" != main ]; then
    log "test build: update skipped (branch $branch)"
    return 0
  fi
  for i in 1 2 3; do
    if runuser -u pilnk -- env HOME=/home/pilnk PILNK_UPDATE_NO_RESTART=1 bash "$PILNK_DIR/update.sh"; then
      log "update: ok"; return 0
    fi
    log "update: attempt $i failed"; sleep "$POLL"
  done
  log "update: giving up after 3 attempts; starting on the shipped code"
  return 0
}

stage_receiver() {
  local rc
  PILNK_ROOT="$R" PILNK_HW_NONINTERACTIVE=1 bash "$PILNK_DIR/pilnk-hw-setup.sh" --apply; rc=$?
  case "$rc" in
    0) rm -f "$PILNK_DIR/.no_receiver"
       # the image ships readsb disabled: an unpinned readsb would grab a dongle
       systemctl enable readsb || log "receiver: enable readsb failed"
       PILNK_DIR="$PILNK_DIR" bash "$PILNK_DIR/bootstrap-selfheal.sh" || log "receiver: self-heal failed"
       log "receiver: bound" ;;
    2) touch "$PILNK_DIR/.no_receiver"
       log "receiver: none found; will retry at next boot"; return 1 ;;
    *) log "receiver: hw-setup failed (rc=$rc); will retry at next boot"; return 1 ;;
  esac
}

stage_started() {
  systemctl enable --now pilnk || { log "start: enabling pilnk failed"; return 1; }
  log "start: pilnk enabled and started"
}

[ -e "$STATE/identity" ] || { stage_identity && touch "$STATE/identity"; }
[ -e "$STATE/network" ]  || { stage_network  && touch "$STATE/network"; }
[ -e "$STATE/update" ]   || { stage_update   && touch "$STATE/update"; }
[ -e "$STATE/receiver" ] || { stage_receiver && touch "$STATE/receiver"; }
[ -e "$STATE/started" ]  || { stage_started  && touch "$STATE/started"; }

# Done once every marker exists; a missing receiver keeps us enabled to retry.
done_all=1
for m in identity network update receiver started; do [ -e "$STATE/$m" ] || done_all=0; done
if [ "$done_all" = 1 ]; then
  systemctl disable pilnk-firstboot || true
  log "first boot complete"
fi
exit 0
