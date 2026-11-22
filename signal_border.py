"""Signal Border - how far this receiver hears aircraft, in every direction.

360 one-degree bins of true bearing from the receiver (bin i covers [i, i+1) degrees), each holding
the furthest whole-nm range heard. Kept two ways: the last 24 hours and all time.

Pure module: no Flask, no network, no globals. app.py feeds it decoder snapshots (aircraft.json)
and reads the numbers back. Works with dump1090-fa and readsb, anywhere in the world.

The last 24 hours is 24 hourly slots keyed by the UTC hour (epoch seconds // 3600). A slot exists
for every hour sample() ran in, traffic or not, so "hours with data" means hours we were listening.
The live window is the current hour and the 23 before it. Anything older is dropped. An hour that
is AHEAD of the clock is never shown, but it is only dropped when it is more than FUTURE_KEEP_H
ahead: a Pi has no clock of its own and restarts a little behind the real time until the network
corrects it, and that must not cost it the hours it had just recorded. All time has no decay.

Saved to one small JSON file: {"v":1,"site":[lat,lon],"all":[360],"slots":{"<hour>":[360]},"updated":ts}.
The site is kept so a moved receiver starts again: its old ranges were measured from somewhere else.

A position counts only when ALL of these hold:
  * the node decoded it itself from ADS-B (no MLAT, TIS-B, ADS-R; see own_adsb);
  * it is fresh (seen_pos <= MAX_SEEN_POS_S);
  * 0 < range <= MAX_NM, and the caller's reject() does not veto it;
  * the same aircraft gave another accepted fix MIN_GAP_S..MAX_GAP_S earlier that is a plausible
    move (no faster than MAX_KT). One bad frame can therefore never set a bin.
"Fix time" is the position's own time (now - seen_pos), so a stale position sitting in the
decoder's file for a minute is one fix, however many times it is sampled.
"""
import json
import math
import os
import threading

BINS = 360
HOURS = 24
MAX_NM = 400            # past this is a decode glitch (same cap as the coverage map)
MIN_GAP_S = 5           # the confirming fix must be at least this much later...
MAX_GAP_S = 60          # ...and at most this much
MAX_KT = 900            # nothing we hear on 1090 moves faster over the ground
MAX_SEEN_POS_S = 10     # positions older than this are not trusted
MOVE_RESET_KM = 1.0     # receiver moved further than this: the old ranges mean nothing
FUTURE_KEEP_H = 48      # hour slots up to this far AHEAD of the clock are kept (not shown): see _trim

_R_NM = 3440.065
_SLACK_NM = 0.5         # position noise allowance on top of MAX_KT


def own_adsb(ac):
    """True when this record's position was decoded by this receiver from ADS-B.

    readsb says where the record came from in `type`: only adsb_* is the aircraft's own broadcast
    (mlat = worked out by a server, tisb_* / adsr_* = rebroadcast by a ground station, heard from
    the ground station and not the aircraft). dump1090-fa has no `type`; it lists the fields that
    came from MLAT or TIS-B in `mlat` / `tisb`. A record with neither marker is plain ADS-B.
    """
    t = ac.get('type')
    if t is not None and not (isinstance(t, str) and t.startswith('adsb_')):
        return False
    for key in ('mlat', 'tisb'):
        v = ac.get(key)
        if isinstance(v, (list, tuple)) and ('lat' in v or 'lon' in v):
            return False
    return True


def dist_bearing(lat1, lon1, lat2, lon2):
    """Great-circle distance (nm) and initial true bearing (0-360) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    a = min(1.0, max(0.0, a))
    nm = _R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return nm, (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _num(v):
    """A real, finite number (bools and strings are not), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


