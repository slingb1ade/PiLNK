#!/usr/bin/env bash
# ⚠ SUPERSEDED (2026-07-20): this installs the RETIRED v1 engine (SDR++ +
# pilnk_bridge, 30-60 min build). Fleet nodes should use pilnkradio-install.sh
# in the repo root instead — it installs the v2 pilnkradio daemon in minutes.
# Kept only as a reference for dev-bench SDR++ builds.
echo "⚠ SUPERSEDED: use pilnkradio-install.sh (v2 engine) instead." >&2
echo "  Run this v1 SDR++ script only if you know you want the dev-bench build." >&2
read -r -p "Continue with the OLD v1 install anyway? [y/N] " _a </dev/tty || _a=n
[ "${_a,,}" = "y" ] || exit 1
# phase2-engine-install.sh — SDR++ + pilnk_bridge engine onto Pi5 (EpsomPi)
# Mirrors the proven Pi4 build exactly (SDR++ 36ea9a1 + bridge v0.5.1).
# Run:  bash ~/pilnk/phase2-engine-install.sh 2>&1 | tee ~/phase2.log
# Stages are idempotent — safe to re-run if interrupted.
set -euo pipefail
J=$(nproc)

echo "=== [1/6] deps ==="
sudo apt update
sudo apt install -y build-essential cmake git pkg-config libfftw3-dev \
  libglfw3-dev libglew-dev libvolk2-dev libzstd-dev librtaudio-dev \
  libusb-1.0-0-dev xvfb libairspy-dev libhackrf-dev

echo "=== [2/6] rtl-sdr-blog driver ==="
if [ ! -d ~/rtl-sdr-blog ]; then
  git clone https://github.com/rtlsdrblog/rtl-sdr-blog ~/rtl-sdr-blog
fi
cd ~/rtl-sdr-blog && mkdir -p build && cd build
cmake .. -DINSTALL_UDEV_RULES=ON >/dev/null
make -j"$J" >/dev/null
sudo make install >/dev/null && sudo ldconfig
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null

echo "=== [3/6] sources: SDR++ @ 36ea9a1 + bridge from bridge-rescue ==="
if [ ! -d ~/SDRPlusPlus ]; then
  git clone https://github.com/AlexandreRouma/SDRPlusPlus.git ~/SDRPlusPlus
fi
cd ~/SDRPlusPlus && git fetch origin && git checkout -q 36ea9a143422f5b374371461667ff53fb9387300
cd ~/pilnk
git fetch origin bridge-rescue
git checkout origin/bridge-rescue -- pilnk_bridge/
ln -sfn /home/aj/pilnk/pilnk_bridge /home/aj/SDRPlusPlus/misc_modules/pilnk_bridge

echo "=== [4/6] patch SDR++ tree (idempotent) ==="
cd ~/SDRPlusPlus
grep -q PILNK_BRIDGE CMakeLists.txt || cat >> CMakeLists.txt <<'EOF'

option(OPT_BUILD_PILNK_BRIDGE "PiLNK native bridge (FFT + audio + control export)" ON)
if (OPT_BUILD_PILNK_BRIDGE)
add_subdirectory("misc_modules/pilnk_bridge")
endif (OPT_BUILD_PILNK_BRIDGE)
EOF
RTL=source_modules/rtl_sdr_source/src/main.cpp
grep -q 'PiLNK: keep combo' "$RTL" || sed -i \
  's|selectedDevName = devNames\[id\];|selectedDevName = devNames[id];\n        devId = id; // PiLNK: keep combo/play index in sync with name-based selection|' "$RTL"
grep -q 'PiLNK: keep combo' "$RTL" && echo "selectById patch OK"

