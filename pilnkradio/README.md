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

## Squelch modes (engine 2.2.0)

| `squelchEnabled` | `squelchAuto` | Mode | Gate |
|---|---|---|---|
| false | any | off | always open |
| true | false | fixed | exactly as 2.1.5: muted while the channel power is under `squelchLevel`. No hysteresis, no hang. |
| true | true | auto | opens on the learned noise floor + `squelchMarginDb`, with hysteresis and a hang |

- **Noise floor (every mode).** One reading of the smoothed channel power per 4800
  samples (100 ms). The floor is the 15th percentile of the last 300 readings (30 s),
  valid from 50 readings (5 s). It is shown as `noiseFloorDb` (null until valid). A
  retune, bandwidth, gain or AGC change, or the radio starting, clears it. Switching
  Auto on or off does not.
- **Auto gate.** Opens at `T_open = floor + margin` (margin 3–20 dB, default 6),
  clamped to −100..0 dBFS. Until the floor is valid (warm-up) `T_open = squelchLevel`.
  It closes once the smoothed channel power has stayed more than 3 dB under the
  opening level (`SQ_HYST_DB`) for 0.5 s (`SQ_HANG_SAMPLES` = 24000). The smoothing
  adds its own decay first: measured on the fake, Auto closes about 1.35 s after a
  strong carrier drops, against about 0.8 s for fixed mode set 5 dB over the floor.
  `squelchThresholdDb` in `/sdr/status` is `T_open` (the level in fixed mode, null
  when off). `squelchOpen` is the gate.
- **A transmission never moves the floor.** While the auto gate is open, readings are
  kept out of the window. Warm-up is exempt: until the floor is valid every reading
  goes in, so a stored level under the noise (which opens on hiss) cannot keep the
  floor from ever being learned. The other exception is the stuck-open release: if
  the gate stays open 60 s without a break (`SQ_STUCK_SAMPLES` = 2,880,000, hang
  included), readings go back in, so the floor catches up with a raised noise level
  (about 25 s) and the gate closes.
- **Edges.** Gate changes fall on a block's first sample, as before. The raw tap's
  `sq` frames carry them unchanged.
- **ATIS uses fixed.** A channel that carries a signal all the time looks like a stuck
  gate to Auto: the release learns the signal as the floor and mutes it after about a
  minute and a half. Keep ATIS frequencies on a fixed level.

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
| Dongle absent at start | port bound first, engine waits for the dongle in the background (#114); `/sdr/status` shows no gain steps |
| USB drop mid-stream | async loop ends → exit 2 → systemd retries |
| Playing but no samples for 10 s (hung USB, "up but deaf") | exit 3 → systemd retries (2.1.5) |
| Dongle replugged | udev rule restarts the service immediately |
| Port 5656 taken | exit 1 (refuses to fight sdrpp/another instance) |
| Stuck WS client | non-blocking send; a full socket is evicted at once, nobody else waits (2.1.4) |
| Slowloris / flood | 15 s recv timeout + 64-conn cap (audit M1) |
| Oversized POST | 413 at 64 KB (audit M2) |
| Malformed or out-of-range control request | 400, nothing applied, nothing saved (2.1.5) |
| Retune the tuner refuses | old frequency kept, in the radio, status and config (2.1.5) |
| Config write fails (full disk) | old config left untouched, "config: save failed" logged (2.1.5) |
| Interference lifts the floor (Auto) | gate closes after ≤ ~90 s of hiss (2.2.0) |

Control value limits (2.1.5): frequency 24–1766 MHz, bandwidth 1–50 kHz, squelch
−100–0 dBFS, gain a whole-number index into the tuner's own steps, booleans as JSON
`true`/`false`. A request is applied whole or not at all.

`fftFps`/`audioSps` in `/sdr/status` remain for the tab's stall banner and
any external watchdog, but the primary self-heal is process exit + systemd.
