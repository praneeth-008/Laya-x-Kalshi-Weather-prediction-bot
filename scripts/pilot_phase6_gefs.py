"""PILOT Phase 6 -- GEFS ensemble, 40 representative days + lead-in, all 4
run hours/day (00/06/12/18Z), all 31 members, Central Park point + 3x3
patch. Structurally mirrors scripts/pilot_phase3_hrrr.py / pilot_phase4_gfs.py
/ pilot_phase5_nbm.py (same Checkpoint/run_concurrent/flush-dir architecture),
but with an extra work-item dimension: member.

NOT YET LAUNCHED AT PILOT SCALE. This script exists for GEFS
production-readiness validation (small representative sample + benchmark
only) -- see docs/gefs_pilot_readiness.md for the full investigation this
codifies. DO NOT run main() at full scale without explicit approval.

Key GEFS-specific findings (all empirically verified 2026-09-28 across 5
selected-pilot dates, all 4 run hours, control + perturbed members,
forecast hours 0-240 -- see docs/gefs_pilot_readiness.md):

1. GEFS IS AN ENSEMBLE. Member identity is preserved as its own column on
   every row -- never collapsed to mean/std/quantiles in the canonical
   dataset. Derived ensemble statistics (mean/std/percentiles) are computed
   only for VALIDATION sanity-checking (see docs), never stored as a
   replacement for the raw member-level rows.
2. Work-item granularity is (run_time, member, forecast_hour) -- one idx
   fetch + N variable byte-range GETs per item, same per-item efficiency as
   every other source, just with an added member dimension (archive
   structure: one GRIB2 file per member per forecast hour, exactly like
   HRRR/GFS/NBM's one-file-per-forecast-hour).
3. All 31 members (gec00 control + gep01-gep30 perturbed) are present and
   stable across every date/run-hour tested; gep31 confirmed absent (404);
   geavg/gespr (NOAA-precomputed ensemble statistics) confirmed present but
   are NOT members -- excluded. Member identity is independently
   cross-checkable from GRIB metadata (perturbationNumber: gec00=0,
   gepNN=N) against the filename-derived member id.
4. Forecast schedule: uniform 3-hourly F000-F240 for every member, every run
   hour, every date tested -- NOT run-hour-dependent (unlike NBM).
5. F0 uses forecast_desc='anl' for instant fields (TMP/DPT/RH/UGRD/VGRD/PRES)
   -- these ARE present and meaningful at F0. APCP/TCDC/DSWRF are
   STRUCTURALLY ABSENT at F0 entirely (0 idx entries, confirmed, not a
   selection ambiguity). TMAX/TMIN AT F0 use a degenerate '0-0 day max/min
   fcst' zero-width-window format -- a real, decodable value equal to the
   instantaneous F0 reading, NOT a genuine period max/min -- explicitly
   SKIPPED here (never fetched at forecast_hour=0), consistent with how
   HRRR's/GFS's other degenerate F0 accumulation fields are handled.
6. APCP/TMAX/TMIN/TCDC/DSWRF (and several other average-type fields) have
   EXACTLY ONE candidate per (variable, level) at every forecast hour -- no
   duplicate products to disambiguate (unlike HRRR/GFS/NBM's APCP). What
   varies is the accumulation/average WINDOW itself: cumulative-since-start
   for FH<=6, then resets every 6h synoptic mark for FH>6 (window=[6*floor(
   (FH-1)/6), FH], 3h or 6h wide) -- see data.gefs.parse_forecast_desc's
   docstring. The window is always parsed from the observed forecast_desc
   text, never assumed from this formula.
7. Grid is IDENTICAL to GFS's (same md5GridSection=45f3a4a8af23f33a77ab669
   d0fa1d813, regular_ll 0.25deg, Ni=1440 Nj=721) -- independently confirmed,
   not inherited. The +/-0.03deg 3x3 patch fully collapses to 1 grid cell
   (same as GFS), max observed distance from patch points to that cell
   ~8.35km, well within the independently-derived 20km threshold
   (data.gefs.MAX_PATCH_DISTANCE_KM).
8. Availability lag is substantially larger than HRRR/GFS/NBM (~3.8h at
   FH3, growing to ~5.3-5.5h at FH240) and grows monotonically with forecast
   hour within a run (confirmed genuine progressive release, not a single
   batch write). Perturbed members arrive within a tight cluster of each
   other (~2-14 min spread, growing with lead time); the control member
   (gec00) systematically arrives ~1-14 minutes EARLIER than the perturbed
   batch. Per-row available_time (from each row's own byte-range GET) is
   preserved at full member/FH granularity -- never collapsed to one
   artificial run-level timestamp.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.gefs import (
    grib_key, fetch_idx, find_message, fetch_byte_range,
    parse_forecast_desc, MAX_PATCH_DISTANCE_KM, MEMBERS, member_type,
)
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]

RUN_HOURS = [0, 6, 12, 18]

# Instantaneous fields -- present at every forecast hour including F0 ('anl').
INSTANT_VARS = [
    ("TMP", "2 m above ground"), ("DPT", "2 m above ground"), ("RH", "2 m above ground"),
    ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"), ("PRES", "surface"),
]
# Accumulation/average fields -- structurally absent at F0 (0 idx entries),
# window varies with forecast hour, parsed explicitly via parse_forecast_desc.
WINDOWED_VARS = [("APCP", "surface"), ("TCDC", "entire atmosphere"), ("DSWRF", "surface")]
# Period max/min -- present from F0 but F0's window is degenerate (0-0 day,
# zero-width) -- explicitly skipped at forecast_hour=0.
PERIOD_VARS = [("TMAX", "2 m above ground"), ("TMIN", "2 m above ground")]

MAX_WORKERS = 16  # placeholder pending independent benchmark (section 12) -- NOT inherited from HRRR/GFS/NBM.


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in SELECTED_DATES:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("gefs", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


def extract_one_member_hour(run_time: datetime, member: str, forecast_hour: int):
    """Picklable worker -- no shared-state args (ProcessPoolExecutor)."""
    key = grib_key(run_time, run_time.hour, member, forecast_hour, "pgrb2sp25")
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0

    def emit(var_name, level, msg):
        nonlocal total_bytes
        parsed = parse_forecast_desc(msg["forecast_desc"])
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
        total_bytes += len(raw)
        decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)
        temporal_stat = parsed["kind"]
        temporal_window_hours = 0 if parsed["kind"] == "instant" else (parsed["end_hour"] - (parsed["start_hour"] or 0))
        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "gefs", "run_time": run_time, "valid_time": valid_time,
                    "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                    "forecast_hour": forecast_hour, "ensemble_member": member, "member_type": member_type(member),
                    "variable": var_name, "level": level,
                    "temporal_stat": temporal_stat, "temporal_window_hours": temporal_window_hours,
                    "requested_lat": decoded["requested_lat"], "requested_lon": decoded["requested_lon"],
                    "grid_lat": decoded.get("grid_lat"), "grid_lon": decoded.get("grid_lon"),
                    "distance_km": decoded.get("distance_km"),
                    "is_primary_central_park_grid": (i == 4),
                    "value": decoded.get("value"), "units": decoded.get("units"),
                    "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded.get("units") == "K" else None,
                    "source_object": key,
                }
            )

    for var, level in INSTANT_VARS:
        msg = find_message(idx, var, level)
        if msg is not None:
            emit(var, level, msg)

    if forecast_hour > 0:
        for var, level in WINDOWED_VARS:
            msg = find_message(idx, var, level)
            if msg is not None:
                emit(var, level, msg)
        for var, level in PERIOD_VARS:
            msg = find_message(idx, var, level)
            if msg is not None:
                emit(var, level, msg)

    return rows, total_bytes


def main():
    raise RuntimeError(
        "GEFS pilot is NOT approved for a full launch yet -- this is a "
        "production-readiness validation script. Use the small validation "
        "sample harness instead (see docs/gefs_pilot_readiness.md). Remove "
        "this guard only after explicit approval to launch the full pilot."
    )


if __name__ == "__main__":
    main()
