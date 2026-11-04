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

# A real partition node (/dev/mmcblk0p2, /dev/sda2, /dev/nvme0n1p2), not /dev/root.
is_part() { [[ "$1" =~ ^/dev/([a-z]+[0-9]+|mmcblk[0-9]+p[0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)$ ]] && [[ "$1" != /dev/root ]]; }

# findmnt can say /dev/root; recover the device from root= on the kernel cmdline.
resolve_root_from_cmdline() {
  local v dev
  v=$(tr ' ' '\n' < "$R/proc/cmdline" 2>/dev/null | sed -n 's/^root=//p' | head -1)
  case "$v" in
    PARTUUID=*|UUID=*|LABEL=*) dev=$(blkid -t "$v" -o device 2>/dev/null | head -1) ;;
    /dev/*) dev=$v ;;
  esac
  [ -n "$dev" ] && echo "$dev"
}

# Root partition -> disk + number. Echoes "SRC DISK PARTNUM", or nothing.
root_device() {
  local src part disk
  src=$(findmnt -n -o SOURCE / 2>/dev/null)
  is_part "$src" || src=$(resolve_root_from_cmdline)
  is_part "$src" || return 1
  part=$(printf '%s' "$src" | sed -E 's/.*[^0-9]([0-9]+)$/\1/')
  disk=${src%"$part"}
  case "$disk" in *[0-9]p) disk=${disk%p} ;; esac
  echo "$src $disk $part"
}

grow_root() {
  local dev src disk part rc
  dev=$(root_device) || { log "grow: cannot resolve the root device (not a /dev/<disk><part> path); skipping"; touch "$STATE/grown"; return 0; }
  read -r src disk part <<< "$dev"
  if [ ! -e "$STATE/grown" ]; then
    growpart "$disk" "$part"; rc=$?
    case "$rc" in
      0) log "grow: partition grown; rebooting to re-read the table"
         touch "$STATE/grown"       # before reboot: no boot loop, no second grow
         if reboot; then exit 0; fi
         log "grow: reboot failed; growing the filesystem online" ;;
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
  if ! hostnamectl set-hostname "$host"; then
    log "identity: hostnamectl failed; writing /etc/hostname directly"
    printf '%s\n' "$host" > "$R/etc/hostname" || { log "identity: cannot write /etc/hostname"; return 1; }
    if command -v hostname >/dev/null 2>&1; then hostname "$host" || log "identity: hostname command failed"; fi
  fi
  if grep -q '^127\.0\.1\.1' "$R/etc/hosts" 2>/dev/null; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$host/" "$R/etc/hosts" || { log "identity: editing /etc/hosts failed"; return 1; }
  else
    printf '127.0.1.1\t%s\n' "$host" >> "$R/etc/hosts" || { log "identity: appending to /etc/hosts failed"; return 1; }
  fi
  # avahi read the old name when it started; make it advertise the new one
  systemctl --no-block try-restart avahi-daemon || log "identity: avahi restart failed"
  log "identity: hostname $host"
}

online() { nm-online -q -t "$1" && [ -n "$(ip -4 route show default 2>/dev/null)" ]; }

# First boot never starts the hotspot: pilnk-wifi-setup.service does, at every
# boot, once there is still no connection after 90 s. We just wait for online.
stage_network() {
  # online = NetworkManager says connected AND there is an IPv4 default route
  # (the setup hotspot is "connected" to nm-online but has no route out)
  if ! online "$NET_WAIT"; then
    log "network: not online after ${NET_WAIT}s; waiting for pilnk-wifi-setup"
    until online 5; do sleep "$POLL"; done
  fi
  log "network: online"
}

stage_update() {
  local branch i
  # as the repo owner: root hits git's "dubious ownership" refusal
  branch=$(runuser -u pilnk -- git -C "$PILNK_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -z "$branch" ] && [ -e "$PILNK_DIR/.git" ]; then
    log "test build: update skipped (branch unknown)"
    return 0
  fi
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
       systemctl enable readsb || { log "receiver: enable readsb failed; will retry at next boot"; return 1; }
       # NO_BLOCK: self-heal must not start units that are After=pilnk.service
       PILNK_DIR="$PILNK_DIR" PILNK_SELFHEAL_NO_BLOCK=1 PILNK_SELFHEAL_LOG="$LOG" bash "$PILNK_DIR/bootstrap-selfheal.sh" || log "receiver: self-heal failed"
       log "receiver: bound" ;;
    2) touch "$PILNK_DIR/.no_receiver"
       log "receiver: none found; will retry at next boot"; return 1 ;;
    *) log "receiver: hw-setup failed (rc=$rc); will retry at next boot"; return 1 ;;
  esac
}

stage_started() {
  # never a blocking start: this unit is Before=pilnk.service, so a synchronous
  # start would wait on ourselves. pilnk starts as soon as we exit.
  systemctl enable pilnk || { log "start: enabling pilnk failed"; return 1; }
  systemctl --no-block start pilnk || { log "start: queueing pilnk start failed"; return 1; }
  log "start: pilnk enabled; start queued"
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
