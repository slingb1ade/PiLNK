#!/bin/bash
# Rule checks for a sterile PiLNK image. SOURCED by pilnk-sterilise.sh (live
# filesystem, last step) and pilnk-image-verify.sh (mounted image, read-only), so
# both apply exactly the same list (spec section 4 plus the arming rules).
#
#   sterile_check ROOT BOOT
#     ROOT  filesystem root ("" or "/" for the live system)
#     BOOT  the boot partition (/boot/firmware)
#   prints one "FAIL <check>: <path>" line per problem and returns the count
#   (capped at 255). Env PILNK_DIR overrides $ROOT/home/pilnk/pilnk;
#   STERILE_TEMPLATE overrides the config template.
#
# Read-only, and it never prints file contents: saved-connection directories are
# only tested for being non-empty (the entries are never listed), the shadow file
# is reduced to one character, and nothing else is opened except the readsb
# default file and udev rules (grep -q, no output).

_SC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STERILE_TEMPLATE="${STERILE_TEMPLATE:-$_SC_DIR/config.template.json}"

_SC_N=0
_SC_R=""

_sc_fail() { # check path
  printf 'FAIL %s: %s\n' "$1" "${2#"$_SC_R"}"
  _SC_N=$((_SC_N + 1))
}

# fail once per existing path (file, dir or symlink, even a dangling one)
_sc_absent() { # check path...
  local id="$1" p; shift
  for p in "$@"; do
    if [ -e "$p" ] || [ -L "$p" ]; then _sc_fail "$id" "$p"; fi
  done
}

# fail once if DIR has any entry; the entries are not listed (they can be named
# after a Wi-Fi network)
_sc_dir_empty() { # check dir
  [ -d "$2" ] || return 0
  if [ -n "$(find "$2" -mindepth 1 -print -quit 2>/dev/null)" ]; then _sc_fail "$1" "$2"; fi
}

# fail for each entry of DIR matching the find args
_sc_none_matching() { # check dir find-args...
  local id="$1" dir="$2" f; shift 2
  [ -d "$dir" ] || return 0
  while IFS= read -r -d '' f; do
    _sc_fail "$id" "$f"
  done < <(find "$dir" "$@" -print0 2>/dev/null)
}

_sc_enabled() { [ -e "$1" ] || [ -L "$1" ]; }

