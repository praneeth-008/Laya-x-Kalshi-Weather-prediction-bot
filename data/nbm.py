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

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-nbm-grib2-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

NYC_TZ = ZoneInfo("America/New_York")

# OBSERVED FROM ARCHIVE (HEAD probes + real idx fetches against 2025-06-06 and
# other 2025 dates, during NBM production-readiness validation -- see
# docs/nbm_pilot_readiness.md). CORRECTS an earlier feasibility-only
# assumption that every run shares one fixed schedule to F264: the schedule
# is actually RUN-HOUR DEPENDENT, keyed by run_hour % 6, analogous to (but a
# finer 3-tier version of) HRRR's EXTENDED_RUN_HOURS split:
#   run_hour % 6 in (0, 1): FULL   -- hourly F1-F36, 3-hourly F39-F192, 6-hourly F198-F264
#   run_hour % 6 == 3:      MEDIUM -- hourly F1-F36, 3-hourly F39-F189 (no 6-hourly extension, stops at F189)
#   run_hour % 6 in (2,4,5):SHORT  -- hourly F1-F36 only
# F0 does NOT exist for NBM's core product at any run hour (confirmed: 404
# for every run_hour/date tested) -- unlike HRRR/GFS, there is no degenerate
# F0 message to special-case; it is simply absent from the schedule.
HOURLY_MAX_FH = 36
MID_STEP_HOURS = 3
MID_MAX_FH_FULL = 192
MID_MAX_FH_MEDIUM = 189
LONG_STEP_HOURS = 6
MAX_FORECAST_HOUR_FULL = 264

# TMAX/TMIN 12-hour period products exist ONLY for run_hour in (0, 12) --
# CONFIRMED empirically across all 8 run hours that otherwise share the FULL
# forecast-hour tier (0,1,6,7,12,13,18,19): only 0Z and 12Z carry any
# TMAX/TMIN messages at all. Which of a run's 12-hour periods is "max" vs
# "min" depends on which one covers local afternoon vs. overnight, which
# flips between the 0Z and 12Z cases (0Z's first period [0,12) is
# overnight-into-morning -> TMIN; 12Z's first period [0,12) is
# afternoon-into-evening -> TMAX) -- alternating every 12h thereafter.
TMAX_TMIN_RUN_HOURS = {0, 12}


def nbm_schedule_tier(run_hour: int) -> str:
    """Returns 'full', 'medium', or 'short' -- see the module-level schedule
    note above. Never guesses: only these three tiers have been empirically
    observed; any other run_hour%6 residue is impossible (0-5 covers all
    cases) so this function cannot silently fall through."""
    r = run_hour % 6
    if r in (0, 1):
        return "full"
    if r == 3:
        return "medium"
    return "short"  # r in (2, 4, 5)


def available_forecast_hours(run_hour: int) -> list[int]:
    """Run-hour-aware forecast-hour schedule. NOTE: data/weather_state.py's
    _forecast_hour_candidates() for 'nbm' does NOT currently call this and
    instead hardcodes the FULL-tier schedule for every run hour -- a known,
    reported (not silently fixed) gap, see docs/nbm_pilot_readiness.md. This
    function is the corrected, validated schedule for the extraction path."""
    tier = nbm_schedule_tier(run_hour)
    hours = list(range(1, HOURLY_MAX_FH + 1))
    if tier == "short":
        return hours
    if tier == "medium":
        hours += list(range(HOURLY_MAX_FH + MID_STEP_HOURS, MID_MAX_FH_MEDIUM + 1, MID_STEP_HOURS))
        return hours
    # tier == "full"
    hours += list(range(HOURLY_MAX_FH + MID_STEP_HOURS, MID_MAX_FH_FULL + 1, MID_STEP_HOURS))
    hours += list(range(MID_MAX_FH_FULL + LONG_STEP_HOURS, MAX_FORECAST_HOUR_FULL + 1, LONG_STEP_HOURS))
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


_FORECAST_DESC_INSTANT = re.compile(r"^(\d+) hour fcst$")
_FORECAST_DESC_WINDOW = re.compile(r"^(\d+)-(\d+) hour (acc|ave|max|min) fcst$")

# NBM's own distance sanity-check ceiling, derived from its OWN grid geometry
# (Lambert Conformal, Dx=Dy=2539.703m, confirmed via a real decoded message --
# see docs/nbm_pilot_readiness.md). Theoretical worst-case nearest-point
# distance for an equal-Dx/Dy Lambert grid is half the cell diagonal:
# 0.5*sqrt(2539.703^2 + 2539.703^2) ~= 1.796km. 5km gives a deliberate
# ~2.8x margin -- tighter than both HRRR's 10km and GFS's 20km (NBM's grid is
# the finest of the three, ~2.5km vs HRRR's ~3km and GFS's ~28km), and NOT
# inherited from either without this independent derivation.
MAX_PATCH_DISTANCE_KM = 5.0


