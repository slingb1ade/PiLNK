"""Live side of the ATC context feed (spec pilnk-tasks/atc-context-spec.md v2.2).

Keeps 30 minutes of 5-second snapshots of this node's aircraft.json, loads the airport (from
pilnk.io's runways list, cached on disk), STT's profile file and the learned arrival paths, and
answers GET /api/atc/context?freq=<MHz>&t=<unix> with atc_context.build_context().

OFF unless config.json has "atc_context": true. Everything stays on the node: the only outbound
call of its own is the runways look-up, which sends this node's lat/lon at 1 dp like its other
pilnk.io calls; the learned paths come through the dashboard's own /api/arrival-paths. Both are
refreshed on a background thread, never inside a request. The route sits behind app.py's
same-origin guard like every other dashboard route.
"""
import bisect
import json
import logging
import math
import os
import threading
import time
import urllib.request
from collections import deque

import atc_context as AC

log = logging.getLogger(__name__)
MAX_AGE_S = 1800
NEAR_S = 30
REFRESH_S = 30          # inputs refresh tick
DOC_TTL_S = 3600        # learned paths: refetch after an hour
DOC_RETRY_S = 600       # ... or 10 min after a failed fetch
CACHE_N = 8             # answers kept, per (snapshot, freq)
STALE_POS_S = 30
RUNWAYS_URL = 'https://pilnk.io/api/runways.php?lat=%.1f&lon=%.1f&limit=6'
FIELDS = ('callsign', 'hex', 'lat', 'lon', 'alt_baro', 'gs', 'track', 'baro_rate', 'squawk',
          'nav_altitude_mcp', 'nav_heading', 'nav_qnh', 'nav_altitude_fms', 'nav_modes',
          'dl_next_fix', 'dl_rwy')
_COPY = ('lat', 'lon', 'gs', 'track', 'squawk', 'nav_altitude_mcp', 'nav_heading', 'nav_qnh',
         'nav_altitude_fms', 'nav_modes')


def _n(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ── aircraft.json -> snapshot (§2.2) ─────────────────────────────────────────
def snapshot_from_aircraft(data, now, dl_merge=None):
    t = int(data.get('now') if _n(data.get('now')) else now)
    out = []
    for ac in data.get('aircraft') or []:
        if not isinstance(ac, dict) or not (_n(ac.get('lat')) and _n(ac.get('lon'))):
            continue
        if _n(ac.get('seen_pos')) and ac['seen_pos'] > STALE_POS_S:
            continue
        hx = ac.get('hex').strip().lower() if isinstance(ac.get('hex'), str) and ac.get('hex').strip() else None
        cs = ac.get('flight').strip() if isinstance(ac.get('flight'), str) and ac.get('flight').strip() else None
        if not hx and not cs:
            continue
        s = {k: None for k in FIELDS}
        s['callsign'], s['hex'] = cs, hx
        for k in _COPY:
            s[k] = ac.get(k)
        alt = ac.get('alt_baro')
        s['alt_baro'] = alt if (alt == 'ground' or _n(alt)) else None
        s['baro_rate'] = ac.get('baro_rate') if _n(ac.get('baro_rate')) else (
            ac.get('geom_rate') if _n(ac.get('geom_rate')) else None)
        if dl_merge is not None and hx:
            try:
                tmp = dict(ac)
                dl_merge(tmp, hx.upper())
                d = tmp.get('datalink') if isinstance(tmp.get('datalink'), dict) else {}
                s['dl_next_fix'] = d.get('next_wp') if isinstance(d.get('next_wp'), str) else None
                s['dl_rwy'] = d.get('rwy') if isinstance(d.get('rwy'), str) else None
            except Exception as e:      # datalink is extra: it never costs a snapshot
                log.debug('[atc-context] datalink merge skipped for %s: %s', hx, e)
        out.append(s)
    return {'t': t, 'aircraft': out}


# ── the 30-minute ring ───────────────────────────────────────────────────────
class SnapshotRing:
    def __init__(self, max_age=MAX_AGE_S):
        self.max_age = max_age
        self._snaps = deque()
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._snaps)

    def add(self, snap):
        with self._lock:
            if self._snaps and snap['t'] <= self._snaps[-1]['t']:
                return
            self._snaps.append(snap)
            while self._snaps and self._snaps[0]['t'] < snap['t'] - self.max_age:
                self._snaps.popleft()

    def pick(self, t):
        """Nearest snapshot to t, and the snapshots in [snap.t - max_age, snap.t), oldest first."""
        with self._lock:
            snaps = list(self._snaps)
        if not snaps:
            return None
        ts = [s['t'] for s in snaps]
        i = bisect.bisect_left(ts, t)
        if i == len(ts) or (i > 0 and t - ts[i - 1] <= ts[i] - t):
            i -= 1
        snap = snaps[i]
        lo = bisect.bisect_left(ts, snap['t'] - self.max_age)
        return snap, snaps[lo:i]


