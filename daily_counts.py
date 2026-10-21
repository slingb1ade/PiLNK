"""Node-side "Aircraft Tracked" counters: Today, Best Day, All Time.

Until v1.6.x these lived in the dashboard browser's localStorage, so they only
counted while a tab was open and reset whenever a browser was cleared (on a
fresh browser Today / All Time / Best Day all read the same number). The node
sees every aircraft whether or not anyone is looking, so it keeps the count.

Definitions (same as the old browser tiles, so the numbers mean what they did):
  today  - unique aircraft (by hex) seen since local midnight on this node
  best   - the highest daily figure, with its date
  total  - sum of every day's unique count since `since` (an aircraft seen on
           two days counts on both days, as before)
"Local" is the node's own clock and time zone, so each node's day runs on its
own midnight - global by default, nothing assumes New Zealand.

Disk: one small JSON file, written atomically. Today's hex list is stored so a
restart does not count the same aircraft twice.
"""
import json
import os
import threading
import time

KEEP_DAYS = 400


def _clean_hex(h):
    if not isinstance(h, str):
        return None
    h = h.strip().lower()
    return h or None


def _nonneg_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class DailyCounter:
    def __init__(self, path, localtime=time.localtime):
        self.path = path
        self._localtime = localtime
        self._lock = threading.Lock()
        self._saved_at = 0.0
        self._reset()

    def _reset(self):
        self.since = ''
        self.total = 0
        self.days = {}                 # 'YYYY-MM-DD' -> count
        self.best = {'count': 0, 'date': ''}
        self.date = ''
        self.hexes = set()

    def day_key(self, ts):
        return time.strftime('%Y-%m-%d', self._localtime(ts))

    # ---- persistence -------------------------------------------------------
    def load(self):
        with self._lock:
            self._reset()
            try:
                with open(self.path, 'r') as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return
            if not isinstance(d, dict):
                return
            total = _nonneg_int(d.get('total'))
            days = d.get('days') if isinstance(d.get('days'), dict) else {}
            best = d.get('best') if isinstance(d.get('best'), dict) else {}
            today = d.get('today') if isinstance(d.get('today'), dict) else {}
            hexes = today.get('hexes')
            if total is None or not isinstance(hexes, list):
                return                 # not our shape - start clean, never guess
            self.total = total
            self.since = d.get('since') if isinstance(d.get('since'), str) else ''
            self.days = {k: v for k, v in days.items()
                         if isinstance(k, str) and _nonneg_int(v) is not None}
            bc = _nonneg_int(best.get('count'))
            if bc is not None:
                self.best = {'count': bc, 'date': best.get('date') if isinstance(best.get('date'), str) else ''}
            self.date = today.get('date') if isinstance(today.get('date'), str) else ''
            self.hexes = {h for h in (_clean_hex(x) for x in hexes) if h}

    def save(self):
        with self._lock:
            data = {
                'version': 1,
                'since': self.since,
                'total': self.total,
                'best': dict(self.best),
                'days': dict(sorted(self.days.items())[-KEEP_DAYS:]),
                'today': {'date': self.date, 'hexes': sorted(self.hexes)},
            }
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
        os.replace(tmp, self.path)

    def save_if_due(self, ts, every=60):
        if ts - self._saved_at < every:
            return False
        self._saved_at = ts
        self.save()
        return True

    # ---- counting ----------------------------------------------------------
    def _roll(self, ts):
        """Caller holds the lock. Move to the day `ts` falls in."""
        key = self.day_key(ts)
        if key != self.date:
            self.date = key
            self.hexes = set()
            if not self.since:
                self.since = key
            if len(self.days) > KEEP_DAYS:
                for k in sorted(self.days)[:-KEEP_DAYS]:
                    del self.days[k]

    def _add(self, hexes):
        added = 0
        for h in hexes:
            h = _clean_hex(h)
            if h and h not in self.hexes:
                self.hexes.add(h)
                added += 1
        if added:
            self.total += added
            n = len(self.hexes)
            self.days[self.date] = n
            if n > self.best['count']:
                self.best = {'count': n, 'date': self.date}
        return added

    def note(self, hexes, ts):
        """Count every hex in `hexes` not yet seen today. Returns how many were new."""
        with self._lock:
            self._roll(ts)
            return self._add(hexes)

    def seed_today(self, hexes, ts):
        """Fold in aircraft already seen today (e.g. from the 24 h history store)."""
        self.note(hexes, ts)

    def seed_best(self, count, date):
        """Adopt an older best day (e.g. stats_records most_day) if it is higher."""
        count = _nonneg_int(count)
        if count is None:
            return
        with self._lock:
            if count > self.best['count']:
                self.best = {'count': count, 'date': date if isinstance(date, str) else ''}

    def snapshot(self, ts):
        with self._lock:
            today = len(self.hexes) if self.day_key(ts) == self.date else 0
            return {'today': today, 'date': self.day_key(ts), 'total': self.total,
                    'best': dict(self.best), 'since': self.since or self.day_key(ts)}
