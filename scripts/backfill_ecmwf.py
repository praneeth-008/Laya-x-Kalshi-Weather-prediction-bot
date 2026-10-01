"""HISTORICAL BACKFILL -- ECMWF IFS deterministic, one six-month calendar
block. Generalizes scripts/pilot_phase7_ecmwf.py (approved baseline 8af8d30)
from 40 sampled days to every calendar day in a block.

REGIME BOUNDARY (explicit, pre-documented, NOT a guess): data/ecmwf.py's
grib_key() (ifs/0p25 archive path) is only valid from 2024-02-29 onward (see
pilot_phase7_ecmwf.py's docstring, confirmed via a 21-date probe). Any block
day before this date is STRUCTURALLY UNAVAILABLE under the current
methodology -- not attempted, not recorded as a failure. A block straddling
the boundary (2024H1) extracts ECMWF only for its days >= 2024-02-29; a block
entirely before it (2024H2 and earlier -- note 2024H2 is AFTER, so this means
2023H2 and older) skips ECMWF entirely and writes a STRUCTURALLY_UNAVAILABLE
report instead of a checkpoint/extraction run.

Usage: python scripts/backfill_ecmwf.py --block 2026H1
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.ecmwf import (
    grib_key, fetch_index, find_message, fetch_byte_range, temporal_metadata,
    MAX_PATCH_DISTANCE_KM, RUN_HOURS,
)
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log
from scripts.backfill_common import (
    CENTRAL_PARK, PATCH_OFFSETS, block_arg_parser, get_block, calendar_days,
    block_out_dir, scaled_byte_budget, ECMWF_VALID_FROM,
)

SOURCE = "ecmwf"
INSTANT_VARS = ["2t", "2d", "10u", "10v", "sp"]
WINDOWED_VARS = ["tp", "ssrd"]
PERIOD_MAXMIN_REGIME_BOUNDARY_HOURS = 144
# Reduced from 16 (the pilot's benchmarked value) to 6 after Block 1 (2025H1)
# saw 100% HTTP 503 on its first 25 items at 16 concurrent workers, even
# after data/ecmwf.py's own built-in retry/backoff was exhausted -- a
# thundering-herd effect from many workers opening fresh connections
# simultaneously, consistent with this bucket's already-documented
# "aggressive rate limiting... substantially higher run-to-run variance"
# (see pilot_phase7_ecmwf.py's docstring). Concurrency tuning only -- no
# change to extraction methodology/semantics.
MAX_WORKERS = 6


def run_times_for_selected_days(selected_dates: list) -> list:
    dates_needed = set()
    for d in selected_dates:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=1))
    run_times = []
    for d in sorted(dates_needed):
        for h in sorted(RUN_HOURS):
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def required_forecast_hours_for_run(run_time: datetime, selected_dates: set) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in selected_dates:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("ecmwf_deterministic", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


def extract_one_run_step(run_time: datetime, step: int):
    key = grib_key(run_time, run_time.hour, step, "oper")
    entries = fetch_index(key)
    valid_time = run_time + timedelta(hours=step)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    rows = []
    total_bytes = 0

    def emit(param, msg):
        nonlocal total_bytes
        import eccodes
        raw, last_modified = fetch_byte_range(key, msg["_offset"], msg["_length"])
        total_bytes += len(raw)
        gid = eccodes.codes_new_from_message(raw)
        try:
            tmeta = temporal_metadata(gid)
        finally:
            eccodes.codes_release(gid)
        decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)
        for i, decoded in enumerate(decoded_points):
            rows.append(
                {
                    "source": "ecmwf_deterministic", "model": "ECMWF-IFS", "stream": "oper",
                    "run_time": run_time, "valid_time": valid_time,
                    "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                    "availability_confidence": "LOW",
                    "forecast_hour": step, "variable": param,
                    "temporal_stat": tmeta["temporal_stat"], "temporal_window_hours": tmeta["temporal_window_hours"],
                    "requested_lat": decoded["requested_lat"], "requested_lon": decoded["requested_lon"],
                    "grid_lat": decoded.get("grid_lat"), "grid_lon": decoded.get("grid_lon"),
                    "distance_km": decoded.get("distance_km"),
                    "is_primary_central_park_grid": (i == 4),
                    "value": decoded.get("value"), "units": decoded.get("units"),
                    "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded.get("units") == "K" else None,
                    "archive_regime": "ifs_0p25", "source_object": key,
                }
            )

    for param in INSTANT_VARS:
        msg = find_message(entries, param)
        if msg is not None:
            emit(param, msg)

    if step > 0:
        for param in WINDOWED_VARS:
            msg = find_message(entries, param)
            if msg is not None:
                emit(param, msg)
        maxmin_suffix = "3" if step <= PERIOD_MAXMIN_REGIME_BOUNDARY_HOURS else "6"
        for base in ("mx2t", "mn2t"):
            param = base + maxmin_suffix
            msg = find_message(entries, param)
            if msg is not None:
                emit(param, msg)

    return rows, total_bytes


def main():
    args = block_arg_parser("ECMWF").parse_args()
    block = get_block(args.block)
    out_dir = block_out_dir(SOURCE, block.block_id)
    flush_dir = out_dir / "parts"
    checkpoint_path = out_dir / f"backfill_{SOURCE}_checkpoint.json"

    all_days = calendar_days(block.start, block.end)
    eligible_days = [d for d in all_days if d >= ECMWF_VALID_FROM]

    if not eligible_days:
        _log(f"ECMWF BACKFILL [{block.block_id}] SKIPPED: entire block ({block.start}..{block.end}) is before "
             f"the validated archive-path regime start ({ECMWF_VALID_FROM}). STRUCTURALLY UNAVAILABLE, not a failure.")
        report = {
            "block_id": block.block_id, "start": str(block.start), "end": str(block.end),
            "status": "STRUCTURALLY_UNAVAILABLE",
            "reason": f"entire block is before the validated ECMWF ifs/0p25 archive-path regime start ({ECMWF_VALID_FROM})",
            "n_calendar_days": len(all_days), "n_eligible_days": 0,
        }
        with open(out_dir / f"backfill_{SOURCE}_download_report.json", "w") as f:
            json.dump(report, f, indent=2, default=str)
        _log(json.dumps(report, default=str))
        return

    if len(eligible_days) < len(all_days):
        _log(f"ECMWF BACKFILL [{block.block_id}] PARTIAL COVERAGE: {len(all_days) - len(eligible_days)} of "
             f"{len(all_days)} calendar days fall before {ECMWF_VALID_FROM} and are STRUCTURALLY UNAVAILABLE; "
             f"extracting only the {len(eligible_days)} eligible days ({eligible_days[0]}..{eligible_days[-1]}).")

    selected_dates = set(eligible_days)
    _log(f"ECMWF BACKFILL [{block.block_id}] starting: {len(selected_dates)} eligible calendar days, MAX_WORKERS={MAX_WORKERS}")

    checkpoint = Checkpoint(checkpoint_path)
    run_times = run_times_for_selected_days(sorted(selected_dates))
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt, selected_dates)):
            key = f"ecmwf|{block.block_id}|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    byte_budget = scaled_byte_budget(SOURCE, len(work_items))
    _log(f"[{block.block_id}] Total work items: {len(work_items)} across {len(run_times)} runs; byte budget {byte_budget/1e9:.2f} GB")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_step, checkpoint, flush_dir,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=byte_budget, label=f"ecmwf_{block.block_id}")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status, out_dir, flush_dir, block, len(work_items), len(all_days), len(eligible_days))
    _log(f"ECMWF BACKFILL [{block.block_id}] finished." if status == "DONE" else f"ECMWF BACKFILL [{block.block_id}] stopped (see status).")


def write_report(checkpoint, result, status, out_dir, flush_dir, block, n_planned, n_calendar_days, n_eligible_days):
    counts = checkpoint.counts()
    n_parts = len(list(flush_dir.glob("*.parquet"))) if flush_dir.exists() else 0
    report = {
        "block_id": block.block_id, "start": str(block.start), "end": str(block.end),
        "n_calendar_days": n_calendar_days, "n_eligible_days": n_eligible_days,
        "structurally_unavailable_days": n_calendar_days - n_eligible_days,
        "ecmwf_valid_from": str(ECMWF_VALID_FROM),
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
