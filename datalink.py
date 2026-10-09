"""PiLNK datalink ingest (ACARS over VDL2, Phase 1 c — 7 Oct 2026).

dumpvdl2 (pilnk-datalink.service) decodes VDL2 on the node's own datalink stick
and sends one JSON object per message to 127.0.0.1:<udp_port>. This module
listens there, keeps the last few messages per aircraft in memory, and gives
app.py three things:

  merge(ac, hex, reg)  adds a SMALL summary ({"n", "last_label", "last_ts"}) to
                       an aircraft in /flights, so the payload stays light;
  messages(hex, reg)   the full recent list, for the aircraft card;
  status(...)          installed / decoder running / driver_ok / msgs per hour.

HARD RULE (AJ, 27 Sep 2026): datalink never leaves the node. This module binds
127.0.0.1 only, opens no outbound connection, and nothing here is added to the
pilnk.io ping. Every field is untrusted text from the air: the dashboard must
render it with textContent, never innerHTML.

No Flask at import time (register() imports it lazily); app.py wires it in. Nothing runs unless
/etc/pilnk-datalink/config.json exists, so nodes without a datalink stick see
no thread, no socket and no change in /flights.
"""
import collections
import json
import logging
import re
import socket
import threading
import time

try:                                  # message -> facts (route, ETA, runway, ATIS ...)
    import datalink_decode as _decode
except Exception:                     # a missing decoder must never stop the ingest
    _decode = None

CFG_PATH = '/etc/pilnk-datalink/config.json'
STATUS_PATH = '/run/pilnk-datalink/status.json'
BIND_HOST = '127.0.0.1'          # never 0.0.0.0: local only
PER_AIRCRAFT = 20                # messages kept per aircraft
TTL = 30 * 60                    # seconds a message stays on the card
MAX_AIRCRAFT = 500               # bound memory: oldest aircraft evicted past this
MAX_TEXT = 2000                  # characters of message text kept
_HEX_RE = re.compile(r'^[0-9A-F]{6}$')
_ICAO_RE = re.compile(r'^[A-Z]{4}$')
_NOT_AIRCRAFT = {'FFFFFF', '000000'}   # VDL2 broadcast / null addresses, never a real airframe
_REG_DROP = re.compile(r'[^A-Z0-9-]')
# How long a decoded fact stays on the aircraft. The route outlives the
# 30-minute message list (one progress report can name the destination an hour
# out); the fast-changing ones do not.
ROUTE_FACTS = ('orig', 'dest', 'flight', 'out_ts', 'off_ts', 'on_ts', 'in_ts', 'arr_gate')
ROUTE_TTL = 3 * 3600
FAST_FACTS = ('eta_ts', 'rwy', 'next_wp', 'next_wp_eta_ts', 'last_wp', 'then_wp',
              'alt_ft', 'temp_c', 'lat', 'lon')
FAST_TTL = 30 * 60
ARRIVAL_FACTS = ('eta_ts', 'rwy', 'next_wp', 'next_wp_eta_ts', 'then_wp')   # void once landed
DEPART_FACTS = ('dep_rwy', 'sid')                # from the departure clearance
DEPART_TTL = 90 * 60
ATIS_TTL = 2 * 3600
MAX_FACT_KEYS = 1000
BOARD_DROP = 45 * 60            # a landed / departed flight leaves the board after this
BOARD_MAX = 30                  # rows per list
AIRBORNE_FACTS = ('alt_ft', 'next_wp', 'lat', 'lon', 'eta_ts')   # only reported once flying
_FLIGHT_RE = re.compile(r'^[A-Z0-9]{2,8}$')
_FLIGHT_ZEROS = re.compile(r'^([A-Z0-9]{2})0*([0-9]+)$')
log = logging.getLogger('datalink')


def norm_flight(s):
    """'NZ0620' -> 'NZ620'; 'ANZ481' stays; anything not shaped like a flight -> None."""
    if not isinstance(s, str):
        return None
    s = s.strip().upper()
    m = _FLIGHT_ZEROS.match(s)
    if m:
        s = m.group(1) + m.group(2)
    return s if _FLIGHT_RE.match(s) else None


