"""
PiLNK SDR Controller — sdr_controller.py
Manages rtl_fm → sox → aplay pipeline for VHF airband audio.

Dongles are identified by USB SERIAL (not index) so the audio path
NEVER accidentally clobbers the ADS-B dongle if USB enumeration
order changes after a power cycle, USB error, or replug.

Default serials (override via SDRController(device_serial=...)):
  VHF audio dongle (RTL-SDR Blog V4):     '00000002'
  ADS-B dongle (FlightAware Pro Stick):    '00001000' — NEVER touched
"""
import subprocess
import threading
import time

VHF_DEFAULT_SERIAL = '00000002'

class SDRController:
    def __init__(self, device_serial=VHF_DEFAULT_SERIAL):
        self.device_serial = str(device_serial)
        # rtl-sdr's `-d` flag accepts a serial directly. It first tries to
        # parse as integer → device index. If the string isn't a valid index,
        # it falls through to exact/prefix/suffix serial matching.
        # We pass the bare serial; for AJ's 2-dongle setup it correctly
        # falls through to serial match (no device 2 exists).
        self._device_arg = self.device_serial
        self.frequency = 118700000   # Hz
        self.squelch = 50            # 0-100
        self.gain = 35               # 0-50
        self.sample_rate = 24000     # 24kHz for better quality
        self.process = None          # rtl_fm subprocess
        self.sox_process = None      # sox subprocess
        self.aplay_process = None    # aplay subprocess
        self.is_playing = False
        self.lock = threading.Lock()
        self.biast_enabled = False

    def enable_biast(self):
        """Enable bias-T on VHF dongle to power LNA."""
        try:
            result = subprocess.run(
                ['rtl_biast', '-d', self._device_arg, '-b', '1'],
                capture_output=True, text=True, timeout=5
            )
            self.biast_enabled = True
            print(f'[SDR] Bias-T enabled on dongle serial={self.device_serial}')
            return True
        except FileNotFoundError:
            print('[SDR] rtl_biast not found — skipping')
            return False
        except Exception as e:
            print(f'[SDR] Bias-T error: {e}')
            return False

    def disable_biast(self):
        """Disable bias-T on VHF dongle."""
        try:
            subprocess.run(
                ['rtl_biast', '-d', self._device_arg, '-b', '0'],
                capture_output=True, text=True, timeout=5
            )
            self.biast_enabled = False
            print(f'[SDR] Bias-T disabled on dongle serial={self.device_serial}')
        except Exception:
            pass

    def start(self, freq_hz=None):
        """Start the rtl_fm → sox → aplay pipeline."""
        with self.lock:
            if self.is_playing:
                self._kill_pipeline()

            if freq_hz:
                self.frequency = int(freq_hz)

            # Enable bias-T before starting
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
                    '-l', str(self.squelch),
                    '-g', str(self.gain),
                    '-'
                ]

                # sox: audio processing (gain, filtering)
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

                # aplay: output to ALSA (HDMI or 3.5mm jack)
                aplay_cmd = [
                    'aplay',
                    '-r', str(self.sample_rate),
                    '-f', 'S16_LE',
                    '-c', '1',
                    '-t', 'raw',
                    '-D', 'default'
                ]

                # Chain: rtl_fm | sox | aplay
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

                self.aplay_process = subprocess.Popen(
                    aplay_cmd,
                    stdin=self.sox_process.stdout,
                    stderr=subprocess.DEVNULL
                )

                # Allow rtl_fm stdout to be consumed by sox
                self.process.stdout.close()
                self.sox_process.stdout.close()

                self.is_playing = True
                freq_mhz = self.frequency / 1e6
                print(f'[SDR] Playing {freq_mhz:.3f} MHz | Squelch: {self.squelch} | Gain: {self.gain}')
                return True

            except FileNotFoundError as e:
                print(f'[SDR] Required tool not found: {e}')
                self.is_playing = False
                return False
            except Exception as e:
                print(f'[SDR] Start failed: {e}')
                self._kill_pipeline()
                return False

    def stop(self):
        """Stop the audio pipeline."""
        with self.lock:
            self._kill_pipeline()
            self.is_playing = False
            print('[SDR] Stopped')

    def _kill_pipeline(self):
        """Kill all processes in the pipeline."""
        for proc in [self.process, self.sox_process, self.aplay_process]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except:
                    pass

        # Give processes 0.3s to die gracefully
        time.sleep(0.3)

        # Force kill anything still alive
        for proc in [self.process, self.sox_process, self.aplay_process]:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except:
                    pass

        self.process = None
        self.sox_process = None
        self.aplay_process = None

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
        print(f'[SDR] Frequency → {freq_mhz:.3f} MHz')

    def set_squelch(self, level):
        """Set squelch level (0-100) — requires restart."""
        with self.lock:
            self.squelch = max(0, min(100, int(level)))
            was_playing = self.is_playing
            if self.is_playing:
                self._kill_pipeline()
                time.sleep(0.1)
                self.is_playing = False
        if was_playing:
            self.start()
        print(f'[SDR] Squelch → {self.squelch}')

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
        print(f'[SDR] Gain → {self.gain}')

    def get_status(self):
        """Return current state."""
        # Check if processes are still alive
        if self.is_playing and self.process and self.process.poll() is not None:
            self.is_playing = False

        return {
            'playing': self.is_playing,
            'frequency': self.frequency,
            'frequency_mhz': round(self.frequency / 1e6, 3),
            'squelch': self.squelch,
            'gain': self.gain,
            'biast': self.biast_enabled,
            'device_serial': self.device_serial
        }

    def cleanup(self):
        """Clean shutdown."""
        self.stop()
        self.disable_biast()
