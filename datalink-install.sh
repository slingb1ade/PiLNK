#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  datalink-install.sh — add VDL2 datalink (ACARS over VDL Mode 2)
#  to a PiLNK node. Opt-in. Idempotent: safe to re-run.
#
#  Builds dumpvdl2 against the rtl-sdr-blog driver fork, picks (or
#  prepares) a dedicated RTL-SDR stick for it, writes
#  /etc/pilnk-datalink/config.json and installs pilnk-datalink.service.
#
#  Needs: a PiLNK node, a SPARE RTL-SDR stick (not the ADS-B or radio
#  one) and an airband antenna (a splitter off the radio antenna works).
#
#  LOCAL ONLY. Decoded messages stay on this node: nothing goes in the
#  ping and nothing is fed to any third party.
#
#  Run:   bash ~/pilnk/datalink-install.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BOLD='\033[1m'; BLUE='\033[0;34m'; RESET='\033[0m'
ok()   { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
info() { printf "${CYAN}→ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}⚠ %s${RESET}\n" "$1"; }
err()  { printf "${RED}✗ %s${RESET}\n" "$1"; }
step() { CURRENT_STEP="$1"; _state running; printf "\n${BOLD}${BLUE}[ %s ]${RESET}\n" "$1"; }
die()  { LAST_ERR="$1"; err "$1"; exit 1; }

DUMPVDL2_TAG="v2.7.0"
LIBACARS_TAG="v2.2.1"
# Every path below can be overridden by a PILNK_DL_* variable; that is for the
# test harness only. In normal use none are set and the defaults apply.
FORK_ROOT="${PILNK_DL_FORK_ROOT:-/usr/local/lib}"                    # where the rtl-sdr-blog fork installs
BIN="${PILNK_DL_BIN:-/usr/local/bin/dumpvdl2}"
BUILT_STAMP="${PILNK_DL_STAMP:-/usr/local/share/pilnk-datalink/BUILT}"  # holds the tag we built
SRC="${PILNK_DL_SRC:-$HOME/datalink-build}"
STATE_FILE="${PILNK_DL_STATE:-$HOME/pilnk/datalink_build_state.json}"
MODPROBE_CONF="${PILNK_DL_MODPROBE:-/etc/modprobe.d/blacklist-rtl.conf}"
SYSFS="${PILNK_DL_SYSFS:-/sys/bus/usb/devices}"
TTY="${PILNK_DL_TTY:-/dev/tty}"
CFG="${PILNK_DL_CFG:-/etc/pilnk-datalink/config.json}"
DUMP1090_DEFAULT="${PILNK_DL_DUMP1090_DEFAULT:-/etc/default/dump1090-fa}"
READSB_DEFAULT="${PILNK_DL_READSB_DEFAULT:-/etc/default/readsb}"
RADIO_CFG="${PILNK_DL_RADIO_CFG:-/etc/pilnkradio/config.json}"
PAUSE_FILE="${PILNK_DL_PAUSE:-/run/pilnk-sdr-pause}"   # read by sdr-recover.sh (7 Oct 2026)
PAUSE_SECS=1800                                         # sdr-recover ignores anything longer
UNIT_DIR="${PILNK_DL_UNIT_DIR:-/etc/systemd/system}"
LIBEXEC="${PILNK_DL_LIBEXEC:-/usr/local/lib/pilnk}"     # root-owned copies: the unit never runs repo files
STATUS="${PILNK_DL_STATUS:-/run/pilnk-datalink/status.json}"
VERIFY_SECS="${PILNK_DL_VERIFY_SECS:-30}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
J=$(nproc 2>/dev/null || echo 2)

# ── build outcome, recorded locally (same shape as audio_build_state.json) ──
CURRENT_STEP="0/7 starting"
LAST_ERR=""
_state() {
    local d
    d="$(printf '%s' "${2:-}" | tr -d '\\"' | tr '\n\r\t' '   ' | cut -c1-200)"
    printf '{"step":"%s","result":"%s","detail":"%s","ts":%s}\n' \
        "$CURRENT_STEP" "$1" "$d" "$(date +%s)" > "$STATE_FILE" 2>/dev/null || true
}
_on_exit() {
    local rc=$?
    if [ "$rc" -eq 0 ]; then _state ok; else _state failed "$LAST_ERR"; fi
    return "$rc"
}

# ── driver fork helpers ──────────────────────────────────────
# Directory holding the rtl-sdr-blog librtlsdr.so (flat or multiarch subdir),
# or nothing. A [ -e ] test per candidate: no `ls … | head` (killer #3).
find_fork_dir() {
    local c
    for c in "$FORK_ROOT"/librtlsdr.so "$FORK_ROOT"/*/librtlsdr.so; do
        if [ -e "$c" ]; then dirname "$c"; return 0; fi
    done
    return 0
}

# True if ldd output (passed as $1) shows librtlsdr coming from the fork prefix.
# Takes the TEXT, never a pipe: `ldd | grep -q` is killer #4 (SIGPIPE + pipefail).
links_fork() {
    local re="${FORK_ROOT}(/[^/]+)?/librtlsdr"
    [[ "$1" =~ $re ]]
}

# The installed dumpvdl2 is current: our stamp says this tag, and it links the fork.
installed_current() {
    [ -x "$BIN" ] && [ -f "$BUILT_STAMP" ] || return 1
    [ "$(cat "$BUILT_STAMP" 2>/dev/null)" = "$DUMPVDL2_TAG" ] || return 1
    links_fork "$(ldd "$BIN" 2>/dev/null || true)"
}

# Same commands as pilnkradio-install.sh [3/8]. Only runs when the fork is missing.
build_fork() {
    info "the rtl-sdr-blog driver fork is not installed — building it (~3-5 min on a Pi 4)"
    if [ ! -d "$HOME/rtl-sdr-blog" ]; then
        git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog "$HOME/rtl-sdr-blog"
    fi
    cmake -B "$HOME/rtl-sdr-blog/build" "$HOME/rtl-sdr-blog" \
        -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON
    make -C "$HOME/rtl-sdr-blog/build" -j"$J"
    sudo make -C "$HOME/rtl-sdr-blog/build" install
    sudo ldconfig
    echo 'blacklist dvb_usb_rtl28xxu' | sudo tee "$MODPROBE_CONF" >/dev/null
}

# ── [1/7] checks ─────────────────────────────────────────────
step_checks() {
    step "1/7 checks"
    if [ "$(id -u)" -eq 0 ] && [ -z "${PILNK_NONINTERACTIVE:-}" ]; then
        die "Run as your normal user, not root (sudo is used where needed)."
    fi
    command -v sudo >/dev/null || die "sudo is required."
    ok "running as $(id -un)"
}

# ── [2/7] build dependencies ─────────────────────────────────
step_deps() {
    step "2/7 build dependencies"
    # `update` is advisory: one stale third-party repo must not block packages
    # that are already present (pilnkradio-install learned this on adsb-pi).
    sudo apt-get update -qq \
        || warn "apt-get update reported errors — continuing; the install below decides"
    local out errs
    if ! out="$(sudo apt-get install -y --no-install-recommends git cmake build-essential \
            pkg-config libusb-1.0-0-dev libglib2.0-dev zlib1g-dev libxml2-dev 2>&1)"; then
        printf '%s\n' "$out" | tail -8
        errs="$(printf '%s\n' "$out" | grep -E '^E:' || true)"
        die "build dependencies failed — apt: ${errs:-see the lines above}"
    fi
    ok "build dependencies installed"
}

# ── [3/7] driver fork + dumpvdl2 ─────────────────────────────
step_build() {
    step "3/7 driver fork + dumpvdl2 $DUMPVDL2_TAG"
    FORK_DIR="$(find_fork_dir)"
    if [ -z "$FORK_DIR" ]; then
        build_fork
        FORK_DIR="$(find_fork_dir)"
        [ -n "$FORK_DIR" ] || die "driver fork install missing — no librtlsdr.so under $FORK_ROOT"
    fi
    ok "driver fork: $FORK_DIR"

    if installed_current; then
        ok "dumpvdl2 $DUMPVDL2_TAG already installed and linked to the fork — not rebuilding"
        return 0
    fi

    mkdir -p "$SRC"
    export PKG_CONFIG_PATH="$FORK_DIR/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    if pkg-config --exists libacars-2; then
        ok "libacars already installed: $(pkg-config --modversion libacars-2 2>/dev/null || echo '?')"
    else
        info "building libacars $LIBACARS_TAG"
        rm -rf "$SRC/libacars"
        git clone --depth 1 --branch "$LIBACARS_TAG" https://github.com/szpajder/libacars "$SRC/libacars"
        cmake -S "$SRC/libacars" -B "$SRC/libacars/build" -DCMAKE_BUILD_TYPE=Release
        make -C "$SRC/libacars/build" -j"$J"
        sudo make -C "$SRC/libacars/build" install
        sudo ldconfig
    fi

    info "building dumpvdl2 $DUMPVDL2_TAG (~2-4 min)"
    rm -rf "$SRC/dumpvdl2"
    git clone --depth 1 --branch "$DUMPVDL2_TAG" https://github.com/szpajder/dumpvdl2 "$SRC/dumpvdl2"
    cmake -S "$SRC/dumpvdl2" -B "$SRC/dumpvdl2/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/local \
        -DCMAKE_BUILD_RPATH="$FORK_DIR" \
        -DSDRPLAY=FALSE -DSOAPYSDR=FALSE -DMIRISDR=FALSE -DZMQ=FALSE
    make -C "$SRC/dumpvdl2/build" -j"$J"

    local built=""
    built="$(find "$SRC/dumpvdl2/build" -type f -name dumpvdl2 -perm -u+x -print -quit)"
    [ -n "$built" ] || die "dumpvdl2 binary not found after the build"
    # Hard gate (the deaf-V4 lesson): stock librtlsdr = ~10 dB deaf, no error anywhere.
    local ldd_out
    ldd_out="$(ldd "$built" 2>/dev/null || true)"
    if ! links_fork "$ldd_out"; then
        printf '%s\n' "$ldd_out" | grep rtlsdr || true
        die "dumpvdl2 linked STOCK librtlsdr — it would be ~10 dB deaf. Not installing."
    fi
    sudo install -m 755 "$built" "$BIN"
    sudo mkdir -p "$(dirname "$BUILT_STAMP")"
    echo "$DUMPVDL2_TAG" | sudo tee "$BUILT_STAMP" >/dev/null
    ok "dumpvdl2 $DUMPVDL2_TAG installed to $BIN, linked to the fork"
}

# ── dongle helpers ───────────────────────────────────────────
# Every RTL2832/2838 on the bus: "serial<TAB>product", one line per STICK
# (two sticks with one serial give two lines — that is the point).
list_sticks() {
    local d dir
    for d in "$SYSFS"/*/idVendor; do
        [ -e "$d" ] || continue
        dir=${d%/idVendor}
        [ "$(cat "$d" 2>/dev/null)" = "0bda" ] || continue
        case "$(cat "$dir/idProduct" 2>/dev/null)" in 2838|2832) ;; *) continue ;; esac
        printf '%s\t%s\n' "$(cat "$dir/serial" 2>/dev/null || echo '?')" \
                          "$(cat "$dir/product" 2>/dev/null || echo '?')"
    done
}
present_serials() { list_sticks | cut -f1; }

