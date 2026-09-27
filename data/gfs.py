"""Shared helpers for reading NOAA's public GFS archive on AWS S3.

Source (verified live 2025-09-26): s3://noaa-gfs-bdp-pds, fully public --
confirmed by listing/fetching objects over plain HTTPS with no AWS
credentials, boto3, or SNS subscription required.
Registry: https://registry.opendata.aws/noaa-gfs-bdp-pds/

This module only READS the archive (list objects, parse .idx sidecars,
byte-range GET single GRIB2 messages) -- it never bulk-downloads a full
global GRIB2 file. It does not touch Kalshi data, HRRR test data, Jev,
Laya, or any trading logic.

Run/product structure (OBSERVED FROM ARCHIVE by listing/HEAD-probing real
objects for 2025-07-01, consistent with GFS's documented schedule):
  - 4 runs/day: 00Z, 06Z, 12Z, 18Z (confirmed via directory listing).
  - Product used: pgrb2.0p25 (0.25-degree, "common" surface/pressure
    fields -- confirmed to contain every variable this test needs).
  - Forecast hours: HOURLY from F000 through F120, then every 3 hours
    from F123 through F384 (confirmed live: f120 exists, f121 returns
    404, f123 exists, f384 exists, f385 returns 404).
  - Grid: regular lat-lon (gridType='regular_ll'), 1440 x 721 points,
    0.25 degree spacing (confirmed via eccodes on a decoded message) --
    structurally different from HRRR's Lambert Conformal ~3km grid.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-gfs-bdp-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

RUN_HOURS = {0, 6, 12, 18}
HOURLY_MAX_FH = 120
EXTENDED_MAX_FH = 384
EXTENDED_STEP = 3

NYC_TZ = ZoneInfo("America/New_York")


def available_forecast_hours() -> list[int]:
    """Every forecast hour GFS's 0.25-degree product actually publishes,
    for ANY run (the schedule itself does not depend on init hour, unlike
    HRRR's synoptic-vs-hourly split)."""
    hours = list(range(0, HOURLY_MAX_FH + 1))
    hours += list(range(HOURLY_MAX_FH + EXTENDED_STEP, EXTENDED_MAX_FH + 1, EXTENDED_STEP))
    return hours


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def grib_key(run_date: datetime, run_hour: int, forecast_hour: int, product: str = "pgrb2.0p25") -> str:
    return f"gfs.{run_date:%Y%m%d}/{run_hour:02d}/atmos/gfs.t{run_hour:02d}z.{product}.f{forecast_hour:03d}"


def list_objects(prefix: str, max_keys: int = 1000, delimiter: str = "") -> tuple[list[dict], list[str]]:
    import re

    params = {"list-type": "2", "prefix": prefix, "max-keys": max_keys}
    if delimiter:
        params["delimiter"] = delimiter
    r = requests.get(f"{BASE_URL}/", params=params, timeout=30)
    r.raise_for_status()
    keys = re.findall(r"<Key>(.*?)</Key>", r.text)
    mods = re.findall(r"<LastModified>(.*?)</LastModified>", r.text)
    sizes = re.findall(r"<Size>(.*?)</Size>", r.text)
    prefixes = re.findall(r"<Prefix>(.*?)</Prefix>", r.text)
    return [{"key": k, "last_modified": m, "size": int(s)} for k, m, s in zip(keys, mods, sizes)], prefixes


def fetch_idx(grib_key_: str, stats: RequestStats | None = None) -> list[dict]:
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/{grib_key_}.idx", timeout=30)
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    r.raise_for_status()
    entries = []
    for line in r.text.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        entries.append(
            {
                "msg_num": int(parts[0]),
                "byte_start": int(parts[1]),
                "run_date": parts[2].replace("d=", ""),
                "variable": parts[3],
                "level": parts[4],
                "forecast_desc": parts[5],
            }
        )
    return entries


def find_message(entries: list[dict], variable: str, level: str) -> dict | None:
    """First matching message. NOTE: GFS's pgrb2.0p25 idx has been
    observed to list APCP:surface:0-N hour acc fcst TWICE, back-to-back,
    with no other distinguishing text in the simple .idx format. We take
    the first occurrence and flag this as an OBSERVED anomaly rather than
    silently guessing which one is "correct" -- see feasibility report."""
    for i, e in enumerate(entries):
        if e["variable"] == variable and e["level"] == level:
            byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
            return {**e, "byte_end": byte_end}
    return None


def fetch_byte_range(grib_key_: str, byte_start: int, byte_end: int | None, stats: RequestStats | None = None) -> tuple[bytes, str | None]:
    range_header = f"bytes={byte_start}-{byte_end if byte_end is not None else ''}"
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/{grib_key_}", headers={"Range": range_header}, timeout=60)
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Range GET failed for {grib_key_} ({range_header}): HTTP {r.status_code}")
    return r.content, r.headers.get("Last-Modified")


def decode_message(raw_bytes: bytes, requested_lat: float, requested_lon: float) -> dict:
    """Same technique as data/hrrr.py's decode_message -- a byte-range
    slice is a self-contained, valid GRIB2 message that eccodes can open
    directly."""
    import os
    import tempfile

    import eccodes

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(raw_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                raise RuntimeError("eccodes could not parse this byte range as a GRIB2 message")
            try:
                variable = eccodes.codes_get(gid, "shortName")
                level = eccodes.codes_get(gid, "level")
                level_type = eccodes.codes_get(gid, "typeOfLevel")
                units = eccodes.codes_get(gid, "units")
                forecast_hour = eccodes.codes_get(gid, "forecastTime")
                data_date = eccodes.codes_get(gid, "dataDate")
                data_time = eccodes.codes_get(gid, "dataTime")
                grid_type = eccodes.codes_get(gid, "gridType")

                nearest = eccodes.codes_grib_find_nearest(gid, requested_lat, requested_lon)
                point = nearest[0] if isinstance(nearest, (list, tuple)) else nearest
                return {
                    "variable": variable,
                    "level": level,
                    "level_type": level_type,
                    "units": units,
                    "forecast_hour": forecast_hour,
                    "data_date": data_date,
                    "data_time": data_time,
                    "grid_type": grid_type,
                    "grid_lat": getattr(point, "lat", None),
                    "grid_lon": getattr(point, "lon", None),
                    "value": getattr(point, "value", None),
                    "distance_km": getattr(point, "distance", None),
                }
            finally:
                eccodes.codes_release(gid)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Windows can hold the handle briefly; harmless for a feasibility test.


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def needed_forecast_hours(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime) -> dict:
    """Same two-flag coverage concept as data/hrrr.py, adapted to GFS's
    run-independent forecast-hour schedule (available_forecast_hours())."""
    fh_schedule = available_forecast_hours()
    needed_valid_times = []
    t = day_start_utc
    while t < day_end_utc:
        needed_valid_times.append(t)
        t += timedelta(hours=1)

    available = {run_dt + timedelta(hours=fh): fh for fh in fh_schedule}
    covered = [vt for vt in needed_valid_times if vt in available]
    forecast_hours = sorted(available[vt] for vt in covered)

    needed_from_own_start = [vt for vt in needed_valid_times if vt >= run_dt]
    full_day_coverage = len(covered) == len(needed_valid_times)
    adequate_coverage = len(covered) == len(needed_from_own_start) and len(needed_from_own_start) > 0

    return {
        "run_dt": run_dt,
        "needed_hours_total": len(needed_valid_times),
        "covered_hours": len(covered),
        "forecast_hours": forecast_hours,
        "full_day_coverage": full_day_coverage,
        "adequate_coverage": adequate_coverage,
    }
