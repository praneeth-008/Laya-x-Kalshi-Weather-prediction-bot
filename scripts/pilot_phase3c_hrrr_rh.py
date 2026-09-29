"""PILOT Phase 3c -- targeted HRRR RH backfill.

The frozen HRRR pilot (scripts/pilot_phase3_hrrr.py, 20,836 work items,
835 Parquet parts, ALREADY COMPLETE) did not extract relative humidity --
a deliberate, documented decision at the time (source-identity preservation
across HRRR/GFS, not a technical barrier; see docs/variable_semantics.md's
"GFS RH -- included after production-readiness validation" entry). The
pre-backfill variable inventory audit (2026-09-29,
docs/variable_inventory.md) confirmed RH is directly and uniquely
selectable in HRRR's own archive (same "2 m above ground" level string
already used for TMP/DPT, no duplicate-candidate ambiguity -- verified live
across F0/F6/F24 before writing this script). This script fetches ONLY
that RH product, for the SAME 20,836 (run_time, forecast_hour) work items
as the original pilot, as an ADDITIVE dataset. It does not touch, read,
rewrite, or depend on the existing 835 parts in any way.

Native HRRR RH is kept explicitly identified as its own variable ("RH") --
it is NEVER used to replace DPT, and is never confused with a later
engineered RH computed from TMP+DPT (which remains a distinct, hypothetical
future feature-engineering choice, not implemented here).

Reuses the SAME grid-index cache (data/pilot_extraction.py) as the
original HRRR pilot -- same grid fingerprint, same Central Park patch, so
caching applies immediately. Reuses the SAME Checkpoint/run_concurrent
architecture for identical checkpointed, resumable, crash-safe semantics.
Uses its OWN separate output/checkpoint directory
(data/processed/pilot/hrrr_rh/), never shared with the original HRRR pilot
or the APCP_1H backfill's directories.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.hrrr import grib_key, fetch_idx, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]
MAX_WORKERS = 24  # same as the original HRRR pilot -- not rebenchmarked, per instruction, single-variable workload expected to behave similarly to the APCP_1H backfill
# First attempt (2026-09-29) aborted at 8774/20836 items: actual RH byte
# rate was ~1.6MB/item, not the ~0.33MB/item originally assumed -- a benign
# sizing miscalculation (confirmed via steady, non-anomalous accumulation
# rate in the run log, not a product/semantic issue -- RH's find_message
# selectivity was already validated live before this script was written).
# Revised estimate: 20,836 * 1.6MB ~= 33.3GB total; budget set with margin.
BYTE_BUDGET = 45e9

OUT_DIR = ROOT / "data/processed/pilot/hrrr_rh"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_hrrr_rh_checkpoint.json"


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    """IDENTICAL logic to scripts/pilot_phase3_hrrr.py's function of the
    same name -- guarantees this backfill's work-item set is exactly the
    original pilot's 20,836 (run_time, forecast_hour) pairs."""
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
    Fetches ONLY the RH message -- every other HRRR variable is already
    persisted in the original pilot and is never re-fetched or touched here."""
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
                "value_f": None,  # RH is %, never a temperature -- value_f is exclusively for Kelvin-unit fields (see the variable-mixing fix, 2026-09-29)
                "source_object": key,
            }
        )
    return rows, total_bytes


def main():
    _log(f"HRRR RH BACKFILL starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"hrrr_rh|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Total (run,forecast_hour) work items: {len(work_items)} across {len(run_times)} runs (expect 20,836, matching the original HRRR pilot exactly)")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_hour, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="hrrr_rh")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("HRRR RH BACKFILL finished." if status == "DONE" else "HRRR RH BACKFILL stopped (see status).")


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
    with open(OUT_DIR / "pilot_hrrr_rh_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
