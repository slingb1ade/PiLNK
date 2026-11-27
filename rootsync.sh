#!/bin/bash
# rootsync.sh: root's own signed copy of PiLNK (Part B).
#
#   rootsync.sh            one sync (the timer): fetch the newest signed release,
#                          check AJ's signature against ROOT's pinned keys, extract
#                          that commit into a folder only root can write, swap it in.
#                          On a manual-update node it only checks (reason=held).
#   rootsync.sh --quick    the same, but it also moves a manual-update node, and it
#                          always exits 0 (ExecStartPre of the audio build: the
#                          build then runs from whatever current is)
#   rootsync.sh --selftest exit 0 if this copy can run here (tools, trust, state);
#                          a new rootsync is only installed after this passes
#   rootsync.sh --install-self
#                          one-time self-install (spec 5.4), run as root by the
#                          bootstrap block in sdr-recover.sh / pilnkradio-install.sh:
#                          root's layout, trust pinned from PINNED_SIGNERS, rootsync,
#                          rootsync-run and its timer from this folder. No sync.
#
# After every good run, root's own files are reconciled from current (spec 5.2
# step 8). root_policy=report (the default): only pilnk-rootsync.{service,timer}.
# root_policy=switch, once current holds everything the units need: every root
# unit, the udev rule, sudoers and PiLNK OS first boot run from
# /var/lib/pilnk/current, and status.json says switched:true. It never goes back.
#
# Root jobs run from /var/lib/pilnk/current, never from the PiLNK user's folder.
# This script never reads the user's tree. Spec:
# pilnk-tasks/root-signed-copy-spec.md 5.1-5.2.
#
# Layout (all root-owned, nothing group/world-writable, else "unsafe"):
#   /var/lib/pilnk/repo                 root's bare git repo (fetches from GitHub)
#   /var/lib/pilnk/signed/<v>-<c12>/    extracted signed trees (no .git)
#   /var/lib/pilnk/current, previous    links into signed/
#   /var/lib/pilnk/trust/release_signers  root's pinned public keys
#   /var/lib/pilnk/state/status.json    this script's result (readable by all)
#
# Exit: 0 ok or nothing to do (also: another run holds the lock)
#       1 failure, recorded in status.json (reason)
#       2 usage
#       3 internal error (status.json could not be written)
#
# Env (tests only; production uses the defaults):
#   PILNK_ROOTSYNC_PREFIX     prepended to every absolute root path
#   PILNK_RELEASE_URL         release details (default https://pilnk.io/api/version.php)
#   PILNK_ROOTSYNC_GIT_URL    code (default https://github.com/slingb1ade/PiLNK)
#   PILNK_ROOTSYNC_UID        the owner every root path must have (default 0)
#   PILNK_ROOTSYNC_LOCK_WAIT  seconds to wait for another run (default 60)
#   PILNK_ROOTSYNC_DEPTH      shallow depth for main (default 200)
#   PILNK_RELEASE_RETRY_DELAY seconds between download tries (default 5)
set -u
umask 022

P="${PILNK_ROOTSYNC_PREFIX:-}"
BASE="$P/var/lib/pilnk"
LIB="$P/usr/local/lib/pilnk"
LOCK="$P/run/pilnk-rootsync.lock"
RELEASE_URL="${PILNK_RELEASE_URL:-https://pilnk.io/api/version.php}"
GIT_URL="${PILNK_ROOTSYNC_GIT_URL:-https://github.com/slingb1ade/PiLNK}"
OWNER_UID="${PILNK_ROOTSYNC_UID:-0}"
LOCK_WAIT="${PILNK_ROOTSYNC_LOCK_WAIT:-60}"
DEPTH="${PILNK_ROOTSYNC_DEPTH:-200}"
RETRY_DELAY="${PILNK_RELEASE_RETRY_DELAY:-5}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# The release keys root trusts on its very first sync (spec 5.4 step 2): the
# repo's release_signers, byte for byte (tests/image/test_rootsync_bootstrap.sh
# checks it). The first sync also requires the fetched release to carry exactly
# these. Later rotations arrive the usual way, one release late.
# PILNK_ROOTSYNC_PINNED_SIGNERS (a file) replaces them in tests only.
PINNED_SIGNERS=$(cat <<'SIGNERS'
release namespaces="pilnk-release" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIATRIJFuAnfEFRO76I7BzZHfZBp4TbVRGu5AxC5SB0lUAAAAEXNzaDpwaWxuay1yZWxlYXNl pilnk-release-yubikey
release namespaces="pilnk-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKY/NXwfJKHl/dzUZjYOE4DuJ4BGI5NC7AKzsrzbgmBn pilnk-release-backup
SIGNERS
)

