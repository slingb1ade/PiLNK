"""Turn ACARS messages into facts about the aircraft (Phase 1 d, 7 Oct 2026).

AJ's brief: not the messages themselves, but data that enhances PiLNK's ADS-B
picture: where the aircraft is going and from where, when it lands and on which
runway, its next waypoint, when it took off and landed, and the airport's ATIS.

decode(msg) takes one message as datalink.parse() returns it and gives back a
dict of facts, possibly empty. It never raises and it never guesses: a field is
only reported when it matches its expected shape, and the ARINC 702 (H1 FMS)
reports are only trusted when their CRC checks out.

Formats follow the open-source airframes.io decoder
(github.com/airframesio/acars-decoder-typescript, MIT License, Copyright (c)
2026 Airframes contributors). This is an independent Python implementation of
the same message layouts, checked against messages received on the hub.

Local only, like the rest of datalink: nothing here leaves the node.
"""
import calendar
import re
import time

_ICAO = re.compile(r'^[A-Z]{4}$')
_WP = re.compile(r'^[A-Z][A-Z0-9]{1,6}$')
_HHMMSS = re.compile(r'^([01]\d|2[0-3])([0-5]\d)([0-5]\d)$')
_HHMM = re.compile(r'^([01]\d|2[0-3])([0-5]\d)$')
_RWY = re.compile(r'^(0[1-9]|[12]\d|3[0-6])[LRC]?$')
_COORD = re.compile(r'^([NS])(\d{5})\s?([EW])(\d{6})$')
_H1_TYPES = ('FPN', 'FTX', 'INI', 'INR', 'LDI', 'PER', 'POS', 'PRG', 'PWI', 'WXR', 'REJ', 'REQ', 'RES', 'SUM')


# ── helpers ──────────────────────────────────────────────────────────────────
def _crc_x25(data):
    crc = 0xFFFF
    for b in data.encode('latin-1', errors='replace'):
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _crc_genibus(data):
    crc = 0xFFFF
    for b in data.encode('latin-1', errors='replace'):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def crc_ok(text):
    """ARINC 702 messages end in a 4-hex-digit CRC of everything before it.
    VDL2/ACARS: CRC-16/X25 with its four hex digits written in reverse order;
    GENIBUS as the fallback (Inmarsat-relayed)."""
    t = text.replace('\r', '').replace('\n', '')
    if len(t) < 6 or not re.match(r'^[0-9A-Fa-f]{4}$', t[-4:]):
        return False
    data, ck = t[:-4], t[-4:].upper()
    return ('%04X' % _crc_x25(data))[::-1] == ck or '%04X' % _crc_genibus(data) == ck


def _tod(s):
    """'HHMMSS' or 'HHMM' -> seconds since midnight, or None."""
    m = _HHMMSS.match(s or '')
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = _HHMM.match(s or '')
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    return None


def _near(tod, ts):
    """A UTC time of day -> the epoch second nearest the message time: within
    6 h before it or 18 h after (an ETA after midnight belongs to tomorrow)."""
    if tod is None:
        return None
    day = calendar.timegm(time.gmtime(ts)[:3] + (0, 0, 0))
    t = day + tod
    if t < ts - 6 * 3600:
        t += 86400
    elif t > ts + 18 * 3600:
        t -= 86400
    return t


def _dm(coord):
    """'S37068E173404' (degrees + decimal minutes) -> (lat, lon), or None."""
    m = _COORD.match((coord or '').strip())
    if not m:
        return None
    la, lo = int(m.group(2)), int(m.group(4))
    lat = la // 1000 + (la % 1000) / 10.0 / 60.0
    lon = lo // 1000 + (lo % 1000) / 10.0 / 60.0
    if m.group(1) == 'S':
        lat = -lat
    if m.group(3) == 'W':
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (la % 1000) >= 600 or (lo % 1000) >= 600:
        return None
    return round(lat, 5), round(lon, 5)


def _icao(s):
    s = (s or '').strip().upper()
    return s if _ICAO.match(s) else None


