"""Shared helpers for reading NOAA's public National Blend of Models (NBM)
archive on AWS S3.

WHAT NBM IS (DOCUMENTED, from NOAA/MDL's own documentation, not memory --
https://vlab.noaa.gov/web/mdl/nbm and https://nomads.ncep.noaa.gov/txt_descriptions/BLEND_txt.html):
NBM is NOT a raw numerical weather model. It is NOAA's statistically
post-processed, BLENDED guidance product that combines multiple NWP model
and ensemble inputs (GFS, GEFS, HRRR/RRFS, ECMWF-via-exchange, Canadian, and
others) using bias-correction (decaying-average, quantile mapping) and
ensemble-weighting techniques to produce a single calibrated deterministic
grid PLUS limited post-processed uncertainty information. It sits one
layer above raw model output and one layer below a fully custom
probability model like this project's eventual Laya.

Source (verified live 2025-09-26): s3://noaa-nbm-grib2-pds, fully public
HTTPS, no AWS credentials required.
Registry: https://registry.opendata.aws/noaa-nbm/

Structure (OBSERVED FROM ARCHIVE, verified live for 2025-06-30/07-01):
  - Runs HOURLY (00Z-23Z all present) -- MORE frequent than GFS/GEFS (4/day)
    and matching HRRR's hourly cadence.
  - Products per run (under blend.{YYYYMMDD}/{HH}/): "core" (deterministic
    grids + a single ensemble-std-dev field per variable -- NOT a full
    percentile distribution), "qmd" (Quantile Mapped Distribution --
    CONFIRMED to cover ONLY precipitation/wind-gust/wind percentiles, NOT
    temperature -- this is an important, non-assumed finding), "text"
    (bulletin text products, not used here).
  - Region "co" = CONUS (also ak/hi/pr/gu/oc for other domains).
  - Forecast-hour schedule (OBSERVED via HEAD probes): hourly F001-F036,
    then 3-hourly F039-F192, then 6-hourly F198-F264 (max horizon 264h/11 days).
  - Grid: Lambert Conformal (same NDFD-based ~2.5km CONUS grid family as
    HRRR's grid, confirmed via eccodes gridType='lambert').
  - .idx sidecars present for every GRIB2 file -- same byte-range technique
    as HRRR/GFS/GEFS works identically here.
  - TMAX/TMIN are 12-hour PERIOD max/min fields anchored to the run's own
    cycle-relative hours (e.g. a 12Z run: F012="0-12 hour max", F024="12-24
    hour min", F036="24-36 hour max", ...) -- NOT aligned to any local
    calendar day. Reconstructing a specific local day's max therefore
    requires either (a) picking whichever period-window(s) happen to
    overlap that day, or (b) building it from hourly TMP:2m the same way
    as for HRRR/GFS/GEFS. Both are computed in the test script; they are
    NOT assumed to agree.
  - Historical depth (OBSERVED): data exists for blend.20210101/ and
    blend.20220101/ and blend.20230101/; blend.20200101/ returns zero keys
    -- archive depth is at least back to 2021, not to 2020-01-01.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-nbm-grib2-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

NYC_TZ = ZoneInfo("America/New_York")

# OBSERVED FROM ARCHIVE (HEAD probes against 2025-07-01 12Z core CONUS files).
HOURLY_MAX_FH = 36
MID_STEP_HOURS = 3
MID_MAX_FH = 192
LONG_STEP_HOURS = 6
MAX_FORECAST_HOUR = 264


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def available_forecast_hours() -> list[int]:
    hours = list(range(1, HOURLY_MAX_FH + 1))
    hours += list(range(HOURLY_MAX_FH + MID_STEP_HOURS, MID_MAX_FH + 1, MID_STEP_HOURS))
    hours += list(range(MID_MAX_FH + LONG_STEP_HOURS, MAX_FORECAST_HOUR + 1, LONG_STEP_HOURS))
    return hours


def grib_key(run_date: datetime, run_hour: int, forecast_hour: int, product: str = "core", region: str = "co") -> str:
    return f"blend.{run_date:%Y%m%d}/{run_hour:02d}/{product}/blend.t{run_hour:02d}z.{product}.f{forecast_hour:03d}.{region}.grib2"


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
                # NBM idx lines sometimes carry a trailing qualifier after a
                # further colon (e.g. "...:24 hour fcst:ens std dev") --
                # joining everything from parts[5] onward preserves it
                # instead of silently dropping it (a real bug caught while
                # testing: dropping this made every "ens std dev" lookup
                # fail even though the message was genuinely present).
                "forecast_desc": ":".join(parts[5:]),
            }
        )
    return entries


def find_message(entries: list[dict], variable: str, level: str, desc_contains: str | None = None, desc_excludes: str | None = None) -> dict | None:
    for i, e in enumerate(entries):
        if e["variable"] != variable or e["level"] != level:
            continue
        if desc_contains and desc_contains not in e["forecast_desc"]:
            continue
        if desc_excludes and desc_excludes in e["forecast_desc"]:
            continue
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
                units = eccodes.codes_get(gid, "units")
                forecast_hour = eccodes.codes_get(gid, "forecastTime")
                grid_type = eccodes.codes_get(gid, "gridType")
                nearest = eccodes.codes_grib_find_nearest(gid, requested_lat, requested_lon)
                point = nearest[0] if isinstance(nearest, (list, tuple)) else nearest
                return {
                    "variable": variable,
                    "units": units,
                    "forecast_hour": forecast_hour,
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
            pass


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def needed_forecast_hours(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime) -> dict:
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
