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


@app.after_request
def _dashboard_html_no_cache(resp):
    """Make the dashboard HTML revalidate, so an OTA actually reaches the screen.

    20 Sep 2026: AJ saw Carto "API key required" tiles on a node dashboard. The
    Carto swap had been done on 31 Aug and the template was clean — his BROWSER
    was holding a copy from before the fix. A hard refresh cured it.

    Flask's render_template response carries NO Cache-Control, so browsers fall
    back to HEURISTIC caching and may reuse a page for a long time. A node can
    therefore OTA to a new version while its operator keeps seeing the old UI,
    with every layer reporting success. That is the same failure as the audio
    engine installing a new binary without restarting the process (#110), and
    as sdr_audio reporting the installed file rather than the running one
    (#102): the thing updated, the update did not take effect.

    It is invisible by construction — nobody reports "I am looking at last
    month's UI", because it looks like the UI. This was only caught because
    Carto drew its refusal onto the map.

    pilnk.io already does this in .htaccess (no-cache, must-revalidate on
    .js/.css/.html, from the cache-bust work). The node never got it.

    SCOPE, deliberately narrow:
      - text/html ONLY. JSON, images, and the tile/photo proxies are untouched.
      - never overrides a Cache-Control already set, so the four endpoints that
        choose their own caching (RainViewer 300s, photo cache 1y) keep it.
      - fail-soft: a header helper must never be able to break a response.
    """
    try:
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype == 'text/html' and not resp.headers.get('Cache-Control'):
            resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    except Exception:
        pass
    return resp

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
def _cfg_port(key, default):
    """Read a port from config.json, falling back to `default` on anything odd.

    Shared by every port PiLNK takes from config so the validation cannot drift
    between them. A second near-identical parser is how two ports end up with
    two different ideas of what counts as valid, and nobody finds out until a
    node refuses to start.
    """
    try:
        p = int(_config.get(key) or default)
        return p if 1 <= p <= 65535 else default
    except (TypeError, ValueError):
        return default

DASHBOARD_PORT = _cfg_port('dashboard_port', 5000)


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


# ── Compute capability probe (v1.4.16) ─────────────────────────────────────
# Speech-to-text is the first feature PiLNK has that a node can be genuinely
# too slow to run. EpsomPi is a Pi 5 and decodes at 94% of real time — not
# comfortably capable, JUST capable. Anything slower does not degrade, it falls
# behind and never recovers, because the backlog only grows.
#
# So before offering anyone a ~1.5GB model download we want to be able to say
# "THIS node decodes at 0.6x real time", not "a Pi 4 is probably slow". That
# means measuring, and measuring means a number from every node.
#
# Deliberately NOT a verdict. This reports a raw score only. Thresholds get set
# once there is fleet data to calibrate against — EpsomPi's score is the known
# anchor, because its real whisper throughput has been measured. Inventing
# cutoffs now, from one machine, would just be a guess wearing a number's
# clothes.
#
# Why a matmul: it is the inner loop of transformer inference, and numpy is
# already present fleet-wide (see the env fingerprint above). It also picks up
# the thing that actually matters and that a model name cannot tell you —
# whether this box has a decent BLAS and usable SIMD. An x86 machine with
# AVX2 will pull far ahead of its clock speed here, which is exactly the
# signal we want.
#
# Costs ~0.5s, once, on a background thread so startup is never blocked.
# Wrapped so that a node without numpy, or with a broken BLAS, reports None
# and carries on — a capability probe must never be the thing that breaks a
# node that was working fine.
STT_BENCH = {'score': None, 'note': 'not yet run'}

def _run_capability_probe():
    global STT_BENCH
    try:
        import numpy as _np
        n, reps = 192, 12
        a = _np.random.rand(n, n).astype(_np.float32)
        b = _np.random.rand(n, n).astype(_np.float32)
        a @ b                                   # warm BLAS, don't time the first
        t0 = time.time()
        for _ in range(reps):
            a = a @ b
        dt = time.time() - t0
        if dt <= 0:
            STT_BENCH = {'score': None, 'note': 'timer resolution too coarse'}
            return
        gflops = (2.0 * (n ** 3) * reps) / dt / 1e9
        STT_BENCH = {'score': round(gflops, 2), 'note': 'gflops_f32_matmul'}
        print('[PILNK] capability probe: %.2f GFLOPS (f32 matmul)' % gflops)
    except ImportError:
        STT_BENCH = {'score': None, 'note': 'numpy absent'}
    except Exception as e:
        STT_BENCH = {'score': None, 'note': 'probe failed: %s' % e}

threading.Thread(target=_run_capability_probe, daemon=True).start()

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
#
# NOTE: build-overlay-nz.py REBUILDS this file from scratch (atomic replace) and
# keeps only NZ 'C8' hexes, so a hand-added row here is silently deleted on its
# next cron run. It also writes a blank TYPE column, so it cannot correct a type
# even for an NZ aircraft. Hand corrections go in the MANUAL file below.
AIRCRAFT_OVERLAY_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aircraft-overlay.csv.gz')

# Hand-maintained corrections, shipped IN THE REPO so every node gets them by
# OTA. Plain CSV rather than gzip on purpose: it is meant to be opened, diffed
# and reviewed. No generator ever writes it. Loaded LAST, so it wins over both
# the global DB and the generated national overlay.
#
# For when upstream tar1090-db has a hex against the wrong airframe. First case
# (13 Sep 2026): 897005 was listed as H4-OTA / DHC6, but it is Solomon Airlines'
# A320 H4-SAL — so the dashboard labelled an A320 a Twin Otter, while the photo
# (fetched by hex from Planespotters, bypassing this DB) correctly showed the A320.
AIRCRAFT_OVERLAY_MANUAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aircraft-overlay-manual.csv')

# --- ATC STT transcript (fed by the node-local atc_service daemon) -----------
# Resolved from the SERVICE USER's home, never a hardcoded username. This read
# '/home/aj/...' — correct on the development node and wrong on every other one
# in the fleet, where the service runs as 'pi'. It fails safe (see the route
# below), so nothing broke — but when STT ships fleet-wide it would have been
# silently dead everywhere except here, which is exactly how the audio engine
# managed to look shipped for six weeks without ever running on another node.
#
# DEFINED HERE rather than beside the route that uses it (moved 14 Sep 2026).
# ping_server() reads this path to report STT status, and the ping thread starts
# ~670 lines before that route — so on every restart the first ping raced the
# definition and died with "name 'ATC_TRANSCRIPT_PATH' is not defined". It healed
# on the next ping, which is exactly why it went unnoticed: a once-per-restart
# failure that repairs itself reads as noise in the log rather than a bug.
ATC_TRANSCRIPT_PATH = os.path.join(os.path.expanduser('~'), 'atc-stt', 'atc_transcript.json')
ATC_TRANSCRIPT_STALE_SECS = 120   # no update in this long => treat the daemon as down

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
                # Column 4 is tar1090-db's POSITIONAL flag string, and char 0 is
                # MILITARY (tar1090-db toJson.py sets it from ADS-B Exchange's
                # 'mil': e['f'] = '1' + f[1:]). Until v1.5.22 this column was
                # thrown away, so the node could only call an aircraft military
                # if its hex sat in a reserved military block — and air arms
                # like the RNZAF/RAAF have none (they share their nation's civil
                # block). Kept only when set: ~20k airframes pay for one key.
                mil = len(row) > 3 and (row[3] or '')[:1] == '1'
                # Only store if we have at least a type or a registration
                if typ or reg:
                    e = {'t': typ, 'r': reg}
                    if mil:
                        e['m'] = 1
                    new_db[hex_code] = e
        AIRCRAFT_DB = new_db
        logging.info(f'Aircraft DB loaded: {len(AIRCRAFT_DB)} entries from {path}')
        return len(AIRCRAFT_DB)
    except Exception as e:
        logging.error(f'Failed to load aircraft DB: {e}')
        return 0

def _merge_overlay_file(path, label):
    """Merge one ';'-delimited hex;reg;type overlay onto AIRCRAFT_DB.

    Shared by both overlays deliberately: two copies of this parser would be
    free to drift apart, and a subtle difference between them would show up as
    one aircraft being right on one node and wrong on another.

    Handles .gz and plain text by extension. Overlay values win; a blank field
    preserves whatever the primary DB already had. Lines beginning '#' are
    skipped, because the manual file is meant to be read by people. Silent
    no-op when the file is absent.
    """
    if not os.path.exists(path):
        return 0
    try:
        merged = 0
        opener = gzip.open if path.endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8', errors='replace') as f:
            for row in csv.reader(f, delimiter=';'):
                if not row or (row[0] or '').lstrip().startswith('#'):
                    continue
                if len(row) < 2:
                    continue
                hex_code = (row[0] or '').strip().upper()
                # Validate the DIGITS, not just the length. A hand-typed 'O'
                # for '0' is still six characters: it would be accepted here,
                # stored, and then never match a real aircraft — a correction
                # that silently does nothing. Reject it and say so.
                if len(hex_code) != 6 or any(c not in '0123456789ABCDEF' for c in hex_code):
                    logging.warning(
                        f'[overlay] {label}: skipping malformed hex {hex_code!r} in {path}')
                    continue
                reg = (row[1] or '').strip()
                typ = (row[2].strip() if len(row) > 2 else '')
                if not (reg or typ):
                    continue
                existing = AIRCRAFT_DB.get(hex_code, {})
                merged_entry = {
                    't': typ or existing.get('t', ''),
                    'r': reg or existing.get('r', ''),
                }
                # Carry the primary DB's military flag across. The overlays only
                # correct reg/type — rebuilding the entry without this silently
                # un-militaried every NZ 'C8' hex the national overlay touches,
                # i.e. exactly the RNZAF airframes the flag exists for.
                if existing.get('m'):
                    merged_entry['m'] = 1
                AIRCRAFT_DB[hex_code] = merged_entry
                merged += 1
        logging.info(f'Aircraft overlay merged: {merged} entries from {path} ({label})')
        return merged
    except Exception as e:
        logging.error(f'Failed to load {label} aircraft overlay: {e}')
        return 0


def load_aircraft_overlay():
    """Merge the national-register overlay, then hand corrections on top.

    The primary source (wiedehopf/tar1090-db) has coverage gaps for some
    national fleets — light aircraft, microlights, gliders, amateur-built —
    which surface as unidentified "ghosts". A node operator can drop an
    authoritative overlay built from their CAA/FAA register at
    aircraft-overlay.csv.gz and it is merged here.

    aircraft-overlay-manual.csv is merged AFTER it and therefore wins, because
    it is the one a human edited on purpose. It ships in the repo, so a
    correction made once reaches the whole fleet on the next OTA — unlike the
    generated overlay, which is per-node and rebuilt from scratch.

    Call AFTER load_aircraft_db (which rebuilds AIRCRAFT_DB from scratch), so
    both overlays survive refreshes.
    """
    return (_merge_overlay_file(AIRCRAFT_OVERLAY_LOCAL, 'national register')
            + _merge_overlay_file(AIRCRAFT_OVERLAY_MANUAL, 'manual corrections'))

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

# ── History-tab day store (24 Sep 2026) ─────────────────────────────────────
# AJ: "My hourly graph looks very empty. During the day there is always
# something flying in Auckland." Two causes, both in how /api/history used to
# read TRAIL_HISTORY:
#   1. It lives only in memory — every service restart (OTA, a fix, a reboot)
#      wiped the day. On 24 Sep three restarts left one bar at 17:00.
#   2. Each aircraft's trail is capped at 500 points (~83 min at 10 s), so a
#      regional ATR flying rotations all day silently fell out of the morning
#      bars even with no restart at all.
# This keeps what the History tab actually needs — which aircraft were seen in
# each hour, and a one-line summary per aircraft — small (~100 KB for a busy
# day), uncapped per aircraft, saved every HIST_SAVE_EVERY seconds and
# reloaded on start. A restart now costs at most those few minutes.
# TRAIL_HISTORY is untouched: it still feeds the map trails.
# NOT PILNK_DIR: that is defined ~900 lines further down, and this runs at import.
HIST_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'history_day.json')
HIST_SAVE_EVERY = 300          # seconds between disk writes (SD-card friendly)
HIST_NEW_VISIT  = 1800         # unseen this long, then back = a new visit (first_seen resets)
HIST_HOURS = {}                # epoch hour (int) -> set(hex)
HIST_AC    = {}                # hex -> {first, last, max_alt, cs, n, lat, lon}
_HIST_SAVED = {'t': time.time()}

def _hist_load():
    try:
        with open(HIST_FILE, 'r') as f:
            d = json.load(f)
        cutoff = time.time() - MAX_TRAIL_AGE
        for k, v in (d.get('hours') or {}).items():
            h = int(k)
            if (h + 1) * 3600 > cutoff and isinstance(v, list):
                HIST_HOURS[h] = set(v)
        for hx, a in (d.get('ac') or {}).items():
            if isinstance(a, dict) and (a.get('last') or 0) >= cutoff:
                HIST_AC[hx] = a
        logging.info('[history] restored %d aircraft over %d hours from %s', len(HIST_AC), len(HIST_HOURS), HIST_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning('[history] could not restore %s (starting empty): %s', HIST_FILE, e)

def _hist_save():
    """Caller holds TRAIL_LOCK. Atomic, so a power cut never leaves half a file."""
    tmp = HIST_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'hours': {str(h): sorted(s) for h, s in HIST_HOURS.items()}, 'ac': HIST_AC},
                  f, separators=(',', ':'))
    os.replace(tmp, HIST_FILE)

