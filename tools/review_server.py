#!/usr/bin/env python3
"""Manifest review helper — turn the STT collector's draft transcripts into clean NZ
training labels. Run it on the Pi5 (where the clips are), review from a laptop browser.

    ~/atc-stt-venv/bin/python review_server.py [--captures ~/atc-stt/captures] [--port 5058]
    -> open http://<pi5-ip>:5058/

Per clip: play it, fix the pre-filled draft to what was actually said, hit Enter to
save + advance. Corrections go to captures/reviewed.json (keyed by clip filename); the
raw manifest.jsonl is never touched. Only status=='good' feeds the fine-tune.

LISTENING AIDS (2026-08-09) — the controller sits ~5 dB over broadband AM hiss, which
is too marginal to verify by ear on the raw clip. The clips are DELIBERATELY stored raw
at true levels (training needs the real SNR), so the enhancement happens at PLAYBACK:

  enhanced   declip -> 250-3200 Hz voiceband -> spectral subtraction -> normalize.
             Same chain the model gets (atc_stt._preprocess), tuned harder for ears.
  trim       play only the speech regions (+padding), skipping the dead static.
  speed      0.5x/0.75x — ATC cadence is fast; slowing it is the cheapest win.
  level      per-clip speech-over-floor dB. NOTE (2026-08-10): validated against 104 human
             labels and it does NOT predict intelligibility (good-rate is flat across bands:
             36%/26%/35% vs a 33% baseline). Shown for context; never filter or delete on it.

Enhancement needs numpy+scipy (present in ~/atc-stt-venv). Without them the server still
runs and serves raw audio; the toggles just do nothing.

THE 'UNSURE' STATUS MATTERS: a clip you cannot actually make out must NOT be saved as
'good' — a wrong label teaches the fine-tune the wrong thing, which is worse than having
no clip at all. Mark it unsure (U); it stays in the pool for a second pass.
"""
import argparse
import io
import json
import os
import threading
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURES = MANIFEST = REVIEWED = CLIPS = STATSFILE = None
_lock = threading.Lock()
STATS = {}

try:
    import numpy as np
    from scipy import signal as ss
    DSP = True
except ImportError:                                     # stdlib fallback: raw audio only
    DSP = False


# --------------------------------------------------------------------------- audio
def read_wav(fp):
    with wave.open(fp, 'rb') as w:
        sr, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    return a, sr


