#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  tools/build-libdf.sh — build the DeepFilterNet C library (libdf.so) that
#  the radio engine loads for live denoise. Not run by nodes — the built file
#  is committed under denoise/<arch>/ and shipped by OTA.
#
#    bash build-libdf.sh            native build (run on the machine type you
#                                   are building for, e.g. a 64-bit Pi)
#    bash build-libdf.sh armhf      32-bit ARM, cross-built on a 64-bit Pi —
#                                   for the PiAware SD image, which runs a
#                                   32-bit userland on a 64-bit kernel
#
#  4 Oct 2026: the aarch64 copy only ever existed on the old Pi 5 and was
#  lost in the hardware swap; then MME1's PiAware node (32-bit userland,
#  `uname -m` still says aarch64) could not load the 64-bit one (probe code 10).
#  This script means every build can always be made again.
#
#  Output: ~/libdf-build/libdf.so (native) or ~/libdf-build/libdf-armhf.so
#  Takes ~15-30 min the first time on a Pi 5. Needs ~3 GB free disk.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

TAG="${DFN_TAG:-v0.5.6}"
WORK="$HOME/libdf-build"
WANT="${1:-native}"

case "$WANT" in
    native) TARGET=""; LABEL="$(uname -m)"; NM=nm; OUTNAME=libdf.so ;;
    armhf)  TARGET="armv7-unknown-linux-gnueabihf"; LABEL=armhf; NM=arm-linux-gnueabihf-nm; OUTNAME=libdf-armhf.so ;;
    *) echo "usage: $0 [native|armhf]"; exit 2 ;;
esac
echo "== building libdf.so for $LABEL from DeepFilterNet $TAG"

# 16 KB-page kernels (Pi 5) SIGBUS on libraries linked for 4 KB pages.
# Linking for 64 KB pages runs on 4 KB, 16 KB and 64 KB kernels alike.
export RUSTFLAGS="${RUSTFLAGS:-} -C link-arg=-Wl,-z,max-page-size=65536"

# Always the rustup toolchain, never the distro's. Debian bookworm ships
# rustc 1.63, and the tract crates need 1.65+ (found the hard way, 4 Oct).
# rustup lives in ~/.cargo and does not touch the apt-installed one.
if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
    echo "== installing Rust (user-level, rustup; the distro's is too old)"
    curl -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh
    RUSTUP_INIT_SKIP_PATH_CHECK=yes sh /tmp/rustup-init.sh -y --profile minimal
fi
export PATH="$HOME/.cargo/bin:$PATH"
"$HOME/.cargo/bin/rustup" update stable >/dev/null 2>&1 || true
cargo --version
rustc --version

for p in gcc pkg-config git; do
    command -v "$p" >/dev/null 2>&1 || { echo "== installing build tools"; sudo apt-get install -y build-essential pkg-config git; break; }
done

TARGET_ARGS=()
OUTDIR="release"
if [ -n "$TARGET" ]; then
    # Cross linker + 32-bit C library headers (Debian's cross packages).
    if ! command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1; then
        echo "== installing the 32-bit ARM cross compiler"
        sudo apt-get install -y gcc-arm-linux-gnueabihf binutils-arm-linux-gnueabihf
    fi
    rustup target add "$TARGET"
    export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABIHF_LINKER=arm-linux-gnueabihf-gcc
    export CC_armv7_unknown_linux_gnueabihf=arm-linux-gnueabihf-gcc
    TARGET_ARGS=(--target "$TARGET")
    OUTDIR="$TARGET/release"
fi

mkdir -p "$WORK"
if [ ! -d "$WORK/src/.git" ]; then
    git clone --depth 1 --branch "$TAG" https://github.com/Rikorose/DeepFilterNet "$WORK/src"
fi
cd "$WORK/src"
# v0.5.6's Cargo.lock pins time 0.3.28, which does not compile on Rust 1.80+
# ("type annotations needed for Box<_>"). 0.3.36 is the fixed release and is
# API-compatible. Idempotent: re-running with it already pinned is a no-op.
cargo update -p time --precise 0.3.36
# v0.5.6 declares no crate-type, so a plain `cargo build` makes only an rlib.
# Ask for the shared library explicitly (later releases list cdylib themselves).
cargo rustc --release "${TARGET_ARGS[@]}" -p deep_filter --lib --features capi --crate-type cdylib

OUT="$WORK/src/target/$OUTDIR/libdf.so"
[ -f "$OUT" ] || { echo "!! build finished but $OUT is missing"; exit 1; }
cp "$OUT" "$WORK/$OUTNAME"

echo "== checking the C API the engine needs"
MISSING=0
SYMS="$("$NM" -D --defined-only "$WORK/$OUTNAME" 2>/dev/null || true)"
for s in df_create df_get_frame_length df_process_frame df_free; do
    if [[ "$SYMS" == *" $s"* ]]; then echo "   ok  $s"; else echo "   MISSING $s"; MISSING=1; fi
done
[ "$MISSING" -eq 0 ] || { echo "!! library lacks the df_* C API"; exit 1; }

file "$WORK/$OUTNAME"
sha256sum "$WORK/$OUTNAME"
echo "== done: $WORK/$OUTNAME"
