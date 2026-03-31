import subprocess
import threading
import time

class RadioStream:
    def __init__(self):
        self.lock        = threading.Lock()
        self.running     = False
        self.proc        = None
        self.thread      = None
        self.frequency   = 118.7e6
        self.squelch     = 0
        self.callbacks   = []
        self._buf        = b''
        self._buf_lock   = threading.Lock()

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
        if self.proc:
            try:
                self.proc.terminate()
            except:
                pass
            self.proc = None

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.running = True
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_frequency(self, freq):
        self.frequency = freq
        self.restart()
        print(f'[RADIO] Frequency set to {freq/1e6:.3f} MHz')

    def set_squelch(self, squelch):
        self.squelch = squelch
        self.restart()

    def _run(self):
        cmd = [
            'rtl_fm',
            '-d', '00000002',
            '-f', str(int(self.frequency)),
            '-M', 'am',
            '-s', '12k',
            '-g', '49.6',
            '-l', str(self.squelch)
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            print(f'[RADIO] Tuned to {self.frequency/1e6:.3f} MHz')
            while self.running:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                # Dispatch to callbacks in separate threads
                # so a slow callback never blocks the reader
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
