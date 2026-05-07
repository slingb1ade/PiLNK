"""
PiLNK SDR Controller — sdr_controller.py
Manages rtl_fm → sox → ffmpeg pipeline for VHF airband audio,
streamed over HTTP as Ogg/Opus to browser <audio> elements.

Dongles are identified by USB SERIAL (not index) so the audio path
NEVER accidentally clobbers the ADS-B dongle if USB enumeration
order changes after a power cycle, USB error, or replug.

Default serials (override via SDRController(device_serial=...)):
  VHF audio dongle (RTL-SDR Blog V4):     '00000002'
  ADS-B dongle (FlightAware Pro Stick):    '00001000' — NEVER touched

Streaming model:
  - One pipeline per Pi. Multiple browser clients can subscribe to the
    same stream concurrently. A background reader thread copies bytes
    from ffmpeg's stdout and broadcasts to each subscriber's queue.
  - On freq/squelch/gain changes, rtl_fm + sox + ffmpeg are restarted.
    Each browser's <audio> element will see the Ogg stream end and
    must reconnect to /audio/stream — handled client-side via the
    'ended' event listener.
"""
import subprocess
import threading
import time
import logging
import queue

logger = logging.getLogger(__name__)

VHF_DEFAULT_SERIAL = '00000002'

# Audio output format. Opus-in-Ogg, voice mode, 32 kbps.
# Voice-grade ATC fits comfortably in 24 kbps; 32 kbps gives a small
# safety margin. Browsers play Ogg/Opus natively via <audio src>.
OPUS_BITRATE = '32k'
CHUNK_SIZE = 4096

# Per-subscriber queue depth. Bounded so a slow client can't OOM the
# Pi. At ~32 kbps and 4KB chunks, 64 queued chunks ≈ 16 seconds of
# buffered audio — plenty of headroom; if a client falls further
# behind we drop chunks rather than block other listeners.
QUEUE_MAX = 64


