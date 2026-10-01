"""HISTORICAL BACKFILL -- NBM, one six-month calendar block. Generalizes
scripts/pilot_phase5_nbm.py (approved baseline 8af8d30) from 40 sampled days
to every calendar day in a block. Uses NBM's OWN validated forecast-hour
schedule (data.nbm.available_forecast_hours), run-hour-dependent tiers, F0
non-existence, dual-APCP-window handling, and TMAX/TMIN period products --
all unchanged from the pilot.

Usage: python scripts/backfill_nbm.py --block 2026H1
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.nbm import (
    grib_key, fetch_idx, find_message, fetch_byte_range,
    select_message_explicit, MAX_PATCH_DISTANCE_KM,
    available_forecast_hours, TMAX_TMIN_RUN_HOURS, NYC_TZ, local_day_utc_bounds,
)
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log
from scripts.backfill_common import (
    CENTRAL_PARK, PATCH_OFFSETS, block_arg_parser, get_block, calendar_days,
    block_out_dir, scaled_byte_budget,
)

SOURCE = "nbm"
INSTANT_VARS = [
    ("TMP", "2 m above ground"), ("DPT", "2 m above ground"), ("RH", "2 m above ground"),
    ("WIND", "10 m above ground"), ("WDIR", "10 m above ground"), ("TCDC", "surface"),
]
APCP_VARIANTS = [("APCP_1H", "shortest_window", 1), ("APCP_6H", "sixhour_window", 6)]
MAX_WORKERS = 20


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


def required_forecast_hours_for_run(run_time: datetime, selected_dates: set) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    schedule = set(available_forecast_hours(run_time.hour))
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in selected_dates:
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
            continue
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
    args = block_arg_parser("NBM").parse_args()
    block = get_block(args.block)
    selected_dates = set(calendar_days(block.start, block.end))
    out_dir = block_out_dir(SOURCE, block.block_id)
    flush_dir = out_dir / "parts"
    checkpoint_path = out_dir / f"backfill_{SOURCE}_checkpoint.json"

    _log(f"NBM BACKFILL [{block.block_id}] starting: {len(selected_dates)} calendar days, MAX_WORKERS={MAX_WORKERS}")

    checkpoint = Checkpoint(checkpoint_path)
    run_times = run_times_for_selected_days(sorted(selected_dates))
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt, selected_dates)):
            key = f"nbm|{block.block_id}|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    byte_budget = scaled_byte_budget(SOURCE, len(work_items))
    _log(f"[{block.block_id}] Total work items: {len(work_items)} across {len(run_times)} runs; byte budget {byte_budget/1e9:.2f} GB")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, flush_dir,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=byte_budget, label=f"nbm_{block.block_id}")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status, out_dir, flush_dir, block, len(work_items))
    _log(f"NBM BACKFILL [{block.block_id}] finished." if status == "DONE" else f"NBM BACKFILL [{block.block_id}] stopped (see status).")


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