def _hist_note(a, now):
    """Record one aircraft sighting. Caller holds TRAIL_LOCK."""
    hx = a['hex']
    HIST_HOURS.setdefault(int(now // 3600), set()).add(hx)
    alt = a.get('alt_baro', 0)
    alt = alt if isinstance(alt, (int, float)) else 0
    fl = (a.get('flight') or '').strip()
    e = HIST_AC.get(hx)
    if e is None or now - (e.get('last') or 0) > HIST_NEW_VISIT:
        # A fresh visit: the row describes THIS visit (start, length, top altitude).
        # The earlier visit still counts in its own hours of the graph.
        HIST_AC[hx] = e = {'first': now, 'last': now, 'max_alt': alt, 'cs': fl, 'n': 0,
                           'lat': a.get('lat'), 'lon': a.get('lon')}
    e['last'] = now
    e['n'] = e.get('n', 0) + 1
    e['lat'], e['lon'] = a.get('lat'), a.get('lon')
    if alt > (e.get('max_alt') or 0):
        e['max_alt'] = alt
    if fl:
        e['cs'] = fl

_hist_load()

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
                            _hist_note(a, now)
                    # Clean old entries
                    cutoff = now - MAX_TRAIL_AGE
                    for hex in list(TRAIL_HISTORY.keys()):
                        while TRAIL_HISTORY[hex] and TRAIL_HISTORY[hex][0]['t'] < cutoff:
                            TRAIL_HISTORY[hex].popleft()
                        if not TRAIL_HISTORY[hex]:
                            del TRAIL_HISTORY[hex]
                    for h in [h for h in HIST_HOURS if (h + 1) * 3600 <= cutoff]:
                        del HIST_HOURS[h]
                    for hx in [hx for hx, e in HIST_AC.items() if (e.get('last') or 0) < cutoff]:
                        del HIST_AC[hx]
                    if now - _HIST_SAVED['t'] >= HIST_SAVE_EVERY:
                        _HIST_SAVED['t'] = now
                        try:
                            _hist_save()
                        except Exception as e:
                            logging.warning('[history] save failed: %s', e)
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
                    records = _sane_records(json.load(f))
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
# Loop heartbeat — advances at the TOP of every ping-loop iteration, regardless of
# whether the ping itself SUCCEEDS. This is the liveness signal the node watchdog
# reads via /api/health. A stale PING_LOOP_TS means the ping THREAD has died and a
# restart will fix it; PING_LAST_OK_TS going stale can instead just mean pilnk.io
# is unreachable (a server outage — restarting the node would not help, and a whole
# fleet restarting at once would be worse). Watch the loop, not the ping.
PING_LOOP_TS = 0.0

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
    global PING_LAST_OK_TS, PING_LOOP_TS, _ping_loop_running
    with _ping_loop_lock:
        if _ping_loop_running:
            print('[PILNK] ping_server already running — duplicate launch ignored')
            return
        _ping_loop_running = True
    while True:
        PING_LOOP_TS = time.time()   # loop heartbeat — see PING_LOOP_TS decl above
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
                    'sdr_audio': _sdr_audio_feature(),
                    'audio_build': _audio_build_feature(),
                    'audio_build_err': _audio_build_err(),
                    'atc_stt':   'ready' if os.path.exists(ATC_TRANSCRIPT_PATH) else 'absent',
                    # Raw compute score, no verdict attached — see the capability
                    # probe above. Answers "which nodes COULD run STT" with a
                    # measurement instead of a guess from the model name.
                    'stt_bench': STT_BENCH.get('score'),
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

AUDIO_BUILD_STATE_FILE = os.path.join(PILNK_DIR, 'audio_build_state.json')

def _audio_build_state():
    """The raw audio-build state file, or None if absent/unreadable."""
    try:
        with open(AUDIO_BUILD_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _audio_build_err():
    """WHY the last audio build failed. Empty string when it did not.

    Deliberately SEPARATE from audio_build, and deliberately shaped like the
    existing ota_last_result / ota_fail pair: the state stays low-cardinality
    so fleet_query can group it, while the reason is free text that only
    appears when something actually broke.

    Added 21 Sep 2026. The step number alone was not enough — two nodes
    reported failed:2/8 on every release and we still could not say why
    without opening the node, which is the exact problem this reporting was
    added to end. The installer knows apt's own error text; this carries it.

    An EMPTY detail on a failed build is itself informative: it means the
    script died without reaching die(), i.e. a bare `set -e` abort.
    """
    s = _audio_build_state()
    if not s or str(s.get('result') or '') != 'failed':
        return ''
    return str(s.get('detail') or '')[:200]

def _audio_build_feature():
    """Whether the ATC audio engine ever BUILT, and if not, WHERE it stopped.

    Companion to _sdr_audio_feature(): that one says whether the engine RUNS,
    this says whether it was ever produced at all. They fail independently.

    Three fleet-wide killers of this build each hid for weeks because a failed
    build and a build that never ran leave identical evidence — no binary and
    no word. pilnkradio-install.sh always knew which of its eight steps it died
    on; it printed it to a journal nobody reads.

      never_run     no state file AND no binary — never attempted here
      ok_prereport  no state file but the binary exists — built before this
                    reporting shipped. Healthy; it will become 'ok' at the
                    next engine rebuild.
      building:N/8  a build is running right now
      failed:N/8    died on that step — THE ACTIONABLE ONE
      ok            last attempt completed

    Step NUMBER only, never the prose: 'failed:6/8' groups across the fleet,
    while 'failed:6/8 radio dongle' would make every node its own group.
    """
    s = _audio_build_state()
    if s is None:
        # No readable state file. Separate a node that never built from one
        # that built BEFORE this reporting existed — otherwise every healthy
        # node reports never_run until its next engine rebuild, which is
        # exactly the confidently-wrong status this change exists to remove.
        return ('ok_prereport' if os.path.exists('/usr/local/bin/pilnkradio')
                else 'never_run')
    result = str(s.get('result') or 'unknown')
    if result == 'ok':
        return 'ok'
    step = str(s.get('step') or '?').split()[0]
    return ('building:' if result == 'running' else 'failed:') + step

def _sdr_audio_feature():
    """What is ACTUALLY answering on the radio port — not what sits on disk.

    This was `'ready' if os.path.exists('/usr/local/bin/pilnkradio')`. On
    LINKLABS that reported 'ready' for 28 days while sdrpp held :5656 and
    pilnkradio failed to start 393,391 consecutive times. A file-existence
    check cannot fail, so it never told anyone anything.

    LOW-CARDINALITY states, so fleet_query's grouping stays useful:
      engine_absent    no binary installed
      not_listening    binary present, nothing answering on :5656
      foreign:<name>   something ELSE holds the port (the sdrpp case)
      no_device        our engine is up but has no tuner behind it
      running:<ver>    our engine, answering, and which build it is

    Fail-soft and fast. This runs on every ping and a radio problem must never
    delay or break an ADS-B ping: the common failure (crash-loop) is a refused
    connection, which returns instantly.
    """
    if not os.path.exists('/usr/local/bin/pilnkradio'):
        return 'engine_absent'
    try:
        s = requests.get('http://127.0.0.1:5656/sdr/status', timeout=1.5).json()
    except Exception:
        # Refused, timed out, or not JSON. Operationally identical: the
        # installed engine is not serving.
        return 'not_listening'
    eng = s.get('engine')
    if eng is None:
        # Builds before 2.0.0-m1 do not name themselves — and neither does
        # sdrpp. So a healthy old engine and a squatter are genuinely
        # indistinguishable here. Say exactly that rather than guess either way.
        # This state should drain to running:<ver> as the fleet rebuilds; a node
        # still reporting it afterwards is the one worth opening.
        return 'unidentified'
    if eng != 'pilnkradio':
        return 'foreign:' + str(eng)
    if not s.get('rfGainSteps'):
        # Bound and answering, but no tuner open — the honest middle state that
        # 'ready' used to swallow.
        return 'no_device'
    return 'running:' + str(s.get('version') or 'unknown')

def _ota_ping_features():
    """Three LOW-CARDINALITY strings for the ping's features dict — states,
    not timestamps, so fleet_query's grouping stays useful (a raw timestamp
    would make every node an 'outlier' every ping).
      ota:            auto | manual   (how updates are configured)
      ota_check:      ok | stale | starting | never   (is the checker ALIVE)
      ota_last_result:success | interrupted | started@v | exit_N | error:X | none
      ota_fail:       '' | cd_failed | fetch_failed | reset_failed |
                      version_unchanged | restart_blocked | service_dead |
                      restart_noop | exception | unknown
                      (WHY the last attempt failed — empty when it did not)

    ota_last_result says an attempt broke; ota_fail says where. update.sh has
    three separate exit-1 paths, so the exit code alone cannot tell them apart
    and the only way to find out used to be asking the node's owner to read his
    own log back to us. Added 14 Sep 2026.
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
        'ota_fail':        str(_load_ota_state().get('last_fail', ''))[:24],
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

    Default is TRUE as of 2026-09-12 (was FALSE since v0.1.11).

    install.sh has written `"auto_update": true` into config.json for a long
    time, so this fallback never decided anything for a normally-installed
    node — it only decided for configs written BEFORE the key existed. Those
    users were never asked; they were defaulted to manual by the age of their
    install and then quietly left behind. M0CRT sat 19 releases back that way
    while feeding perfectly for 110 days.

    Opting out stays easy and explicit: `"auto_update": false` in
    ~/pilnk/config.json. An explicit false always wins over this default.
    Required updates ignore the flag entirely and install regardless.
    """
    config_path = os.path.join(PILNK_DIR, 'config.json')
    try:
        with open(config_path, 'r') as f:
            return json.load(f).get('auto_update', True)
    except FileNotFoundError:
        # No config yet: a node with no stated preference is better off current.
        return True
    except Exception as e:
        # A config we cannot PARSE is a different thing from one that says
        # nothing. Unknown state is not consent to change the machine, and the
        # old bare `except: return False` swallowed the reason. Say it.
        logging.warning('[ota] config.json unreadable (%s) — auto-update OFF '
                        'until it parses', e)
        return False

# Fixed vocabulary for WHY an update failed. The ping's features dict has to
# stay LOW-CARDINALITY (see _ota_ping_features), so this maps update.sh's error
# text onto a handful of states instead of reporting the raw line — free text
# would make every failing node its own group and defeat fleet_query entirely.
#
# Ordered: the first match wins, so specific patterns sit above general ones.
_OTA_FAIL_PATTERNS = (
    ('cannot cd to',                     'cd_failed'),          # exit 1
    ('git fetch failed',                 'fetch_failed'),       # exit 1
    ('git reset --hard failed',          'reset_failed'),       # exit 1
    ('version unchanged',                'version_unchanged'),  # exit 3, abort
    ('restart blocked',                  'restart_blocked'),    # exit 4, no NOPASSWD sudo
    ('service not active after restart', 'service_dead'),       # exit 2
    ('mainpid is unchanged',             'restart_noop'),       # restart silently no-op'd
)


def _classify_ota_failure(output):
    """Name the failure in one short token, from update.sh's own output.

    update.sh log()s every error to stdout as well as to its log file, so the
    reason is already in hand at the moment we record 'exit_N' — it was simply
    printed and thrown away. That gap cost real time: update.sh has THREE
    separate exit-1 paths, so a node could report the same code for three
    unrelated faults, and the only way to tell them apart was to ask its owner
    to read his own log back to us. An exit code says that it broke; this says
    where.
    """
    low = (output or '').lower()
    for needle, token in _OTA_FAIL_PATTERNS:
        if needle in low:
            return token
    return 'unknown'


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
        _ok = result.returncode == 0
        _save_ota_state(
            last_result=('success' if _ok else 'exit_%d' % result.returncode),
            # WHY, not just THAT. Cleared on success so a node that recovers
            # stops advertising an old fault it no longer has.
            last_fail=('' if _ok else _classify_ota_failure(
                (result.stdout or '') + '\n' + (result.stderr or ''))),
            last_result_ts=time.time())
        return _ok
    except Exception as e:
        print(f'[PILNK-OTA] Update failed: {e}')
        ota_status['updating'] = False
        _save_ota_state(last_result='error:' + type(e).__name__,
                        last_fail='exception',
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

# ── Watchlist alert proxy (#123, node-side proxy — 23 Sep 2026) ─────────
# The pilnk.io watchlist (RNZAF/USAF/squawk/ghost/type/callsign) fires alerts
# on the SERVER. This surfaces the OWNER's recent hits on the node dashboard,
# the same way the Badges tab does: server-to-server with the node's
# verify_code, so the secret never reaches a browser, there is no CORS /
# SameSite involvement, and any LAN viewer sees it with no pilnk.io login.
# See api/node_alerts.php and pilnk-tasks/watchlist-on-node-dash.md.
#
# OPT-IN, DEFAULT OFF. Not a privacy wall — a node is a home LAN, not a public
# kiosk — but the owner's call to make, like Remote Assist. One line in
# config.json ("watchlist_notify": true) turns it on, read FRESH each call so
# it toggles without a restart.
_WATCH_ALERT_CACHE = {'ts': 0.0, 'data': None}
_WATCH_ALERT_TTL = 45   # seconds; watch hits run ~5/day so this is plenty

def _watchlist_notify_enabled():
    """Fresh-read config.json so the owner can toggle without restarting."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return bool(json.load(f).get('watchlist_notify', False))
    except Exception:
        return False

@app.route('/api/watchlist_alerts', methods=['GET'])
def api_watchlist_alerts():
    # Off by default: report disabled so the dashboard renders nothing at all.
    if not _watchlist_notify_enabled():
        return jsonify({'enabled': False, 'alerts': [], 'unseen': 0})
    if not NODE_VERIFY_CODE:
        return jsonify({'enabled': True, 'alerts': [], 'unseen': 0, 'error': 'unpaired'})

    now = time.time()
    if _WATCH_ALERT_CACHE['data'] is not None and (now - _WATCH_ALERT_CACHE['ts']) < _WATCH_ALERT_TTL:
        out = dict(_WATCH_ALERT_CACHE['data']); out['cached'] = True
        return jsonify(out)
    try:
        r = requests.post(
            'https://pilnk.io/api/node_alerts.php',
            json={'verify_code': NODE_VERIFY_CODE, 'limit': 50},
            headers={'User-Agent': 'PiLNK/1.0'}, timeout=10)
        data = r.json()
        if not isinstance(data, dict) or not data.get('ok'):
            raise ValueError('unexpected payload: ' + str(data)[:120])
        out = {
            'enabled':    True,
            'alerts':     data.get('alerts', []),
            'unseen':     int(data.get('unseen', 0) or 0),
            'fetched_at': int(now),
        }
        _WATCH_ALERT_CACHE['ts'] = now
        _WATCH_ALERT_CACHE['data'] = out
        return jsonify(out)
    except Exception as e:
        # Fail-soft, but NEVER silent — record the reason (standing lesson: no
        # bare except:pass). Serve the last good copy marked stale if we have one.
        logging.warning('[watchlist] alert fetch failed (serving cache if any): %s', e)
        if _WATCH_ALERT_CACHE['data'] is not None:
            out = dict(_WATCH_ALERT_CACHE['data']); out['stale'] = True
            return jsonify(out)
        return jsonify({'enabled': True, 'alerts': [], 'unseen': 0, 'error': type(e).__name__}), 503

# ── Military card proxies (24 Sep 2026) ─────────────────────────────────────
# The cinematic card's military section called pilnk.io straight from the
# browser with credentials:'include'. From a dashboard on the Pi's LAN address
# that is a cross-site request, and the pilnk.io session cookie is
# SameSite=Lax — the browser never sends it. So "📒 LOG THIS CATCH" answered
# "Not logged in" on every press (no manual catch has ever landed), and the
# card could never see what the owner had already caught.
#
# Same cure as the Badges tab and the watchlist panel: the node makes the call
# server-to-server with its verify_code, so the secret never reaches a browser
# and no login is involved. The catch is credited to the node's owner, from
# this node, in this node's country pool (mil-catch.php), matching the
# automatic catch path.
_MIL_CATCH_FIELDS = ('hex', 'icao_type', 'callsign', 'country')

@app.route('/api/mil_lookup', methods=['GET'])
def api_mil_lookup():
    hx = str(request.args.get('hex') or '').upper().strip()
    ty = str(request.args.get('type') or '').upper().strip()
    if not re.match(r'^[0-9A-F]{6}$', hx):
        return jsonify({'is_military': False, 'catalog': None, 'user_catch': None, 'country_serials_used': 0})
    try:
        # verify_code travels in the POST body, never the URL (URLs get logged).
        r = requests.post('https://pilnk.io/api/mil-lookup.php',
                          params={'hex': hx, 'type': ty[:8]},
                          json={'verify_code': NODE_VERIFY_CODE} if NODE_VERIFY_CODE else {},
                          headers={'User-Agent': 'PiLNK/1.0'}, timeout=8)
        return jsonify(r.json())
    except Exception as e:
        logging.warning('[mil] lookup proxy failed for %s: %s', hx, e)
        return jsonify({'error': 'pilnk.io unreachable'}), 503

@app.route('/api/mil_catch', methods=['POST'])
def api_mil_catch():
    if not NODE_VERIFY_CODE:
        return jsonify({'error': 'This node is not paired with a pilnk.io account yet, so catches have nowhere to go.'}), 400
    body = request.get_json(silent=True) or {}
    payload = {k: str(body.get(k) or '')[:20] for k in _MIL_CATCH_FIELDS}
    payload['verify_code'] = NODE_VERIFY_CODE
    try:
        r = requests.post('https://pilnk.io/api/mil-catch.php?action=log_catch', json=payload,
                          headers={'User-Agent': 'PiLNK/1.0'}, timeout=10)
        try:
            data = r.json()
        except ValueError:
            data = {'error': 'pilnk.io answered HTTP %d' % r.status_code}
        return jsonify(data), r.status_code
    except Exception as e:
        logging.warning('[mil] catch proxy failed: %s', e)
        return jsonify({'error': 'pilnk.io unreachable, try again in a moment'}), 503

# ── Flight capsules — Ship 1: track recorder (23 Sep 2026) ─────────────────
# Click a plane → ● REC on the cinematic card, or mark it ⟳ AUTO ("always
# record") → the node records that airframe every CAPSULE_INTERVAL seconds —
# position, altitude, speed, heading, vertical rate, squawk, callsign — to
# disk, until it has been out of coverage for CAPSULE_GAP seconds. Replayable
# on the dashboard (full track, playback, screenshot mode), exportable as KML.
# Built for AJ's use case: Auckland police helicopter orbits, recorded whether
# or not anyone is watching the dashboard at the time.
#
# Why its own recorder instead of TRAIL_HISTORY: that samples every 10 s (too
# coarse for a helicopter orbit), caps each aircraft at 500 points (~83 min —
# a loitering helicopter silently loses its oldest track), keeps no speed /
# heading / squawk, and is lost on every restart. Capsules are keyed by HEX,
# not callsign, so an aircraft that changes or drops its callsign stays ONE
# recording.
#
# On disk (folder is gitignored):
#   capsules/<id>.jsonl   one point per line, appended as it flies
#   capsules/<id>.json    meta, rewritten on start/finish
#   capsules/active.json  what is recording now — survives a restart
#   capsules/rules.json   auto-record rules [{hex|callsign, label, added, wake?}]
#   capsules/radio_wake.json  set while a capsule has the radio switched on (v1.5.26)
# ~220 KB per recorded hour; total bounded by CAPSULE_MAX_MB, oldest finished
# capsules pruned first.
CAPSULE_DIR      = os.path.join(PILNK_DIR, 'capsules')
CAPSULE_INTERVAL = 2           # seconds between samples
CAPSULE_GAP      = 600         # out of coverage this long -> capsule ends
CAPSULE_MAX_SEC  = 6 * 3600    # hard cap per capsule (an auto rule starts a fresh one)
CAPSULE_MAX_MB   = 500         # disk budget for all capsules
CAPSULE_LOCK     = threading.Lock()
CAPSULE_ACTIVE   = {}          # hex -> {id, started, last_seen, auto, points, flight}
CAPSULE_SUPPRESS = {}          # hex -> last_seen; manually stopped, so auto must not re-arm until it leaves
CAPSULE_RESUMED  = threading.Event()   # set once active.json is reloaded — the audio tap waits for it,
                                       # or it would see "nothing recording" after a restart and
                                       # switch off a radio a resumed capsule still needs
_CAPSULE_ID_RE   = re.compile(r'^[0-9A-F]{6}-\d{8}-\d{6}$')
_CAPSULE_HEX_RE  = re.compile(r'^[0-9A-F]{6}$')

def _cap_path(name):
    return os.path.join(CAPSULE_DIR, name)

def _cap_valid_id(cid):
    return bool(_CAPSULE_ID_RE.match(cid or ''))

def _cap_load_json(name, default):
    try:
        with open(_cap_path(name), 'r') as f:
            return json.load(f)
    except Exception:
        return default

def _cap_save_json(name, data):
    # Atomic write: a power cut mid-write can never leave half a file behind.
    os.makedirs(CAPSULE_DIR, exist_ok=True)
    tmp = _cap_path(name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, _cap_path(name))

def _cap_rules():
    r = _cap_load_json('rules.json', [])
    return r if isinstance(r, list) else []

def _cap_rule_match(rules, hx, flight):
    """The first rule that matches this aircraft, or None. Returns the RULE (not
    True) so the caller can read its options — `wake` since v1.5.26."""
    fl = (flight or '').upper()
    for r in rules:
        if (r.get('hex') or '').upper() == hx:
            return r
        cs = (r.get('callsign') or '').upper()
        if cs and fl.startswith(cs):
            return r
    return None

def _cap_read_points(cid):
    pts = []
    try:
        with open(_cap_path(cid + '.jsonl'), 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pts.append(json.loads(line))
                except ValueError:
                    continue   # a torn final line after a power cut — skip it, keep the rest
    except FileNotFoundError:
        pass
    return pts

def _cap_save_active():
    _cap_save_json('active.json', CAPSULE_ACTIVE)

def _cap_start(hx, auto=False, flight='', wake=False):
    """Begin a capsule. Caller holds CAPSULE_LOCK. `wake`: the rule that started it
    lets it switch the radio on (v1.5.26, opt-in per rule)."""
    now = time.time()
    cid = hx + '-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
    db = AIRCRAFT_DB.get(hx, {}) if isinstance(AIRCRAFT_DB, dict) else {}
    _cap_save_json(cid + '.json', {
        'id': cid, 'hex': hx, 'started': now, 'ended': None, 'status': 'recording',
        'auto': bool(auto), 'flight': flight, 'reg': db.get('r', ''), 'type': db.get('t', ''),
        'points': 0, 'wake': bool(wake),
    })
    CAPSULE_ACTIVE[hx] = {'id': cid, 'started': now, 'last_seen': now, 'auto': bool(auto),
                          'points': 0, 'flight': flight, 'wake': bool(wake)}
    _cap_save_active()
    logging.info('[capsule] started %s (%s)', cid, 'auto' if auto else 'manual')
    return CAPSULE_ACTIVE[hx]

def _cap_finish(hx, reason):
    """Close a capsule and summarise its track into the meta file. Caller holds CAPSULE_LOCK."""
    c = CAPSULE_ACTIVE.pop(hx, None)
    if not c:
        return None
    cid = c['id']
    pts = _cap_read_points(cid)
    if len(pts) < 2:
        # A blip, not a flight — don't clutter the library with it.
        for fn in [cid + '.json', cid + '.jsonl'] + _cap_audio_files(cid):
            try:
                os.remove(_cap_path(fn))
            except OSError:
                pass
        _cap_save_active()
        logging.info('[capsule] discarded %s (%d point(s), %s)', cid, len(pts), reason)
        return None
    meta = _cap_load_json(cid + '.json', {'id': cid, 'hex': hx})
    alts = [p['alt'] for p in pts if isinstance(p.get('alt'), (int, float))]
    lats = [p['lat'] for p in pts]
    lons = [p['lon'] for p in pts]
    fls = []
    for p in pts:
        f = p.get('fl') or ''
        if f and f not in fls:
            fls.append(f)
    try:
        size = os.path.getsize(_cap_path(cid + '.jsonl'))
    except OSError:
        size = 0
    meta.update({
        'status': 'done', 'ended': pts[-1]['t'], 'end_reason': reason,
        'points': len(pts), 'duration': int(round(pts[-1]['t'] - pts[0]['t'])),
        'max_alt': max(alts) if alts else None, 'min_alt': min(alts) if alts else None,
        'bbox': [min(lats), min(lons), max(lats), max(lons)],
        'callsigns': fls[:10], 'flight': fls[0] if fls else meta.get('flight', ''),
        'size': size, 'cat': c.get('cat') or meta.get('cat', ''),
    })
    _cap_save_json(cid + '.json', meta)
    _cap_save_active()
    _cap_prune()
    logging.info('[capsule] finished %s: %d points, %ds (%s)', cid, len(pts), meta['duration'], reason)
    return meta

def _cap_audio_files(cid):
    """This capsule's audio segments (<id>.a<N>.ogg), in recording order."""
    try:
        names = [fn for fn in os.listdir(CAPSULE_DIR)
                 if fn.startswith(cid + '.a') and fn.endswith('.ogg')]
    except FileNotFoundError:
        return []
    def _n(fn):
        try:
            return int(fn[len(cid) + 2:-4])
        except ValueError:
            return 0
    return sorted(names, key=_n)

def _cap_prune():
    """Keep all capsules inside CAPSULE_MAX_MB — oldest FINISHED capsules go first.
    Audio counts against the same budget (v1.5.23): ~9 MB per recorded hour of
    Opus against ~0.2 MB of track, so it is the part that actually fills a disk."""
    try:
        budget = CAPSULE_MAX_MB * 1024 * 1024
        total = sum(os.path.getsize(_cap_path(fn)) for fn in os.listdir(CAPSULE_DIR)
                    if fn.endswith('.jsonl') or fn.endswith('.ogg'))
        if total <= budget:
            return
        active_ids = {c['id'] for c in CAPSULE_ACTIVE.values()}
        done = [fn[:-5] for fn in os.listdir(CAPSULE_DIR)
                if fn.endswith('.json') and _cap_valid_id(fn[:-5]) and fn[:-5] not in active_ids]
        done.sort(key=lambda cid: cid.split('-', 1)[1])   # YYYYmmdd-HHMMSS = chronological
        for cid in done:
            if total <= budget:
                break
            files = [cid + '.jsonl'] + _cap_audio_files(cid)
            sz = sum(os.path.getsize(_cap_path(fn)) for fn in files if os.path.exists(_cap_path(fn)))
            for fn in [cid + '.json'] + files:
                try:
                    os.remove(_cap_path(fn))
                except OSError:
                    pass
            total -= sz
            logging.warning('[capsule] pruned %s to stay under %d MB', cid, CAPSULE_MAX_MB)
    except Exception as e:
        logging.warning('[capsule] prune failed: %s', e)

def capsule_recorder():
    # Resume after a restart: a capsule still marked as recording either carries
    # on (its aircraft was seen within the gap) or is closed out using the time
    # of its last recorded point — never silently abandoned.
    with CAPSULE_LOCK:
        saved = _cap_load_json('active.json', {})
        if isinstance(saved, dict):
            for hx, c in saved.items():
                if isinstance(c, dict) and _cap_valid_id(c.get('id', '')):
                    pts = _cap_read_points(c['id'])
                    c['last_seen'] = pts[-1]['t'] if pts else c.get('started', time.time())
                    c['points'] = len(pts)
                    CAPSULE_ACTIVE[hx] = c
            for hx in list(CAPSULE_ACTIVE):
                if time.time() - CAPSULE_ACTIVE[hx]['last_seen'] > CAPSULE_GAP:
                    _cap_finish(hx, 'left coverage (across restart)')
    CAPSULE_RESUMED.set()
    last_err = 0.0
    while True:
        try:
            raw = read_aircraft_json()
            ac = (json.loads(raw).get('aircraft') or []) if raw else []
            rules = _cap_rules()
            now = time.time()
            with CAPSULE_LOCK:
                seen_now = set()
                for a in ac:
                    hx = (a.get('hex') or '').upper().strip()
                    if not _CAPSULE_HEX_RE.match(hx):
                        continue                      # skips ~ non-ICAO TIS-B/MLAT ids
                    lat, lon = a.get('lat'), a.get('lon')
                    if lat is None or lon is None:
                        continue
                    if (a.get('seen_pos') or 0) > 5:
                        continue                      # stale position, not a fresh fix
                    flight = (a.get('flight') or '').strip()
                    if hx in CAPSULE_SUPPRESS:
                        CAPSULE_SUPPRESS[hx] = now    # still overhead after a manual stop
                    if hx not in CAPSULE_ACTIVE:
                        rule = _cap_rule_match(rules, hx, flight) if (rules and hx not in CAPSULE_SUPPRESS) else None
                        if rule:
                            _cap_start(hx, auto=True, flight=flight, wake=bool(rule.get('wake')))
                        else:
                            continue
                    c = CAPSULE_ACTIVE[hx]
                    alt = a.get('alt_baro')
                    pt = {'t': round(now, 1), 'lat': round(lat, 5), 'lon': round(lon, 5),
                          'alt': 0 if alt == 'ground' else alt,
                          'gs': a.get('gs'), 'trk': a.get('track'), 'vr': a.get('baro_rate'),
                          'sq': a.get('squawk'), 'fl': flight}
                    if alt == 'ground':
                        pt['gnd'] = 1
                    with open(_cap_path(c['id'] + '.jsonl'), 'a') as f:
                        f.write(json.dumps(pt, separators=(',', ':')) + '\n')
                    c['last_seen'] = now
                    c['points'] += 1
                    if flight:
                        c['flight'] = flight
                    # ADS-B emitter category (A7 = rotorcraft…) so replay can draw the
                    # right silhouette — the type alone left the police Bell 429 a jet.
                    if a.get('category') and not c.get('cat'):
                        c['cat'] = str(a.get('category'))[:3]
                    seen_now.add(hx)
                for hx in list(CAPSULE_ACTIVE):
                    c = CAPSULE_ACTIVE[hx]
                    if hx not in seen_now and now - c['last_seen'] > CAPSULE_GAP:
                        _cap_finish(hx, 'left coverage')
                    elif now - c['started'] > CAPSULE_MAX_SEC:
                        _cap_finish(hx, 'max length')
                for hx in list(CAPSULE_SUPPRESS):
                    if now - CAPSULE_SUPPRESS[hx] > CAPSULE_GAP:
                        del CAPSULE_SUPPRESS[hx]      # it has left — auto may record its next visit
        except Exception as e:
            if time.time() - last_err > 300:          # say it, but not every 2 s
                logging.warning('[capsule] recorder pass failed: %s', e)
                last_err = time.time()
        time.sleep(CAPSULE_INTERVAL)

def _cap_public(hx, c):
    return {'hex': hx, 'id': c['id'], 'started': c['started'], 'last_seen': c['last_seen'],
            'auto': c['auto'], 'points': c['points'], 'flight': c.get('flight', ''),
            'audio': CAPSULE_AUDIO_STATE.get('state', 'idle'), 'wake': bool(c.get('wake'))}

@app.route('/api/capsules', methods=['GET'])
def api_capsules():
    with CAPSULE_LOCK:
        active = [_cap_public(h, c) for h, c in CAPSULE_ACTIVE.items()]
        active_ids = {c['id'] for c in CAPSULE_ACTIVE.values()}
    done = []
    try:
        for fn in os.listdir(CAPSULE_DIR):
            if fn.endswith('.json') and _cap_valid_id(fn[:-5]) and fn[:-5] not in active_ids:
                m = _cap_load_json(fn, None)
                if isinstance(m, dict) and m.get('status') == 'done':
                    done.append(m)
    except FileNotFoundError:
        pass
    done.sort(key=lambda m: m.get('started') or 0, reverse=True)
    # 'now' lets the dashboard compute elapsed time against the PI's clock, not
    # the viewing device's (a phone with a skewed clock would otherwise show nonsense).
    return jsonify({'active': active, 'capsules': done[:300], 'rules': _cap_rules(),
                    'now': time.time(), 'gap_sec': CAPSULE_GAP, 'interval_sec': CAPSULE_INTERVAL,
                    'radio_woken': bool(CAPSULE_WAKE.get('woke'))})

@app.route('/api/capsules/start', methods=['POST'])
def api_capsule_start():
    body = request.get_json(silent=True) or {}
    hx = str(body.get('hex') or '').upper().strip()
    if not _CAPSULE_HEX_RE.match(hx):
        return jsonify({'ok': False, 'error': 'bad hex'}), 400
    with CAPSULE_LOCK:
        CAPSULE_SUPPRESS.pop(hx, None)
        if hx in CAPSULE_ACTIVE:
            return jsonify({'ok': True, 'already': True, 'capsule': _cap_public(hx, CAPSULE_ACTIVE[hx])})
        c = _cap_start(hx, auto=False, flight=str(body.get('flight') or '').strip()[:10])
        return jsonify({'ok': True, 'capsule': _cap_public(hx, c)})

@app.route('/api/capsules/stop', methods=['POST'])
def api_capsule_stop():
    body = request.get_json(silent=True) or {}
    hx = str(body.get('hex') or '').upper().strip()
    with CAPSULE_LOCK:
        if hx not in CAPSULE_ACTIVE:
            return jsonify({'ok': False, 'error': 'not recording'}), 404
        meta = _cap_finish(hx, 'stopped')
        CAPSULE_SUPPRESS[hx] = time.time()   # don't let an auto rule re-arm it on the next pass
    return jsonify({'ok': True, 'capsule': meta})

@app.route('/api/capsules/rules', methods=['POST'])
def api_capsule_rules():
    body = request.get_json(silent=True) or {}
    action = body.get('action')
    hx = str(body.get('hex') or '').upper().strip()
    cs = str(body.get('callsign') or '').upper().strip()
    label = str(body.get('label') or '').strip()[:40]
    # v1.5.27 (MME1, thread 75): add an aircraft BEFORE it ever shows up, by its
    # registration. Resolved to a hex here, against this node's aircraft database,
    # because hex is what a rule matches on (it survives callsign changes). Compared
    # with punctuation stripped, so G-ABCD, GABCD and g abcd are the same aircraft.
    reg_in = re.sub(r'[^A-Z0-9]', '', str(body.get('reg') or '').upper())
    reg_found = ''
    if reg_in and not hx and action == 'add':
        if not (2 <= len(reg_in) <= 10):
            return jsonify({'ok': False, 'error': 'bad registration'}), 400
        hits = []
        db = AIRCRAFT_DB if isinstance(AIRCRAFT_DB, dict) else {}
        try:
            # Walk the dict itself, no copy: it is ~570k entries and a Pi 4 has RAM to spare
            # for neither. The weekly reload swaps in a new dict (safe); an overlay merge
            # mutates in place, and if that races us we say so rather than half-answer.
            for h, e in db.items():
                r = e.get('r') or ''
                if r and r.replace('-', '').replace(' ', '').upper() == reg_in:   # str ops, not a regex: ~570k rows
                    hits.append({'hex': h, 'reg': r, 'type': e.get('t', '')})
                    if len(hits) > 10:
                        break
        except RuntimeError:
            return jsonify({'ok': False, 'error': 'busy',
                            'message': 'The aircraft database is reloading. Try again in a moment.'}), 503
        if not hits:
            return jsonify({'ok': False, 'error': 'not_found',
                            'message': 'Not in this node\'s aircraft database. Add it by hex code instead.'}), 404
        if len(hits) > 1:
            # A registration can be re-used, or sit on two airframes in the data. Never
            # guess which one: hand the choice back to the person.
            return jsonify({'ok': False, 'error': 'several', 'candidates': hits[:10]}), 409
        hx, reg_found = hits[0]['hex'], hits[0]['reg']
        if not label:
            label = ' '.join(x for x in (reg_found, hits[0]['type']) if x)[:40]
    if hx and not _CAPSULE_HEX_RE.match(hx):
        return jsonify({'ok': False, 'error': 'bad hex'}), 400
    if cs and not re.match(r'^[A-Z0-9]{2,8}$', cs):
        return jsonify({'ok': False, 'error': 'bad callsign'}), 400
    if not hx and not cs:
        return jsonify({'ok': False, 'error': 'hex or callsign required'}), 400
    if action == 'add' and hx and not label:
        # Typed in by hex (v1.5.27): name it from the database so the list reads
        # "ZK-IPC B429", not a bare code — the card's ⟳ AUTO always sends a label.
        e = AIRCRAFT_DB.get(hx, {}) if isinstance(AIRCRAFT_DB, dict) else {}
        reg_found = reg_found or e.get('r', '')
        label = ' '.join(x for x in (e.get('r', ''), e.get('t', '')) if x)[:40] or hx
    if action not in ('add', 'remove', 'wake'):
        return jsonify({'ok': False, 'error': 'action must be add, remove or wake'}), 400
    def _same(r):
        return (hx and (r.get('hex') or '').upper() == hx) or (cs and (r.get('callsign') or '').upper() == cs)
    with CAPSULE_LOCK:
        if action == 'wake':
            # v1.5.26 (MME1, AJ: opt-in per rule): may a capsule started by THIS rule
            # switch the radio on? Off unless the owner ticks it, rule by rule.
            rules = _cap_rules()
            hit = [r for r in rules if _same(r)]
            if not hit:
                return jsonify({'ok': False, 'error': 'no such rule'}), 404
            for r in hit:
                if body.get('on'):
                    r['wake'] = True
                else:
                    r.pop('wake', None)
            _cap_save_json('rules.json', rules)
            # An aircraft already being recorded under this rule follows the change now,
            # not on its next visit — unticking must stop it holding the radio on.
            for ahx, c in CAPSULE_ACTIVE.items():
                if c.get('auto') and _cap_rule_match(hit, ahx, c.get('flight')):
                    c['wake'] = bool(body.get('on'))
            _cap_save_active()
            return jsonify({'ok': True, 'rules': rules})
        old = [r for r in _cap_rules() if _same(r)]
        rules = [r for r in _cap_rules() if not _same(r)]
        if action == 'add':
            rule = {'label': label, 'added': time.time()}
            if hx:
                rule['hex'] = hx
            if cs:
                rule['callsign'] = cs
            if reg_found:
                rule['reg'] = reg_found
            if body.get('wake') or any(r.get('wake') for r in old):
                rule['wake'] = True     # re-adding a rule must not quietly drop its radio opt-in
            rules.append(rule)
        _cap_save_json('rules.json', rules)
    return jsonify({'ok': True, 'rules': rules})

@app.route('/api/capsules/<cid>', methods=['GET'])
def api_capsule_get(cid):
    cid = cid.upper()
    if not _cap_valid_id(cid):
        return jsonify({'error': 'bad id'}), 400
    meta = _cap_load_json(cid + '.json', None)
    if not isinstance(meta, dict):
        return jsonify({'error': 'not found'}), 404
    with CAPSULE_LOCK:                       # a live capsule's category is only in memory until it finishes
        for c in CAPSULE_ACTIVE.values():
            if c.get('id') == cid and c.get('cat') and not meta.get('cat'):
                meta = dict(meta, cat=c['cat'])
    return jsonify({'meta': meta, 'points': _cap_read_points(cid)})

@app.route('/api/capsules/<cid>/delete', methods=['POST'])
def api_capsule_delete(cid):
    cid = cid.upper()
    if not _cap_valid_id(cid):
        return jsonify({'ok': False, 'error': 'bad id'}), 400
    with CAPSULE_LOCK:
        if cid in {c['id'] for c in CAPSULE_ACTIVE.values()}:
            return jsonify({'ok': False, 'error': 'still recording — stop it first'}), 409
        removed = 0
        for fn in [cid + '.json', cid + '.jsonl'] + _cap_audio_files(cid):
            try:
                os.remove(_cap_path(fn))
                removed += 1
            except OSError:
                pass
    return jsonify({'ok': removed > 0})

@app.route('/api/capsules/<cid>/kml', methods=['GET'])
def api_capsule_kml(cid):
    # Google Earth export: a static path (LineString) plus a timed gx:Track, so
    # Earth's time slider can fly it. Altitudes are barometric feet -> metres,
    # absolute — close enough for screenshots, not survey-grade.
    from xml.sax.saxutils import escape
    cid = cid.upper()
    if not _cap_valid_id(cid):
        return jsonify({'error': 'bad id'}), 400
    meta = _cap_load_json(cid + '.json', None)
    pts = _cap_read_points(cid)
    if not isinstance(meta, dict) or len(pts) < 2:
        return jsonify({'error': 'not found'}), 404
    title = escape(' '.join(x for x in [meta.get('flight') or '', meta.get('reg') or '', meta.get('type') or ''] if x)
                   or meta.get('hex', cid))
    title += ' — ' + time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(meta.get('started') or pts[0]['t']))
    line, whens, coords = [], [], []
    for p in pts:
        alt = p.get('alt')
        alt_m = round(alt * 0.3048, 1) if isinstance(alt, (int, float)) else 0
        line.append('%s,%s,%s' % (p['lon'], p['lat'], alt_m))
        whens.append('<when>%s</when>' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p['t'])))
        coords.append('<gx:coord>%s %s %s</gx:coord>' % (p['lon'], p['lat'], alt_m))
    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">\n'
           '<Document><name>' + title + '</name>\n'
           '<Style id="trk"><LineStyle><color>ff00c8ff</color><width>3</width></LineStyle></Style>\n'
           '<Placemark><name>' + title + ' (path)</name><styleUrl>#trk</styleUrl>'
           '<LineString><altitudeMode>absolute</altitudeMode><tessellate>1</tessellate><coordinates>'
           + ' '.join(line) + '</coordinates></LineString></Placemark>\n'
           '<Placemark><name>' + title + ' (flight)</name><styleUrl>#trk</styleUrl>'
           '<gx:Track><altitudeMode>absolute</altitudeMode>' + ''.join(whens) + ''.join(coords)
           + '</gx:Track></Placemark>\n'
           '</Document></kml>\n')
    resp = Response(kml, mimetype='application/vnd.google-earth.kml+xml')
    resp.headers['Content-Disposition'] = 'attachment; filename="pilnk-capsule-%s.kml"' % cid
    return resp

