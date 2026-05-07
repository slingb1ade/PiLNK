from flask import Flask, render_template, Response, jsonify, request, make_response
from flask_cors import CORS
from flask_socketio import SocketIO
from sdr_controller import SDRController
# from whisper_atc  # disabled until v2.0 import ATCWhisper
import subprocess
import queue
import threading
import requests
import time
import collections
import json
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = 'pilnk_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Read location — config.json is authoritative (installer writes it).
# /etc/default/dump1090-fa is a legacy fallback for pre-0.1.7 installs.
# If neither is set we return None — caller guards, and node pings without
# coordinates rather than silently falsifying location (GLOBAL BY DEFAULT).
import re

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

def _load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()

def read_receiver_location():
    # 1. config.json (authoritative, written by installer)
    try:
        lat = _config.get('lat')
        lon = _config.get('lon')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    except (TypeError, ValueError):
        pass
    # 2. Legacy: /etc/default/dump1090-fa (pre-0.1.7 installs)
    try:
        with open('/etc/default/dump1090-fa', 'r') as f:
            content = f.read()
        lat_m = re.search(r'RECEIVER_LAT=([^\n]+)', content)
        lon_m = re.search(r'RECEIVER_LON=([^\n]+)', content)
        if lat_m and lon_m:
            return float(lat_m.group(1)), float(lon_m.group(1))
    except Exception:
        pass
    # 3. Unknown — caller must handle None
    return None, None

RX_LAT, RX_LON = read_receiver_location()
if RX_LAT is None or RX_LON is None:
    print('[PILNK] WARNING: Receiver location not set. Add lat/lon to config.json or re-run the installer.')

# ── Flight trail history — stores last 24h of positions ───
# { hex: deque([ {lat, lon, alt_baro, baro_rate, flight, t} ]) }
TRAIL_HISTORY = collections.defaultdict(lambda: collections.deque(maxlen=500))
TRAIL_LOCK = threading.Lock()
MAX_TRAIL_AGE = 24 * 3600  # 24 hours in seconds

def record_trails():
    while True:
        try:
            import urllib.request
            url = 'http://localhost:8080/data/aircraft.json'
            with urllib.request.urlopen(url, timeout=2) as r:
                import json
                data = json.loads(r.read())
                now = time.time()
                with TRAIL_LOCK:
                    for a in data.get('aircraft', []):
                        if a.get('lat') and a.get('lon'):
                            TRAIL_HISTORY[a['hex']].append({
                                'lat': a.get('lat'),
                                'lon': a.get('lon'),
                                'alt_baro': a.get('alt_baro', 0),
                                'baro_rate': a.get('baro_rate', 0),
                                'flight': a.get('flight', '').strip(),
                                't': now
                            })
                    # Clean old entries
                    cutoff = now - MAX_TRAIL_AGE
                    for hex in list(TRAIL_HISTORY.keys()):
                        while TRAIL_HISTORY[hex] and TRAIL_HISTORY[hex][0]['t'] < cutoff:
                            TRAIL_HISTORY[hex].popleft()
                        if not TRAIL_HISTORY[hex]:
                            del TRAIL_HISTORY[hex]
        except:
            pass
        time.sleep(10)  # Record every 10 seconds

# Start trail recorder thread
trail_thread = threading.Thread(target=record_trails, daemon=True)
trail_thread.start()

# ── PiLNK.io server ping — sends aircraft data + stats every 30s
# PiLNK Code is read from config.json (created by installer, gitignored)
def _load_pilnk_code():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
            code = cfg.get('pilnk_code', '').strip()
            if code:
                return code
    except Exception:
        pass
    return 'YOUR_VERIFY_CODE_HERE'

NODE_VERIFY_CODE = _load_pilnk_code()
# Set this to your node's verify code from your profile page

# Stats tracker (computed server-side for profile display)
node_stats = {
    'today': time.strftime('%Y-%m-%d'),
    'seen_hexes': set(),
    'total_today': 0,
    'fastest': None,
    'highest': None,
    'furthest': None,
    'squawk': None,
    'type_counts': {},
    'hour_counts': [0] * 24,
    'phases': {'climbing': 0, 'cruising': 0, 'descending': 0, 'approach': 0}
}
node_stats_lock = threading.Lock()

