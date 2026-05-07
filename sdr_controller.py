"""
PiLNK SDR Controller — sdr_controller.py
Manages rtl_fm → sox → (PCM copier) → ffmpeg pipeline for VHF
airband audio, streamed to browser <audio> elements as Ogg/Opus.

Dongles are identified by USB SERIAL (not index) so the audio path
NEVER accidentally clobbers the ADS-B dongle if USB enumeration
order changes after a power cycle, USB error, or replug.

Default serials (override via SDRController(device_serial=...)):
  VHF audio dongle (RTL-SDR Blog V4):     '00000002'
  ADS-B dongle (FlightAware Pro Stick):    '00001000' — NEVER touched

Architecture:
  rtl_fm → sox → [Python copier thread] → ffmpeg → [Python reader] → subscribers
                          ↓                              ↓
                  signal-level meter             recording file (.ogg)

  - PCM copier thread reads raw int16 samples from sox.stdout, computes
    a smoothed peak signal level, then writes the chunk to ffmpeg.stdin.
  - Reader thread reads Ogg/Opus from ffmpeg.stdout, broadcasts to all
    subscriber queues, and tees to the recording file when active.
  - On freq/squelch/gain changes, rtl_fm + sox + ffmpeg are restarted.
    Each browser's <audio> element will see the Ogg stream end and
    reconnect (handled client-side via the 'ended' event).
  - Recordings span pipeline restarts: the file becomes a chained Ogg/
    Opus stream, which is valid spec and plays in browsers/VLC.
"""
import os
import struct
import subprocess
import threading
import time
import logging
import queue
import datetime

logger = logging.getLogger(__name__)

VHF_DEFAULT_SERIAL = '00000002'

# Output format. Opus-in-Ogg, voice mode, 32 kbps.
OPUS_BITRATE = '32k'
CHUNK_SIZE = 4096

# Per-subscriber queue depth. Bounded so a slow client can't OOM.
QUEUE_MAX = 64

# Recording file location (created on first record).
RECORDINGS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'recordings'
)

# Signal-level smoothing factor. New = ALPHA * raw + (1-ALPHA) * old.
# Higher = more responsive, more jitter. Lower = smoother but laggier.
SIGNAL_SMOOTHING_ALPHA = 0.3