class Border:
    def __init__(self, path, log=print):
        self.path = path
        self.log = log
        self.lock = threading.Lock()
        self.all = [0] * BINS
        self.slots = {}           # epoch hour -> [BINS] furthest whole nm heard in that hour
        self.site = None          # [lat, lon] the ranges were measured from
        self._anchors = {}        # hex -> [fix time, lat, lon, bin, whole nm, counted]
        self._dirty = False
        self._saved_at = None     # time of the last save attempt (None = save at the first chance)
        self._said_at = None      # time of the last log line (at most one an hour)
        self._io = threading.Lock()   # one writer at a time on the temp file (sampler thread vs the reset route)

    # ---- internals (call with the lock held) ----
    def _say(self, now, msg):
        if self._said_at is None or abs(now - self._said_at) >= 3600:
            self._said_at = now
            try:
                self.log('[PILNK] Signal Border: ' + msg)
            except Exception:
                pass

    def _live(self, now):
        """The hours that make up 'the last 24 h' at this moment."""
        h = int(now // 3600)
        return [k for k in self.slots if h - (HOURS - 1) <= k <= h]

    def _trim(self, now):
        """Drop hours that can never be shown again: older than the window, or absurdly far ahead."""
        h = int(now // 3600)
        for k in [k for k in self.slots if k < h - (HOURS - 1) or k > h + FUTURE_KEEP_H]:
            del self.slots[k]
            self._dirty = True

    def _put(self, now, b, nm):
        slot = self.slots[int(now // 3600)]
        if nm > slot[b]:
            slot[b] = nm
            self._dirty = True
        if nm > self.all[b]:
            self.all[b] = nm
            self._dirty = True

    def _check_site(self, slat, slon):
        if self.site is None:
            self.site = [slat, slon]
            self._dirty = True
        elif dist_bearing(self.site[0], self.site[1], slat, slon)[0] * 1.852 > MOVE_RESET_KM:
            self.all = [0] * BINS
            self.slots = {}
            self._anchors = {}
            self.site = [slat, slon]
            self._dirty = True
            self._saved_at = None

    # ---- feeding ----
    def sample(self, aircraft, now, site, reject=None):
        """Feed one decoder snapshot. Returns how many positions counted on this call."""
        if not site or _num(site[0]) is None or _num(site[1]) is None:
            return 0
        slat, slon = float(site[0]), float(site[1])
        counted = 0
        with self.lock:
            self._check_site(slat, slon)
            self._trim(now)
            if int(now // 3600) not in self.slots:
                self.slots[int(now // 3600)] = [0] * BINS
                self._dirty = True
            for ac in (aircraft if isinstance(aircraft, (list, tuple)) else ()):
                if not isinstance(ac, dict):
                    continue
                hx = ac.get('hex')
                if not isinstance(hx, str) or not hx.strip():
                    continue
                hx = hx.strip().lower()
                lat, lon, sp = _num(ac.get('lat')), _num(ac.get('lon')), _num(ac.get('seen_pos'))
                if lat is None or lon is None or sp is None or not (0 <= sp <= MAX_SEEN_POS_S) or abs(lat) > 90 or abs(lon) > 360:
                    continue
                if not own_adsb(ac):
                    continue
                nm, brg = dist_bearing(slat, slon, lat, lon)
                if not (0 < nm <= MAX_NM):
                    continue
                if reject is not None:
                    try:
                        if reject(ac, nm):
                            continue
                    except Exception:
                        continue          # a broken veto must not let a doubtful position through
                t = now - sp
                fix = [t, lat, lon, int(brg) % BINS, max(1, min(MAX_NM, int(round(nm)))), False]
                a = self._anchors.get(hx)
                if a is None:
                    self._anchors[hx] = fix
                    continue
                dt = t - a[0]
                if -MAX_GAP_S <= dt < MIN_GAP_S:
                    continue              # the same fix again, or too soon to prove anything
                if MIN_GAP_S <= dt <= MAX_GAP_S and \
                        dist_bearing(a[1], a[2], lat, lon)[0] <= MAX_KT * dt / 3600.0 + _SLACK_NM:
                    if not a[5]:
                        self._put(now, a[3], a[4]); counted += 1
                    self._put(now, fix[3], fix[4]); counted += 1
                    fix[5] = True
                self._anchors[hx] = fix   # confirmed or not, the newest fix is what the next one is checked against
            for hx in [h for h, a in self._anchors.items() if abs(now - a[0]) > MAX_GAP_S + MAX_SEEN_POS_S]:
                del self._anchors[hx]
        return counted

    # ---- reading ----
    def snapshot(self, now):
        with self.lock:
            live = self._live(now)
            day = [0] * BINS
            for k in live:
                slot = self.slots[k]
                for i in range(BINS):
                    if slot[i] > day[i]:
                        day[i] = slot[i]
            return {'border_24h': day, 'border_all': list(self.all), 'border_hours': len(live)}

    def reset_all(self, now):
        """Forget the all-time outline (the owner asked). The last 24 h is left alone."""
        with self.lock:
            self.all = [0] * BINS
            self._dirty = True
            self._saved_at = None

    # ---- disk ----
    @staticmethod
    def _bins_ok(v):
        return isinstance(v, list) and len(v) == BINS and \
            all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= MAX_NM for x in v)

    def load(self, now):
        """Read the saved file. Missing = first run (quiet). Anything wrong with it = start fresh."""
        if not self.path:
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            with self.lock:
                self._say(now, 'saved outline unreadable, starting fresh (%s)' % type(e).__name__)
            return
        ok = isinstance(d, dict) and d.get('v') == 1 and self._bins_ok(d.get('all')) and isinstance(d.get('slots'), dict)
        slots = {}
        if ok:
            for k, v in d['slots'].items():
                try:
                    hour = int(k) if (isinstance(k, str) and k.isascii() and k.isdigit()) else None
                except ValueError:        # belt and braces: load() must never raise, or app.py switches the feature off for good
                    hour = None
                if hour is None or not self._bins_ok(v):
                    ok = False
                    break
                slots[hour] = list(v)
        with self.lock:
            if not ok:
                self._say(now, 'saved outline not valid, starting fresh')
                return
            self.all = list(d['all'])
            self.slots = slots
            st = d.get('site')
            if isinstance(st, list) and len(st) == 2 and _num(st[0]) is not None and _num(st[1]) is not None:
                self.site = [float(st[0]), float(st[1])]
            self._dirty = False
            self._trim(now)       # a node that was off, or whose clock was wrong, shows no stale hours

    def save(self, now):
        """Write the file (temp file + atomic replace). Raises on failure; see save_if_due."""
        if not self.path:
            return
        with self.lock:
            self._trim(now)
            text = json.dumps({'v': 1, 'site': self.site, 'all': self.all,
                               'slots': {str(k): v for k, v in sorted(self.slots.items())},
                               'updated': int(now)}, separators=(',', ':'))
            self._dirty = False
        try:
            with self._io:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(text)
                os.replace(tmp, self.path)
        except Exception:
            with self.lock:
                self._dirty = True
            raise

    def save_if_due(self, now, every=300):
        """Save when something changed and the last attempt was at least `every` seconds ago.
        Never raises: a disk that refuses the write is reported at most once an hour and retried later."""
        with self.lock:
            if not self.path or not self._dirty:
                return False
            if self._saved_at is not None and 0 <= now - self._saved_at < every:
                return False
            self._saved_at = now
        try:
            self.save(now)
            return True
        except Exception as e:
            with self.lock:
                self._say(now, 'could not save the outline (%s: %s)' % (type(e).__name__, e))
            return False