# First "serial" value in a JSON file. sed reads the whole file: no `| head`.
json_serial() {
    [ -f "$1" ] || return 0
    local all
    all="$(sed -nE 's/.*"serial"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' "$1" 2>/dev/null || true)"
    printf '%s' "${all%%$'\n'*}"
}

# Serials the ADS-B decoder is pinned to (dump1090-fa RECEIVER_SERIAL, readsb
# --device). A bare 0-99 is a device INDEX, not a serial, and is skipped.
adsb_serials() {
    local v=""
    if [ -f "$DUMP1090_DEFAULT" ]; then
        v="$(sed -nE 's/^[[:space:]]*RECEIVER_SERIAL=["'"'"']?([^"'"'"' ]*).*/\1/p' "$DUMP1090_DEFAULT" 2>/dev/null || true)"
        v="${v%%$'\n'*}"
        case "$v" in ''|[0-9]|[0-9][0-9]) ;; *) echo "$v" ;; esac
    fi
    if [ -f "$READSB_DEFAULT" ]; then
        v="$(sed -nE 's/.*--device[[:space:]]+([^" ]+).*/\1/p' "$READSB_DEFAULT" 2>/dev/null || true)"
        v="${v%%$'\n'*}"
        case "$v" in ''|[0-9]|[0-9][0-9]) ;; *) echo "$v" ;; esac
    fi
    return 0
}

