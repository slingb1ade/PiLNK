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
        current_gain = 0
        radio.set_gain(0)
    return jsonify({'status': 'ok', 'agc': current_agc})

# ── Signal level for squelch-aware scanner ─────────────────
@app.route('/get_signal_level')
def get_signal_level():
    try:
        level = radio.get_signal_level()
        active = current_squelch == 0 or level > current_squelch
    except:
        active = False
    return jsonify({'active': active})

# ── Recordings ─────────────────────────────────────────────
import os
from datetime import datetime

RECORDINGS_DIR = os.path.expanduser('~/pilnk/recordings')
MAX_RECORDINGS_MB = 500
current_recording_file = None

os.makedirs(RECORDINGS_DIR, exist_ok=True)

def get_recordings_size_mb():
    total = sum(
        os.path.getsize(os.path.join(RECORDINGS_DIR, f))
        for f in os.listdir(RECORDINGS_DIR)
        if os.path.isfile(os.path.join(RECORDINGS_DIR, f))
    )
    return total / (1024 * 1024)

def cleanup_old_recordings():
    while get_recordings_size_mb() > MAX_RECORDINGS_MB:
        files = sorted(
            [os.path.join(RECORDINGS_DIR, f) for f in os.listdir(RECORDINGS_DIR)],
            key=os.path.getmtime
        )
        if files:
            os.remove(files[0])
        else:
            break

@app.route('/start_recording', methods=['POST'])
def start_recording():
    global current_recording_file
    data = request.json
    freq = str(data.get('frequency', '118.700')).replace('.', '').replace(' ', '')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = 'atc_{}_{}.ogg'.format(freq, ts)
    filepath = os.path.join(RECORDINGS_DIR, filename)
    try:
        radio.start_recording(filepath)
        current_recording_file = filename
        cleanup_old_recordings()
        return jsonify({'status': 'ok', 'filename': filename})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stop_recording', methods=['POST'])
def stop_recording():
    global current_recording_file
    try:
        radio.stop_recording()
    except:
        pass
    current_recording_file = None
    return jsonify({'status': 'ok'})

@app.route('/recordings')
def list_recordings():
    try:
        files = []
        for f in sorted(os.listdir(RECORDINGS_DIR),
                        key=lambda x: os.path.getmtime(os.path.join(RECORDINGS_DIR, x)),
                        reverse=True):
            if f.endswith('.ogg') or f.endswith('.mp3'):
                fp = os.path.join(RECORDINGS_DIR, f)
                size_mb = os.path.getsize(fp) / (1024*1024)
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                files.append({
                    'name': f,
                    'size': '{:.1f} MB'.format(size_mb),
                    'date': mtime.strftime('%d %b %H:%M')
                })
        total_mb = get_recordings_size_mb()
        return jsonify({
            'recordings': files,
            'total_size': '{:.0f} MB / {}MB max'.format(total_mb, MAX_RECORDINGS_MB)
        })
    except Exception as e:
        return jsonify({'recordings': [], 'total_size': '0 MB'})

@app.route('/recordings/<filename>')
def download_recording(filename):
    from flask import send_from_directory
    return send_from_directory(RECORDINGS_DIR, filename, as_attachment=True)

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

# ── AeroDataBox FIDS proxy ─────────────────────────────────
AERODATABOX_KEY = '1b21053b5cmsha60f2e2a02b5dcep19d59bjsn94d2eba60b85'

@app.route('/api/fids')
def fids_proxy():
    try:
        url = 'https://aerodatabox.p.rapidapi.com/flights/airports/icao/NZAA'
        params = {
            'offsetMinutes': '0',
            'durationMinutes': '120',
            'withLeg': 'true',
            'withCancelled': 'true',
            'withCodeshared': 'false',
            'withCargo': 'false',
            'withPrivate': 'false'
        }
        headers = {
            'x-rapidapi-host': 'aerodatabox.p.rapidapi.com',
            'x-rapidapi-key': AERODATABOX_KEY
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'departures': [], 'arrivals': [], 'error': str(e)}), 500

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
