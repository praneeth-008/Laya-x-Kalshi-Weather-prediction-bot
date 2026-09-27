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

import re
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

# GFS's 0.25deg regular_ll grid is far coarser than HRRR's ~3km Lambert grid,
# so the distance sanity-check ceiling used by decode_message_multi_point
# must be derived from GFS's OWN grid geometry, not inherited from HRRR.
# DERIVATION (verified against a real decoded message: gridType=regular_ll,
# Ni=1440, Nj=721, 0.25deg spacing in both dimensions): at Central Park's
# latitude (~40.78N), one grid cell measures ~27.75km (lat direction,
# latitude-independent) x ~21.0km (lon direction, scaled by cos(40.78deg)).
# The theoretical WORST CASE nearest-grid-point distance for any query point
# is half the cell diagonal: 0.5*sqrt(27.75**2 + 21.0**2) = ~17.4km. 20km
# gives a deliberate, but not excessive, margin above that theoretical worst
# case (~15%) -- tight enough that a genuinely wrong grid (e.g. a mismatched
# projection or a stale/corrupt fingerprint) mapping to a point tens of km
# away would still be caught, while never rejecting a legitimately correct
# GFS grid mapping. This is a SAFETY CEILING on plausibility, not a mechanism
# for choosing between candidate grid points -- see data/pilot_extraction.py.
MAX_PATCH_DISTANCE_KM = 20.0


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
    """First matching message. Safe ONLY for (variable, level) pairs known to
    be unique in GFS's pgrb2.0p25 idx (verified empirically for every
    CORE_VARS entry except APCP and TCDC). For APCP and TCDC specifically,
    GFS publishes multiple distinct products under the identical
    (variable, level) key -- use select_message_explicit() for those, which
    never relies on idx ordering. See docs/gfs_pilot_readiness.md."""
    for i, e in enumerate(entries):
        if e["variable"] == variable and e["level"] == level:
            byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
            return {**e, "byte_end": byte_end}
    return None


_FORECAST_DESC_INSTANT = re.compile(r"^(\d+) hour fcst$")
_FORECAST_DESC_WINDOW_HOUR = re.compile(r"^(\d+)-(\d+) hour (acc|ave) fcst$")
_FORECAST_DESC_WINDOW_DAY = re.compile(r"^(\d+)-(\d+) day (acc|ave) fcst$")


def parse_forecast_desc(desc: str) -> dict:
    """Parse a GFS .idx forecast_desc field into a structured descriptor:
    {"kind": "instant" | "accum" | "average", "start_hour": int | None, "end_hour": int}.
    start_hour is None for "instant" (a single instantaneous valid time, no
    window). Raises ValueError for any format not explicitly recognized --
    this function NEVER guesses at an unfamiliar format, matching the
    project's existing policy of raising rather than silently continuing
    when something isn't understood."""
    desc = desc.strip()
    if desc == "anl":
        # "anl" (analysis) is GFS's forecast_desc for the model's initial
        # state -- i.e. forecast_hour=0, before any forecast projection.
        # Functionally an instantaneous value at end_hour=0, same as any
        # other "N hour fcst" instant entry; GFS's own convention never uses
        # "anl" for any forecast_hour other than 0, so this is not a guess.
        # Only instantaneous fields have an "anl" entry -- APCP/DSWRF (both
        # accumulation-based) have NO entry at all at forecast_hour=0
        # (verified empirically), which find_message()/select_message_explicit()
        # already handle gracefully by returning None (that variable is
        # legitimately absent for that one work item, not a failure). See
        # docs/gfs_pilot_readiness.md.
        return {"kind": "instant", "start_hour": None, "end_hour": 0}
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
    raise ValueError(f"Unrecognized GFS forecast_desc format: {desc!r}")


def select_message_explicit(entries: list[dict], variable: str, level: str, forecast_hour: int, prefer: str) -> dict | None:
    """Explicit, documented product selection for GFS variables that publish
    MULTIPLE distinct products under one (variable, level) idx key -- never
    relies on idx ordering, unlike find_message().

    prefer:
      "instant"         -- select the single instantaneous-valid-time entry
                            (used for TCDC: preserves an instantaneous
                            atmospheric-state interpretation, consistent with
                            HRRR's own instantaneous TCDC and with this
                            project's point-in-time weather_state philosophy).
      "shortest_window" -- among accumulation-type ("accum") entries whose
                            window ends at forecast_hour, select the one with
                            the SMALLEST (end_hour - start_hour) window (used
                            for APCP: this is the "how much precipitation
                            fell recently" signal the project wants, as
                            opposed to a cumulative-since-run-start total that
                            would conflate all precipitation since forecast
                            init and lose the ability to see whether
                            precipitation is recent/ongoing).

    Returns None if no candidate exists at all for (variable, level).
    Raises RuntimeError (fails loudly, with full diagnostic detail) if:
      - a candidate's forecast_desc cannot be parsed,
      - zero candidates satisfy the rule at this forecast_hour, or
      - more than one candidate satisfies it with a DIFFERENT accumulation
        window (start_hour, end_hour).
    A tie between candidates that share the exact same (start_hour, end_hour)
    is not an error: EMPIRICALLY VERIFIED (see docs/gfs_pilot_readiness.md)
    that at forecast_hour<=6, GFS's pipeline emits the [0, forecast_hour]
    accumulation as two separate GRIB messages (different byte offsets) with
    byte-for-byte identical GRIB metadata (startStep/endStep/stepRange/
    typeOfStatisticalProcessing) and identical decoded values -- a file-
    generation artifact, not a second distinct product. Any tie where the
    windows genuinely differ is a real ambiguity and still fails loudly."""
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

    if prefer == "instant":
        matches = [(e, p) for e, p in parsed if p["kind"] == "instant"]
    elif prefer == "shortest_window":
        accum = [(e, p) for e, p in parsed if p["kind"] == "accum"]
        if accum:
            min_window = min(p["end_hour"] - (p["start_hour"] or 0) for _, p in accum)
            matches = [(e, p) for e, p in accum if (p["end_hour"] - (p["start_hour"] or 0)) == min_window]
        else:
            matches = []
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
        # Same (start_hour, end_hour) window repeated across multiple GRIB
        # messages (verified empirically to carry identical values) -- not
        # an error; pick the first, they are scientifically equivalent.

    chosen_entry, chosen_parsed = matches[0]
    i = entries.index(chosen_entry)
    byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
    return {**chosen_entry, "byte_end": byte_end, "_parsed": chosen_parsed}


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
