"""PiLNK - fix positions learned from the planes themselves (17 Nov 2026).

Why: the dashboard's NZ fix list is 2013 data. QFA145's FMS said "next DOMRI in 31 s" while the
list put DOMRI 25 nm away - the fix had moved. Nobody may republish the AIP list, so the node
measures fixes itself: when an aircraft's position report names its next fix and the time it
will be there, the node looks up where that aircraft really was at that time (its own ADS-B
trail). Several flights agreeing = a learned position. Local only: nothing here leaves the node.

Pure logic, no Flask, no threads: datalink.py feeds it and runs it.
"""
import json
import logging
import math
import os
import re

MAX_PENDING = 500          # reports waiting for their ETA
MAX_SAMPLES = 20           # newest kept per fix name
MAX_NAMES = 2000
AHEAD_MAX_S = 300          # only ETAs at most 5 min ahead of the report (further = wind/speed drift)
RESOLVE_AFTER_S = 20       # look the position up this long after the ETA (trail points are 10 s apart)
CONFIRM_BEFORE_S = 120     # "passed N" may arrive a little before the predicted ETA ...
CONFIRM_AFTER_S = 900      # ... and up to 15 min after it
TRAIL_GAP_S = 30           # trail points used must be this close to the ETA
INLIER_NM = 2.0
MIN_WEIGHT = 3             # unconfirmed sample = 1, confirmed = 2
MIN_SHARE = 0.6            # inliers must be at least this share of the samples
SAVE_EVERY_S = 60
SAME_PASS_S = 900          # one airframe naming one fix within 15 min = one pass (revised ETAs, holds)
MIN_HEXES = 2              # a fix needs at least two airframes: one plane off its route must not move it
_NAME = re.compile(r'^[A-Z][A-Z0-9]{1,6}$')
_HEX = re.compile(r'^[0-9a-f]{6}$')
log = logging.getLogger('fix_learn')


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))


def interpolate(points, t, max_gap=TRAIL_GAP_S):
    """(lat, lon) at time t from trail points {lat, lon, t}, linear between the points either side.
    None when there is no point within max_gap on each side. Never extrapolates."""
    before = after = None
    for p in points or ():
        if not isinstance(p, dict) or not (_num(p.get('lat')) and _num(p.get('lon')) and _num(p.get('t'))):
            continue
        pt = p['t']
        if pt == t:
            return (float(p['lat']), float(p['lon']))
        if pt < t and t - pt <= max_gap and (before is None or pt > before['t']):
            before = p
        if pt > t and pt - t <= max_gap and (after is None or pt < after['t']):
            after = p
    if before is None or after is None:
        return None
    f = (t - before['t']) / float(after['t'] - before['t'])
    return (before['lat'] + (after['lat'] - before['lat']) * f, before['lon'] + (after['lon'] - before['lon']) * f)


def _median(v):
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def accept(samples):
    """samples: [[lat, lon, confirmed, ...], ...] -> {lat, lon, n, spread_nm, confirmed} or None."""
    pts = [(float(s[0]), float(s[1]), bool(s[2]), s[4] if len(s) > 4 else None)
           for s in samples or () if _num(s[0]) and _num(s[1])]
    if not pts:
        return None
    lons = [p[1] for p in pts]
    wrap = max(lons) - min(lons) > 180              # a fix on the dateline: work in 0..360
    if wrap:
        pts = [(la, lo + 360 if lo < 0 else lo, c, h) for la, lo, c, h in pts]
    mlat, mlon = _median([p[0] for p in pts]), _median([p[1] for p in pts])
    inl = [p for p in pts if nm(mlat, mlon, p[0], p[1]) <= INLIER_NM]
    weight = sum(2 if p[2] else 1 for p in inl)
    if weight < MIN_WEIGHT or len(inl) < MIN_SHARE * len(pts) or len({p[3] for p in inl}) < MIN_HEXES:
        return None
    lat = sum(p[0] for p in inl) / len(inl)
    lon = sum(p[1] for p in inl) / len(inl)
    spread = max(nm(lat, lon, p[0], p[1]) for p in inl)
    if lon > 180:
        lon -= 360
    return {'lat': round(lat, 5), 'lon': round(lon, 5), 'n': len(inl),
            'spread_nm': round(spread, 1), 'confirmed': sum(1 for p in inl if p[2])}


