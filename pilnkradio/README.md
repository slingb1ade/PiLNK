# pilnkradio — PiLNK v2 radio engine

One standalone daemon replacing the entire v1 SDR++ train (SDR++ GUI + Xvfb +
x11vnc + websockify + pilnk_bridge module). Serves the identical `:5656`
HTTP/WS wire contract, so the dashboard SDR Audio tab works unchanged.

```
V4 dongle ── librtlsdr (rtl-sdr-blog fork) ── 2.4 MS/s IQ
  ├── 1024-pt FFT @ 25 fps ──────────────────► WS /sdr/fft
  └── NCO −950 kHz → ÷50 → 48 kHz → LPF(bw/2)
        → squelch → AM env → DC block → AGC ──► WS /sdr/audio (f32 mono 48k)
Control: GET /sdr/status · POST /sdr/{frequency,mode,bandwidth,playing,
                                      squelch,gain,agc}
```

## Local unsquelched tap (engine 2.1.3)

`ws://127.0.0.1:5656/sdr/audio_raw` is for programs on the node itself (the ATC
STT), not for browsers. Same frames as `/sdr/audio` (binary float32 LE, mono,
48 kHz), taken after the AGC and **before** the squelch gate and denoise.

- **Local only.** Loopback peers, no browser `Origin` (a loopback one is fine), no
  proxy headers (`X-Forwarded-For`, `Forwarded`, `Via`, ...). Anything else gets 403.
  Captured ATC audio never leaves the node.
- **Gap-free or closed.** Each reader has its own writer and queue. A reader about
  4 s behind is closed, never skipped. If the radio thread stalls longer than the
  dongle can buffer (150 ms), every raw reader is closed, because IQ may be lost.
  Stopping the radio closes them too. While the radio is stopped, new readers get 503.
- **Squelch edges as text frames.** `{"sq":0|1,"n":0}` comes first (the state at the
  reader's sample 0), then `{"sq":..,"n":N}` at each edge. N counts samples since
  that reader's first sample. `sq` is exactly the gate `/sdr/audio` applies. With
  the squelch disabled it stays 1.
- It keeps streaming through retunes. At most 4 readers (`rawTap` in `/sdr/status`).

## THE driver lesson (do not skip)

The RTL-SDR **Blog V4 requires the rtl-sdr-blog librtlsdr fork**. With the
stock Debian librtlsdr it *appears to work* but runs **~10 dB deaf** with
bogus gain/ppm behavior (cost this project a full day, 2026-07-09). The
CMake build links the fork explicitly with an rpath and **warns loudly** if
it falls back to the stock lib. At startup the daemon logs which library the
loader actually mapped:

    driver: loaded /usr/local/lib/librtlsdr.so.0.6git   ← fork = correct

Check that line after **every** install. If it shows
`/lib/.../librtlsdr.so.0`, the node is deaf — rebuild against the fork.

## Install (fleet nodes — the easy way)

One command on any PiLNK node (v1.3.0+). Builds everything, self-tests the
DSP with no antenna, and walks you through dongle + frequency:

```bash
curl -s https://raw.githubusercontent.com/slingb1ade/PiLNK/main/pilnkradio-install.sh | bash
```

The manual steps below are what the script automates.

## Build

```bash
# 1. the driver fork (once per node)
git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog
cmake -B rtl-sdr-blog/build rtl-sdr-blog -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON
make -C rtl-sdr-blog/build -j4 && sudo make -C rtl-sdr-blog/build install && sudo ldconfig

# 2. the daemon
cmake -B build . -DRTLSDR_PREFIX=/usr/local
make -C build -j4
build/pilnkradio --selftest        # validates the DSP chain, no antenna needed
sudo make -C build install
```

## Install

```bash
sudo mkdir -p /etc/pilnkradio
sudo cp config.example.json /etc/pilnkradio/config.json   # then edit: serial, ppm, allowedOrigins
sudo cp pilnkradio.service /etc/systemd/system/
sudo cp 99-pilnk-v4.rules /etc/udev/rules.d/ && sudo udevadm control --reload
sudo systemctl daemon-reload && sudo systemctl enable --now pilnkradio
journalctl -u pilnkradio -n 20     # verify the "driver: loaded ..." line
```

## Config notes

- `playing` is **operator consent** (Local Laws): off by default, persisted
  across restarts, only ever changed by an operator action. The daemon
  resumes playback after a crash only if consent was on.
- `allowedOrigins`: list of dashboard origins allowed to control the radio
  from a browser (e.g. `["http://192.168.50.22:5000"]`). Requests with no
  Origin header (curl, watchdog) and localhost origins are always allowed;
  anything else is 403. Closes the CSRF/DNS-rebinding hole (audit M3).
- `token`: optional shared secret; when set, POSTs must carry
  `X-PiLNK-Token`.
- `ppm` is per-crystal: measure per dongle (V4 `00000002` = −7).
- `serial`: the daemon opens ONLY this serial and exits if absent — it will
  never grab the ADS-B stick. Note V4s can share factory serials across
  nodes; pinning is per-host.

## Self-healing model

| Failure | What happens |
|---|---|
| Dongle absent at start | exit 1 → systemd retries every 5 s |
| USB drop mid-stream | async loop ends → exit 2 → systemd retries |
| Dongle replugged | udev rule restarts the service immediately |
| Port 5656 taken | exit 1 (refuses to fight sdrpp/another instance) |
| Stuck WS client | 250 ms send timeout → client evicted (audit H2) |
| Slowloris / flood | 15 s recv timeout + 64-conn cap (audit M1) |
| Oversized POST | 413 at 64 KB (audit M2) |

`fftFps`/`audioSps` in `/sdr/status` remain for the tab's stall banner and
any external watchdog, but the primary self-heal is process exit + systemd.
