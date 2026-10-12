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
FIELDS = ["time"] + HOURLY_VARS  # time is UTC, e.g. 2026-10-13T05:00