def _temp(s):
    m = re.match(r'^([MP+-]?)(\d{1,2})$', (s or '').strip())
    if not m:
        return None
    v = int(m.group(2))
    return -v if m.group(1) in ('M', '-') else v


# ── ARINC 702 (H1 FMS reports) ───────────────────────────────────────────────
def _pos_block(fields, ts, out):
    ll = _dm(fields[0]) if fields else None
    if not ll:
        return
    out['lat'], out['lon'] = ll
    # [1] last waypoint, [2] its time, [3] flight level, [4] next waypoint,
    # [5] its ETA, [6] the one after, [7] temperature. Each only if well formed.
    f = fields + [''] * 8
    if _WP.match(f[1]) and _tod(f[2]) is not None and f[3].isdigit() and _WP.match(f[4]) and _tod(f[5]) is not None:
        out['alt_ft'] = int(f[3]) * 100
        out['last_wp'] = f[1]
        out['next_wp'] = f[4]
        out['next_wp_eta_ts'] = _near(_tod(f[5]), ts)
        if _WP.match(f[6]):
            out['then_wp'] = f[6]
        t = _temp(f[7])
        if t is not None and -80 <= t <= 60:
            out['temp_c'] = t


def _arinc702(text, ts):
    t = text.replace('\r', '').replace('\n', '')
    if not crc_ok(t):
        return {}
    parts = t[:-4].split('/')
    head = parts[0]
    out = {}
    if head.startswith('POS') and len(head) > 3:
        out['kind'] = 'position'
        _pos_block(head[3:].split(','), ts, out)
        return out
    typ = head[:3]
    if typ not in _H1_TYPES:
        return {}
    out['kind'] = {'PRG': 'progress', 'FPN': 'flightplan', 'POS': 'position'}.get(typ, typ.lower())
    for p in parts[1:]:
        iei, data = p[:2], p[2:]
        if iei == 'DT':
            d = data.split(',')
            if len(d) >= 4:
                dest = _icao(d[0])
                if dest:
                    out['dest'] = dest
                    if _RWY.match(d[1]):
                        out['rwy'] = d[1]
                    eta = _near(_tod(d[3]), ts)
                    if eta:
                        out['eta_ts'] = eta
        elif iei == 'AF':
            d = data.split(',')
            if len(d) >= 2 and _icao(d[0]) and _icao(d[1]):
                out['orig'], out['dest'] = _icao(d[0]), _icao(d[1])
        elif iei == 'PS':
            _pos_block(data.split(','), ts, out)
        elif iei in ('RP', 'RF', 'RI', 'RM', 'RS', 'P:', ':D') or data.startswith(':DA:') or ':DA:' in p:
            kv = p.split(':')
            for i, k in enumerate(kv[:-1]):
                if k == 'DA' and _icao(kv[i + 1]):
                    out['orig'] = kv[i + 1]
                elif k == 'AA' and _icao(kv[i + 1]):
                    out['dest'] = kv[i + 1]
        elif iei == 'FN' and re.match(r'^[A-Z0-9]{2,8}$', data):
            out['flight'] = data
    return out


# ── OOOI ─────────────────────────────────────────────────────────────────────
_Q_EVENT = {'QP': 'out_ts', 'QQ': 'off_ts', 'QR': 'on_ts', 'QS': 'in_ts'}
_44_EVENT = {'OFF': 'off_ts', 'ON': 'on_ts', 'IN': 'in_ts'}


def _oooi_q(label, text, ts):
    t = text.strip()
    orig, dest, tod = _icao(t[0:4]), _icao(t[4:8]), _tod(t[8:12])
    if not (orig and dest and tod is not None):
        return {}
    return {'kind': 'oooi', 'orig': orig, 'dest': dest, _Q_EVENT[label]: _near(tod, ts)}


