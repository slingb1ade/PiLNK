# PiLNK on macOS — Portability Map

Read-only survey of the public repo at **v1.6.20 (5670a2d)**, 3 Nov 2026. Nothing was changed anywhere.
Verdicts: **PORT** = works with small edits · **REPLACE** = needs a macOS equivalent · **SKIP** = leave out of the Mac node (at least at first).

## Headline

The hard part is smaller than expected. The C++ radio engine is ~3 lines away from building on a Mac, and the dashboard (`app.py`) only reads an `aircraft.json` path from config, so it doesn't care which decoder writes it. Most of the work is **plumbing**: services, installer, OTA restart, and the Linux-only self-heal/diagnostics.

## 1. Radio engine — `pilnkradio/main.cpp` (C++) — **PORT (easy)**

Deps: librtlsdr + FFTW3f + pthreads + dlopen. All available on macOS via Homebrew (`librtlsdr`, `fftw`).

Only three Linux-isms found:

| Line | What | Mac fix |
|---|---|---|
| 217 | `logLoadedRtlsdr()` reads `/proc/self/maps` to log which librtlsdr loaded | Use `dladdr()` on an rtlsdr symbol, or skip the log line on Apple (`#ifdef __APPLE__`) |
| 1380, 1397 | `send(..., MSG_NOSIGNAL)` | macOS has no `MSG_NOSIGNAL`: set `SO_NOSIGPIPE` on the socket once, pass flag 0 |
| CMake | `RTLSDR_PREFIX` defaults to `/usr/local`; rpath | Point at Homebrew prefix (`/opt/homebrew` on Apple Silicon) |

Open question: the **rtl-sdr-blog fork** (needed so V4 dongles aren't ~10 dB deaf) must build on macOS. It's plain CMake + libusb, so likely fine — CI will prove it.

`pilnk_bridge/` — FFTW only via pkg-config → **PORT**, probably builds as-is.

## 2. Dashboard — `app.py` (Flask, ~7,000 lines) — **PORT, with patches**

Pure Python (Flask, flask-socketio, requests, psutil) — all fine on macOS. Linux-specific spots:

| Area | Lines | Verdict |
|---|---|---|
| Pi model from `/proc/device-tree/model`, OS from `/etc/os-release` | 310, 321, 1129 | **PORT** — already inside try/except; add `sw_vers` / `sysctl hw.model` for Mac |
| Receiver location from `/etc/default/dump1090-fa`, `/etc/piaware.conf` | 414, 428 | **PORT** — fallbacks only; Mac uses config.json / the pilnk.io pin |
| `aircraft_json_path` default `/run/dump1090-fa/aircraft.json` | 488 | **PORT** — already configurable; Mac sets its own path |
| Radio config `/etc/pilnkradio/config.json` | 3743 | **PORT** — move to `$(brew --prefix)/etc` or `~/Library/Application Support/PiLNK` |
| System tab: `systemctl show` health | 4189–4199 | **REPLACE** — `launchctl print gui/$UID/io.pilnk.*` |
| CPU temp `/sys/class/thermal/...` | 4253 | **SKIP** — psutil has no Mac temps; show "n/a" |
| OTA restart / reboot: `sudo -n systemctl restart pilnk`, `systemctl reboot` | 2082–2193, 6598–6665 | **REPLACE** — `launchctl kickstart -k`; reboot = skip |
| Remote Assist reads: `journalctl`, `lsusb`, `vcgencmd`, `dmesg`, `ip`, modprobe blacklist | 5860–5958 | **REPLACE** each: `log show`, `system_profiler SPUSBDataType`, n/a, n/a, `ifconfig`, n/a |

Suggested shape: one small `platform_mac.py` / `platform_linux.py` shim so the ~15 call sites ask "restart me", "service status", "usb list" instead of shelling out to systemd directly. Linux behaviour stays byte-for-byte.

## 3. ADS-B decoder — **PORT (to verify)**

The installer prefers **readsb** (wiedehopf) and will consume an existing **dump1090-fa**. dump1090-fa has macOS compatibility code; readsb on macOS is unconfirmed. Plan: try both in CI, ship whichever builds cleanly. The Mac node just needs *something* writing `aircraft.json`.

## 4. Services — 7 systemd units + 1 timer — **REPLACE**

`pilnk.service`, `pilnkradio.service`, `pilnk-audio-build`, `pilnk-dashboard-watchdog`, `pilnk-radio-watchdog`, `pilnk-sdr-recover` (+ timer), `pilnk-datalink`.

Phase 1 needs just three LaunchAgent plists: **pilnk** (app.py), **decoder**, **pilnkradio** — with `KeepAlive` doing what `Restart=on-failure` and the watchdogs do today.
`check-dongles.sh` (ExecStartPre wait-for-dongles) → small wrapper script in the plist.

## 5. Installer + OTA — `install.sh` (832 lines), `update.sh` — **REPLACE**

apt-get, Debian version checks, kernel-module blacklist (`/etc/modprobe.d`), udev rules — none of it applies. A separate **`install-mac.sh`**: Homebrew deps → build engine + decoder → write plists → `launchctl bootstrap`. `update.sh` stays mostly the same (git pull) with a Mac restart branch.
Note: macOS needs **no driver blacklist** — the DVB kernel driver that grabs dongles on Linux doesn't exist there.

## 6. Self-heal + dongle recovery — **SKIP (phase 1)**

`sdr-recover.sh` (421 lines), `bootstrap-selfheal.sh`, `99-pilnk-sdr.rules`, `99-pilnk-v4.rules`: udev serial pinning, USB resets, sudoers grants. All Linux internals. launchd `KeepAlive` covers "process died"; true USB-reset recovery waits until we see how dongles misbehave on a real Mac.

## 7. Extras — **SKIP for now**

- **Datalink** (`dumpvdl2`, `datalink-install.sh`, 580 lines): later; dumpvdl2 may build on macOS but it's a phase-3 job.
- **Denoise** (DeepFilterNet libdf): we ship prebuilt `.so` for aarch64/armhf/x86_64 only. Mac needs a `.dylib` built with `tools/build-libdf.sh` (Rust) — engine already loads it optionally via dlopen, so it simply runs without denoise until then.
- **whisper_atc.py / STT**: out of scope for the node port.

## Phasing

1. **CI first (no Mac needed):** GitHub Actions on a free Apple Silicon macOS runner (public repo) — build pilnkradio, pilnk_bridge, rtl-sdr-blog, readsb/dump1090-fa. Answers "does it build?" for arm64.
2. **Phase 1 on the test Mac:** decoder + app.py + ping under launchd, one ADS-B dongle. Node appears on the network map.
3. **Phase 2:** pilnkradio + second dongle → ATC audio in the browser.
4. **Phase 3:** OTA restart, Remote Assist Mac branch, denoise dylib, maybe datalink.

## Test-Mac note

A cheap **Intel** Mac mini is fine for phases 1–3 (launchd, Homebrew, USB). Prefer a **2018** (runs macOS Sequoia, security updates to about Sept 2027); older minis are stuck on older macOS where Homebrew support is thin. Intel ≠ Apple Silicon at the binary level — CI covers the arm64 builds.