# ── Flight capsules — Ship 2: synced ATC audio (24 Sep 2026) ─────────────
# While any capsule is recording AND the radio is already playing, the node
# taps pilnkradio's audio stream (ws://127.0.0.1:5656/sdr/audio — float32 mono
# 48 kHz, 512-sample frames; no-Origin local clients are allowed) and encodes
# it to Opus through ffmpeg, beside the track.
#
# AJ's rule (24 Sep): a capsule does not touch the radio. The engine's `playing`
# flag is operator consent and it is shared with every dashboard; if it is off,
# the capsule is track-only and says so.
#
# ONE exception, v1.5.26 (MME1 asked; AJ chose opt-in PER RULE, 25 Sep): an
# auto-record rule the owner has ticked "wake the radio" on may switch `playing`
# on for its capsule. Ticking it IS the consent, given in advance, rule by rule.
# The limits that keep it consent and not a backdoor:
#   - only a ticked rule's capsule can wake it; manual REC and unticked rules never do;
#   - dashboards stay silent — each browser plays only after its own LISTEN (P1-B);
#   - if the radio goes OFF while we hold it on, someone pressed STOP: that capsule
#     is vetoed and never wakes it again (the watchdog's never-auto-resume rule);
#   - when nothing is recording any more we switch it back off — unless a person
#     pressed LISTEN meanwhile (/api/capsules/radio_claim), then it is theirs;
#   - held-on state is saved (radio_wake.json): the engine persists `playing`, so a
#     restart mid-flight must still end with the radio switched back off.
#
# SEGMENTS, not one file. Audio exists only in stretches: the radio can be
# switched off and on mid-flight, the service can restart, the frequency can
# change. Each stretch is its own file, capsules/<id>.a<N>.ogg, and the meta
# records {n, file, t0, dur, hz, mode} for it — so sync is a plain offset
# (t - t0) and a gap in the audio is simply a stretch with no segment, never
# silence pretending to be a recording. Ogg because a file cut short by a power
# cut or restart is still playable up to where it stopped (MP4 would not be).
#
# Within a segment the audio is anchored to WALL CLOCK: if the stream falls
# more than CAPSULE_AUDIO_SLACK behind (a USB hiccup), zeros are padded; if it
# runs ahead, a chunk is dropped. Replay can then trust t0 + position.
#
# Honest limit, shown in the UI: this is everything on the TUNED frequency
# while the aircraft was recorded, not that aircraft's own calls.
#
# No new Python dependency: the WebSocket client is ~40 lines of stdlib, the
# same approach as tools/controller_watch.py, with the gaps closed (64-bit
# lengths, ping/pong, close frames, a proper 101 check). ffmpeg is installed by
# install.sh with `|| true`, so it can be missing; then capsules stay track-only
# and /api/capsules says 'no_ffmpeg'.
import base64 as _cap_b64
import shutil as _cap_shutil
import struct as _cap_struct