class FixLearner(object):
    """Not thread-safe on its own: the caller holds a lock (datalink.Store does)."""

    def __init__(self, path=None):
        self.path = path
        self._pending = {}          # (hex, name, eta) -> confirmed flag
        self._fixes = {}            # name -> [[lat, lon, confirmed, eta, hex], ...]
        self._dirty = False
        self._saved_at = None

    # ── input ────────────────────────────────────────────────────
    def note(self, hexc, name, eta, msg_ts):
        """A report said: next fix `name` at time `eta` (sent at msg_ts)."""
        if not (isinstance(name, str) and _NAME.match(name) and _num(eta) and _num(msg_ts)):
            return
        h = (hexc or '').lower() if isinstance(hexc, str) else ''
        if not _HEX.match(h):
            return
        if not (0 <= eta - msg_ts <= AHEAD_MAX_S):
            return
        if int(eta) % 60 == 0:      # minute-only ETA (HHMM) or exactly :00 - up to 60 s early, so skip it
            return
        key = (h, name, int(eta))
        if key in self._pending:
            return
        flag = False
        for k in [k for k in self._pending if k[0] == h and k[1] == name and abs(k[2] - key[2]) <= SAME_PASS_S]:
            flag = flag or self._pending.pop(k)         # newer report, shorter horizon: it replaces the older one
        if len(self._pending) >= MAX_PENDING:
            del self._pending[min(self._pending, key=lambda k: k[2])]
        self._pending[key] = flag

    def confirm(self, hexc, name, ts):
        """A report from hexc said it has passed `name` (last waypoint) at time ts."""
        if not (isinstance(name, str) and _NAME.match(name) and _num(ts)) or not isinstance(hexc, str):
            return
        h = hexc.lower()
        ok = lambda eta: eta - CONFIRM_BEFORE_S <= ts <= eta + CONFIRM_AFTER_S
        for key in self._pending:
            if key[0] == h and key[1] == name and ok(key[2]):
                self._pending[key] = True
        for s in self._fixes.get(name, ()):
            if s[4] == h and ok(s[3]) and not s[2]:
                s[2] = True
                self._dirty = True

    def due(self, now):
        """Pending reports whose ETA is RESOLVE_AFTER_S past: [(hex, name, eta), ...] oldest first."""
        return sorted((k for k in self._pending if now >= k[2] + RESOLVE_AFTER_S), key=lambda k: k[2])

    def apply(self, positions):
        """positions: {(hex, name, eta): (lat, lon) or None} for keys from due(). Returns samples added.
        A key with no position is dropped: the trail had no points either side of the ETA."""
        added = 0
        for key, pos in positions.items():
            if key not in self._pending:
                continue
            if pos and _num(pos[0]) and _num(pos[1]):
                self._add(key[1], [round(pos[0], 5), round(pos[1], 5), self._pending[key], key[2], key[0]])
                added += 1
            del self._pending[key]
        return added

    def resolve(self, trail_at, now):
        """due() + trail lookups + apply() in one go (for callers holding no other lock)."""
        positions = {}
        for key in self.due(now):
            try:
                positions[key] = trail_at(key[0], key[2])
            except Exception as e:                   # a broken trail lookup must not stop learning
                log.warning('[fix_learn] trail lookup failed: %s', e)
                positions[key] = None
        return self.apply(positions)

    def _add(self, name, sample):
        lst = self._fixes.setdefault(name, [])
        same = [s for s in lst if s[4] == sample[4] and abs(s[3] - sample[3]) <= SAME_PASS_S]
        if same:                                     # same airframe, same pass (it was late): keep the newer
            sample[2] = bool(sample[2] or any(s[2] for s in same))
            lst[:] = [s for s in lst if s not in same]
        lst.append(sample)
        lst.sort(key=lambda s: s[3])
        del lst[:-MAX_SAMPLES]
        while len(self._fixes) > MAX_NAMES:
            oldest = min(self._fixes, key=lambda n: self._fixes[n][-1][3])
            del self._fixes[oldest]
        self._dirty = True

    # ── output ───────────────────────────────────────────────────
    def learned(self):
        out = []
        for name in sorted(self._fixes):
            r = accept(self._fixes[name])
            if r:
                r['name'] = name
                out.append(r)
        return out

    def stats(self):
        return {'pending': len(self._pending), 'names': len(self._fixes)}

    # ── file ─────────────────────────────────────────────────────
    def load(self):
        self._fixes = {}
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            fixes = data.get('fixes') if isinstance(data, dict) else None
            for name, lst in (fixes or {}).items():
                if not (isinstance(name, str) and _NAME.match(name) and isinstance(lst, list)):
                    continue
                good = [[float(s[0]), float(s[1]), bool(s[2]), float(s[3]), str(s[4]).lower()] for s in lst
                        if isinstance(s, list) and len(s) == 5 and _num(s[0]) and _num(s[1]) and _num(s[3])
                        and abs(s[0]) <= 90 and abs(s[1]) <= 180 and _HEX.match(str(s[4]).lower())]
                if good:
                    self._fixes[name] = sorted(good, key=lambda s: s[3])[-MAX_SAMPLES:]
        except Exception as e:
            log.warning('[fix_learn] %s unreadable, starting empty: %s', self.path, e)
            self._fixes = {}

    def save(self, now, force=False):
        """Write the file if something changed and SAVE_EVERY_S has passed. True when written."""
        if not self.path or not (self._dirty or force):
            return False
        if not force and self._saved_at is not None and now - self._saved_at < SAVE_EVERY_S:
            return False
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'v': 1, 'fixes': self._fixes}, f, separators=(',', ':'))
        os.replace(tmp, self.path)
        self._dirty = False
        self._saved_at = now
        return True
