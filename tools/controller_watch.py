#!/usr/bin/env python3
# Standing controller watch for pilnkradio (124.300 Approach) — Pi5-resident.
# Runs 24/7 as a systemd service (controller-watch.service). Logs every
# FFT-detected burst with timestamp/SNR/duration, saves WAV clips of weak
# (controller-class) bursts that pass the squelch, emits hourly summaries,
# prunes clips older than RETENTION_DAYS, and pushes an ntfy alert when a
# strong-controller window opens (trailing weak-burst median >= ALERT_SNR).
#
# Install (Pi5) — source lives in the repo at tools/:
#   sudo cp ~/pilnk/tools/controller_watch.py /usr/local/bin/controller_watch.py
#   sudo cp ~/pilnk/tools/controller-watch.service /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now controller-watch
# Log:    /home/aj/ctrl-watch/controller-watch.log
# Clips:  /home/aj/ctrl-watch/clips/
import json, time, urllib.request, socket, base64, os, struct, threading, wave, collections

HOST = "127.0.0.1"; PORT = 5656
B = f"http://{HOST}:{PORT}"
VFO = 124.3e6
BASE = "/home/aj/ctrl-watch"
LOG = os.path.join(BASE, "controller-watch.log")
CLIPDIR = os.path.join(BASE, "clips")
WEAK_LO, WEAK_HI = 8.0, 17.0        # controller-class band (FFT SNR dB)
ALERT_SNR = 13.5                     # trailing median >= this -> window alert
ALERT_MIN_N = 3                      # need >=3 weak bursts in trailing 15 min
ALERT_COOLDOWN = 1800                # max one push per 30 min
QUIET = (22, 7)                      # no pushes 22:00-07:00 (log still runs)
NTFY = "https://ntfy.sh/pilnk-ctrl-w7k2p9x4"
MAX_CLIPS_PER_DAY = 150
RETENTION_DAYS = 14                  # clips older than this are pruned hourly

os.makedirs(CLIPDIR, exist_ok=True)

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with open(LOG, "a") as f: f.write(line + "\n")

def prune_clips():
    cutoff = time.time() - RETENTION_DAYS * 86400
    n = 0
    try:
        for f in os.listdir(CLIPDIR):
            p = os.path.join(CLIPDIR, f)
            if f.endswith(".wav") and os.path.getmtime(p) < cutoff:
                os.remove(p); n += 1
    except Exception as e:
        log(f"prune error: {e}")
    if n: log(f"pruned {n} clips older than {RETENTION_DAYS} days")

def ws_connect(path):
    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp: resp += s.recv(1024)
    return s

def frames(s, buf):
    while len(buf[0]) >= 2:
        b = buf[0]
        ln = b[1] & 0x7F; off = 2
        if ln == 126:
            if len(b) < 4: return
            ln = struct.unpack(">H", b[2:4])[0]; off = 4
        if len(b) < off + ln: return
        yield b[off:off+ln]
        buf[0] = b[off+ln:]

# ---- audio thread: rolling 20 s ring of float samples + liveness ----------
RING_SECONDS = 20
ring = collections.deque(maxlen=48000 * RING_SECONDS)
ring_lock = threading.Lock()
audio_live = 0.0

def audio_loop():
    global audio_live
    while True:
        try:
            s = ws_connect("/sdr/audio"); s.settimeout(5); buf = [b""]
            while True:
                c = s.recv(65536)
                if not c: break
                buf[0] += c
                for p in frames(s, buf):
                    n = len(p) // 4
                    if not n: continue
                    vals = struct.unpack(f"<{n}f", p[:n*4])
                    with ring_lock: ring.extend(vals)
                    if any(v != 0.0 for v in vals): audio_live = time.time()
        except Exception as e:
            log(f"audio ws reconnect: {e}"); time.sleep(10)

