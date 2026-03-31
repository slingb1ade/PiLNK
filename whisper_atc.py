import threading
import numpy as np
import queue
import re
from faster_whisper import WhisperModel
from scipy import signal as scipy_signal

class ATCWhisper:
    def __init__(self, socketio):
        self.socketio  = socketio
        self.model     = WhisperModel('tiny', device='cpu', compute_type='int8')
        self.running   = False
        self.thread    = None
        self.q         = queue.Queue(maxsize=50)
        self.buf       = b''
        self.CHUNK     = 12000 * 2 * 5
        self.THRESHOLD = 0.025

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        print('[WHISPER] Started')

    def stop(self):
        self.running = False

    def feed(self, raw_bytes):
        try:
            self.q.put_nowait(raw_bytes)
        except queue.Full:
            pass

    def _worker(self):
        while self.running:
            try:
                chunk = self.q.get(timeout=1)
                self.buf += chunk
                if len(self.buf) >= self.CHUNK:
                    self._transcribe(self.buf[:self.CHUNK])
                    self.buf = self.buf[self.CHUNK:]
            except queue.Empty:
                continue

    def _preprocess(self, audio, sample_rate=12000):
        # Highpass filter
        sos_high = scipy_signal.butter(
            4, 200, btype='high', fs=sample_rate, output='sos'
        )
        audio = scipy_signal.sosfilt(sos_high, audio)

        # Lowpass filter
        sos_low = scipy_signal.butter(
            4, 3000, btype='low', fs=sample_rate, output='sos'
        )
        audio = scipy_signal.sosfilt(sos_low, audio)

        # Resample to 16kHz for Whisper
        target_rate = 16000
        num_samples = int(len(audio) * target_rate / sample_rate)
        audio = scipy_signal.resample(audio, num_samples).astype(np.float32)

        return audio

    def _transcribe(self, raw_bytes):
        try:
            audio = np.frombuffer(
                raw_bytes, dtype=np.int16
            ).astype(np.float32) / 32768.0

            max_val = np.max(np.abs(audio))
            if max_val < self.THRESHOLD:
                return

            print(f'[WHISPER] Signal detected! Amplitude: {max_val:.4f}')

            # Clean and resample
            audio = self._preprocess(audio)

            # Aggressive normalisation — push audio to full scale
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.95
            
            # Clip to prevent distortion
            audio = np.clip(audio, -1.0, 1.0)

            segments, _ = self.model.transcribe(
                audio,
                language='en',
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=200,
                    speech_pad_ms=100
                ),
                condition_on_previous_text=False,
                no_speech_threshold=0.5,
                temperature=0.0
            )
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                print(f'[WHISPER] {text}')
                callsign = self._extract_callsign(text)
                if callsign:
                    print(f'[CALLSIGN] {callsign}')
                    self.socketio.emit('transmission', {
                        'callsign': callsign,
                        'text':     text
                    })
        except Exception as e:
            print(f'[WHISPER ERROR] {e}')

    def _extract_callsign(self, text):
        patterns = [
            r'\b([A-Z]{2,3}\d{1,4}[A-Z]?)\b',
            r'\b(N\d{1,5}[A-Z]{0,2})\b',
            r'\b(ZK[-\s]?[A-Z]{3})\b',
        ]
        text_upper = text.upper()
        for pattern in patterns:
            match = re.search(pattern, text_upper)
            if match:
                return match.group(1).replace(' ', '').replace('-', '')
        return None
