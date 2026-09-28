"""PILOT Phase 7 -- ECMWF IFS deterministic (oper stream), 40 representative
days + lead-in, 00/12Z run hours, Central Park point + 3x3 patch.
Structurally mirrors scripts/pilot_phase3_hrrr.py / pilot_phase4_gfs.py /
pilot_phase5_nbm.py / pilot_phase6_gefs.py (same
Checkpoint/run_concurrent/flush-dir architecture).

APPROVED FOR FULL-SCALE LAUNCH: ECMWF deterministic production-readiness
validation passed -- see docs/ecmwf_pilot_readiness.md. Production commit:
c95b54d802dcea5599939bbe00c2ca87793bfbfb.

Production output (data/processed/pilot/ecmwf/) is a DEDICATED directory,
never shared with any validation/benchmark script -- all prior ECMWF
validation/benchmark work in this project ran entirely from Claude's
scratchpad directory, outside the repo and outside this path (same
contamination-prevention discipline established after the NBM pilot).

Key ECMWF-specific findings (all empirically verified 2026-09-28 across 5
selected-pilot dates, both run hours, steps 0-360 -- see
docs/ecmwf_pilot_readiness.md):

1. ARCHIVE PATH REGIME: this module's grib_key() (ifs/0p25) is only valid
   from 2024-02-29 onward (independently confirmed via 21-date probe) --
   every selected pilot day (2025-01-02 to 2025-08-24) falls safely inside
   this single regime. Older regimes (0p4-beta, bare 0p25) exist but are
   NOT implemented here.
2. Run cadence: oper (deterministic) confirmed present ONLY at 00Z/12Z
   across every date tested (06Z/18Z confirmed absent for oper, though
   enfo/ensemble IS present at all 4 hours -- out of scope here).
3. Forecast schedule: 3-hourly F0-F144, then 6-hourly F150-F360, identical
   at both run hours, identical across every date tested.
4. F0: instant fields (2t/2d/10u/10v/sp) ARE present and meaningful. tp is
   present but DEGENERATE (stepType=accum, startStep=endStep=0, value=0.0)
   -- a zero-width window, not real. mx2t3/mn2t3 at F0 are ALSO degenerate
   placeholders: same declared window as the eventual F3 file's real
   message (startStep=0,endStep=3) but value=0.0 KELVIN (physically
   impossible -- 0K is absolute zero -- an unambiguous placeholder/fill
   marker, not a real temperature). All three fields are explicitly SKIPPED
   at forecast_hour=0 here; mx2t3/mn2t3's first REAL value appears in the
   F3 file's own message.
5. tp is PURE CUMULATIVE-SINCE-RUN-START at every forecast hour tested,
   including F360 (startStep ALWAYS 0) -- unlike GEFS's 6h-reset pattern.
   A windowed/"recent precipitation" signal would require differencing
   consecutive tp values ourselves (same situation HRRR's raw APCP was in
   before its APCP_1H backfill) -- not implemented in this validation
   phase; only the native cumulative field is extracted.
6. mx2t3/mn2t3 are a clean, ALWAYS-3-HOUR rolling window (startStep=
   endStep-3) for forecast hours in the 3-hourly regime (F3-F144).
   CRITICAL: at F150 and beyond (the 6-hourly regime), these fields are
   REPLACED by mx2t6/mn2t6 (a symmetric ALWAYS-6-HOUR rolling window,
   startStep=endStep-6) -- mx2t3/mn2t3 are structurally ABSENT there. This
   was caught during the validation sample (mx2t3/mn2t3 silently missing
   from 3 of 10 non-F0 items, all exactly at F150/F240/F360) and fixed here
   by selecting the correct param name based on which schedule regime the
   forecast hour falls in, rather than assuming one name applies throughout
   the full horizon.
7. No native 2m relative humidity or cloud-cover field exists in this
   product (confirmed by exhaustively listing all params in the index --
   'r' only appears at pressure levels, no tcc/hcc/mcc/lcc/cc at any
   level) -- NOT derived here (would require a computed transform from
   2t/2d, never implemented for any other source in this project either).
   ssrd (surface solar radiation downward) IS present and used as the
   DSWRF-equivalent radiation field.
8. Grid: regular_ll, 0.25deg, Ni=1440/Nj=721 -- SAME cell size as GFS/GEFS
   but a DIFFERENT md5GridSection fingerprint (different grid origin
   longitude convention) -- independently confirmed, not assumed. The
   +/-0.03deg patch fully collapses to 1 cell (max distance 8.35km, same
   numeric value as GFS/GEFS since the cell size is identical).
9. AVAILABILITY -- the most significant finding: Last-Modified for EVERY
   forecast hour of a run (F0 through F360, the full 15-day horizon) falls
   within a 37-59 SECOND window, at a lag of essentially EXACTLY 514
   minutes (~8.57h) after nominal run_time -- confirmed uniform across 8
   different (date, run_hour) combinations, to within 1 minute every time.
   This is NOT genuine per-forecast-hour progressive dissemination like
   every NOAA source in this project; it is best explained as a single
   BULK ARCHIVE-SYNC EVENT that publishes the entire run's file set nearly
   atomically. Classified LOW CONFIDENCE as a per-forecast-hour
   availability signal (it cannot distinguish "F0 became available" from
   "F360 became available" -- they are indistinguishable in this archive),
   but its extreme consistency makes it a reliable conservative floor: by
   ~514 minutes after run_time, the entire run is reliably present.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.ecmwf import (
    grib_key, fetch_index, find_message, fetch_byte_range, temporal_metadata,
    MAX_PATCH_DISTANCE_KM, RUN_HOURS,
)
from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ
from data.pilot_extraction import Checkpoint, run_concurrent, decode_message_multi_point, _log

ROOT = Path(__file__).resolve().parents[1]
SELECTED_DATES = sorted(pd.to_datetime(pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")["date"]).dt.date.tolist())
CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]

# Instant fields -- present at every forecast hour including F0.
INSTANT_VARS = ["2t", "2d", "10u", "10v", "sp"]
# Windowed fields -- structurally degenerate/absent at F0, explicitly skipped there.
WINDOWED_VARS = ["tp", "ssrd"]
# mx2t3/mn2t3 (3-hourly regime, F3-F144) are REPLACED by mx2t6/mn2t6 (6-hourly
# regime, F150+) -- structurally absent, not a selection ambiguity. The
# 3-hourly/6-hourly schedule boundary (F144/F150) is the same one validated
# for the overall forecast-hour schedule.
PERIOD_MAXMIN_REGIME_BOUNDARY_HOURS = 144

MAX_WORKERS = 16  # independently benchmarked for ECMWF (docs/ecmwf_pilot_readiness.md sections 30-31): highest single-trial throughput, avoids the dip observed at 20 workers -- NOT inherited from HRRR/GFS/NBM/GEFS; this bucket rate-limits more aggressively and shows substantially higher run-to-run variance (data/ecmwf.py's own retry/backoff design absorbs this transparently).
# ~5.6 GB estimated remote download for the full 904-item pilot (measured
# ~6.2 MB/item during benchmarking); 2x safety margin, same policy as every other source.
BYTE_BUDGET = 5.6e9 * 2

OUT_DIR = ROOT / "data/processed/pilot/ecmwf"
FLUSH_DIR = OUT_DIR / "parts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_ecmwf_checkpoint.json"


def run_times_for_selected_days():
    dates_needed = set()
    for d in SELECTED_DATES:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=1))
    run_times = []
    for d in sorted(dates_needed):
        for h in sorted(RUN_HOURS):
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def required_forecast_hours_for_run(run_time: datetime) -> set[int]:
    local_date = run_time.astimezone(NYC_TZ).date()
    fhs = set()
    for d in (local_date, local_date + timedelta(days=1)):
        if d not in SELECTED_DATES:
            continue
        day_start, day_end = local_day_utc_bounds(d)
        for vt in expected_target_day_valid_times("ecmwf_deterministic", run_time, day_start, day_end):
            fh = int((vt - run_time).total_seconds() / 3600)
            if fh >= 0:
                fhs.add(fh)
    return fhs


def extract_one_run_step(run_time: datetime, step: int):
    """Picklable worker -- no shared-state args (ProcessPoolExecutor)."""
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
    _log(f"ECMWF PILOT starting: {len(SELECTED_DATES)} selected target days, MAX_WORKERS={MAX_WORKERS}")
    _log(f"Network-safety byte budget: {BYTE_BUDGET/1e9:.1f} GB")

    checkpoint = Checkpoint(CHECKPOINT_PATH)
    run_times = run_times_for_selected_days()
    work_items = []
    for rt in run_times:
        for fh in sorted(required_forecast_hours_for_run(rt)):
            key = f"ecmwf|{rt.isoformat()}|{fh}"
            work_items.append((key, (rt, fh)))
    _log(f"Total (run,forecast_hour) work items: {len(work_items)} across {len(run_times)} runs")

    status = "DONE"
    try:
        result = run_concurrent(work_items, extract_one_run_step, checkpoint, FLUSH_DIR,
                                 max_workers=MAX_WORKERS, save_every=25, byte_budget=BYTE_BUDGET, label="ecmwf")
    except RuntimeError as e:
        _log(f"ABORTED: {e}")
        status = "ABORTED_NETWORK_SAFETY"
        result = {}

    write_report(checkpoint, result, status)
    _log("ECMWF PILOT finished." if status == "DONE" else "ECMWF PILOT stopped (see status).")


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
    with open(OUT_DIR / "pilot_ecmwf_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    _log(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