def compute_node_stats(aircraft):
    """Update running stats from current aircraft snapshot."""
    import math
    with node_stats_lock:
        # Reset if new day
        today = time.strftime('%Y-%m-%d')
        if node_stats['today'] != today:
            node_stats['today'] = today
            node_stats['seen_hexes'] = set()
            node_stats['total_today'] = 0
            node_stats['fastest'] = None
            node_stats['highest'] = None
            node_stats['furthest'] = None
            node_stats['squawk'] = None
            node_stats['type_counts'] = {}
            node_stats['hour_counts'] = [0] * 24
            node_stats['phases'] = {'climbing': 0, 'cruising': 0, 'descending': 0, 'approach': 0}

        # Live phases
        climbing = cruising = descending = approach = 0
        hour = int(time.strftime('%H'))

        for ac in aircraft:
            alt_raw = ac.get('alt_baro', 0) or ac.get('alt', 0) or 0; alt = 0 if alt_raw == 'ground' else int(alt_raw)
            rate = int(ac.get('baro_rate', 0) or 0)
            speed = int(ac.get('gs', 0) or 0)
            cs = (ac.get('flight', '') or ac.get('hex', '')).strip()
            hex_code = ac.get('hex', '')
            ac_type = (ac.get('t', '') or ac.get('type', '') or '').upper().strip() or 'UNKNOWN'
            squawk = ac.get('squawk', '') or ''
            lat = float(ac.get('lat', 0) or 0)
            lon = float(ac.get('lon', 0) or 0)

            # Phase
            if alt < 3000 and rate < 0:
                approach += 1
            elif rate > 200:
                climbing += 1
            elif rate < -200:
                descending += 1
            else:
                cruising += 1

            # Unique tracking
            if hex_code and hex_code not in node_stats['seen_hexes']:
                node_stats['seen_hexes'].add(hex_code)
                node_stats['total_today'] += 1

            # Fastest
            if speed > 0 and (not node_stats['fastest'] or speed > node_stats['fastest']['val']):
                node_stats['fastest'] = {'cs': cs, 'val': speed}

            # Highest
            if alt > 0 and (not node_stats['highest'] or alt > node_stats['highest']['val']):
                node_stats['highest'] = {'cs': cs, 'val': alt}

            # Furthest (haversine in nm) — requires receiver location
            if lat and lon and RX_LAT is not None and RX_LON is not None:
                dLat = math.radians(lat - RX_LAT)
                dLon = math.radians(lon - RX_LON)
                a = math.sin(dLat/2)**2 + math.cos(math.radians(RX_LAT)) * math.cos(math.radians(lat)) * math.sin(dLon/2)**2
                dist = round(3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
                if dist > 0 and (not node_stats['furthest'] or dist > node_stats['furthest']['val']):
                    node_stats['furthest'] = {'cs': cs, 'val': dist}

            # Squawk
            if squawk and squawk not in ('1200', '0000'):
                priority = 4 if squawk == '7500' else 3 if squawk == '7700' else 2 if squawk == '7600' else 1
                if not node_stats['squawk'] or priority > node_stats['squawk'].get('priority', 0):
                    node_stats['squawk'] = {'cs': cs, 'val': squawk, 'priority': priority}

            # Types
            type_key = hex_code + '-' + ac_type
            if hex_code and type_key not in node_stats.get('_type_seen', set()):
                node_stats.setdefault('_type_seen', set()).add(type_key)
                node_stats['type_counts'][ac_type] = node_stats['type_counts'].get(ac_type, 0) + 1

        node_stats['phases'] = {'climbing': climbing, 'cruising': cruising, 'descending': descending, 'approach': approach}

        # Hourly peak
        if len(aircraft) > node_stats['hour_counts'][hour]:
            node_stats['hour_counts'][hour] = len(aircraft)


def get_stats_payload():
    """Get stats as a JSON-safe dict for the ping payload."""
    with node_stats_lock:
        # Load all-time records
        records = {}
        try:
            if os.path.exists(STATS_RECORDS_FILE):
                with open(STATS_RECORDS_FILE, 'r') as f:
                    records = json.load(f)
        except Exception:
            pass

        # Top 5 types
        sorted_types = sorted(node_stats['type_counts'].items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            'phases': node_stats['phases'],
            'total_today': node_stats['total_today'],
            'fastest': node_stats['fastest'],
            'highest': node_stats['highest'],
            'furthest': node_stats['furthest'],
            'squawk': node_stats['squawk'],
            'top_types': [{'type': t, 'count': c} for t, c in sorted_types],
            'hour_counts': node_stats['hour_counts'],
            'records': records
        }


def ping_server():
    while True:
        try:
            import urllib.request
            # Grab current aircraft from dump1090
            aircraft = []
            try:
                with urllib.request.urlopen('http://localhost:8080/data/aircraft.json', timeout=3) as r:
                    data = json.loads(r.read())
                    for a in data.get('aircraft', []):
                        if a.get('lat'):
                            aircraft.append({
                                'hex': a.get('hex', ''),
                                'flight': a.get('flight', '').strip(),
                                'alt': a.get('alt_baro', 0),
                                'alt_baro': a.get('alt_baro', 0),
                                'gs': a.get('gs', 0),
                                'lat': a.get('lat', 0),
                                'lon': a.get('lon', 0),
                                'squawk': a.get('squawk', ''),
                                'baro_rate': a.get('baro_rate', 0),
                                't': a.get('t', ''),
                                'track': a.get('track', 0)
                            })
            except:
                pass

            # Compute stats
            compute_node_stats(aircraft)

            # Send to pilnk.io
            payload = json.dumps({
                'action': 'ping',
                'verify_code': NODE_VERIFY_CODE,
                'aircraft_count': len(aircraft),
                'aircraft': aircraft,
                'node_stats': get_stats_payload(),
                'version': _get_local_version(),
                'lat': RX_LAT,
                'lon': RX_LON
            }).encode()
            req = urllib.request.Request(
                'https://pilnk.io/api/node.php',
                data=payload,
                headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'}
            )
            urllib.request.urlopen(req, timeout=10)
            print(f'[PILNK] Ping sent — {len(aircraft)} aircraft')
        except Exception as e:
            print(f'[PILNK] Ping failed: {e}')
        time.sleep(30)

# Start ping thread
if NODE_VERIFY_CODE != 'YOUR_VERIFY_CODE_HERE':
    ping_thread = threading.Thread(target=ping_server, daemon=True)
    ping_thread.start()
    print('[PILNK] Server ping active — reporting to pilnk.io')
else:
        print('[PILNK] Set your PiLNK Code in config.json or re-run the installer')

# ── OTA Update System ─────────────────────────────────────
PILNK_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(PILNK_DIR, 'VERSION')
UPDATE_SCRIPT = os.path.join(PILNK_DIR, 'update.sh')
OTA_CHECK_INTERVAL = 3600  # 1 hour
OTA_COOLDOWN = 3600       # 1 hour after update before re-checking
ota_last_update = 0
ota_status = {'available': False, 'current': '', 'latest': '', 'last_check': 0, 'updating': False}

def _get_local_version():
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return '0.0.0'

def _is_auto_update_enabled():
    config_path = os.path.join(PILNK_DIR, 'config.json')
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
            return cfg.get('auto_update', True)  # default on for beta
    except:
        return True

def _run_update():
    global ota_last_update
    ota_status['updating'] = True
    print('[PILNK-OTA] Starting update...')
    try:
        result = subprocess.run(
            ['bash', UPDATE_SCRIPT],
            capture_output=True, text=True, timeout=120,
            cwd=PILNK_DIR
        )
        print(f'[PILNK-OTA] Update script output: {result.stdout}')
        if result.stderr:
            print(f'[PILNK-OTA] Update stderr: {result.stderr}')
        ota_last_update = time.time()
        ota_status['updating'] = False
        # Note: if update.sh restarts the service, this code won't reach here
        # That's fine — the new instance will start fresh
        return result.returncode == 0
    except Exception as e:
        print(f'[PILNK-OTA] Update failed: {e}')
        ota_status['updating'] = False
        return False

def ota_checker():
    global ota_last_update
    time.sleep(60)  # wait 60s after startup before first check
    while True:
        try:
            # Cooldown check
            if time.time() - ota_last_update < OTA_COOLDOWN:
                time.sleep(OTA_CHECK_INTERVAL)
                continue

            # Fetch latest version from pilnk.io
            import urllib.request
            req = urllib.request.Request(
                'https://pilnk.io/api/version.php',
                headers={'User-Agent': 'PiLNK-OTA/1.0'}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())

            local_ver = _get_local_version()
            remote_ver = data.get('version', '')
            required = data.get('required', False)

            ota_status['current'] = local_ver
            ota_status['latest'] = remote_ver
            ota_status['last_check'] = time.time()

            if remote_ver and remote_ver != local_ver:
                ota_status['available'] = True
                print(f'[PILNK-OTA] Update available: {local_ver} → {remote_ver}')

                if _is_auto_update_enabled() or required:
                    print(f'[PILNK-OTA] Auto-update enabled — applying update')
                    _run_update()
                else:
                    print(f'[PILNK-OTA] Auto-update disabled — skipping')
            else:
                ota_status['available'] = False

        except Exception as e:
            print(f'[PILNK-OTA] Check failed: {e}')

        time.sleep(OTA_CHECK_INTERVAL)

# Start OTA checker thread
ota_thread = threading.Thread(target=ota_checker, daemon=True)
ota_thread.start()
print('[PILNK-OTA] Update checker active — checking every 5 minutes')

current_frequency = 118.7e6
current_gain      = 35
current_squelch   = 50

# ── SDR Controller — manages VHF audio via rtl_fm ─────────
# Identifies VHF dongle by USB serial (default '00000002') so the
# audio path can never accidentally clobber the ADS-B dongle.
sdr = SDRController()

@app.route('/')
def index():
    return render_template('index.html')

# ── Audio API — controls VHF radio via SDR controller ─────
@app.route('/audio/start', methods=['POST'])
def audio_start():
    data = request.get_json() or {}
    freq = data.get('freq', sdr.frequency)
    squelch = data.get('squelch', sdr.squelch)
    gain = data.get('gain', sdr.gain)
    sdr.squelch = int(squelch)
    sdr.gain = int(gain)
    ok = sdr.start(freq_hz=int(freq))
    return jsonify({'success': ok, 'status': sdr.get_status()})

@app.route('/audio/stop', methods=['POST'])
def audio_stop():
    sdr.stop()
    return jsonify({'success': True, 'status': sdr.get_status()})

@app.route('/audio/freq', methods=['POST'])
def audio_freq():
    data = request.get_json() or {}
    freq = data.get('freq')
    if not freq:
        return jsonify({'error': 'freq required'}), 400
    sdr.set_frequency(int(freq))
    return jsonify({'success': True, 'status': sdr.get_status()})

@app.route('/audio/squelch', methods=['POST'])
def audio_squelch():
    data = request.get_json() or {}
    level = data.get('level', 50)
    sdr.set_squelch(int(level))
    return jsonify({'success': True, 'status': sdr.get_status()})

@app.route('/audio/gain', methods=['POST'])
def audio_gain():
    data = request.get_json() or {}
    gain = data.get('gain', 35)
    sdr.set_gain(int(gain))
    return jsonify({'success': True, 'status': sdr.get_status()})

@app.route('/audio/stream')
def audio_stream():
    """Stream live VHF audio as Ogg/Opus to a browser <audio> element.
    Multiple clients can connect concurrently; each gets its own
    bounded queue. The ffmpeg pipeline is shared, so additional
    listeners cost only the queue + HTTP overhead.
    """
    if not sdr.is_playing:
        return jsonify({'error': 'audio pipeline not started — POST /audio/start first'}), 503

    import queue as _q  # local import to avoid polluting module globals
    sub = sdr.subscribe()

    def generate():
        try:
            while True:
                try:
                    chunk = sub.get(timeout=2.0)
                except _q.Empty:
                    if not sdr.is_playing:
                        break
                    continue
                if not chunk:
                    continue
                yield chunk
        finally:
            sdr.unsubscribe(sub)

    return Response(
        generate(),
        mimetype='audio/ogg',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/audio/status')
def audio_status():
    return jsonify(sdr.get_status())

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
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        print('TAF ERROR:', str(e))
        return jsonify({'error': str(e)}), 500

# ── TAF proxy ──────────────────────────────────────────────
@app.route('/api/taf/<station>')
def taf_proxy(station):
    try:
        r = requests.get(
            'https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/' + station + '.TXT',
            timeout=10
        )
        resp = make_response(jsonify({'raw': r.text}))
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        print('TAF ERROR:', str(e))
        return jsonify({'error': str(e)}), 500

# ── PiLNK.io API Proxy — avoids CORS from local IP ───────
@app.route('/api/pilnkio/<path:endpoint>', methods=['GET','POST','OPTIONS'])
def pilnkio_proxy(endpoint):
    url = 'https://pilnk.io/api/' + endpoint
    try:
        if request.method == 'POST':
            r = requests.post(url, json=request.get_json(), timeout=10,
                headers={'Content-Type': 'application/json'})
        else:
            r = requests.get(url, params=request.args.to_dict(), timeout=10)
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Flight Search API (Fli / Google Flights) ─────────────
@app.route('/api/flights/search', methods=['POST'])
def flights_search():
    try:
        from fli.search import SearchFlights
        from fli.models import (FlightSearchFilters, FlightSegment, Airport,
                                PassengerInfo, SeatType, MaxStops, TripType)
        data = request.get_json()
        origin      = data.get('origin', 'AKL').upper()
        destination = data.get('destination', 'SYD').upper()
        date        = data.get('date', '')
        adults      = int(data.get('adults', 1))
        seat        = data.get('seat', 'ECONOMY').upper()
        stops       = data.get('stops', 'ANY').upper()

        # Map airport codes
        try:
            dep = getattr(Airport, origin)
            arr = getattr(Airport, destination)
        except AttributeError as e:
            return jsonify({'error': f'Unknown airport code: {str(e)}'}), 400

        # Map seat type
        seat_map = {'ECONOMY': SeatType.ECONOMY, 'BUSINESS': SeatType.BUSINESS,
                    'FIRST': SeatType.FIRST, 'PREMIUM_ECONOMY': SeatType.PREMIUM_ECONOMY}
        seat_type = seat_map.get(seat, SeatType.ECONOMY)

        # Map stops
        stops_map = {'ANY': MaxStops.ANY, 'NON_STOP': MaxStops.NON_STOP,
                     'ONE_STOP': MaxStops.ONE_STOP}
        max_stops = stops_map.get(stops, MaxStops.ANY)

        filters = FlightSearchFilters(
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=adults),
            flight_segments=[FlightSegment(
                departure_airport=[[dep, 0]],
                arrival_airport=[[arr, 0]],
                travel_date=date
            )],
            seat_type=seat_type,
            stops=max_stops
        )

        results = SearchFlights().search(filters)

        flights = []
        for r in results[:20]:  # Return top 20
            legs = []
            for leg in r.legs:
                legs.append({
                    'airline': leg.airline.value if leg.airline else '',
                    'flight_number': leg.flight_number or '',
                    'departure_airport': leg.departure_airport.name if leg.departure_airport else '',
                    'arrival_airport': leg.arrival_airport.name if leg.arrival_airport else '',
                    'departure_time': leg.departure_datetime.strftime('%H:%M') if leg.departure_datetime else '',
                    'arrival_time': leg.arrival_datetime.strftime('%H:%M') if leg.arrival_datetime else '',
                    'duration': leg.duration or 0,
                })
            flights.append({
                'price': r.price,
                'duration': r.duration,
                'stops': r.stops,
                'legs': legs
            })

        return jsonify({'flights': flights, 'count': len(results)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Receiver location API ─────────────────────────────────
@app.route('/api/location')
def receiver_location():
    return jsonify({'lat': RX_LAT, 'lon': RX_LON})


# ── Flight trail history API ──────────────────────────────
@app.route('/api/trails')
def trails():
    hours = float(request.args.get('hours', 24))
    cutoff = time.time() - (hours * 3600)
    result = {}
    with TRAIL_LOCK:
        for hex, pts in TRAIL_HISTORY.items():
            filtered = [p for p in pts if p['t'] >= cutoff]
            if len(filtered) >= 2:
                result[hex] = filtered
    return jsonify(result)

@app.route('/api/history')
def history_summary():
    """Summary of all aircraft tracked in the last N hours."""
    hours = float(request.args.get('hours', 24))
    cutoff = time.time() - (hours * 3600)
    now = time.time()

    aircraft = []
    hour_counts = {}

    with TRAIL_LOCK:
        for hex_code, pts in TRAIL_HISTORY.items():
            filtered = [p for p in pts if p['t'] >= cutoff]
            if not filtered:
                continue

            first_seen = min(p['t'] for p in filtered)
            last_seen = max(p['t'] for p in filtered)
            # alt_baro can be the string 'ground' from dump1090 when an aircraft
            # is on the runway — coerce non-numeric values to 0 so max() works
            def _alt_int(p):
                a = p.get('alt_baro', 0)
                return a if isinstance(a, (int, float)) else 0
            max_alt = max(_alt_int(p) for p in filtered)
            callsign = ''
            for p in reversed(filtered):
                if p.get('flight', '').strip():
                    callsign = p['flight'].strip()
                    break

            # Count by hour
            for p in filtered:
                import datetime
                h = datetime.datetime.fromtimestamp(p['t']).strftime('%H')
                hour_counts[h] = hour_counts.get(h, set())
                hour_counts[h].add(hex_code)

            aircraft.append({
                'hex': hex_code,
                'callsign': callsign,
                'first_seen': first_seen,
                'last_seen': last_seen,
                'duration': round(last_seen - first_seen),
                'max_alt': max_alt,
                'positions': len(filtered),
                'last_lat': filtered[-1].get('lat', 0),
                'last_lon': filtered[-1].get('lon', 0),
            })

    # Sort by most recently seen
    aircraft.sort(key=lambda a: a['last_seen'], reverse=True)

    # Hourly activity
    hourly = []
    for h in range(24):
        hstr = f'{h:02d}'
        hourly.append({'hour': hstr, 'count': len(hour_counts.get(hstr, set()))})

    # Busiest hour
    busiest = max(hourly, key=lambda x: x['count']) if hourly else None

    return jsonify({
        'total_unique': len(aircraft),
        'period_hours': hours,
        'aircraft': aircraft[:200],
        'hourly': hourly,
        'busiest_hour': busiest,
    })

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

# -- Stats Records (all-time records persistence) --
STATS_RECORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stats_records.json')

@app.route('/api/stats/records', methods=['GET'])
def get_stats_records():
    try:
        if os.path.exists(STATS_RECORDS_FILE):
            with open(STATS_RECORDS_FILE, 'r') as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify({})

@app.route('/api/stats/records', methods=['POST'])
def save_stats_records():
    try:
        data = request.get_json()
        if data:
            with open(STATS_RECORDS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            return jsonify({'success': True})
    except Exception:
        pass
    return jsonify({'success': False}), 400

# ── OTA Update API ─────────────────────────────────────────
@app.route('/api/ota/status')
def ota_get_status():
    return jsonify({
        'current_version': _get_local_version(),
        'latest_version': ota_status.get('latest', ''),
        'update_available': ota_status.get('available', False),
        'auto_update': _is_auto_update_enabled(),
        'updating': ota_status.get('updating', False),
        'last_check': ota_status.get('last_check', 0)
    })

@app.route('/api/ota/update', methods=['POST'])
def ota_trigger_update():
    if ota_status.get('updating', False):
        return jsonify({'success': False, 'error': 'Update already in progress'})
    # Run update in background thread
    t = threading.Thread(target=_run_update, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': 'Update started — node will restart shortly'})

# -- Favicon --────
@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/remote')
def remote():
    return render_template('remote.html')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