class SDRController:
    def __init__(self, device_serial=VHF_DEFAULT_SERIAL):
        self.device_serial = str(device_serial)
        self._device_arg = self.device_serial
        self.frequency = 118700000   # Hz
        self.squelch = 50            # 0-100 (UI scale)
        self.gain = 35               # 0-50
        self.sample_rate = 24000     # 24 kHz — Opus-native
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
        self._copier_thread = None
        self._reader_stop = threading.Event()

        # Signal-level meter (smoothed peak amplitude, 0-100)
        self._signal_level = 0.0

        # Recording state
        self._record_lock = threading.Lock()
        self._record_fh = None
        self._record_filename = None  # absolute path
        self._record_basename = None  # short name for API responses
        self._record_started_at = None  # epoch seconds
        self._record_bytes = 0

    # ── helpers ────────────────────────────────────────────────
    @staticmethod
    def _ui_to_rtl_squelch(ui_val):
        """Map UI squelch 0-100 → rtl_fm `-l` units (0-150)."""
        return int(max(0, min(100, ui_val)) * 1.5)

    # ── streaming subscription API ────────────────────────────
    def subscribe(self):
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self._subscribers_lock:
            self._subscribers.append(q)
        logger.info('Stream subscriber added (total=%d)', len(self._subscribers))
        return q

    def unsubscribe(self, q):
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
        logger.info('Stream subscriber removed (total=%d)', len(self._subscribers))

    def subscriber_count(self):
        with self._subscribers_lock:
            return len(self._subscribers)

    # ── signal level ──────────────────────────────────────────
    def get_signal_level(self):
        """Smoothed peak signal level, 0-100. Updated each PCM chunk."""
        return int(self._signal_level)

    # ── bias-T ────────────────────────────────────────────────
    def enable_biast(self):
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
                # ffmpeg's stdin is a Python-managed pipe (the copier
                # thread writes to it after sampling for signal level).
                self.ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                # rtl_fm.stdout is owned by sox now
                self.process.stdout.close()
                # NOTE: do NOT close sox.stdout — Python copier reads it

                # Health check: if rtl_fm dies in the first ~150ms, the SDR
                # isn't accessible (no VHF dongle, wrong serial, USB error,
                # already in use). Bail out cleanly here — otherwise sox
                # gets EOF and dies, but ffmpeg keeps idling on its
                # Python-managed stdin and accumulates as an orphan on
                # every retry. See: Jim's node, May 6 2026 — 5 ffmpeg
                # zombies after repeated Listen clicks with no VHF dongle.
                time.sleep(0.15)
                if self.process.poll() is not None:
                    rc = self.process.returncode
                    logger.error(
                        'rtl_fm exited immediately (rc=%s) — VHF dongle '
                        '(serial=%s) not available. Check that a second '
                        'RTL-SDR is plugged in and matches the configured '
                        'VHF serial.',
                        rc, self.device_serial
                    )
                    self._kill_pipeline()
                    self.is_playing = False
                    return False

                self._ensure_copier_thread()
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
        """Stop the audio pipeline. Recording is also stopped."""
        with self.lock:
            self._kill_pipeline()
            self.is_playing = False
        # Stop recording outside the controller lock to avoid deadlock
        # with _record_lock (different ordering rules).
        self.stop_recording()
        # Reset signal level so meter doesn't show a stale value
        self._signal_level = 0.0
        logger.info('Stopped')

    def _kill_pipeline(self):
        """Kill all processes in the pipeline."""
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

    # ── threads ───────────────────────────────────────────────
    def _ensure_copier_thread(self):
        if self._copier_thread and self._copier_thread.is_alive():
            return
        self._reader_stop.clear()
        self._copier_thread = threading.Thread(
            target=self._pcm_copier_loop, daemon=True, name='sdr-copier'
        )
        self._copier_thread.start()

    def _ensure_reader_thread(self):
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name='sdr-reader'
        )
        self._reader_thread.start()

    def _pcm_copier_loop(self):
        """Read raw int16 PCM from sox.stdout, compute signal level,
        forward to ffmpeg.stdin. Survives pipeline restarts: when sox
        or ffmpeg are nulled, idle until the next start().
        """
        while not self._reader_stop.is_set():
            sox = self.sox_process
            ff = self.ffmpeg_process
            if (sox is None or sox.stdout is None
                    or ff is None or ff.stdin is None):
                self._signal_level *= 0.5  # decay during gap
                time.sleep(0.05)
                continue

            try:
                chunk = sox.stdout.read(CHUNK_SIZE)
            except (ValueError, OSError):
                time.sleep(0.05)
                continue

            if not chunk:
                # sox closed its stdout — wait for restart
                time.sleep(0.05)
                continue

            # Signal level: peak |sample| → 0-100, exponentially smoothed
            try:
                n_samples = len(chunk) // 2
                if n_samples:
                    samples = struct.unpack('<%dh' % n_samples, chunk[:n_samples * 2])
                    peak = max(abs(s) for s in samples)
                    raw = min(peak / 327.67, 100.0)  # 32767/100 = 327.67
                    self._signal_level = (
                        SIGNAL_SMOOTHING_ALPHA * raw
                        + (1.0 - SIGNAL_SMOOTHING_ALPHA) * self._signal_level
                    )
            except Exception:
                pass  # sample-level failure shouldn't kill the pipeline

            # Forward to ffmpeg
            try:
                ff.stdin.write(chunk)
                ff.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                # ffmpeg gone — drop chunk, wait for next pipeline
                time.sleep(0.05)

    def _reader_loop(self):
        """Pump Ogg/Opus from ffmpeg.stdout to subscribers + recording."""
        while not self._reader_stop.is_set():
            proc = self.ffmpeg_process
            if proc is None or proc.stdout is None:
                time.sleep(0.05)
                continue

            try:
                data = proc.stdout.read(CHUNK_SIZE)
            except (ValueError, OSError):
                time.sleep(0.05)
                continue

            if not data:
                time.sleep(0.05)
                continue

            # Broadcast to live listeners
            with self._subscribers_lock:
                subs = list(self._subscribers)
            for q in subs:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass  # drop for slow client

            # Tee to recording if active. Holding _record_lock keeps
            # start/stop_recording from racing with file writes.
            with self._record_lock:
                if self._record_fh is not None:
                    try:
                        self._record_fh.write(data)
                        self._record_bytes += len(data)
                    except Exception as e:
                        logger.error('Recording write failed: %s', e)

    # ── setters ───────────────────────────────────────────────
    def set_frequency(self, freq_hz):
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

    # ── recording API ─────────────────────────────────────────
    def start_recording(self):
        """Start recording the current stream to disk. Returns
        (success: bool, info: dict). Filename format:
            recordings/2026-05-07_18-30-22_118.700MHz.ogg
        """
        if not self.is_playing:
            return False, {'error': 'pipeline not running — start audio first'}

        with self._record_lock:
            if self._record_fh is not None:
                return False, {
                    'error': 'recording already in progress',
                    'filename': self._record_basename
                }

            try:
                os.makedirs(RECORDINGS_DIR, exist_ok=True)
            except Exception as e:
                logger.error('Failed to create recordings dir: %s', e)
                return False, {'error': 'cannot create recordings dir'}

            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            freq_mhz = self.frequency / 1e6
            basename = '{ts}_{f:.3f}MHz.ogg'.format(ts=ts, f=freq_mhz)
            full_path = os.path.join(RECORDINGS_DIR, basename)

            try:
                fh = open(full_path, 'wb')
            except Exception as e:
                logger.error('Failed to open recording file: %s', e)
                return False, {'error': 'cannot open file'}

            self._record_fh = fh
            self._record_filename = full_path
            self._record_basename = basename
            self._record_started_at = time.time()
            self._record_bytes = 0

        logger.info('Recording started → %s', basename)
        return True, {
            'filename': basename,
            'started_at': self._record_started_at
        }

    def stop_recording(self):
        """Stop the active recording. Returns dict with stats, or
        None if no recording was active.
        """
        with self._record_lock:
            if self._record_fh is None:
                return None

            fh = self._record_fh
            basename = self._record_basename
            full_path = self._record_filename
            started = self._record_started_at
            byte_count = self._record_bytes

            try:
                fh.flush()
                fh.close()
            except Exception:
                pass

            self._record_fh = None
            self._record_filename = None
            self._record_basename = None
            self._record_started_at = None
            self._record_bytes = 0

        duration = max(0.0, time.time() - started) if started else 0.0
        try:
            on_disk = os.path.getsize(full_path)
        except OSError:
            on_disk = byte_count

        logger.info(
            'Recording stopped → %s (%.1fs, %d bytes)',
            basename, duration, on_disk
        )
        return {
            'filename': basename,
            'duration_seconds': round(duration, 2),
            'size_bytes': on_disk
        }

    def is_recording(self):
        with self._record_lock:
            return self._record_fh is not None

    def recording_info(self):
        """Live info about the in-progress recording, or None."""
        with self._record_lock:
            if self._record_fh is None:
                return None
            duration = (
                time.time() - self._record_started_at
                if self._record_started_at else 0.0
            )
            return {
                'filename': self._record_basename,
                'duration_seconds': round(duration, 2),
                'size_bytes': self._record_bytes
            }

    # ── status ────────────────────────────────────────────────
    def get_status(self):
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
            'bitrate': OPUS_BITRATE,
            'signal_level': self.get_signal_level(),
            'recording': self.recording_info()
        }

    def cleanup(self):
        """Clean shutdown."""
        self._reader_stop.set()
        self.stop()
        self.disable_biast()
