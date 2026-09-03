from flask import Flask, render_template, Response, jsonify, request, make_response
from flask_cors import CORS
from flask_socketio import SocketIO
# from whisper_atc  # disabled until v2.0 import ATCWhisper
import subprocess
import queue
import threading
import requests
import time
import collections
import json
import os
import gzip
import csv
import logging
import urllib.request
import socket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True  # dev: reflect templates/index.html edits on a refresh (no restart needed)
app.jinja_env.auto_reload = True
CORS(app)
app.config['SECRET_KEY'] = 'pilnk_secret'
# async_mode pinned to 'threading' (v1.4.1, 3 Sep 2026). Left unset,
# flask-socketio picks eventlet whenever it is importable — and the PiAware
# image ships eventlet 0.26.1 (2020) on Python 3.9. That put ONE node in the
# fleet (MME1, armv7l) on a different, ancient server engine from the other
# twelve, where any blocking call in a background thread (ping, OTA check,
# weather proxy) stalls the whole hub: dashboard sometimes loads, sometimes
# not, map freezes mid-session. Threading is what every other node already
# runs, so this changes nothing for them and fixes the outlier.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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

# ── Dashboard port (v1.3.2) ────────────────────────────────────────────────
# Read from config.json, NOT hardcoded, because config.json is gitignored and
# update.sh does `git reset --hard origin/main` — which deliberately discards
# local edits to tracked files. A node that needs a different port therefore
# CANNOT keep that setting in app.py; every OTA would revert it.
#
# This is not hypothetical. A PiAware SD-card image ships piaware-configurator,
# a Flask app bound to 127.0.0.1:5000. PiLNK binds 0.0.0.0:5000, and on Linux a
# wildcard bind collides with an existing loopback bind on the same port — so
# PiLNK died with EADDRINUSE, 606 restarts. The operator edited app.py to use
# 5001, which worked until the next release reset it and took the node down a
# second time. Hence: the port lives in config, where the updater leaves it.
def _dashboard_port():
    try:
        p = int(_config.get('dashboard_port') or 5000)
        return p if 1 <= p <= 65535 else 5000
    except (TypeError, ValueError):
        return 5000

DASHBOARD_PORT = _dashboard_port()


# ── Node environment fingerprint (v1.3.6) ──────────────────────────────────
# Six failures in one week were all "PiLNK assumed the environment it was built
# on" — port 5000 free, no eventlet, pyModeS returning a bool, libraries in a
# flat lib dir, a UTF-8 locale, and a compiled audio engine. Five were found by
# users rather than by us, because nothing here can see what a node actually IS.
# Diagnosing one of them took eight rounds of asking an operator to run commands
# and paste output back.
#
# So the node now reports its own environment on every ping. This is not
# telemetry for its own sake: it is the difference between "what's different
# about your node?" costing eight round-trips or one query. Computed ONCE at
# startup — none of it changes while the process runs, and a ping is not the
# place to be shelling out.
def _describe_environment():
    import platform
    env = {}
    try:
        env['arch']    = platform.machine()                       # aarch64 / armv7l
        env['python']  = platform.python_version()
        env['libc']    = (platform.libc_ver() or ('', ''))[1] or None
    except Exception:
        pass
    try:
        import sys, locale
        env['stdout_encoding'] = (sys.stdout.encoding or '').lower() or None
        env['locale'] = (locale.getlocale()[1] or locale.getpreferredencoding(False) or '').lower() or None
    except Exception:
        pass
    # Library versions — the pyModeS 3.3-vs-3.6 split silently partitioned the
    # fleet by install date, and nothing recorded which side a node was on.
    for mod, key in (('pyModeS', 'pymodes'), ('flask_socketio', 'flask_socketio'),
                     ('eventlet', 'eventlet'), ('numpy', 'numpy')):
        try:
            m = __import__(mod)
            env[key] = getattr(m, '__version__', 'present')
        except Exception:
            env[key] = None          # explicitly absent — as informative as present
    try:
        with open('/proc/device-tree/model', 'r') as f:
            env['pi_model'] = f.read().strip().rstrip('\x00')
    except Exception:
        env['pi_model'] = None
    env['dashboard_port'] = DASHBOARD_PORT
    return env

NODE_ENV = _describe_environment()
print('[PILNK] env: arch=%s python=%s pyModeS=%s eventlet=%s enc=%s port=%s' % (
    NODE_ENV.get('arch'), NODE_ENV.get('python'), NODE_ENV.get('pymodes'),
    NODE_ENV.get('eventlet'), NODE_ENV.get('stdout_encoding'), NODE_ENV.get('dashboard_port')))

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
    # 3. PiAware images keep the receiver location in /etc/piaware.conf, not in
    #    dump1090-fa's defaults — so on a PiAware install we found NOTHING and
    #    returned None, and the dashboard then fell back to a hardcoded Auckland
    #    default (i.e. the developer's own house). MME1 reported his UK map
    #    opening in New Zealand; this is why. Format is `receiver-lat 54.5211`.
    try:
        with open('/etc/piaware.conf', 'r') as f:
            content = f.read()
        lat_m = re.search(r'^\s*receiver-lat\s+(-?[\d.]+)', content, re.M)
        lon_m = re.search(r'^\s*receiver-lon\s+(-?[\d.]+)', content, re.M)
        if lat_m and lon_m:
            return float(lat_m.group(1)), float(lon_m.group(1))
    except Exception:
        pass
    # 4. Unknown — caller must handle None
    return None, None

RX_LAT, RX_LON = read_receiver_location()
if RX_LAT is None or RX_LON is None:
    print('[PILNK] WARNING: Receiver location not set. Add lat/lon to config.json or re-run the installer.')

def _adopt_server_location(new_lat, new_lon):
    """Phase 1 (web onboarding): adopt a web-set location pushed DOWN in the
    node.php ping response. Writes lat/lon into config.json (preserving every
    other key) and refreshes the in-memory RX_LAT/RX_LON so the dashboard map
    and haversine distance pick it up with no restart.

    config.json is the authoritative location source (read_receiver_location
    reads it first), so this is sufficient: the decoder's own RECEIVER_LAT/LON
    is irrelevant to PiLNK (no MLAT) and is deliberately left untouched — no
    sudo, no decoder restart. The server is authoritative; the caller only
    invokes this when the server sends a non-null value that differs from what
    we currently hold (see the ping loop), so there is no node<->server
    oscillation.
    """
    global RX_LAT, RX_LON, _config
    try:
        # Re-read from disk so a concurrent writer (e.g. vhf-serial autodetect)
        # isn't clobbered by a stale in-memory copy.
        cfg = {}
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = dict(_config) if isinstance(_config, dict) else {}
        cfg['lat'] = new_lat
        cfg['lon'] = new_lon
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)   # atomic swap — never a half-written config
        _config = cfg
        RX_LAT, RX_LON = new_lat, new_lon
        print(f'[PILNK] Adopted web-set location {new_lat:.5f},{new_lon:.5f} from pilnk.io')
        return True
    except Exception as e:
        print(f'[PILNK] Location adopt failed: {e}')
        return False

# ── dump1090-fa aircraft data ──────────────────────────────
# Read aircraft.json directly from disk. dump1090-fa writes this
# every second via --write-json /run/dump1090-fa. Reading from disk
# instead of HTTP avoids a dependency on lighttpd (formerly served on
# port 8080) and saves a network round-trip on every poll. Works on
# any install that runs dump1090-fa, including amd64 boxes that don't
# bundle the SkyAware web UI.
DUMP1090_AIRCRAFT_JSON = _config.get('aircraft_json_path', '/run/dump1090-fa/aircraft.json')

def read_aircraft_json():
    """Return raw bytes from dump1090-fa's aircraft.json, or None on error.

    readsb rewrites this file every ~1s (--write-json-every 1). On a BUSY node
    (200+ aircraft) the file is large and the write takes longer, so a plain
    read can catch it mid-write and get a truncated/empty buffer → json.loads
    fails → the ping sends 0 aircraft → the node flickers full↔empty on the map.
    Mode S (BDS) enrichment made each record bigger, widening that race window.

    Fix: read, and if the result doesn't parse as JSON with an 'aircraft' key,
    retry a couple of times with a short sleep (the next 1s write completes the
    file). Only give up — returning None — if every attempt is bad. Callers then
    keep behaving as before, but the common mid-write collision is absorbed here.
    """
    for attempt in range(3):
        try:
            with open(DUMP1090_AIRCRAFT_JSON, 'rb') as f:
                raw = f.read()
        except (IOError, OSError):
            return None
        # Validate it's a complete JSON doc with the aircraft array. A
        # mid-write read typically fails here (truncated) — retry.
        try:
            if raw and 'aircraft' in json.loads(raw):
                return raw
        except (ValueError, KeyError):
            pass
        if attempt < 2:
            time.sleep(0.4)   # let readsb finish the in-flight write
    return None

# ── Aircraft type/registration database (enrichment) ──────
# dump1090-fa does NOT populate the `t` (type) or `r` (registration)
# fields by default — those come from an external database. We load
# the Mictronics/wiedehopf-maintained aircraft database at startup
# and merge into the /flights response. This is what powers the
# type-specific icons (B737/A320/B777/B787/A350/A380) and makes the
# size/category fallback meaningful for non-helicopter aircraft.
#
# Database source: https://github.com/wiedehopf/tar1090-db (csv branch)
# Format: gzipped CSV with header line `icao24,r,t,...` (Mictronics format)
# Refresh: see scripts/refresh-aircraft-db.sh (weekly systemd timer)
#
# Memory footprint: ~30-50 MB for the full database (~500K aircraft).
# If the file is missing, enrichment silently skips — aircraft just
# render with the category-fallback icons from v1.0.6.
# ─────────────────────────────────────────────────────────
AIRCRAFT_DB_LOCAL  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aircraft.csv.gz')
AIRCRAFT_DB_LEGACY = '/usr/local/share/pilnk-aircraft-db/aircraft.csv.gz'
AIRCRAFT_DB_URL    = 'https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz'
AIRCRAFT_DB_MAX_AGE = 7 * 24 * 3600   # 7 days — refresh weekly
AIRCRAFT_DB = {}  # hex (uppercase) -> {'t': type, 'r': registration}
# Optional national-register overlay (CAA/FAA-derived). Same Mictronics
# ';' format (hex;reg;type). Merged on top of AIRCRAFT_DB after every load.
# Absent on most nodes -> silent no-op. See scripts/build-overlay-nz.py.
AIRCRAFT_OVERLAY_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aircraft-overlay.csv.gz')

def _aircraft_db_path():
    """Return the first available aircraft DB path, or None.

    Prefers the user-writable ~/pilnk/ path (auto-downloadable) over
    the legacy system-wide /usr/local/share/ path that older nodes
    have from manual install. Either works — the user-local path
    just doesn't require sudo to refresh.
    """
    if os.path.exists(AIRCRAFT_DB_LOCAL):
        return AIRCRAFT_DB_LOCAL
    if os.path.exists(AIRCRAFT_DB_LEGACY):
        return AIRCRAFT_DB_LEGACY
    return None

def load_aircraft_db():
    """Load the aircraft enrichment database into memory.

    Idempotent: safe to call multiple times. Clears the dict before
    reload so deletions in the source file propagate.

    Format (Mictronics, semicolon-delimited, NO header):
        icao24;r;t;flags;desc;...
    Example row:
        004002;Z-WPA;B732;00;BOEING 737-200;;;
    """
    global AIRCRAFT_DB
    path = _aircraft_db_path()
    if not path:
        logging.info('Aircraft DB not present at either local or legacy path — '
                     'enrichment disabled. Will auto-download in background.')
        AIRCRAFT_DB = {}
        return 0
    try:
        new_db = {}
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            reader = csv.reader(f, delimiter=';')
            for row in reader:
                if len(row) < 3:
                    continue
                hex_code = (row[0] or '').strip().upper()
                if not hex_code or len(hex_code) != 6:
                    continue
                reg = (row[1] or '').strip()
                typ = (row[2] or '').strip()
                # Only store if we have at least a type or a registration
                if typ or reg:
                    new_db[hex_code] = {'t': typ, 'r': reg}
        AIRCRAFT_DB = new_db
        logging.info(f'Aircraft DB loaded: {len(AIRCRAFT_DB)} entries from {path}')
        return len(AIRCRAFT_DB)
    except Exception as e:
        logging.error(f'Failed to load aircraft DB: {e}')
        return 0

def load_aircraft_overlay():
    """Merge an optional national-register overlay on top of AIRCRAFT_DB.

    The primary source (wiedehopf/tar1090-db) has coverage gaps for some
    national fleets — light aircraft, microlights, gliders, amateur-built —
    which surface as unidentified "ghosts". A node operator can drop an
    authoritative overlay built from their CAA/FAA register at
    aircraft-overlay.csv.gz (same ';' format: hex;reg;type) and it is merged
    here. Overlay registration wins; a blank overlay type preserves whatever
    the primary DB already had. Silent no-op when the file is absent, so this
    is harmless on every node that doesn't use one. Call AFTER load_aircraft_db
    (which rebuilds AIRCRAFT_DB from scratch), so the overlay survives refreshes.
    """
    path = AIRCRAFT_OVERLAY_LOCAL
    if not os.path.exists(path):
        return 0
    try:
        merged = 0
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            for row in csv.reader(f, delimiter=';'):
                if len(row) < 2:
                    continue
                hex_code = (row[0] or '').strip().upper()
                if len(hex_code) != 6:
                    continue
                reg = (row[1] or '').strip()
                typ = (row[2].strip() if len(row) > 2 else '')
                if not (reg or typ):
                    continue
                existing = AIRCRAFT_DB.get(hex_code, {})
                AIRCRAFT_DB[hex_code] = {
                    't': typ or existing.get('t', ''),
                    'r': reg or existing.get('r', ''),
                }
                merged += 1
        logging.info(f'Aircraft overlay merged: {merged} entries from {path}')
        return merged
    except Exception as e:
        logging.error(f'Failed to load aircraft overlay: {e}')
        return 0

def _download_aircraft_db():
    """Fetch Mictronics aircraft DB and save to AIRCRAFT_DB_LOCAL.

    Atomic via temp-file + rename so a partial download never corrupts
    an existing DB. Validates the gzip magic bytes before promoting.
    Returns True on success.
    """
    try:
        logging.info(f'[aircraft-db] Downloading from {AIRCRAFT_DB_URL}')
        r = requests.get(AIRCRAFT_DB_URL, timeout=300, stream=True)
        r.raise_for_status()
        tmp = AIRCRAFT_DB_LOCAL + '.tmp'
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        # Sanity: must be gzip (magic bytes 1F 8B)
        with open(tmp, 'rb') as f:
            magic = f.read(2)
        if magic != b'\x1f\x8b':
            os.remove(tmp)
            logging.error('[aircraft-db] Downloaded file is not gzip — aborting')
            return False
        os.replace(tmp, AIRCRAFT_DB_LOCAL)
        size = os.path.getsize(AIRCRAFT_DB_LOCAL)
        logging.info(f'[aircraft-db] Downloaded {size:,} bytes to {AIRCRAFT_DB_LOCAL}')
        return True
    except Exception as e:
        logging.error(f'[aircraft-db] Download failed: {e}')
        return False