decoder_active() { systemctl is-active --quiet dump1090-fa || systemctl is-active --quiet readsb; }
# Installed at all (running or not). Captured, then matched: no `| grep -q`.
decoder_installed() {
    local out
    out="$(systemctl list-unit-files dump1090-fa.service readsb.service 2>/dev/null || true)"
    [[ "$out" == *dump1090-fa.service* || "$out" == *readsb.service* ]]
}
# A serial we are willing to put in a config, a command line or a sed: plain
# letters and digits only. A stick with no readable serial shows as "?".
serial_ok() { [[ "$1" =~ ^[0-9A-Za-z]{1,16}$ ]]; }

# First of 00000003..00000009 that no stick carries and nobody has reserved.
next_free_serial() {
    local n s
    for n in 3 4 5 6 7 8 9; do
        s="0000000$n"
        [[ $'\n'"$PRESENT"$'\n'"$RESERVED"$'\n' == *$'\n'"$s"$'\n'* ]] && continue
        echo "$s"; return 0
    done
    echo "00000042"
}

# Decide what to do with the datalink stick. Sets DL_ACTION (keep | use |
# prepare | wait | ask) and DL_SERIAL, plus CANDIDATES for "ask". No prompts.
choose_stick() {
    local existing dups s
    PRESENT="$(present_serials)"
    RESERVED="$( { adsb_serials; json_serial "$RADIO_CFG"; echo; } | grep . || true)"
    existing="$(json_serial "$CFG")"
    dups="$(printf '%s\n' "$PRESENT" | grep . | sort | uniq -d || true)"
    DL_ACTION=""; DL_SERIAL=""; CANDIDATES=(); ADSB_UNKNOWN=""
    # Unknown when the decoder is installed (running OR down: a crash loop or a
    # hand stop must not make its stick look free) and records no serial.
    if [ -z "$(adsb_serials)" ] && { decoder_active || decoder_installed; }; then ADSB_UNKNOWN=1; fi

    _is_present() { [[ $'\n'"$PRESENT"$'\n' == *$'\n'"$1"$'\n'* ]]; }
    _is_reserved() { [[ $'\n'"$RESERVED"$'\n' == *$'\n'"$1"$'\n'* ]]; }
    _is_dup() { [[ $'\n'"$dups"$'\n' == *$'\n'"$1"$'\n'* ]]; }

    # 1. already set up and its stick is here, unambiguous
    if [ -n "$existing" ] && _is_present "$existing" && ! _is_dup "$existing"; then
        DL_ACTION=keep; DL_SERIAL="$existing"; return 0
    fi
    # the serial a prepared/awaited stick will get: an earlier reservation, else the next free one
    local target
    if [ -n "$existing" ] && ! _is_present "$existing" && ! _is_reserved "$existing"; then
        target="$existing"
    else
        target="$(next_free_serial)"
    fi
    # 2. two sticks share a serial, or a stick has no usable serial: the new
    #    one must be re-serialized before anything is written anywhere
    if [ -n "$dups" ]; then
        DL_ACTION=prepare; DL_SERIAL="$target"; return 0
    fi
    while IFS= read -r s; do
        [ -n "$s" ] || continue
        serial_ok "$s" || { DL_ACTION=prepare; DL_SERIAL="$target"; return 0; }
    done <<< "$PRESENT"
    # 3. free sticks = present minus reserved (when the ADS-B serial is unknown
    #    and a decoder runs, any of them could be the ADS-B stick: ask, never guess)
    while IFS= read -r s; do
        [ -n "$s" ] || continue
        _is_reserved "$s" && continue
        CANDIDATES+=("$s")
    done <<< "$PRESENT"
    if [ -n "$ADSB_UNKNOWN" ] && [ ${#CANDIDATES[@]} -gt 0 ]; then DL_ACTION=ask; return 0; fi
    case ${#CANDIDATES[@]} in
        0) DL_ACTION="wait"; DL_SERIAL="$target" ;;
        1) DL_ACTION=use;  DL_SERIAL="${CANDIDATES[0]}" ;;
        *) DL_ACTION=ask ;;
    esac
}

