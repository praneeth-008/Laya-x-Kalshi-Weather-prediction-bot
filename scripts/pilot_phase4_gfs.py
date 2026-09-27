"""PILOT Phase 4 -- GFS, 40 representative days + lead-in, 00/06/12/18Z,
CORE variables, Central Park point + 3x3 patch. Mirrors pilot_phase3_hrrr.py.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.gfs import grib_key, fetch_idx, find_message, fetch_byte_range
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import PilotStats, Checkpoint, run_concurrent, decode_message_multi_point

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]
CORE_VARS = [("TMP", "2 m above ground"), ("DPT", "2 m above ground"), ("UGRD", "10 m above ground"),
             ("VGRD", "10 m above ground"), ("PRES", "surface"), ("TCDC", "entire atmosphere"),
             ("APCP", "surface"), ("DSWRF", "surface")]
RUN_HOURS = [0, 6, 12, 18]
MAX_WORKERS = 12

OUT_DIR = ROOT / "data/processed/pilot/gfs"
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


def extract_one_run_hour(run_time: datetime, forecast_hour: int, stats: PilotStats):
    key = grib_key(run_time, run_time.hour, forecast_hour)
    idx = fetch_idx(key, stats=stats)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0
    for var, level in CORE_VARS:
        msg = find_message(idx, var, level)
        if msg is None:
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"), stats=stats)
        total_bytes += len(raw)
        decoded_points = decode_message_multi_point(raw, points)
        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "gfs", "run_time": run_time, "valid_time": valid_time,
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
    stats = PilotStats()
    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"gfs|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh, stats)))
    print(f"GFS PILOT: {len(SELECTED_DATES)} selected days, {len(run_times)} runs, {len(work_items)} work items")

    rows = run_concurrent(work_items, extract_one_run_hour, checkpoint, max_workers=MAX_WORKERS,
                           stats=stats, label="gfs")

    parquet_path = OUT_DIR / "pilot_gfs_canonical.parquet"
    if rows:
        new_df = pd.DataFrame(rows)
        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            combined = pd.concat([existing, new_df], ignore_index=True).drop_duplicates(
                subset=["run_time", "valid_time", "variable", "requested_lat", "requested_lon"]
            )
        else:
            combined = new_df
        combined.to_parquet(parquet_path, index=False)
    else:
        combined = pd.read_parquet(parquet_path) if parquet_path.exists() else pd.DataFrame()

    report = {
        "n_selected_days": len(SELECTED_DATES), "n_runs": len(run_times), "n_work_items": len(work_items),
        "work_item_status_counts": checkpoint.counts(),
        "total_requests_this_run": stats.n_requests, "total_bytes_this_run": stats.bytes_downloaded,
        "total_rows": int(len(combined)), "status": "DONE",
    }
    with open(OUT_DIR / "pilot_gfs_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