def _ensure_aircraft_db_async():
    """Background task: download the DB if missing or stale, then reload.

    Runs in a daemon thread on startup so it never blocks Flask. Logs
    progress to the service log so admins can see what happened.
    """
    def task():
        path = _aircraft_db_path()
        if path is None:
            logging.info('[aircraft-db] Missing — downloading on startup')
            if _download_aircraft_db():
                load_aircraft_db()
                load_aircraft_overlay()
            return
        try:
            age = time.time() - os.path.getmtime(path)
        except Exception:
            age = AIRCRAFT_DB_MAX_AGE + 1
        if age > AIRCRAFT_DB_MAX_AGE:
            days = age / 86400
            logging.info(f'[aircraft-db] DB at {path} is {days:.1f} days old — refreshing')
            if _download_aircraft_db():
                load_aircraft_db()
                load_aircraft_overlay()
        else:
            hours = age / 3600
            logging.info(f'[aircraft-db] DB at {path} is {hours:.1f} hours old — fresh enough')
    threading.Thread(target=task, daemon=True).start()

# Load on startup. Non-fatal if missing.
load_aircraft_db()
load_aircraft_overlay()   # merge optional national-register overlay (e.g. NZ CAA)
# Kick off a background refresh — won't block startup, downloads if needed
_ensure_aircraft_db_async()

# ── Flight trail history — stores last 24h of positions ───
# { hex: deque([ {lat, lon, alt_baro, baro_rate, flight, t} ]) }
TRAIL_HISTORY = collections.defaultdict(lambda: collections.deque(maxlen=500))
TRAIL_LOCK = threading.Lock()
MAX_TRAIL_AGE = 24 * 3600  # 24 hours in seconds
_TRAIL_ERR = {'last': 0.0}   # throttle for the recorder's error line (Pass 2)

def record_trails():
    while True:
        try:
            raw = read_aircraft_json()
            if raw is not None:
                data = json.loads(raw)
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
        except Exception as e:
            # Fail-soft is right — one bad iteration must never kill the
            # recorder. Eternal silence wasn't: a persistent failure here
            # degrades trails/History invisibly, the "looks like nobody used
            # it" shape (Pass 2, 28 Aug). At most one journal line per hour.
            if time.time() - _TRAIL_ERR['last'] > 3600:
                _TRAIL_ERR['last'] = time.time()
                print(f'[PILNK] Trail recorder error (continuing, throttled 1/hr): {e}')
        time.sleep(10)  # Record every 10 seconds

# Start trail recorder thread
trail_thread = threading.Thread(target=record_trails, daemon=True)
trail_thread.start()

# ── PiLNK.io server ping — sends aircraft data + stats every 15s
# (tightened from 30s 2026-07-01 — AJ observed 30s+ Network-page lag live at NZAA)
# PiLNK Code is read from config.json (created by installer, gitignored).
# Phase 2 web-pairing: if no code is stored, app.py self-bootstraps via
# register_pending → pairing code shown at startup → operator claims on
# pilnk.io → poll_token confirms claim → verify_code written to config.json
# and ping thread starts live (no restart).
def _load_pilnk_code():
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
            code = cfg.get('pilnk_code', '').strip()
            if code:
                return code
    except Exception:
        pass
    return 'YOUR_VERIFY_CODE_HERE'

def _save_pending(pairing_code, poll_token):
    """Persist pairing state so a reboot mid-pairing resumes (same code, same token)."""
    try:
        cfg = {}
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass
        cfg['pending'] = {'pairing_code': pairing_code, 'poll_token': poll_token}
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        print(f'[PILNK-PAIR] Warning: could not save pending state: {e}')

