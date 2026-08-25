#!/usr/bin/env python3
"""PiLNK live ATC speech-to-text — one module, the whole chain.

    capture (WS /sdr/audio, float32 48k) -> RMS gate -> ATC model
        -> callsign reconcile (ADS-B /flights) -> GATE -> transcript

Consolidates the June prototype (whisper_atc.py + live_demo_capture/transcribe.py
+ atc_gate.py + callsign_reconcile.py) into a single service against the CURRENT
audio path: pilnkradio v2 on the Pi5 (NOT the retired SDR++ bridge on the Pi4).

The reconcile is PiLNK's edge and doubles as a hallucination filter: the ATC model
is European-biased ("New Zealand 570" -> "wizz air 570") and spells numbers as words,
but the *number* is usually right and we already track every aircraft overhead. Match
the spoken number against tracked traffic -> recover the authoritative callsign
(ANZ570). A line with no number matching traffic overhead never reaches the screen.

Model-on-node rule: in production this runs ON the SDR node (the Pi5), never the hub.

CLI:
    python3 atc_stt.py selftest              # reconcile unit tests (no model load)
    python3 atc_stt.py file <wav> [seconds]  # offline transcript of a WAV (+ reconcile if --flights)
    python3 atc_stt.py live [seconds]        # live capture from the Pi5, gate + reconcile

    optional: --model <id>  --work-rate <hz>  --flights <url>  --audio <ws-url>  --raw
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter

import numpy as np
from scipy import signal as scipy_signal

# ---------------------------------------------------------------------------
# Config — the CURRENT PiLNK audio path (Pi5, pilnkradio v2). Same :5656 wire
# contract as the old bridge, different host (Pi4 192.168.50.18 is retired).
# ---------------------------------------------------------------------------
PI5          = "192.168.50.22"
AUDIO_WS     = f"ws://{PI5}:5656/sdr/audio"   # raw float32, 48 kHz, mono
STATUS_URL   = f"http://{PI5}:5656/sdr/status"
FLIGHTS_URL  = f"http://{PI5}:5000/flights"   # ADS-B tracked traffic (reconcile target)
SOURCE_RATE  = 48000                          # pilnkradio audio sample rate

ATC_MODEL    = "jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper"


# ===========================================================================
# 1. ADS-B callsign reconcile  (verbatim from the proven callsign_reconcile.py,
#    8/8 unit tests + recovers the live ANZ049M sub-window case)
# ===========================================================================
_NUM = {'zero': '0', 'oh': '0', 'one': '1', 'two': '2', 'three': '3', 'tree': '3',
        'four': '4', 'fower': '4', 'for': '4', 'five': '5', 'fife': '5', 'six': '6',
        'seven': '7', 'eight': '8', 'nine': '9', 'niner': '9'}


_FREQ_MARK = {'decimal', 'point'}          # a digit run next to these is a FREQUENCY, not a callsign


def spoken_numbers(text):
    """Contiguous runs of spoken/written digits -> list of digit strings (2-5 long).

    Frequency readbacks are excluded: ATC always says frequencies as 'NNN decimal N'
    ('one two one decimal zero'), so a run adjacent to 'decimal'/'point' (and the
    fractional digits after it) is dropped — it caused false callsign matches
    (121.0 -> '21' -> ANZ021M)."""
    runs, cur, skip_frac = [], [], False
    for t in re.findall(r"[a-z]+|\d+", text.lower()):
        if t.isdigit() or t in _NUM:
            if skip_frac:                  # fractional part of a frequency -> ignore
                continue
            cur.append(t if t.isdigit() else _NUM[t])
        elif t in _FREQ_MARK:              # the run we just built is a frequency's integer part
            cur = []                       # drop it, and drop the digits that follow the decimal
            skip_frac = True
        else:
            if cur:
                runs.append(''.join(cur)); cur = []
            skip_frac = False
    if cur:
        runs.append(''.join(cur))
    return [r for r in runs if 2 <= len(r) <= 5]


# --- NZ airline lexicon (phraseology corpus section 11) ----------------------
# The model is fine-tuned on European ATC, so it renders NZ airline names as the
# European ones it was trained on ("csa six one eight" for ANZ618, "praha radar"
# over Auckland). ADS-B already knows the authoritative identity, so the spoken
# airline word is used two ways: to VALIDATE a number match, and to REPAIR the
# wrong word once the match is trusted.
_AIRLINES = {                                   # spoken form -> ICAO operator code
    'new zealand': 'ANZ', 'air new zealand': 'ANZ', 'link': 'ANZ',
    'jetstar': 'JST', 'qantas': 'QFA', 'singapore': 'SIA', 'emirates': 'UAE',
    'cathay': 'CPA', 'china eastern': 'CES', 'china southern': 'CSN',
    'malaysian': 'MAS', 'fiji': 'FJI', 'korean': 'KAL', 'united': 'UAL',
    'american': 'AAL', 'air canada': 'ACA', 'latam': 'LAN', 'tahiti': 'THT',
    'virgin': 'VOZ', 'philippine': 'PAL',
}
_SPOKEN = {                                     # ICAO -> how ATC actually says it
    'ANZ': 'new zealand', 'JST': 'jetstar', 'QFA': 'qantas', 'SIA': 'singapore',
    'UAE': 'emirates', 'CPA': 'cathay', 'CES': 'china eastern', 'CSN': 'china southern',
    'MAS': 'malaysian', 'FJI': 'fiji', 'KAL': 'korean', 'UAL': 'united',
    'AAL': 'american', 'ACA': 'air canada', 'LAN': 'latam', 'THT': 'tahiti',
    'VOZ': 'virgin', 'PAL': 'philippine',
}
# OPERATORS with no NZ service (Air Berlin folded in 2017). Hearing one is proof of the
# model's European prior, never of local traffic — and since the aircraft's real operator
# is known from ADS-B, the word can be safely rewritten.
_NOT_HERE_OPS = ['csa', 'wizz air', 'wizz', 'aeroflot', 'klm', 'air baltic', 'baltic',
                 'air berlin', 'air malta', 'lufthansa', 'austrian', 'swiss', 'cimber',
                 'czech', 'alitalia', 'iberia', 'ryanair', 'easyjet', 'vueling', 'finnair',
                 'britair', 'brit air', 'finn air', 'speed bird', 'speedbird', 'air france', 'sas',
                 'norwegian', 'tap']
# STATION/facility names from European airspace. These are equally strong evidence of
# hallucination but must NEVER be rewritten to an airline: 'praha radar' would become
# 'jetstar radar', inventing a phrase nobody said. Flag them, leave the words alone.
_NOT_HERE_STATIONS = ['praha', 'ruzyne', 'warsaw', 'bratislava', 'vienna', 'zurich']


def _csnum(cs):
    m = re.search(r'(\d+)', cs)
    return m.group(1) if m else ''


def _csop(cs):
    """Operator prefix of a callsign: ANZ049M -> ANZ. Registrations (ZKJRA, VHABC)
    have no operator, so they never take part in airline agreement."""
    m = re.match(r'^([A-Z]{3})\d', cs.strip().upper())
    return m.group(1) if m else ''


def spoken_airline(text):
    """(icao, phrase, not_here) for the airline named in `text`, else (None, None, False).
    Longest phrase wins so 'air new zealand' is not shadowed by 'new zealand'."""
    t = ' ' + re.sub(r'[^a-z ]', ' ', text.lower()) + ' '
    t = re.sub(r'\s+', ' ', t)
    best = None
    for phrase, icao in _AIRLINES.items():
        if ' %s ' % phrase in t and (best is None or len(phrase) > len(best[1])):
            best = (icao, phrase)
    if best:
        return best[0], best[1], False
    for phrase in sorted(_NOT_HERE_OPS, key=len, reverse=True):
        if ' %s ' % phrase in t:
            return None, phrase, True
    return None, None, False


def _airline_mentions(text):
    """Every operator name in the line, local or not. More than one means the model is
    inventing rather than mis-naming a single aircraft."""
    t = re.sub(r'\s+', ' ', ' ' + re.sub(r'[^a-z ]', ' ', text.lower()) + ' ')
    found = []
    for phrase in list(_AIRLINES) + _NOT_HERE_OPS:
        if ' %s ' % phrase in t:
            found.append(phrase)
    return [p for p in found                    # drop phrases contained in a longer hit
            if not any(p != q and p in q for q in found)]


def hallucinated_station(text):
    """A European facility name heard over Auckland ('praha radar', 'warsaw approach').
    Evidence that the whole transmission is invented — reported, never rewritten."""
    t = ' ' + re.sub(r'[^a-z ]', ' ', text.lower()) + ' '
    for s in _NOT_HERE_STATIONS:
        if ' %s ' % s in re.sub(r'\s+', ' ', t):
            return s
    return None


def repair_airline(text, callsign):
    """Rewrite a wrong airline word to the one ADS-B proves. 'csa six one eight' with
    a confirmed ANZ618 becomes 'new zealand six one eight'. Returns (text, changed).

    Only ever runs on a transmission whose NUMBER already matched tracked traffic, so
    the identity is not a guess — the audio supplies the instruction, ADS-B the identity.
    Repairing matters beyond display: an uncorrected label teaches the next fine-tune to
    say 'csa' over Auckland, which is the bias we are trying to remove."""
    want = _SPOKEN.get(_csop(callsign))
    if not want:
        return text, False
    icao, phrase, not_here = spoken_airline(text)
    if not phrase or (icao and icao == _csop(callsign)):
        return text, False                      # nothing named, or already correct
    if not not_here:
        # A REAL local operator was named and it disagrees with the number match. Emirates
        # does fly here, so if the controller said "emirates" the number match is the thing
        # that is wrong, not the word. Overwriting it would convert a truth into a
        # falsehood. Only operators that provably cannot be overhead get rewritten.
        return text, False

    # REFUSE to repair a line that is fabricated rather than merely mis-named. Rewriting
    # one word inside an invention makes it READ as authentic — "air berlin alfa new zone
    # five five five" becomes "new zealand alfa new zone five five five", which looks like
    # real ATC and would no longer be spotted as nonsense. A visibly wrong line is safer
    # than a plausible false one.
    if hallucinated_station(text):
        return text, False                      # European facility named -> whole line suspect
    if len(_airline_mentions(text)) > 1:
        return text, False                      # two operators in one transmission

    out = re.sub(r'(?i)(?<![a-z])%s(?![a-z])' % re.escape(phrase), want, text, count=1)
    return out, out != text


def _windows(run):
    """The full run plus its digit PREFIXES (len-1 .. 2). The model sometimes glues a
    trailing digit onto the flight number ('zero four nine five' -> '0495'), so a
    prefix recovers it (049). Internal/suffix windows are deliberately NOT returned:
    '121' -> '21' (a suffix) was matching real callsigns off frequency readbacks."""
    out = [run]
    for L in range(len(run) - 1, 1, -1):   # len-1 down to 2, prefixes only
        out.append(run[:L])
    return out


def reconcile(spoken_text, tracked_callsigns):
    """Return (authoritative_callsign, matched_number) or (None, None).

    Matches spoken numbers (and their prefixes) against the numeric part of each tracked
    callsign; the flight number is usually first, so earlier candidates win.

    The spoken AIRLINE name then arbitrates (corpus section 11 lexicon):
      agrees with the match      -> strongest possible confirmation, take it
      names a DIFFERENT operator
        that is also overhead    -> the number match is probably coincidence -> REJECT.
                                    A wrong callsign on the slide is worse than none,
                                    and this is the ANZ28-vs-'one two eight' failure
                                    in a new guise.
      names an operator with no
        NZ service (csa, praha)  -> proven model bias, not traffic: trust the number and
                                    let repair_airline() fix the word.
      names nothing              -> numbers only, exactly as before.
    """
    idx = {}
    for cs in tracked_callsigns:
        n = _csnum(cs)
        if n:
            idx.setdefault(n, []).append(cs)
            idx.setdefault(n.lstrip('0'), []).append(cs)
    icao, _phrase, not_here = spoken_airline(spoken_text)
    ops_overhead = {_csop(cs) for cs in tracked_callsigns} - {''}

    for run in spoken_numbers(spoken_text):
        for w in _windows(run):
            if len(w) < 2:
                continue
            hits = idx.get(w) or idx.get(w.lstrip('0'))
            if not hits:
                continue
            if icao:
                agree = [cs for cs in hits if _csop(cs) == icao]
                if agree:
                    return agree[0], w                  # number AND operator agree
                if icao in ops_overhead:
                    return None, None                   # named a different aircraft that
                                                        # is genuinely here -> don't guess
            return hits[0], w                           # bias word, or no airline named
    return None, None


# ===========================================================================
# 2. Hallucination / garbage filter  (verbatim from whisper_atc.py)
# ===========================================================================
_HALLUCINATION = re.compile(
    r'thank you for (watching|your time)|see you (in the next|next time)|'
    r'subscribe|thanks for watching|commentary show|please like',
    re.IGNORECASE)


def is_garbage(text):
    """Drop Whisper hallucinations before they reach the slide."""
    words = [w for w in re.split(r'[\s,\-.]+', text.lower()) if w]
    if not words:
        return True
    cnt = Counter(words)
    if len(words) >= 10 and cnt.most_common(1)[0][1] / len(words) > 0.45:
        return True                       # repetition loop ("6-6-6-6…")
    if len(words) >= 20 and len(cnt) / len(words) < 0.3:
        return True                       # low-diversity number-spam loop
    if _HALLUCINATION.search(text):
        return True                       # known filler hallucination
    return False


# ===========================================================================
# 3. Transcriber — preprocessing + the ATC-fine-tuned model
#    (preprocessing chain verbatim from whisper_atc._preprocess; the one change
#    is that it works at a configurable WORK_RATE and only resamples to 16k for
#    Whisper when needed — native 16k avoids the old lossy 48->12->16 hop.)
# ===========================================================================
class ATCTranscriber:
    TARGET_RMS    = 0.12
    GATE_RMS      = 0.004
    NO_SPEECH_MAX = 0.8
    DECLIP        = True
    DENOISE       = True
    DECLIP_THRESH = 0.98
    DENOISE_OVER  = 1.5
    DENOISE_FLOOR = 0.08
    WHISPER_RATE  = 16000

    def __init__(self, model=ATC_MODEL, work_rate=12000, compute_type='int8'):
        from faster_whisper import WhisperModel
        self.model_id  = model
        self.work_rate = work_rate
        t0 = time.time()
        self.model = WhisperModel(model, device='cpu', compute_type=compute_type)
        self.load_s = time.time() - t0

    # --- proven preprocessing (declip -> voiceband bandpass -> spectral denoise) ---
    def _declip(self, x):
        clipped = np.abs(x) >= self.DECLIP_THRESH
        if not clipped.any() or clipped.all():
            return x
        idx = np.arange(len(x)); good = ~clipped
        x = x.copy()
        x[clipped] = np.interp(idx[clipped], idx[good], x[good])
        return x

    def _denoise(self, x, sr):
        nper, nov = 512, 384
        _, _, Z = scipy_signal.stft(x, fs=sr, nperseg=nper, noverlap=nov)
        mag, phase = np.abs(Z), np.angle(Z)
        if mag.shape[1] >= 4:
            fe = np.sum(mag ** 2, axis=0)
            quiet = mag[:, fe <= np.percentile(fe, 15)]
            noise = np.mean(quiet, axis=1, keepdims=True) if quiet.size else np.zeros((mag.shape[0], 1))
        else:
            noise = np.zeros((mag.shape[0], 1))
        mag_clean = np.maximum(mag - self.DENOISE_OVER * noise, self.DENOISE_FLOOR * mag)
        _, xc = scipy_signal.istft(mag_clean * np.exp(1j * phase), fs=sr, nperseg=nper, noverlap=nov)
        if len(xc) < len(x):
            xc = np.pad(xc, (0, len(x) - len(xc)))
        return xc[:len(x)].astype(np.float32)

    def _preprocess(self, audio, in_rate):
        if self.DECLIP:
            audio = self._declip(audio)
        sos_hi = scipy_signal.butter(4, 200,  btype='high', fs=in_rate, output='sos')
        audio  = scipy_signal.sosfilt(sos_hi, audio)
        sos_lo = scipy_signal.butter(4, 3000, btype='low',  fs=in_rate, output='sos')
        audio  = scipy_signal.sosfilt(sos_lo, audio)
        if self.DENOISE:
            audio = self._denoise(audio, in_rate)
        if in_rate != self.WHISPER_RATE:
            n = int(len(audio) * self.WHISPER_RATE / in_rate)
            audio = scipy_signal.resample(audio, n)
        return audio.astype(np.float32)

    def transcribe_chunk(self, audio, in_rate=None):
        """audio: float32 mono at in_rate (default work_rate). Yields cleaned text lines.

        Returns nothing for chunks below the RMS gate (dead air / squelch zeros)."""
        in_rate = in_rate or self.work_rate
        if np.sqrt(np.mean(audio ** 2)) < self.GATE_RMS:
            return
        a = self._preprocess(audio, in_rate)
        rms = float(np.sqrt(np.mean(a ** 2)))
        if rms > 1e-5:
            a = a * (self.TARGET_RMS / rms)
        a = np.clip(a, -1, 1).astype(np.float32)
        segs, _ = self.model.transcribe(
            a, language='en', beam_size=5, vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=200, speech_pad_ms=100),
            condition_on_previous_text=False, no_speech_threshold=0.6, temperature=0.0)
        for s in segs:
            if getattr(s, 'no_speech_prob', 0.0) > self.NO_SPEECH_MAX:
                continue
            t = s.text.strip()
            if t and not is_garbage(t):
                yield t


# ===========================================================================
# 4. Sources: ADS-B tracked traffic + WAV decode + live WS capture
# ===========================================================================
def fetch_tracked(url=FLIGHTS_URL, timeout=8):
    """Snapshot the callsigns PiLNK is tracking right now (the reconcile target)."""
    data = json.load(urllib.request.urlopen(url, timeout=timeout))
    recs = data if isinstance(data, list) else (
        data.get('flights') or data.get('aircraft') or data.get('ac') or [])
    seen = {(f.get('flight') or '').strip() for f in recs}
    return sorted(c for c in seen if c and c != '00000000')


def wav_to_audio(path, rate):
    raw = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'],
        capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


async def capture_ws(seconds, url=AUDIO_WS, on_status=print):
    """Capture `seconds` of float32 mono @ SOURCE_RATE from the pilnkradio WS.

    pilnkradio only pumps audio while playing:true (and emits zeros when squelch
    mutes). If nothing arrives, the receiver is stopped/tuned away — say so."""
    import websockets
    chunks, total, target = [], 0, SOURCE_RATE * seconds
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        while total < target:
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                on_status(f'[live] stream stalled at {total/SOURCE_RATE:.0f}s')
                break
            if isinstance(frame, (bytes, bytearray)):
                a = np.frombuffer(frame, dtype=np.float32)
                chunks.append(a); total += len(a)
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)


# ===========================================================================
# 5. Orchestration: chunk -> transcribe -> reconcile -> gate
# ===========================================================================
CHUNK_SECONDS = 5


def run_stream(tx, audio, in_rate, tracked, raw=False, on_line=None):
    """Walk a float32 stream in CHUNK_SECONDS windows. Returns (confirmed, heard).

    confirmed = [{'callsign','number','text','t'}]  -> the dashboard slide
    heard     = [{'text','t'}]                       -> full log / debug
    With raw=True nothing is suppressed by the reconcile (offline signal review).
    """
    confirmed, heard = [], []
    n = int(CHUNK_SECONDS * in_rate)
    last = ''
    for i in range(0, max(0, len(audio) - n + 1), n):
        ts = i / in_rate
        for t in tx.transcribe_chunk(audio[i:i + n], in_rate):
            if t.lower() == last:            # consecutive-duplicate suppression
                continue
            last = t.lower()
            heard.append({'text': t, 't': ts})
            cs, num = reconcile(t, tracked)
            if cs:
                fixed, changed = repair_airline(t, cs)
                rec = {'callsign': cs, 'number': num, 'text': fixed, 't': ts}
                if changed:
                    rec['raw_text'] = t          # keep what was actually heard, for audit
                confirmed.append(rec)
            if on_line:
                on_line(ts, t, cs, num)
    return confirmed, heard


def _mmss(ts):
    return f'{int(ts)//60:02d}:{int(ts)%60:02d}'


# ===========================================================================
# 6. CLI modes
# ===========================================================================
def cmd_selftest(_args):
    TRACKED = ['ANZ049M', 'ANZ808M', 'ANZ570', 'PLC1', 'ANZ618', 'TMN1',
               'JST298', 'PHA70', 'PHA73', 'JST262', 'ANZ021M', 'ANZ28']
    TESTS = [
        ('cleared wizz air five seven zero descend flight level two three eight', 'ANZ570'),
        ('jetstar two nine eight contact tower one one eight decimal seven',      'JST298'),
        ('csa six one eight roger fly heading three six zero',                    'ANZ618'),
        ('klm two six two ready for departure',                                   'JST262'),
        ('hotel papa alpha seven zero descend',                                   'PHA70'),
        ('five seven zero',                                                       'ANZ570'),
        ('fly heading three six zero maintain flight level two zero zero',        None),
        ('good morning tower',                                                    None),
        # glued-number recovery still works (prefix of 0495 -> 049)
        ('new zealand zero four nine five heavy descend',                         'ANZ049M'),
        # FREQUENCY GUARD: '121 decimal 0' is a freq, must NOT match ANZ021M (was the live false hit)
        ('contact ground one two one decimal zero',                              None),
        ('report established one one eight decimal seven',                        None),
        # SUFFIX GUARD: 128 must NOT match ANZ28 via an internal/suffix window
        ('maintain flight level one two eight',                                   None),
        # AIRLINE AGREEMENT: spoken operator matches the tracked one -> confirm
        ('jetstar two six two ready for departure',                               'JST262'),
        # AIRLINE CONFLICT: 'jetstar' is overhead but 618 is ANZ's -> refuse to guess
        ('jetstar six one eight roger',                                           None),
        # NOT-HERE operator: pure model bias, the number is still trustworthy
        ('lufthansa zero two one mike descend',                                   'ANZ021M'),
    ]
    ok = 0
    for txt, exp in TESTS:
        cs, num = reconcile(txt, TRACKED)
        ok += cs == exp
        print(f"{'OK ' if cs == exp else 'XX '} want={exp!s:8} got={cs!s:8} (num={num}) | {txt[:52]}")

    # --- airline repair -----------------------------------------------------
    REPAIRS = [
        ('csa six one eight roger fly heading three six zero', 'ANZ618',
         'new zealand six one eight roger fly heading three six zero'),
        # station name, NOT an operator -> must be left exactly as heard
        ('cleared to land praha radar two nine eight', 'JST298',
         'cleared to land praha radar two nine eight'),
        ('jetstar two nine eight contact tower', 'JST298',
         'jetstar two nine eight contact tower'),          # already right -> untouched
        ('five seven zero descend', 'ANZ570', 'five seven zero descend'),   # none named
    ]
    rok = 0
    print()
    for txt, cs, exp in REPAIRS:
        got, _ = repair_airline(txt, cs)
        rok += got == exp
        print(f"{'OK ' if got == exp else 'XX '} {cs} | {got[:66]}")

    total, good = len(TESTS) + len(REPAIRS), ok + rok
    print(f'\n{good}/{total} correct  (reconcile {ok}/{len(TESTS)}, repair {rok}/{len(REPAIRS)})')
    return 0 if good == total else 1


def cmd_file(args):
    tracked = fetch_tracked(args.flights) if args.flights else []
    raw = args.raw or not tracked
    print(f'[atc] loading {args.model}', flush=True)
    tx = ATCTranscriber(model=args.model, work_rate=args.work_rate)
    print(f'[atc] loaded in {tx.load_s:.1f}s  work_rate={tx.work_rate}Hz', flush=True)
    audio = wav_to_audio(args.path, tx.work_rate)
    if args.seconds:
        audio = audio[:int(args.seconds * tx.work_rate)]
    dur = len(audio) / tx.work_rate
    if tracked:
        print(f'[atc] reconciling against {len(tracked)} tracked: {tracked}', flush=True)
    print(f'[atc] {dur:.1f}s audio, chunk={CHUNK_SECONDS}s gate_rms={tx.GATE_RMS}', flush=True)

    def show(ts, t, cs, num):
        tag = f'   ✈ {cs} (matched {num})' if cs else ''
        print(f'  [{_mmss(ts)}] [{cs or "      "}] {t}{tag}', flush=True)

    t0 = time.time()
    confirmed, heard = run_stream(tx, audio, tx.work_rate, tracked, raw=raw, on_line=show)
    el = time.time() - t0
    print(f'\n--- done in {el:.1f}s ({dur/el:.1f}x realtime) | '
          f'{len(heard)} heard, {len(confirmed)} reconciled ---')
    if tracked and not raw:
        print(f'GATE: {len(confirmed)} would reach the slide, '
              f'{len(heard) - len(confirmed)} suppressed')
    return 0


def cmd_live(args):
    # 1. snapshot tracked traffic (reconcile target) up front
    try:
        tracked = fetch_tracked(args.flights)
    except Exception as e:
        print(f'[live] /flights unreachable ({e}) — running un-gated (raw)'); tracked = []
    print(f'[live] tracking at capture: {tracked}', flush=True)
    # 2. sanity: is the receiver actually pumping audio?
    try:
        st = json.load(urllib.request.urlopen(STATUS_URL, timeout=6))
        print(f"[live] receiver: playing={st.get('playing')} vfoHz={st.get('vfoHz')} "
              f"squelch={st.get('squelchEnabled')}@{st.get('squelchLevel')}", flush=True)
        if not st.get('playing'):
            print('[live] NOTE receiver is stopped — start playback + tune 124.300 '
                  'or no audio will arrive.', flush=True)
    except Exception as e:
        print(f'[live] status unreachable ({e})', flush=True)
    # 3. load model while (optionally) capturing
    print(f'[live] loading {args.model}', flush=True)
    tx = ATCTranscriber(model=args.model, work_rate=args.work_rate)
    print(f'[live] loaded in {tx.load_s:.1f}s — capturing {args.seconds}s', flush=True)
    src = asyncio.run(capture_ws(args.seconds, args.audio))
    if len(src) == 0:
        print('[live] NO AUDIO (receiver stopped/tuned away?)'); return 1
    nz = float(np.mean(np.abs(src) > 1e-4))
    print(f'[live] captured {len(src)/SOURCE_RATE:.0f}s | non-silent {nz*100:.0f}% '
          f'| peak {np.max(np.abs(src)):.3f}', flush=True)
    # 4. resample source 48k -> work_rate, then chunk/gate/transcribe/reconcile
    if tx.work_rate != SOURCE_RATE:
        src = scipy_signal.resample(src, int(len(src) * tx.work_rate / SOURCE_RATE)).astype(np.float32)

    def show(ts, t, cs, num):
        tag = f'   ✈ {cs} (matched {num})' if cs else ''
        print(f'  [{_mmss(ts)}] [{cs or "      "}] {t}{tag}', flush=True)

    confirmed, heard = run_stream(tx, src, tx.work_rate, tracked, raw=args.raw, on_line=show)
    print(f'\n[live] {len(heard)} transcribed, {len(confirmed)} reconciled to tracked traffic')
    for c in confirmed:
        print(f"   ✈ {c['callsign']} (matched {c['number']})  \"{c['text']}\"")
    return 0


def main():
    p = argparse.ArgumentParser(description='PiLNK live ATC speech-to-text')
    sub = p.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('selftest', help='reconcile unit tests (no model load)')
    sp.set_defaults(func=cmd_selftest)

    common = dict()
    fp = sub.add_parser('file', help='transcribe a WAV offline')
    fp.add_argument('path')
    fp.add_argument('seconds', nargs='?', type=float, default=None, help='limit (s)')
    fp.add_argument('--flights', nargs='?', const=FLIGHTS_URL, default=None,
                    help='reconcile against tracked traffic (default: un-gated raw)')
    fp.add_argument('--raw', action='store_true', help='never gate, show all heard lines')
    fp.set_defaults(func=cmd_file)

    lp = sub.add_parser('live', help='capture from the Pi5 and transcribe')
    lp.add_argument('seconds', nargs='?', type=int, default=90)
    lp.add_argument('--flights', default=FLIGHTS_URL)
    lp.add_argument('--audio', default=AUDIO_WS)
    lp.add_argument('--raw', action='store_true', help='do not gate on reconcile')
    lp.set_defaults(func=cmd_live)

    for q in (fp, lp):
        q.add_argument('--model', default=ATC_MODEL)
        q.add_argument('--work-rate', type=int, default=12000, dest='work_rate')

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