sterile_check() {
  local R="${1%/}" B="${2%/}" D W home h f st
  _SC_N=0; _SC_R="$R"
  D="${PILNK_DIR:-$R/home/pilnk/pilnk}"
  W="$R/etc/systemd/system/multi-user.target.wants"

  # -- logins
  _sc_none_matching ssh-hostkeys "$R/etc/ssh" -maxdepth 1 -name 'ssh_host_*'
  for home in "$R"/home/* "$R/root"; do
    [ -d "$home" ] || continue
    _sc_none_matching authorized-keys "$home" -maxdepth 3 -name authorized_keys
  done
  _sc_absent ssh-enabled "$W/ssh.service" "$W/sshd.service" \
    "$R/etc/systemd/system/sockets.target.wants/ssh.socket" \
    "$R/etc/systemd/system/sshd.service" "$B/ssh" "$B/ssh.txt"
  if [ -f "$R/etc/shadow" ]; then
    st=$(awk -F: '$1=="pilnk"{print "E" substr($2,1,1)}' "$R/etc/shadow" 2>/dev/null)
    [ "$st" = "E!" ] || _sc_fail shadow-locked "$R/etc/shadow"
  else
    _sc_fail shadow-locked "$R/etc/shadow"
  fi

  # -- cloud-init
  _sc_absent cloud-init-seed "$B/user-data" "$B/network-config" "$B/meta-data"
  [ -e "$R/etc/cloud/cloud-init.disabled" ] || _sc_fail cloud-init-enabled "$R/etc/cloud/cloud-init.disabled"

  # -- network
  _sc_dir_empty nm-connections "$R/etc/NetworkManager/system-connections"
  _sc_dir_empty nm-connections "$R/run/NetworkManager/system-connections"
  _sc_none_matching netplan-nm "$R/etc/netplan" -maxdepth 1 -name '90-NM-*.yaml'
  _sc_none_matching wpa-supplicant "$R/etc/wpa_supplicant" -maxdepth 1 -name '*.conf'
  _sc_absent wpa-supplicant "$B/wpa_supplicant.conf"
  for f in "$R/var/lib/NetworkManager" "$R/var/lib/dhcp" "$R/var/lib/dhcpcd"; do
    _sc_none_matching dhcp-lease "$f" -maxdepth 2 -name '*.lease*'
  done

  # -- identity
  [ ! -s "$R/etc/machine-id" ] || _sc_fail machine-id "$R/etc/machine-id"
  _sc_absent machine-id "$R/var/lib/dbus/machine-id"

  # -- traces
  _sc_dir_empty journal "$R/var/log/journal"
  for f in wtmp btmp lastlog; do
    [ ! -s "$R/var/log/$f" ] || _sc_fail logs "$R/var/log/$f"
  done
  _sc_dir_empty logs "$R/var/lib/lastlog"
  for home in "$R"/home/* "$R/root"; do
    [ -d "$home" ] || continue
    for h in .bash_history .zsh_history .sh_history .python_history .lesshst .viminfo .node_repl_history; do
      _sc_absent history "$home/$h"
    done
    for h in .cache .ssh .gitconfig .wget-hsts; do
      _sc_absent home-traces "$home/$h"
    done
  done

  # -- PiLNK
  if [ -f "$D/config.json" ] && [ -f "$STERILE_TEMPLATE" ]; then
    cmp -s "$D/config.json" "$STERILE_TEMPLATE" || _sc_fail config-json "$D/config.json"
  else
    _sc_fail config-json "$D/config.json"
  fi
  _sc_absent secret-key "$D/.secret_key"
  _sc_absent update-log "$D/update.log"
  _sc_absent ota-state "$D/ota_state.json"
  _sc_absent no-receiver "$D/.no_receiver"
  _sc_dir_empty capsules "$D/capsules"
  _sc_dir_empty history-data "$D/history"
  _sc_none_matching history-data "$D" -maxdepth 1 -name 'history_day.json*'
  _sc_dir_empty transcripts "$R/home/pilnk/atc-stt"
  _sc_absent firstboot-markers "$R/var/lib/pilnk-firstboot"
  _sc_absent firstboot-log "$R/var/log/pilnk-firstboot.log"

  # -- receiver
  if [ -f "$R/etc/default/readsb" ] && grep -qE -- '--device' "$R/etc/default/readsb" 2>/dev/null; then
    _sc_fail readsb-device "$R/etc/default/readsb"
  fi
  _sc_absent pilnkradio-config "$R/etc/pilnkradio/config.json"
  _sc_absent udev-rule "$R/etc/udev/rules.d/99-pilnk-v4.rules"
  if [ -d "$R/etc/udev/rules.d" ]; then
    while IFS= read -r -d '' f; do
      if grep -qE 'ATTRS?\{serial\}' "$f" 2>/dev/null; then _sc_fail udev-rule "$f"; fi
    done < <(find "$R/etc/udev/rules.d" -maxdepth 1 -name '*.rules' -print0 2>/dev/null)
  fi

  # -- armed for first boot
  _sc_enabled "$W/pilnk-firstboot.service" || _sc_fail firstboot-enabled "$W/pilnk-firstboot.service"
  _sc_enabled "$W/pilnk-wifi-setup.service" || _sc_fail wifi-setup-enabled "$W/pilnk-wifi-setup.service"
  ! _sc_enabled "$W/pilnk.service" || _sc_fail pilnk-enabled "$W/pilnk.service"
  ! _sc_enabled "$W/readsb.service" || _sc_fail readsb-enabled "$W/readsb.service"

  local n=$_SC_N
  [ "$n" -gt 255 ] && n=255
  return "$n"
}