def _load_pending():
    """Return (pairing_code, poll_token) from a previous run, or (None, None)."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        p = cfg.get('pending')
        if p and p.get('pairing_code') and p.get('poll_token'):
            return p['pairing_code'], p['poll_token']
    except Exception:
        pass
    return None, None

def _clear_pending_adopt_code(verify_code):
    """On successful claim: write verify_code into config.json, drop the pending block."""
    global NODE_VERIFY_CODE, _config
    try:
        cfg = {}
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass
        cfg['pilnk_code'] = verify_code
        cfg.pop('pending', None)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _config = cfg
        NODE_VERIFY_CODE = verify_code
        print(f'[PILNK-PAIR] Claimed! verify_code adopted — ping thread starting')
        return True
    except Exception as e:
        print(f'[PILNK-PAIR] Failed to adopt verify_code: {e}')
        return False

def _clear_pending():
    """Drop ONLY the pending block (no code adopted). Used to discard a dead/expired
    pairing token so _start_pairing_flow() registers genuinely fresh instead of
    resuming the same poisoned token via _load_pending()."""
    try:
        cfg = {}
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass
        if 'pending' not in cfg:
            return
        cfg.pop('pending', None)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        print(f'[PILNK-PAIR] Warning: could not clear pending state: {e}')

# Pairing state (shared between pairing thread and the /api/pairing/status endpoint)
pairing_state = {
    'active': False,        # True while waiting to be claimed
    'claimed': False,       # True once successfully claimed
    'pairing_code': None,   # 6-char code displayed at startup
    'error': None,          # set if registration fails
}
pairing_state_lock = threading.Lock()

def _pairing_poll_thread(poll_token):
    """Poll claim_poll every 5s until claimed, then start the ping thread live."""
    global pairing_state
    while True:
        try:
            payload = json.dumps({'action': 'claim_poll', 'poll_token': poll_token}).encode()
            req = urllib.request.Request(
                'https://pilnk.io/api/node.php',
                data=payload,
                headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            rj = json.loads(resp.read().decode())
            if rj.get('claimed') and rj.get('verify_code'):
                # Adopt and start ping
                if _clear_pending_adopt_code(rj['verify_code']):
                    with pairing_state_lock:
                        pairing_state['active'] = False
                        pairing_state['claimed'] = True
                    ping_t = threading.Thread(target=ping_server, daemon=True)
                    ping_t.start()
                    print('[PILNK] Server ping active — reporting to pilnk.io')
                    return   # done — this thread exits
            elif rj.get('expired'):
                # The token is dead — either the operator took >24h (TTL lapsed) OR the
                # row was poisoned (claimed by a since-deleted user, so the server can
                # never hand down a verify_code). Either way this code is unusable.
                # SELF-HEAL: discard the dead pending token and re-register a FRESH code
                # automatically — no manual restart needed. We MUST _clear_pending()
                # first, otherwise _start_pairing_flow() would _load_pending() and resume
                # this very same dead token, looping forever on the poison.
                print('[PILNK-PAIR] Pairing code dead/expired — re-registering a fresh code...')
                with pairing_state_lock:
                    pairing_state['active'] = False
                    pairing_state['error'] = None
                _clear_pending()
                _start_pairing_flow()   # registers fresh, prints new code, starts a new poll thread
                return                  # this (old) thread exits; the new one takes over
        except Exception as e:
            print(f'[PILNK-PAIR] Poll error (will retry): {e}')
        time.sleep(5)

def _start_pairing_flow():
    """Register with pilnk.io (or resume a saved pairing), print the code, start poll thread."""
    global pairing_state

    # Resume from a previous run if we already have a token
    saved_code, saved_token = _load_pending()
    if saved_code and saved_token:
        print(f'[PILNK-PAIR] Resuming pairing from previous run')
        with pairing_state_lock:
            pairing_state['active'] = True
            pairing_state['pairing_code'] = saved_code
        _print_pairing_banner(saved_code)
        t = threading.Thread(target=_pairing_poll_thread, args=(saved_token,), daemon=True)
        t.start()
        return

    # Fresh registration
    try:
        import platform
        pi_model = None
        try:
            with open('/proc/device-tree/model', 'r') as f:
                pi_model = f.read().strip().rstrip('\x00')
        except Exception:
            pass
        payload = json.dumps({
            'action': 'register_pending',
            'node_name': platform.node(),
            'pi_model': pi_model,
        }).encode()
        req = urllib.request.Request(
            'https://pilnk.io/api/node.php',
            data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        rj = json.loads(resp.read().decode())
        pairing_code = rj.get('pairing_code')
        poll_token = rj.get('poll_token')
        if not pairing_code or not poll_token:
            raise ValueError(f'Unexpected registration response: {rj}')
        _save_pending(pairing_code, poll_token)
        with pairing_state_lock:
            pairing_state['active'] = True
            pairing_state['pairing_code'] = pairing_code
        _print_pairing_banner(pairing_code)
        t = threading.Thread(target=_pairing_poll_thread, args=(poll_token,), daemon=True)
        t.start()
    except Exception as e:
        print(f'[PILNK-PAIR] Registration failed: {e}')
        with pairing_state_lock:
            pairing_state['error'] = str(e)
        print('[PILNK-PAIR] Will retry on next restart. Check your internet connection.')

def _print_pairing_banner(code):
    bar = '=' * 58
    print(f'\n{bar}')
    print(f'  PILNK NODE PAIRING')
    print(f'{bar}')
    print(f'')
    print(f'  Pairing code:   >>> {code} <<<')
    print(f'')
    print(f'  1. Log in at  https://pilnk.io')
    print(f'  2. Go to your Profile → Node section')
    print(f'  3. Enter the code above to claim this node')
    print(f'')
    print(f'  Waiting... (code valid for 24 hours)')
    print(f'{bar}\n')

NODE_VERIFY_CODE = _load_pilnk_code()
# Populated at startup from config.json; updated in-memory when pairing completes.

# Sanity gates for all-time records — single corrupt ADS-B frames have
# polluted historical leaderboards with impossible values (e.g. 943 kts
# civilian, 125,800 ft). Anything outside these bounds is a decode error.
# Loose enough to still catch genuine military / U-2 / supercruise activity.
STATS_MIN_SPEED_KTS = 30      # filter taxi / decode noise floor
STATS_MAX_SPEED_KTS = 750     # SR-71 retired; F-22 supercruise ~600 kts; 750 = generous ceiling
STATS_MAX_ALT_FT    = 60000   # FL600 — above any commercial traffic; rejects Gillham code decode glitches (e.g. C150 at FL960)

# ── Type-aware plausibility (v1.3.1) ────────────────────────────────────
# The absolute gates above only catch the extremes. A corrupted frame that
# decodes to a plausible-looking hex will sail straight through them: a
# Solomon Airlines DHC-6 Twin Otter (H4-SIC, hex 897004) was reported at
# FL360 and 400+ kts — inside both limits, and impossible for the airframe.
# It is unpressurised, tops out near FL250, and cruises around 170 kts. The
# real aircraft has been grounded at Honiara since 2024, so the frame was a
# garbled decode whose bits happened to form a valid hex.
#
# Since AIRCRAFT_DB already gives us hex -> type on every node, we can ask a
# better question than "is this number huge?": "could THIS airframe do this?"
#
# Two deliberate design choices:
#
#   1. ALTITUDE IS THE RELIABLE TEST. An unpressurised aeroplane cannot
#      cruise at FL360 regardless of conditions. Ground speed is much weaker
#      evidence — it is NOT airspeed, and a Twin Otter with a 100 kt tailwind
#      genuinely shows ~270 kts over the ground. Speed limits below therefore
#      carry generous headroom; altitude limits are closer to the book figure.
#
#   2. ONLY REJECT WHEN CONFIDENT. Unknown or absent types fall through to
#      the absolute gates above. A missing lookup must never silently drop
#      real traffic — the cost of a false negative (one bad row) is far lower
#      than a false positive (a real aircraft vanishing from the map).
#
# Limits are (max_alt_ft, max_ground_speed_kts), already padded.
AIRFRAME_LIMITS = {
    # Piston singles
    'C150': (18000, 170), 'C152': (18000, 170), 'C162': (18000, 170),
    'C172': (20000, 190), 'C177': (20000, 190), 'C182': (22000, 210),
    'C185': (22000, 210), 'C206': (22000, 220), 'C207': (22000, 220),
    'C210': (26000, 240), 'P210': (28000, 250),
    'PA18': (18000, 160), 'PA22': (18000, 170), 'PA24': (22000, 210),
    'PA28': (20000, 200), 'P28A': (20000, 200), 'P28B': (20000, 200),
    'P28R': (22000, 210), 'PA32': (22000, 210), 'P32R': (22000, 220),
    'PA38': (18000, 170), 'PA46': (30000, 280), 'P46T': (32000, 300),
    'BE33': (22000, 220), 'BE35': (22000, 220), 'BE36': (22000, 230),
    'SR20': (20000, 220), 'SR22': (22000, 250),
    'DA40': (20000, 190), 'DA20': (18000, 170),
    'RV7' : (20000, 220), 'RV8' : (20000, 220), 'RV10': (20000, 230),
    'GLID': (32000, 180),   # wave soaring reaches surprising altitudes
    'ULAC': (14000, 150), 'GYRO': (14000, 140),

    # Piston twins
    'BE55': (24000, 250), 'BE58': (24000, 260), 'BE76': (22000, 220),
    'PA31': (28000, 280), 'PA34': (26000, 250), 'PA44': (22000, 220),
    'C310': (26000, 260), 'C337': (22000, 220), 'C402': (28000, 270),
    'C404': (28000, 270), 'C421': (30000, 280),
    'DA42': (22000, 220), 'DA62': (22000, 230),
    'BN2P': (18000, 190),

    # Turboprops — unpressurised
    'DHC6': (28000, 270),   # Twin Otter: ceiling ~FL250, cruise ~170 kts
    'DHC2': (20000, 190), 'DHC3': (20000, 200), 'DHC7': (26000, 300),
    'C208': (28000, 260), 'AC90': (30000, 290),

    # Turboprops — pressurised
    'PC12': (32000, 330), 'TBM7': (33000, 380), 'TBM8': (33000, 390),
    'TBM9': (33000, 400), 'BE20': (37000, 350), 'B350': (37000, 360),
    'BE9L': (32000, 300), 'SW4' : (28000, 310), 'D228': (28000, 260),
    'C441': (35000, 320), 'P180': (41000, 420),

    # Regional turboprops
    'AT43': (28000, 330), 'AT45': (28000, 330), 'AT72': (28000, 340),
    'AT75': (28000, 340), 'AT76': (28000, 340),
    'DH8A': (28000, 320), 'DH8B': (28000, 320), 'DH8C': (28000, 330),
    'DH8D': (30000, 400), 'JS31': (28000, 300), 'JS32': (28000, 300),
    'JS41': (28000, 320), 'SF34': (28000, 310), 'SB20': (28000, 380),
    'E110': (24000, 260), 'E120': (32000, 330), 'F27' : (28000, 300),

    # Helicopters
    'R22' : (14000, 150), 'R44' : (14000, 160), 'R66' : (14000, 160),
    'B06' : (20000, 180), 'B407': (20000, 190), 'B412': (20000, 180),
    'B429': (20000, 190), 'B505': (18000, 170),
    'AS50': (20000, 180), 'AS55': (20000, 180), 'AS65': (20000, 190),
    'EC20': (20000, 180), 'EC30': (20000, 180), 'EC35': (20000, 190),
    'EC45': (20000, 190), 'EC55': (20000, 200), 'EC75': (20000, 200),
    'H125': (20000, 180), 'H130': (20000, 180), 'H135': (20000, 190),
    'H145': (20000, 190), 'H160': (20000, 200), 'H175': (20000, 200),
    'A139': (20000, 200), 'A169': (20000, 190), 'A189': (20000, 200),
    'S76' : (20000, 200), 'S92' : (20000, 200), 'MD90': (18000, 170),
}


def _airframe_implausible(hex_up, alt, gs):
    """Is this altitude/speed impossible for the airframe behind this hex?

    Returns a short reason string when the frame should be rejected, or None
    to accept. Absent hex, absent type, or a type we have no figures for all
    return None — unknown means "let it through and rely on the absolute
    gates", never "drop it".
    """
    if not hex_up:
        return None
    entry = AIRCRAFT_DB.get(hex_up)
    if not entry:
        return None
    limits = AIRFRAME_LIMITS.get((entry.get('t') or '').upper())
    if not limits:
        return None
    max_alt, max_gs = limits
    if alt and alt > max_alt:
        return f"{entry['t']} at {int(alt)}ft (max {max_alt})"
    if gs and gs > max_gs:
        return f"{entry['t']} at {int(gs)}kt (max {max_gs})"
    return None


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

# ── Coverage map (polar reception footprint) ──────────────────────────────
# Per-bearing footprint accumulated for ALL TIME (persisted; survives restarts
# and OTA updates). 36 × 10° sectors, two metrics per sector:
#   max_nm   — furthest position ever decoded on that bearing (range footprint)
#   min_elev — lowest elevation angle (deg) received on that bearing at
#              >= COVERAGE_ELEV_MIN_NM out: the long-range horizon floor.
#              Terrain lifts this floor, so the plot literally draws your
#              obstructions (e.g. a mountain range) from your own ADS-B data.
# Sanity gates mirror the all-time records — a single corrupt frame must not
# poison a persisted footprint, hence the COVERAGE_MAX_NM cap.
COVERAGE_SECTORS     = 36
COVERAGE_MAX_NM      = 400   # radio horizon @ FL600 ≈ 300 nm; past 400 = decode glitch
COVERAGE_ELEV_MIN_NM = 20    # horizon floor ignores close-in low traffic (in FRONT of, not behind, obstructions)

# The all-time "furthest" record was the one stat with NO upper gate — the
# coverage map above is capped at COVERAGE_MAX_NM, but a single corrupt
# position frame could set a permanent distance record nobody can beat.
#
# A flat cap is crude, because what's plausible depends on how high the
# aircraft is. Radio horizon ≈ 1.23 × √(altitude in ft):
#     FL400 → ~246 nm     FL100 → ~123 nm     5,000 ft → ~87 nm
# so 200 nm from an aircraft at 5,000 ft is impossible, while the same figure
# at FL400 is unremarkable. Gating on altitude catches far more bad frames
# than one number ever could.
#
# Two allowances keep real contacts safe:
#   HORIZON_RX_ALLOWANCE_NM — the receiver's own horizon; 30 nm covers a site
#     around 600 ft, comfortably above any normal home installation.
#   HORIZON_DUCT_FACTOR — tropospheric ducting genuinely bends signals past
#     line of sight, so allow 25% beyond the geometric figure.
# With no usable altitude we fall back to the flat COVERAGE_MAX_NM cap.
HORIZON_RX_ALLOWANCE_NM = 30
HORIZON_DUCT_FACTOR     = 1.25


def _beyond_radio_horizon(dist_nm, alt_ft):
    """True when a contact is further away than physics reasonably allows."""
    import math   # module-level `math` isn't imported in app.py — it's brought
                  # in locally where needed (see compute_node_stats). Keep this.
    if not alt_ft or alt_ft <= 0:
        return dist_nm > COVERAGE_MAX_NM
    horizon = (1.23 * math.sqrt(alt_ft)) + HORIZON_RX_ALLOWANCE_NM
    return dist_nm > horizon * HORIZON_DUCT_FACTOR
COVERAGE_SAVE_S      = 300   # flush to disk at most every 5 min
COVERAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'coverage.json')

coverage = {'max_nm': [0.0] * COVERAGE_SECTORS, 'min_elev': [None] * COVERAGE_SECTORS}
_coverage_dirty = False
_coverage_saved_at = 0.0

def _coverage_load():
    global coverage
    try:
        if os.path.exists(COVERAGE_FILE):
            with open(COVERAGE_FILE, 'r') as f:
                d = json.load(f)
            mx, el = d.get('max_nm'), d.get('min_elev')
            if isinstance(mx, list) and len(mx) == COVERAGE_SECTORS and isinstance(el, list) and len(el) == COVERAGE_SECTORS:
                coverage = {'max_nm': [float(v or 0) for v in mx],
                            'min_elev': [None if v is None else float(v) for v in el]}
    except Exception as e:
        print(f'[PILNK] Coverage load failed (starting fresh): {e}')

def _coverage_save_if_due():
    """Flush coverage to disk, rate-limited. Called inside node_stats_lock."""
    global _coverage_dirty, _coverage_saved_at
    if not _coverage_dirty or (time.time() - _coverage_saved_at) < COVERAGE_SAVE_S:
        return
    try:
        tmp = COVERAGE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'max_nm': coverage['max_nm'], 'min_elev': coverage['min_elev'],
                       'sectors': COVERAGE_SECTORS, 'updated': int(time.time())}, f)
        os.replace(tmp, COVERAGE_FILE)
        _coverage_dirty = False
        _coverage_saved_at = time.time()
    except Exception as e:
        print(f'[PILNK] Coverage save failed: {e}')

_coverage_load()

def compute_node_stats(aircraft):
    """Update running stats from current aircraft snapshot."""
    import math
    global _coverage_dirty
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

            # Type-aware plausibility (v1.3.1). The absolute gates below catch
            # extremes; this catches a corrupt frame that looks reasonable in
            # isolation but is impossible for the airframe it claims to be —
            # e.g. a DHC-6 Twin Otter at FL360 and 400 kts.
            bad_frame = _airframe_implausible(hex_code.upper() if hex_code else '', alt, speed)
            if bad_frame:
                logging.debug('[sanity] rejected record from implausible frame: %s', bad_frame)

            # Fastest (gated — see STATS_MAX_SPEED_KTS above; rejects decode glitches)
            if not bad_frame and STATS_MIN_SPEED_KTS <= speed <= STATS_MAX_SPEED_KTS and (not node_stats['fastest'] or speed > node_stats['fastest']['val']):
                node_stats['fastest'] = {'cs': cs, 'val': speed}

            # Highest (gated — see STATS_MAX_ALT_FT above; rejects decode glitches)
            if not bad_frame and 0 < alt <= STATS_MAX_ALT_FT and (not node_stats['highest'] or alt > node_stats['highest']['val']):
                node_stats['highest'] = {'cs': cs, 'val': alt}

            # Furthest (haversine in nm) — requires receiver location
            if lat and lon and RX_LAT is not None and RX_LON is not None:
                dLat = math.radians(lat - RX_LAT)
                dLon = math.radians(lon - RX_LON)
                a = math.sin(dLat/2)**2 + math.cos(math.radians(RX_LAT)) * math.cos(math.radians(lat)) * math.sin(dLon/2)**2
                dist = round(3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
                # Gated on the radio horizon for the reported altitude — see
                # _beyond_radio_horizon. Previously ungated, which let a single
                # corrupt frame set an unbeatable all-time record.
                if dist > 0 and not bad_frame and not _beyond_radio_horizon(dist, alt) \
                   and (not node_stats['furthest'] or dist > node_stats['furthest']['val']):
                    node_stats['furthest'] = {'cs': cs, 'val': dist}

                # Coverage map — per-bearing range + horizon floor (all-time, persisted)
                if 0 < dist <= COVERAGE_MAX_NM:
                    y = math.sin(dLon) * math.cos(math.radians(lat))
                    x = math.cos(math.radians(RX_LAT)) * math.sin(math.radians(lat)) - \
                        math.sin(math.radians(RX_LAT)) * math.cos(math.radians(lat)) * math.cos(dLon)
                    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
                    sector = int(bearing // (360 / COVERAGE_SECTORS)) % COVERAGE_SECTORS
                    if dist > coverage['max_nm'][sector]:
                        coverage['max_nm'][sector] = dist
                        _coverage_dirty = True
                    if dist >= COVERAGE_ELEV_MIN_NM and 0 < alt <= STATS_MAX_ALT_FT:
                        d_m = dist * 1852.0
                        # elevation angle incl. earth curvature drop (4/3-earth radio horizon; 2×R_eff ≈ 16,989 km)
                        elev = math.degrees(math.atan2(alt * 0.3048 - d_m * d_m / 16989000.0, d_m))
                        cur = coverage['min_elev'][sector]
                        if cur is None or elev < cur:
                            coverage['min_elev'][sector] = round(elev, 2)
                            _coverage_dirty = True

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

        # Coverage flush (rate-limited to every COVERAGE_SAVE_S)
        _coverage_save_if_due()


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
            'coverage': {'max_nm': coverage['max_nm'], 'min_elev': coverage['min_elev']},
            'records': records
        }


# ── Network liveness ──────────────────────────────────────────────────────
# The ping loop stamps PING_LAST_OK_TS on every successful report to pilnk.io.
# /api/net/status reads it so the dashboard can show a clear OFFLINE pill when
# we stop reaching the network. A node counts as "online" only if it is paired
# AND pinged within NET_STALE_S (i.e. 3 missed 30s pings before we alarm, so a
# single blip never false-triggers).
NET_STALE_S = 90
PING_LAST_OK_TS = 0.0

# ── Spurious-empty suppression (v1.2.15.5) ───────────────────────────────────
# On a busy node the decoder's aircraft.json is large and rewritten every ~1s.
# We can still read it at a moment it reports zero/near-zero aircraft (a brief
# decoder hiccup, or a read landing in a gap) even when traffic is steady. A
# real node tracking 200+ aircraft does NOT genuinely drop to 0 for one cycle —
# planes don't all vanish in a second — so broadcasting that 0 just makes the
# node strobe on the map (full→empty→full). We hold the LAST GOOD aircraft list
# and reuse it when a cycle collapses implausibly, rather than send the zero.
# Guarded by a staleness timeout so a genuinely-dead decoder DOES eventually
# report empty (we don't pretend a truly-offline receiver is still tracking).
_LAST_GOOD_AIRCRAFT = []
_LAST_GOOD_TS = 0.0
EMPTY_HOLD_MAX_S = 120          # after this long with no fresh data, report the truth (empty)
COLLAPSE_FRACTION = 0.25        # a drop to <25% of the last good count is treated as spurious

def _suppress_spurious_empty(aircraft):
    """Return the aircraft list to actually send. If this cycle collapsed
    implausibly versus the last good cycle (and we're still within the hold
    window), reuse the last good list to avoid strobing the map. Otherwise
    accept the new list and remember it as the last good one."""
    global _LAST_GOOD_AIRCRAFT, _LAST_GOOD_TS
    now = time.time()
    n = len(aircraft)
    prev = len(_LAST_GOOD_AIRCRAFT)

    # Healthy reading (or a plausible change): accept + remember it.
    # "Plausible" = we have planes, OR we never had many to begin with.
    if n > 0 and (prev == 0 or n >= prev * COLLAPSE_FRACTION):
        _LAST_GOOD_AIRCRAFT = aircraft
        _LAST_GOOD_TS = now
        return aircraft

    # Collapse (0, or a drastic drop) while we recently had a healthy list.
    # Hold the last good list — UNLESS it's gone stale (decoder really is down),
    # in which case report the truth so a dead node isn't shown as alive.
    if prev > 0 and (now - _LAST_GOOD_TS) <= EMPTY_HOLD_MAX_S:
        return _LAST_GOOD_AIRCRAFT

    # Genuinely empty for too long — accept reality, reset.
    _LAST_GOOD_AIRCRAFT = aircraft
    _LAST_GOOD_TS = now
    return aircraft

# Singleton guard for the ping loop. ping_server can be launched from more than
# one place (startup with a code, AND the pairing claim-poll on success, AND the
# self-heal re-registration path). Without a guard, a node that bounces through
# pairing/self-heal can end up with TWO ping threads in ONE process — both POST
# every cycle, and if one catches aircraft.json mid-write it sends 0, which
# overwrites the good ping (last-write-wins) and makes the node flicker empty on
# the map. This guard ensures only the FIRST launch ever runs the loop; any
# later launch is a no-op. (Fix shipped v1.2.16.1 — the "Thor flicker".)
_ping_loop_running = False
_ping_loop_lock = threading.Lock()

def ping_server():
    global PING_LAST_OK_TS, _ping_loop_running
    with _ping_loop_lock:
        if _ping_loop_running:
            print('[PILNK] ping_server already running — duplicate launch ignored')
            return
        _ping_loop_running = True
    while True:
        try:
            # Grab current aircraft from dump1090
            aircraft = []
            emergency_aircraft = []
            raw = read_aircraft_json()
            if raw is not None:
                try:
                    data = json.loads(raw)
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

                    # ── Emergency black-box payload ─────────────────────────
                    # Aircraft squawking an emergency code (incl. the 2200
                    # pipe-test) get their FULL record — every dump1090 field
                    # plus live Mode S (BDS40/50/60) enrichment — in a separate
                    # array. Normal traffic stays lean; emergencies are rare, so
                    # the heavier payload is negligible. The server
                    # (node.php recordEmergencyHistory) records these.
                    EMERGENCY_SQUAWKS = ('7700', '7600', '7500', '2200')
                    for a in data.get('aircraft', []):
                        if str(a.get('squawk', '')).strip() not in EMERGENCY_SQUAWKS:
                            continue
                        rec = dict(a)
                        hex_up = (rec.get('hex') or '').upper()
                        if hex_up:
                            entry = AIRCRAFT_DB.get(hex_up) if AIRCRAFT_DB else None
                            if entry:
                                if not rec.get('t') and entry.get('t'):
                                    rec['t'] = entry['t']
                                if not rec.get('r') and entry.get('r'):
                                    rec['r'] = entry['r']
                            _merge_bds(rec, hex_up)
                        emergency_aircraft.append(rec)
                except (ValueError, KeyError):
                    pass

            # Compute stats
            compute_node_stats(aircraft)

            # Suppress a spurious collapse-to-zero so the node doesn't strobe on
            # the map (v1.2.15.5). Stats above are computed from the REAL read;
            # only the reported aircraft list is held-last-good when implausible.
            aircraft = _suppress_spurious_empty(aircraft)

            # Send to pilnk.io
            ping_data = {
                'action': 'ping',
                'verify_code': NODE_VERIFY_CODE,
                'aircraft_count': len(aircraft),
                'aircraft': aircraft,
                'node_stats': get_stats_payload(),
                'version': _get_local_version(),
                # Environment fingerprint + why self-hiding features are hidden.
                # A feature that hides itself when its backend is missing looks
                # identical to one nobody opened — that's how the audio engine
                # appeared shipped for six weeks while running nowhere. Reporting
                # the reason makes "who actually has working ATC audio?" a query
                # instead of a survey.
                'env': NODE_ENV,
                'features': dict({
                    'sdr_audio': 'ready' if os.path.exists('/usr/local/bin/pilnkradio') else 'engine_absent',
                    'atc_stt':   'ready' if os.path.exists(ATC_TRANSCRIPT_PATH) else 'absent',
                }, **_ota_ping_features()),
            }
            if emergency_aircraft:
                ping_data['emergency_aircraft'] = emergency_aircraft
            # Only report location UP when we actually have one. Omitting it for
            # a not-yet-located node avoids seeding a 0,0 "null island" row; the
            # server seeds from this only when its own value is null (see the
            # node.php precedence rule).
            if RX_LAT is not None and RX_LON is not None:
                ping_data['lat'] = RX_LAT
                ping_data['lon'] = RX_LON
            payload = json.dumps(ping_data).encode()
            req = urllib.request.Request(
                'https://pilnk.io/api/node.php',
                data=payload,
                headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'}
            )
            resp = urllib.request.urlopen(req, timeout=10)

            # Phase 1: adopt the server-authoritative location pushed DOWN in
            # the ping response (config flows down, telemetry flows up). The
            # server always wins; we act only when it returns a non-null lat/lon
            # that differs from what we hold — which is exactly the case after
            # the operator pins/repins on pilnk.io. Wrapped so a malformed or
            # empty response can never break the ping loop.
            try:
                body = resp.read()
                rj = json.loads(body.decode()) if body else {}
                scfg = rj.get('config') or {}
                slat, slon = scfg.get('lat'), scfg.get('lon')
                if slat is not None and slon is not None:
                    if (RX_LAT is None or RX_LON is None
                            or abs(float(slat) - float(RX_LAT)) > 1e-6
                            or abs(float(slon) - float(RX_LON)) > 1e-6):
                        _adopt_server_location(float(slat), float(slon))
            except Exception as e:
                print(f'[PILNK] Config adopt skipped: {e}')

            PING_LAST_OK_TS = time.time()
            print(f'[PILNK] Ping sent — {len(aircraft)} aircraft')
        except Exception as e:
            print(f'[PILNK] Ping failed: {e}')
        time.sleep(15)

# Ping thread is started further down — AFTER _get_local_version() and the
# rest of the module are defined (see "Start ping thread" beside the OTA
# thread start). Starting it here raced the daemon against the still-loading
# module: the first ping fired before _get_local_version() existed, throwing
# "name '_get_local_version' is not defined" on the first ping after every
# restart.

# ── OTA Update System ─────────────────────────────────────
PILNK_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(PILNK_DIR, 'VERSION')
UPDATE_SCRIPT = os.path.join(PILNK_DIR, 'update.sh')
OTA_CHECK_INTERVAL = 300   # 5 minutes (was 1 hour — too slow for active dev iteration)
OTA_COOLDOWN = 3600       # 1 hour after update before re-checking
OTA_STATE_FILE = os.path.join(PILNK_DIR, 'ota_state.json')

# ── GUARDRAIL #2 (May 2026): Persistent OTA cooldown ──
# ota_last_update used to be a plain module variable that reset to 0
# on every process restart. update.sh restarts the service as its
# final step, so the new process always started with no cooldown —
# meaning if an update genuinely failed but was re-attempted after a
# restart, there'd be no rate limit. Persisting to disk means the
# cooldown survives restarts, capping retry attempts at 1 per hour
# even in worst-case loop scenarios.
def _load_ota_state():
    """Full persisted OTA state. Backward compatible: an old-format file
    holding only {'last_update': ts} still loads fine."""
    try:
        with open(OTA_STATE_FILE, 'r') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_ota_state(**updates):
    """Merge-write OTA state. Merging (not overwrite) so the cooldown write
    in _run_update can't erase the result written moments earlier."""
    try:
        state = _load_ota_state()
        state.update(updates)
        with open(OTA_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f'[PILNK-OTA] Warning: could not persist OTA state: {e}')

