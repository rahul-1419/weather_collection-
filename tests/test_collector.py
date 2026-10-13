import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collector as c  # noqa: E402


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def hourly_payload(now, past_days=3):
    """Mimics Open-Meteo: full days from past_days ago through end of today (UTC)."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=past_days)
    n = (past_days + 1) * 24
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    h = {"time": times}
    for v in c.HOURLY_VARS:
        h[v] = [round(20 + i * 0.1, 1) for i in range(n)]
    return {"hourly": h}


def make_fetch(now, calls=None):
    def fake(url):
        if calls is not None:
            calls.append(url)
        if "daily=" in url:
            return {"daily_units": {}, "daily": {"time": ["2026-10-13"], "temperature_2m_max": [33]}}
        return hourly_payload(now)
    return fake


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = utc(2026, 10, 13, 5, 7)

    def tearDown(self):
        self.tmp.cleanup()

    def test_future_hours_dropped(self):
        rows = c.parse_hourly(hourly_payload(self.now), self.now)
        self.assertEqual(rows[-1]["time"], "2026-10-13T05:00")

    def test_idempotent(self):
        c.collect(now=self.now, data_dir=self.dir, fetch=make_fetch(self.now))
        again = c.collect(now=self.now, data_dir=self.dir, fetch=make_fetch(self.now))
        self.assertEqual(again, 0)  # nothing new -> no git commit

    def test_missed_runs_recovered(self):
        c.collect(now=self.now, data_dir=self.dir, fetch=make_fetch(self.now))
        path = self.dir / "2026-10.csv"
        rows = list(csv.DictReader(open(path)))
        kept = [r for r in rows if not ("2026-10-12T10:00" <= r["time"] <= "2026-10-12T20:00")]
        with open(path, "w", newline="") as f:  # simulate an 11-hour outage
            w = csv.DictWriter(f, fieldnames=c.FIELDS)
            w.writeheader()
            w.writerows(kept)
        self.assertEqual(len(c.find_gaps(self.dir, 7, self.now)), 11)
        c.collect(now=self.now + timedelta(hours=1), data_dir=self.dir,
                  fetch=make_fetch(self.now + timedelta(hours=1)))
        self.assertEqual(c.find_gaps(self.dir, 7, self.now + timedelta(hours=1)), [])

    def test_month_boundary(self):
        now = utc(2026, 11, 1, 2, 5)
        c.collect(now=now, data_dir=self.dir, fetch=make_fetch(now))
        self.assertTrue((self.dir / "2026-10.csv").exists())
        self.assertTrue((self.dir / "2026-11.csv").exists())

    def test_fills_empty_cells_but_keeps_existing(self):
        t = "2026-10-13T05:00"
        c.write_month(self.dir / "2026-10.csv",
                      {t: {"time": t, "temperature_2m": "", "dew_point_2m": "9.9"}})
        row = {"time": t, **{v: "1" for v in c.HOURLY_VARS}}
        c.save_rows([row], self.dir)
        got = c.read_month(self.dir / "2026-10.csv")[t]
        self.assertEqual(got["temperature_2m"], "1")
        self.assertEqual(got["dew_point_2m"], "9.9")

    def test_storage_limit(self):
        (self.dir / "big.bin").write_bytes(b"x" * 2_000_000)
        with self.assertRaises(SystemExit):
            c.check_storage(self.dir, max_mb=1)

    def test_forecast_once_per_day(self):
        calls = []
        f = make_fetch(self.now, calls)
        self.assertTrue(c.maybe_save_forecast(self.now, self.dir, f))
        self.assertFalse(c.maybe_save_forecast(self.now + timedelta(hours=3), self.dir, f))
        self.assertEqual(len(calls), 1)
        self.assertIn("daily", json.loads((self.dir / "forecast_latest.json").read_text()))

    def test_url_has_location(self):
        u = c.build_url(3)
        self.assertIn("latitude=21.175722", u)
        self.assertIn("longitude=73.285556", u)


if __name__ == "__main__":
    unittest.main()
