"""Shared helpers for reading NOAA's public GEFS archive on AWS S3.

Source (verified live 2025-09-26): s3://noaa-gefs-pds, fully public --
confirmed by listing/fetching objects over plain HTTPS with no AWS
credentials, boto3, or SNS subscription required.
Registry: https://registry.opendata.aws/noaa-gefs/
Docs:     https://github.com/awslabs/open-data-docs/tree/main/docs/noaa/noaa-gefs-pds

This module only READS the archive -- it never bulk-downloads a full
global ensemble file. It does not touch Kalshi/HRRR/GFS data, Jev, Laya,
or any trading logic.

Structure (OBSERVED FROM ARCHIVE by listing/HEAD-probing real objects for
2025-07-01, NOT assumed from memory or from modern-day GEFS documentation
applied retroactively):
  - 4 runs/day: 00Z, 06Z, 12Z, 18Z.
  - Products under atmos/: pgrb2ap5 (0.5deg, common fields), pgrb2bp5
    (0.5deg, less-common fields), pgrb2sp25 (0.25deg, compact "small"
    subset -- confirmed to contain every variable this test needs, in
    only 38 messages per member/hour vs HRRR's 173 or GFS's 743).
  - 31 members: gec00 (control) + gep01..gep30 (30 perturbed). gep31
    confirmed absent (404). geavg (ensemble mean) and gespr (ensemble
    spread) also exist as NOAA-precomputed products but are NOT members
    -- excluded from MEMBERS below.
  - Forecast hours: uniform 3-hourly steps, F000 through F240 (10 days),
    for every member and every run hour tested (f001/f002 absent, f003
    present, f240 present, f243 absent).
  - Bonus: TMAX/TMIN 2m fields are directly available per 3-6h window
    (not used in this test's method, which follows the same
    instantaneous-sample-then-max approach as the HRRR/GFS tests for
    methodological consistency, but noted for a future design decision).
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-gefs-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

CONTROL_MEMBER = "gec00"
PERTURBED_MEMBERS = [f"gep{i:02d}" for i in range(1, 31)]
MEMBERS = [CONTROL_MEMBER] + PERTURBED_MEMBERS  # 31 total
EXPECTED_MEMBER_COUNT = len(MEMBERS)

RUN_HOURS = {0, 6, 12, 18}
FORECAST_STEP_HOURS = 3
MAX_FORECAST_HOUR = 240

# Independently derived (production-readiness validation, 2026-09-28) from
# GEFS's own pgrb2sp25 grid geometry: regular_ll, Ni=1440, Nj=721, 0.25deg
# (md5GridSection=45f3a4a8af23f33a77ab669d0fa1d813, confirmed via direct
# decode of a real message) -- worst-case distance from any point inside a
# 0.25deg cell to its center is 0.5*sqrt(Dx^2+Dy^2), Dy=27.75km,
# Dx=27.75km*cos(40.7deg)=21.0km at NYC's latitude -> ~17.4km theoretical
# worst case, 20km chosen. This happens to equal GFS's own value because
# both sources use the exact same NCEP 0.25deg global lat/lon grid (IDENTICAL
# md5GridSection to GFS's -- confirmed, not assumed) -- NOT inherited without
# independent verification.
MAX_PATCH_DISTANCE_KM = 20.0


def parse_forecast_desc(desc: str) -> dict:
    """Parse a GEFS idx forecast_desc into {kind, start_hour, end_hour}.

    GEFS's pgrb2sp25 product (validated 2026-09-28 across 5 dates, all 4 run
    hours, control + perturbed members, forecast hours 0-240) has EXACTLY ONE
    candidate per (variable, level) at every forecast hour -- no duplicate
    products to disambiguate, unlike HRRR/GFS/NBM's APCP. The only thing that
    varies is the accumulation/average WINDOW, which must be parsed from the
    text (never assumed from a formula), and F0 uses 'anl' like GFS's
    analysis-time format:
      'anl'                    -> instant, window=0 (F0 only; instant fields)
      'N hour fcst'             -> instant snapshot at hour N (F>=1)
      'A-B hour acc fcst'       -> accumulation over [A,B]
      'A-B hour ave fcst'       -> average over [A,B]
      'A-B hour max fcst'       -> period max over [A,B] (TMAX)
      'A-B hour min fcst'       -> period min over [A,B] (TMIN)
      '0-0 day max/min fcst'    -> DEGENERATE zero-width period, F0 TMAX/TMIN
                                    only (stepType=max/min, lengthOfTimeRange=0
                                    -- confirmed via direct decode: a real,
                                    decodable value equal to the instantaneous
                                    F0 reading, NOT a genuine period max/min).
                                    Production code explicitly skips TMAX/TMIN
                                    at forecast_hour=0 rather than fetch this
                                    (same convention as HRRR's/GFS's other
                                    degenerate-F0 accumulation fields).
    Empirically confirmed window rule (not hardcoded, just informative): for
    FH<=6 the window is [0,FH] (cumulative since run start); for FH>6 it
    resets every 6h synoptic mark, window=[6*floor((FH-1)/6), FH] (3h or 6h
    wide). Production code parses the observed text directly rather than
    relying on this formula."""
    desc = desc.strip()
    if desc == "anl":
        return {"kind": "instant", "start_hour": None, "end_hour": 0}
    m = re.match(r"^(\d+) hour fcst$", desc)
    if m:
        return {"kind": "instant", "start_hour": None, "end_hour": int(m.group(1))}
    m = re.match(r"^(\d+)-(\d+) hour (acc|ave|max|min) fcst$", desc)
    if m:
        start_hour, end_hour, kind_code = int(m.group(1)), int(m.group(2)), m.group(3)
        kind = {"acc": "accum", "ave": "average", "max": "max", "min": "min"}[kind_code]
        return {"kind": kind, "start_hour": start_hour, "end_hour": end_hour}
    m = re.match(r"^(\d+)-(\d+) day (max|min) fcst$", desc)
    if m:
        kind = {"max": "max", "min": "min"}[m.group(3)]
        return {"kind": kind, "start_hour": int(m.group(1)) * 24, "end_hour": int(m.group(2)) * 24, "degenerate_zero_width": m.group(1) == m.group(2)}
    raise RuntimeError(f"Unrecognized GEFS forecast_desc format: {desc!r}")

NYC_TZ = ZoneInfo("America/New_York")


def member_type(member_id: str) -> str:
    return "control" if member_id == CONTROL_MEMBER else "perturbed"


def available_forecast_hours() -> list[int]:
    return list(range(0, MAX_FORECAST_HOUR + 1, FORECAST_STEP_HOURS))


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def grib_key(run_date: datetime, run_hour: int, member: str, forecast_hour: int, product: str = "pgrb2sp25", variant: str = "pgrb2s.0p25") -> str:
    return f"gefs.{run_date:%Y%m%d}/{run_hour:02d}/atmos/{product}/{member}.t{run_hour:02d}z.{variant}.f{forecast_hour:03d}"


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
    """Fetch and parse a wgrib2-style .idx file. GEFS idx lines carry an
    extra trailing 'ENS=+N' tag after forecast_desc; our generic
    colon-split parser already tolerates this the same way it tolerates
    HRRR/GFS's trailing empty field (we only ever read parts[0:6])."""
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
                perturbation_number = None
                try:
                    perturbation_number = eccodes.codes_get(gid, "perturbationNumber")
                except Exception:
                    pass

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

    # For a 3-hourly-only model, "full coverage" of every UTC HOUR of the
    # day is structurally impossible (only every 3rd hour can ever be
    # covered) -- so we define coverage relative to the hours GEFS could
    # ever produce, not the impossible ideal of all 24 hourly marks.
    all_possible_hours_in_day = [vt for vt in needed_valid_times if int((vt - run_dt).total_seconds()) % (FORECAST_STEP_HOURS * 3600) == 0]
    needed_from_own_start = [vt for vt in all_possible_hours_in_day if vt >= run_dt]
    full_day_coverage = len(covered) == len(all_possible_hours_in_day) and len(all_possible_hours_in_day) > 0
    adequate_coverage = len(covered) == len(needed_from_own_start) and len(needed_from_own_start) > 0

    return {
        "run_dt": run_dt,
        "needed_hours_total": len(needed_valid_times),
        "possible_3hourly_marks": len(all_possible_hours_in_day),
        "covered_hours": len(covered),
        "forecast_hours": forecast_hours,
        "full_day_coverage": full_day_coverage,
        "adequate_coverage": adequate_coverage,
    }