def write_wav(a, sr):
    i16 = (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(i16.tobytes())
    return b.getvalue()


def enhance(a, sr):
    """Make the transmission audible: declip -> voiceband -> spectral subtraction ->
    normalize. Over-subtraction is pushed past the model's setting (1.5 -> 2.2) and the
    gain is smoothed over time, which trades a little 'musical noise' for a much lower
    hiss bed — the right trade for a human ear, not for the model."""
    clipped = np.abs(a) >= 0.98
    if clipped.any() and not clipped.all():             # declip (verbatim from atc_stt)
        idx = np.arange(len(a)); a = a.copy()
        a[clipped] = np.interp(idx[clipped], idx[~clipped], a[~clipped])
    a = ss.sosfilt(ss.butter(4, 250,  btype='high', fs=sr, output='sos'), a)
    a = ss.sosfilt(ss.butter(4, 3200, btype='low',  fs=sr, output='sos'), a)

    nper, nov = 512, 384
    _, _, Z = ss.stft(a, fs=sr, nperseg=nper, noverlap=nov)
    mag, phase = np.abs(Z), np.angle(Z)
    if mag.shape[1] >= 4:
        fe = np.sum(mag ** 2, axis=0)
        quiet = mag[:, fe <= np.percentile(fe, 20)]     # quietest frames == the hiss bed
        noise = np.mean(quiet, axis=1, keepdims=True) if quiet.size else np.zeros((mag.shape[0], 1))
        gain = np.maximum(mag - 2.2 * noise, 0.05 * mag) / np.maximum(mag, 1e-9)
        if gain.shape[1] >= 3:                          # smooth across time -> less warble
            gain = ss.medfilt(gain, kernel_size=(1, 3))
        mag = mag * gain
    _, out = ss.istft(mag * np.exp(1j * phase), fs=sr, nperseg=nper, noverlap=nov)
    out = np.pad(out, (0, max(0, len(a) - len(out))))[:len(a)].astype(np.float32)
    pk = float(np.abs(out).max())
    return (out * (0.89 / pk)).astype(np.float32) if pk > 1e-6 else out


def speech_spans(a, sr, pad=0.25):
    """Energy-gated speech regions [(start,end)] in samples. Threshold is floor+7 dB,
    so it adapts per clip instead of assuming an absolute level."""
    n = max(1, int(0.02 * sr))
    f = a[:len(a) // n * n].reshape(-1, n)
    if not len(f):
        return [(0, len(a))]
    e = 20 * np.log10(np.sqrt((f ** 2).mean(1)) + 1e-12)
    thr = np.percentile(e, 20) + 7.0
    hot = e > thr
    spans, i = [], 0
    while i < len(hot):
        if hot[i]:
            j = i
            while j < len(hot) and hot[j]:
                j += 1
            spans.append([i * n, min(len(a), j * n)])
            i = j
        else:
            i += 1
    p = int(pad * sr)
    spans = [[max(0, s - p), min(len(a), e_ + p)] for s, e_ in spans]
    merged = []
    for s in spans:                                     # merge overlaps after padding
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    merged = [m for m in merged if m[1] - m[0] >= int(0.15 * sr)]
    return merged or [(0, len(a))]


def clip_stats(fp):
    """(clarity_dB, speech_seconds) — clarity = speech level over the noise floor,
    measured AFTER enhancement, i.e. what the reviewer will actually hear."""
    a, sr = read_wav(fp)
    if len(a) < sr * 0.3:
        return 0.0, 0.0
    e = enhance(a, sr)
    n = max(1, int(0.02 * sr))
    f = e[:len(e) // n * n].reshape(-1, n)
    lv = 20 * np.log10(np.sqrt((f ** 2).mean(1)) + 1e-12)
    clarity = float(np.percentile(lv, 95) - np.percentile(lv, 20))
    secs = float(sum(s[1] - s[0] for s in speech_spans(e, sr)) / sr)
    return round(clarity, 1), round(secs, 1)


def load_stats():
    if os.path.exists(STATSFILE):
        try:
            return json.load(open(STATSFILE))
        except ValueError:
            pass
    return {}


_building = threading.Event()


def build_stats_async(items):
    """Kick off a clarity measurement pass in the background (never blocks a request —
    on the Pi5 a full pass over hundreds of clips takes minutes). Unmeasured clips just
    show no clarity badge until it lands; the collector keeps adding clips, so this is
    re-triggered whenever /api/items sees an unmeasured one."""
    if not DSP or _building.is_set():
        return
    if not [it for it in items if os.path.basename(it.get('audio', '')) not in STATS]:
        return
    _building.set()

    def run():
        try:
            build_stats(items)
        finally:
            _building.clear()
    threading.Thread(target=run, daemon=True).start()


def build_stats(items):
    """Compute clarity for any clip we haven't measured yet; cache to disk."""
    if not DSP:
        return
    global STATS
    STATS = dict(load_stats(), **STATS)
    todo = [it for it in items if os.path.basename(it.get('audio', '')) not in STATS]
    if not todo:
        return
    print('[review] measuring clarity for %d clip(s) …' % len(todo), flush=True)
    for it in todo:
        name = os.path.basename(it['audio'])
        fp = os.path.join(CLIPS, name)
        if not os.path.isfile(fp):
            continue
        try:
            c, s = clip_stats(fp)
            STATS[name] = {'clarity': c, 'speech': s}
        except Exception as ex:
            print('[review] stats failed on %s: %s' % (name, ex), flush=True)
    with _lock:
        tmp = STATSFILE + '.tmp'
        json.dump(STATS, open(tmp, 'w'))
        os.replace(tmp, STATSFILE)
    print('[review] clarity cached for %d clips' % len(STATS), flush=True)


# --------------------------------------------------------------------------- data
def load_manifest():
    items = []
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        pass
    return items


def load_reviewed():
    if os.path.exists(REVIEWED):
        try:
            return json.load(open(REVIEWED))
        except ValueError:
            return {}
    return {}


def save_reviewed(d):
    tmp = REVIEWED + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=0)
    os.replace(tmp, REVIEWED)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>ATC review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0e14;--card:#141922;--bd:#2a3140;--tx:#e6edf3;--dim:#8b97a7;--grn:#10b981;--blu:#3b82f6;--amb:#f59e0b;--red:#ef4444;--vio:#a78bfa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif;font-size:15px}
header{padding:12px 18px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:16px;position:sticky;top:0;background:var(--bg);flex-wrap:wrap}
h1{font-size:0.8rem;letter-spacing:0.2em;text-transform:uppercase;color:var(--blu);margin:0;font-weight:700}
.bar{flex:1;min-width:120px;height:8px;background:var(--card);border:1px solid var(--bd);border-radius:4px;overflow:hidden}
.bar>div{height:100%;background:var(--grn);width:0%}
.count{font-family:ui-monospace,monospace;font-size:0.8rem;color:var(--dim)}
.wrap{max-width:760px;margin:24px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px}
.meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px;font-size:0.8rem;color:var(--dim);font-family:ui-monospace,monospace}
.cs{color:var(--grn);border:1px solid var(--grn);background:rgba(16,185,129,.12);border-radius:5px;padding:2px 8px;font-weight:700}
audio{width:100%;margin:8px 0 4px}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 2px;font-size:0.72rem;color:var(--dim);font-family:ui-monospace,monospace}
.tools button{flex:0 0 auto;padding:5px 10px;font-size:0.68rem;letter-spacing:0.04em}
.on{border-color:var(--blu);color:var(--blu);background:rgba(59,130,246,.12)}
.draft{font-size:0.72rem;color:var(--dim);margin:10px 0 4px;font-family:ui-monospace,monospace}
.draft b{color:var(--amb)}
textarea{width:100%;min-height:70px;background:var(--bg);border:1px solid var(--bd);border-radius:8px;color:var(--tx);
  font-family:ui-monospace,monospace;font-size:1rem;padding:12px;line-height:1.5;resize:vertical}
