import subprocess
import threading

class AudioStreamer:
    def __init__(self):
        self.process = None
        self.frequency = 118.7e6
        self.gain = 40
        self.device_serial = "00000002"
        self.running = False

    def set_frequency(self, freq):
        self.frequency = freq
        if self.running:
            self.stop()
            self.start()

    def start(self):
        cmd = [
            'rtl_fm',
            '-d', self.device_serial,
            '-f', str(int(self.frequency)),
            '-M', 'am',
            '-s', '12k',
            '-g', str(self.gain)
        ]
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        self.running = True

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process = None
        self.running = False

    def read_chunk(self, size=1024):
        if self.process and self.process.stdout:
            return self.process.stdout.read(size)
        return b''