echo "=== [5/6] build + install (30-60 min on Pi5) ==="
mkdir -p ~/SDRPlusPlus/build && cd ~/SDRPlusPlus/build
cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
  -DOPT_BUILD_PILNK_BRIDGE=ON -DOPT_BUILD_NOISE_REDUCTION_LOGMMSE=ON \
  -DOPT_BUILD_AIRSPYHF_SOURCE=OFF -DOPT_BUILD_BLADERF_SOURCE=OFF \
  -DOPT_BUILD_SDRPLAY_SOURCE=OFF -DOPT_BUILD_SOAPY_SOURCE=OFF \
  -DOPT_BUILD_LIMESDR_SOURCE=OFF -DOPT_BUILD_M17_DECODER=OFF \
  -DOPT_BUILD_SCHEDULER=OFF \
  -DOPT_BUILD_PLUTOSDR_SOURCE=OFF -DOPT_BUILD_PERSEUS_SOURCE=OFF \
  -DOPT_BUILD_USRP_SOURCE=OFF -DOPT_BUILD_RFNM_SOURCE=OFF \
  -DOPT_BUILD_FOBOSSDR_SOURCE=OFF -DOPT_BUILD_HAROGIC_SOURCE=OFF \
  -DOPT_BUILD_KCSDR_SOURCE=OFF -DOPT_BUILD_HYDRASDR_SOURCE=OFF \
  -DOPT_BUILD_BADGESDR_SOURCE=OFF -DOPT_BUILD_DRAGONLABS_SOURCE=OFF \
  -DOPT_BUILD_SPECTRAN_SOURCE=OFF -DOPT_BUILD_DAB_DECODER=OFF \
  -DOPT_BUILD_FALCON9_DECODER=OFF -DOPT_BUILD_KG_SSTV_DECODER=OFF \
  -DOPT_BUILD_RYFI_DECODER=OFF -DOPT_BUILD_VOR_RECEIVER=OFF \
  -DOPT_BUILD_WEATHER_SAT_DECODER=OFF -DOPT_BUILD_NEW_PORTAUDIO_SINK=OFF \
  -DOPT_BUILD_PORTAUDIO_SINK=OFF
make -j"$J"
sudo make install && sudo ldconfig
ls -la /usr/lib/sdrpp/plugins/pilnk_bridge.so && echo "BRIDGE MODULE INSTALLED"

echo "=== [6/6] tuned config from Pi4 + services ==="
mkdir -p ~/.config
scp -r aj@192.168.50.18:~/.config/sdrpp ~/.config/ || echo "!! scp failed — copy config manually"

sudo tee /etc/systemd/system/sdrpp-xvfb.service >/dev/null <<'EOF'
[Unit]
Description=SDR++ virtual display (Xvfb :1) [PiLNK]
After=network.target

[Service]
User=aj
ExecStart=/usr/bin/Xvfb :1 -screen 0 1360x768x24 +extension GLX +render -noreset
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sdrpp.service >/dev/null <<'EOF'
[Unit]
Description=SDR++ headless SDR (PiLNK radio front-end)
Requires=sdrpp-xvfb.service
After=sdrpp-xvfb.service

[Service]
User=aj
Environment=HOME=/home/aj
Environment=DISPLAY=:1
Environment=LIBGL_ALWAYS_SOFTWARE=1
Environment=GALLIUM_DRIVER=llvmpipe
# M5 (audit 2026-07-09): the RTL-SDR Blog V4 REQUIRES the rtl-sdr-blog librtlsdr
# fork (installed to /usr/local/lib in step [2/6]). Without this line SDR++ links
# the stock Debian librtlsdr and the V4 is ~10 dB DESENSITIZED with NO error
# anywhere — the node ships deaf. This env forces the fork. Cost 2 days to find.
Environment=LD_LIBRARY_PATH=/usr/local/lib
ExecStartPre=/bin/sleep 2
ExecStart=/usr/bin/sdrpp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo ""
echo "================ BUILD DONE ================"
echo "NEXT (by hand):"
echo "  1. rtl_test -t          # note the FREE dongle's serial (busy one = ADS-B)"
echo "  2. edit ~/.config/sdrpp/config.json — set rtl_sdr device to that serial"
echo "  3. sudo systemctl enable --now sdrpp-xvfb sdrpp"
echo "  4. curl -s localhost:5656/sdr/status   # bridge alive?"
echo "  4b. VERIFY THE V4 DRIVER (critical):"
echo "      grep librtlsdr /proc/\$(pidof sdrpp)/maps | head -1"
echo "      MUST show /usr/local/lib/librtlsdr...  (stock /lib path = deaf V4, ~10dB down)"
echo "  5. dashboard -> SDR Audio -> should CONNECT"