# git never reads anyone's config, never runs hooks or an fsmonitor, never prompts.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
RG() { git -c core.hooksPath=/dev/null -c core.fsmonitor= -C "$BASE/repo" "$@"; }

MODE=timer
case "$#:${1:-}" in
    0:) ;;
    1:--quick) MODE=quick ;;
    1:--selftest) MODE=selftest ;;
    3:--selftest) MODE=selftest; ST_STATEMENT="$2"; ST_SIG="$3" ;;
    1:--install-self) MODE=install ;;
    *) echo "usage: rootsync.sh [--quick | --selftest | --install-self]" >&2; exit 2 ;;
esac

# --install-self: only as root's uid, and only from a complete folder.
if [ "$MODE" = install ]; then
    [ "$(id -u)" = "$OWNER_UID" ] || exit 0
    for t in rootsync.sh release-check.sh rootside/rootsync-run rootside/pilnk-rootsync.service \
             rootside/pilnk-rootsync.timer; do
        [ -f "$HERE/$t" ] || { echo "rootsync: --install-self: $t missing beside me" >&2; exit 1; }
    done
fi

# ---- --selftest: can this copy run here? (no lock: the old rootsync holds it) --
if [ "$MODE" = selftest ]; then
    for t in bash python3 git curl flock tar ssh-keygen timeout find install cmp sort sed; do
        command -v "$t" >/dev/null 2>&1 || { echo "rootsync selftest: no $t" >&2; exit 1; }
    done
    [ -r "$BASE/trust/release_signers" ] && [ -s "$BASE/trust/release_signers" ] \
        || { echo "rootsync selftest: no pinned signers" >&2; exit 1; }
    t=$(mktemp "$BASE/state/selftest.XXXXXX" 2>/dev/null) && rm -f "$t" \
        || { echo "rootsync selftest: state not writable" >&2; exit 1; }
    # Given the release this copy came in (spec 5.2): it must verify it with its
    # own checker against root's pinned keys, or it is not installed.
    if [ -n "${ST_STATEMENT:-}" ]; then
        timeout 30 env PILNK_DIR="$BASE/repo" RELEASE_SIGNERS="$BASE/trust/release_signers" \
            bash "$HERE/release-check.sh" "$ST_STATEMENT" "$ST_SIG" 2>/dev/null | grep -qx 'ota_sig=ok' \
            || { echo "rootsync selftest: cannot verify the release it came in" >&2; exit 1; }
    fi
    exit 0
fi

# --quick never fails the audio build; every exit also cleans the download.
REL_TMP=""; RENDER_TMP=""
on_exit() {
    local rc=$?
    [ -n "$REL_TMP" ] && rm -rf "$REL_TMP"
    [ -n "$RENDER_TMP" ] && rm -rf "$RENDER_TMP"
    [ "$MODE" = quick ] && exit 0
    exit "$rc"
}
trap on_exit EXIT