def _load_ota_last_update():
    try:
        return float(_load_ota_state().get('last_update', 0))
    except Exception:
        return 0.0

def _save_ota_last_update(ts):
    _save_ota_state(last_update=ts)

# ── OTA health in the ping (v1.3.10 "Vital-Signs") ──
# M0CRT sat on 1.3.0 for weeks while pinging healthily every minute, and
# nothing server-side could say WHY: the ping carried no OTA state at all. A
# node whose updater is dead is indistinguishable from one whose owner opted
# out. Same class of gap as pi_model — the node knew, nobody asked.
#
# 'started@<ver>' reconciliation: a SUCCESSFUL update restarts this service
# from inside update.sh, so the success path can never record its own result —
# the process is dead before it gets the chance. Instead _run_update records
# 'started@<old-ver>' beforehand; on the next boot, if the version moved on,
# that pending marker becomes the success record. If the version did NOT move,
# the update ran and went nowhere — recorded as 'interrupted' rather than
# left dangling.
_OTA_START_TS = time.time()

def _reconcile_pending_ota_result():
    """Called at startup, AFTER _get_local_version is defined (calling it from
    module scope up here would NameError into a silent skip — the exact
    py_compile-passes-names-don't bug from the 26 Aug postmortem)."""
    _pend = _load_ota_state().get('last_result', '')
    if isinstance(_pend, str) and _pend.startswith('started@'):
        _save_ota_state(last_result=(
            'success' if _pend.split('@', 1)[1] != _get_local_version()
            else 'interrupted'))

def _ota_ping_features():
    """Three LOW-CARDINALITY strings for the ping's features dict — states,
    not timestamps, so fleet_query's grouping stays useful (a raw timestamp
    would make every node an 'outlier' every ping).
      ota:            auto | manual   (how updates are configured)
      ota_check:      ok | stale | starting | never   (is the checker ALIVE)
      ota_last_result:success | interrupted | started@v | exit_N | error:X | none
    """
    now = time.time()
    checked = ota_status.get('last_check', 0)
    if checked:
        check = 'ok' if (now - checked) < 3 * OTA_CHECK_INTERVAL else 'stale'
    elif (now - _OTA_START_TS) < 2 * OTA_CHECK_INTERVAL:
        check = 'starting'   # too soon after boot to judge
    else:
        check = 'never'      # process is old and the checker has never succeeded
    return {
        'ota':             'auto' if _is_auto_update_enabled() else 'manual',
        'ota_check':       check,
        'ota_last_result': str(_load_ota_state().get('last_result', 'none'))[:40],
    }

ota_last_update = _load_ota_last_update()
ota_status = {'available': False, 'current': '', 'latest': '', 'last_check': 0, 'updating': False}

def _get_local_version():
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return '0.0.0'

_reconcile_pending_ota_result()

def _semver_gt(a, b):
    """Return True if version string `a` is semantically greater than `b`.

    Used by the OTA check so the 'update available' banner only fires
    when remote is genuinely NEWER than local. Previous logic just used
    string inequality (a != b), which fired the banner whenever the
    api/version.php cache returned a stale OLDER value than what the
    Pi already had locally — surfacing a phantom 'update available'
    that Guardrail #1 then aborted. With this helper, that false
    positive is suppressed at the source.

    Parses '1.0.22' style strings into integer tuples. Returns False
    on any parse error (safer than crashing the OTA loop). Tuples
    compare element-by-element: (1,0,22) > (1,0,21) is True;
    (1,0,21) > (1,0,22) is False; (1,0,22) > (1,0,22) is False.
    """
    try:
        # Parse all dotted parts (not capped at 3) so 4-part tweak versions
        # like '1.2.10.1' compare correctly. Pad the shorter side with
        # zeros so '1.2.9' and '1.2.9.0' compare equal (no spurious update).
        parse = lambda s: [int(p) for p in s.strip().split('.') if p != '']
        pa, pb = parse(a), parse(b)
        n = max(len(pa), len(pb))
        pa += [0] * (n - len(pa))
        pb += [0] * (n - len(pb))
        return tuple(pa) > tuple(pb)
    except (ValueError, AttributeError, TypeError):
        return False

def _is_auto_update_enabled():
    """Whether to silently auto-install non-required updates.

    Default is FALSE as of v0.1.11 — the new UX shows a banner on
    the dashboard and waits for the user to click "Install now".
    Users who prefer the old silent-update behaviour can opt in by
    setting `"auto_update": true` in their ~/pilnk/config.json.
    Required updates (security/breaking) ignore this flag and
    install immediately regardless.
    """
    config_path = os.path.join(PILNK_DIR, 'config.json')
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
            return cfg.get('auto_update', False)
    except:
        return False

def _run_update():
    global ota_last_update
    ota_status['updating'] = True
    print('[PILNK-OTA] Starting update...')
    # Recorded BEFORE the attempt: a successful update.sh restarts this
    # process, so there is no "after" from which to record success. The next
    # boot's _reconcile_pending_ota_result() turns this marker into
    # 'success' or 'interrupted' by whether VERSION actually moved.
    _save_ota_state(last_result='started@' + _get_local_version(),
                    last_result_ts=time.time())
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
        _save_ota_last_update(ota_last_update)   # GUARDRAIL #2: persist
        ota_status['updating'] = False
        # Reaching HERE means update.sh exited without restarting us —
        # an abort (exit 3 = Rule #28 mismatch, 4 = restart blocked, ...)
        # or a failure. Record which: this is the number that would have
        # named M0CRT's fault in one fleet query.
        _save_ota_state(
            last_result=('success' if result.returncode == 0
                         else 'exit_%d' % result.returncode),
            last_result_ts=time.time())
        return result.returncode == 0
    except Exception as e:
        print(f'[PILNK-OTA] Update failed: {e}')
        ota_status['updating'] = False
        _save_ota_state(last_result='error:' + type(e).__name__,
                        last_result_ts=time.time())
        return False

def _perform_ota_check(auto_install_on_available=True):
    """Single canonical OTA check. Hits pilnk.io/api/version.php, compares
    against local VERSION, updates ota_status, and returns a result dict
    so callers (timer thread or manual /api/ota/check) can act on it.

    auto_install_on_available controls whether a detected update may be
    installed inline:
      - True  (default, used by the background timer): if remote sets
              required:true OR local config has auto_update:true, the
              update installs synchronously before returning. This is
              fine for the timer — nobody is waiting for an HTTP
              response.
      - False (used by the manual /api/ota/check endpoint): NEVER auto-
              installs, even if required or auto_update is set. Just
              flags ota_status['available']=True so the dashboard banner
              appears. The user clicks Install Now to trigger the
              actual install via /api/ota/install. This avoids killing
              the HTTP response mid-flight when update.sh restarts the
              service — which previously surfaced as a misleading
              "connection error" toast on the Check button.
    """
    try:
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

        if remote_ver and _semver_gt(remote_ver, local_ver):
            ota_status['available'] = True
            print(f'[PILNK-OTA] Update available: {local_ver} → {remote_ver}')
            if auto_install_on_available:
                if required:
                    print(f'[PILNK-OTA] Required update — installing immediately (overrides user preference)')
                    _run_update()
                elif _is_auto_update_enabled():
                    # The audio-active deferral that used to guard this call is gone
                    # along with the rtl_fm stack it protected (v1.3.1). rtl_fm ran as
                    # a CHILD of this service, so restarting pilnk cut audio mid-stream
                    # and it never resumed. pilnkradio is its own systemd unit and
                    # survives a pilnk restart untouched, so there is nothing left to
                    # defer for. Leaving the old check in place was worse than useless:
                    # it referenced a deleted global, and the NameError was swallowed by
                    # the except below, silently skipping every auto-install.
                    print(f'[PILNK-OTA] auto_update=true in config - installing silently')
                    _run_update()
                else:
                    print(f'[PILNK-OTA] Update available — waiting for user to click Install on dashboard')
            else:
                print(f'[PILNK-OTA] Manual check — banner will prompt user; not auto-installing on this path')
            return {'ok': True, 'available': True, 'current': local_ver, 'latest': remote_ver}
        else:
            ota_status['available'] = False
            return {'ok': True, 'available': False, 'current': local_ver, 'latest': remote_ver}
    except Exception as e:
        print(f'[PILNK-OTA] Check failed: {e}')
        return {'ok': False, 'error': str(e)}

def ota_checker():
    """Background timer thread that calls _perform_ota_check() on a loop.
    Passes auto_install_on_available=True so the timer preserves the
    silent-update behavior for users who have auto_update enabled or
    when the release is marked required.
    """
    global ota_last_update
    time.sleep(60)  # wait 60s after startup before first check
    while True:
        # Cooldown check — skip the next check if we just ran an update
        if time.time() - ota_last_update < OTA_COOLDOWN:
            time.sleep(OTA_CHECK_INTERVAL)
            continue
        _perform_ota_check(auto_install_on_available=True)
        time.sleep(OTA_CHECK_INTERVAL)

# Start ping thread — placed here, after _get_local_version() and the rest of
# the module are defined, so the daemon can't fire its first ping before its
# dependencies exist (fixes the startup "name '_get_local_version' is not
# defined" race).
if NODE_VERIFY_CODE != 'YOUR_VERIFY_CODE_HERE':
    ping_thread = threading.Thread(target=ping_server, daemon=True)
    ping_thread.start()
    print('[PILNK] Server ping active — reporting to pilnk.io')
