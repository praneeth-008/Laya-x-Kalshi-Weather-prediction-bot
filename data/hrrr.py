"""Shared helpers for reading NOAA's public HRRR archive on AWS S3.

Source (verified live 2025-09-26): s3://noaa-hrrr-bdp-pds, fully public --
confirmed by listing/fetching objects over plain HTTPS with no AWS
credentials, SNS subscription, or boto3 required.
Registry: https://registry.opendata.aws/noaa-hrrr-pds/
Docs:     https://github.com/awslabs/open-data-docs/tree/main/docs/noaa/noaa-hrrr

This module only READS the archive (list objects, parse .idx sidecars,
byte-range GET single GRIB2 messages) -- it never bulk-downloads a full
national GRIB2 file. It does not touch Kalshi data, Jev, Laya, or any
trading logic.

Run-length schedule (OBSERVED FROM ARCHIVE by listing real objects for
2025-06-30/2025-07-01, consistent with HRRR's documented extended-run
design): the four "synoptic" init hours (00/06/12/18Z) produce forecast
hours F00-F48; every other hourly init produces only F00-F18.
"""

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-hrrr-bdp-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

EXTENDED_RUN_HOURS = {0, 6, 12, 18}
EXTENDED_MAX_FH = 48
STANDARD_MAX_FH = 18

NYC_TZ = ZoneInfo("America/New_York")

# HRRR's own distance sanity-check ceiling for decode_message_multi_point,
# named explicitly here (matching data.gfs.MAX_PATCH_DISTANCE_KM's pattern)
# rather than relying only on that function's shared default. Unchanged
# value/derivation from the original grid-cache work: ~3x HRRR's measured
# ~1km nearest-grid-point distance / 3km native grid spacing.
MAX_PATCH_DISTANCE_KM = 10.0


def max_forecast_hour(run_hour: int) -> int:
    return EXTENDED_MAX_FH if run_hour in EXTENDED_RUN_HOURS else STANDARD_MAX_FH


@dataclass
class RequestStats:
    """Tracks exactly what Part 6 of the feasibility test asks to measure."""

    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def grib_key(run_date: datetime, run_hour: int, forecast_hour: int, product: str = "wrfsfc", region: str = "conus") -> str:
    return f"hrrr.{run_date:%Y%m%d}/{region}/hrrr.t{run_hour:02d}z.{product}f{forecast_hour:02d}.grib2"


def list_objects(prefix: str, max_keys: int = 1000) -> list[dict]:
    """List objects under a prefix via the bucket's public (unauthenticated)
    ListObjectsV2 REST API -- works over plain HTTPS, no boto3 needed."""
    import re

    r = requests.get(
        f"{BASE_URL}/",
        params={"list-type": "2", "prefix": prefix, "max-keys": max_keys},
        timeout=30,
    )
    r.raise_for_status()
    keys = re.findall(r"<Key>(.*?)</Key>", r.text)
    mods = re.findall(r"<LastModified>(.*?)</LastModified>", r.text)
    sizes = re.findall(r"<Size>(.*?)</Size>", r.text)
    return [{"key": k, "last_modified": m, "size": int(s)} for k, m, s in zip(keys, mods, sizes)]


def fetch_idx(grib_key_: str, stats: RequestStats | None = None) -> list[dict]:
    """Fetch and parse a wgrib2-style .idx sidecar file.

    Each line: N:BYTE_OFFSET:d=YYYYMMDDHH:VARIABLE:LEVEL:FORECAST_DESC:
    """
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
    """Locate one variable's message in a parsed .idx list and compute its
    byte range (end = next message's start - 1, or None for "to EOF").
    Safe ONLY for (variable, level) pairs known to be unique in HRRR's idx
    (verified empirically for every CORE_VARS entry except APCP -- HRRR
    always publishes exactly two APCP:surface candidates from forecast_hour
    2 onward: a cumulative-since-run-start total and a direct 1-hour
    windowed accumulation). For APCP, use select_message_explicit(), which
    never relies on idx ordering. See docs/gfs_pilot_readiness.md for the
    GFS analog and docs/hrrr_apcp_backfill.md for HRRR's own investigation."""
    for i, e in enumerate(entries):
        if e["variable"] == variable and e["level"] == level:
            byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
            return {**e, "byte_end": byte_end}
    return None