# ---- status.json -------------------------------------------------------------
# write_status SRC REASON VERSION COMMIT OK(1|0)
# Keeps switched/ok_at from the last file, counts consecutive failures, and
# only ever stores a short reason token: ota_sig_why's rule plus "_", because
# release-check.sh's own tokens (not_on_main, no_tool) are reasons here too.
write_status() {
    python3 -I - "$BASE/state/status.json" "$1" "$2" "$3" "$4" "$5" "$(read_policy)" "${SWITCHED_NOW:-0}" <<'PY'
import json, os, re, sys, time
path, src, reason, ver, commit, ok, policy, now_switched = sys.argv[1:9]
try:
    old = json.load(open(path))
    if not isinstance(old, dict):
        old = {}
except Exception:
    old = {}
now = int(time.time())
if not re.match(r'^[a-z][a-z_]{0,11}(:[0-9]{1,3})?$', reason or ''):
    reason = ''
d = {
    'src': src, 'version': ver, 'commit': commit,
    'ok_at': now if ok == '1' else int(old.get('ok_at') or 0),
    'checked_at': now, 'reason': reason, 'policy': policy,
    'switched': bool(old.get('switched')) or now_switched == '1',
    'fails': 0 if ok == '1' else int(old.get('fails') or 0) + 1,
}
tmp = '%s.tmp.%d' % (path, os.getpid())
with open(tmp, 'w') as f:
    json.dump(d, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

read_policy() {
    local p=""
    [ -f "$BASE/current/root_policy" ] && p=$(head -n 1 "$BASE/current/root_policy" 2>/dev/null | tr -d '[:space:]')
    [ "$p" = switch ] && echo switch || echo report
}

switched() {
    python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("switched") else 1)' \
        "$BASE/state/status.json" 2>/dev/null
}

# What `current` holds: "VERSION COMMIT" from its .complete marker, or nothing.
current_id() {
    local c="$BASE/current/.complete"
    [ -f "$c" ] && head -n 1 "$c" 2>/dev/null
}

ok_status() { # VERSION COMMIT [REASON]
    local src=user
    { switched || [ "${SWITCHED_NOW:-0}" = 1 ]; } && src=signed
    write_status "$src" "${3:-$SU_REASON}" "$1" "$2" 1 || exit 3
}
SU_REASON=""

# ---- who the PiLNK user is, from root's own pilnk.service (never from the user) --
# svc_info: sets SVC_USER, SVC_HOME, SVC_APP. Returns 1 if there is no usable unit.
# The app folder is the folder of app.py in ExecStart; if that can't be read
# cleanly, it is SVC_HOME/pilnk, the layout install.sh writes.
svc_info() {
    local unit="$P/etc/systemd/system/pilnk.service" u h a g
    SVC_USER=""; SVC_HOME=""; SVC_APP=""; SVC_GROUP=""
    [ -f "$unit" ] || return 1
    u=$(sed -n 's/^[[:space:]]*User=\([^[:space:]]*\)[[:space:]]*$/\1/p' "$unit" 2>/dev/null | tail -n 1)
    [[ $u =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || return 1
    if [ -n "$P" ]; then
        h=$(awk -F: -v u="$u" '$1 == u { print $6; exit }' "$P/etc/passwd" 2>/dev/null)
    else
        h=$(getent passwd "$u" 2>/dev/null | cut -d: -f6)
    fi
    [[ $h =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ $h != *..* ]] || return 1
    a=$(sed -n 's/^[[:space:]]*ExecStart=.*[[:space:]]\(\/[^[:space:]]*\)\/app\.py[[:space:]]*$/\1/p' "$unit" 2>/dev/null | tail -n 1)
    [[ $a =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ $a != *..* ]] || a="$h/pilnk"
    if [ -n "$P" ]; then
        g=$(awk -F: -v u="$u" '$1 == u { print $4; exit }' "$P/etc/passwd" 2>/dev/null)
        g=$(awk -F: -v g="$g" '$3 == g { print $1; exit }' "$P/etc/group" 2>/dev/null)
    else
        g=$(id -gn "$u" 2>/dev/null)
    fi
    [[ $g =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || g=$u
    SVC_USER=$u; SVC_HOME=$h; SVC_APP=$a; SVC_GROUP=$g
}

# read_user_json PATH: the user's config.json, read as data only. Prints
# "false" only when auto_update is exactly false; "true" on anything else
# (missing, symlink, FIFO, over 64 KB, bad JSON, slow). Never sourced or run.
read_user_json() {
    local v
    v=$(timeout 5 python3 -I -c '
import json, os, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
        raise ValueError("not a small regular file")
    data = os.read(fd, 65537)
    if len(data) > 65536:
        raise ValueError("too big")
    d = json.loads(data.decode("utf-8"))
    print("false" if isinstance(d, dict) and d.get("auto_update") is False else "true")
except Exception:
    print("true")
' "$1" 2>/dev/null)
    [ "$v" = false ] && echo false || echo true
}

auto_update() {
    svc_info || { echo true; return; }
    read_user_json "$SVC_APP/config.json"
}

# The app tree's VERSION, read as data (nofollow, regular file, <= 64 bytes);
# empty unless it is a plain version string.
user_version() {
    svc_info || return 0
    timeout 5 python3 -I -c '
import os, re, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    st = os.fstat(fd)
    if stat.S_ISREG(st.st_mode) and st.st_size <= 64:
        v = os.read(fd, 65).decode("ascii").strip()
        if re.fullmatch(r"[0-9]+(\.[0-9]+){1,3}", v):
            print(v)
except Exception:
    pass
' "$SVC_APP/VERSION" 2>/dev/null
}

# ---- keep the installed rootsync (and its wrapper) in step with current ---------
# A new rootsync.sh is installed only once its own --selftest passes here; the
# old one is kept as rootsync.prev for rootsync-run's fallback.
self_update() {
    local new="$BASE/current/rootsync.sh" run="$BASE/current/rootside/rootsync-run"
    [ -f "$BASE/current/.complete" ] || return 0
    [ -d "$LIB" ] || mkdir -m 0755 -p "$LIB" || return 0
    if [ -f "$new" ] && ! cmp -s "$new" "$LIB/rootsync"; then
        local st=()
        [ -n "$REL_TMP" ] && [ -s "$REL_TMP/statement" ] && [ -s "$REL_TMP/sig" ] && st=("$REL_TMP/statement" "$REL_TMP/sig")
        if timeout 60 bash "$new" --selftest "${st[@]}" >/dev/null 2>&1; then
            if [ -f "$LIB/rootsync" ]; then
                install -m 0755 "$LIB/rootsync" "$LIB/rootsync.prev.new" \
                    && mv -f "$LIB/rootsync.prev.new" "$LIB/rootsync.prev"
            fi
            install -m 0755 "$new" "$LIB/rootsync.new" && mv -f "$LIB/rootsync.new" "$LIB/rootsync"
        else
            SU_REASON=selftest
            echo "rootsync: the new rootsync failed its selftest, keeping the installed one" >&2
        fi
    fi
    # The checker the first sync uses when there is no current yet.
    if [ -f "$BASE/current/release-check.sh" ] && ! cmp -s "$BASE/current/release-check.sh" "$LIB/release-check.sh" \
       && bash -n "$BASE/current/release-check.sh" 2>/dev/null; then
        install -m 0644 "$BASE/current/release-check.sh" "$LIB/release-check.sh.new" \
            && mv -f "$LIB/release-check.sh.new" "$LIB/release-check.sh"
    fi
    if [ -f "$run" ] && ! cmp -s "$run" "$LIB/rootsync-run" && bash -n "$run" 2>/dev/null; then
        install -m 0755 "$run" "$LIB/rootsync-run.new" && mv -f "$LIB/rootsync-run.new" "$LIB/rootsync-run"
    fi
}

fail() { # REASON
    local id ver="" commit=""
    id=$(current_id)
    [ -n "$id" ] && { ver=${id%% *}; commit=${id#* }; }
    write_status error "$1" "$ver" "$commit" 0 || exit 3
    echo "rootsync: refused or failed: $1" >&2
    exit 1
}

# ---- root's own files, from current (spec 5.2 step 8) -------------------------
# Rendered values never come from the user: user, group and app folder are read
# from root's pilnk.service (svc_info); the code path is always the real
# /var/lib/pilnk/current (no test prefix inside a rendered file).
ROOT_CURRENT=/var/lib/pilnk/current
SYSD="$P/etc/systemd/system"
SWITCH_NEEDS="rootside/pilnk-sdr-recover.service rootside/pilnk-dashboard-watchdog.service
rootside/pilnk-audio-build.service rootside/pilnk-rootsync.service rootside/pilnk-rootsync.timer
rootside/sudoers.template pilnk-sdr-recover.timer 99-pilnk-sdr.rules sdr-recover.sh
pilnk-dashboard-watchdog.sh pilnkradio-install.sh"
FIRSTBOOT_LINE='ROOT_CODE="${PILNK_ROOT_CODE:-$PILNK_DIR}"'
FIRSTBOOT_ROOT='ROOT_CODE="${PILNK_ROOT_CODE:-/var/lib/pilnk/current}"'

render() { # TEMPLATE OUT
    sed -e "s|__ROOT_CURRENT__|$ROOT_CURRENT|g" -e "s|__PILNK_USER__|$SVC_USER|g" \
        -e "s|__PILNK_GROUP__|$SVC_GROUP|g" -e "s|__PILNK_APP_DIR__|$SVC_APP|g" \
        -e "s|__SYSTEMCTL__|$SYSTEMCTL|g" "$1" > "$2" || fail disk
}

# install_if_changed SRC DST MODE: returns 0 if DST was (re)written, 1 if it
# already matched. A symlink at DST is replaced, never followed.
install_if_changed() {
    local src="$1" dst="$2" mode="$3"
    if [ -f "$dst" ] && [ ! -L "$dst" ] && cmp -s "$src" "$dst"; then return 1; fi
    mkdir -p "$(dirname "$dst")" || fail disk
    if install -m "$mode" "$src" "$dst.pilnk-new" && mv -f "$dst.pilnk-new" "$dst"; then
        return 0
    fi
    rm -f "$dst.pilnk-new"; fail disk
}

reconcile() {
    local cur="$BASE/current" mode=report t units=0 udev=0 wd=0 rw=0 sc
    [ -f "$cur/.complete" ] || return 0
    { [ "$(read_policy)" = switch ] || switched; } && mode=switch
    RENDER_TMP=$(mktemp -d "$BASE/state/render.XXXXXX") || exit 3
    if [ "$mode" = switch ]; then
        svc_info || fail nouser
        for t in $SWITCH_NEEDS; do [ -f "$cur/$t" ] || fail nocopy; done
        sc=$(command -v systemctl 2>/dev/null)
        [[ $sc =~ ^/[A-Za-z0-9._/-]+$ ]] || sc=/usr/bin/systemctl
        SYSTEMCTL=$sc
        # sudoers first: if it doesn't validate, nothing at all changes.
        render "$cur/rootside/sudoers.template" "$RENDER_TMP/sudoers"
        visudo -c -f "$RENDER_TMP/sudoers" >/dev/null 2>&1 || fail sudoers
        for t in pilnk-sdr-recover.service pilnk-dashboard-watchdog.service pilnk-audio-build.service; do
            render "$cur/rootside/$t" "$RENDER_TMP/$t"
        done
    fi
    for t in pilnk-rootsync.service pilnk-rootsync.timer; do
        [ -f "$cur/rootside/$t" ] || continue   # a release from before Part B
        install_if_changed "$cur/rootside/$t" "$SYSD/$t" 0644 && units=1
    done
    if [ "$mode" = switch ]; then
        install_if_changed "$RENDER_TMP/pilnk-sdr-recover.service" "$SYSD/pilnk-sdr-recover.service" 0644 && units=1
        install_if_changed "$cur/pilnk-sdr-recover.timer" "$SYSD/pilnk-sdr-recover.timer" 0644 && units=1
        install_if_changed "$RENDER_TMP/pilnk-audio-build.service" "$SYSD/pilnk-audio-build.service" 0644 && units=1
        if install_if_changed "$RENDER_TMP/pilnk-dashboard-watchdog.service" "$SYSD/pilnk-dashboard-watchdog.service" 0644; then
            units=1; wd=1
        fi
        install_if_changed "$cur/99-pilnk-sdr.rules" "$P/etc/udev/rules.d/99-pilnk-sdr.rules" 0644 && udev=1
        install_if_changed "$RENDER_TMP/sudoers" "$P/etc/sudoers.d/pilnk-ota" 0440
        if [ -f "$P/usr/local/bin/pilnk-radio-watchdog.sh" ] && [ -f "$cur/pilnk-radio-watchdog.sh" ]; then
            install_if_changed "$cur/pilnk-radio-watchdog.sh" "$P/usr/local/bin/pilnk-radio-watchdog.sh" 0755 && rw=1
        fi
        # PiLNK OS first boot, only where it exists: same script, but hw-setup and
        # self-heal come from current. Rendered only if the line is there exactly once.
        t="$cur/image/firstboot/pilnk-firstboot.sh"
        if [ -f "$P/usr/local/sbin/pilnk-firstboot" ] && [ -f "$t" ]; then
            if [ "$(grep -cxF "$FIRSTBOOT_LINE" "$t")" = 1 ]; then
                awk -v o="$FIRSTBOOT_LINE" -v n="$FIRSTBOOT_ROOT" '$0 == o { print n; next } { print }' \
                    "$t" > "$RENDER_TMP/firstboot" || fail disk
                if bash -n "$RENDER_TMP/firstboot" 2>/dev/null; then
                    install_if_changed "$RENDER_TMP/firstboot" "$P/usr/local/sbin/pilnk-firstboot" 0755
                fi
            else
                echo "rootsync: first boot script has changed shape, leaving it" >&2
            fi
        fi
    fi
    [ "$units" = 1 ] && systemctl daemon-reload
    [ "$udev" = 1 ] && udevadm control --reload-rules
    systemctl is-active --quiet pilnk-rootsync.timer || systemctl enable --now pilnk-rootsync.timer
    if [ "$mode" = switch ]; then
        # Self-heal and the watchdog keep running, now from current (bootstrap-
        # selfheal.sh stops touching these once switched).
        if ! systemctl is-active --quiet pilnk-sdr-recover.timer; then
            systemctl enable pilnk-sdr-recover.timer; systemctl --no-block start pilnk-sdr-recover.timer
        fi
        if ! systemctl is-active --quiet pilnk-dashboard-watchdog.service; then
            systemctl enable pilnk-dashboard-watchdog.service; systemctl --no-block start pilnk-dashboard-watchdog.service
        elif [ "$wd" = 1 ]; then
            systemctl --no-block try-restart pilnk-dashboard-watchdog.service
        fi
        [ "$rw" = 1 ] && systemctl --no-block try-restart pilnk-radio-watchdog.service
        SWITCHED_NOW=1
    fi
    rm -rf "$RENDER_TMP"; RENDER_TMP=""
    return 0
}

# ---- --install-self (spec 5.4) ---------------------------------------------------
# Idempotent: trust is written only if there is none (a rotated trust is kept),
# files only if they differ, and the timer is enabled only when something changed
# or it is not enabled. The first sync runs later, from the timer.
install_self() {
    local t changed=0 fresh=0
    # Not yet installed (first try, or an earlier try stopped part way):
    # enable and start whatever systemd says, so a retry always finishes.
    [ -x "$LIB/rootsync" ] || fresh=1
    if [ ! -s "$BASE/trust/release_signers" ]; then
        if [ -n "${PILNK_ROOTSYNC_PINNED_SIGNERS:-}" ]; then
            install -m 0644 "$PILNK_ROOTSYNC_PINNED_SIGNERS" "$BASE/trust/release_signers.new" || exit 3
        else
            printf '%s\n' "$PINNED_SIGNERS" > "$BASE/trust/release_signers.new" \
                && chmod 0644 "$BASE/trust/release_signers.new" || exit 3
        fi
        mv -f "$BASE/trust/release_signers.new" "$BASE/trust/release_signers" || exit 3
    fi
    [ -d "$LIB" ] || mkdir -m 0755 -p "$LIB" || exit 3
    [ -f "$BASE/state/status.json" ] || write_status user installing "" "" 0 || exit 3
    install_if_changed "$HERE/release-check.sh" "$LIB/release-check.sh" 0644 || true
    install_if_changed "$HERE/rootside/rootsync-run" "$LIB/rootsync-run" 0755 || true
    for t in pilnk-rootsync.service pilnk-rootsync.timer; do
        install_if_changed "$HERE/rootside/$t" "$SYSD/$t" 0644 && changed=1
    done
    if [ "$changed" = 1 ] || [ "$fresh" = 1 ] || ! systemctl is-enabled --quiet pilnk-rootsync.timer; then
        [ "$changed" = 1 ] && systemctl daemon-reload
        systemctl enable pilnk-rootsync.timer || { echo "rootsync: enabling the timer failed; will retry" >&2; exit 3; }
        changed=1
    fi
    # Last: the bootstrap blocks treat an executable rootsync as "installed".
    install_if_changed "$HERE/rootsync.sh" "$LIB/rootsync" 0755 || true
    [ -x "$LIB/rootsync" ] || exit 3
    [ "$changed" = 1 ] && systemctl --no-block start pilnk-rootsync
    # Enabling a timer only arms it for the next boot: start it now as well, or a
    # first sync that fails is not retried until the node reboots (27 Nov hub trial).
    systemctl is-active --quiet pilnk-rootsync.timer || systemctl --no-block start pilnk-rootsync.timer
    echo "rootsync: installed; the first sync runs from pilnk-rootsync.timer"
}

# ---- 1. one run at a time ----------------------------------------------------
mkdir -p "$P/run" 2>/dev/null
exec 9>"$LOCK" || exit 3
flock -w "$LOCK_WAIT" 9 || exit 0

# ---- 2. every root path is root's alone --------------------------------------
unsafe() {
    mkdir -p "$BASE/state" 2>/dev/null
    local id ver="" commit=""
    id=$(current_id); [ -n "$id" ] && { ver=${id%% *}; commit=${id#* }; }
    write_status unsafe unsafe "$ver" "$commit" 0 2>/dev/null
    echo "rootsync: unsafe ownership or modes under $1, refusing" >&2
    exit 1
}
[ -L "$BASE" ] && { echo "rootsync: $BASE is a symlink, refusing" >&2; exit 1; }
for d in "$BASE" "$LIB"; do
    [ -e "$d" ] || continue
    bad=$(find "$d" \( ! -uid "$OWNER_UID" -o \( ! -type l -perm /022 \) \) -print -quit 2>/dev/null)
    [ -n "$bad" ] && unsafe "$d"
done

for d in "$BASE" "$BASE/signed" "$BASE/trust" "$BASE/state" "$BASE/build"; do
    [ -d "$d" ] || mkdir -m 0755 -p "$d" || exit 3
done
if [ ! -d "$BASE/repo" ]; then
    git init -q --bare "$BASE/repo" || fail git
fi
if [ "$MODE" = install ]; then
    install_self; exit 0
fi
RG remote remove origin >/dev/null 2>&1
RG remote add origin "$GIT_URL" || fail git

# An interrupted swap: current missing or without .complete. Use previous,
# never anything older.
if [ ! -f "$BASE/current/.complete" ] && [ -L "$BASE/previous" ] && [ -f "$BASE/previous/.complete" ]; then
    ln -sfn "$(readlink "$BASE/previous")" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current"
    echo "rootsync: current was incomplete, using previous" >&2
fi

# ---- 3. the newest release object from pilnk.io ------------------------------
REL_TMP=$(mktemp -d "$BASE/state/release.XXXXXX") || exit 3
CURL_RC=0
for TRY in 1 2 3; do
    CURL_RC=0
    curl -fsS --max-time 15 --max-filesize 1048576 --proto '=https,file' \
        -H 'User-Agent: PiLNK-rootsync/1.0' -o "$REL_TMP/version.json" "$RELEASE_URL" 2>/dev/null \
        || CURL_RC=$?
    [ "$CURL_RC" = 0 ] && break
    [ "$TRY" = 3 ] && break
    sleep "$RETRY_DELAY" 2>/dev/null
done
[ "$CURL_RC" = 0 ] || fail "fetch:$CURL_RC"
python3 -I - "$REL_TMP" <<'PY' 2>/dev/null || fail parse
import json, os, sys
d = sys.argv[1]
rel = json.load(open(os.path.join(d, 'version.json'))).get('release')
def text(k):
    v = rel.get(k) if isinstance(rel, dict) else None
    return v.encode('utf-8') if isinstance(v, str) else b''
for name in ('statement', 'sig'):
    with open(os.path.join(d, name), 'wb') as f:
        f.write(text(name))
PY

# ---- 4. AJ's signature, against ROOT's pinned keys ---------------------------
# The check is release-check.sh from the copy root already trusts; on the very
# first sync, the one shipped next to this script.
CHECKER="$HERE/release-check.sh"
[ -f "$BASE/current/.complete" ] && [ -f "$BASE/current/release-check.sh" ] && CHECKER="$BASE/current/release-check.sh"
[ -f "$CHECKER" ] || fail no_tool
check() { # [--fetched]
    local out tok why
    out=$(PILNK_DIR="$BASE/repo" RELEASE_SIGNERS="$BASE/trust/release_signers" \
          bash "$CHECKER" "$@" "$REL_TMP/statement" "$REL_TMP/sig" 2>/dev/null)
    tok=$(printf '%s\n' "$out" | sed -n 's/^ota_sig=//p')
    why=$(printf '%s\n' "$out" | sed -n 's/^ota_sig_why=//p')
    SV=$(printf '%s\n' "$out" | sed -n 's/^signed_version=//p')
    SC=$(printf '%s\n' "$out" | sed -n 's/^signed_commit=//p')
    case "$tok" in
        ok) return 0 ;;
        error) fail "${why:-check}" ;;
        '') fail check ;;
        *) fail "$tok" ;;
    esac
}
SV=""; SC=""
check
[[ $SV =~ ^[0-9]+(\.[0-9]+){1,3}$ ]] && [[ $SC =~ ^[0-9a-f]{40}$ ]] || fail check

# ---- 5. nothing to do, or never backwards ------------------------------------
ver_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]; }
CUR=$(current_id)
if [ -n "$CUR" ]; then
    CV=${CUR%% *}; CC=${CUR#* }
    if [ "$CV" = "$SV" ] && [ "$CC" = "$SC" ]; then
        self_update; reconcile; ok_status "$SV" "$SC"; exit 0
    fi
    if [ "$CV" = "$SV" ] || ver_lt "$SV" "$CV"; then
        fail older
    fi
    # Manual-update node: the timer only checks. Root moves with the audio build
    # (--quick), i.e. when the owner installs the update.
    # Also moves when the owner has installed this exact release (VERSION in
    # the app tree, read as data): root never goes past what the owner chose,
    # never backwards, and only ever to a signed release.
    if [ "$MODE" = timer ] && [ "$(auto_update)" = false ] && [ "$(user_version)" != "$SV" ]; then
        self_update; reconcile; ok_status "$CV" "$CC" held; exit 0
    fi
fi

# ---- room for one more copy (spec 7: about 250 MB in all) --------------------
# Need the size of current (or 80 MB on a first sync) plus 64 MB, else stop here.
AVAIL=$(df -Pk "$BASE" 2>/dev/null | awk 'NR==2 {print $4}')
NEED=81920
[ -n "$CUR" ] && NEED=$(du -sk "$BASE/current/" 2>/dev/null | awk '{print $1}')
[[ $NEED =~ ^[0-9]+$ ]] || NEED=81920
if [[ $AVAIL =~ ^[0-9]+$ ]] && [ "$AVAIL" -lt $((NEED + 65536)) ]; then
    fail space
fi

# ---- 6. that commit from GitHub, into root's own repo ------------------------
RG fetch -q --no-tags --depth "$DEPTH" origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || fail git
if ! RG cat-file -e "${SC}^{commit}" 2>/dev/null; then
    RG fetch -q --no-tags --depth 1 origin "$SC" 2>/dev/null   # the check below says why if it is not on main
fi
check --fetched

# ---- 7. extract, check, swap ---------------------------------------------------
NAME="$SV-${SC:0:12}"
find "$BASE/signed" -mindepth 1 -maxdepth 1 -name '*.tmp' -exec rm -rf {} + 2>/dev/null
TMPD="$BASE/signed/$NAME.tmp"
mkdir -m 0755 "$TMPD" || fail disk
# Root never runs the dashboard's web assets or the test harness: leave them out
# (they are most of the tree's size).
if ! ( set -o pipefail; RG archive --format=tar "$SC" -- . ':(exclude)static' ':(exclude)tests' | tar -x --no-same-owner --no-same-permissions -C "$TMPD" ) 2>/dev/null; then
    rm -rf "$TMPD"; fail disk
fi
MAN="$TMPD/rootside/MANIFEST"
[ -f "$MAN" ] || { rm -rf "$TMPD"; fail manifest; }
while IFS= read -r m || [ -n "$m" ]; do
    case "$m" in ''|'#'*) continue ;; esac
    if [[ ! $m =~ ^[A-Za-z0-9._/-]+$ ]] || [[ $m == *..* ]] || [ ! -f "$TMPD/$m" ]; then
        rm -rf "$TMPD"; fail manifest
    fi
    bash -n "$TMPD/$m" 2>/dev/null || { rm -rf "$TMPD"; fail syntax; }
done < "$MAN"
# First sync (spec 5.4 step 2): the release root fetched for itself must carry
# exactly the keys root pinned. After that, rotation is the usual one-late rule.
if [ -z "$CUR" ] && ! cmp -s "$TMPD/release_signers" "$BASE/trust/release_signers"; then
    rm -rf "$TMPD"; fail signers
fi
printf '%s %s\n' "$SV" "$SC" > "$TMPD/.complete" || { rm -rf "$TMPD"; fail disk; }
rm -rf "$BASE/signed/$NAME"
mv "$TMPD" "$BASE/signed/$NAME" || { rm -rf "$TMPD"; fail disk; }

if [ -L "$BASE/current" ]; then
    ln -sfn "$(readlink "$BASE/current")" "$BASE/previous.new" && mv -T "$BASE/previous.new" "$BASE/previous"
fi
ln -sfn "signed/$NAME" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current" || fail disk

# Signer rotation: a release that ships a new release_signers is trusted for
# the release AFTER it (this one was verified with the old keys).
if [ -f "$BASE/current/release_signers" ] && ! cmp -s "$BASE/current/release_signers" "$BASE/trust/release_signers"; then
    install -m 0644 "$BASE/current/release_signers" "$BASE/trust/release_signers.new" \
        && mv -f "$BASE/trust/release_signers.new" "$BASE/trust/release_signers"
fi

# Keep current and previous only.
KEEP1=$(basename "$(readlink "$BASE/current")")
KEEP2=$(basename "$(readlink "$BASE/previous" 2>/dev/null)" 2>/dev/null)
for d in "$BASE/signed"/*; do
    [ -e "$d" ] || continue
    b=$(basename "$d")
    [ "$b" = "$KEEP1" ] || [ "$b" = "$KEEP2" ] || rm -rf "$d"
done

self_update
reconcile
ok_status "$SV" "$SC"
exit 0