textarea:focus{outline:none;border-color:var(--blu)}
.row{display:flex;gap:10px;margin-top:14px}
button{flex:1;padding:12px;border-radius:8px;border:1px solid var(--bd);background:transparent;color:var(--tx);
  font-weight:700;font-size:0.82rem;letter-spacing:0.05em;cursor:pointer}
.good{border-color:var(--grn);color:var(--grn)}.good:hover{background:rgba(16,185,129,.12)}
.unsure{border-color:var(--vio);color:var(--vio)}.unsure:hover{background:rgba(167,139,250,.12)}
.skip{border-color:var(--amb);color:var(--amb)}.skip:hover{background:rgba(245,158,11,.12)}
.nav{max-width:70px;color:var(--dim)}.nav:hover{background:var(--card)}
.hint{margin-top:12px;font-size:0.68rem;color:var(--dim);text-align:center;line-height:1.8}
.hint kbd{background:var(--bg);border:1px solid var(--bd);border-radius:4px;padding:1px 6px;font-family:ui-monospace,monospace}
.done{text-align:center;color:var(--grn);padding:40px;font-size:1.1rem}
.tag{font-size:0.62rem;padding:1px 7px;border-radius:4px}
.tag.r{background:rgba(16,185,129,.15);color:var(--grn)}.tag.s{background:rgba(245,158,11,.15);color:var(--amb)}
.tag.u{background:rgba(167,139,250,.15);color:var(--vio)}
.cl{font-size:0.62rem;padding:1px 7px;border-radius:4px;border:1px solid var(--bd);color:var(--dim)}
label.only,select{font-size:0.72rem;color:var(--dim);display:flex;align-items:center;gap:5px;cursor:pointer}
select{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:4px 6px;color:var(--tx)}
</style></head><body>
<header>
  <h1>ATC Review</h1>
  <div class="bar"><div id="prog"></div></div>
  <span class="count" id="count">…</span>
  <label class="only"><input type="checkbox" id="onlyNew" checked> unreviewed only</label>
  <select id="sort"><option value="ts">collection order</option><option value="clarity">loudest first</option></select>
