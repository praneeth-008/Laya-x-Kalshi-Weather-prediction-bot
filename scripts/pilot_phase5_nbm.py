"""PILOT Phase 5 -- NBM, 40 representative days + lead-in, all 24 UTC run
hours/day, Central Park point + 3x3 patch. Structurally mirrors
scripts/pilot_phase3_hrrr.py / pilot_phase4_gfs.py (same
Checkpoint/run_concurrent/flush-dir architecture).

APPROVED FOR FULL-SCALE LAUNCH: NBM production-readiness validation and the
pre-production phase (completeness fix, worker benchmark, expanded APCP_6H
validation) both passed -- see docs/nbm_pilot_readiness.md. Production commit:
cb09e8f25395e0276d7a2168fb10e5c933cf0d71.

Key NBM-specific findings (all empirically verified, not assumed from prior
feasibility work -- see docs/nbm_pilot_readiness.md):

1. Forecast-hour schedule is RUN-HOUR DEPENDENT (data.nbm.nbm_schedule_tier),
   a 3-tier split keyed by run_hour % 6 -- NOT one fixed schedule for every
   run as data/weather_state.py's _forecast_hour_candidates() currently
   (incorrectly) assumes for NBM. This script uses the validated
   data.nbm.available_forecast_hours(run_hour), NOT weather_state.py's
   version, for its own work-item planning.
2. forecast_hour=0 does not exist for NBM at all (confirmed 404 for every
   run_hour/date tested) -- there is no degenerate F0 message to special-case,
   unlike HRRR/GFS.
3. APCP has TWO deterministic amount candidates (1-hour trailing, always
   present from fh>=1; 6-hour rolling, only when fh%6==0) PLUS separate
   probability-of-exceedance products that must be excluded. Both amount
   products are extracted here (see CORE_VARS) since both carry distinct
   information, per docs/nbm_pilot_readiness.md.
4. TMAX/TMIN 12-hour period products exist ONLY for run_hour in (0, 12), and
   which one is "max" vs "min" alternates depending on which period covers
   local afternoon vs. overnight (run-hour-dependent parity) -- handled
   separately from the main hourly-variable loop, only attempted for those
   two run hours at their own period-boundary forecast hours.
5. WIND/WDIR are SCALAR speed+direction, explicitly at "10 m above ground"
   (multiple other heights exist under the same variable name -- level must
   always be explicit) -- NOT the same representation as HRRR/GFS's UGRD/VGRD
   components.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.nbm import (
    grib_key, fetch_idx, find_message, fetch_byte_range,
    select_message_explicit, parse_forecast_desc, MAX_PATCH_DISTANCE_KM,
    available_forecast_hours, TMAX_TMIN_RUN_HOURS, NYC_TZ, local_day_utc_bounds,
)
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]

# Simple instant variables: (variable, level), all with an "ens std dev"
# sibling to exclude via desc_excludes. Unlike HRRR/GFS, level alone is not
# always sufficient to disambiguate (WIND/WDIR exist at 4 different heights
# under the identical variable name) -- level is always explicit here.
INSTANT_VARS = [
    ("TMP", "2 m above ground"),
    ("DPT", "2 m above ground"),
    ("RH", "2 m above ground"),
    ("WIND", "10 m above ground"),
    ("WDIR", "10 m above ground"),
    ("TCDC", "surface"),
]
# APCP: both amount products, explicitly selected (never idx-first).
APCP_VARIANTS = [("APCP_1H", "shortest_window", 1), ("APCP_6H", "sixhour_window", 6)]

MAX_WORKERS = 20  # independently benchmarked for NBM (see docs/nbm_pilot_readiness.md section 17): flat 8-24 throughput curve, network-latency-bound; 20 is the highest STABLE value across repeated trials.
# ~314 GB estimated remote download for the full 31,578-item pilot (measured
# ~9.94 MB/item during benchmarking); 2x safety margin, same policy as HRRR/GFS.
BYTE_BUDGET = 314e9 * 2

OUT_DIR = ROOT / "data/processed/pilot/nbm"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_nbm_checkpoint.json"


def run_times_for_selected_days():
    dates_needed = set()
    for d in SELECTED_DATES:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=1))
    run_times = []
    for d in sorted(dates_needed):
        for h in range(24):
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    """NBM's OWN validated schedule (data.nbm.available_forecast_hours),
    NOT data/weather_state.py's expected_target_day_valid_times('nbm', ...)
    -- that function depends on _forecast_hour_candidates(), which hardcodes
    one fixed schedule for every NBM run hour and does NOT reflect the
    run-hour-dependent tiers validated here. See docs/nbm_pilot_readiness.md."""
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    schedule = set(available_forecast_hours(run_time.hour))
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in SELECTED_DATES:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        t = day_start
        while t < day_end:
            fh = int((t - run_time).total_seconds() / 3600)
            if fh in schedule:
                fhs.add(fh)
            t += timedelta(hours=1)
    return fhs


def extract_one_run_hour(run_time: datetime, forecast_hour: int):
    """Picklable worker -- no shared-state args (ProcessPoolExecutor)."""
    key = grib_key(run_time, run_time.hour, forecast_hour, "core")
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0

    def emit(var_name, level, msg, temporal_stat, temporal_window_hours):
        nonlocal total_bytes
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
        total_bytes += len(raw)
        decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)
        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "nbm", "run_time": run_time, "valid_time": valid_time,
                    "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                    "forecast_hour": forecast_hour, "variable": var_name, "level": level,
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
        msg = find_message(idx, var, level, desc_excludes="ens std dev")
        if msg is None:
            continue
        emit(var, level, msg, "instant", 0)

    for var_name, prefer, window_hours in APCP_VARIANTS:
        msg = select_message_explicit(idx, "APCP", "surface", forecast_hour, prefer=prefer)
        if msg is None:
            continue  # legitimate absence (e.g. sixhour_window when (run_hour+forecast_hour) % 6 != 0, i.e. valid_time is not an absolute UTC synoptic hour), not a failure
        emit(var_name, "surface", msg, "accum", window_hours)

    if run_time.hour in TMAX_TMIN_RUN_HOURS:
        pmax = select_message_explicit(idx, "TMAX", "2 m above ground", forecast_hour, prefer="period_max")
        if pmax is not None:
            emit("TMAX_PERIOD", "2 m above ground", pmax, "max", 12)
        pmin = select_message_explicit(idx, "TMIN", "2 m above ground", forecast_hour, prefer="period_min")
        if pmin is not None:
            emit("TMIN_PERIOD", "2 m above ground", pmin, "min", 12)

    return rows, total_bytes


def main():
    _log(f"NBM PILOT starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"nbm|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Total (run,forecast_hour) work items: {len(work_items)} across {len(run_times)} runs")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="nbm")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("NBM PILOT finished." if status == "DONE" else "NBM PILOT stopped (see status).")


def write_report(checkpoint, result, status):
    counts = checkpoint.counts()
    n_parts = len(list(FLUSH_DIR.glob("*.parquet"))) if FLUSH_DIR.exists() else 0
    report = {
        "n_selected_days": len(SELECTED_DATES),
        "work_item_status_counts": counts,
        "total_bytes_this_run": result.get("total_bytes_this_run"),
        "n_part_files": n_parts,
        "status": status,
    }
    with open(OUT_DIR / "pilot_nbm_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
