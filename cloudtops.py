"""Cloud tops for the 3D Approach cloud deck (AJ 29 Oct 2026).

The airport METAR gives each cloud layer's base but never its top. The forecast model does: Open-Meteo
(free, no key) serves cloud cover and height at standard pressure levels. This turns that into a simple
profile, [{ft: height above the field, cover: %}, ...] lowest first, and static/cloud-deck.js reads the
tops out of it. Cached per 0.1 deg cell for 30 minutes; on failure the last good answer is kept for up
to 3 hours and a new try waits 5 minutes. Only a lat/lon leaves the node, and only to Open-Meteo.
"""
import math
import threading
import time

LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300)   # hPa, surface to ~30,000 ft
BASE = 'https://api.open-meteo.com/v1/forecast?'
TTL = 1800          # s: a fresh answer is reused this long
STALE_MAX = 3 * 3600  # s: on failure, an older good answer is still better than none
RETRY = 300         # s: after a failure, wait this long before asking again
FT = 3.28084


def url(lat, lon):
    vars_ = ','.join(['cloud_cover_%dhPa' % lv for lv in LEVELS] + ['geopotential_height_%dhPa' % lv for lv in LEVELS])
    return BASE + 'latitude=%.3f&longitude=%.3f&hourly=%s&forecast_hours=1' % (lat, lon, vars_)


def _first(v):
    if isinstance(v, list) and v:
        v = v[0]
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def profile(j, elev_m=None):
    """Open-Meteo answer -> [{'ft', 'cover'}] above the field (elev_m, else the model's ground), lowest first."""
    if not isinstance(j, dict) or not isinstance(j.get('hourly'), dict):
        return []
    h = j['hourly']
    ground = elev_m if isinstance(elev_m, (int, float)) and math.isfinite(elev_m) else _first(j.get('elevation'))
    if ground is None:
        ground = 0.0
    out = []
    for lv in LEVELS:
        cover, gph = _first(h.get('cloud_cover_%dhPa' % lv)), _first(h.get('geopotential_height_%dhPa' % lv))
        if cover is None or gph is None or gph < ground:
            continue
        out.append({'ft': int(round((gph - ground) * FT)), 'cover': int(round(cover))})
    out.sort(key=lambda q: q['ft'])
    return out


class Cache:
    def __init__(self, fetch=None, now=time.time):
        if fetch is None:
            import requests
            fetch = lambda u, timeout=None: requests.get(u, timeout=timeout, headers={'User-Agent': 'PiLNK node (cloud deck)'})
        self.fetch, self.now, self.lock, self.cells = fetch, now, threading.Lock(), {}

    def get(self, lat, lon, elev_m=None):
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise ValueError('lat/lon')
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError('lat/lon')
        key = (round(lat, 1), round(lon, 1), None if elev_m is None else int(round(elev_m)))
        t = self.now()
        with self.lock:
            c = self.cells.get(key)
            if c and c.get('ok') is not None and t - c['ok'] < TTL:
                return {'profile': c['profile'], 'src': 'open-meteo', 'age': int(t - c['ok'])}
            if c and c.get('fail') is not None and t - c['fail'] < RETRY:
                return self._fallback(c, t, c.get('err', 'unavailable'))
        try:
            r = self.fetch(url(lat, lon), timeout=8)
            if getattr(r, 'status_code', 0) != 200:
                raise OSError('HTTP %s' % getattr(r, 'status_code', '?'))
            p = profile(r.json(), elev_m)
            if not p:
                raise ValueError('no levels')
        except Exception as e:   # never let the weather break the page
            with self.lock:
                c = self.cells.setdefault(key, {})
                c['fail'], c['err'] = t, str(e)[:80]
                return self._fallback(c, t, c['err'])
        with self.lock:
            self.cells[key] = {'ok': t, 'profile': p}
            if len(self.cells) > 64:   # a node watches one or two airports; keep it bounded anyway
                for k in sorted(self.cells, key=lambda k: self.cells[k].get('ok') or self.cells[k].get('fail') or 0)[:16]:
                    del self.cells[k]
        return {'profile': p, 'src': 'open-meteo', 'age': 0}

    @staticmethod
    def _fallback(c, t, err):
        if c.get('ok') is not None and t - c['ok'] < STALE_MAX:
            return {'profile': c['profile'], 'src': 'open-meteo', 'age': int(t - c['ok']), 'stale': True}
        return {'profile': [], 'error': err}


_cache = None


def get(lat, lon, elev_m=None):
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache.get(lat, lon, elev_m)


def register(app, cache=None):
    """GET /api/cloudtops?lat=&lon=[&elev= field elevation, m] -> {profile: [{ft, cover}], src, age} on the node.
    400 on bad input; 200 with an empty profile when the model is unreachable (the deck falls back to defaults)."""
    from flask import request, jsonify

    def cloudtops_route():
        a = request.args
        try:
            elev = None if a.get('elev') in (None, '') else float(a['elev'])
            if elev is not None and not math.isfinite(elev):
                raise ValueError('elev')
            body = (cache.get if cache is not None else get)(a.get('lat'), a.get('lon'), elev)
        except (ValueError, TypeError):
            return jsonify({'error': 'lat, lon (and elev) must be numbers'}), 400
        r = jsonify(body)
        r.headers['Cache-Control'] = 'max-age=600'
        return r

    app.add_url_rule('/api/cloudtops', 'cloudtops_route', cloudtops_route)
