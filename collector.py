#!/usr/bin/env python3
"""Hourly weather collector. Free Open-Meteo API (no key), stdlib only.

Usage:
  python collector.py                      # collect (also back-fills last 3 days)
  python collector.py --backfill-days 30   # recover a longer outage (max 92)
  python collector.py --check --days 7     # report missing hours, exit 1 if any
  python collector.py --dry-run            # fetch + print, write nothing
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 21°10'32.6"N 73°17'08.0"E in decimal degrees
LAT, LON = 21.175722, 73.285556
API_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "precipitation", "pressure_msl", "cloud_cover",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "shortwave_radiation", "et0_fao_evapotranspiration",
    "vapour_pressure_deficit", "soil_temperature_6cm",
    "soil_moisture_3_to_9cm", "weather_code",
]
DAILY_VARS = [
    "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
    "precipitation_probability_max", "et0_fao_evapotranspiration",
    "wind_speed_10m_max",
]
FIELDS = ["time", "time_ist"] + HOURLY_VARS  # time = UTC; time_ist = India time


DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
MAX_DATA_MB = float(os.environ.get("MAX_DATA_MB", "50"))
PAST_DAYS = int(os.environ.get("PAST_DAYS", "3"))


def build_url(past_days, forecast_days=1):
    q = {"latitude": LAT, "longitude": LON, "hourly": ",".join(HOURLY_VARS),
         "past_days": past_days, "forecast_days": forecast_days, "timezone": "UTC"}
    return API_URL + "?" + urllib.parse.urlencode(q)


def build_forecast_url():
    q = {"latitude": LAT, "longitude": LON, "daily": ",".join(DAILY_VARS),
         "forecast_days": 7, "timezone": "Asia/Kolkata"}
    return API_URL + "?" + urllib.parse.urlencode(q)


def fetch_json(url, retries=4):
    """GET with exponential backoff (2s, 4s, 8s, 16s)."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "farm-weather-collector"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network, HTTP 429/5xx, bad JSON
            last = e
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"API request failed after {retries + 1} attempts: {last}")


def _s(v):
    return "" if v is None else str(v)

IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(t):
    """UTC 'YYYY-MM-DDTHH:MM' -> India time 'YYYY-MM-DD HH:MM'."""
    dt = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M")


def parse_hourly(payload, now):
    """Rows up to and including the current UTC hour (future hours dropped)."""
    h = payload["hourly"]
    cutoff = now.strftime("%Y-%m-%dT%H:00")
    rows = []
    for i, t in enumerate(h["time"]):
        if t > cutoff:
            break
        row = {"time": t}
        for v in HOURLY_VARS:
            col = h.get(v) or []
            row[v] = _s(col[i]) if i < len(col) else ""
        rows.append(row)
    return rows


# ---------- storage ----------
def month_path(data_dir, t):
    return Path(data_dir) / f"{t[:7]}.csv"


def read_month(path):
    if not Path(path).exists():
        return {}
    with open(path, newline="") as f:
        return {r["time"]: r for r in csv.DictReader(f)}


def write_month(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for t in sorted(rows):
            row = {k: rows[t].get(k, "") for k in FIELDS}
            row["time_ist"] = to_ist(t)
            w.writerow(row)
    os.replace(tmp, path)  # atomic: no half-written CSV if interrupted


def save_rows(rows, data_dir=DATA_DIR):
    """Add missing hours; fill empty cells. Never overwrites existing values.
    Returns number of rows added or changed (0 means nothing to commit)."""
    by_month = {}
    for r in rows:
        by_month.setdefault(r["time"][:7], []).append(r)
    changed = 0
    for month, new in by_month.items():
        path = month_path(data_dir, new[0]["time"])
        existing = read_month(path)
        dirty = False
        for r in new:
            cur = existing.get(r["time"])
            if cur is None:
                existing[r["time"]] = r
                dirty = True
                changed += 1
            else:
                filled = False
                for k in HOURLY_VARS:
                    if cur.get(k, "") == "" and r[k] != "":
                        cur[k] = r[k]
                        filled = True
                if filled:
                    dirty = True
                    changed += 1
        if dirty:
            write_month(path, existing)
    return changed


def data_size_mb(data_dir=DATA_DIR):
    d = Path(data_dir)
    if not d.exists():
        return 0.0
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1e6


def check_storage(data_dir=DATA_DIR, max_mb=None):
    max_mb = MAX_DATA_MB if max_mb is None else max_mb
    size = data_size_mb(data_dir)
    if size >= max_mb:
        raise SystemExit(f"STORAGE LIMIT: data/ is {size:.1f} MB (limit {max_mb} MB). "
                         "Archive old CSVs or raise MAX_DATA_MB.")
    if size >= 0.8 * max_mb:
        print(f"WARNING: data/ is {size:.1f} MB, over 80% of the {max_mb} MB limit.")


# ---------- gaps ----------
def find_gaps(data_dir=DATA_DIR, days=7, now=None):
    """Missing hourly timestamps in the last `days`, counted from the first stored hour."""
    now = now or datetime.now(timezone.utc)
    present = set()
    for p in Path(data_dir).glob("????-??.csv"):
        present.update(read_month(p).keys())
    if not present:
        return []
    end = now.replace(minute=0, second=0, microsecond=0)
    start = max(end - timedelta(days=days),
                datetime.strptime(min(present), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc))
    gaps, t = [], start
    while t <= end:
        s = t.strftime("%Y-%m-%dT%H:%M")
        if s not in present:
            gaps.append(s)
        t += timedelta(hours=1)
    return gaps


# ---------- daily forecast snapshot (once per UTC day, overwritten) ----------
def maybe_save_forecast(now, data_dir=DATA_DIR, fetch=fetch_json):
    path = Path(data_dir) / "forecast_latest.json"
    today = now.strftime("%Y-%m-%d")
    if path.exists():
        try:
            if json.loads(path.read_text()).get("fetched_date") == today:
                return False
        except ValueError:
            pass
    p = fetch(build_forecast_url())
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_date": today, "units": p.get("daily_units"),
                                "daily": p["daily"]}, indent=1))
    return True


# ---------- main flow ----------
def collect(past_days=PAST_DAYS, now=None, data_dir=DATA_DIR, fetch=fetch_json, dry_run=False):
    now = now or datetime.now(timezone.utc)
    check_storage(data_dir)
    rows = parse_hourly(fetch(build_url(past_days)), now)
    if not rows:
        raise RuntimeError("API returned no hourly rows")
    if dry_run:
        print(f"{len(rows)} rows; first={rows[0]}\nlast={rows[-1]}")
        return 0
    changed = save_rows(rows, data_dir)
    try:
        maybe_save_forecast(now, data_dir, fetch)
    except Exception as e:  # forecast is optional; never fail the hourly run for it
        print(f"WARNING: forecast snapshot skipped: {e}")
    print(f"OK: {len(rows)} rows fetched, {changed} added/filled")
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=0, help="re-fetch last N days (max 92)")
    ap.add_argument("--check", action="store_true", help="report missing hours")
    ap.add_argument("--days", type=int, default=7, help="window for --check")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        gaps = find_gaps(days=a.days)
        print(f"{len(gaps)} missing hour(s) in last {a.days} days")
        for g in gaps[:20]:
            print("  ", g)
        return 1 if gaps else 0
    days = min(max(a.backfill_days, PAST_DAYS), 92)
    collect(past_days=days, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
