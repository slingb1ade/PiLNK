#!/usr/bin/env bash
# PiLNK macOS build check — one component per call.
# Used only by .github/workflows/macos-build.yml (branch mac-port).
# It builds; it never installs anything onto a node and never runs a radio.
set -euo pipefail

COMPONENT="${1:?usage: build.sh <pilnkradio|dump1090-fa|readsb|python>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${RUNNER_TEMP:-/tmp}/pilnk-mac"
RTL_PREFIX="$WORK/rtlsdr-blog"
mkdir -p "$WORK"

BREW="$(brew --prefix)"
export CPATH="$BREW/include${CPATH:+:$CPATH}"
export LIBRARY_PATH="$BREW/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
export PKG_CONFIG_PATH="$RTL_PREFIX/lib/pkgconfig:$BREW/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
JOBS="$(sysctl -n hw.ncpu)"

summary() { echo "$*" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"; echo "$*"; }

deps() {
  for f in libusb fftw; do brew list "$f" >/dev/null 2>&1 || brew install "$f"; done
  command -v cmake >/dev/null || brew install cmake
  command -v pkg-config >/dev/null || brew install pkgconf
}

# The rtl-sdr-blog fork is what the fleet uses (stock librtlsdr leaves V4 dongles ~10 dB deaf).
rtlsdr_blog() {
  [ -f "$RTL_PREFIX/lib/pkgconfig/librtlsdr.pc" ] && return 0
  git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog.git "$WORK/rtl-sdr-blog"
  cmake -S "$WORK/rtl-sdr-blog" -B "$WORK/rtl-build" \
        -DCMAKE_INSTALL_PREFIX="$RTL_PREFIX" \
        -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=OFF
  cmake --build "$WORK/rtl-build" -j "$JOBS"
  cmake --install "$WORK/rtl-build"
  summary "- rtl-sdr-blog fork: built OK"
}

case "$COMPONENT" in
  pilnkradio)
    deps; rtlsdr_blog
    cmake -S "$ROOT/pilnkradio" -B "$WORK/radio-build" \
          -DRTLSDR_PREFIX="$RTL_PREFIX" -DCMAKE_PREFIX_PATH="$BREW"
    cmake --build "$WORK/radio-build" -j "$JOBS"
    file "$WORK/radio-build/pilnkradio"
    summary "- pilnkradio: built OK ($(uname -m))"
    ;;
  dump1090-fa)
    deps; rtlsdr_blog
    git clone --depth 1 https://github.com/flightaware/dump1090.git "$WORK/dump1090"
    make -C "$WORK/dump1090" -j "$JOBS" RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no SOAPYSDR=no
    "$WORK/dump1090/dump1090" --help >/dev/null 2>&1 || true
    summary "- dump1090-fa: built OK ($(uname -m))"
    ;;
  readsb)
    deps; rtlsdr_blog
    git clone --depth 1 https://github.com/wiedehopf/readsb.git "$WORK/readsb"
    make -C "$WORK/readsb" -j "$JOBS" RTLSDR=yes
    summary "- readsb: built OK ($(uname -m))"
    ;;
  python)
    python3 -m pip install --quiet flask flask-socketio requests psutil
    cd "$ROOT"
    python3 -m py_compile ./*.py
    python3 -c "import flask, flask_socketio, requests, psutil; print('deps import OK')"
    summary "- python: $(ls ./*.py | wc -l | tr -d ' ') files compile, deps import OK"
    ;;
  *) echo "unknown component: $COMPONENT" >&2; exit 2 ;;
esac
