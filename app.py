from flask import Flask, render_template, Response, jsonify, request, make_response
from flask_socketio import SocketIO
from radio import RadioStream
from whisper_atc import ATCWhisper
import subprocess
import queue
import threading
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pilnk_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

current_frequency = 118.7e6
current_gain      = 35
current_squelch   = 0

FREQUENCIES = {
    'ground':   {'name': 'AKL Ground',   'freq': 121.100},
    'tower':    {'name': 'AKL Tower',    'freq': 118.700},
    'approach': {'name': 'AKL Approach', 'freq': 124.300},
}

# ── Enable bias tee on RTL-SDR V4 to power LNA ───────────
def enable_bias_tee():
    try:
        result = subprocess.run(
            ['rtl_biast', '-d', '00000002', '-b', '1'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print('[PILNK] Bias tee enabled — LNA powered')
        else:
            print('[PILNK] Bias tee note:', result.stderr.strip())
    except FileNotFoundError:
        print('[PILNK] rtl_biast not found — skipping bias tee')
    except Exception as e:
        print('[PILNK] Bias tee error:', e)

# Small delay then enable bias tee in background
# (must run before rtl_fm starts using the device)
bias_thread = threading.Thread(target=enable_bias_tee, daemon=True)
bias_thread.start()
bias_thread.join(timeout=3)  # wait up to 3s

radio   = RadioStream()
whisper = ATCWhisper(socketio)
radio.subscribe(whisper.feed)
radio.start()
whisper.start()

@app.route('/')
def index():
    return render_template('index.html', frequencies=FREQUENCIES)

@app.route('/audio_feed')
def audio_feed():
    def generate():
        q = queue.Queue(maxsize=100)
        def on_chunk(chunk):
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass
        radio.subscribe(on_chunk)
        sox_cmd = [
            'sox',
            '-t', 'raw', '-r', '12k', '-e', 'signed', '-b', '16', '-c', '1', '-',
            '-t', 'ogg', '-C', '1', '-',
            'gain', str(current_gain),
            'lowpass', '3000',
            'highpass', '200'
        ]
        sox = subprocess.Popen(sox_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def feed_sox():
            while True:
                try:
                    chunk = q.get(timeout=2)
                    sox.stdin.write(chunk)
                    sox.stdin.flush()
                except queue.Empty:
                    continue
                except:
                    break
        feeder = threading.Thread(target=feed_sox, daemon=True)
        feeder.start()
        try:
            while True:
                chunk = sox.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            sox.terminate()
            sox.wait()
            radio.unsubscribe(on_chunk)
    return Response(generate(), mimetype='audio/ogg', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/set_frequency', methods=['POST'])
def set_frequency():
    global current_frequency
    data = request.json
    current_frequency = float(data.get('frequency', 118.7)) * 1e6
    radio.set_frequency(current_frequency)
    return jsonify({'status': 'ok', 'frequency': current_frequency})

@app.route('/set_gain', methods=['POST'])
def set_gain():
    global current_gain
    data = request.json
    current_gain = int(data.get('gain', 35))
    return jsonify({'status': 'ok', 'gain': current_gain})

@app.route('/set_squelch', methods=['POST'])
def set_squelch():
    global current_squelch
    data = request.json
    current_squelch = int(data.get('squelch', 0))
    return jsonify({'status': 'ok', 'squelch': current_squelch})

@app.route('/flights')
def flights():
    import urllib.request
    try:
        url = 'http://localhost:8080/data/aircraft.json'
        with urllib.request.urlopen(url, timeout=2) as r:
            return Response(r.read(), mimetype='application/json')
    except:
        return jsonify({'aircraft': []})

# ── OpenAIP proxy — avoids CORS issues in browser ─────────
@app.route('/api/openaip/<path:endpoint>')
def openaip_proxy(endpoint):
    OPENAIP_KEY = '7670c503a1c0929ee8e87ad581d9119e'
    params = request.args.to_dict()
    params['apiKey'] = OPENAIP_KEY
    url = f'https://api.core.openaip.net/api/{endpoint}'
    try:
        r = requests.get(url, params=params, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e), 'items': []}), 500

@app.route('/api/adsbdb/<path:callsign>')
def adsbdb_proxy(callsign):
    try:
        r = requests.get('https://api.adsbdb.com/v0/callsign/' + callsign, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── RainViewer proxy — avoids CORS issues in browser ──────
@app.route('/api/rainviewer')
def rainviewer_proxy():
    try:
        r = requests.get('https://api.rainviewer.com/public/weather-maps.json', timeout=10)
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Planespotters.net proxy — aircraft photos ──────────────
@app.route('/api/planespotters/<path:hex>')
def planespotters_proxy(hex):
    try:
        r = requests.get('https://api.planespotters.net/pub/photos/hex/' + hex, timeout=8)
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'photos': []}), 500

# ── METAR proxy ────────────────────────────────────────────
@app.route('/api/metar/<station>')
def metar_proxy(station):
    try:
        r = requests.get(
            'https://aviationweather.gov/api/data/metar?ids=' + station + '&format=json',
            timeout=10,
            headers={'User-Agent': 'PiLNK/1.0'}
        )
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify([]), 500

# ── FIDS stub — flight information display ────────────────
@app.route('/api/fids')
def fids():
    return jsonify([])

# ── Recordings ────────────────────────────────────────────
@app.route('/recordings')
def recordings():
    import os, glob
    rec_dir = os.path.join(os.path.dirname(__file__), 'recordings')
    os.makedirs(rec_dir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(rec_dir, '*.ogg')), reverse=True)
    total = sum(os.path.getsize(f) for f in files)
    def fmt_size(b):
        return f'{b/1024/1024:.1f} MB' if b > 1024*1024 else f'{b/1024:.0f} KB'
    recs = [{'name': os.path.basename(f),
             'size': fmt_size(os.path.getsize(f)),
             'time': os.path.getmtime(f)} for f in files[:20]]
    return jsonify({'recordings': recs, 'total_size': fmt_size(total)})

@app.route('/recordings/<path:filename>')
def serve_recording(filename):
    import os
    from flask import send_from_directory
    rec_dir = os.path.join(os.path.dirname(__file__), 'recordings')
    return send_from_directory(rec_dir, filename)

# ── Favicon ───────────────────────────────────────────────
@app.route('/favicon.ico')
def favicon():
    return '', 204

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