_FORECAST_DESC_INSTANT = re.compile(r"^(\d+) hour fcst$")
_FORECAST_DESC_WINDOW_HOUR = re.compile(r"^(\d+)-(\d+) hour (acc|ave) fcst$")
_FORECAST_DESC_WINDOW_DAY = re.compile(r"^(\d+)-(\d+) day (acc|ave) fcst$")


def parse_forecast_desc(desc: str) -> dict:
    """Parse an HRRR .idx forecast_desc field into a structured descriptor:
    {"kind": "instant" | "accum" | "average", "start_hour": int | None, "end_hour": int}.
    Same format/parser as data/gfs.py's (duplicated per this project's
    existing per-source-module convention -- each source owns its own
    fetch_idx/find_message primitives, unchanged elsewhere). Raises
    ValueError for any unrecognized format -- never guesses."""
    desc = desc.strip()
    m = _FORECAST_DESC_INSTANT.match(desc)
    if m:
        return {"kind": "instant", "start_hour": None, "end_hour": int(m.group(1))}
    m = _FORECAST_DESC_WINDOW_HOUR.match(desc)
    if m:
        start, end, kind = int(m.group(1)), int(m.group(2)), m.group(3)
        return {"kind": "accum" if kind == "acc" else "average", "start_hour": start, "end_hour": end}
    m = _FORECAST_DESC_WINDOW_DAY.match(desc)
    if m:
        start, end, kind = int(m.group(1)) * 24, int(m.group(2)) * 24, m.group(3)
        return {"kind": "accum" if kind == "acc" else "average", "start_hour": start, "end_hour": end}
    raise ValueError(f"Unrecognized HRRR forecast_desc format: {desc!r}")


def select_message_explicit(entries: list[dict], variable: str, level: str, forecast_hour: int, prefer: str) -> dict | None:
    """Explicit, documented product selection for HRRR's APCP -- never
    relies on idx ordering, unlike find_message(). See
    docs/hrrr_apcp_backfill.md for the full investigation this codifies.

    prefer:
      "cumulative_since_start" -- select the candidate whose accumulation
                                  window starts at hour 0 (the quantity
                                  already persisted in the frozen pilot as
                                  "APCP").
      "windowed_1h"             -- select the candidate whose accumulation
                                  window is exactly 1 hour ending at
                                  forecast_hour (HRRR's direct NOAA 1-hour
                                  product -- the "APCP_1H" feature; NEVER
                                  reconstructed by differencing cumulative
                                  values, which was empirically shown to be
                                  inexact in ~47% of tested cases).

    At forecast_hour == 1, HRRR publishes only ONE APCP candidate, whose
    window is simultaneously [0,1] (satisfies "cumulative_since_start") and
    exactly 1 hour long (satisfies "windowed_1h") -- both prefer values
    correctly resolve to this SAME message, since the two definitions
    describe the same physical interval at this one forecast hour. This is
    intentional, not a special case in the code.

    Returns None if no candidate exists at all for (variable, level).
    Raises RuntimeError (fails loudly) if a candidate's forecast_desc can't
    be parsed, if zero candidates satisfy the rule at this forecast_hour, or
    if more than one candidate satisfies it with a DIFFERENT accumulation
    window (a genuine, unexpected ambiguity)."""
    candidates = [e for e in entries if e["variable"] == variable and e["level"] == level]
    if not candidates:
        return None

    parsed = []
    for e in candidates:
        try:
            p = parse_forecast_desc(e["forecast_desc"])
        except ValueError as exc:
            raise RuntimeError(
                f"select_message_explicit: cannot parse forecast_desc for {variable}:{level} "
                f"(forecast_hour={forecast_hour}): {exc}. All candidates: "
                f"{[c['forecast_desc'] for c in candidates]}"
            ) from exc
        if p["end_hour"] == forecast_hour:
            parsed.append((e, p))

    if prefer == "cumulative_since_start":
        matches = [(e, p) for e, p in parsed if p["kind"] == "accum" and (p["start_hour"] or 0) == 0]
    elif prefer == "windowed_1h":
        matches = [(e, p) for e, p in parsed if p["kind"] == "accum" and p["end_hour"] - (p["start_hour"] or 0) == 1]
    else:
        raise ValueError(f"select_message_explicit: unknown prefer strategy {prefer!r}")

    if not matches:
        raise RuntimeError(
            f"select_message_explicit: NO {variable}:{level} candidate satisfies rule "
            f"prefer={prefer!r} at forecast_hour={forecast_hour}. All candidates seen: "
            f"{[c['forecast_desc'] for c in candidates]}"
        )
    if len(matches) > 1:
        windows = {(p["start_hour"], p["end_hour"]) for _, p in matches}
        if len(windows) > 1:
            raise RuntimeError(
                f"select_message_explicit: AMBIGUOUS -- {len(matches)} candidates for {variable}:{level} "
                f"satisfy rule prefer={prefer!r} at forecast_hour={forecast_hour} with DIFFERING "
                f"accumulation windows: {[e['forecast_desc'] for e, _ in matches]}"
            )
        # Same (start_hour, end_hour) window repeated -- not an error.

    chosen_entry, chosen_parsed = matches[0]
    i = entries.index(chosen_entry)
    byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
    return {**chosen_entry, "byte_end": byte_end, "_parsed": chosen_parsed}