</header>
<div class="wrap" id="wrap"><div class="card" id="card">loading…</div></div>
<script>
let ITEMS=[], VIEW=[], i=0, TOTAL=0, REV=0;
let ENH=true, TRIM=true, RATE=1.0;                 // listening aids persist across clips
const $=id=>document.getElementById(id);
async function load(){
  const d=await (await fetch('/api/items')).json();
  ITEMS=d.items; TOTAL=d.total; REV=d.reviewed; rebuild(); render();
}
function rebuild(){
  const onlyNew=$('onlyNew').checked;
  VIEW = onlyNew ? ITEMS.filter(x=>!x.reviewed) : ITEMS.slice();
  if($('sort').value==='clarity') VIEW.sort((a,b)=>(b.clarity||0)-(a.clarity||0));
  if(i>=VIEW.length) i=Math.max(0,VIEW.length-1);
}
function src(it){ return '/clips/'+encodeURIComponent(it.audio)+'?enh='+(ENH?1:0)+'&trim='+(TRIM?1:0); }
function render(){
  $('prog').style.width = TOTAL? (100*REV/TOTAL)+'%':'0%';
  $('count').textContent = REV+' / '+TOTAL+' reviewed';
  if(!VIEW.length){ $('card').className=''; $('card').innerHTML='<div class="done">✓ nothing to review here.<br><small style="color:var(--dim)">toggle “unreviewed only” to revisit.</small></div>'; return; }
  const it=VIEW[i];
  const cs=(it.callsigns||[]).map(c=>'<span class="cs">✈ '+esc(c)+'</span>').join(' ');
  const st=it.status, tagcls=st==='skip'?'s':(st==='unsure'?'u':'r');
  const tagtxt=st==='skip'?'unusable':(st==='unsure'?'unsure':'reviewed');
  const tag=it.reviewed?('<span class="tag '+tagcls+'">'+tagtxt+'</span>'):'';
  // NO COLOUR CODING — validated against 104 human labels on 2026-08-10 and this number
  // does NOT predict intelligibility (good-rate by band: 36% / 26% / 35% — flat vs a 33%
  // baseline; best of 5 acoustic features separated good from unintelligible by 0.67σ).
  // It is a SIGNAL-LEVEL reading, shown for context only. A red/green badge here would be
  // a false signal that biases the labelling it is supposed to support.
  const c=it.clarity;
  const cl=(c!=null)?('<span class="cl">level '+c.toFixed(1)+' dB · '+(it.speech||0).toFixed(1)+'s speech</span>'):'';
  const cur = it.reviewed && it.corrected!=null ? it.corrected : it.draft;
  $('card').className='card';
  $('card').innerHTML =
    '<div class="meta">'+cs+' <span>'+(it.freq_mhz||'')+' MHz</span> <span>'+fmt(it.ts)+'</span> '+cl+' '+tag+' <span style="margin-left:auto">item '+(i+1)+' / '+VIEW.length+'</span></div>'+
    '<audio id="au" controls autoplay src="'+src(it)+'"></audio>'+
    '<div class="tools">'+
      '<button id="bE" class="'+(ENH?'on':'')+'" onclick="tog(\'e\')">enhanced</button>'+
      '<button id="bT" class="'+(TRIM?'on':'')+'" onclick="tog(\'t\')">speech only</button>'+
      '<span style="margin-left:6px">speed</span>'+
      '<button class="'+(RATE==1?'on':'')+'" onclick="setRate(1)">1×</button>'+
      '<button class="'+(RATE==0.75?'on':'')+'" onclick="setRate(0.75)">0.75×</button>'+
      '<button class="'+(RATE==0.5?'on':'')+'" onclick="setRate(0.5)">0.5×</button>'+
      '<button onclick="replay()" style="margin-left:auto">↻ replay</button>'+
    '</div>'+
    '<div class="draft">model draft: <b>'+esc(it.draft||'(none)')+'</b></div>'+
    '<textarea id="tx" spellcheck="false"></textarea>'+
    '<div class="row"><button class="nav" onclick="go(-1)">‹ back</button>'+
    '<button class="good" onclick="save(\'good\')">✓ Good — save &amp; next</button>'+
    '<button class="unsure" onclick="save(\'unsure\')">? Can\'t tell</button>'+
    '<button class="skip" onclick="save(\'skip\')">✕ Unusable</button></div>'+
    '<div class="hint"><kbd>Enter</kbd> save &amp; next · <kbd>U</kbd> can\'t tell · <kbd>S</kbd> unusable · <kbd>R</kbd> replay · <kbd>E</kbd> enhanced · <kbd>T</kbd> speech-only · <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> speed · <kbd>←</kbd>/<kbd>→</kbd> move'+
    '<br><span style="color:var(--vio)">only “Good” becomes training data — if you can’t make it out, press U rather than guessing.</span></div>';
  $('tx').value=cur||''; $('tx').focus();
  const a=$('au'); a.playbackRate=RATE;
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(ts){ if(!ts)return''; const d=new Date(ts*1000); const p=n=>('0'+n).slice(-2); return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds()); }
function go(d){ i=Math.max(0,Math.min(VIEW.length-1,i+d)); render(); }
function tog(w){ if(w==='e')ENH=!ENH; else TRIM=!TRIM; const a=$('au'); const it=VIEW[i];
  a.src=src(it); a.playbackRate=RATE; a.play(); $('bE').className=ENH?'on':''; $('bT').className=TRIM?'on':''; }