def _label44(text, ts):
    f = [x.strip() for x in text.strip().split(',')]
    m = re.match(r'^(?:00)?(OFF|ON|IN|ETA|POS)0[123]$', f[0]) if f else None
    if not m:
        return {}
    kind = m.group(1)
    out = {'kind': 'oooi' if kind in _44_EVENT else ('eta' if kind == 'ETA' else 'position')}
    if kind in _44_EVENT and len(f) >= 6:
        orig, dest = _icao(f[2]), _icao(f[3])
        if not (orig and dest):
            return {}
        out['orig'], out['dest'] = orig, dest
        when = _near(_tod(f[5]), ts)
        if when:
            out[_44_EVENT[kind]] = when
        if kind == 'OFF' and len(f) >= 7:
            eta = _near(_tod(f[6]), ts)
            if eta:
                out['eta_ts'] = eta
    elif kind == 'ETA' and len(f) >= 8:
        orig, dest = _icao(f[3]), _icao(f[4])
        if not (orig and dest):
            return {}
        out['orig'], out['dest'] = orig, dest
        if f[2].isdigit():
            out['alt_ft'] = int(f[2]) * 100
        eta = _near(_tod(f[7]), ts)
        if eta:
            out['eta_ts'] = eta
    elif kind == 'POS' and len(f) >= 5:
        orig, dest = _icao(f[3]), _icao(f[4])
        if orig and dest:
            out['orig'], out['dest'] = orig, dest
    else:
        return {}
    ll = _dm(f[1]) if len(f) > 1 else None
    if ll:
        out['lat'], out['lon'] = ll
    return out


# ── ATIS sent up to an aircraft ─────────────────────────────────────────────
_RWY_WORDS = {'LEFT': 'L', 'RIGHT': 'R', 'CENTRE': 'C', 'CENTER': 'C', 'L': 'L', 'R': 'R', 'C': 'C'}


def _atis(text):
    t = text.upper()
    m = re.search(r'\b([A-Z]{4}) (?:ARR |DEP |ARR/DEP )?ATIS ([A-Z])\b', t)
    m2 = None if m else re.search(r'\bATIS ([A-Z]{4}) ([A-Z])\b(?: (\d{4})\b)?', t)   # "ATIS NZAA Q 0142"
    if not (m or m2):
        return {}
    a = {'airport': (m or m2).group(1), 'letter': (m or m2).group(2)}
    if m2 and m2.group(3) and _HHMM.match(m2.group(3)):
        a['time'] = m2.group(3) + 'Z'
    m = re.search(r'^\s*APCH:\s*([A-Z][A-Z /-]{0,20}?)\s*$', t, re.M)
    if m:
        a['apch'] = m.group(1)
    m = re.search(r'\bRWY:?\s*(\d{2})\s*(LEFT|RIGHT|CENTRE|CENTER|[LRC])?\b', t)
    if m and _RWY.match(m.group(1)):
        a['rwy'] = m.group(1) + _RWY_WORDS.get(m.group(2) or '', '')
    m = re.search(r'\bQNH:?\s*(\d{3,4})\b', t)
    if m and 900 <= int(m.group(1)) <= 1100:
        a['qnh'] = int(m.group(1))
    m = re.search(r'\bWIND:?\s*(\d{3}|VRB)/(\d{1,3})KT', t)
    if m:
        d = None if m.group(1) == 'VRB' else int(m.group(1))
        if d is None or d <= 360:
            a['wind_dir'], a['wind_kt'] = d, int(m.group(2))
    m = re.search(r'^\s*(\d{4})Z\s*$', t, re.M)
    if m and _HHMM.match(m.group(1)):
        a['time'] = m.group(1) + 'Z'
    return {'kind': 'atis', 'atis': a}


# ── layouts heard on the hub (7 Oct 2026) ───────────────────────────────────
# Each one was seen in real traffic on 136.975; the tests use invented copies.
_OOOI_KEY = {'OUT': 'out_ts', 'OFF': 'off_ts', 'ON': 'on_ts', 'IN': 'in_ts'}
_GATE = re.compile(r'^\s*GATE\s+([A-Z]?\d{1,3}[A-Z]?)\s*$', re.M)
_SID = re.compile(r'^[A-Z]{2,6}\d[A-Z]?$')
_MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


