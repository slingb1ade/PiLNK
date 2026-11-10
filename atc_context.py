"""ATC context feed: who is likely talking on a frequency (spec pilnk-tasks/atc-context-spec.md v2.2).

One pure function, build_context(snapshot, history, airport, profile, arrival_doc, freq=None),
used by the node's live route and by STT's replay. Standard library only: no file reads, no
clock, no network, nothing imported from PiLNK, so STT can load this file by path.
The module constants below are the spec's starting values; STT may tune them on its training
clips by setting them before calling (never on the locked 134).
Holds ADS-B and datalink facts only. Nothing built here is ever sent to pilnk.io.
"""
import math

TOWER_NM = 8.0
TOWER_FT = 3000
APP_NM = 40.0
APP_FT = 24500
RAMP_NM = 2.0
RAMP_FT = 1000.0
MATCH_KM = 4.0
TIE_KM = 1.0
ALT_MARGIN_FT = 700
LOOK_S = 300
KEEP_EVERY_S = 10
GS_WINDOW_S = 20
FLOOR_FROM_KM = 8.0
FLOOR_FT_PER_KM = 150
FLOOR_MAX_FT = 1600
NM_KM = 1.852


# ── geometry (the same formulas as static/arrival-paths.js) ──────────────────
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def km(lat1, lon1, lat2, lon2):
    r, p = 6371.0088, math.pi / 180
    dp, dl = (lat2 - lat1) * p, (lon2 - lon1) * p
    a = math.sin(dp / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def brg(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    y = math.sin((lon2 - lon1) * p) * math.cos(lat2 * p)
    x = math.cos(lat1 * p) * math.sin(lat2 * p) - math.sin(lat1 * p) * math.cos(lat2 * p) * math.cos((lon2 - lon1) * p)
    return (math.atan2(y, x) / p + 360) % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def km_to_line(lat, lon, line):
    """Distance (km) from a point to a polyline [[lat, lon, alt], ...], local flat projection."""
    if not line:
        return float('inf')
    kx, ky, best = 111.32 * math.cos(lat * math.pi / 180), 110.57, float('inf')
    for i in range(len(line)):
        ax, ay = (line[i][1] - lon) * kx, (line[i][0] - lat) * ky
        if i == len(line) - 1:
            best = min(best, math.hypot(ax, ay))
            break
        bx, by = (line[i + 1][1] - lon) * kx, (line[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / l2)) if l2 else 0.0
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _has_pos(a):
    return isinstance(a, dict) and _num(a.get('lat')) and _num(a.get('lon'))


# ── matching an aircraft across snapshots (§2.2) ─────────────────────────────
def _hex(a):
    h = a.get('hex') if isinstance(a, dict) else None
    return h.strip().lower() if isinstance(h, str) and h.strip() else None


def _cs(a):
    c = a.get('callsign') if isinstance(a, dict) else None
    return c.strip().upper() if isinstance(c, str) and c.strip() else None


def ac_key(ac):
    h = _hex(ac)
    if h:
        return ('hex', h)
    c = _cs(ac)
    return ('cs', c) if c else None


def index_history(history):
    """history snapshots -> {'hex': {hex: [(t, ac)]}, 'cs': {callsign: [(t, ac)]}}."""
    idx = {'hex': {}, 'cs': {}}
    for snap in history or []:
        if not isinstance(snap, dict) or not _num(snap.get('t')) or not isinstance(snap.get('aircraft'), list):
            continue
        for a in snap['aircraft']:
            if not isinstance(a, dict):
                continue
            h, c = _hex(a), _cs(a)
            if h:
                idx['hex'].setdefault(h, []).append((snap['t'], a))
            if c:
                idx['cs'].setdefault(c, []).append((snap['t'], a))
    return idx


def own_points(ac, t, idx):
    """This aircraft's history points plus the current one, oldest first.
    By hex when both sides have it, otherwise by callsign."""
    h, c = _hex(ac), _cs(ac)
    pts = list(idx['hex'].get(h, [])) if h else []
    if c:
        pts += [(tt, a) for tt, a in idx['cs'].get(c, []) if not (h and _hex(a))]
    seen, out = set(), []
    for tt, a in sorted(pts, key=lambda p: p[0]):
        if tt < t and tt not in seen:
            seen.add(tt)
            out.append((tt, a))
    out.append((t, ac))
    return out


def derive_gs(points, t):
    """Ground speed (kt) from this aircraft's own positions over the last 20 s, else None."""
    if not points or not _has_pos(points[-1][1]):
        return None
    cur = points[-1][1]
    for tt, a in points[:-1]:
        if t - GS_WINDOW_S <= tt < t and t - tt >= 4 and _has_pos(a):
            return km(a['lat'], a['lon'], cur['lat'], cur['lon']) / (t - tt) * 3600 / NM_KM
    return None


def track5(points, t):
    """Last 5 minutes as [{'lat','lon','alt','t'}], one point per 10 s, the current point last."""
    out = []
    for tt, a in points:
        if tt < t - LOOK_S or not _has_pos(a):
            continue
        p = {'lat': a['lat'], 'lon': a['lon'], 'alt': a['alt_baro'] if _num(a.get('alt_baro')) else None, 't': tt}
        if not out or tt - out[-1]['t'] >= KEEP_EVERY_S:
            out.append(p)
        elif tt == t:
            out[-1] = p
    return out


# ── the dashboard's rules, ported from static/arrival-paths.js ──────────────
def too_low_for_arrival(ref, lat, lon, alt):
    if not _num(alt) or not _num(lat) or not _num(lon) or not ref:
        return False
    d = km(ref[0], ref[1], lat, lon)
    if d <= FLOOR_FROM_KM:
        return False
    return alt < min(FLOOR_MAX_FT, (d - FLOOR_FROM_KM) * FLOOR_FT_PER_KM)


def is_arriving(track, ref):
    if not track or len(track) < 3 or not ref:
        return False
    a, b = track[0], track[-1]
    d_a, d_b = km(ref[0], ref[1], a['lat'], a['lon']), km(ref[0], ref[1], b['lat'], b['lon'])
    if not (d_b < 160 and d_b < d_a - 1):
        return False
    if a['alt'] is None or b['alt'] is None or b['alt'] > 20000 or a['alt'] - b['alt'] < 300:
        return False
    if too_low_for_arrival(ref, b['lat'], b['lon'], b['alt']):
        return False
    return ang_diff(brg(a['lat'], a['lon'], b['lat'], b['lon']), brg(b['lat'], b['lon'], ref[0], ref[1])) <= 45


def final_rwy(ac, ref, rwy_ends):
    """Short final: inside 8 km, below 2,000 ft, descending, lined up -> runway name, else None."""
    if not ref or not _has_pos(ac) or not _num(ac.get('alt_baro')):
        return None
    if ac['alt_baro'] >= 2000 or km(ref[0], ref[1], ac['lat'], ac['lon']) > 8:
        return None
    if not (_num(ac.get('baro_rate')) and ac['baro_rate'] < -300) or not _num(ac.get('track')):
        return None
    for e in rwy_ends or []:
        if _num(e.get('hdg')) and ang_diff(ac['track'], e['hdg']) <= 25:
            return e.get('name')
    return None


def _nearest_vertex_alt(line, lat, lon):
    best, alt = float('inf'), None
    for v in line:
        if len(v) < 3 or not _num(v[2]):
            continue
        d = km(lat, lon, v[0], v[1])
        if d < best:
            best, alt = d, v[2]
    return alt


def _alt_err(track, path):
    s, n = 0.0, 0
    for q in track:
        if q['alt'] is None:
            continue
        pa = _nearest_vertex_alt(path['line'], q['lat'], q['lon'])
        if pa is None:
            continue
        s, n = s + abs(q['alt'] - pa), n + 1
    return s / n if n else None


def match_path(track, paths, rwy=None):
    """matchAll + pick: the learned path under 4 km mean distance; a tie within 1 km goes to
    the clearly better height (700 ft), else to the path more flights use."""
    if not track or len(track) < 3 or not paths:
        return None
    cands = []
    for p in paths:
        if not isinstance(p, dict) or not isinstance(p.get('line'), list) or not p['line']:
            continue
        if rwy and p.get('rwy') != rwy:
            continue
        budget, s = MATCH_KM * len(track), 0.0
        for q in track:                 # stop once this path can no longer be under MATCH_KM on average
            s += km_to_line(q['lat'], q['lon'], p['line'])
            if s >= budget:
                break
        if s < budget:
            cands.append((s / len(track), p))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    tied = [c for c in cands if c[0] <= cands[0][0] + TIE_KM]
    if len(tied) == 1:
        return tied[0][1]
    by_alt = sorted(((e, c) for e, c in ((_alt_err(track, c[1]), c) for c in tied) if e is not None),
                    key=lambda x: x[0])
    if len(by_alt) >= 2 and by_alt[1][0] - by_alt[0][0] >= ALT_MARGIN_FT:
        return by_alt[0][1][1]
    tied.sort(key=lambda c: (-(c[1]['flights'] if _num(c[1].get('flights')) else 0), c[0]))
    return tied[0][1]


def along(line, lat, lon):
    """Where a point sits along a polyline: segment index + fraction of its closest point (flat projection)."""
    if not line:
        return None
    if len(line) == 1:
        return 0.0
    kx, ky = 111.32 * math.cos(lat * math.pi / 180), 110.57
    best, at = float('inf'), 0.0
    for i in range(len(line) - 1):
        ax, ay = (line[i][1] - lon) * kx, (line[i][0] - lat) * ky
        bx, by = (line[i + 1][1] - lon) * kx, (line[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / l2)) if l2 else 0.0
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best:
            best, at = d, i + t
    return at


def fixes_ahead(path, lat, lon):
    """Names in path['fix_pts'] still ahead of (lat, lon) along the path's line, in flying order;
    None when the path document carries no fix positions (never guessed from names alone)."""
    fp, line = path.get('fix_pts'), path.get('line')
    if not isinstance(fp, list) or not isinstance(line, list) or not line:
        return None
    here = along(line, lat, lon)
    return [f[0] for f in fp
            if isinstance(f, list) and len(f) == 3 and isinstance(f[0], str) and _num(f[1]) and _num(f[2])
            and along(line, f[1], f[2]) > here]


def opening(points, t, ref):
    """Moving away: 0.1 km further out than 60 s ago, else track within 90 deg of outbound."""
    cur = points[-1][1] if points else None
    if not ref or not _has_pos(cur):
        return False
    d_now = km(ref[0], ref[1], cur['lat'], cur['lon'])
    for tt, a in points[:-1]:
        if t - 60 <= tt < t and _has_pos(a):
            return d_now - km(ref[0], ref[1], a['lat'], a['lon']) >= 0.1
    if _num(cur.get('track')):
        return ang_diff(cur['track'], brg(ref[0], ref[1], cur['lat'], cur['lon'])) <= 90
    return False


def _clean_line(line):
    """A path line keeps only its [lat, lon, ...] points with numeric lat/lon; none left -> None."""
    if not isinstance(line, list):
        return None
    pts = [v for v in line if isinstance(v, (list, tuple)) and len(v) >= 2 and _num(v[0]) and _num(v[1])]
    return pts or None


def paths_for(arrival_doc, airport):
    """The learned paths, only when the document is for this airport. The document comes over the
    network, so a path keeps only its well-formed line points; a path left without a line is dropped."""
    if not isinstance(arrival_doc, dict) or not isinstance(airport, dict):
        return []
    if str(arrival_doc.get('apt') or '').upper() != str(airport.get('icao') or '').upper():
        return []
    paths = arrival_doc.get('paths')
    out = []
    for p in paths if isinstance(paths, list) else []:
        line = _clean_line(p.get('line')) if isinstance(p, dict) else None
        if line is not None:
            q = dict(p)
            q['line'] = line
            out.append(q)
    return out


# ── phase of flight (§2.4) ───────────────────────────────────────────────────
def phase_of(ac, points, t, airport, rwy_ends, paths):
    """-> (phase, extras {dist_nm, gs, final_rwy, path})."""
    alt = ac.get('alt_baro')
    altn = alt if _num(alt) else None
    ref = airport.get('ref') if isinstance(airport, dict) else None
    ref = ref if isinstance(ref, (list, tuple)) and len(ref) == 2 and all(_num(x) for x in ref) else None
    dist_km = km(ref[0], ref[1], ac['lat'], ac['lon']) if ref and _has_pos(ac) else None
    dist_nm = dist_km / NM_KM if dist_km is not None else None
    gs = ac.get('gs') if _num(ac.get('gs')) else derive_gs(points, t)
    vs = ac.get('baro_rate') if _num(ac.get('baro_rate')) else None
    ex = {'dist_nm': dist_nm, 'gs': gs, 'final_rwy': None, 'path': None}
    if alt == 'ground':
        return 'ground', ex
    if altn is None:
        if dist_nm is not None and gs is not None and gs < 40 and dist_nm <= 3:
            return 'ground', ex
        return 'unknown', ex
    if dist_nm is None:
        return ('enroute' if altn >= 15000 else 'overflight'), ex
    ex['final_rwy'] = final_rwy(ac, ref, rwy_ends)
    if ex['final_rwy']:
        return 'final', ex
    out = None
    if dist_nm <= 5 and altn < 3000 and vs is not None and vs > 500:
        out = opening(points, t, ref)
        if out:
            return 'takeoff', ex
    trk = track5(points, t)
    if is_arriving(trk, ref):
        rwy_hint = ac.get('dl_rwy') if isinstance(ac.get('dl_rwy'), str) and ac.get('dl_rwy') else None
        ex['path'] = match_path(trk, paths, rwy_hint) if paths else None
        if dist_nm <= 20 or altn < 6000 or ex['path']:
            return 'approach', ex
        return 'arrival', ex
    if vs is not None and vs > 300 and dist_nm <= 40 and altn < 15000:
        if out is None:
            out = opening(points, t, ref)
        if out:
            return 'departure', ex
    if too_low_for_arrival(ref, ac.get('lat'), ac.get('lon'), altn):
        return 'local', ex
    if altn >= 15000 or (vs is not None and abs(vs) <= 300 and dist_nm > 40):
        return 'enroute', ex
    return 'overflight', ex


# ── airport and profile inputs (§2.3) ────────────────────────────────────────
def _float(v):
    """A number, or a numeric string, as a finite float; anything else None. Never a bool."""
    if isinstance(v, bool):
        return None
    if _num(v):
        return float(v)
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def norm_profile(p):
    """STT's fixes/<ICAO>.json -> only the valid parts of groups, frequencies, lat, lon, runways."""
    if not isinstance(p, dict):
        return None
    out = {}
    fr = p.get('frequencies')
    if isinstance(fr, dict):
        f2 = {}
        for svc, vals in fr.items():
            if isinstance(svc, str) and isinstance(vals, list):
                nums = [x for x in (_float(v) for v in vals) if x is not None]
                if nums:
                    f2[svc] = nums
        if f2:
            out['frequencies'] = f2
    gr = p.get('groups')
    if isinstance(gr, list):
        g2 = [[g[0], g[1], [f for f in g[2] if isinstance(f, str)]] for g in gr
              if isinstance(g, list) and len(g) == 3 and isinstance(g[0], str) and isinstance(g[1], str)
              and isinstance(g[2], list)]
        if g2:
            out['groups'] = g2
    for k in ('lat', 'lon'):
        if _num(p.get(k)):
            out[k] = float(p[k])
    rw = p.get('runways')
    if isinstance(rw, list):
        r2 = [r for r in rw if isinstance(r, str)]
        if r2:
            out['runways'] = r2
    return out or None


def airport_from_runways(doc, icao=None):
    """pilnk.io /api/runways.php answer -> {icao, ref, elev_ft, runways: [{name, lat, lon, hdg}]}."""
    aps = doc.get('airports') if isinstance(doc, dict) else None
    if not isinstance(aps, list):
        return None
    aps = [a for a in aps if isinstance(a, dict)]
    if icao:
        aps = [a for a in aps if str(a.get('icao') or '').upper() == str(icao).upper()]
    if not aps or not (_num(aps[0].get('lat')) and _num(aps[0].get('lon'))):
        return None
    a = aps[0]
    ends, elev = [], None
    for r in a.get('runways') or []:
        if not isinstance(r, dict):
            continue
        for s in ('le', 'he'):
            name, hdg = r.get(s + '_ident'), r.get(s + '_hdg')
            if isinstance(name, str) and name and _num(hdg) and _num(r.get(s + '_lat')) and _num(r.get(s + '_lon')):
                ends.append({'name': name, 'lat': float(r[s + '_lat']), 'lon': float(r[s + '_lon']), 'hdg': float(hdg)})
            if elev is None and _num(r.get(s + '_elev_ft')):
                elev = r[s + '_elev_ft']
    return {'icao': str(a.get('icao')).upper(), 'ref': [float(a['lat']), float(a['lon'])], 'elev_ft': elev,
            'runways': ends}


def _airport(a):
    if not isinstance(a, dict) or not isinstance(a.get('icao'), str):
        return None
    ref = a.get('ref')
    if not (isinstance(ref, (list, tuple)) and len(ref) == 2 and all(_num(x) for x in ref)):
        return None
    return a


def _rwy_ends(airport, prof):
    rw = airport.get('runways') if airport else None
    ends = [e for e in rw if isinstance(e, dict) and isinstance(e.get('name'), str)
            and _num(e.get('hdg'))] if isinstance(rw, list) else []
    names = set(prof.get('runways') or []) if prof else set()
    sel = [e for e in ends if e['name'] in names]
    return sel or ends


# ── service, frequencies, score, why (§2.5) ──────────────────────────────────
def freq_match(freqs, f):
    return _num(f) and any(_num(x) and abs(x - f) <= 0.005 for x in freqs or [])


def service_of(phase, dist_nm, alt):
    if phase == 'unknown':
        return None
    if phase == 'ground':
        return 'ground'
    if dist_nm is None or alt is None:
        return 'control'
    if phase in ('final', 'takeoff') or (dist_nm <= TOWER_NM and alt <= TOWER_FT):
        return 'tower'
    if phase in ('approach', 'arrival', 'departure') and dist_nm <= APP_NM and alt <= APP_FT:
        return 'approach'
    return 'control'


def score_of(phase, service, dist_nm, alt):
    """0..1, lower near the boundary where hand-offs happen (ramps of RAMP_NM / RAMP_FT)."""
    if phase == 'unknown' or service is None:
        return 0.0
    if phase == 'ground':
        return 1.0
    if dist_nm is None or alt is None:
        return 0.5

    def out(r_nm, c_ft):
        return max((dist_nm - r_nm) / RAMP_NM, (alt - c_ft) / RAMP_FT)

    if service == 'tower':
        s = -out(TOWER_NM, TOWER_FT)
    elif service == 'approach':
        s = min(-out(APP_NM, APP_FT), out(TOWER_NM, TOWER_FT))
    else:
        o = out(APP_NM, APP_FT)
        s = o if o > 0 else 0.5
    return round(max(0.1, min(1.0, s)), 2)


_TAGS = {'approach': 'closing', 'arrival': 'closing', 'takeoff': 'outbound', 'departure': 'outbound',
         'local': 'below arrival floor'}


def why_of(phase, ac, extras):
    if phase == 'unknown':
        return 'no altitude'
    parts = []
    vs = ac.get('baro_rate')
    if _num(vs):
        n = int(round(abs(vs)))
        parts.append('level' if abs(vs) <= 300 else ('desc %dfpm' % n if vs < 0 else 'climb %dfpm' % n))
    d = extras.get('dist_nm')
    if d is not None:
        parts.append('%.1fnm' % d if d < 10 else '%.0fnm' % d)
    if phase == 'final':
        parts.append('lined up %s' % extras.get('final_rwy'))
    elif phase in _TAGS:
        parts.append(_TAGS[phase])
    elif phase == 'enroute' and _num(ac.get('alt_baro')):
        parts.append('FL%03d' % int(ac['alt_baro'] // 100))
    elif phase == 'ground':
        gs = extras.get('gs')
        parts.append('slow %.0fkt' % gs if ac.get('alt_baro') != 'ground' and gs is not None else 'on ground')
    return ', '.join(parts)


# ── what the pilot has dialled in (§2.6) ─────────────────────────────────────
SEL_FIELDS = (('alt', 'nav_altitude_mcp', 'alt_ft', 50), ('hdg', 'nav_heading', 'hdg', 2), ('qnh', 'nav_qnh', 'qnh', 0.5))
MERGE_S = 10


def _sel_diff(what, a, b):
    return ang_diff(a, b) if what == 'hdg' else abs(a - b)


def selected_and_changes(points):
    """(t, ac) points, oldest first -> ({alt_ft, hdg, qnh}, [{t, what, from, to}]).
    Selected = newest known value. A change is a new value at least 50 ft / 2 deg / 0.5 hPa from the
    last accepted one; changes of one field within 10 s of each other are one change (a knob turning),
    and a turn that ends where it started is none."""
    sel = {'alt_ft': None, 'hdg': None, 'qnh': None}
    changes = []
    for what, key, out, tol in SEL_FIELDS:
        prev, mine = None, []
        for tt, a in points:
            v = a.get(key) if isinstance(a, dict) else None
            if not _num(v):
                continue
            sel[out] = v
            if prev is None:
                prev = v
                continue
            if _sel_diff(what, v, prev) < tol:
                continue
            if mine and tt - mine[-1]['_last'] <= MERGE_S:
                mine[-1]['to'], mine[-1]['_last'] = v, tt
            else:
                mine.append({'t': tt, 'what': what, 'from': prev, 'to': v, '_last': tt})
            prev = v
        for c in mine:
            if _sel_diff(what, c['to'], c['from']) >= tol:
                changes.append({k: c[k] for k in ('t', 'what', 'from', 'to')})
    changes.sort(key=lambda c: c['t'])
    return sel, changes


# ── the one entry point (§2.1, response §2.7) ────────────────────────────────
def _int(v):
    return int(round(v)) if _num(v) else None


def build_context(snapshot, history, airport, profile, arrival_doc, freq=None):
    snap = snapshot if isinstance(snapshot, dict) else {}
    t = snap.get('t') if _num(snap.get('t')) else None
    freq = float(freq) if _num(freq) else None
    apt = _airport(airport)
    prof = norm_profile(profile)
    freqs_by = prof.get('frequencies', {}) if prof else {}
    ends = _rwy_ends(apt, prof)
    paths = paths_for(arrival_doc, apt)
    idx = index_history(history if t is not None else [])
    rows = []
    for a in snap.get('aircraft') if isinstance(snap.get('aircraft'), list) else []:
        if not isinstance(a, dict) or ac_key(a) is None:
            continue
        tt = t if t is not None else 0
        pts = own_points(a, tt, idx)
        ph, ex = phase_of(a, pts, tt, apt, ends, paths)
        altn = a.get('alt_baro') if _num(a.get('alt_baro')) else None
        svc = service_of(ph, ex['dist_nm'], altn)
        dl_rwy = a.get('dl_rwy') if isinstance(a.get('dl_rwy'), str) and a.get('dl_rwy').strip() else None
        path = ex['path']
        sel, changes = selected_and_changes(pts)
        rows.append({
            'callsign': _cs(a), 'hex': _hex(a), 'phase': ph, 'why': why_of(ph, a, ex), 'service': svc,
            'freqs': list(freqs_by.get(svc, [])) if svc else [],
            'score': score_of(ph, svc, ex['dist_nm'], altn),
            'alt_ft': _int(altn), 'vs_fpm': _int(a.get('baro_rate')), 'gs_kt': _int(ex['gs']),
            'track': a.get('track') if _num(a.get('track')) else None,
            'dist_nm': round(ex['dist_nm'], 1) if ex['dist_nm'] is not None else None,
            'rwy': ex['final_rwy'] or dl_rwy or (path.get('rwy') if path else None),
            'selected': sel, 'changes': changes,
            'path': {'name': path.get('name'), 'fixes_ahead': fixes_ahead(path, a['lat'], a['lon'])} if path else None,
            'next_fix': a['dl_next_fix'].strip().upper() if isinstance(a.get('dl_next_fix'), str) and a['dl_next_fix'].strip() else None,
        })
    if freq is not None and freqs_by:
        rows = [r for r in rows if freq_match(r['freqs'], freq)]
    rows.sort(key=lambda r: (-r['score'], r['dist_nm'] if r['dist_nm'] is not None else float('inf'),
                             r['callsign'] or ''))
    return {'t': t, 'airport': apt['icao'] if apt else None, 'freq': freq, 'profile': prof is not None,
            'aircraft': rows}