CAPSULE_AUDIO_RATE  = 48000
CAPSULE_AUDIO_KBPS  = 20        # Opus voip; ~9 MB per recorded hour, squelched silence is nearly free
CAPSULE_AUDIO_SLACK = 0.5       # seconds of drift tolerated before re-anchoring to wall clock
CAPSULE_AUDIO_STALL = 4         # no audio this long -> the segment ends (radio off, dongle gone)
CAPSULE_AUDIO_MIN   = 1.0       # a segment shorter than this is discarded
CAPSULE_AUDIO_STATE = {'state': 'idle', 'hz': None, 'mode': None, 'since': time.time()}

def _cap_audio_set(state, hz=None, mode=None):
    if CAPSULE_AUDIO_STATE.get('state') != state or CAPSULE_AUDIO_STATE.get('hz') != hz:
        CAPSULE_AUDIO_STATE.update({'state': state, 'hz': hz, 'mode': mode, 'since': time.time()})
        logging.info('[capsule-audio] %s%s', state, (' @ %.3f MHz' % (hz / 1e6)) if hz else '')

def _cap_ws_open(path):
    s = socket.create_connection(('127.0.0.1', 5656), timeout=5)
    key = _cap_b64.b64encode(os.urandom(16)).decode()
    s.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:5656\r\nUpgrade: websocket\r\n'
               'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'
               % (path, key)).encode())
    resp = b''
    while b'\r\n\r\n' not in resp:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise ConnectionError('closed during handshake')
        resp += chunk
        if len(resp) > 16384:
            s.close()
            raise ConnectionError('oversized handshake')
    head, _, rest = resp.partition(b'\r\n\r\n')
    if b' 101 ' not in head.split(b'\r\n', 1)[0] + b' ':
        s.close()
        raise ConnectionError('upgrade refused: %r' % head[:60])
    return s, bytearray(rest)