# Prompt on stdout, answer from the terminal. The terminal is opened ONCE on
# fd 3, so answers come from the keyboard even when stdin is redirected, and
# are read in order.
ask() {
    if [ -z "${_TTY_OPEN:-}" ]; then exec 3<"$TTY"; _TTY_OPEN=1; fi
    printf '▶ %s ' "$1"
    REPLY_VAL=""
    IFS= read -r REPLY_VAL <&3 || return 1
}

# Write (or re-point) /etc/pilnk-datalink/config.json. This is the RESERVATION:
# sdr-recover.sh never counts this serial as an ADS-B stick once it is here.
write_config() {
    local serial="$1" cur
    serial_ok "$serial" || die "refusing to write serial '$serial' — letters and digits only"
    cur="$(json_serial "$CFG")"
    [ "$cur" = "$serial" ] && return 0
    sudo mkdir -p "$(dirname "$CFG")"
    if [ -f "$CFG" ]; then
        sudo sed -i -E "s/(\"serial\"[[:space:]]*:[[:space:]]*\")[^\"]*\"/\1$serial\"/" "$CFG"
        ok "$CFG now reserves serial $serial (was ${cur:-blank})"
    else
        printf '{"mode":"vdl2","serial":"%s","freqs_mhz":[136.975],"gain":40,"udp_port":5555}\n' "$serial" \
            | sudo tee "$CFG" >/dev/null
        ok "$CFG written — serial $serial reserved for datalink"
    fi
}