def _s(v, cap):
    """A field as a plain string: str/int/float only, anything else is ''."""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return ''
    return str(v).strip()[:cap]


def split_datagram(data):
    """One UDP datagram -> list of JSON texts (normally one; tolerate several per line)."""
    text = data.decode('utf-8', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data)
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse(obj, now=None):
    """One dumpvdl2 JSON message -> normalised dict, or None if it is not ACARS.

    The aircraft is whichever AVLC end is of type Aircraft: the source for a
    downlink (aircraft -> ground), the destination for an uplink.
    """
    try:
        if isinstance(obj, (bytes, bytearray, str)):
            obj = json.loads(obj)
        v = obj.get('vdl2') if isinstance(obj, dict) else None
        if not isinstance(v, dict):
            return None
        avlc = v.get('avlc')
        if not isinstance(avlc, dict):
            return None
        acars = avlc.get('acars')
        if not isinstance(acars, dict):
            return None
        src = avlc.get('src') if isinstance(avlc.get('src'), dict) else {}
        dst = avlc.get('dst') if isinstance(avlc.get('dst'), dict) else {}
        hexc, direction = '', ''
        if str(src.get('type', '')).lower().startswith('aircraft'):
            hexc, direction = _s(src.get('addr'), 8).upper(), 'down'
        elif str(dst.get('type', '')).lower().startswith('aircraft'):
            hexc, direction = _s(dst.get('addr'), 8).upper(), 'up'
        if not _HEX_RE.match(hexc) or hexc in _NOT_AIRCRAFT:
            hexc = ''
        t = v.get('t')
        ts = None
        if isinstance(t, dict) and isinstance(t.get('sec'), (int, float)):
            ts = float(t['sec']) + float(t.get('usec') or 0) / 1e6
        if ts is None:
            ts = time.time() if now is None else float(now)
        reg = _REG_DROP.sub('', _s(acars.get('reg'), 20).lstrip('.').upper())[:10]
        text = acars.get('msg_text') if isinstance(acars.get('msg_text'), str) else ''
        # ground systems send CRLF; one newline convention for the card
        text = text.replace('\r\n', '\n').replace('\r', '\n')[:MAX_TEXT]
        freq = v.get('freq')
        sig = v.get('sig_level')
        return {
            'ts': ts,
            'hex': hexc,
            'reg': reg,
            'flight': _s(acars.get('flight'), 10),
            'label': _s(acars.get('label'), 4),
            'sublabel': _s(acars.get('sublabel'), 4),
            'msg_num': _s(acars.get('msg_num'), 8),
            'text': text,
            'dir': direction,
            'freq': round(freq / 1e6, 3) if isinstance(freq, (int, float)) else None,
            'sig': round(float(sig), 1) if isinstance(sig, (int, float)) and not isinstance(sig, bool) else None,
        }
    except Exception:
        return None