class Sampler:
    def __init__(self, ring, read_raw, dl_merge, step=5):
        self.ring, self.read_raw, self.dl_merge, self.step = ring, read_raw, dl_merge, step
        self._last_err = 0.0

    def tick(self, now=None):
        now = time.time() if now is None else now
        try:
            raw = self.read_raw()
            if raw is None:
                return
            self.ring.add(snapshot_from_aircraft(json.loads(raw), now, self.dl_merge))
        except Exception as e:
            if now - self._last_err > 600:      # one line per 10 min, never silent
                self._last_err = now
                log.warning('[atc-context] snapshot skipped: %s', e)

    def start(self):
        def loop():
            while True:
                self.tick()
                time.sleep(self.step)
        threading.Thread(target=loop, name='atc-context-sampler', daemon=True).start()


# ── inputs: airport, profile, arrival paths ──────────────────────────────────
def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'PiLNK/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class AirportSource:
    def __init__(self, lat, lon, icao, fetch_json, cache_path, ttl=86400, retry=600):
        self.icao, self.fetch_json, self.cache_path = icao, fetch_json, cache_path
        self.ttl, self.retry = ttl, retry
        self.url = RUNWAYS_URL % (lat, lon)
        self._airport, self._fetched, self._next_try = None, None, 0
        self._lock = threading.Lock()
        try:
            with open(cache_path) as f:
                c = json.load(f)
            ap = AC.airport_from_runways(c.get('doc'), icao)
            if ap is not None and _n(c.get('fetched_at')):
                self._airport, self._fetched = ap, c['fetched_at']
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, now):
        with self._lock:
            stale = self._fetched is None or now - self._fetched >= self.ttl
            if stale and now >= self._next_try:
                try:
                    doc = self.fetch_json(self.url)
                    ap = AC.airport_from_runways(doc, self.icao)
                    if ap is None:
                        raise ValueError('no airport %s in the runways answer' % (self.icao or '(nearest)'))
                    self._airport, self._fetched = ap, now
                    self._write({'fetched_at': now, 'doc': doc})
                except Exception as e:
                    self._next_try = now + self.retry
                    log.warning('[atc-context] runways fetch failed (keeping last good): %s', e)
            return self._airport

    def _write(self, obj):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(obj, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning('[atc-context] airport cache not written: %s', e)


_PROFILE_CACHE = {}


def load_profile(path):
    """STT's profile file, re-read only when its mtime changes; missing or invalid -> None."""
    if not path:
        return None
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        return None
    hit = _PROFILE_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning('[atc-context] profile %s unreadable: %s', path, e)
        data = None
    _PROFILE_CACHE[path] = (mt, data)
    return data


class Feed:
    """Holds the ring and the last good inputs. refresh() fetches; the route only reads."""

    def __init__(self, ring, airport_source, profile_path, get_arrival_doc, sampler=None):
        self.ring, self.airport_source, self.profile_path = ring, airport_source, profile_path
        self.get_arrival_doc, self.sampler = get_arrival_doc, sampler
        self._airport, self._doc, self._doc_next = None, None, 0
        self._cache, self._cache_lock = [], threading.Lock()

    def refresh(self, now=None):
        now = time.time() if now is None else now
        if self.airport_source is not None:
            try:
                self._airport = self.airport_source.get(now)
            except Exception as e:
                log.warning('[atc-context] airport unavailable (keeping last good): %s', e)
        if callable(self.get_arrival_doc) and now >= self._doc_next:
            try:
                d = self.get_arrival_doc()
                if not isinstance(d, dict) or d.get('unavailable'):
                    raise ValueError('no arrival paths in the answer')
                self._doc, self._doc_next = d, now + DOC_TTL_S
            except Exception as e:
                self._doc_next = now + DOC_RETRY_S
                log.warning('[atc-context] arrival paths unavailable (keeping last good): %s', e)

    def start_refresh(self, every=REFRESH_S):
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:     # never let the inputs thread die
                    log.warning('[atc-context] refresh failed: %s', e)
                time.sleep(every)
        threading.Thread(target=loop, name='atc-context-inputs', daemon=True).start()

    def airport(self):
        return self._airport

    def arrival_doc(self):
        return self._doc

    def context(self, snap, hist, freq):
        """build_context, remembered per (snapshot, freq) while the inputs are the same objects."""
        ap, prof, doc = self._airport, load_profile(self.profile_path), self._doc
        key = (snap['t'], freq)
        with self._cache_lock:
            for k, a, p, d, body in self._cache:
                if k == key and a is ap and p is prof and d is doc:
                    return body
        body = AC.build_context(snap, hist, ap, prof, doc, freq)
        with self._cache_lock:
            self._cache = [e for e in self._cache if e[0] != key][-(CACHE_N - 1):] + [(key, ap, prof, doc, body)]
        return body


def start_from_config(config_path, read_raw, dl_merge, get_arrival_doc, start=True):
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        log.warning('[atc-context] off: config unreadable (%s)', e)
        return None
    if not isinstance(cfg, dict) or cfg.get('atc_context') is not True:
        return None
    lat, lon = cfg.get('lat'), cfg.get('lon')
    cache = os.path.join(os.path.dirname(os.path.abspath(config_path)), 'cache', 'atc_airport.json')
    icao = cfg.get('atc_context_airport') if isinstance(cfg.get('atc_context_airport'), str) else None
    src = AirportSource(lat, lon, icao, fetch_json, cache) if _n(lat) and _n(lon) else None
    if src is None:
        log.warning('[atc-context] no node lat/lon in config: airport will be null')
    prof = cfg.get('atc_context_profile') if isinstance(cfg.get('atc_context_profile'), str) else None
    ring = SnapshotRing()
    sampler = Sampler(ring, read_raw, dl_merge)
    feed = Feed(ring, src, prof, get_arrival_doc, sampler)
    if start:
        sampler.start()
        feed.start_refresh()
        log.info('[atc-context] on: /api/atc/context (airport %s, profile %s)', icao or 'nearest', prof or 'none')
    return feed


# ── the route ────────────────────────────────────────────────────────────────
def register(app, feed, now=time.time):
    from flask import jsonify, request
    last_err = [0.0]

    def out(body, code=200):
        r = jsonify(body)
        r.status_code = code
        r.headers['Cache-Control'] = 'no-store'
        return r

    def num_arg(name):
        v = request.args.get(name)
        if v is None or v == '':
            return None, False
        try:
            f = float(v)
        except ValueError:
            return None, True
        return (f, False) if math.isfinite(f) else (None, True)

    @app.route('/api/atc/context', methods=['GET'])
    def atc_context_route():
        if feed is None:
            return out({'error': 'off'}, 503)
        n = now()
        freq, bad = num_arg('freq')
        if bad:
            return out({'error': 'bad freq'}, 400)
        t, bad = num_arg('t')
        if bad:
            return out({'error': 'bad t'}, 400)
        t = n if t is None else min(t, n)
        if t < n - MAX_AGE_S:
            return out({'error': 'too old'}, 404)
        picked = feed.ring.pick(t)
        if picked is None:
            return out({'error': 'no data yet'}, 503)
        snap, hist = picked
        if abs(snap['t'] - t) > NEAR_S:
            return out({'error': 'no data near t'}, 503)
        try:
            return out(feed.context(snap, hist, freq))
        except Exception as e:      # JSON, never Flask's HTML page; one log line per 10 min
            if n - last_err[0] > 600:
                last_err[0] = n
                log.exception('[atc-context] build failed: %s', e)
            return out({'error': 'internal'}, 500)
