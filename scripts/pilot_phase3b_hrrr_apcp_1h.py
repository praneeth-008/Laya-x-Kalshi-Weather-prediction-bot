"""PILOT Phase 3b -- targeted HRRR APCP_1H backfill.

The frozen HRRR pilot (scripts/pilot_phase3_hrrr.py, 20,836 work items,
835 Parquet parts, ALREADY COMPLETE) persisted "APCP" as HRRR's
cumulative-since-run-start precipitation product (see
docs/hrrr_apcp_backfill.md for the full investigation). HRRR ALSO publishes
a direct 1-hour windowed accumulation under the same idx key -- this script
fetches ONLY that second product, for the SAME 20,836 (run_time,
forecast_hour) work items, as an ADDITIVE dataset. It does not touch, read,
rewrite, or depend on the existing 835 parts in any way; it only fetches a
byte range (the windowed message) that the original run never downloaded.

Explicit selection (data/hrrr.py's select_message_explicit, prefer=
"windowed_1h") is used instead of find_message() -- this is the entire
point of the fix: APCP_1H is identified by its accumulation window
(exactly 1 hour ending at forecast_hour), never by idx position. The
direct NOAA product is always fetched; nothing here derives APCP_1H by
differencing cumulative values (verified inexact in ~47% of tested cases,
see docs/hrrr_apcp_backfill.md).

Reuses the SAME grid-index cache (data/pilot_extraction.py) as the original
HRRR pilot -- same grid fingerprint, same Central Park patch, so caching
applies immediately (no separate warm-up needed). Reuses the SAME
Checkpoint/run_concurrent architecture for identical checkpointed,
resumable, crash-safe semantics.

RULE: HRRR APCP_1H is defined only for forecast_hour >= 1, because F0 has
no preceding forecast interval -- there is no "hour before forecast start"
from which a genuine 1-hour accumulation ending at F0 could be defined.
The 840 F0 work items (out of the full 20,836) are therefore EXCLUDED from
the APCP_1H work-item universe before extraction is ever attempted -- never
submitted, never recorded as FAILED, never assigned a value of 0.0. This is
a structural ineligibility, not a missing value. Eligible APCP_1H work
items: 20,836 - 840 = 19,996.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.hrrr import grib_key, fetch_idx, select_message_explicit, fetch_byte_range, MAX_PATCH_DISTANCE_KM
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]
MAX_WORKERS = 24  # same as the original HRRR pilot -- not rebenchmarked, per instruction, unless this single-variable workload behaves materially differently
# Only ONE message per work item now (vs 8 originally) and APCP messages are
# small (~0.33 MB avg, see scripts/pilot_recalculate_cost_40day.py's cost
# table) -- ~20,836 * 0.33MB ~= 6.9GB expected remote download; 2x safety.
BYTE_BUDGET = 14e9

OUT_DIR = ROOT / "data/processed/pilot/hrrr_apcp_1h"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_hrrr_apcp_1h_checkpoint.json"


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    """IDENTICAL logic to scripts/pilot_phase3_hrrr.py's function of the
    same name -- guarantees this backfill's work-item set is exactly the
    original pilot's 20,836 (run_time, forecast_hour) pairs, not a
    redefinition. Duplicated rather than imported, matching this project's
    existing convention of each pilot-phase script being self-contained."""
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
    """Picklable worker -- no shared-state args (ProcessPoolExecutor).
    Fetches ONLY the direct 1-hour windowed APCP message -- the cumulative
    product is already persisted in the original pilot and is never
    re-fetched or touched here."""
    key = grib_key(run_time, run_time.hour, forecast_hour)
    idx = fetch_idx(key)
    valid_time = run_time + timedelta(hours=forecast_hour)
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]

    msg = select_message_explicit(idx, "APCP", "surface", forecast_hour, prefer="windowed_1h")
    if msg is None:
        raise RuntimeError(f"No APCP:surface candidate found at all for {key} fh={forecast_hour}")

    raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    total_bytes = len(raw)
    decoded_points = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)

    rows = []
    for i, decoded in enumerate(decoded_points):
        rows.append(
            {
                "source": "hrrr", "run_time": run_time, "valid_time": valid_time,
                "available_time": last_modified, "availability_time_type": "S3_LAST_MODIFIED_PROXY",
                "forecast_hour": forecast_hour, "variable": "APCP_1H", "level": "surface",
                "temporal_stat": "accum", "temporal_window_hours": 1,
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
    _log(f"HRRR APCP_1H BACKFILL starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    all_hrrr_work_items = 0
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            all_hrrr_work_items += 1
            # RULE: HRRR APCP_1H is defined only for forecast_hour >= 1,
            # because F0 has no preceding forecast interval -- there is no
            # "hour before forecast start" from which a genuine 1-hour
            # accumulation ending at F0 could be defined. This is a
            # structural ineligibility, not a missing value and not a
            # physical zero: F0 work items are excluded from the APCP_1H
            # work-item universe BEFORE extraction is ever attempted, never
            # submitted, and never recorded as FAILED. See
            # docs/hrrr_apcp_backfill.md.
            if fh == 0:
                continue
            key = f"hrrr_apcp_1h|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Full HRRR work-item universe: {all_hrrr_work_items} (matches the original 20,836-item pilot)")
    _log(f"APCP_1H-eligible (forecast_hour>=1) work items: {len(work_items)} across {len(run_times)} runs "
         f"({all_hrrr_work_items - len(work_items)} F0 items structurally excluded)")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="hrrr_apcp_1h")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("HRRR APCP_1H BACKFILL finished." if status == "DONE" else "HRRR APCP_1H BACKFILL stopped (see status).")


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
    with open(OUT_DIR / "pilot_hrrr_apcp_1h_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