def save_clip(tag):
    # dump the last RING_SECONDS of audio (burst is inside it)
    with ring_lock: snap = list(ring)
    if not snap: return None
    day = time.strftime("%Y-%m-%d")
    existing = [f for f in os.listdir(CLIPDIR) if f.startswith(day)]
    if len(existing) >= MAX_CLIPS_PER_DAY: return None
    path = os.path.join(CLIPDIR, f"{day}-{time.strftime('%H%M%S')}-{tag}.wav")
    w = wave.open(path, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
    w.writeframes(struct.pack(f"<{len(snap)}h",
        *[max(-32767, min(32767, int(v * 24000))) for v in snap]))
    w.close()
    return path

last_alert = 0.0
def maybe_alert(recent):
    global last_alert
    now = time.time()
    h = time.localtime().tm_hour
    if QUIET[0] <= h or h < QUIET[1]: return
    if now - last_alert < ALERT_COOLDOWN: return
    weak = [snr for t, snr in recent if now - t < 900]
    if len(weak) < ALERT_MIN_N: return
    med = sorted(weak)[len(weak)//2]
    if med < ALERT_SNR: return
    last_alert = now
    try:
        urllib.request.urlopen(urllib.request.Request(NTFY,
            data=f"Controller window OPEN on 124.300 — {len(weak)} weak bursts / 15 min, median {med:.1f} dB. Go listen!".encode(),
            headers={"Title": "PiLNK controller watch", "Priority": "high", "Tags": "radio"}), timeout=10)
        log(f"ALERT pushed: weak median {med:.1f} dB (n={len(weak)})")
    except Exception as e:
        log(f"ntfy push failed: {e}")

def main():
    log("=== controller watch start (Pi5-resident) ===")
    threading.Thread(target=audio_loop, daemon=True).start()
    recent = []          # (time, snr) of weak bursts, trailing
    hour_stats = {"weak": [], "strong": [], "floor": None}
    last_hour = time.localtime().tm_hour
    while True:
        try:
            s = ws_connect("/sdr/fft"); s.settimeout(5); buf = [b""]
            floors = []; base = None
            in_burst = False; peak = 0.0; passed = False; t0 = 0
            idle = 0
            while True:
                try:
                    c = s.recv(65536)
                    if not c: raise ConnectionError("fft ws closed")
                    idle = 0
                except socket.timeout:
                    c = b""; idle += 1
                    # FFT runs at 25 fps; 30 s without a byte = dead socket
                    if idle > 6: raise ConnectionError("fft ws stale")
                if c: buf[0] += c
                for p in frames(s, buf):
                    if len(p) < 20: continue
                    center, span = struct.unpack("<dd", p[:16])
                    nb = struct.unpack("<I", p[16:20])[0]
                    if len(p) < 20 + nb*4: continue
                    db = struct.unpack(f"<{nb}f", p[20:20+nb*4])
                    floors.append(sorted(db)[nb//2])
                    if len(floors) > 3000: floors.pop(0)
                    base = sorted(floors)[len(floors)//4]
                    lo = center - span/2
                    i0 = int((VFO - 8000 - lo)/span*nb); i1 = int((VFO + 8000 - lo)/span*nb)
                    if not (0 <= i0 < i1 < nb): continue
                    snr = max(db[i0:i1+1]) - base
                    now = time.time()
                    if snr > WEAK_LO:
                        if not in_burst:
                            in_burst = True; peak = snr; passed = False; t0 = now
                        peak = max(peak, snr)
                        if now - audio_live < 0.3: passed = True
                    elif in_burst and snr < WEAK_LO - 2:
                        in_burst = False
                        dur = now - t0
                        if dur >= 0.4:
                            cls = "weak" if peak < WEAK_HI else "strong"
                            clip = None
                            if cls == "weak" and passed:
                                time.sleep(1.0)      # let the tail land in the ring
                                clip = save_clip(f"{peak:.0f}dB")
                            log(f"burst {peak:5.1f} dB {dur:4.1f}s {cls} "
                                f"{'passed' if passed else 'MUTED'}"
                                f"{' clip=' + os.path.basename(clip) if clip else ''}")
                            hour_stats[cls].append(peak)
                            if cls == "weak":
                                recent.append((now, peak))
                                recent[:] = [(t, v) for t, v in recent if now - t < 900]
                                maybe_alert(recent)
                    # hourly summary
                    h = time.localtime().tm_hour
                    if h != last_hour:
                        w, st = hour_stats["weak"], hour_stats["strong"]
                        def med(v): v = sorted(v); return f"{v[len(v)//2]:.1f}" if v else "—"
                        log(f"HOURLY floor={base:.1f} weak n={len(w)} med={med(w)} max={max(w) if w else 0:.1f} "
                            f"| strong n={len(st)} med={med(st)}")
                        hour_stats = {"weak": [], "strong": [], "floor": None}
                        last_hour = h
                        prune_clips()
        except Exception as e:
            log(f"fft ws reconnect: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