def _close(when, ts, hours=3):
    """An event report is sent within minutes of the event: anything further
    from the message time than this is a misread, not a fact."""
    return when is not None and abs(when - ts) <= hours * 3600


def _poswx(text, ts):
    """Position/weather report: '3C01 POSWX  0144/07 NZAA/WMKK .9M-XXX' then
    '/POS S36.960/E174.008/OVR 0131' blocks and '/ALT 12553'."""
    t = text.replace('\r', '')
    m = re.match(r'^\w{4} POS\w*\s+\S+\s+([A-Z]{4})/([A-Z]{4})\b', t)
    if not m:
        return {}
    out = {'kind': 'position', 'orig': m.group(1), 'dest': m.group(2)}
    best = None
    for p in re.finditer(r'/POS ([NS])(\d{1,2}\.\d+)/([EW])(\d{1,3}\.\d+)/OVR (\d{4})', t):
        lat = float(p.group(2)) * (-1 if p.group(1) == 'S' else 1)
        lon = float(p.group(4)) * (-1 if p.group(3) == 'W' else 1)
        when = _near(_tod(p.group(5)), ts)
        if abs(lat) <= 90 and abs(lon) <= 180 and when is not None and (best is None or when > best[0]):
            best = (when, lat, lon)
    if best:
        out['lat'], out['lon'] = round(best[1], 5), round(best[2], 5)
    a = re.search(r'/ALT (\d{3,5})\b', t)
    if a and 0 < int(a.group(1)) <= 60000:
        out['alt_ft'] = int(a.group(1))
    return out


def _oooi_compact(text, ts):
    """'OFF030623/07070136NZAANZQN': event, flight, then HHMM, origin, destination."""
    m = re.match(r'^(OUT|OFF|ON|IN)0\d[A-Z0-9]{1,5}/\d{4}(\d{4})([A-Z]{4})([A-Z]{4})', text.strip())
    if not m:
        return {}
    when = _near(_tod(m.group(2)), ts)
    if not _close(when, ts):
        return {}
    return {'kind': 'oooi', 'orig': m.group(3), 'dest': m.group(4), _OOOI_KEY[m.group(1)]: when}


def _oooi_comma(text, ts):
    """'OFF,V01,CX 198 20261007 1,NZAA,VHHH,0132,0152,1231, 755':
    OFF gives pushback, takeoff and the estimated arrival time."""
    m = re.match(r'^(OUT|OFF|ON|IN),V\d{2},[^,]*,([A-Z]{4}),([A-Z]{4}),(\d{4}),(\d{4})(?:,(\d{4}))?', text.strip())
    if not m:
        return {}
    out = {'kind': 'oooi', 'orig': m.group(2), 'dest': m.group(3)}
    if m.group(1) == 'OFF':
        off = _near(_tod(m.group(5)), ts)
        if not _close(off, ts):
            return {}
        out['off_ts'] = off
        pb = _near(_tod(m.group(4)), ts)
        if _close(pb, ts):
            out['out_ts'] = pb
        eta = _near(_tod(m.group(6)), ts) if m.group(6) else None
        if eta and eta > off:
            out['eta_ts'] = eta
    return out


def _engine_report(text, reg):
    """Airbus engine/performance report header '/CCZK-OAB,OCT07,010659,NZQN,NZAA,0620/'.
    Only trusted when the registration in it is the aircraft that sent it."""
    m = re.search(r'/CC([A-Z0-9-]{3,8}),[A-Z]{3}\d{2},\d{6},([A-Z]{4}),([A-Z]{4}),', text)
    if not m:
        return {}
    if reg and m.group(1).replace('-', '') != reg.replace('-', '').lstrip('.'):
        return {}
    return {'kind': 'engine', 'orig': m.group(2), 'dest': m.group(3)}