else:
    # No code in config.json — start Phase 2 pairing flow
    _start_pairing_flow()

# ── Pairing status endpoint (polled by local dashboard while unclaimed)
@app.route('/api/pairing/status', methods=['GET'])
def api_pairing_status():
    with pairing_state_lock:
        return jsonify({
            'active':       pairing_state['active'],
            'claimed':      pairing_state['claimed'],
            'pairing_code': pairing_state['pairing_code'],
            'error':        pairing_state['error'],
        })

# ── Badges tab proxy (badges Phase 4, 28 Aug 2026) ───────────
# The dashboard's Badges tab shows the OWNER's achievements without a
# pilnk.io login: the node's verify_code is the credential, and this proxy
# keeps it server-side (the browser never sees the code — the tab just
# calls /api/badges). 5-minute cache; if pilnk.io is unreachable we serve
# the last good copy marked stale rather than an empty wall.
_BADGE_CACHE = {'ts': 0.0, 'data': None}

# ── Award toasts (badges Phase 4b, 29 Aug 2026) ──────────────
# Every badge must announce ON THE DASHBOARD, not just the site (AJ).
# The node keeps its own record of which awards it has already announced
# (badge_announced.json). Each /api/badges response carries new_awards =
# earned-but-never-announced, then marks them announced. NODE-LOCAL by
# design: no schema change, works offline, independent of the site's
# `seen` flag so both surfaces can celebrate in their own way.
# First run (no file yet) SEEDS silently — a node's first sync announces
# nothing, so fleet rollout doesn't toast months of history; the launch
# cascade belongs to the site. new_awards is computed FRESH on every
# request and never stored in _BADGE_CACHE — a cached copy must not
# replay yesterday's celebration.
BADGE_ANNOUNCED_FILE = os.path.join(PILNK_DIR, 'badge_announced.json')

def _load_announced():
    try:
        with open(BADGE_ANNOUNCED_FILE, 'r') as f:
            d = json.load(f)
            return set(d.get('announced', [])), True
    except Exception:
        return set(), False

def _save_announced(slugs):
    try:
        with open(BADGE_ANNOUNCED_FILE, 'w') as f:
            json.dump({'announced': sorted(slugs)}, f)
    except Exception as e:
        print(f'[PILNK] Warning: could not persist announced badges: {e}')

def _diff_new_awards(data):
    """Return renderable new-award dicts. DOES NOT mark them announced —
    the first live test caught the flaw: marking on GET means the first
    screen to poll (AJ's kiosk) eats the toast and every other dashboard
    stays silent. Marking now happens via POST /api/badges/ack AFTER a
    client has actually shown the toast, so every open dashboard gets to
    celebrate and each keeps its own session dedupe."""
    try:
        earned = set((data.get('earned') or {}).keys())
        # XP / Rank (v1.0): a promotion rides the same toast/ack/dedupe path
        # as a badge, as a synthetic slug 'rank:<index>'. Seeds silently the
        # first time this node sees the rank feature (no phantom toast for
        # the starting rank at rollout); every later rise toasts once.
        rank = data.get('rank') or {}
        rank_slug = None
        if isinstance(rank, dict) and rank.get('rank_index') is not None:
            rank_slug = 'rank:%d' % int(rank.get('rank_index') or 0)
        announced, existed = _load_announced()
        if not existed:
            _save_announced(earned | ({rank_slug} if rank_slug else set()))   # silent seed on first sync
            return []
        if rank_slug:
            if not any(a.startswith('rank:') for a in announced):
                announced.add(rank_slug)                 # first sight of ranks: seed, don't toast
                _save_announced(announced)
            earned.add(rank_slug)
        new = earned - announced
        if not new:
            return []
        defs_by_slug = {d.get('slug'): d for d in (data.get('defs') or [])}
        out = []
        for slug in sorted(new):
            if slug.startswith('rank:'):
                out.append({
                    'slug': slug,
                    'kind': 'promotion',
                    'name': rank.get('rank_name', ''),
                    'rank_index': int(rank.get('rank_index') or 0),
                    'xp': rank.get('xp'),
                    'next_name': rank.get('next_name'),
                    'xp_remaining': rank.get('xp_remaining'),
                })
                continue
            d = defs_by_slug.get(slug) or {}
            out.append({
                'slug': slug,
                'name': d.get('name', slug),
                'category': d.get('category', ''),
                'tier': d.get('tier', 0),
                'icon': d.get('icon', ''),
                'serial': (data['earned'].get(slug) or {}).get('serial'),
            })
        return out
    except Exception as e:
        print(f'[PILNK] Award diff failed (continuing): {e}')
        return []

@app.route('/api/badges/ack', methods=['POST'])
def api_badges_ack():
    """A dashboard confirms it has shown toasts for these slugs."""
    try:
        body = request.get_json(silent=True) or {}
        slugs = body.get('slugs') or []
        if not isinstance(slugs, list):
            return jsonify({'ok': False}), 400
        announced, _ = _load_announced()
        _save_announced(announced | set(str(s)[:64] for s in slugs[:200]))
        return jsonify({'ok': True})
    except Exception as e:
        print(f'[PILNK] Badge ack failed: {e}')
        return jsonify({'ok': False}), 500

