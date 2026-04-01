import subprocess
import threading
import time
import os

class RadioStream:
    def __init__(self):
        self.lock            = threading.Lock()
        self.running         = False
        self.proc            = None
        self.thread          = None
        self.frequency       = 118.7e6
        self.squelch         = 0
        self.gain            = 49.6
        self.callbacks       = []
        self._buf            = b''
        self._buf_lock       = threading.Lock()
        self._signal_level   = 0
        self._recording      = False
        self._record_file    = None
        self._record_proc    = None

    def subscribe(self, callback):
        with self.lock:
            self.callbacks.append(callback)

    def unsubscribe(self, callback):
        with self.lock:
            if callback in self.callbacks:
                self.callbacks.remove(callback)

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print('[RADIO] Stream started')

    def stop(self):
        self.running = False
        self.stop_recording()
        if self.proc:
            try:
                self.proc.terminate()
            except:
                pass
            self.proc = None

    def restart(self):
        self.stop_recording()
        self.stop()
        time.sleep(0.3)
        self.running = True
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_frequency(self, freq):
        self.frequency = freq
        self.restart()
        print(f'[RADIO] Tuned to {freq/1e6:.3f} MHz')

    def set_squelch(self, squelch):
        self.squelch = squelch
        self.restart()

    def set_gain(self, gain):
        self.gain = gain if gain > 0 else 49.6
        self.restart()
        print(f'[RADIO] Gain set to {self.gain}')

    def get_signal_level(self):
        """Returns current signal level estimate (0-100)"""
        return self._signal_level

    def start_recording(self, filepath):
        """Start recording raw audio to an ogg file via sox"""
        self.stop_recording()
        self._record_file = filepath
        self._recording = True
        print(f'[RADIO] Recording to {filepath}')

    def stop_recording(self):
        """Stop recording and close file"""
        if self._recording:
            self._recording = False
            if self._record_proc:
                try:
                    self._record_proc.stdin.close()
                    self._record_proc.wait(timeout=3)
                except:
                    try:
                        self._record_proc.terminate()
                    except:
                        pass
                self._record_proc = None
            self._record_file = None
            print('[RADIO] Recording stopped')

    def _start_record_proc(self, filepath):
        """Spin up a sox process to write ogg file"""
        sox_cmd = [
            'sox',
            '-t', 'raw', '-r', '12k', '-e', 'signed', '-b', '16', '-c', '1', '-',
            '-t', 'ogg', filepath
        ]
        try:
            self._record_proc = subprocess.Popen(
                sox_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f'[RADIO] Record proc error: {e}')
            self._record_proc = None

    def _run(self):
        cmd = [
            'rtl_fm',
            '-d', '00000002',
            '-f', str(int(self.frequency)),
            '-M', 'am',
            '-s', '12k',
            '-g', str(self.gain),
            '-l', str(self.squelch)
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            print(f'[RADIO] Tuned to {self.frequency/1e6:.3f} MHz')

            # Start recording process if needed
            if self._recording and self._record_file:
                self._start_record_proc(self._record_file)

            while self.running:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break

                # Estimate signal level from chunk amplitude (0-100)
                try:
                    import struct
                    samples = struct.unpack('<' + 'h' * (len(chunk)//2), chunk)
                    peak = max(abs(s) for s in samples) if samples else 0
                    self._signal_level = int(min(peak / 327, 100))
                except:
                    self._signal_level = 0

                # Write to recording if active
                if self._recording and self._record_proc and self._record_proc.stdin:
                    try:
                        self._record_proc.stdin.write(chunk)
                    except:
                        pass

                # Dispatch to callbacks
                with self.lock:
                    cbs = list(self.callbacks)
                for cb in cbs:
                    threading.Thread(
                        target=cb,
                        args=(chunk,),
                        daemon=True
                    ).start()

        except Exception as e:
            print(f'[RADIO] Error: {e}')
        finally:
            if self.proc:
                try:
                    self.proc.terminate()
                except:
                    pass
            print('[RADIO] Stream ended')
            if self.running:
                time.sleep(1)
                print('[RADIO] Restarting...')
                self._run()