def _cap_ws_send(s, opcode, payload=b''):
    # Client-to-server frames must be masked (RFC 6455 5.3).
    mask = os.urandom(4)
    n = len(payload)
    hdr = bytes([0x80 | opcode])
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + _cap_struct.pack('>H', n)
    else:
        hdr += bytes([0x80 | 127]) + _cap_struct.pack('>Q', n)
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

def _cap_ws_frames(buf):
    """Pop complete frames off `buf` (a bytearray); yields (opcode, payload)."""
    while len(buf) >= 2:
        op = buf[0] & 0x0F
        masked = buf[1] & 0x80
        ln = buf[1] & 0x7F
        off = 2
        if ln == 126:
            if len(buf) < 4:
                return
            ln = _cap_struct.unpack('>H', bytes(buf[2:4]))[0]
            off = 4
        elif ln == 127:
            if len(buf) < 10:
                return
            ln = _cap_struct.unpack('>Q', bytes(buf[2:10]))[0]
            off = 10
        mk = None
        if masked:
            if len(buf) < off + 4:
                return
            mk = bytes(buf[off:off + 4])
            off += 4
        if len(buf) < off + ln:
            return
        payload = bytes(buf[off:off + ln])
        del buf[:off + ln]
        if mk:
            payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
        yield op, payload

class _CapAudioSeg:
    """One stretch of audio for one capsule: an ffmpeg process fed raw f32le."""
    def __init__(self, cid, n, hz, mode):
        self.cid, self.n, self.hz, self.mode = cid, n, hz, mode
        self.name = '%s.a%d.ogg' % (cid, n)
        self.t0 = None
        self.samples = 0
        self.dead = False
        self.registered = False     # in the capsule meta yet? (see _cap_audio_register)
        self.proc = subprocess.Popen(
            # Write-through (24 Sep, after the 17:11 restart lost a whole stretch):
            # by default ffmpeg probes its input and then holds ~32 KB of output
            # before touching disk — several MINUTES of squelched radio — so a
            # restart left a 0-byte file. No probing, a flush per packet and
            # half-second Ogg pages: measured, a hard kill now keeps all but the
            # last ~1 s.
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
             '-probesize', '32', '-analyzeduration', '0', '-fflags', '+nobuffer',
             '-f', 'f32le', '-ar', str(CAPSULE_AUDIO_RATE), '-ac', '1', '-i', 'pipe:0',
             '-c:a', 'libopus', '-b:a', '%dk' % CAPSULE_AUDIO_KBPS, '-application', 'voip',
             '-flush_packets', '1', '-page_duration', '500000',
             '-f', 'ogg', _cap_path(self.name)],
            # stderr to DEVNULL, not PIPE: nothing drains a pipe while recording,
            # and a chatty ffmpeg filling 64 KB of stderr would block mid-flight.
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, pcm, now):
        n = len(pcm) // 4
        if not n or self.dead:
            return
        if self.t0 is None:
            self.t0 = now - n / CAPSULE_AUDIO_RATE     # the chunk's first sample is n samples old
        expected = (now - self.t0) * CAPSULE_AUDIO_RATE
        slack = CAPSULE_AUDIO_SLACK * CAPSULE_AUDIO_RATE
        try:
            behind = expected - (self.samples + n)
            if behind > slack:                          # stream stalled briefly: keep the clock honest
                pad = int(behind)
                self.proc.stdin.write(b'\x00' * (4 * pad))
                self.samples += pad
            elif (self.samples + n) - expected > slack:
                return                                  # running ahead: drop this chunk
            self.proc.stdin.write(pcm)
            self.samples += n
        except (BrokenPipeError, OSError, ValueError) as e:
            self.dead = True
            logging.warning('[capsule-audio] encoder for %s died: %s', self.name, e)

    def close(self):
        """Finish the file. Returns its meta entry, or None if too short / failed."""
        # EOF on stdin is ffmpeg's signal to flush and finish the Ogg file. Close it,
        # then wait — NOT communicate(), which flushes stdin and raises on a pipe
        # that is already closed (caught in the sandbox test: every segment was
        # being killed and deleted).
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        dur = self.samples / CAPSULE_AUDIO_RATE
        ok = (self.t0 is not None and dur >= CAPSULE_AUDIO_MIN and self.proc.returncode == 0
              and os.path.exists(_cap_path(self.name)))
        if not ok:
            if self.proc.returncode not in (0, None):
                logging.warning('[capsule-audio] ffmpeg exit %s for %s', self.proc.returncode, self.name)
            try:
                os.remove(_cap_path(self.name))
            except OSError:
                pass
            return None
        return {'n': self.n, 'file': self.name, 't0': round(self.t0, 3), 'dur': round(dur, 1),
                'hz': self.hz, 'mode': self.mode}

def _cap_audio_register(seg):
    """Record a stretch in the capsule meta the moment its start time is known,
    marked open (dur None). If the service stops before the stretch closes,
    restart recovery finds it, measures what reached disk, and keeps it — the
    17:11 restart on 24 Sep left an orphan file nothing could place in time."""
    seg.registered = True
    with CAPSULE_LOCK:
        if not os.path.exists(_cap_path(seg.cid + '.json')):
            return
        meta = _cap_load_json(seg.cid + '.json', {})
        segs = [a for a in (meta.get('audio') or []) if a.get('n') != seg.n]
        segs.append({'n': seg.n, 'file': seg.name, 't0': round(seg.t0, 3), 'dur': None,
                     'hz': seg.hz, 'mode': seg.mode, 'open': True})
        segs.sort(key=lambda a: a.get('t0') or 0)
        meta['audio'] = segs
        _cap_save_json(seg.cid + '.json', meta)

def _cap_probe_dur(path):
    """Duration of an Ogg file on disk, or None. ffprobe ships with ffmpeg."""
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=nw=1:nk=1', path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except Exception:
        return None

def _cap_audio_recover():
    """Runs once when the tap starts, before any stretch is open in this process.
    Open stretches left by a stop/crash are measured and closed out; files no
    meta knows about (no start time, so they can never be placed) are removed."""
    try:
        names = os.listdir(CAPSULE_DIR)
    except FileNotFoundError:
        return
    with CAPSULE_LOCK:
        for fn in names:
            if not (fn.endswith('.json') and _cap_valid_id(fn[:-5])):
                continue
            cid = fn[:-5]
            meta = _cap_load_json(fn, None)
            if not isinstance(meta, dict):
                continue
            keep, changed = [], False
            for a in (meta.get('audio') or []):
                if a.get('open') or a.get('dur') is None:
                    changed = True
                    d = _cap_probe_dur(_cap_path(a.get('file', '')))
                    if d is not None and d >= CAPSULE_AUDIO_MIN:
                        a = dict(a, dur=round(d, 1))
                        a.pop('open', None)
                        keep.append(a)
                        logging.info('[capsule-audio] recovered %s (%.0fs) after a stop', a['file'], d)
                    else:
                        try:
                            os.remove(_cap_path(a.get('file', '')))
                        except OSError:
                            pass
                else:
                    keep.append(a)
            if changed:
                meta['audio'] = keep
                _cap_save_json(fn, meta)
            known = {a.get('file') for a in keep}
            for af in _cap_audio_files(cid):
                if af not in known:
                    try:
                        os.remove(_cap_path(af))
                        logging.info('[capsule-audio] removed unplaceable orphan %s', af)
                    except OSError:
                        pass

