"""PILOT Phase 3 -- HRRR, 40 representative target days (+ 1-day lead-in),
all 24 runs/day, CORE variables, Central Park point + 3x3 patch.

Resumable, checkpointed, process-parallel (ProcessPoolExecutor -- eccodes is
NOT thread-safe in this environment, confirmed empirically). Stream-and-
discard: raw GRIB bytes never touch disk, decoded in memory, dropped
immediately. Extracted rows are flushed to a durable Parquet part-file every
25 completed work items (data/pilot_extraction.run_concurrent) so an
interruption never loses more than a small window of already-completed work.

Designed to run as an independent, long-lived local process: launch it with
`start /B` or PowerShell Start-Process and it keeps running even if this
Claude session ends. Rerunning this exact command later resumes automatically
from the checkpoint -- already-DONE work items are skipped, nothing is
re-fetched, and no row is ever duplicated (each flushed part-file is written
once, keyed by a timestamp+count that only advances forward).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.hrrr import grib_key, fetch_idx, find_message, fetch_byte_range
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]
CORE_VARS = [("TMP", "2 m above ground"), ("DPT", "2 m above ground"), ("UGRD", "10 m above ground"),
             ("VGRD", "10 m above ground"), ("PRES", "surface"), ("TCDC", "entire atmosphere"),
             ("APCP", "surface"), ("DSWRF", "surface")]
MAX_WORKERS = 12  # matches this machine's CPU count -- conservative, not increased beyond it.
# Estimated ~259 GB for this 40-day pilot (see pilot_recalculate_cost_40day.py); 2x safety margin.
BYTE_BUDGET = 259e9 * 2

OUT_DIR = ROOT / "data/processed/pilot/hrrr"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_hrrr_checkpoint.json"


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in SELECTED_DATES:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("hrrr", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


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


def extract_one_run_hour(run_time: datetime, forecast_hour: int):
    """Picklable worker -- no shared-state args (ProcessPoolExecutor)."""
    key = grib_key(run_time, run_time.hour, forecast_hour)
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0
    for var, level in CORE_VARS:
        msg = find_message(idx, var, level)
        if msg is None:
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
        total_bytes += len(raw)
        decoded_points = decode_message_multi_point(raw, points)
        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "hrrr", "run_time": run_time, "valid_time": valid_time,
                    "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                    "forecast_hour": forecast_hour, "variable": var, "level": level,
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
    _log(f"HRRR PILOT starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB (2x the ~259 GB estimate)")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"hrrr|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Total (run,forecast_hour) work items: {len(work_items)} across {len(run_times)} runs")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="hrrr")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("HRRR PILOT finished." if status == "DONE" else "HRRR PILOT stopped (see status).")


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
    with open(OUT_DIR / "pilot_hrrr_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
