"""Shared helpers for reading ECMWF's official AWS-hosted Open Data archive.

CRITICAL FINDING (see scripts/test_ecmwf_nyc_feasibility.py Part B2/B3 for
full discussion): ECMWF's own documentation states Open Data retains only
"the most recent 12 forecast runs (~2-3 days)". However, the S3 bucket
registered on the AWS Open Data registry as "ECMWF real-time forecasts"
(arn:aws:s3:::ecmwf-forecasts, https://registry.opendata.aws/ecmwf-forecasts/)
was OBSERVED LIVE (2025-09-26) to still contain objects from as early as
2023-01-18, including our target date 2025-07-01. This directly
CONTRADICTS the documented retention policy. This module uses that bucket
because it demonstrably works for our target date right now, but this
should NOT be treated as a documented or guaranteed capability -- a
production system depending on it should assume the older objects could
be pruned at any time without notice, since retaining them is evidently
not part of the source's stated design.

Structure (OBSERVED FROM ARCHIVE for 2025-07-01):
  s3://ecmwf-forecasts/{YYYYMMDD}/{HH}z/ifs/0p25/{stream}/{YYYYMMDD}{HH}0000-{step}h-{stream}-{suffix}.grib2
  stream="oper" (deterministic HRES, suffix "fc") and stream="enfo"
  (ensemble ENS, suffix "ef") both confirmed present for 00z and 12z only
  -- 06z/18z were OBSERVED to have zero "ifs/0p25/oper" objects for this
  date (HRES/ENS run only twice daily; 06/18Z produce a different,
  separate product not investigated here). An "aifs-single" product also
  exists alongside "ifs" -- that is ECMWF's newer AI-based forecast model,
  NOT the classic physics-based IFS this project is testing; not used here.

Each GRIB2 file bundles MANY messages (all variables/levels, and for enfo
ALL 51 members) in one huge file (enfo files observed at ~6.5 GB for a
single forecast step) -- byte-range extraction via the accompanying
".index" sidecar is not just an optimization here, it is essential.

Index format (OBSERVED, DIFFERENT from the wgrib2-style text .idx used by
HRRR/GFS/GEFS/NBM): one JSON object per line, e.g.
  {"date": "20250701", "time": "0000", "step": "24", "levtype": "sfc",
   "param": "2t", "_offset": 149489038, "_length": 1331627}
ensemble member entries additionally carry "number" (1-50) and
"type": "pf" (perturbed) vs "type": "cf" (control, no "number" field).

Parameter short names (OBSERVED, standard ECMWF/GRIB naming): 2t (2m
temp), 2d (2m dewpoint), 10u/10v (10m wind components), sp (surface
pressure), msl (mean sea level pressure), tp (total precipitation),
ssrd (surface solar radiation downward), mx2t3/mn2t3 (3-hour period
max/min 2m temp -- a DIRECT max-guidance field, analogous to NBM's TMAX
but on a rolling 3h window rather than a 12h cycle-anchored one).
"""

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "ecmwf-forecasts"
BASE_URL = f"https://{BUCKET}.s3.eu-central-1.amazonaws.com"

RUN_HOURS = {0, 12}  # OBSERVED: oper/enfo only present at 00z/12z for this date
STREAM_SUFFIX = {"oper": "fc", "enfo": "ef"}

# OBSERVED: this bucket enforces S3-level rate limiting more aggressively
# than the NOAA buckets used elsewhere in this project -- plain sequential
# byte-range requests here produced real "503 Slow Down" responses during
# this project's own testing. Retried with exponential backoff + jitter,
# same pattern already proven for data/kalshi.py's kalshi_get().
RETRYABLE_STATUS_CODES = {429, 503}
MAX_RETRIES = 6


def _backoff_delay(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)


def _get_with_retry(url: str, headers: dict | None = None, max_retries: int = MAX_RETRIES) -> requests.Response:
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == max_retries:
                raise RuntimeError(f"Request to {url} failed after {max_retries} retries: {exc}")
            time.sleep(_backoff_delay(attempt))
            continue
        if r.status_code in (200, 206):
            time.sleep(0.15)  # OBSERVED: this bucket rate-limits more readily than NOAA's -- pace requests
            return r
        if r.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            time.sleep(_backoff_delay(attempt))
            continue
        return r  # let the caller decide how to handle a non-retryable failure
    raise RuntimeError(f"Request to {url} failed with no successful response ({last_exc})")

NYC_TZ = ZoneInfo("America/New_York")


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def grib_key(run_date: datetime, run_hour: int, step: int, stream: str = "oper") -> str:
    suffix = STREAM_SUFFIX[stream]
    return f"{run_date:%Y%m%d}/{run_hour:02d}z/ifs/0p25/{stream}/{run_date:%Y%m%d}{run_hour:02d}0000-{step}h-{stream}-{suffix}.grib2"


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
    key_count = re.findall(r"<KeyCount>(.*?)</KeyCount>", r.text)
    return [{"key": k, "last_modified": m, "size": int(s)} for k, m, s in zip(keys, mods, sizes)], prefixes, (
        int(key_count[0]) if key_count else 0
    )


def fetch_index(grib_key_: str, stats: RequestStats | None = None) -> list[dict]:
    """Fetch and parse ECMWF's JSON-lines .index sidecar."""
    index_key = grib_key_[: -len(".grib2")] + ".index"
    t0 = time.time()
    r = _get_with_retry(f"{BASE_URL}/{index_key}")
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Index GET failed for {index_key}: HTTP {r.status_code}")
    entries = []
    for line in r.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def find_message(entries: list[dict], param: str, number: int | None = None, levtype: str | None = "sfc") -> dict | None:
    for e in entries:
        if e.get("param") != param:
            continue
        if levtype is not None and e.get("levtype") != levtype:
            continue
        if number is None:
            if "number" in e:
                continue  # skip ensemble members when we want the plain/control field
            return e
        else:
            if str(e.get("number")) == str(number):
                return e
    return None


def fetch_byte_range(grib_key_: str, offset: int, length: int, stats: RequestStats | None = None) -> tuple[bytes, str | None]:
    byte_end = offset + length - 1
    t0 = time.time()
    r = _get_with_retry(f"{BASE_URL}/{grib_key_}", headers={"Range": f"bytes={offset}-{byte_end}"})
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Range GET failed for {grib_key_} (bytes={offset}-{byte_end}): HTTP {r.status_code}")
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
                forecast_hour = eccodes.codes_get(gid, "step") if _has_key(gid) else eccodes.codes_get(gid, "forecastTime")
                grid_type = eccodes.codes_get(gid, "gridType")
                perturbation_number = None
                try:
                    perturbation_number = eccodes.codes_get(gid, "perturbationNumber")
                except Exception:
                    pass
                nearest = eccodes.codes_grib_find_nearest(gid, requested_lat, requested_lon)
                point = nearest[0] if isinstance(nearest, (list, tuple)) else nearest
                return {
                    "variable": variable,
                    "units": units,
                    "forecast_hour": forecast_hour,
                    "grid_type": grid_type,
                    "perturbation_number": perturbation_number,
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


def _has_key(gid) -> bool:
    import eccodes

    try:
        eccodes.codes_get(gid, "step")
        return True
    except Exception:
        return False


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def steps_for_local_day(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, available_steps: list[int]) -> list[int]:
    needed_valid_times = []
    t = day_start_utc
    while t < day_end_utc:
        needed_valid_times.append(t)
        t += timedelta(hours=1)
    available = {run_dt + timedelta(hours=s): s for s in available_steps}
    return sorted(available[vt] for vt in needed_valid_times if vt in available)
