"""HISTORICAL BACKFILL -- HRRR RH (relative humidity), one six-month calendar
block. Generalizes scripts/pilot_phase3c_hrrr_rh.py (approved baseline
8af8d30) from 40 sampled days to every calendar day in a block. Additive to
backfill_hrrr.py's core-variable output -- never touches it. RH is kept
explicitly identified as its own variable, never used to replace DPT.

Usage: python scripts/backfill_hrrr_rh.py --block 2026H1
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.hrrr import grib_key, fetch_idx, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log
from scripts.backfill_common import (
    CENTRAL_PARK, PATCH_OFFSETS, block_arg_parser, get_block, calendar_days,
    block_out_dir, scaled_byte_budget,
)

SOURCE = "hrrr_rh"
MAX_WORKERS = 24


def required_forecast_hours_for_run(run_time: datetime, selected_dates: set) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in selected_dates:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("hrrr", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


def run_times_for_selected_days(selected_dates: list) -> list:
    dates_needed = set()
    for d in selected_dates:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=1))
    run_times = []
    for d in sorted(dates_needed):
        for h in range(24):
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def extract_one_run_hour(run_time: datetime, forecast_hour: int):
    key = grib_key(run_time, run_time.hour, forecast_hour)
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]

    msg = find_message(idx, "RH", "2 m above ground")
    if msg is None:
        raise RuntimeError(f"No RH:2 m above ground candidate found for {key} fh={forecast_hour}")

    raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    total_bytes = len(raw)
    decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)

    rows = []
    for i, decoded in enumerate(decoded_points):
        rows.append(
            {
                "source": "hrrr", "run_time": run_time, "valid_time": valid_time,
                "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                "forecast_hour": forecast_hour, "variable": "RH", "level": "2 m above ground",
                "temporal_stat": "instant", "temporal_window_hours": 0,
                "requested_lat": decoded["requested_lat"], "requested_lon": decoded["requested_lon"],
                "grid_lat": decoded.get("grid_lat"), "grid_lon": decoded.get("grid_lon"),
                "distance_km": decoded.get("distance_km"),
                "is_primary_central_park_grid": (i == 4),
                "value": decoded.get("value"), "units": decoded.get("units"),
                "value_f": None,
                "source_object": key,
            }
        )
    return rows, total_bytes


def main():
    args = block_arg_parser("HRRR RH").parse_args()
    block = get_block(args.block)
    selected_dates = set(calendar_days(block.start, block.end))
    out_dir = block_out_dir(SOURCE, block.block_id)
    flush_dir = out_dir / "parts"
    checkpoint_path = out_dir / f"backfill_{SOURCE}_checkpoint.json"

    _log(f"HRRR RH BACKFILL [{block.block_id}] starting: {len(selected_dates)} calendar days, MAX_WORKERS={MAX_WORKERS}")

    checkpoint = Checkpoint(checkpoint_path)
    run_times = run_times_for_selected_days(sorted(selected_dates))
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt, selected_dates)):
            key = f"hrrr_rh|{block.block_id}|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    byte_budget = scaled_byte_budget(SOURCE, len(work_items))
    _log(f"[{block.block_id}] Total work items: {len(work_items)} across {len(run_times)} runs; byte budget {byte_budget/1e9:.2f} GB")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, flush_dir,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=byte_budget, label=f"hrrr_rh_{block.block_id}")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status, out_dir, flush_dir, block, len(work_items))
    _log(f"HRRR RH BACKFILL [{block.block_id}] finished." if status == "DONE" else f"HRRR RH BACKFILL [{block.block_id}] stopped (see status).")


def write_report(checkpoint, result, status, out_dir, flush_dir, block, n_planned):
    counts = checkpoint.counts()
    n_parts = len(list(flush_dir.glob("*.parquet"))) if flush_dir.exists() else 0
    report = {
        "block_id": block.block_id, "start": str(block.start), "end": str(block.end),
        "n_planned_work_items": n_planned,
        "work_item_status_counts": counts,
        "total_bytes_this_run": result.get("total_bytes_this_run"),
        "n_part_files": n_parts,
        "status": status,
    }
    with open(out_dir / f"backfill_{SOURCE}_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