class SDRController:
    def __init__(self, device_serial=VHF_DEFAULT_SERIAL):
        self.device_serial = str(device_serial)
        # rtl-sdr's `-d` flag accepts a serial directly. It first tries to
        # parse as integer → device index. If the string isn't a valid index,
        # it falls through to exact/prefix/suffix serial matching.
        self._device_arg = self.device_serial
        self.frequency = 118700000   # Hz
        self.squelch = 50            # 0-100 (UI scale)
        self.gain = 35               # 0-50
        self.sample_rate = 24000     # 24 kHz — Opus-native, no resample
        self.process = None          # rtl_fm subprocess
        self.sox_process = None      # sox subprocess
        self.ffmpeg_process = None   # ffmpeg subprocess (Opus encoder)
        self.is_playing = False
        self.lock = threading.Lock()
        self.biast_enabled = False

        # Streaming fan-out
        self._subscribers = []
        self._subscribers_lock = threading.Lock()
        self._reader_thread = None
        self._reader_stop = threading.Event()

    # ── helpers ────────────────────────────────────────────────
    @staticmethod
    def _ui_to_rtl_squelch(ui_val):
        """Map UI squelch 0-100 → rtl_fm `-l` units (0-150).
        rtl_fm's `-l` is an RMS-based threshold, not a normalised %.
        UI 0 = no squelch (open), UI 100 = strict cutoff.
        """
        return int(max(0, min(100, ui_val)) * 1.5)

    # ── streaming subscription API ────────────────────────────
    def subscribe(self):
        """Register a new streaming client. Returns a Queue that the
        client should drain. Bounded queue — slow clients will lose
        chunks rather than back-pressure other listeners.
        """
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self._subscribers_lock:
            self._subscribers.append(q)
        logger.info('Stream subscriber added (total=%d)', len(self._subscribers))
        return q

    def unsubscribe(self, q):
        """Remove a streaming client (e.g. on browser disconnect)."""
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
        logger.info('Stream subscriber removed (total=%d)', len(self._subscribers))

    def subscriber_count(self):
        with self._subscribers_lock:
            return len(self._subscribers)

    # ── bias-T ────────────────────────────────────────────────
    def enable_biast(self):
        """Enable bias-T on VHF dongle to power LNA."""
        try:
            subprocess.run(
                ['rtl_biast', '-d', self._device_arg, '-b', '1'],
                capture_output=True, text=True, timeout=5
            )
            self.biast_enabled = True
            logger.info('Bias-T enabled on dongle serial=%s', self.device_serial)
            return True
        except FileNotFoundError:
            logger.warning('rtl_biast not found — skipping')
            return False
        except Exception as e:
            logger.error('Bias-T error: %s', e)
            return False

    def disable_biast(self):
        """Disable bias-T on VHF dongle."""
        try:
            subprocess.run(
                ['rtl_biast', '-d', self._device_arg, '-b', '0'],
                capture_output=True, text=True, timeout=5
            )
            self.biast_enabled = False
            logger.info('Bias-T disabled on dongle serial=%s', self.device_serial)
        except Exception:
            pass

    # ── pipeline lifecycle ────────────────────────────────────
    def start(self, freq_hz=None):
        """Start the rtl_fm → sox → ffmpeg pipeline."""
        with self.lock:
            if self.is_playing:
                self._kill_pipeline()

            if freq_hz:
                self.frequency = int(freq_hz)

            if not self.biast_enabled:
                self.enable_biast()
                time.sleep(0.3)

            try:
                # rtl_fm: receive and demodulate VHF AM
                rtl_cmd = [
                    'rtl_fm',
                    '-d', self._device_arg,
                    '-f', str(self.frequency),
                    '-M', 'am',
                    '-s', str(self.sample_rate),
                    '-l', str(self._ui_to_rtl_squelch(self.squelch)),
                    '-g', str(self.gain),
                    '-'
                ]

                # sox: gain + airband-shaped band-pass (Jim's recipe)
                sox_cmd = [
                    'sox',
                    '-t', 'raw', '-r', str(self.sample_rate),
                    '-e', 'signed', '-b', '16', '-c', '1', '-',
                    '-t', 'raw', '-r', str(self.sample_rate),
                    '-e', 'signed', '-b', '16', '-c', '1', '-',
                    'gain', '12',
                    'lowpass', '3400',
                    'highpass', '200'
                ]

                # ffmpeg: encode raw PCM to Opus-in-Ogg, voice mode,
                # 20 ms frames, packet-flushed for low latency.
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-hide_banner', '-loglevel', 'error',
                    '-f', 's16le',
                    '-ar', str(self.sample_rate),
                    '-ac', '1',
                    '-i', '-',
                    '-c:a', 'libopus',
                    '-b:a', OPUS_BITRATE,
                    '-application', 'voip',
                    '-vbr', 'on',
                    '-frame_duration', '20',
                    '-f', 'ogg',
                    '-flush_packets', '1',
                    '-'
                ]

                # Chain: rtl_fm | sox | ffmpeg
                self.process = subprocess.Popen(
                    rtl_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
                self.sox_process = subprocess.Popen(
                    sox_cmd,
                    stdin=self.process.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
                self.ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=self.sox_process.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                # Allow upstream stdouts to be consumed by their
                # downstream stdin (parent process keeps no handle).
                self.process.stdout.close()
                self.sox_process.stdout.close()

                # Ensure the broadcaster thread is running
                self._ensure_reader_thread()

                self.is_playing = True
                freq_mhz = self.frequency / 1e6
                logger.info(
                    'Streaming %.3f MHz | Squelch UI=%d (rtl=%d) | Gain=%d | %s Opus',
                    freq_mhz, self.squelch,
                    self._ui_to_rtl_squelch(self.squelch),
                    self.gain, OPUS_BITRATE
                )
                return True

            except FileNotFoundError as e:
                logger.error('Required tool not found: %s', e)
                self.is_playing = False
                return False
            except Exception as e:
                logger.error('Start failed: %s', e)
                self._kill_pipeline()
                return False

    def stop(self):
        """Stop the audio pipeline. Subscribers stay connected and
        will block on empty queue until the pipeline restarts or the
        Flask generator decides to disconnect.
        """
        with self.lock:
            self._kill_pipeline()
            self.is_playing = False
            logger.info('Stopped')

    def _kill_pipeline(self):
        """Kill all processes in the pipeline.

        Polls each up to 300 ms for graceful exit, SIGKILLs anything
        still alive. Returns immediately if no processes are alive.
        """
        procs = [self.process, self.sox_process, self.ffmpeg_process]
        alive = [p for p in procs if p and p.poll() is None]

        if not alive:
            self.process = None
            self.sox_process = None
            self.ffmpeg_process = None
            return

        for p in alive:
            try:
                p.terminate()
            except Exception:
                pass

        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            if all(p.poll() is not None for p in alive):
                break
            time.sleep(0.03)

        for p in alive:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass

        self.process = None
        self.sox_process = None
        self.ffmpeg_process = None

    # ── reader / broadcaster ──────────────────────────────────
    def _ensure_reader_thread(self):
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name='sdr-reader'
        )
        self._reader_thread.start()

    def _reader_loop(self):
        """Pump bytes from ffmpeg.stdout to all subscriber queues.
        Survives pipeline restarts: when ffmpeg.stdout closes, it
        sleeps and waits for the next start() to wire up a fresh
        ffmpeg_process.
        """
        idle_log_at = 0.0
        while not self._reader_stop.is_set():
            proc = self.ffmpeg_process  # snapshot; could be None
            if proc is None or proc.stdout is None:
                # Idle wait — log once per 10s so journalctl isn't spammed
                now = time.monotonic()
                if now > idle_log_at:
                    logger.debug('Reader idle (no ffmpeg)')
                    idle_log_at = now + 10
                time.sleep(0.05)
                continue

            try:
                data = proc.stdout.read(CHUNK_SIZE)
            except (ValueError, OSError):
                time.sleep(0.05)
                continue

            if not data:
                # EOF on current ffmpeg — pipeline likely restarted
                time.sleep(0.05)
                continue

            with self._subscribers_lock:
                subs = list(self._subscribers)
            for q in subs:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # Slow client — drop this chunk for them only
                    pass

    # ── setters ───────────────────────────────────────────────
    def set_frequency(self, freq_hz):
        """Change frequency — instant restart."""
        with self.lock:
            self.frequency = int(freq_hz)
            was_playing = self.is_playing
            if self.is_playing:
                self._kill_pipeline()
                time.sleep(0.1)
                self.is_playing = False
        if was_playing:
            self.start()
        freq_mhz = self.frequency / 1e6
        logger.info('Frequency → %.3f MHz', freq_mhz)

    def set_squelch(self, level):
        """Set squelch level (UI 0-100)."""
        with self.lock:
            self.squelch = max(0, min(100, int(level)))
            was_playing = self.is_playing
            if self.is_playing:
                self._kill_pipeline()
                time.sleep(0.1)
                self.is_playing = False
        if was_playing:
            self.start()
        logger.info(
            'Squelch → UI=%d (rtl=%d)',
            self.squelch, self._ui_to_rtl_squelch(self.squelch)
        )

    def set_gain(self, gain):
        """Set RF gain (0-50) — requires restart."""
        with self.lock:
            self.gain = max(0, min(50, int(gain)))
            was_playing = self.is_playing
            if self.is_playing:
                self._kill_pipeline()
                time.sleep(0.1)
                self.is_playing = False
        if was_playing:
            self.start()
        logger.info('Gain → %d', self.gain)

    def get_status(self):
        """Return current state."""
        if self.is_playing and self.process and self.process.poll() is not None:
            self.is_playing = False

        return {
            'playing': self.is_playing,
            'frequency': self.frequency,
            'frequency_mhz': round(self.frequency / 1e6, 3),
            'squelch': self.squelch,
            'gain': self.gain,
            'biast': self.biast_enabled,
            'device_serial': self.device_serial,
            'subscribers': self.subscriber_count(),
            'codec': 'opus',
            'bitrate': OPUS_BITRATE
        }

    def cleanup(self):
        """Clean shutdown."""
        self._reader_stop.set()
        self.stop()
        self.disable_biast()
