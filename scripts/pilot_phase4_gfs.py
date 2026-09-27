"""PILOT Phase 4 -- GFS, 40 representative days + lead-in, 00/06/12/18Z,
CORE variables, Central Park point + 3x3 patch. Structurally mirrors
scripts/pilot_phase3_hrrr.py (same Checkpoint/run_concurrent/flush-dir
architecture, same resumability guarantees).

Two GFS-specific corrections vs. a naive HRRR mirror, both empirically
verified (see docs/gfs_pilot_readiness.md):

1. APCP and TCDC each have MULTIPLE distinct products published under the
   identical (variable, level) .idx key (GFS-specific; does not occur for
   any other CORE_VARS entry). Selecting via plain find_message() would
   silently depend on idx ordering. select_message_explicit() instead
   parses each candidate's forecast_desc and picks the one matching this
   project's intended product semantics:
     - APCP: shortest accumulation window ending at forecast_hour (a
       "recent precipitation" signal, not a cumulative-since-run-start
       total).
     - TCDC: the instantaneous entry (consistent with HRRR's own
       instantaneous TCDC and this project's point-in-time weather_state
       philosophy).
   Every row also carries temporal_stat/temporal_window_hours so any
   accumulated/averaged quantity (APCP, DSWRF) is explicitly distinguishable
   from an instantaneous one downstream -- this matters especially for
   DSWRF, which in GFS is ALWAYS a running average (no instantaneous variant
   exists in this product), unlike HRRR's instantaneous DSWRF.

2. The grid distance sanity-check ceiling is GFS's own derived value
   (data.gfs.MAX_PATCH_DISTANCE_KM = 20km, see that module for the geometric
   derivation), not HRRR's 10km -- GFS's 0.25deg grid is far coarser than
   HRRR's ~3km grid.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.gfs import (
    grib_key, fetch_idx, find_message, fetch_byte_range,
    select_message_explicit, parse_forecast_desc, MAX_PATCH_DISTANCE_KM,
)
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]

# CORE_VARS: matches HRRR's set plus RH -- RH was validated in earlier GFS
# feasibility work (unique idx entry at "2 m above ground", no duplicate-
# product ambiguity) and dropped from this list with no documented
# scientific/technical reason found (see docs/gfs_pilot_readiness.md).
# Reinstated here; NOT added to HRRR (source identity stays explicit, no
# forced cross-source symmetry).
CORE_VARS = [("TMP", "2 m above ground"), ("DPT", "2 m above ground"), ("RH", "2 m above ground"),
             ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"), ("PRES", "surface"),
             ("TCDC", "entire atmosphere"), ("APCP", "surface"), ("DSWRF", "surface")]

# Explicit product-selection rule per (variable, level) pair that publishes
# more than one candidate under the same idx key. Every other CORE_VARS
# entry is uniquely selectable via find_message() (empirically verified).
EXPLICIT_SELECTION = {
    ("APCP", "surface"): "shortest_window",
    ("TCDC", "entire atmosphere"): "instant",
}

RUN_HOURS = [0, 6, 12, 18]
MAX_WORKERS = 20  # benchmarked best worker count for GFS on this machine (see docs/gfs_pilot_readiness.md) -- NOT the same as HRRR's 24.
# ~37 GB estimated remote download for the full 5,836-item pilot (measured
# ~6.35 MB/item during benchmarking); 2x safety margin, same policy as HRRR.
BYTE_BUDGET = 37e9 * 2

OUT_DIR = ROOT / "data/processed/pilot/gfs"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_gfs_checkpoint.json"


def run_times_for_selected_days():
    dates_needed = set()
    for d in SELECTED_DATES:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=1))
    run_times = []
    for d in sorted(dates_needed):
        for h in RUN_HOURS:
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in SELECTED_DATES:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("gfs", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


def extract_one_run_hour(run_time: datetime, forecast_hour: int):
    """Picklable worker -- no shared-state args (ProcessPoolExecutor)."""
    key = grib_key(run_time, run_time.hour, forecast_hour)
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0
    for var, level in CORE_VARS:
        prefer = EXPLICIT_SELECTION.get((var, level))
        if prefer is not None:
            msg = select_message_explicit(idx, var, level, forecast_hour, prefer=prefer)
        else:
            msg = find_message(idx, var, level)
        if msg is None:
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
        total_bytes += len(raw)
        decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)

        parsed = msg.get("_parsed") or parse_forecast_desc(msg["forecast_desc"])
        temporal_stat = parsed["kind"]
        temporal_window_hours = 0 if parsed["kind"] == "instant" else (parsed["end_hour"] - (parsed["start_hour"] or 0))

        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "gfs", "run_time": run_time, "valid_time": valid_time,
                    "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                    "forecast_hour": forecast_hour, "variable": var, "level": level,
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
    return rows, total_bytes


def main():
    _log(f"GFS PILOT starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"gfs|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Total (run,forecast_hour) work items: {len(work_items)} across {len(run_times)} runs")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="gfs")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("GFS PILOT finished." if status == "DONE" else "GFS PILOT stopped (see status).")


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
    with open(OUT_DIR / "pilot_gfs_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