class Store(object):
    """Recent messages per aircraft, thread-safe, bounded in count and age."""

    def __init__(self, now=None):
        self._lock = threading.Lock()
        # when this store began counting: a restart starts it again from empty,
        # so readers can tell a fresh store from a quiet decoder (AJ, 9 Oct)
        self.since = time.time() if now is None else float(now)
        self._by_key = collections.OrderedDict()   # 'C81E2A' or '~ZK-MCJ' -> deque
        self._reg_hex = {}                          # 'ZK-MCJ' -> 'C81E2A'
        self._times = collections.deque(maxlen=20000)
        self._frames = collections.deque(maxlen=50000)   # every VDL2 frame: decoder is alive
        self._seen = collections.OrderedDict()             # dedupe key -> ts
        self._facts = collections.OrderedDict()            # key -> {field: (value, msg ts)}
        self._atis = {}                                    # airport -> dict incl. ts
        self._legs = {}                                    # key -> ts the current flight leg began
        self.last_frame_ts = None
        self.dupes = 0
        self.total = 0
        self.bad = 0
        self.last_ts = None

    def note_bad(self):
        with self._lock:
            self.bad += 1

    def note_frame(self, ts):
        """Any decoded VDL2 frame (link chatter included): proves the decoder hears traffic."""
        with self._lock:
            self._frames.append(ts)
            if self.last_frame_ts is None or ts > self.last_frame_ts:
                self.last_frame_ts = ts

    def _key(self, hexc, reg):
        if hexc:
            return hexc
        if reg and reg in self._reg_hex:
            return self._reg_hex[reg]
        return ('~' + reg) if reg else None

    def add(self, msg):
        with self._lock:
            # A block the ground did not acknowledge in time is sent again with the
            # same message number and text (seen in the first hub capture, 45 s
            # apart). One message on the card, not two.
            dk = (msg.get('hex') or msg.get('reg') or '', msg.get('label', ''),
                  msg.get('msg_num', ''), msg.get('text', ''))
            prev = self._seen.get(dk)
            if prev is not None and abs(msg['ts'] - prev) < TTL:
                self.dupes += 1
                return
            self._seen[dk] = msg['ts']
            self._seen.move_to_end(dk)
            while len(self._seen) > 5000:
                self._seen.popitem(last=False)
            self.total += 1
            self._times.append(msg['ts'])
            if self.last_ts is None or msg['ts'] > self.last_ts:
                self.last_ts = msg['ts']
            hexc, reg = msg.get('hex') or '', msg.get('reg') or ''
            if hexc and reg:
                self._reg_hex[reg] = hexc
                orphan = self._by_key.pop('~' + reg, None)       # fold reg-only messages in
                if orphan:
                    self._by_key.setdefault(hexc, collections.deque(maxlen=PER_AIRCRAFT)).extend(orphan)
            if hexc and reg:
                ofacts = self._facts.pop('~' + reg, None)
                self._legs.pop('~' + reg, None)
                if ofacts:
                    self._merge_facts(hexc, ofacts)
            key = self._key(hexc, reg)
            facts = {}
            if _decode is not None:
                facts = _decode.decode(msg)
            atis = facts.pop('atis', None)
            if isinstance(atis, dict) and atis.get('airport'):
                cur = self._atis.get(atis['airport'])
                if cur is None or cur.get('ts', 0) <= msg['ts']:
                    self._atis[atis['airport']] = dict(atis, ts=msg['ts'])
            facts.pop('kind', None)
            if key is None:
                return
            if facts:
                self._merge_facts(key, {k: (v, msg['ts']) for k, v in facts.items()})
            dq = self._by_key.setdefault(key, collections.deque(maxlen=PER_AIRCRAFT))
            dq.append(dict(msg))
            self._by_key.move_to_end(key)
            while len(self._by_key) > MAX_AIRCRAFT:
                old, _ = self._by_key.popitem(last=False)
                if not old.startswith('~'):
                    for r in [r for r, h in self._reg_hex.items() if h == old]:
                        del self._reg_hex[r]

    def _merge_facts(self, key, new):
        """Fold {field: (value, ts)} into an aircraft's facts; a newer message wins.

        A newer message naming a different origin or destination starts a new
        flight leg: every fact older than it belonged to the last leg and goes,
        and late copies of old-leg messages are not let back in."""
        rec = self._facts.setdefault(key, {})
        leg = self._legs.get(key, 0)
        for f in ('orig', 'dest'):
            if f in new and f in rec and new[f][0] != rec[f][0] and new[f][1] > rec[f][1]:
                leg = max(leg, new[f][1])
        if leg > self._legs.get(key, 0):
            self._legs[key] = leg
            for k in [k for k, (_, ts) in rec.items() if ts < leg]:
                del rec[k]
        for k, (v, ts) in new.items():
            if ts < leg:
                continue
            if k not in rec or rec[k][1] <= ts:
                rec[k] = (v, ts)
        self._facts.move_to_end(key)
        while len(self._facts) > MAX_FACT_KEYS:
            old, _ = self._facts.popitem(last=False)
            self._legs.pop(old, None)

    def _fact_summary(self, hex_upper, reg, now, has_adsb_pos):
        key = self._key(hex_upper or '', reg or '')
        rec = self._facts.get(key) if key else None
        if not rec and reg:
            rec = self._facts.get('~' + reg)
        if not rec:
            return {}
        out = {}
        for k, (v, ts) in rec.items():
            ttl = ROUTE_TTL if k in ROUTE_FACTS else DEPART_TTL if k in DEPART_FACTS else FAST_TTL
            if now - ts <= ttl:
                out[k] = (v, ts)
        # once it has landed, the old arrival details no longer apply
        landed = max([out[k][1] for k in ('on_ts', 'in_ts') if k in out] or [0])
        for k in ARRIVAL_FACTS + DEPART_FACTS:
            if k in out and out[k][1] < landed:
                del out[k]
        # the gate belongs to the arrival: a later departure makes it stale
        departed = max([out[k][1] for k in ('out_ts', 'off_ts') if k in out] or [0])
        if 'arr_gate' in out and out['arr_gate'][1] < departed:
            del out['arr_gate']
        if 'eta_ts' in out and out['eta_ts'][0] < now - FAST_TTL:
            del out['eta_ts']
        if 'next_wp_eta_ts' in out and out['next_wp_eta_ts'][0] < now - 600:
            for k in ('next_wp', 'next_wp_eta_ts', 'then_wp'):
                out.pop(k, None)
        summary = {k: v for k, (v, _) in out.items() if k not in ('lat', 'lon')}
        if 'lat' in out and 'lon' in out and not has_adsb_pos:
            summary['pos'] = [out['lat'][0], out['lon'][0], out['lat'][1]]
        if summary:
            summary['src'] = 'datalink'
        return summary

    def _live_facts(self, rec, now):
        """{field: (value, msg ts)} for the facts still inside their lifetime."""
        out = {}
        for k, (v, ts) in rec.items():
            ttl = ROUTE_TTL if k in ROUTE_FACTS else DEPART_TTL if k in DEPART_FACTS else FAST_TTL
            if now - ts <= ttl:
                out[k] = (v, ts)
        return out

    def _ident(self, key):
        row = {}
        if _HEX_RE.match(key):
            row['hex'] = key.lower()
        reg = key[1:] if key.startswith('~') else ''
        flight = None
        for msg in reversed(self._by_key.get(key) or ()):
            if not reg and msg.get('reg'):
                reg = msg['reg']
            if flight is None and msg.get('flight'):
                flight = norm_flight(msg['flight'])
            if reg and flight:
                break
        if reg:
            row['reg'] = reg
        return row, flight

    def board(self, airport, now=None):
        """Arrivals and departures for one airport, from what the aircraft reported."""
        now = time.time() if now is None else now
        arr, dep = [], []
        with self._lock:
            for key, rec in self._facts.items():
                f = self._live_facts(rec, now)          # facts of the current leg only

                def val(k):
                    return f[k][0] if k in f else None
                orig, dest = val('orig'), val('dest')
                if airport not in (orig, dest):
                    continue
                off, out, on, inn = val('off_ts'), val('out_ts'), val('on_ts'), val('in_ts')
                local = orig == dest == airport          # a local flight is an arrival once it is moving
                row, flight = self._ident(key)
                flight = flight or norm_flight(val('flight'))
                if flight:
                    row['flight'] = flight
                if orig == airport and not (local and (off or on or inn)):
                    row['to'] = dest
                    if val('dep_rwy'):
                        row['rwy'] = val('dep_rwy')
                    if val('sid'):
                        row['sid'] = val('sid')
                    # any sign of flight this leg, even one past its card lifetime
                    airborne = [rec[k][1] for k in AIRBORNE_FACTS if k in rec]
                    if off:
                        if now - off > BOARD_DROP:
                            continue
                        row['status'], row['time'] = 'departed', off
                    elif out:
                        row['status'], row['time'] = 'pushed_back', out
                    elif airborne:                       # heard in the air, takeoff not heard
                        if now - max(airborne) > BOARD_DROP:
                            continue
                        row['status'] = 'departed'
                    elif val('dep_rwy'):
                        row['status'] = 'cleared'
                    else:
                        row['status'] = 'scheduled'
                    dep.append(row)
                elif dest == airport:
                    row['from'] = orig
                    if val('rwy'):
                        row['rwy'] = val('rwy')
                    if val('arr_gate'):
                        row['gate'] = val('arr_gate')
                    if inn or on:
                        t = inn or on
                        if now - t > BOARD_DROP:
                            continue
                        row['status'], row['time'] = ('at_gate' if inn else 'landed'), t
                    else:
                        row['status'] = 'enroute'
                        eta = val('eta_ts')
                        if eta and eta >= now - FAST_TTL:
                            row['eta'] = row['time'] = eta
                        elif off:
                            row['time'] = off
                    arr.append(row)
        for r in arr + dep:
            for k in [k for k, v in r.items() if v is None]:
                del r[k]
        # en route soonest first (no ETA last), then landed / at the gate, newest first
        arr.sort(key=lambda r: (0, 'eta' not in r, r.get('eta', 0)) if r['status'] == 'enroute'
                 else (1, False, -r['time']))
        rank = {'pushed_back': 0, 'cleared': 1, 'scheduled': 2, 'departed': 3}
        dep.sort(key=lambda r: (rank[r['status']], -(r.get('time') or 0)))
        return {'airport': airport, 'arrivals': arr[:BOARD_MAX], 'departures': dep[:BOARD_MAX]}

    def atis(self, now=None):
        """Fresh ATIS per airport, newest first."""
        now = time.time() if now is None else now
        with self._lock:
            fresh = [dict(a) for a in self._atis.values() if now - a.get('ts', 0) <= ATIS_TTL]
        return sorted(fresh, key=lambda a: a['ts'], reverse=True)

    def _recent(self, hex_upper, reg, now):
        key = self._key(hex_upper or '', reg or '')
        dq = self._by_key.get(key) if key else None
        if not dq and reg:
            dq = self._by_key.get('~' + reg)
        if not dq:
            return []
        cutoff = now - TTL
        return [m for m in dq if m['ts'] >= cutoff]

    def merge(self, ac, hex_upper, reg, now=None):
        """Add {"n", "last_label", "last_ts"} to an aircraft dict when it has recent messages."""
        now = time.time() if now is None else now
        with self._lock:
            recent = self._recent(hex_upper, reg, now)
            facts = self._fact_summary(hex_upper, reg, now, ac.get('lat') is not None)
            if recent or facts:
                d = {'n': len(recent)}
                if recent:
                    last = max(recent, key=lambda m: m['ts'])
                    d['last_label'], d['last_ts'] = last['label'], last['ts']
                d.update(facts)
                ac['datalink'] = d

    def messages(self, hex_upper, reg, now=None):
        """Recent messages for one aircraft, newest first, as copies."""
        now = time.time() if now is None else now
        with self._lock:
            recent = self._recent(hex_upper, reg, now)
            return [dict(m) for m in sorted(recent, key=lambda m: m['ts'], reverse=True)]

    def has_any(self):
        with self._lock:
            return bool(self._by_key)

    def stats(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return {'msgs_last_hour': sum(1 for t in self._times if t >= now - 3600),
                    'frames_last_hour': sum(1 for t in self._frames if t >= now - 3600),
                    'last_ts': self.last_ts, 'last_frame_ts': self.last_frame_ts,
                    'total': self.total, 'bad': self.bad, 'dupes': self.dupes,
                    'aircraft': len(self._by_key), 'since': self.since}


def load_config(path=CFG_PATH):
    """The datalink config, or None when datalink is not installed / unusable."""
    try:
        with open(path) as f:
            c = json.load(f)
        if not isinstance(c, dict):
            return None
        port = c.get('udp_port', 5555)
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            log.warning('[datalink] udp_port %r invalid in %s — datalink ingest off', port, path)
            return None
        c['udp_port'] = port
        return c
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning('[datalink] cannot read %s (%s) — datalink ingest off', path, e)
        return None


def udp_loop(store, port, stop=None):
    """Listen on 127.0.0.1:port for dumpvdl2's JSON. Retries if the port is busy."""
    while not (stop and stop.is_set()):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((BIND_HOST, port))
            sock.settimeout(1.0)
            log.info('[datalink] listening on %s:%d', BIND_HOST, port)
            while not (stop and stop.is_set()):
                try:
                    data = sock.recv(65535)
                except socket.timeout:
                    continue
                for text in split_datagram(data):
                    try:
                        obj = json.loads(text)
                    except Exception:
                        store.note_bad()
                        continue
                    msg = parse(obj)
                    if isinstance(obj, dict) and isinstance(obj.get('vdl2'), dict):
                        store.note_frame(msg['ts'] if msg else time.time())
                    if msg is not None:          # link chatter (RR, XID, DISC) is not a message
                        store.add(msg)
        except Exception as e:
            log.warning('[datalink] listener error (%s) — retrying in 30s', e)
            for _ in range(30):
                if stop and stop.is_set():
                    break
                time.sleep(1)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


def start(cfg_path=CFG_PATH):
    """Start the listener thread if datalink is installed. Returns the Store or None."""
    cfg = load_config(cfg_path)
    if cfg is None:
        return None
    store = Store()
    threading.Thread(target=udp_loop, args=(store, cfg['udp_port']), name='datalink-udp', daemon=True).start()
    return store


def status(store, cfg_path=CFG_PATH, status_path=STATUS_PATH, now=None):
    """Everything the dashboard needs to show or hide the datalink card."""
    cfg = load_config(cfg_path)
    svc = {}
    try:
        with open(status_path) as f:
            svc = json.load(f)
        if not isinstance(svc, dict):
            svc = {}
    except Exception:
        svc = {}
    out = {'installed': cfg is not None,
           'decoder_running': bool(svc.get('running')),
           'driver_ok': svc.get('driver_ok') if svc else None,
           'lib': svc.get('lib', '') if svc else '',
           'serial': (cfg or {}).get('serial', ''),
           'freqs_mhz': (cfg or {}).get('freqs_mhz', [136.975]) if cfg else [],
           'msgs_last_hour': 0, 'frames_last_hour': 0, 'last_ts': None, 'last_frame_ts': None,
           'aircraft': 0, 'total': 0, 'bad': 0, 'dupes': 0, 'since': None}
    out['atis'] = []
    if store is not None:
        out.update(store.stats(now=now))
        out['atis'] = store.atis(now=now)
    return out


def merge_into(store, ac, hex_upper):
    """/flights helper: add the datalink summary to one aircraft. Never raises."""
    if store is None:
        return
    try:
        store.merge(ac, hex_upper, (ac.get('r') or '').upper() or None)
    except Exception as e:      # datalink is extra: it must never cost /flights
        log.warning('[datalink] merge skipped for %s: %s', hex_upper, e)


def register(app, store, cfg_path=CFG_PATH, status_path=STATUS_PATH):
    """Add the two read-only routes to the Flask app.

    Both sit behind app.py's same-origin guard like every other route, answer
    JSON only (the card puts text in with textContent), and are never cached.
    """
    from flask import jsonify, request

    def _nocache(resp):
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/api/datalink/status', methods=['GET'])
    def datalink_status():
        return _nocache(jsonify(status(store, cfg_path, status_path)))

    last_err = [0.0]

    @app.route('/api/datalink/board', methods=['GET'])
    def datalink_board():
        ap = (request.args.get('airport') or '').strip().upper()
        if not _ICAO_RE.match(ap):
            return _nocache(jsonify({'error': 'airport'})), 400
        empty = {'airport': ap, 'arrivals': [], 'departures': []}
        if store is None:
            return _nocache(jsonify(empty))
        try:
            return _nocache(jsonify(store.board(ap)))
        except Exception as e:                       # the board must never break the dashboard
            if time.time() - last_err[0] > 60:
                last_err[0] = time.time()
                log.warning('[datalink] board failed: %s', e)
            return _nocache(jsonify(empty))

    @app.route('/api/datalink/<hexcode>', methods=['GET'])
    def datalink_messages(hexcode):
        h = (hexcode or '').upper()
        if not _HEX_RE.match(h):
            return _nocache(jsonify({'error': 'hex must be 6 hex digits'})), 400
        reg = _REG_DROP.sub('', (request.args.get('reg') or '').upper())[:10] or None
        msgs = store.messages(h, reg) if store is not None else []
        return _nocache(jsonify({'hex': h, 'messages': msgs}))