def parse_forecast_desc(desc: str) -> dict:
    """Parse an NBM .idx forecast_desc field. NBM's format differs from
    HRRR/GFS's: it always has a trailing ':' and may carry further qualifier
    segments after it (e.g. 'ens std dev', or 'prob >0.254:prob fcst 255/255'
    for probability-of-exceedance products) -- see fetch_idx()'s own comment
    on this. Only the FIRST segment (before the first ':') carries temporal
    meaning; returns {"kind": ..., "start_hour": int|None, "end_hour": int,
    "qualifier": str} where qualifier is everything after the first ':'
    (empty string if none). Raises ValueError if the first segment doesn't
    match a recognized temporal pattern -- never guesses."""
    core, _, qualifier = desc.partition(":")
    core = core.strip()
    m = _FORECAST_DESC_INSTANT.match(core)
    if m:
        return {"kind": "instant", "start_hour": None, "end_hour": int(m.group(1)), "qualifier": qualifier}
    m = _FORECAST_DESC_WINDOW.match(core)
    if m:
        start, end, kind_code = int(m.group(1)), int(m.group(2)), m.group(3)
        kind = {"acc": "accum", "ave": "average", "max": "max", "min": "min"}[kind_code]
        return {"kind": kind, "start_hour": start, "end_hour": end, "qualifier": qualifier}
    raise ValueError(f"Unrecognized NBM forecast_desc format: {desc!r}")


def select_message_explicit(entries: list[dict], variable: str, level: str, forecast_hour: int, prefer: str) -> dict | None:
    """Explicit, documented product selection for NBM variables that publish
    multiple candidates under one (variable, level) idx key -- never relies
    on idx ordering. See docs/nbm_pilot_readiness.md for the full
    investigation this codifies.

    prefer:
      "shortest_window" -- APCP: among non-probability ("prob" not in the
                            qualifier), accum-kind candidates ending at
                            forecast_hour, select the smallest window. This
                            is the 1-hour trailing product, present at every
                            forecast_hour >= 1.
      "sixhour_window"  -- APCP: among non-probability candidates, select the
                            one whose window is exactly 6 hours (if any).
                            EMPIRICALLY VERIFIED to exist only when this
                            forecast_hour's valid_time falls on an ABSOLUTE
                            UTC synoptic hour (00/06/12/18Z) -- i.e. when
                            (run_hour + forecast_hour) % 6 == 0, NOT simply
                            forecast_hour % 6 == 0 (those coincide only for
                            runs that themselves start on a synoptic hour,
                            e.g. 12Z; a 03Z run's 6-hour windows land at
                            forecast_hour in {9,15,21,27,33,39,...}, verified
                            directly -- see docs/nbm_pilot_readiness.md).
                            This function never hardcodes that alignment --
                            it only returns whatever exactly-6-hour candidate
                            the idx genuinely contains, so it is correct
                            regardless of run_hour. Returns None when no such
                            candidate exists (a legitimate absence, not a
                            failure).
      "period_max"      -- TMAX-equivalent: the accum-kind... actually a
                            "max"-kind candidate ending at forecast_hour
                            (only exists for run_hour in TMAX_TMIN_RUN_HOURS
                            at 12-hour period boundaries).
      "period_min"      -- same for "min"-kind candidates.

    Raises RuntimeError if a candidate's forecast_desc can't be parsed, or if
    more than one candidate satisfies the rule with DIFFERING windows
    (genuine ambiguity, never silently resolved). Returns None if no
    candidate exists at all, or if none satisfy the rule (both are
    legitimate absences for NBM -- e.g. APCP simply doesn't publish a
    sixhour_window candidate except when forecast_hour%6==0)."""
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
        if "prob" in p["qualifier"]:
            continue  # probability-of-exceedance product, never a deterministic amount
        if p["end_hour"] == forecast_hour:
            parsed.append((e, p))

    if prefer == "shortest_window":
        matches_pool = [(e, p) for e, p in parsed if p["kind"] == "accum"]
        if matches_pool:
            min_window = min(p["end_hour"] - (p["start_hour"] or 0) for _, p in matches_pool)
            matches = [(e, p) for e, p in matches_pool if (p["end_hour"] - (p["start_hour"] or 0)) == min_window]
        else:
            matches = []
    elif prefer == "sixhour_window":
        matches = [(e, p) for e, p in parsed if p["kind"] == "accum" and (p["end_hour"] - (p["start_hour"] or 0)) == 6]
    elif prefer == "period_max":
        matches = [(e, p) for e, p in parsed if p["kind"] == "max"]
    elif prefer == "period_min":
        matches = [(e, p) for e, p in parsed if p["kind"] == "min"]
    else:
        raise ValueError(f"select_message_explicit: unknown prefer strategy {prefer!r}")

    if not matches:
        return None
    if len(matches) > 1:
        windows = {(p["start_hour"], p["end_hour"]) for _, p in matches}
        if len(windows) > 1:
            raise RuntimeError(
                f"select_message_explicit: AMBIGUOUS -- {len(matches)} candidates for {variable}:{level} "
                f"satisfy rule prefer={prefer!r} at forecast_hour={forecast_hour} with DIFFERING windows: "
                f"{[e['forecast_desc'] for e, _ in matches]}"
            )

    chosen_entry, chosen_parsed = matches[0]
    i = entries.index(chosen_entry)
    byte_end = entries[i + 1]["byte_start"] - 1 if i + 1 < len(entries) else None
    return {**chosen_entry, "byte_end": byte_end, "_parsed": chosen_parsed}


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
    fh_schedule = available_forecast_hours(run_dt.hour)
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
