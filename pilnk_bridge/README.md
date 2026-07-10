# pilnk_bridge

Custom **SDR++ module** (GPL-3.0, derivative of SDR++) that is the native engine
behind PiLNK's dashboard radio tab. Runs inside a headless (Xvfb) SDR++ and exports
everything the dashboard needs over **one port (5656)**: a live FFT stream, live
audio, and an HTTP/WebSocket control API. SDR++'s own GUI is never shown.

Built and verified headless on Pi4 (2026-06-22). Full build report:
`~/Downloads/PiLNK/2026-06-22-pilnk-bridge-build-report.md`.

## Endpoint contract (bind `0.0.0.0:5656`, CORS `*`)

**WS `/sdr/fft`** — binary, little-endian, ~25 fps:
```
float64 centerHz | float64 spanHz | uint32 nBins(=1024) | float32[nBins] dB
```
`centerHz` = SDR/waterfall centre (not the VFO). Bins are fftshifted ascending:
bin 0 = `centerHz - spanHz/2`, bin N-1 = `centerHz + spanHz/2`.

**WS `/sdr/audio`** — binary, little-endian: raw `float32[]` mono PCM at
`audioSampleRate` (48000). Raw demod level — peaks can exceed 1.0; apply gain/AGC
in WebAudio.

**HTTP control (JSON)** — `{"ok":true}` on success:
- `POST /sdr/frequency` `{"hz":124300000}`
- `POST /sdr/mode` `{"mode":"AM"}`  (NFM|WFM|AM|DSB|USB|CW|LSB|RAW)
- `POST /sdr/bandwidth` `{"hz":12500}`
- `POST /sdr/playing` `{"on":true}`
- `GET /sdr/status` → `{centerHz,vfoHz,mode,bandwidthHz,playing,audioSampleRate}`

FFT/audio only flow while `playing:true`.

- **VFO controlled:** `"Radio"`  ·  **Audio sink registered:** `"PiLNK"`

## Building into SDR++ (Pi4)

This folder is the **canonical source**. The SDR++ build tree symlinks to it so there
is one source of truth:

```sh
ln -s /home/aj/pilnk/pilnk_bridge /home/aj/SDRPlusPlus/misc_modules/pilnk_bridge
```

Then in SDR++'s **top-level `CMakeLists.txt`** add (mirroring the other modules):

```cmake
option(OPT_BUILD_PILNK_BRIDGE "PiLNK native bridge (FFT + audio + control export)" ON)
# ...
if (OPT_BUILD_PILNK_BRIDGE)
add_subdirectory("misc_modules/pilnk_bridge")
endif (OPT_BUILD_PILNK_BRIDGE)
```

Build (`cmake .. && make pilnk_bridge`), copy `pilnk_bridge.so` into the SDR++
plugins dir, and add an instance to `~/.config/sdrpp/config.json`:

```json
"moduleInstances": { "PiLNK Bridge": { "enabled": true, "module": "pilnk_bridge" } }
```

Deps: `fftw3f` (single-precision FFTW). The module otherwise links only SDR++ core.

## Design notes
- Self-contained HTTP+WS server on POSIX sockets (no external deps); server→client
  WS frames only; minimal vendored SHA-1/base64 for the WS handshake.
- Control is threadsafe: HTTP workers enqueue commands; the queue is drained on the
  GUI/main thread inside the menu-draw callback, where SDR++ mutators are applied.
- FFT: own FFTW (size 1024, Blackman window) on a `bindIQStream` tap.
- Audio: `network_sink`-style `Packer → StereoToMono → Handler<float>` tap.
