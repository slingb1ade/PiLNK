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
current_agc       = False
current_nr        = False
current_bw_low    = 3000
current_bw_high   = 200

FREQUENCIES = {
    'ground':   {'name': 'AKL Ground',   'freq': 121.100},
    'tower':    {'name': 'AKL Tower',    'freq': 118.700},
    'approach': {'name': 'AKL Approach', 'freq': 124.300},
}

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
            'lowpass', str(current_bw_low),
            'highpass', str(current_bw_high),
        ]
        # Add noise reduction if enabled (sox noisered requires a profile — use compand as a simpler alternative)
        if current_nr:
            sox_cmd += ['compand', '0.1,0.2', '-inf,-50.1,-inf,-50,-50', '0', '-90', '0.1']
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

@app.route('/set_bandwidth', methods=['POST'])
def set_bandwidth():
    global current_bw_low, current_bw_high
    data = request.json
    current_bw_low  = int(data.get('low',  3000))
    current_bw_high = int(data.get('high', 200))
    return jsonify({'status': 'ok', 'low': current_bw_low, 'high': current_bw_high})

@app.route('/set_nr', methods=['POST'])
def set_nr():
    global current_nr
    data = request.json
    current_nr = bool(data.get('nr', False))
    return jsonify({'status': 'ok', 'nr': current_nr})

@app.route('/set_agc', methods=['POST'])
def set_agc():
    global current_agc, current_gain
    data = request.json
    current_agc = bool(data.get('agc', False))
    if current_agc:
        # Set RTL-SDR to auto gain via radio module
        current_gain = 0  # 0 = auto gain for rtl_fm
        radio.set_gain(0)
    return jsonify({'status': 'ok', 'agc': current_agc})

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

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