def _pdc(text):
    """Pre-departure clearance sent up: 'NZAA PDC' then
    'ANZ551 CLRD TO NZCH OFF 23L VIA LEVRA2P'."""
    t = text.upper()
    o = re.search(r'\b([A-Z]{4}) PDC\b', t)
    c = re.search(r'\bCLRD TO ([A-Z]{4})\b', t)
    if not (o and c):
        return {}
    out = {'kind': 'clearance', 'orig': o.group(1), 'dest': c.group(1)}
    r = re.search(r'\bOFF (\d{2}[LRC]?)\b', t)
    if r and _RWY.match(r.group(1)):
        out['dep_rwy'] = r.group(1)
        s = re.search(r'\bOFF \S+ VIA ([A-Z0-9]{2,8})\b', t)
        if s and _SID.match(s.group(1)):
            out['sid'] = s.group(1)
    return out


def _arrival_info(text):
    """Airline ops 'ARRIVAL INFO' sent up: the gate."""
    t = text.upper().replace('\r', '')
    if 'ARRIVAL INFO' not in t:
        return {}
    g = _GATE.search(t)
    return {'kind': 'arrival', 'arr_gate': g.group(1)} if g else {}


def _schedule(text, ts):
    """'NZ948     07OCT26021000NZAANCRG...': the aircraft's next leg, sent up.
    Trusted only for today's date and a departure in the next 12 hours."""
    m = re.match(r'^[A-Z0-9]{2}\d{1,4}\s+(\d{2})([A-Z]{3})(\d{2})(\d{4})\d{2}([A-Z]{4})([A-Z]{4})', text.strip())
    if not m or m.group(2) not in _MONTHS:
        return {}
    g = time.gmtime(ts)
    if (int(m.group(1)), _MONTHS.index(m.group(2)) + 1, int(m.group(3))) != (g.tm_mday, g.tm_mon, g.tm_year % 100):
        return {}
    dep = _near(_tod(m.group(4)), ts)
    if dep is None or not (ts - 2 * 3600 <= dep <= ts + 12 * 3600):
        return {}
    return {'kind': 'schedule', 'orig': m.group(5), 'dest': m.group(6)}


# ── entry point ──────────────────────────────────────────────────────────────
def _route(label, text, ts, up, reg):
    if label in _Q_EVENT:
        return _oooi_q(label, text, ts)
    if label == '44':
        return _label44(text, ts)
    t = text.strip().upper()
    if re.match(r'^(OUT|OFF|ON|IN),', t):
        return _oooi_comma(text, ts)
    if re.match(r'^(OUT|OFF|ON|IN)0\d', t):
        return _oooi_compact(text, ts)
    if label == '80' and ' POS' in t[:20]:
        return _poswx(text, ts)
    if up:
        if ' PDC ' in t or 'CLRD TO' in t:
            return _pdc(text)
        if 'ARRIVAL INFO' in t:
            return _arrival_info(text)
        if label == '20':
            return _schedule(text, ts)
    if label == 'A9' or (up and re.search(r'\bATIS\b', t)):
        return _atis(text)
    if '/CC' in text and re.match(r'^A3\d\d,', t):
        return _engine_report(text, reg)
    if label in ('H1', '2P', '1M', '5Z', '4A', '80') or crc_ok(text):
        return _arinc702(text, ts)
    return {}


def decode(msg):
    """Facts from one message (see module doc). Always a dict; {} when nothing."""
    try:
        if not isinstance(msg, dict):
            return {}
        text = msg.get('text')
        if not isinstance(text, str) or not text.strip():
            return {}
        ts = msg.get('ts')
        ts = float(ts) if isinstance(ts, (int, float)) else time.time()
        label = str(msg.get('label') or '').upper()
        reg = str(msg.get('reg') or '').upper()
        out = _route(label, text, ts, msg.get('dir') == 'up', reg)
        return {k: v for k, v in out.items() if v is not None}
    except Exception:
        return {}