@app.route('/api/badges', methods=['GET'])
def api_badges():
    now = time.time()
    if _BADGE_CACHE['data'] is not None and (now - _BADGE_CACHE['ts']) < 300:
        out = dict(_BADGE_CACHE['data'])
        out['cached'] = True
        out['new_awards'] = _diff_new_awards(out)
        return jsonify(out)
    try:
        req = urllib.request.Request(
            'https://pilnk.io/api/achievements.php?action=node_showcase&verify_code=' + NODE_VERIFY_CODE,
            headers={'User-Agent': 'PiLNK/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if not isinstance(data, dict) or 'defs' not in data:
            raise ValueError('unexpected payload')
        data['fetched_at'] = int(now)
        _BADGE_CACHE['ts'] = now
        _BADGE_CACHE['data'] = data
        out = dict(data)
        out['new_awards'] = _diff_new_awards(out)
        return jsonify(out)
    except Exception as e:
        print(f'[PILNK] Badge sync failed (serving cache if any): {e}')
        if _BADGE_CACHE['data'] is not None:
            out = dict(_BADGE_CACHE['data'])
            out['stale'] = True
            out['new_awards'] = []
            return jsonify(out)
        return jsonify({'error': 'badge sync unavailable', 'detail': type(e).__name__}), 503

# Start OTA checker thread
ota_thread = threading.Thread(target=ota_checker, daemon=True)
ota_thread.start()
print(f'[PILNK-OTA] Update checker active — checking every {OTA_CHECK_INTERVAL // 60} minutes')

# Manual on-demand check — bypasses the timer entirely. Use this when
# you've just pushed a release and don't want to wait for the next
# scheduled poll. Safe to call repeatedly. Returns the same shape as
# /api/ota/status would after a successful check.
#
# IMPORTANT: passes auto_install_on_available=False so this endpoint
# NEVER installs inline, even when auto_update is enabled. Reason: if
# we install synchronously here, update.sh restarts the service and
# kills the HTTP response mid-flight — the browser sees this as a
# misleading "connection error" toast even though the update worked.
# Banner-based Install Now flow is the explicit user path; background
# timer handles silent auto-update for users who want it.
@app.route('/api/ota/check', methods=['POST'])
def api_ota_check():
    if ota_status.get('updating'):
        return jsonify({'ok': False, 'error': 'Update already in progress'}), 409
    result = _perform_ota_check(auto_install_on_available=False)
    return jsonify(result), (200 if result.get('ok') else 502)

# ── OTA Dashboard API ─────────────────────────────────────
# Polled by the dashboard banner. Returns current/latest version
# and a flag the dashboard uses to render the "Install now" CTA.
@app.route('/api/ota/status', methods=['GET'])
def api_ota_status():
    return jsonify({
        'current':     _get_local_version(),
        'latest':      ota_status.get('latest', ''),
        'available':   bool(ota_status.get('available', False)),
        'updating':    bool(ota_status.get('updating', False)),
        'last_check':  ota_status.get('last_check', 0),
        'auto_update': _is_auto_update_enabled()
    })

# Manual install trigger — called when the user clicks "Install now"
# on the dashboard banner. Runs update.sh in a background thread so
# the HTTP response returns immediately; the dashboard polls
# /api/ota/status to track progress and detect post-restart recovery.
@app.route('/api/ota/install', methods=['POST'])
def api_ota_install():
    if ota_status.get('updating'):
        return jsonify({'success': False, 'error': 'Update already in progress'}), 409
    if not ota_status.get('available'):
        return jsonify({'success': False, 'error': 'No update currently available'}), 400
    threading.Thread(target=_run_update, daemon=True, name='ota-install-manual').start()
    return jsonify({'success': True, 'message': 'Update started — service will restart in ~30s'})

current_frequency = 118.7e6
current_gain      = 35
current_squelch   = 50


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/net/status')
def net_status():
    """Network liveness for the dashboard OFFLINE pill. Reports whether this
    node is paired and whether its last successful ping to pilnk.io is recent.
    online = paired AND pinged within NET_STALE_S. Safe on every node: an
    unpaired node returns paired=false so the UI shows the pairing banner
    rather than a false OFFLINE alarm."""
    now = time.time()
    ts = PING_LAST_OK_TS
    paired = bool(NODE_VERIFY_CODE)
    age = (now - ts) if ts else None
    online = bool(paired and ts and (now - ts) < NET_STALE_S)
    return jsonify({
        'paired': paired,
        'online': online,
        'last_ok_age_s': round(age, 1) if age is not None else None,
        'stale_after_s': NET_STALE_S,
    })

@app.route('/api/services')
def services_status():
    """systemd health for the System tab. Read-only `systemctl show` (no root).
    Only units that actually exist on this node are returned, so nodes with a
    different stack never show phantom rows. Fully defensive — any failure just
    omits that unit."""
    import subprocess
    candidates = ['pilnk', 'dump1090-fa', 'dump978-fa']
    out = []
    for unit in candidates:
        try:
            r = subprocess.run(
                ['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState'],
                capture_output=True, text=True, timeout=4
            )
            props = {}
            for line in r.stdout.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    props[k] = v
            if props.get('LoadState') != 'loaded':
                continue  # unit not installed on this node — skip
            out.append({
                'name': unit,
                'active': props.get('ActiveState', '') == 'active',
                'state': props.get('ActiveState', 'unknown'),
                'sub': props.get('SubState', ''),
            })
        except Exception:
            continue
    return jsonify({'services': out})

@app.route('/api/system')
def system_health():
    """Node health for the System tab: CPU/mem/disk/temp/uptime (psutil),
    receiver RF stats (dump1090 stats.json), and version/OTA. Every field is
    optional — anything unavailable returns null so the UI degrades cleanly.
    NOTE: the receiver._debug_* fields are temporary probe aids to surface the
    real stats.json key names; they get stripped once the shape is confirmed."""
    info = {'vitals': {}, 'receiver': {}, 'version': {}}

    # ── System vitals (psutil) ──
    try:
        import psutil
        v = info['vitals']
        try: v['load1'] = round(os.getloadavg()[0], 2)
        except Exception: v['load1'] = None
        v['cpu_pct'] = psutil.cpu_percent(interval=0.2)
        v['mem_pct'] = psutil.virtual_memory().percent
        v['disk_pct'] = psutil.disk_usage('/').percent
        try: v['uptime_s'] = int(time.time() - psutil.boot_time())
        except Exception: v['uptime_s'] = None
        temp = None
        try:
            temps = psutil.sensors_temperatures() or {}
            for key in ('cpu_thermal', 'coretemp', 'cpu-thermal'):
                if key in temps and temps[key]:
                    temp = round(temps[key][0].current, 1); break
            if temp is None:
                for arr in temps.values():
                    if arr:
                        temp = round(arr[0].current, 1); break
        except Exception:
            temp = None
        if temp is None:
            try:
                with open('/sys/class/thermal/thermal_zone0/temp') as f:
                    temp = round(int(f.read().strip()) / 1000.0, 1)
            except Exception:
                temp = None
        v['temp_c'] = temp
    except Exception as e:
        info['vitals_error'] = str(e)

    # ── Receiver RF (dump1090 stats.json, same dir as aircraft.json) ──
    try:
        stats_path = os.path.join(os.path.dirname(DUMP1090_AIRCRAFT_JSON), 'stats.json')
        with open(stats_path) as f:
            sj = json.load(f)
        rx = info['receiver']
        blk = sj.get('last1min') or sj.get('total') or {}
        local = blk.get('local') or {}
        sig = local.get('signal')
        noise = local.get('noise')
        rx['signal'] = sig
        rx['noise'] = noise
        rx['snr'] = round(sig - noise, 1) if (sig is not None and noise is not None) else None
        rx['peak_signal'] = local.get('peak_signal')
        rx['strong_signals'] = local.get('strong_signals')
        rx['gain_db'] = local.get('gain_db')
        msgs = blk.get('messages')
        rx['messages_1min'] = msgs
        rx['msg_per_sec'] = round(msgs / 60.0, 1) if isinstance(msgs, (int, float)) else None
    except Exception as e:
        info['receiver_error'] = str(e)

    # ── Version / OTA (already tracked) ──
    try:
        info['version'] = {
            'current': ota_status.get('current') or _get_local_version(),
            'latest': ota_status.get('latest'),
            'available': ota_status.get('available', False),
            'last_check': ota_status.get('last_check'),
        }
    except Exception as e:
        info['version_error'] = str(e)

    return jsonify(info)


# ── Mode S Comm-B enrichment ("Hidden Sky Data") ───────────────────────────
# Passive consumer of dump1090-fa's raw AVR stream on :30002. Decodes Comm-B
# (DF20/21) replies — BDS 4,0 / 5,0 / 6,0 / 4,4 — with pyModeS v3 and caches
# the extra fields per ICAO; /flights merges fresh entries additively. Never
# writes to dump1090-fa. Degrades gracefully: if pyModeS is missing or :30002
# is down, enrichment is simply absent and /flights returns standard data.
try:
    import pyModeS as _pms
except Exception:
    _pms = None

BDS_PORT = 30002
BDS_CACHE_TTL = 60            # seconds a cached field stays valid for display
enrichment_cache = {}        # {ICAO_UPPER: {field: value, ..., '_updated': ts}}
_bds_lock = threading.Lock()
_BDS_SKIP = {'df', 'icao', 'crc_valid', 'icao_verified', 'bds'}
_BDS_FIELDS = {
    'bds40': ('selected_altitude_mcp', 'selected_altitude_fms', 'baro_pressure_setting',
              'vnav_mode', 'altitude_hold_mode', 'approach_mode'),
    'bds50': ('roll', 'true_track', 'track_rate', 'true_airspeed'),
    'bds60': ('magnetic_heading', 'indicated_airspeed', 'mach',
              'baro_vertical_rate', 'inertial_vertical_rate'),
    'bds44': ('wind_speed', 'wind_direction', 'temperature'),
}


def _bds_enrichment_loop():
    """Daemon: read :30002, decode Comm-B replies, cache decoded fields per ICAO."""
    seen = 0
    while True:
        try:
            sock = socket.socket()
            sock.connect(('127.0.0.1', BDS_PORT))
            logging.info('[bds] connected to dump1090-fa :%d', BDS_PORT)
            buf = ''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError('stream closed')
                buf += chunk.decode(errors='ignore')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    msg = line.strip().lstrip('*').rstrip(';').strip()
                    if len(msg) != 28:           # Comm-B = 112-bit (28 hex chars)
                        continue
                    try:
                        if (int(msg[0:2], 16) >> 3) not in (20, 21):
                            continue
                        res = _pms.decode(msg)
                        # pyModeS 3.6+ returns crc_valid=None for DF20/21 Comm-B —
                        # an address-overlaid CRC can't be validated standalone (it
                        # also sets icao_verified=False). The original gate tested
                        # `not res.get('crc_valid')`, and `not None` is True, so every
                        # frame was rejected and the enrichment cache never filled.
                        # Reject ONLY an explicit False: this accepts True (pyModeS
                        # <=3.3, e.g. Pi5) and None (3.6+, e.g. a fresh install), so
                        # it is correct on every node whatever pip pulled in.
                        if not res or not res.get('icao') or res.get('crc_valid') is False:
                            continue
                        icao = res['icao'].upper()
                        now = time.time()
                        with _bds_lock:
                            rec = enrichment_cache.setdefault(icao, {})
                            for k, v in res.items():
                                if k not in _BDS_SKIP and v is not None:
                                    rec[k] = v
                            rec['_updated'] = now
                            seen += 1
                            if seen % 2000 == 0:  # bound memory: evict long-stale ICAOs
                                cutoff = now - BDS_CACHE_TTL * 5
                                stale = [h for h, r in enrichment_cache.items()
                                         if r.get('_updated', 0) < cutoff]
                                for h in stale:
                                    del enrichment_cache[h]
                    except Exception:
                        pass
        except Exception as e:
            logging.warning('[bds] stream error (%s) — reconnecting in 5s', e)
            time.sleep(5)


def _merge_bds(ac, icao_upper):
    """Additively merge fresh (<TTL) cached BDS fields onto one aircraft dict."""
    now = time.time()
    with _bds_lock:
        rec = enrichment_cache.get(icao_upper)
        if not rec or (now - rec.get('_updated', 0)) >= BDS_CACHE_TTL:
            return
        for prefix, fields in _BDS_FIELDS.items():
            for f in fields:
                if f in rec:
                    ac['{}_{}'.format(prefix, f)] = rec[f]


def _bds_bootstrap():
    """Start Mode S enrichment, self-installing pyModeS if it is missing.

    Existing nodes that OTA-update into a build that needs pyModeS will not have
    it yet: the installer only adds it for fresh installs, and the OTA path
    pulls code but never runs pip. So on first run we attempt a one-shot install
    into the service user's ~/.local (--user, so no sudo is needed;
    --break-system-packages for PEP 668 on Trixie/Bookworm), make it importable
    in this already-running process, then re-import. numpy is already present
    from the base install, so this is a small, fast download. Runs in a daemon
    thread so the dashboard never blocks on it; if the install cannot run
    (offline, locked-down host, no pip, ...) the feature simply stays dark — no
    crash, identical to the previous behaviour.
    """
    global _pms
    if _pms is None:
        try:
            import subprocess, sys, site, importlib
            logging.info('[bds] pyModeS missing — attempting one-shot --user install')
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--user',
                 '--break-system-packages', '--disable-pip-version-check',
                 '-q', 'pyModeS'],
                timeout=600, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # ~/.local may not have been on sys.path at startup (e.g. it did not
            # exist yet) — add the user site dir so the fresh install imports now.
            usp = site.getusersitepackages()
            if usp and usp not in sys.path:
                site.addsitedir(usp)
            importlib.invalidate_caches()
            import pyModeS as _pms_new
            _pms = _pms_new
            logging.info('[bds] pyModeS installed — Mode S enrichment enabled')
        except Exception as e:
            logging.warning('[bds] pyModeS unavailable (%s) — Mode S enrichment disabled', e)
            _pms = None
    if _pms is not None:
        _bds_enrichment_loop()


threading.Thread(target=_bds_bootstrap, name='bds-bootstrap', daemon=True).start()


# ── Military aircraft overlay ────────────────────────
# Loaded once at startup from mil_catalog_seed.json (generated from the myHost
# mil_hex_ranges + mil_aircraft_catalog tables). classify_icao() is called for
# every aircraft on every /flights poll, so it stays pure in-memory — no DB or
# network in the request cycle.
MIL_HEX_RANGES = []        # [{'start': int, 'end': int, 'cc': str, 'branch': str}]
MIL_AIRCRAFT_CATALOG = {}  # keyed by ICAO type designator (uppercase)

def load_military_data():
    """Load military hex ranges + aircraft catalog into memory from the seed file."""
    global MIL_HEX_RANGES, MIL_AIRCRAFT_CATALOG
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mil_catalog_seed.json')
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        ranges = []
        for r in data.get('hex_ranges', []):
            ranges.append({
                'start': int(r['start'], 16),
                'end': int(r['end'], 16),
                'cc': r.get('cc', ''),
                'branch': r.get('branch', ''),
            })
        MIL_HEX_RANGES = ranges
        MIL_AIRCRAFT_CATALOG = {k.upper(): v for k, v in data.get('catalog', {}).items()}
        logging.info('[MIL] loaded %d hex ranges, %d catalog types',
                     len(MIL_HEX_RANGES), len(MIL_AIRCRAFT_CATALOG))
    except Exception as e:
        logging.warning('[MIL] could not load military catalog: %s', e)
        MIL_HEX_RANGES = []
        MIL_AIRCRAFT_CATALOG = {}

def classify_icao(hex_code):
    """Fast: does this hex fall inside a military range? Returns the matching
    range dict (cc/branch) or None. Called per-aircraft, per-poll."""
    if not hex_code or not MIL_HEX_RANGES:
        return None
    try:
        h = int(hex_code, 16)
    except ValueError:
        return None
    for r in MIL_HEX_RANGES:
        if r['start'] <= h <= r['end']:
            return r
    return None

def match_aircraft_type(icao_type):
    """Look up an ICAO type designator in the in-memory catalog. Returns the
    catalog entry dict or None."""
    if not icao_type:
        return None
    return MIL_AIRCRAFT_CATALOG.get(icao_type.upper())

load_military_data()


# ── Gone-dark ghost tracking (military transponder suppression) ──
# In-memory + ephemeral: a military aircraft seen airborne that stops
# transmitting becomes a "ghost" marker at its last position, fading over
# GHOST_TTL_S. NOTE: unrelated to the ghost_aircraft DB / ghost.php, which is
# the community hex-identification game.
GHOST_GRACE_S = 20      # ignore momentary gaps (still actively transmitting)
GHOST_TTL_S = 300       # ghost lifetime (client fades opacity over this)
GHOST_MIN_ALT = 500     # must have been airborne when last seen
_mil_seen = {}          # hex -> {ts,lat,lon,alt,callsign,emoji,name,rarity}
_mil_seen_lock = threading.Lock()

def _track_mil(hex_code, ac):
    """Record a currently-visible military aircraft's last-known state."""
    lat = ac.get('lat'); lon = ac.get('lon')
    if lat is None or lon is None:
        return
    alt = ac.get('alt_baro')
    if alt == 'ground':
        alt = 0
    try:
        alt = float(alt)
    except (TypeError, ValueError):
        alt = 0
    with _mil_seen_lock:
        _mil_seen[hex_code] = {
            'ts': time.time(), 'lat': lat, 'lon': lon, 'alt': alt,
            'callsign': (ac.get('flight') or '').strip(),
            'emoji': ac.get('mil_emoji', ''), 'name': ac.get('mil_common_name', ''),
            'rarity': ac.get('mil_rarity', ''),
        }


# --- ATC STT transcript (fed by the node-local atc_service daemon) -----------
# Resolved from the SERVICE USER's home, never a hardcoded username. This read
# '/home/aj/...' — correct on the development node and wrong on every other one
# in the fleet, where the service runs as 'pi'. It fails safe (see the route
# below), so nothing broke — but when STT ships fleet-wide it would have been
# silently dead everywhere except here, which is exactly how the audio engine
# managed to look shipped for six weeks without ever running on another node.
ATC_TRANSCRIPT_PATH = os.path.join(os.path.expanduser('~'), 'atc-stt', 'atc_transcript.json')
ATC_TRANSCRIPT_STALE_SECS = 120   # no update in this long => treat the daemon as down


@app.route('/atc/transcript')
def atc_transcript():
    """Serve the latest reconciled ATC transmissions for the dashboard slide.

    Read-only pass-through of the daemon's atomic JSON. Fails safe: if the file is
    missing/corrupt or hasn't updated recently, return an empty, not-running result
    so the slide simply shows nothing (fully decoupled from the STT daemon).
    """
    try:
        with open(ATC_TRANSCRIPT_PATH) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return jsonify({'running': False, 'stale': True, 'freq_mhz': None,
                        'updated': 0, 'lines': []})
    age = time.time() - doc.get('updated', 0)
    doc['stale'] = age > ATC_TRANSCRIPT_STALE_SECS
    if doc['stale']:
        doc['running'] = False
    return jsonify(doc)


@app.route('/flights')
def flights():
    """Return aircraft data with type/registration enrichment from AIRCRAFT_DB.

    If AIRCRAFT_DB is empty (file missing on disk), we pass through
    aircraft.json untouched — same behaviour as pre-v1.0.7.
    """
    raw = read_aircraft_json()
    if raw is None:
        return jsonify({'aircraft': []})

    # Fast path: nothing to enrich at all → pass raw bytes through unchanged.
    # Engages only when there's neither a type/reg DB nor live Mode S data.
    if not AIRCRAFT_DB and not enrichment_cache and not MIL_HEX_RANGES:
        return Response(raw, mimetype='application/json')

    # Slow path: parse, enrich, re-serialize. Adds ~5ms for typical
    # 50-aircraft payload. dump1090's hex field is lowercase; AIRCRAFT_DB
    # is keyed uppercase. Mode S Comm-B fields (bds40_*/bds50_*/bds60_*/bds44_*)
    # are merged additively when fresh.
    try:
        data = json.loads(raw)
        for ac in data.get('aircraft', []):
            hex_code = (ac.get('hex') or '').upper()
            if not hex_code:
                continue
            entry = AIRCRAFT_DB.get(hex_code)
            if entry:
                # Only fill if dump1090 didn't already provide it (rare, but possible)
                if not ac.get('t') and entry['t']:
                    ac['t'] = entry['t']
                if not ac.get('r') and entry['r']:
                    ac['r'] = entry['r']
            # Mode S Comm-B enrichment — additive, no-op without fresh cache
            _merge_bds(ac, hex_code)
            # Military overlay — fast in-memory hex-range + type classification
            _mil = classify_icao(hex_code)
            if _mil:
                ac['is_military'] = True
                _cat = match_aircraft_type(ac.get('t'))
                if _cat:
                    ac['mil_common_name'] = _cat['name']
                    ac['mil_rarity'] = _cat['rarity']
                    ac['mil_emoji'] = _cat['emoji']
                    ac['mil_class'] = _cat['class']
                    ac['mil_branch'] = _cat['branch']
                    ac['mil_country'] = _cat.get('cc', '')
                else:
                    ac['mil_branch'] = _mil.get('branch', '')
                    ac['mil_country'] = _mil.get('cc', '')
                _track_mil(hex_code, ac)
            # 7777 = military intercept squawk (in some regions). Flag it on ANY
            # aircraft — an active intercept is notable whether or not the hex is
            # in a military range. Salvaged from the original overlay spec.
            if str(ac.get('squawk') or '').strip() == '7777':
                ac['mil_intercept'] = True
        return jsonify(data)
    except (ValueError, TypeError) as e:
        # Parse failure: fall back to raw passthrough so we never 500
        logging.warning(f'Enrichment failed, falling back to raw: {e}')
        return Response(raw, mimetype='application/json')

# ── OpenAIP proxy — avoids CORS issues in browser ─────────
@app.route('/api/gone_dark')
def gone_dark():
    """Military aircraft that recently went dark (stopped transmitting while
    airborne). Ephemeral / in-memory; client fades each over GHOST_TTL_S."""
    now = time.time()
    ghosts = []
    with _mil_seen_lock:
        stale = []
        for hexc, info in _mil_seen.items():
            age = now - info['ts']
            if age > GHOST_TTL_S:
                stale.append(hexc); continue
            if age < GHOST_GRACE_S:
                continue   # still actively transmitting
            if info['alt'] < GHOST_MIN_ALT:
                continue   # was on/near the ground
            ghosts.append({
                'hex': hexc, 'lat': info['lat'], 'lon': info['lon'],
                'callsign': info['callsign'], 'last_ts': info['ts'],
                'age_sec': round(age, 1), 'ttl': GHOST_TTL_S,
                'emoji': info['emoji'], 'name': info['name'], 'rarity': info['rarity'],
            })
        for h in stale:
            _mil_seen.pop(h, None)
    return jsonify({'ghosts': ghosts})


# ── OpenAIP proxy — Aviation Overlay (NAVAIDs, airspaces) ────────
#
# ON THE KEY IN SOURCE. Deliberate, AJ's call 3 Sep 2026: OpenAIP is a
# free, read-only aeronautical-data API, this repo is public and GPL,
# and moving the key to config buys little against that threat model.
# Written down so it reads as a decision and not an oversight. If
# OpenAIP ever puts writes or billing behind this key, that reasoning
# no longer holds and it must move out of source.
#
# CACHE + RETRY, added 3 Sep 2026. AJ's dashboard showed
# "Error: navaids 500"; the endpoint answered perfectly seconds later.
# A transient upstream blip, and it got no second chance — this made
# exactly one attempt, and the browser then threw away BOTH layers
# because either failing rejected the pair. Ninety-nine airspaces that
# had loaded fine were discarded along with it.
#
# Aeronautical reference data changes on AIRAC cycles, not by the
# minute, so caching it is free resilience: most overlay toggles never
# leave the Pi, and a blip upstream is usually invisible. On total
# failure we serve stale cache if we have any — an hour-old NAVAID
# list beats an empty map for data that barely moves.
_OPENAIP_CACHE = {}
_OPENAIP_TTL = 900          # 15 minutes


@app.route('/api/openaip/<path:endpoint>')
def openaip_proxy(endpoint):
    OPENAIP_KEY = '7670c503a1c0929ee8e87ad581d9119e'
    params = request.args.to_dict()
    cache_key = endpoint + '?' + '&'.join('%s=%s' % kv for kv in sorted(params.items()))

    hit = _OPENAIP_CACHE.get(cache_key)
    if hit and (time.time() - hit[0]) < _OPENAIP_TTL:
        return jsonify(hit[1])

    params['apiKey'] = OPENAIP_KEY
    url = f'https://api.core.openaip.net/api/{endpoint}'

    last_err = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                _OPENAIP_CACHE[cache_key] = (time.time(), data)
                return jsonify(data)
            last_err = 'upstream HTTP %s' % r.status_code
        except Exception as e:
            last_err = str(e)
        if attempt == 1:
            time.sleep(1.5)

    if hit:
        # Stale, but real. Better than a blank overlay.
        return jsonify(hit[1])

    app.logger.warning('[openaip] %s failed after 2 attempts: %s', endpoint, last_err)
    # 502, not 500: the failure is upstream, not in this node.
    return jsonify({'error': last_err, 'items': []}), 502

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

# ── RainViewer tile proxy — same-origin tiles for cloud cover pixel sampling ──
# Proxies PNG tiles from tilecache.rainviewer.com so the browser treats them as
# same-origin, allowing canvas getImageData() without tainting. Only used when
# the cloud cover toggle is on; normal rain overlay still hits tilecache direct.
# ── RainViewer tile proxy (same-origin) + server-side cache ──
# Serves PNG radar tiles same-origin (so canvas getImageData() isn't tainted) AND
# caches them in memory so the radar overlay + cloud-cover sampler don't hammer
# RainViewer, which 429s under load. Repeated tiles (animation loops, re-pans, both
# layers) come from cache; stale tiles are served when RainViewer errors/throttles.
_RV_TILE_CACHE = {}      # tile_path -> (timestamp, png_bytes)
_RV_TILE_TTL = 300       # seconds
def _rv_tile_response(content):
    resp = make_response(content)
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp
@app.route('/api/rv-tile/<path:tile_path>')
def rainviewer_tile_proxy(tile_path):
    now = time.time()
    ent = _RV_TILE_CACHE.get(tile_path)
    if ent and (now - ent[0]) < _RV_TILE_TTL:
        return _rv_tile_response(ent[1])
    try:
        r = requests.get(
            'https://tilecache.rainviewer.com/' + tile_path,
            timeout=5,
            headers={'User-Agent': 'PiLNK/1.0 (+https://pilnk.io)'}
        )
        if r.status_code == 200 and r.content:
            _RV_TILE_CACHE[tile_path] = (now, r.content)
            if len(_RV_TILE_CACHE) > 3000:
                for k in sorted(_RV_TILE_CACHE, key=lambda kk: _RV_TILE_CACHE[kk][0])[:1000]:
                    _RV_TILE_CACHE.pop(k, None)
            return _rv_tile_response(r.content)
        if ent:
            return _rv_tile_response(ent[1])           # serve stale on 429 / upstream error
        return make_response(b'', 204)
    except Exception:
        if ent:
            return _rv_tile_response(ent[1])
        return make_response(b'', 204)  # silent empty on failure — never block aircraft render

# ── LibreWXR proxy — OPERA (Europe) frame list, RainViewer-format ──
# LibreWXR (api.librewxr.net) is a RainViewer-compatible drop-in serving EUMETNET
# OPERA composites for Europe. EU nodes use it as their weather source; proxied
# here for the same reasons as RainViewer (same-origin + a stable origin).
@app.route('/api/librewxr')
def librewxr_proxy():
    try:
        r = requests.get('https://api.librewxr.net/public/weather-maps.json', timeout=10)
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── LibreWXR tile proxy (same-origin) + server-side cache ──
# Mirrors the RainViewer tile proxy: same-origin canvas-safe PNG tiles + a memory
# cache with stale-on-error so the EU radar + occlusion sampler don't hammer
# LibreWXR. NOTE: LibreWXR's smoothed tiles hang, so the dashboard requests the
# un-smoothed 0_0 option for these (handled frontend-side).
_LW_TILE_CACHE = {}      # tile_path -> (timestamp, png_bytes)
_LW_TILE_TTL = 300       # seconds
def _lw_tile_response(content):
    resp = make_response(content)
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp
@app.route('/api/lw-tile/<path:tile_path>')
def librewxr_tile_proxy(tile_path):
    now = time.time()
    ent = _LW_TILE_CACHE.get(tile_path)
    if ent and (now - ent[0]) < _LW_TILE_TTL:
        return _lw_tile_response(ent[1])
    try:
        r = requests.get(
            'https://api.librewxr.net/' + tile_path,
            timeout=5,
            headers={'User-Agent': 'PiLNK/1.0 (+https://pilnk.io)'}
        )
        if r.status_code == 200 and r.content:
            _LW_TILE_CACHE[tile_path] = (now, r.content)
            if len(_LW_TILE_CACHE) > 3000:
                for k in sorted(_LW_TILE_CACHE, key=lambda kk: _LW_TILE_CACHE[kk][0])[:1000]:
                    _LW_TILE_CACHE.pop(k, None)
            return _lw_tile_response(r.content)
        if ent:
            return _lw_tile_response(ent[1])           # serve stale on upstream error
        return make_response(b'', 204)
    except Exception:
        if ent:
            return _lw_tile_response(ent[1])
        return make_response(b'', 204)  # silent empty on failure — never block aircraft render

# ── NEXRAD tile proxy (same-origin) + cache — for cloud-cover occlusion sampling ──
# Iowa Mesonet n0q tiles proxied same-origin so the occlusion sampler can canvas-read
# them (the display layer hits Mesonet direct). A 5-min bust keeps the sampled frame
# tracking the live radar; short-TTL cache so panning doesn't hammer Mesonet.
_NX_TILE_CACHE = {}
_NX_TILE_TTL = 300
@app.route('/api/nexrad-tile/<int:z>/<int:x>/<int:y>')
def nexrad_tile_proxy(z, x, y):
    bust = int(time.time() // 300)
    key = '%d/%d/%d/%d' % (bust, z, x, y)
    now = time.time()
    ent = _NX_TILE_CACHE.get(key)
    if ent and (now - ent[0]) < _NX_TILE_TTL:
        return _lw_tile_response(ent[1])
    try:
        r = requests.get(
            'https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/%d/%d/%d.png' % (z, x, y),
            timeout=5,
            headers={'User-Agent': 'PiLNK/1.0 (+https://pilnk.io)'}
        )
        if r.status_code == 200 and r.content:
            _NX_TILE_CACHE[key] = (now, r.content)
            if len(_NX_TILE_CACHE) > 3000:
                for k in sorted(_NX_TILE_CACHE, key=lambda kk: _NX_TILE_CACHE[kk][0])[:1000]:
                    _NX_TILE_CACHE.pop(k, None)
            return _lw_tile_response(r.content)
        if ent:
            return _lw_tile_response(ent[1])
        return make_response(b'', 204)
    except Exception:
        if ent:
            return _lw_tile_response(ent[1])
        return make_response(b'', 204)

# ── Planespotters.net proxy — aircraft photos ──────────────
# ── Perf telemetry (dev prototype) — last FPS sample reported by the dashboard HUD ──
_perf_last = {}
@app.route('/api/perf', methods=['GET'])
def perf_get():
    return jsonify(_perf_last or {'note': 'no sample yet'})

@app.route('/api/perf/report', methods=['POST'])
def perf_report():
    global _perf_last
    try:
        d = request.get_json(force=True, silent=True) or {}
        _perf_last = {'fps': d.get('fps'), 'min': d.get('min'),
                      'ms': d.get('ms'), 'ac': d.get('ac'), 'ts': round(time.time())}
    except Exception:
        pass
    return ('', 204)

@app.route('/api/planespotters/<path:hex>')
def planespotters_proxy(hex):
    """Proxy to planespotters.net public photo API.

    Strategy:
      1. If client provides ?reg=X, call /pub/photos/reg/<reg>
      2. Otherwise fall back to /pub/photos/hex/<hex>

    User-Agent: planespotters/Cloudflare appears to discriminate based
    on UA. curl with a browser-style UA gets photos; python-requests
    default UA returns empty. We send a browser-style UA explicitly.
    """
    try:
        reg = request.args.get('reg', '').strip()
        if reg:
            url = 'https://api.planespotters.net/pub/photos/reg/' + reg
        else:
            url = 'https://api.planespotters.net/pub/photos/hex/' + hex
        headers = {
            'User-Agent': 'Mozilla/5.0 (PiLNK community ADS-B tracker; https://pilnk.io)',
            'Accept': 'application/json',
        }
        r = requests.get(url, headers=headers, timeout=8)
        # Diagnostic: log everything about the response so we can debug
        body_preview = r.content[:200] if r.content else b'(empty)'
        logging.info(f'[planespotters] {url} → status={r.status_code} '
                     f'len={len(r.content)} body_preview={body_preview!r}')
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        logging.error(f'[planespotters] exception: {e}')
        return jsonify({'photos': []}), 500

# ── Aircraft photo image cache ─────────────────────────────
# Planespotters images live on BunnyCDN with origin storage in Germany. From
# New Zealand — or anywhere without a warm edge — a cold fetch can take many
# seconds, which is long enough for the click-card's auto-dismiss to fire
# before the photo lands. That is indistinguishable from a broken image.
# So: fetch each photo ONCE, keep it on local disk, and serve every later
# request over the LAN. Every device in the house then shares one warm cache.
PHOTO_CACHE_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'photos')
PHOTO_CACHE_MAX_BYTES = 200 * 1024 * 1024      # prune once we exceed this
PHOTO_CACHE_PRUNE_TO  = 150 * 1024 * 1024      # ...back down to this
# SSRF guard: this endpoint takes a URL from the client, so the host is
# strictly allowlisted. NEVER widen this to a wildcard — that would turn
# every node in the fleet into an open proxy.
PHOTO_CACHE_HOSTS = {
    't.plnspttrs.net',
    'www.airport-data.com',
    'airport-data.com',
}
PHOTO_CACHE_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                     '.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}


def _photo_cache_prune():
    """Keep the cache under its ceiling, evicting least-recently-used first."""
    try:
        files, total = [], 0
        for name in os.listdir(PHOTO_CACHE_DIR):
            p = os.path.join(PHOTO_CACHE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= PHOTO_CACHE_MAX_BYTES:
            return
        files.sort()                                  # oldest touch first
        for _mtime, size, p in files:
            if total <= PHOTO_CACHE_PRUNE_TO:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
        logging.info(f'[photo-cache] pruned to {total} bytes')
    except Exception as e:
        logging.error(f'[photo-cache] prune failed: {e}')


@app.route('/api/photo-img')
def photo_img_proxy():
    """Disk-cached image proxy for aircraft photos.

    Query: ?url=<absolute https URL on an allowlisted host>

    First hit fetches from the CDN and writes to disk; every later hit is
    served from local disk. A cold international fetch therefore happens
    once per photo, not once per viewer per browser cache eviction.
    """
    import hashlib
    import urllib.parse

    raw = request.args.get('url', '').strip()
    if not raw:
        return jsonify({'error': 'missing url'}), 400
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return jsonify({'error': 'bad url'}), 400
    if u.scheme != 'https' or u.hostname not in PHOTO_CACHE_HOSTS:
        logging.warning(f'[photo-cache] refused host: {u.hostname}')
        return jsonify({'error': 'host not allowed'}), 403

    ext = os.path.splitext(u.path)[1].lower()
    if ext not in PHOTO_CACHE_TYPES:
        ext = '.jpg'
    key  = hashlib.sha256(raw.encode('utf-8')).hexdigest() + ext
    path = os.path.join(PHOTO_CACHE_DIR, key)

    # Warm: straight off the disk.
    if os.path.exists(path):
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
            os.utime(path, None)                      # touch = recently used
            resp = make_response(data)
            resp.headers['Content-Type']  = PHOTO_CACHE_TYPES[ext]
            resp.headers['Cache-Control'] = 'public, max-age=31536000'
            resp.headers['X-PiLNK-Photo-Cache'] = 'hit'
            return resp
        except OSError as e:
            logging.error(f'[photo-cache] read failed {key}: {e}')   # fall through and refetch

    # Cold: fetch once, store, serve.
    try:
        os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
        r = requests.get(raw, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (PiLNK community ADS-B tracker; https://pilnk.io)'})
        if r.status_code != 200 or not r.content:
            logging.info(f'[photo-cache] upstream {r.status_code} for {raw}')
            return jsonify({'error': 'upstream'}), 502
        tmp = path + '.part'
        with open(tmp, 'wb') as fh:
            fh.write(r.content)
        os.replace(tmp, path)                         # atomic: no half-written entries
        _photo_cache_prune()
        resp = make_response(r.content)
        resp.headers['Content-Type']  = r.headers.get('Content-Type', PHOTO_CACHE_TYPES[ext])
        resp.headers['Cache-Control'] = 'public, max-age=31536000'
        resp.headers['X-PiLNK-Photo-Cache'] = 'miss'
        return resp
    except Exception as e:
        logging.error(f'[photo-cache] fetch failed {raw}: {e}')
        return jsonify({'error': 'fetch failed'}), 502


# ── airport-data.com proxy — secondary aircraft photos ─────
@app.route('/api/acphoto/<path:hex>')
def acphoto_proxy(hex):
    """Proxy to airport-data.com's free ac_thumb.json photo API.

    Used as a SECONDARY photo source on the dashboard click-card: the
    dash tries PiLNK community photos, then Planespotters, and only
    calls this when both come up empty. Queried per-hex (Mode-S code).

    DISPLAY-ONLY: the result is shown in the card but is never written
    to the PiLNK community photo DB — that table stays real user uploads.

    airport-data.com returns {status,count,data:[{image,link,photographer}]}
    (200px thumbnails); we pass that JSON straight through. Browser-style
    UA + short timeout, same approach as the Planespotters proxy. On any
    error we return an empty result so the cascade simply shows no photo.
    """
    try:
        code = (hex or '').strip().upper()
        url = 'https://airport-data.com/api/ac_thumb.json?m=' + code + '&n=1'
        headers = {
            'User-Agent': 'Mozilla/5.0 (PiLNK community ADS-B tracker; https://pilnk.io)',
            'Accept': 'application/json',
        }
        r = requests.get(url, headers=headers, timeout=8)
        logging.info(f'[acphoto] {url} → status={r.status_code} len={len(r.content)}')
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        logging.error(f'[acphoto] exception: {e}')
        return jsonify({'status': 404, 'count': 0, 'data': []}), 500

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
                # Enrich with type/registration so the dashboard history search
                # can filter by aircraft type (e.g. "AN-124", "A380") — added v1.0.18
                't': AIRCRAFT_DB.get(hex_code.upper(), {}).get('t', ''),
                'r': AIRCRAFT_DB.get(hex_code.upper(), {}).get('r', ''),
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
        'aircraft': aircraft[:300],
        'hourly': hourly,
        'busiest_hour': busiest,
    })

# ── FIDS stub — flight information display ────────────────
# /api/fids endpoint removed 2026-05-09 — frontend FIDS strip + Delayed
# Labels removed in v0.1.17, this endpoint had no remaining callers and
# only ever returned []. Re-introduce when a real FIDS data source is wired.

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

# -- Coverage map (polar reception footprint) --
@app.route('/api/coverage')
def get_coverage():
    with node_stats_lock:
        return jsonify({
            'sectors': COVERAGE_SECTORS,
            'max_nm': coverage['max_nm'],
            'min_elev': coverage['min_elev'],
            'elev_min_nm': COVERAGE_ELEV_MIN_NM
        })

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

# ════════════════════════ REMOTE ASSIST (owner-initiated) ════════════════════
# Phase 1 — the node side. A poller that, ONLY while the owner has opened a
# session, fetches whitelisted READ-ONLY diagnostics from pilnk.io and posts the
# results back. Owner-initiated, capability-not-shell, time-boxed, audited.
# See pilnk-tasks/remote-assist.md.
#
# SAFETY: every capability here is read-only and fixed in code. Params can only
# ever pick a capped N or an enum — never a path or command string. Nothing here
# writes a file, restarts a service, or runs an arbitrary command.


ASSIST_BASE = 'https://pilnk.io/api/assist.php'
_assist_state = {
    'session_id': None,
    'human_code': None,
    'expires_at': 0,        # epoch seconds
    'active': False,
}
_assist_lock = threading.Lock()
ASSIST_RESULT_CAP = 200000   # ~200KB, matches server cap


def _assist_run(cmd, timeout=6):
    """Run a FIXED read-only command (list form, no shell) and return capped
    stdout. Never raises — returns an error string instead."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or '') + (('\n[stderr] ' + r.stderr) if r.stderr else '')
        return out[:ASSIST_RESULT_CAP]
    except Exception as e:
        return f'[error running {cmd[0] if cmd else "?"}]: {e}'


def _assist_read_file(path, cap=ASSIST_RESULT_CAP):
    try:
        with open(path, 'r', errors='replace') as f:
            return f.read()[:cap]
    except Exception as e:
        return f'[cannot read {path}]: {e}'


# ── the capability functions (all read-only) ─────────────────────────────────

def _cap_version(params):
    out = {'version_file': _get_local_version()}
    out['git_head'] = _assist_run(['git', '-C', PILNK_DIR, 'rev-parse', 'HEAD']).strip()
    out['git_describe'] = _assist_run(['git', '-C', PILNK_DIR, 'log', '-1', '--oneline']).strip()
    return out


def _cap_config_read(params):
    # config.json holds no secrets (verified). Return it as-is.
    return {'config': _assist_read_file(CONFIG_PATH)}


def _cap_pairing_status(params):
    try:
        with pairing_state_lock:
            return dict(pairing_state)
    except Exception as e:
        return {'error': str(e)}


def _cap_ota_status(params):
    try:
        return ota_get_status()
    except Exception as e:
        return {'error': str(e)}


def _cap_net_status(params):
    out = {}
    out['last_ping_ok_ago_sec'] = (time.time() - PING_LAST_OK_TS) if 'PING_LAST_OK_TS' in globals() and PING_LAST_OK_TS else None
    out['reachable'] = _assist_run(
        ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
         'https://pilnk.io/api/version.php']).strip()
    return out


def _cap_git_status(params):
    return {
        'status': _assist_run(['git', '-C', PILNK_DIR, 'status', '--porcelain', '-b']),
        'last_commit': _assist_run(['git', '-C', PILNK_DIR, 'log', '-1', '--oneline']).strip(),
    }


def _cap_aircraft_path(params):
    path = DUMP1090_AIRCRAFT_JSON
    out = {'configured_path': path, 'exists': os.path.exists(path)}
    try:
        if out['exists']:
            st = os.stat(path)
            out['size_bytes'] = st.st_size
            out['age_sec'] = round(time.time() - st.st_mtime, 1)
    except Exception as e:
        out['stat_error'] = str(e)
    return out


def _cap_aircraft_sample(params):
    # The actual field shape this node's decoder produces — first 3 records.
    raw = read_aircraft_json()
    if raw is None:
        return {'error': 'read_aircraft_json returned None', 'path': DUMP1090_AIRCRAFT_JSON}
    try:
        data = json.loads(raw)
        acs = data.get('aircraft', [])
        return {
            'total_in_file': len(acs),
            'sample': acs[:3],
            'top_keys': list(data.keys()),
        }
    except Exception as e:
        return {'error': f'parse failed: {e}', 'raw_len': len(raw)}


def _cap_ping_vs_flights(params):
    # THE Thor diagnostic: total in file vs how many survive the ping's
    # position filter (if a.get('lat')). Shows dashboard-vs-ping divergence.
    raw = read_aircraft_json()
    if raw is None:
        return {'error': 'read_aircraft_json returned None'}
    try:
        data = json.loads(raw)
        acs = data.get('aircraft', [])
        with_pos = [a for a in acs if a.get('lat')]
        return {
            'total_in_file': len(acs),
            'with_position': len(with_pos),
            'without_position': len(acs) - len(with_pos),
            'note': 'ping sends with_position; dashboard shows total_in_file',
        }
    except Exception as e:
        return {'error': f'parse failed: {e}'}


def _cap_decoder_status(params):
    out = {}
    for unit in ('readsb', 'dump1090-fa'):
        r = _assist_run(['systemctl', 'show', unit, '-p', 'LoadState',
                         '-p', 'ActiveState', '-p', 'SubState', '-p', 'ActiveEnterTimestamp'])
        out[unit] = r.strip()
    return out


def _cap_decoder_log_tail(params):
    n = _assist_cap_lines(params)
    unit = 'readsb'
    if params and params.get('unit') in ('readsb', 'dump1090-fa'):
        unit = params['unit']
    return {'unit': unit, 'log': _assist_run(
        ['journalctl', '-u', unit, '-n', str(n), '--no-pager'], timeout=8)}


def _cap_bds_status(params):
    # Tests the Mode S enrichment thread health.
    out = {}
    try:
        out['enrichment_cache_size'] = len(enrichment_cache)
    except Exception as e:
        out['enrichment_cache_size'] = f'err: {e}'
    out['port_30002'] = _assist_run(
        ['bash', '-c', 'ss -tn 2>/dev/null | grep :30002 | head -3 || echo "no 30002 sockets"'])
    return out


def _cap_log_tail(params):
    n = _assist_cap_lines(params)
    return {'log': _assist_run(['journalctl', '-u', 'pilnk', '-n', str(n), '--no-pager'], timeout=8)}


def _cap_disk(params):
    return {'df': _assist_run(['df', '-h'])}


def _cap_mem(params):
    return {'free': _assist_run(['free', '-m'])}


def _cap_uptime(params):
    return {'uptime': _assist_run(['uptime']).strip()}


def _cap_usb(params):
    return {'lsusb': _assist_run(['lsusb'])}


def _cap_temp_throttle(params):
    out = {}
    out['temp'] = _assist_run(['vcgencmd', 'measure_temp']).strip()
    out['throttled'] = _assist_run(['vcgencmd', 'get_throttled']).strip()
    return out


def _cap_time_sync(params):
    return {'timedatectl': _assist_run(['timedatectl'])}


def _cap_dmesg_usb(params):
    # dmesg needs no root for USB lines on most Pis; fall back gracefully.
    return {'dmesg_usb': _assist_run(
        ['bash', '-c', 'dmesg 2>/dev/null | grep -i usb | tail -40 || echo "dmesg not readable without root"'])}


def _cap_blacklist(params):
    return {'blacklist': _assist_read_file('/etc/modprobe.d/blacklist-rtlsdr.conf')}


def _cap_loaded_dvb(params):
    return {'lsmod_dvb': _assist_run(
        ['bash', '-c', "lsmod | grep -E 'dvb|rtl28|rtl2832|rtl2830' || echo 'no dvb/rtl modules loaded'"])}


def _cap_network_info(params):
    out = {}
    out['ip'] = _assist_run(['bash', '-c', "ip -brief addr 2>/dev/null || ip addr"])
    out['pilnk_reachable_http'] = _assist_run(
        ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
         'https://pilnk.io/api/version.php']).strip()
    return out


def _assist_cap_lines(params, default=100, hard=500):
    try:
        n = int((params or {}).get('lines', default))
    except (ValueError, TypeError):
        n = default
    return max(1, min(hard, n))


# The whitelist dict — MUST match api/assist.php ASSIST_CAPABILITIES.
ASSIST_CAPABILITIES = {
    'version':          _cap_version,
    'config_read':      _cap_config_read,
    'pairing_status':   _cap_pairing_status,
    'ota_status':       _cap_ota_status,
    'net_status':       _cap_net_status,
    'git_status':       _cap_git_status,
    'aircraft_path':    _cap_aircraft_path,
    'aircraft_sample':  _cap_aircraft_sample,
    'ping_vs_flights':  _cap_ping_vs_flights,
    'decoder_status':   _cap_decoder_status,
    'decoder_log_tail': _cap_decoder_log_tail,
    'bds_status':       _cap_bds_status,
    'log_tail':         _cap_log_tail,
    'disk':             _cap_disk,
    'mem':              _cap_mem,
    'uptime':           _cap_uptime,
    'usb':              _cap_usb,
    'temp_throttle':    _cap_temp_throttle,
    'time_sync':        _cap_time_sync,
    'dmesg_usb':        _cap_dmesg_usb,
    'blacklist':        _cap_blacklist,
    'loaded_dvb':       _cap_loaded_dvb,
    'network_info':     _cap_network_info,
}


def _assist_post(action, payload):
    payload['action'] = action
    payload['verify_code'] = NODE_VERIFY_CODE
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        ASSIST_BASE, data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'})
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode() or '{}')


def assist_open_session():
    """Called by the OWNER (via the local dashboard button) to open a session."""
    try:
        r = _assist_post('open_session', {})
        if r.get('session_id'):
            with _assist_lock:
                _assist_state['session_id'] = r['session_id']
                _assist_state['human_code'] = r.get('human_code')
                _assist_state['active'] = True
                # expires tracked loosely; server is authoritative
                mins = r.get('expires_minutes', 30)
                _assist_state['expires_at'] = time.time() + mins * 60
        return r
    except Exception as e:
        return {'error': str(e)}


def assist_close_session(reason='owner'):
    with _assist_lock:
        sid = _assist_state['session_id']
        _assist_state['active'] = False
    if sid:
        try:
            _assist_post('close_session', {'session_id': sid, 'reason': reason})
        except Exception:
            pass
    with _assist_lock:
        _assist_state['session_id'] = None
        _assist_state['human_code'] = None


def assist_poller():
    """Background thread. Idle until the owner opens a session, then polls
    pilnk.io for whitelisted requests, runs them locally, posts results back."""
    while True:
        with _assist_lock:
            active = _assist_state['active']
            sid = _assist_state['session_id']
            exp = _assist_state['expires_at']
        if not active or not sid:
            time.sleep(5)
            continue
        if time.time() > exp:
            assist_close_session('expired')
            continue
        try:
            r = _assist_post('poll', {'session_id': sid})
            if r.get('session_status') and r['session_status'] != 'open':
                with _assist_lock:
                    _assist_state['active'] = False
                continue
            req = r.get('request')
            if req:
                cap = req.get('capability')
                fn = ASSIST_CAPABILITIES.get(cap)
                if fn:
                    try:
                        result = fn(req.get('params') or {})
                        is_err = isinstance(result, dict) and 'error' in result
                    except Exception as e:
                        result = {'error': f'capability {cap} failed: {e}'}
                        is_err = True
                else:
                    result = {'error': f'unknown capability: {cap}'}
                    is_err = True
                _assist_post('result', {
                    'request_id': req['request_id'],
                    'result': result,
                    'is_error': 1 if is_err else 0,
                })
        except Exception as e:
            print(f'[PILNK] Assist poll error: {e}')
        # Faster cadence while a session is open (snappy for Claude-driven debug)
        time.sleep(5)


# ── Owner-facing controls (local dashboard) ──────────────────────────────────
@app.route('/api/assist/request', methods=['POST'])
def api_assist_open():
    """The OWNER presses 'Request Assist' on their local dashboard -> this opens
    a session at pilnk.io. This is the ONLY way a session begins (owner-initiated)."""
    r = assist_open_session()
    return jsonify(r)


@app.route('/api/assist/status', methods=['GET'])
def api_assist_status():
    with _assist_lock:
        return jsonify({
            'active': _assist_state['active'],
            'human_code': _assist_state['human_code'],
            'session_id': _assist_state['session_id'],
            'expires_in_sec': max(0, int(_assist_state['expires_at'] - time.time())) if _assist_state['active'] else 0,
        })


@app.route('/api/assist/end', methods=['POST'])
def api_assist_end():
    assist_close_session('owner')
    return jsonify({'ok': True})


# Launch the assist poller (idle until the owner opens a session).
if NODE_VERIFY_CODE != 'YOUR_VERIFY_CODE_HERE':
    _assist_thread = threading.Thread(target=assist_poller, daemon=True)
    _assist_thread.start()
    print('[PILNK] Remote Assist poller ready (idle until owner opens a session)')


if __name__ == '__main__':
    # `allow_unsafe_werkzeug` is a WERKZEUG-only argument. When eventlet is
    # installed, flask-socketio takes the eventlet path instead and forwards
    # this kwarg to eventlet.wsgi.server(), which rejects it:
    #     TypeError: server() got an unexpected keyword argument
    # That killed every start on a PiAware image (eventlet present) while
    # being invisible on our own nodes (eventlet absent). Try the Werkzeug
    # form, fall back to the portable one — works on either backend.
    print(f'[PILNK] Dashboard starting on port {DASHBOARD_PORT}')
    try:
        socketio.run(app, host='0.0.0.0', port=DASHBOARD_PORT, debug=False,
                     allow_unsafe_werkzeug=True)
    except TypeError:
        socketio.run(app, host='0.0.0.0', port=DASHBOARD_PORT, debug=False)