def fetch_byte_range(grib_key_: str, byte_start: int, byte_end: int | None, stats: RequestStats | None = None) -> tuple[bytes, str | None]:
    """HTTP Range GET for one GRIB2 message. Returns (bytes, Last-Modified
    header of the underlying object) -- the Last-Modified header is our
    best OBSERVED-FROM-ARCHIVE proxy for when this file was published
    (see Part 2 of the feasibility report; it is NOT a documented
    guarantee of "availability time")."""
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
    """Decode one standalone GRIB2 message (a byte-range slice IS a valid,
    self-contained GRIB2 file since each message carries its own grid
    definition section) and extract the nearest-gridpoint value.

    Uses eccodes' own codes_grib_find_nearest -- it handles HRRR's Lambert
    Conformal projection correctly without us reimplementing map math.
    """
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
                data_date = eccodes.codes_get(gid, "dataDate")  # YYYYMMDD (int)
                data_time = eccodes.codes_get(gid, "dataTime")  # HHMM (int), e.g. 1200

                nearest = eccodes.codes_grib_find_nearest(gid, requested_lat, requested_lon)
                point = nearest[0] if isinstance(nearest, (list, tuple)) else nearest
                grid_lat = getattr(point, "lat", None)
                grid_lon = getattr(point, "lon", None)
                value = getattr(point, "value", None)
                distance_km = getattr(point, "distance", None)

                return {
                    "variable": variable,
                    "level": level,
                    "level_type": level_type,
                    "units": units,
                    "forecast_hour": forecast_hour,
                    "data_date": data_date,
                    "data_time": data_time,
                    "grid_lat": grid_lat,
                    "grid_lon": grid_lon,
                    "value": value,
                    "distance_km": distance_km,
                }
            finally:
                eccodes.codes_release(gid)
    finally:
        # Best-effort cleanup: on Windows, eccodes' C library can hold the
        # file handle open slightly longer than the Python `with` block,
        # so an immediate unlink can raise PermissionError. Leaving a
        # stray temp file is harmless for this feasibility test.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    """UTC [start, end) bounds of one local calendar day (e.g. NYC)."""
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def needed_forecast_hours(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime) -> dict:
    """For one HRRR run, determine which forecast hours are needed to
    cover a target local calendar day, and whether coverage is complete.

    Two coverage concepts, both reported (never conflated):
      full_day_coverage:  every one of the day's 24 UTC-hourly valid times
                           is available from this run (the strict spec).
      adequate_coverage:  every valid time from max(run_dt, day_start)
                           onward is available -- i.e. the run covers
                           everything it COULD possibly have forecast,
                           only missing hours that are structurally in the
                           past relative to its own initialization.
    """
    max_fh = max_forecast_hour(run_dt.hour)
    needed_valid_times = []
    t = day_start_utc
    while t < day_end_utc:
        needed_valid_times.append(t)
        t += timedelta(hours=1)

    available = {run_dt + timedelta(hours=fh) for fh in range(0, max_fh + 1)}
    covered = [vt for vt in needed_valid_times if vt in available]
    forecast_hours = sorted(int((vt - run_dt).total_seconds() // 3600) for vt in covered)

    needed_from_own_start = [vt for vt in needed_valid_times if vt >= run_dt]
    full_day_coverage = len(covered) == len(needed_valid_times)
    adequate_coverage = len(covered) == len(needed_from_own_start) and len(needed_from_own_start) > 0

    return {
        "run_dt": run_dt,
        "max_fh": max_fh,
        "needed_hours_total": len(needed_valid_times),
        "covered_hours": len(covered),
        "forecast_hours": forecast_hours,
        "full_day_coverage": full_day_coverage,
        "adequate_coverage": adequate_coverage,
    }
