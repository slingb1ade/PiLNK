"""Tests for daily_counts.py. Run from the repo root:  python3 -m unittest tools.test_daily_counts"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from daily_counts import DailyCounter

def lt_utc(ts):  # fixed "local" = UTC so tests are deterministic
    return time.gmtime(ts)

D1 = 1792540800  # 2026-10-21 00:00:00 UTC
H = 3600

class T(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'daily_counts.json')
    def mk(self):
        c = DailyCounter(self.path, localtime=lt_utc); c.load(); return c

    def test_counts_unique_hexes_per_day(self):
        c = self.mk()
        self.assertEqual(c.note(['aaa', 'bbb', 'aaa'], D1 + 10), 2)
        self.assertEqual(c.note(['bbb', 'ccc'], D1 + 20), 1)
        s = c.snapshot(D1 + 30)
        self.assertEqual((s['today'], s['total'], s['best']['count'], s['date']), (3, 3, 3, '2026-10-21'))

    def test_rollover_starts_new_day_keeps_best_and_total(self):
        c = self.mk()
        c.note(['a', 'b', 'c'], D1 + 10)
        c.note(['a', 'd'], D1 + 24 * H + 5)     # next day: 'a' counts again
        s = c.snapshot(D1 + 24 * H + 6)
        self.assertEqual(s['today'], 2)
        self.assertEqual(s['total'], 5)
        self.assertEqual(s['best'], {'count': 3, 'date': '2026-10-21'})
        self.assertEqual(s['date'], '2026-10-22')

    def test_snapshot_after_midnight_with_no_traffic_reads_zero(self):
        c = self.mk()
        c.note(['a', 'b'], D1 + 10)
        self.assertEqual(c.snapshot(D1 + 24 * H + 1)['today'], 0)

    def test_survives_restart_without_double_counting(self):
        c = self.mk(); c.note(['a', 'b'], D1 + 10); c.save()
        c2 = self.mk()
        self.assertEqual(c2.note(['a', 'b', 'c'], D1 + 20), 1)
        s = c2.snapshot(D1 + 30)
        self.assertEqual((s['today'], s['total']), (3, 3))

    def test_seed_today_counts_once_and_seed_best_only_raises(self):
        c = self.mk()
        c.seed_today(['x', 'y'], D1 + 5)
        c.seed_today(['x', 'y'], D1 + 6)
        c.seed_best(40, '2026-10-02')
        c.seed_best(1, '2026-10-01')
        s = c.snapshot(D1 + 7)
        self.assertEqual((s['today'], s['total']), (2, 2))
        self.assertEqual(s['best'], {'count': 40, 'date': '2026-10-02'})

    def test_since_is_first_day_seen(self):
        c = self.mk(); c.note(['a'], D1 + 10); c.note(['b'], D1 + 3 * 24 * H)
        self.assertEqual(c.snapshot(D1 + 3 * 24 * H)['since'], '2026-10-21')

    def test_corrupt_or_hostile_file_starts_clean(self):
        for junk in ('not json', '[]', '{"total": "lots", "today": {"hexes": 5}}', '{"best": {"count": -3}}'):
            with open(self.path, 'w') as f: f.write(junk)
            c = self.mk()
            s = c.snapshot(D1)
            self.assertEqual((s['today'], s['total'], s['best']['count']), (0, 0, 0), junk)
            c.note(['a'], D1 + 1)

    def test_save_is_atomic_and_readable(self):
        c = self.mk(); c.note(['a'], D1 + 1); c.save()
        with open(self.path) as f: d = json.load(f)
        self.assertEqual(d['total'], 1)
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_save_if_due(self):
        c = self.mk(); c.note(['a'], D1 + 1)
        self.assertTrue(c.save_if_due(D1 + 1, every=60))
        c.note(['b'], D1 + 2)
        self.assertFalse(c.save_if_due(D1 + 30, every=60))
        self.assertTrue(c.save_if_due(D1 + 62, every=60))

    def test_ignores_blank_and_non_string_hexes(self):
        c = self.mk()
        self.assertEqual(c.note(['', None, 5, ' ', 'abc'], D1 + 1), 1)

    def test_hex_case_insensitive(self):
        c = self.mk()
        self.assertEqual(c.note(['ABC', 'abc', '~abc'], D1 + 1), 2)

    def test_old_days_trimmed(self):
        c = self.mk()
        for i in range(500):
            c.note(['h%d' % i], D1 + i * 24 * H)
        c.save()
        with open(self.path) as f: d = json.load(f)
        self.assertLessEqual(len(d['days']), 400)
        self.assertEqual(c.snapshot(D1 + 499 * 24 * H)['total'], 500)

if __name__ == '__main__':
    unittest.main()