def _cap_audio_close(seg):
    """Finish a segment OUTSIDE the lock (ffmpeg can take a moment to flush), then
    record it in the capsule's meta under the lock."""
    info = seg.close()
    if not info:
        if seg.registered:                      # drop the open entry it left behind
            with CAPSULE_LOCK:
                if os.path.exists(_cap_path(seg.cid + '.json')):
                    meta = _cap_load_json(seg.cid + '.json', {})
                    meta['audio'] = [a for a in (meta.get('audio') or []) if a.get('n') != seg.n]
                    _cap_save_json(seg.cid + '.json', meta)
        return
    with CAPSULE_LOCK:
        if not os.path.exists(_cap_path(seg.cid + '.json')):
            # The capsule was discarded as a blip (or deleted) while this segment
            # was open. Its audio goes with it.
            try:
                os.remove(_cap_path(seg.name))
            except OSError:
                pass
            return
        meta = _cap_load_json(seg.cid + '.json', {})
        segs = [a for a in (meta.get('audio') or []) if a.get('n') != info['n']]
        segs.append(info)
        segs.sort(key=lambda a: a.get('t0') or 0)
        meta['audio'] = segs
        _cap_save_json(seg.cid + '.json', meta)
    logging.info('[capsule-audio] saved %s (%.0fs)', seg.name, info['dur'])

def _cap_sdr_status():
    try:
        return requests.get('http://127.0.0.1:5656/sdr/status', timeout=1.5).json()
    except Exception:
        return None

# ── v1.5.26: "wake the radio" for opted-in auto rules (see the header above) ──
CAPSULE_WAKE = {'woke': False, 'at': None, 'vetoed': []}   # vetoed: capsule ids a person stopped

def _cap_wake_load():
    d = _cap_load_json('radio_wake.json', {})
    if isinstance(d, dict):
        CAPSULE_WAKE['woke'] = bool(d.get('woke'))
        CAPSULE_WAKE['at'] = d.get('at')
        CAPSULE_WAKE['vetoed'] = [v for v in (d.get('vetoed') or []) if _cap_valid_id(str(v))]

def _cap_wake_save():
    try:
        _cap_save_json('radio_wake.json', CAPSULE_WAKE)
    except Exception as e:
        logging.warning('[capsule-audio] could not save radio_wake.json: %s', e)

def _cap_wake_wanted():
    """Ids of recording capsules that may wake the radio and have not been vetoed."""
    with CAPSULE_LOCK:
        return [c['id'] for c in CAPSULE_ACTIVE.values()
                if c.get('wake') and c['id'] not in CAPSULE_WAKE['vetoed']]

def _cap_sdr_post(path, body):
    """POST a control command to the local radio engine. No Origin header, so the
    engine's M3 origin gate lets it through (same as the watchdog). If the owner has
    set the engine's optional token, send it."""
    hdrs = {}
    try:
        with open('/etc/pilnkradio/config.json') as f:
            tok = (json.load(f) or {}).get('token') or ''
        if tok:
            hdrs['X-PiLNK-Token'] = tok
    except Exception:
        pass
    try:
        r = requests.post('http://127.0.0.1:5656' + path, json=body, headers=hdrs, timeout=3)
        return r.status_code == 200 and bool((r.json() or {}).get('ok'))
    except Exception:
        return False

def _cap_radio_wake(ids):
    """Switch the radio on for these capsules. True only once the engine SAYS it is
    playing — the command is queued inside the engine, so the reply alone proves nothing."""
    if not _cap_sdr_post('/sdr/playing', {'on': True}):
        return False
    # Remember BEFORE confirming: from here on we owe the radio an OFF.
    CAPSULE_WAKE.update({'woke': True, 'at': time.time()})
    _cap_wake_save()
    for _ in range(10):
        time.sleep(0.5)
        st = _cap_sdr_status()
        if isinstance(st, dict) and st.get('playing'):
            logging.info('[capsule-audio] radio switched ON for %s (rule opted in)', ', '.join(ids))
            return True
    # Never came on. Take the request back, so it cannot come on later with nobody
    # remembering to switch it off.
    _cap_sdr_post('/sdr/playing', {'on': False})
    CAPSULE_WAKE.update({'woke': False, 'at': None})
    _cap_wake_save()
    return False

def _cap_radio_sleep():
    """Nothing is recording any more. If a capsule switched the radio on and no person
    has claimed it since, switch it back off. Always forget the vetoes."""
    changed = bool(CAPSULE_WAKE['vetoed'])
    CAPSULE_WAKE['vetoed'] = []
    if CAPSULE_WAKE['woke']:
        st = _cap_sdr_status()
        if not isinstance(st, dict):
            if changed:
                _cap_wake_save()
            return                          # engine not answering — it persists `playing`, so keep
                                            # remembering we owe it an OFF and retry next pass
        if st.get('playing'):
            if not _cap_sdr_post('/sdr/playing', {'on': False}):
                return                      # engine busy — try again on the next idle pass
            logging.info('[capsule-audio] radio switched back OFF (capsule finished, nobody claimed it)')
        CAPSULE_WAKE.update({'woke': False, 'at': None})
        changed = True
    if changed:
        _cap_wake_save()

@app.route('/api/capsules/radio_claim', methods=['POST'])
def api_capsule_radio_claim():
    """The dashboard calls this when a person presses LISTEN. From then on the radio
    is theirs: a capsule that switched it on must not switch it off under them."""
    if CAPSULE_WAKE.get('woke'):
        CAPSULE_WAKE.update({'woke': False, 'at': None})
        _cap_wake_save()
        logging.info('[capsule-audio] radio claimed by a listener — capsules will leave it on')
    return jsonify({'ok': True})

def _cap_audio_next_n(cid):
    """Next free segment number. Not len(files): recovery can delete a file in
    the middle of the sequence, and a count would then reuse a live number."""
    ns = []
    for fn in _cap_audio_files(cid):
        try:
            ns.append(int(fn[len(cid) + 2:-4]))
        except ValueError:
            pass
    return (max(ns) + 1) if ns else 0

def capsule_audio_tap():
    segs = {}                                    # cid -> open _CapAudioSeg
    ffmpeg_ok = _cap_shutil.which('ffmpeg') is not None
    last_err = 0.0
    wake_retry_at = 0.0                          # back-off after a wake the engine refused
    off_since = 0.0                              # when a radio we switched on was first seen off
    try:
        _cap_audio_recover()
    except Exception as e:
        logging.warning('[capsule-audio] recovery pass failed: %s', e)
    _cap_wake_load()
    CAPSULE_RESUMED.wait(60)                     # let the recorder reload active.json first

    def wanted():
        with CAPSULE_LOCK:
            return {c['id'] for c in CAPSULE_ACTIVE.values()}

    def close(cid):
        seg = segs.pop(cid, None)
        if seg:
            _cap_audio_close(seg)

    def close_all():
        for cid in list(segs):
            close(cid)

    while True:
        try:
            want = wanted()
            for cid in [c for c in segs if c not in want]:
                close(cid)
            if not want:
                _cap_radio_sleep()               # v1.5.26: hand back a radio we switched on
                off_since = 0.0
                _cap_audio_set('idle')
                time.sleep(2)
                continue
            if not ffmpeg_ok:
                _cap_audio_set('no_ffmpeg')
                time.sleep(30)
                ffmpeg_ok = _cap_shutil.which('ffmpeg') is not None
                continue
            st = _cap_sdr_status()
            if not isinstance(st, dict):
                _cap_audio_set('no_radio')           # no second dongle / engine not answering
                time.sleep(10)
                continue
            if not st.get('playing'):
                if CAPSULE_WAKE['woke'] and not off_since:
                    # Off while we hold it on. Wait before calling it a STOP: the radio
                    # watchdog restarts a stalled engine and resumes it a few seconds
                    # later, and that must not read as a person withdrawing consent.
                    off_since = time.time()
                    _cap_audio_set('radio_off')
                    time.sleep(5)
                    continue
                if CAPSULE_WAKE['woke'] and time.time() - off_since < 20:
                    time.sleep(5)
                    continue
                off_since = 0.0
                if CAPSULE_WAKE['woke']:
                    # We switched it on and it has stayed off: a person pressed STOP.
                    # That withdraws consent for these capsules — never re-wake them.
                    with CAPSULE_LOCK:
                        stopped = [c['id'] for c in CAPSULE_ACTIVE.values() if c.get('wake')]
                    CAPSULE_WAKE['vetoed'] = sorted(set(CAPSULE_WAKE['vetoed']) | set(stopped))
                    CAPSULE_WAKE.update({'woke': False, 'at': None})
                    _cap_wake_save()
                    logging.info('[capsule-audio] radio was stopped by hand — not waking it again for %s',
                                 ', '.join(stopped) or 'these capsules')
                ids = _cap_wake_wanted()
                if ids and time.time() >= wake_retry_at:
                    if _cap_radio_wake(ids):
                        continue                     # on now — start recording on the next pass
                    wake_retry_at = time.time() + 60
                    logging.warning('[capsule-audio] could not switch the radio on (engine refused or did not respond)')
                    _cap_audio_set('wake_failed')
                    time.sleep(5)
                    continue
                _cap_audio_set('wake_failed' if ids else 'radio_off')   # unticked rules: AJ's rule stands
                time.sleep(5)
                continue

            off_since = 0.0                          # it is on: any earlier "off" was a blip
            hz, mode = st.get('vfoHz'), st.get('mode')
            s, buf = _cap_ws_open('/sdr/audio')
            s.settimeout(1.0)
            _cap_audio_set('recording', hz, mode)
            rem = b''
            now = time.time()
            last_audio = last_want = last_poll = now
            try:
                while True:
                    try:
                        chunk = s.recv(65536)
                        if not chunk:
                            raise ConnectionError('audio stream closed')
                    except socket.timeout:
                        chunk = b''
                    now = time.time()
                    if now - last_want >= 1.0:
                        want = wanted()
                        last_want = now
                        for cid in [c for c in segs if c not in want]:
                            close(cid)
                        if not want:
                            break
                    if chunk:
                        buf.extend(chunk)
                        pcm = bytearray()
                        for op, pl in _cap_ws_frames(buf):
                            if op == 0x8:
                                raise ConnectionError('closed by engine')
                            if op == 0x9:
                                _cap_ws_send(s, 0xA, pl)
                            elif op in (0x0, 0x2):
                                pcm.extend(pl)
                        if pcm:
                            data = rem + bytes(pcm)
                            cut = len(data) - (len(data) % 4)
                            rem, data = data[cut:], data[:cut]
                            last_audio = now
                            for cid in want:
                                seg = segs.get(cid)
                                if seg is None or seg.dead:
                                    if seg is not None:
                                        close(cid)
                                    seg = segs[cid] = _CapAudioSeg(cid, _cap_audio_next_n(cid), hz, mode)
                                seg.write(data, now)
                                if seg.t0 is not None and not seg.registered:
                                    _cap_audio_register(seg)
                    if now - last_audio > CAPSULE_AUDIO_STALL:
                        break                        # radio switched off, or the dongle stopped
                    if now - last_poll >= 10:
                        last_poll = now
                        st = _cap_sdr_status()
                        if not isinstance(st, dict) or not st.get('playing'):
                            break
                        if st.get('vfoHz') != hz or st.get('mode') != mode:
                            # Retuned mid-flight: close this stretch so every segment
                            # is labelled with the one frequency it actually holds.
                            close_all()
                            hz, mode = st.get('vfoHz'), st.get('mode')
                            _cap_audio_set('recording', hz, mode)
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            close_all()
        except Exception as e:
            close_all()
            _cap_audio_set('error')
            if time.time() - last_err > 300:
                logging.warning('[capsule-audio] tap failed: %s', e)
                last_err = time.time()
            time.sleep(5)

@app.route('/api/capsules/<cid>/audio/<int:n>', methods=['GET'])
def api_capsule_audio(cid, n):
    from flask import send_file
    cid = cid.upper()
    if not _cap_valid_id(cid) or n < 0 or n > 999:
        return jsonify({'error': 'bad id'}), 400
    p = _cap_path('%s.a%d.ogg' % (cid, n))
    if not os.path.isfile(p):
        return jsonify({'error': 'not found'}), 404
    # conditional=True answers Range requests, which is what lets the browser seek.
    return send_file(p, mimetype='audio/ogg', conditional=True, max_age=0)

capsule_thread = threading.Thread(target=capsule_recorder, daemon=True)
capsule_thread.start()
capsule_audio_thread = threading.Thread(target=capsule_audio_tap, daemon=True)
capsule_audio_thread.start()

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

@app.route('/api/health')
def api_health():
    """Liveness for the node watchdog (pilnk-dashboard-watchdog.sh). Deliberately
    the LIGHTEST route on the node — no shelling out, no DB, no file reads — so a
    wedged worker is the only reason it fails to answer. Reports two independent
    ages so the watchdog acts on LOCAL faults only:
      loop_ago_sec    — since the ping loop last iterated (PING_LOOP_TS). Stale
                        means the ping thread died; a restart revives it.
      ping_ok_ago_sec — since the last SUCCESSFUL ping to pilnk.io. INFORMATIONAL
                        ONLY. This going stale can just mean pilnk.io is down, and
                        the watchdog must NOT restart on it — a server outage would
                        otherwise bounce every node in the fleet at once."""
    now = time.time()
    loop_ago = (now - PING_LOOP_TS) if PING_LOOP_TS else None
    ping_ago = (now - PING_LAST_OK_TS) if PING_LAST_OK_TS else None
    return jsonify({
        'ok': True,
        'loop_ago_sec': round(loop_ago, 1) if loop_ago is not None else None,
        'ping_ok_ago_sec': round(ping_ago, 1) if ping_ago is not None else None,
    })


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