# ── guided re-serialize: the 7 Oct 2026 hub exercise, minus its traps ──
PREP_STOPPED=()
PREP_ARMED=""
# Must finish whatever happens: a dropped SSH session (printing fails) or a
# second Ctrl-C used to cut it short and leave the radio stopped until reboot.
# So: ignore signals, no set -e, do the work FIRST, print afterwards.
_prep_restore() {
    [ -n "$PREP_ARMED" ] || return 0
    PREP_ARMED=""
    local had_e="" rc=0
    case $- in *e*) had_e=1 ;; esac
    trap '' INT TERM HUP
    set +e
    if [ ${#PREP_STOPPED[@]} -gt 0 ]; then
        sudo systemctl start "${PREP_STOPPED[@]}" >/dev/null 2>&1; rc=$?
    fi
    sudo rm -f "$PAUSE_FILE" >/dev/null 2>&1
    if [ ${#PREP_STOPPED[@]} -gt 0 ]; then
        if [ "$rc" -eq 0 ]; then info "started again: ${PREP_STOPPED[*]}" 2>/dev/null
        else warn "could not start ${PREP_STOPPED[*]} — run: sudo systemctl start ${PREP_STOPPED[*]}" 2>/dev/null; fi
    fi
    trap 'exit 130' INT TERM
    trap - HUP
    [ -z "$had_e" ] || set -e
    return 0
}
# Re-arm the self-heal pause for another 30 min. Called before every wait for
# a person, so a slow owner never outlasts it (it expires on its own at most
# 30 min after we stop touching it).
_pause_refresh() { echo $(( $(date +%s) + PAUSE_SECS )) | sudo tee "$PAUSE_FILE" >/dev/null; }
_stick_count() { local n=0 s; while IFS= read -r s; do [ -n "$s" ] && n=$((n+1)); done <<< "$(present_serials)"; echo "$n"; }

prepare_stick() {
    local target="$1" u n want missing s
    echo
    info "A new RTL-SDR usually ships with serial 00000001 — the same as most ADS-B sticks."
    info "Two sticks with one serial make decoders open the wrong one, so the new stick gets serial $target."
    info "ADS-B and the ATC radio pause for a few minutes while we do it, and come back by themselves."
    trap '_prep_restore; _on_exit' EXIT
    trap 'exit 130' INT TERM
    PREP_ARMED=1
    _pause_refresh
    # a self-heal run may already be sleeping out its cooldown; end it so it
    # cannot wake up and restart a decoder onto the stick we are working on
    sudo systemctl stop pilnk-sdr-recover.service >/dev/null 2>&1 || true
    for u in dump1090-fa readsb pilnkradio pilnk-datalink; do
        if systemctl is-active --quiet "$u"; then sudo systemctl stop "$u"; PREP_STOPPED+=("$u"); fi
    done
    [ ${#PREP_STOPPED[@]} -eq 0 ] || info "paused: ${PREP_STOPPED[*]} (self-heal paused too)"

    while :; do
        _pause_refresh
        ask "Unplug every SDR stick except the NEW one (ADS-B and radio sticks too), then press Enter:" \
            || die "cancelled — nothing was written; your services are being started again"
        n="$(_stick_count)"
        [ "$n" -eq 1 ] && break
        if [ "$n" -eq 0 ]; then warn "no stick is plugged in — plug in just the new one"
        else warn "$n sticks are plugged in — leave only the new one"; fi
    done
    s="$(list_sticks)"
    ok "one stick plugged in: SN ${s%%$'\t'*} (${s#*$'\t'})"

    _pause_refresh
    printf '▶ rtl_eeprom will now ask to write serial %s to it — answer y\n' "$target"
    # rtl_eeprom exits 0 even when the answer is not y (and on end of input),
    # so its exit code proves nothing: read the EEPROM back and check.
    local readback
    rtl_eeprom -d 0 -s "$target" <&3 || true
    readback="$(rtl_eeprom -d 0 2>&1 || true)"
    [[ "$readback" =~ Serial\ number:[[:space:]]+$target([^0-9A-Za-z]|$) ]] \
        || die "serial NOT written (rtl_eeprom was answered no, or failed) — your services are being started again"

    while :; do
        _pause_refresh
        ask "Unplug the new stick and plug it back in, then press Enter:" || die "cancelled after the write — replug the stick; it is $target"
        [[ $'\n'"$(present_serials)"$'\n' == *$'\n'"$target"$'\n'* ]] && break
        warn "not seeing serial $target yet — replug the new stick"
    done
    ok "the new stick now reads serial $target"

    want="$( { adsb_serials; json_serial "$RADIO_CFG"; echo; } | grep . || true)"
    while :; do
        _pause_refresh
        ask "Plug your other sticks back in, then press Enter (or type skip):" || break
        [ "$REPLY_VAL" = skip ] && break
        missing=""
        while IFS= read -r s; do
            [ -n "$s" ] || continue
            [[ $'\n'"$(present_serials)"$'\n' == *$'\n'"$s"$'\n'* ]] || missing="$missing $s"
        done <<< "$want"
        [ -z "$missing" ] && break
        warn "still missing:$missing"
    done
    _prep_restore
    trap _on_exit EXIT
    ok "stick prepared as $target"
}

# ── [4/7] datalink stick ─────────────────────────────────────
step_dongle() {
    step "4/7 datalink stick"
    local line s p i
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        s=${line%%$'\t'*}; p=${line#*$'\t'}
        info "stick SN $s ($p)"
    done <<< "$(list_sticks)"

    choose_stick
    while [ "$DL_ACTION" = wait ] && [ -z "${PILNK_NONINTERACTIVE:-}" ]; do
        warn "no free stick found (every stick here belongs to ADS-B or the radio)"
        ask "Plug the new datalink stick in now and press Enter, or type later to finish without it:" || break
        if [ "$REPLY_VAL" = later ]; then break; fi
        sleep 2
        choose_stick
    done

    case "$DL_ACTION" in
        keep) ok "datalink stick: SN $DL_SERIAL (already set up)" ;;
        use)  ok "datalink stick: SN $DL_SERIAL" ;;
        wait)
            write_config "$DL_SERIAL"
            warn "no datalink stick yet: serial $DL_SERIAL is reserved for it."
            warn "A new stick almost always arrives as 00000001 — when it is plugged in, run this installer again to prepare it."
            return 0 ;;
        prepare)
            if [ -n "${PILNK_NONINTERACTIVE:-}" ]; then
                warn "two sticks share serial $(printf '%s\n' "$PRESENT" | grep . | sort | uniq -d | tr '\n' ' ')— run this installer by hand to re-serialize the new one. Nothing changed."
                return 0
            fi
            write_config "$DL_SERIAL"
            prepare_stick "$DL_SERIAL" ;;
        ask)
            if [ -n "${PILNK_NONINTERACTIVE:-}" ]; then
                warn "more than one possible datalink stick — run this installer by hand to choose. Nothing changed."
                return 0
            fi
            [ -z "$ADSB_UNKNOWN" ] || warn "an ADS-B decoder is running but its stick's serial is not in its config — do NOT pick the ADS-B stick"
            echo "Which stick is the datalink stick?"
            for i in "${!CANDIDATES[@]}"; do echo "  $((i+1))) ${CANDIDATES[$i]}"; done
            while :; do
                ask "Number [1-${#CANDIDATES[@]}]:" || die "cancelled — nothing changed"
                case "$REPLY_VAL" in
                    ''|*[!0-9]*) ;;
                    *) if [ "$REPLY_VAL" -ge 1 ] && [ "$REPLY_VAL" -le ${#CANDIDATES[@]} ]; then
                           DL_SERIAL="${CANDIDATES[$((REPLY_VAL-1))]}"; break; fi ;;
                esac
                warn "type a number from the list"
            done
            ok "datalink stick: SN $DL_SERIAL" ;;
    esac
    write_config "$DL_SERIAL"
}

# ── [5/7] config ─────────────────────────────────────────────
step_config() {
    step "5/7 config"
    [ -f "$CFG" ] || die "$CFG is missing — step 4 should have written it"
    sudo chmod 644 "$CFG"
    ok "$CFG (serial $(json_serial "$CFG"), 136.975 MHz unless you changed it)"
}

# ── [6/7] service ────────────────────────────────────────────
step_service() {
    step "6/7 pilnk-datalink service"
    local f
    for f in datalink-run.sh datalink-check.sh pilnk-datalink.service; do
        [ -f "$HERE/datalink/$f" ] || die "missing $HERE/datalink/$f — update the node and re-run"
    done
    sudo mkdir -p "$LIBEXEC"
    sudo install -m 755 "$HERE/datalink/datalink-run.sh" "$LIBEXEC/datalink-run.sh"
    sudo install -m 755 "$HERE/datalink/datalink-check.sh" "$LIBEXEC/datalink-check.sh"
    sudo mkdir -p "$UNIT_DIR"
    sed -e "s|PILNK_USER|$(id -un)|g" -e "s|PILNK_LIBEXEC|$LIBEXEC|g" "$HERE/datalink/pilnk-datalink.service" \
        | sudo tee "$UNIT_DIR/pilnk-datalink.service" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable pilnk-datalink >/dev/null 2>&1 || sudo systemctl enable pilnk-datalink
    # restart, not `enable --now`: on a re-run --now leaves the OLD process running.
    sudo systemctl restart pilnk-datalink
    ok "pilnk-datalink installed and started (runs as $(id -un))"
}

# ── [7/7] verify ─────────────────────────────────────────────
json_udp_port() {
    local v
    v="$(sed -nE 's/.*"udp_port"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$CFG" 2>/dev/null || true)"
    v="${v%%$'\n'*}"
    echo "${v:-5555}"
}