function setRate(r){ RATE=r; const a=$('au'); if(a){a.playbackRate=r;} render(); }
function replay(){ const a=$('au'); if(a){a.currentTime=0;a.playbackRate=RATE;a.play();} }
async function save(status){
  const it=VIEW[i]; if(!it)return;
  const text=$('tx').value.trim();
  const r=await (await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({audio:it.audio,text:text,status:status})})).json();
  REV=r.reviewed;
  const orig=ITEMS.find(x=>x.audio===it.audio); if(orig){orig.reviewed=true;orig.corrected=text;orig.status=status;}
  if($('onlyNew').checked){ VIEW.splice(i,1); if(i>=VIEW.length)i=VIEW.length-1; }
  else { i=Math.min(VIEW.length-1,i+1); }
  render();
}
document.addEventListener('keydown',e=>{
  const inTx = e.target.id==='tx';
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); save('good'); return; }
  if(e.key==='r'||e.key==='R'){ if(!inTx||e.ctrlKey){ replay(); } return; }
  if(inTx) return;                                   // let plain typing reach the textarea
  if(e.key==='u'||e.key==='U'){ save('unsure'); }
  else if(e.key==='s'||e.key==='S'){ save('skip'); }
  else if(e.key==='e'||e.key==='E'){ tog('e'); }
  else if(e.key==='t'||e.key==='T'){ tog('t'); }
  else if(e.key==='1'){ setRate(1); } else if(e.key==='2'){ setRate(0.75); } else if(e.key==='3'){ setRate(0.5); }
  else if(e.key==='ArrowLeft'){ go(-1); }
  else if(e.key==='ArrowRight'){ go(1); }
});
$('onlyNew').addEventListener('change',()=>{i=0;rebuild();render();});
$('sort').addEventListener('change',()=>{i=0;rebuild();render();});
load();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        p = urllib.parse.unquote(parts.path)
        q = urllib.parse.parse_qs(parts.query)
        if p in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', PAGE.encode())
        elif p == '/api/items':
            items, rev = load_manifest(), load_reviewed()
            build_stats_async(items)
            out = []
            for it in items:
                name = os.path.basename(it.get('audio', ''))
                r = rev.get(name, {})
                s = STATS.get(name, {})
                out.append({'audio': name, 'draft': it.get('text', ''),
                            'callsigns': it.get('callsigns', []), 'freq_mhz': it.get('freq_mhz'),
                            'ts': it.get('ts'), 'reviewed': bool(r),
                            'corrected': r.get('text'), 'status': r.get('status'),
                            'clarity': s.get('clarity'), 'speech': s.get('speech')})
            self._send(200, 'application/json',
                       json.dumps({'items': out, 'reviewed': len(rev), 'total': len(items)}).encode())
        elif p.startswith('/clips/'):
            name = os.path.basename(p[len('/clips/'):])
            fp = os.path.join(CLIPS, name)
            if not os.path.isfile(fp):
                return self._send(404, 'text/plain', b'not found')
            want_enh = q.get('enh', ['0'])[0] == '1'
            want_trim = q.get('trim', ['0'])[0] == '1'
            if not DSP or not (want_enh or want_trim):
                with open(fp, 'rb') as f:
                    return self._send(200, 'audio/wav', f.read())
            try:
                a, sr = read_wav(fp)
                proc = enhance(a, sr) if want_enh else a
                if want_trim:
                    spans = speech_spans(proc if want_enh else enhance(a, sr), sr)
                    proc = np.concatenate([proc[s:e] for s, e in spans]) if spans else proc
                return self._send(200, 'audio/wav', write_wav(proc, sr))
            except Exception as ex:                     # never let DSP break playback
                print('[review] enhance failed on %s: %s' % (name, ex), flush=True)
                with open(fp, 'rb') as f:
                    return self._send(200, 'audio/wav', f.read())
        else:
            self._send(404, 'text/plain', b'not found')

    def do_POST(self):
        if self.path == '/api/review':
            n = int(self.headers.get('Content-Length', 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                return self._send(400, 'text/plain', b'bad json')
            name = os.path.basename(body.get('audio', ''))
            if not name:
                return self._send(400, 'text/plain', b'no audio')
            status = body.get('status', 'good')
            if status not in ('good', 'unsure', 'skip'):
                status = 'good'
            with _lock:
                rev = load_reviewed()
                rev[name] = {'text': (body.get('text') or '').strip(), 'status': status}
                save_reviewed(rev)
                cnt = len(rev)
            self._send(200, 'application/json', json.dumps({'ok': True, 'reviewed': cnt}).encode())
        else:
            self._send(404, 'text/plain', b'not found')

    def log_message(self, *a):
        pass


def main():
    global CAPTURES, MANIFEST, REVIEWED, CLIPS, STATSFILE
    ap = argparse.ArgumentParser()
    ap.add_argument('--captures', default=os.path.expanduser('~/atc-stt/captures'))
    ap.add_argument('--port', type=int, default=5058)
    a = ap.parse_args()
    CAPTURES = a.captures
    MANIFEST = os.path.join(CAPTURES, 'manifest.jsonl')
    REVIEWED = os.path.join(CAPTURES, 'reviewed.json')
    CLIPS = os.path.join(CAPTURES, 'clips')
    STATSFILE = os.path.join(CAPTURES, 'clipstats.json')
    items = load_manifest()
    print('[review] captures=%s  (%d clips)  reviewed=%d  dsp=%s'
          % (CAPTURES, len(items), len(load_reviewed()), 'on' if DSP else 'OFF (no numpy/scipy)'))
    build_stats_async(items)
    print('[review] open  http://<this-host>:%d/' % a.port)
    ThreadingHTTPServer(('0.0.0.0', a.port), H).serve_forever()


if __name__ == '__main__':
    main()