# Raw Mode S stream port. dump1090-fa and readsb both default to 30002, but a
# node running a third-party decoder stack (adsb.im and similar) can serve it
# elsewhere. Configurable via "bds_port" in config.json.
#
# Getting it wrong is not silent in the journal — the loop below warns and
# retries every 5s — but it IS invisible everywhere anyone looks. The dashboard
# just shows empty extended fields (selected altitude, roll, true airspeed, the
# values the 3D Approach view draws) and the ping reports nothing about it, so
# the only evidence is a warning repeating forever in a log nobody opens.
# Surfacing a bds state in node_features would close that; not done yet.
BDS_PORT = _cfg_port('bds_port', 30002)
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
    # BDS 4,4 (wind_speed / wind_direction / temperature) REMOVED 2026-09-12.
    # Measured ZERO across a 13.5-hour, 833 MB capture over a full day cycle;
    # aircraft here do not broadcast it and their GICB reports do not advertise
    # it. The only values it ever produced were register-inference false
    # positives. Do not re-add without evidence the register is actually being
    # transmitted — a wrong wind is indistinguishable from a right one.
}

# Physical plausibility bounds, applied BEFORE a value enters the cache.
#
# DF20/21 Comm-B replies carry NO register identifier. The decoder INFERS which
# BDS register a 56-bit payload is by testing whether the bit pattern looks
# plausible for each candidate, and that inference is fallible. Observed 2026-09-12:
# a mostly-zero payload decoded as a valid-looking BDS 4,5 carrying
# static_pressure 1060 hPa — near the highest surface pressure ever recorded, and
# flatly contradicted by barometric_setting 1012.0 in the SAME frame.
#
# An impossible number is worse than a missing one: /flights feeds the 3D view,
# which renders whatever it is given as a real attitude. Bounds are deliberately
# generous — wide enough for military and high-performance types, tight enough to
# catch garbage. A field outside its bound is dropped, not clamped: we do not know
# the true value, and inventing one is the same mistake in a different hat.
_BDS_LIMITS = {
    'selected_altitude_mcp':  (-1000, 65520),
    'selected_altitude_fms':  (-1000, 65520),
    'baro_pressure_setting':  (800.0, 1100.0),
    'roll':                   (-90.0, 90.0),
    'true_track':             (0.0, 360.0),
    'track_rate':             (-16.0, 16.0),
    'true_airspeed':          (0, 1000),
    'magnetic_heading':       (0.0, 360.0),
    'indicated_airspeed':     (0, 600),
    'mach':                   (0.0, 3.0),
    'baro_vertical_rate':     (-20000, 20000),
    'inertial_vertical_rate': (-20000, 20000),
}

# Counters, not globals — a dict avoids a `global` declaration in the loop.
_bds_stats = {'rejected': 0, 'tas_dropped': 0}


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
                            # Per-FIELD timestamps. '_updated' alone is the age of
                            # the newest field of ANY register, so a chatty BDS 6,0
                            # kept a minutes-old bds50_roll looking fresh — and the
                            # 3D view banked the model to a stale angle on final.
                            # Each field now carries its own stamp; _merge_bds
                            # expires them independently.
                            fts = rec.setdefault('_ts', {})
                            for k, v in res.items():
                                if k in _BDS_SKIP or v is None:
                                    continue
                                lim = _BDS_LIMITS.get(k)
                                if (lim is not None
                                        and isinstance(v, (int, float))
                                        and not isinstance(v, bool)
                                        and not (lim[0] <= v <= lim[1])):
                                    _bds_stats['rejected'] += 1
                                    if _bds_stats['rejected'] % 100 == 1:
                                        logging.warning(
                                            '[bds] dropped implausible %s=%r for %s '
                                            '(outside %s) — %d rejected so far',
                                            k, v, icao, lim, _bds_stats['rejected'])
                                    continue
                                rec[k] = v
                                fts[k] = now
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
    """Additively merge cached BDS fields onto one aircraft dict.

    Each field is expired on its OWN age, not the record's. An aircraft
    transmitting one register steadily and another rarely used to have the
    rare one served indefinitely as current; a stale roll is worse than no
    roll, because the 3D view renders it as a real bank angle.
    """
    now = time.time()
    with _bds_lock:
        rec = enrichment_cache.get(icao_upper)
        if not rec or (now - rec.get('_updated', 0)) >= BDS_CACHE_TTL:
            return
        fts = rec.get('_ts') or {}
        for prefix, fields in _BDS_FIELDS.items():
            for f in fields:
                # Absent stamp -> treated as infinitely old and dropped, so a
                # cache written by an older build can never leak an undated value.
                if f in rec and (now - fts.get(f, 0)) < BDS_CACHE_TTL:
                    ac['{}_{}'.format(prefix, f)] = rec[f]

    # Cross-field sanity: a true airspeed below HALF the groundspeed implies a
    # tailwind larger than the aircraft's own airspeed, which cannot happen;
    # likewise TAS above twice GS implies an impossible headwind. Observed live
    # on ZK-NEP (DH8C): bds50_true_airspeed 66 kt against a 215 kt groundspeed at
    # 8,700 ft, sitting beside an otherwise plausible roll — a range check alone
    # would never catch it, because 66 kt is a perfectly legal airspeed.
    #
    # Gated on gs > 150 so a hovering helicopter, which legitimately has near-zero
    # TAS at low groundspeed, is never touched. Only the suspect field is dropped:
    # roll and track are cached with their own timestamps and may well have come
    # from a different, good reply.
    gs = ac.get('gs')
    tas = ac.get('bds50_true_airspeed')
    if (isinstance(gs, (int, float)) and gs > 150
            and isinstance(tas, (int, float)) and not isinstance(tas, bool)
            and (tas < gs * 0.5 or tas > gs * 2.0)):
        ac.pop('bds50_true_airspeed', None)
        _bds_stats['tas_dropped'] += 1
        if _bds_stats['tas_dropped'] % 100 == 1:
            logging.warning('[bds] dropped TAS %r vs groundspeed %r for %s — '
                            '%d dropped so far',
                            tas, gs, icao_upper, _bds_stats['tas_dropped'])


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


# --- ATC STT transcript route ------------------------------------------------
# ATC_TRANSCRIPT_PATH and ATC_TRANSCRIPT_STALE_SECS now live near the top with
# the other path constants. They were defined here, which put them ~670 lines
# after the ping thread that reads the path — see the note there.


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
            # Military overlay — a reserved national military hex block, OR the
            # aircraft DB's own per-airframe military flag (v1.5.22). Hex blocks
            # alone missed every air arm that shares its nation's civil block:
            # an RNZAF C-130J drew as a narrow-body airliner with no card data.
            _mil = classify_icao(hex_code)
            _dbmil = bool(entry and entry.get('m'))
            if _mil or _dbmil:
                ac['is_military'] = True
                _cat = match_aircraft_type(ac.get('t'))
                if _cat:
                    ac['mil_common_name'] = _cat['name']
                    ac['mil_rarity'] = _cat['rarity']
                    ac['mil_emoji'] = _cat['emoji']
                    ac['mil_class'] = _cat['class']
                if _mil:
                    # Inside a military block: unchanged behaviour — the catalog's
                    # type-specific branch (USAF vs USN) beats the block's label.
                    ac['mil_branch'] = _cat['branch'] if _cat else _mil.get('branch', '')
                    ac['mil_country'] = _cat.get('cc', '') if _cat else _mil.get('cc', '')
                else:
                    # Flagged by the DB alone: the air arm is NOT known here, and
                    # the catalog branch is per TYPE (its C-130J row says USAF even
                    # on an RNZAF airframe). Blank beats wrong; the cinematic card
                    # resolves the real operator via pilnk.io's mil-lookup.
                    ac['mil_branch'] = ''
                    ac['mil_country'] = ''
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
# 6 h (was 15 min). Airspaces and NAVAIDs change on 28-day AIRAC cycles, and
# every upstream call spends quota on a key the WHOLE FLEET shares.
_OPENAIP_TTL = 6 * 3600
#
# PERSISTED TO DISK + 429 BACKOFF, 23 Sep 2026. The cache lived only in memory,
# so every restart emptied it — and a release restarts every node at once. That
# night three restarts in an hour, each followed by a dashboard reload, sent
# fresh airspace+navaid requests upstream on the shared key until OpenAIP
# answered 429, and with nothing cached the overlay had nothing to fall back
# on: "airspace isn't loading", 502 in the console. Now a restart comes back
# with its data, and after a 429 the node stops asking for a while instead of
# spending another request on every reload.
_OPENAIP_CACHE_FILE = os.path.join(PILNK_DIR, 'openaip_cache.json')
_OPENAIP_LOCK = threading.Lock()
_OPENAIP_BACKOFF = {'until': 0.0}
_OPENAIP_BACKOFF_SECS = 600
_OPENAIP_DISK_MAX = 20      # entries kept on disk (a node asks for ~2)

def _openaip_load_disk():
    try:
        with open(_OPENAIP_CACHE_FILE, 'r') as f:
            d = json.load(f)
        for k, v in (d or {}).items():
            if isinstance(v, list) and len(v) == 2:
                _OPENAIP_CACHE[k] = (float(v[0]), v[1])
        logging.info('[openaip] loaded %d cached layer(s) from disk', len(_OPENAIP_CACHE))
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning('[openaip] disk cache unreadable, starting empty: %s', e)

def _openaip_save_disk():
    try:
        with _OPENAIP_LOCK:
            newest = sorted(_OPENAIP_CACHE.items(), key=lambda kv: kv[1][0], reverse=True)[:_OPENAIP_DISK_MAX]
            tmp = _OPENAIP_CACHE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({k: [t, d] for k, (t, d) in newest}, f)
            os.replace(tmp, _OPENAIP_CACHE_FILE)
    except Exception as e:
        logging.warning('[openaip] could not persist cache: %s', e)

_openaip_load_disk()


def _openaip_cache_key(endpoint, params):
    """Cache key with the position ROUNDED.

    11 Sep 2026: the overlay was dying with 502s and the cause was this key.
    The browser sends the receiver position at whatever precision it happens to
    have — one request arrives as pos=-37.0082,174.7917 and the next, a second
    later, as pos=-37.008,174.791. Same place, ~20 metres apart, but two
    different strings and therefore two different cache entries.

    So the cache essentially never hit. Every overlay toggle and every hard
    refresh went upstream for both layers, OpenAIP answered 429 Too Many
    Requests, and the proxy turned that into a 502 with an empty items list —
    which the map faithfully drew as "no airspaces". That is the reported
    "shows then disappears": the first pair loaded, the second pair wiped them.

    Rounding to 2dp is ~1 km. The query radius is 150 km and the data changes
    on AIRAC cycles, so this is free: every request from one receiver now
    collapses onto a single entry.
    """
    p = dict(params)
    pos = p.get('pos')
    if pos:
        try:
            lat, lon = pos.split(',')
            p['pos'] = '%.2f,%.2f' % (float(lat), float(lon))
        except Exception:
            pass                      # unparseable: key on it verbatim
    return endpoint + '?' + '&'.join('%s=%s' % kv for kv in sorted(p.items()))