step_verify() {
    step "7/7 verify"
    local i st=""
    for i in $(seq 1 15); do
        [ -s "$STATUS" ] && break
        sleep 1
    done
    st="$(cat "$STATUS" 2>/dev/null || true)"
    if [ -z "$st" ]; then
        warn "no status yet — check: systemctl status pilnk-datalink"
        warn "(if the stick is not plugged in yet, that is expected: it starts by itself once it is)"
        return 0
    fi
    case "$st" in
        *'"driver_ok": true'*) ok "decoder running on the rtl-sdr-blog driver: $st" ;;
        *'"driver_ok": null'*) info "decoder still starting — look again in a minute: cat $STATUS" ;;
        *'"running": false'*)  warn "decoder not running yet: $st — see: journalctl -u pilnk-datalink -n 20" ;;
        *) warn "decoder is NOT on the rtl-sdr-blog driver (it would be ~10 dB deaf): $st" ;;
    esac
    [ "$VERIFY_SECS" -gt 0 ] || return 0
    info "listening for ${VERIFY_SECS}s of VDL2 messages (quiet at night is normal)…"
    python3 - "$(json_udp_port)" "$VERIFY_SECS" <<'PY' || true
import socket, sys, time
port, secs = int(sys.argv[1]), int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    print(f"port {port} is already taken (the dashboard is listening) — check the Datalink card instead")
    sys.exit(0)
s.settimeout(1)
n, end = 0, time.time() + secs
while time.time() < end:
    try:
        s.recv(65535); n += 1
    except socket.timeout:
        pass
print(f"VDL2 messages in {secs}s: {n}")
PY
}

main() {
    trap _on_exit EXIT
    trap 'exit 130' INT TERM
    step_checks
    step_deps
    step_build
    step_dongle
    step_config
    step_service
    step_verify
    printf "\n%b%s%b\n" "$BOLD$GREEN" "════════ DATALINK INSTALL COMPLETE ════════" "$RESET"
    echo "Messages stay on this node. The dashboard card comes in the next release."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