@app.route('/api/openaip/<path:endpoint>')
def openaip_proxy(endpoint):
    OPENAIP_KEY = '7670c503a1c0929ee8e87ad581d9119e'
    params = request.args.to_dict()
    cache_key = _openaip_cache_key(endpoint, params)

    hit = _OPENAIP_CACHE.get(cache_key)
    if hit and (time.time() - hit[0]) < _OPENAIP_TTL:
        return jsonify(hit[1])

    # Rate-limited recently? Don't ask again yet — each refused call during a
    # 429 window digs the hole deeper for every node on the shared key. Stale
    # data if we have it; otherwise an honest rate-limited error.
    if time.time() < _OPENAIP_BACKOFF['until']:
        if hit:
            return jsonify(hit[1])
        return jsonify({'error': 'upstream HTTP 429 (backing off)', 'rate_limited': True}), 502

    params['apiKey'] = OPENAIP_KEY
    url = f'https://api.core.openaip.net/api/{endpoint}'

    last_err = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                _OPENAIP_CACHE[cache_key] = (time.time(), data)
                _openaip_save_disk()
                return jsonify(data)
            last_err = 'upstream HTTP %s' % r.status_code
            # 429 is rate limiting, not a blip. Retrying 1.5s later just spends
            # another request on a refusal and digs the hole deeper — and so
            # does the NEXT reload, so back off for a while too.
            if r.status_code == 429:
                _OPENAIP_BACKOFF['until'] = time.time() + _OPENAIP_BACKOFF_SECS
                break
        except Exception as e:
            last_err = str(e)
        if attempt == 1:
            time.sleep(1.5)

    if hit:
        # Stale, but real. Better than a blank overlay. Note this deliberately
        # ignores TTL — for AIRAC-cycle data, hours old still beats nothing.
        app.logger.info('[openaip] %s serving stale cache (%s)', endpoint, last_err)
        return jsonify(hit[1])

    app.logger.warning('[openaip] %s failed after %d attempt(s): %s',
                       endpoint, attempt, last_err)
    # 502, not 500: the failure is upstream, not in this node.
    #
    # NO 'items': [] here. It used to send an empty list, which is a valid,
    # believable answer meaning "nothing in range" — so the client drew it and
    # erased good data. An error must not be shaped like a successful empty
    # result. The client sees the 502 and keeps what it already had.
    return jsonify({'error': last_err, 'rate_limited': last_err.endswith('429')}), 502

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
    import datetime

    # Reads the persisted day store (HIST_*), not TRAIL_HISTORY — see the
    # comment above _hist_load() for why the graph used to look empty.
    with TRAIL_LOCK:
        for h, hexes in HIST_HOURS.items():
            if (h + 1) * 3600 <= cutoff:
                continue
            label = datetime.datetime.fromtimestamp(h * 3600).strftime('%H')
            hour_counts.setdefault(label, set()).update(hexes)

        for hex_code, e in HIST_AC.items():
            last_seen = e.get('last') or 0
            if last_seen < cutoff:
                continue
            first_seen = max(e.get('first') or last_seen, cutoff)
            aircraft.append({
                'hex': hex_code,
                'callsign': e.get('cs') or '',
                'first_seen': first_seen,
                'last_seen': last_seen,
                'duration': round(last_seen - first_seen),
                'max_alt': e.get('max_alt') or 0,
                'positions': e.get('n') or 0,
                'last_lat': e.get('lat') or 0,
                'last_lon': e.get('lon') or 0,
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

# Line-of-sight at FL400 is ~250 nm. 400 leaves room for tropospheric ducting
# while still rejecting a corrupt position that puts the aircraft in Chile.
STATS_MAX_DIST_NM = 400


def _sane_records(data):
    """Filter all-time records against the same limits the live stats use.

    The dashboard computes these client-side and POSTs them back, so they are
    untrusted input — the server-side gate on the ADS-B feed (STATS_MAX_ALT_FT
    and friends, see ~line 1091) never sees this path. That is how a Gillham
    decode glitch reached stats_records.json as "ANZ974 at 96,400 ft".

    Applied on BOTH write and read: on write so a bad value is never stored, on
    read so records captured before this guard existed stop being served.

    A failing record is DROPPED, not clamped — clamping would publish a number
    nobody actually observed.
    """
    if not isinstance(data, dict):
        return {}
    limits = {
        'highest':  ('alt',   0,                   STATS_MAX_ALT_FT),
        'fastest':  ('speed', STATS_MIN_SPEED_KTS, STATS_MAX_SPEED_KTS),
        'furthest': ('dist',  0,                   STATS_MAX_DIST_NM),
        'most_day': ('count', 0,                   1000000),
    }
    out = {}
    for key, (field, lo, hi) in limits.items():
        rec = data.get(key)
        if not isinstance(rec, dict):
            continue
        val = rec.get(field)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if not (lo < val <= hi):
            logging.warning(f'[stats] dropped implausible {key} record: {field}={val}')
            continue
        out[key] = rec
    return out


@app.route('/api/stats/records', methods=['GET'])
def get_stats_records():
    try:
        if os.path.exists(STATS_RECORDS_FILE):
            with open(STATS_RECORDS_FILE, 'r') as f:
                return jsonify(_sane_records(json.load(f)))
    except Exception:
        pass
    return jsonify({})

@app.route('/api/stats/records', methods=['POST'])
def save_stats_records():
    try:
        data = _sane_records(request.get_json())
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
    'kind': 'owner',        # 'owner' = owner pressed the button; 'maintenance' = standing consent
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


def _cap_ota_log(params):
    """The OTA updater's own log — the ONLY place git's error text is written.

    log_tail reads `journalctl -u pilnk`, which does carry update.sh's own
    log() lines (they echo to stdout as well as the file), so it can tell you
    WHICH step failed. It cannot tell you WHY: update.sh sends git's stderr to
    this file and nowhere else (`git fetch ... 2>> "$LOG_FILE"`).

    That gap is why a node reporting ota_last_result=exit_1 was, for months,
    three possible failures with no way to choose between them — cd, fetch or
    reset. This capability closes it in one request instead of a round trip
    per guess.
    """
    n = _assist_cap_lines(params)
    path = os.path.join(PILNK_DIR, 'update.log')
    if not os.path.exists(path):
        return {'ota_log': '[no update.log on this node — the OTA updater has never run]'}
    return {'ota_log': _assist_run(['tail', '-n', str(n), path])}


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
    'ota_log':          _cap_ota_log,
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


# ── ACTION / WRITE tier (Prong B2) ───────────────────────────────────────────
# SEPARATE from the read whitelist ON PURPOSE. The read tier's safety is "worst
# case you read your own node"; writes need more (see the B2 security review in
# pilnk-tasks/node-management-plane.md). An action runs ONLY when a session is live,
# which happens ONLY under standing consent (kind=maintenance, re-checked at
# dispatch) or owner presence (kind=owner). Every action is a FIXED function; params
# only ever pick a bool/enum, never a command or path. Destructive actions require
# params['confirm'] is True. Each returns {'ok': bool, ...}; the dispatcher keys
# is_err on ok. MUST match the server action allowlist in api/assist.php.
def _act_restart_pilnk(params):
    """Restart the pilnk service. DEFERRED + DETACHED: this very process is what
    systemd restarts, so a synchronous restart would kill us before the result can
    post. A start_new_session child waits ~2s then runs the restart (the same
    passwordless grant update.sh relies on), so we return + post the result first."""
    try:
        subprocess.Popen(
            ['bash', '-c', 'sleep 2; sudo -n systemctl restart pilnk'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {'ok': True, 'action': 'restart_pilnk',
                'detail': 'restart scheduled (~2s) — the node will drop briefly and return'}
    except Exception as e:
        return {'ok': False, 'error': f'restart_pilnk failed to schedule: {e}'}


def _act_restart_service(unit):
    """Restart a sibling systemd unit (NOT app.py itself), synchronously. Needs the
    matching NOPASSWD grant (bootstrap-selfheal.sh installs them)."""
    try:
        r = subprocess.run(['sudo', '-n', 'systemctl', 'restart', unit],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return {'ok': True, 'action': 'restart:' + unit, 'detail': unit + ' restarted'}
        return {'ok': False, 'error': (r.stderr or r.stdout or 'restart failed').strip()[:200]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _act_restart_audio(params):
    """Restart the ATC audio engine (pilnkradio)."""
    return _act_restart_service('pilnkradio')


def _act_restart_decoder(params):
    """Restart the ADS-B decoder — whichever of readsb / dump1090-fa is active, or the
    one named in params['unit']."""
    unit = params.get('unit') if params.get('unit') in ('readsb', 'dump1090-fa') else None
    if not unit:
        for u in ('readsb', 'dump1090-fa'):
            try:
                a = subprocess.run(['systemctl', 'is-active', u],
                                   capture_output=True, text=True, timeout=5)
                if a.stdout.strip() == 'active':
                    unit = u
                    break
            except Exception:
                pass
    if not unit:
        return {'ok': False, 'error': 'no active decoder (readsb / dump1090-fa) found'}
    return _act_restart_service(unit)


def _act_ota_apply(params):
    """Pull the latest release now instead of waiting for the OTA timer. Runs update.sh
    DETACHED (it restarts pilnk at the end, which would kill us), so we return at once
    and the update proceeds on its own. Uses update.sh's own existing sudo grant."""
    try:
        upd = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'update.sh')
        subprocess.Popen(['bash', '-c', 'sleep 1; exec bash "$0"', upd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {'ok': True, 'action': 'ota_apply',
                'detail': 'update started — the node will pull the latest release and restart'}
    except Exception as e:
        return {'ok': False, 'error': f'ota_apply failed to start: {e}'}


def _act_reboot(params):
    """Reboot the whole Pi. DESTRUCTIVE — the dispatcher requires params['confirm'] is
    True. Deferred + detached so the result posts before we go down. Needs the NOPASSWD
    'systemctl reboot' grant (bootstrap-selfheal.sh installs it)."""
    try:
        subprocess.Popen(['bash', '-c', 'sleep 2; sudo -n systemctl reboot'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {'ok': True, 'action': 'reboot',
                'detail': 'reboot scheduled (~2s) — the node will go down and come back'}
    except Exception as e:
        return {'ok': False, 'error': f'reboot failed to schedule: {e}'}


ASSIST_ACTIONS = {
    'restart_pilnk':    _act_restart_pilnk,
    'restart_audio':    _act_restart_audio,
    'restart_decoder':  _act_restart_decoder,
    'ota_apply':        _act_ota_apply,
    'reboot':           _act_reboot,
}

# Destructive actions require an explicit params['confirm'] is True — a guard against a
# blind sweep of the request queue. restart_* / ota_apply are non-destructive.
ASSIST_ACTIONS_DESTRUCTIVE = {'reboot'}

# Node-local, owner-visible audit of operator actions (Prong B3). A file, not memory,
# so the owner sees even the restart that just ran once the node comes back.
ASSIST_ACTION_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assist_actions.log')

def _assist_log_action(action, ok, detail):
    """Append one operator action to the node-local audit log. Capped, best-effort."""
    try:
        rec = {'ts': int(time.time()), 'action': str(action),
               'ok': bool(ok), 'detail': str(detail)[:200]}
        lines = []
        try:
            with open(ASSIST_ACTION_LOG, 'r') as f:
                lines = f.read().splitlines()
        except Exception:
            pass
        lines.append(json.dumps(rec))
        lines = lines[-50:]
        tmp = ASSIST_ACTION_LOG + '.tmp'
        with open(tmp, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, ASSIST_ACTION_LOG)
    except Exception:
        pass


def _assist_post(action, payload):
    payload['action'] = action
    payload['verify_code'] = NODE_VERIFY_CODE
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        ASSIST_BASE, data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'PiLNK/1.0'})
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode() or '{}')


def _remote_maintenance_enabled():
    """Standing consent for unattended operator fixes. Fresh-read each call so a
    toggle in config.json takes effect within one poll cycle — that fresh read IS
    the revoke. Default OFF: a node grants remote maintenance only when its owner
    opts in. Fail-closed: any read error means no consent."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return bool(json.load(f).get('remote_maintenance', False))
    except Exception:
        return False


def assist_open_session(kind='owner'):
    """Open a session at pilnk.io. kind='owner' is the classic owner-pressed-the-
    button session; kind='maintenance' is the standing session the poller keeps
    alive while the owner has granted remote maintenance. Both only ever run
    whitelisted capabilities — the kind is for visibility, audit and revoke."""
    try:
        r = _assist_post('open_session', {'kind': kind})
        if r.get('session_id'):
            with _assist_lock:
                _assist_state['session_id'] = r['session_id']
                _assist_state['human_code'] = r.get('human_code')
                _assist_state['active'] = True
                _assist_state['kind'] = r.get('kind', kind)
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
            kind = _assist_state.get('kind', 'owner')

        # Revoke: standing consent withdrawn while a maintenance session is live.
        # The fresh config read here is what makes a toggle-off take effect within
        # one cycle. Owner-initiated sessions are unaffected.
        if active and sid and kind == 'maintenance' and not _remote_maintenance_enabled():
            assist_close_session('owner')
            continue

        # Expiry. An owner session that expires is closed. A maintenance session
        # that hits its 30-min cap is simply dropped, so the block below re-opens
        # a fresh one — a rolling standing session.
        if active and sid and time.time() > exp:
            if kind == 'maintenance':
                with _assist_lock:
                    _assist_state['active'] = False
                    _assist_state['session_id'] = None
            else:
                assist_close_session('expired')
            continue

        # No live session. Keep a maintenance session open if the owner has opted
        # in (standing consent); otherwise stay idle exactly as before.
        if not active or not sid:
            if _remote_maintenance_enabled():
                assist_open_session(kind='maintenance')
            time.sleep(5)
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
                params = req.get('params') or {}
                if cap in ASSIST_ACTIONS:
                    # WRITE tier. Gate on live consent / owner presence, then confirm.
                    if kind == 'maintenance' and not _remote_maintenance_enabled():
                        result = {'ok': False, 'error': 'remote maintenance consent withdrawn'}
                        is_err = True
                    elif cap in ASSIST_ACTIONS_DESTRUCTIVE and params.get('confirm') is not True:
                        result = {'ok': False, 'error': f'{cap} requires confirm=true'}
                        is_err = True
                    else:
                        try:
                            result = ASSIST_ACTIONS[cap](params)
                            is_err = not (isinstance(result, dict) and result.get('ok'))
                        except Exception as e:
                            result = {'ok': False, 'error': f'action {cap} failed: {e}'}
                            is_err = True
                    _assist_log_action(cap, not is_err,
                                       (isinstance(result, dict) and
                                        (result.get('detail') or result.get('error'))) or '')
                else:
                    fn = ASSIST_CAPABILITIES.get(cap)
                    if fn:
                        try:
                            result = fn(params)
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
        # Snappy for an owner-driven session (Claude-driven debug); gentler for a
        # standing maintenance session, which is idle most of the time.
        time.sleep(5 if kind == 'owner' else 20)


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
            'kind': _assist_state.get('kind', 'owner'),
            'remote_maintenance': _remote_maintenance_enabled(),
            'expires_in_sec': max(0, int(_assist_state['expires_at'] - time.time())) if _assist_state['active'] else 0,
        })


@app.route('/api/assist/end', methods=['POST'])
def api_assist_end():
    assist_close_session('owner')
    return jsonify({'ok': True})


@app.route('/api/assist/consent', methods=['POST'])
def api_assist_consent():
    """Owner grants or revokes standing remote-maintenance consent from their own
    dashboard. Writes remote_maintenance to config.json atomically; the poller reads
    it fresh each cycle, so a revoke takes effect within one cycle."""
    global _config
    body = request.get_json(silent=True) or {}
    want = bool(body.get('on'))
    try:
        cfg = {}
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass
        cfg['remote_maintenance'] = want
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _config = cfg
        _assist_log_action('consent', True, 'granted' if want else 'revoked')
        return jsonify({'ok': True, 'remote_maintenance': want})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/assist/log')
def api_assist_log():
    """Recent operator actions run on this node — owner-visible audit (Prong B3)."""
    out = []
    try:
        with open(ASSIST_ACTION_LOG, 'r') as f:
            for line in f.read().splitlines()[-20:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return jsonify({'actions': list(reversed(out))})


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
