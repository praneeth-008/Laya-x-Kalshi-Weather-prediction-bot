"""Recalculate HRRR/GFS/NBM/GEFS/ECMWF cost estimates for the 40-day
REPRESENTATIVE pilot (selected_days.csv), including required lead-in runs
from the previous calendar day. No downloads -- pure arithmetic using
already-measured per-message sizes and the existing completeness-rule
functions from data/weather_state.py.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.weather_state import expected_target_day_valid_times, local_day_utc_bounds, NYC_TZ

ROOT = Path(__file__).resolve().parents[1]
selected = pd.read_csv(ROOT / "data/processed/weather/pilot/selected_days.csv")
SELECTED_DATES = sorted(pd.to_datetime(selected["date"]).dt.date.tolist())

# Measured average per-message sizes (MB), from the archive-depth/cost audit.
MSG_SIZE_MB = {
    "hrrr": {"TMP": 1.25, "DPT": 1.19, "UGRD": 2.38, "VGRD": 2.38, "PRES": 1.50, "TCDC": 0.78, "APCP": 0.33, "DSWRF": 2.62},
    "gfs": {"TMP": 0.52, "DPT": 0.54, "UGRD": 0.98, "VGRD": 0.96, "PRES": 0.84, "TCDC": 0.84, "APCP": 0.35, "DSWRF": 0.87},
    "nbm": {"TMP": 1.44, "DPT": 1.33, "TCDC": 1.74, "TMAX_direct": 1.53, "TMP_stddev": 1.44},
    "gefs": {"TMP": 0.44, "DPT": 0.44, "UGRD": 0.84, "VGRD": 0.82, "PRES": 0.74, "APCP": 0.29},
    "ecmwf": {"2t": 0.66, "mx2t3": 0.65, "2d": 0.68, "10u": 0.87, "10v": 0.86, "sp": 0.54, "tp": 0.94},
}
MEASURED_THROUGHPUT_ITEMS_PER_SEC = {"hrrr": 0.486}  # 12-process ProcessPoolExecutor, measured


def run_times_for_selected_days(max_lookback_days=1):
    """All hourly UTC run_times whose local-NYC calendar date is a selected
    day or the day immediately before it (a documented simplification: a
    run from 2+ days before a target day, only relevant for HRRR/GFS's
    extended-hour synoptic runs reaching the far end of their horizon, is
    not included here -- flagged, not silently claimed complete)."""
    dates_needed = set()
    for d in SELECTED_DATES:
        dates_needed.add(d)
        dates_needed.add(d - timedelta(days=max_lookback_days))
    run_times = []
    for d in sorted(dates_needed):
        for h in range(24):
            run_times.append(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
    return sorted(set(run_times))


def hrrr_gfs_work_items(source_name: str, run_hours_per_day: list[int]):
    run_times = [rt for rt in run_times_for_selected_days() if rt.hour in run_hours_per_day]
    total_fh_slices = 0
    for rt in run_times:
        local_date = rt.astimezone(NYC_TZ).date()
        fhs = set()
        for d in (local_date, local_date + timedelta(days=1)):
            if d not in SELECTED_DATES:
                continue
            day_start, day_end = local_day_utc_bounds(d)
            for vt in expected_target_day_valid_times(source_name, rt, day_start, day_end):
                fh = int((vt - rt).total_seconds() / 3600)
                if fh >= 0:
                    fhs.add(fh)
        total_fh_slices += len(fhs)
    return len(run_times), total_fh_slices


def report_deterministic(name, source_key, run_hours, variables, msg_size_key=None):
    n_runs, n_fh_slices = hrrr_gfs_work_items(source_key, run_hours)
    msg_size_key = msg_size_key or source_key
    avg_msg = sum(MSG_SIZE_MB[msg_size_key][v] for v in variables) / len(variables)
    remote_mb = n_fh_slices * len(variables) * avg_msg
    print(f"\n=== {name} ===")
    print(f"  runs (incl. lead-in): {n_runs}")
    print(f"  (run, forecast_hour) work items: {n_fh_slices}")
    print(f"  variables: {len(variables)}")
    print(f"  remote bytes: {remote_mb/1e3:.2f} GB")
    if source_key in MEASURED_THROUGHPUT_ITEMS_PER_SEC:
        secs = n_fh_slices / MEASURED_THROUGHPUT_ITEMS_PER_SEC[source_key]
        print(f"  estimated wall-clock (measured throughput): {secs/3600:.2f} hours")
    else:
        print(f"  estimated wall-clock: NOT YET MEASURED for this source -- see note")
    # Processed storage: rows = fh_slices * variables * 9 patch points, ~35 bytes/row optimized parquet.
    rows = n_fh_slices * len(variables) * 9
    print(f"  processed rows (3x3 patch): {rows}, est. processed size: {rows*35/1e6:.2f} MB")
    return {"n_runs": n_runs, "n_work_items": n_fh_slices, "remote_gb": remote_mb / 1e3, "processed_mb": rows * 35 / 1e6}


def main():
    print(f"Selected days: {len(SELECTED_DATES)}")
    print(f"Date range: {SELECTED_DATES[0]} to {SELECTED_DATES[-1]}")

    hrrr_stats = report_deterministic("HRRR", "hrrr", list(range(24)), list(MSG_SIZE_MB["hrrr"].keys()))
    gfs_stats = report_deterministic("GFS", "gfs", [0, 6, 12, 18], list(MSG_SIZE_MB["gfs"].keys()))

    # NBM: mirrors HRRR/GFS mechanics but with its own message sizes; reuse the same function name "nbm" for schedule.
    nbm_stats = report_deterministic("NBM (24 runs/day, deliberate for cadence audit)", "nbm", list(range(24)), list(MSG_SIZE_MB["nbm"].keys()))

    # ECMWF deterministic.
    ecmwf_stats = report_deterministic("ECMWF deterministic", "ecmwf_deterministic", [0, 6, 12, 18], list(MSG_SIZE_MB["ecmwf"].keys()), msg_size_key="ecmwf")

    # GEFS: separate treatment (31 members).
    print("\n=== GEFS (31 members) ===")
    n_runs, n_fh_slices = hrrr_gfs_work_items("gefs", [0, 6, 12, 18])
    print(f"  runs (incl. lead-in): {n_runs}, (run,fh) slices: {n_fh_slices}")
    scenarios = {
        "A: TMP only, all 31 members": (["TMP"], 31, [], 0),
        "B: TMP+DPT, all 31 members": (["TMP", "DPT"], 31, [], 0),
        "C: TMP+DPT+UGRD+VGRD+PRES+APCP, all 31 members": (["TMP", "DPT", "UGRD", "VGRD", "PRES", "APCP"], 31, [], 0),
        "D: TMP+DPT all members; UGRD/VGRD/PRES/APCP control-member only": (["TMP", "DPT"], 31, ["UGRD", "VGRD", "PRES", "APCP"], 1),
    }
    gefs_scenario_results = {}
    for label, (all_member_vars, n_members_all, control_only_vars, n_members_control) in scenarios.items():
        avg_all = sum(MSG_SIZE_MB["gefs"][v] for v in all_member_vars) / len(all_member_vars) if all_member_vars else 0
        avg_control = sum(MSG_SIZE_MB["gefs"][v] for v in control_only_vars) / len(control_only_vars) if control_only_vars else 0
        remote_mb = n_fh_slices * (len(all_member_vars) * n_members_all * avg_all + len(control_only_vars) * n_members_control * avg_control)
        rows = n_fh_slices * (len(all_member_vars) * n_members_all + len(control_only_vars) * n_members_control)
        print(f"  {label}: remote={remote_mb/1e3:.2f} GB, processed={rows*35/1e6:.2f} MB, rows={rows}")
        gefs_scenario_results[label] = {"remote_gb": remote_mb / 1e3, "processed_mb": rows * 35 / 1e6}

    print("\n=== TOTALS (using GEFS scenario D as the working assumption, deterministic sources unchanged) ===")
    total_remote = hrrr_stats["remote_gb"] + gfs_stats["remote_gb"] + nbm_stats["remote_gb"] + ecmwf_stats["remote_gb"] + gefs_scenario_results["D: TMP+DPT all members; UGRD/VGRD/PRES/APCP control-member only"]["remote_gb"]
    total_processed = hrrr_stats["processed_mb"] + gfs_stats["processed_mb"] + nbm_stats["processed_mb"] + ecmwf_stats["processed_mb"] + gefs_scenario_results["D: TMP+DPT all members; UGRD/VGRD/PRES/APCP control-member only"]["processed_mb"]
    hrrr_hours = hrrr_stats["n_work_items"] / MEASURED_THROUGHPUT_ITEMS_PER_SEC["hrrr"] / 3600
    print(f"  TOTAL remote bytes: {total_remote:.2f} GB")
    print(f"  TOTAL processed size: {total_processed:.2f} MB")
    print(f"  HRRR wall-clock (measured throughput): {hrrr_hours:.2f} hours")
    print("  GFS/NBM/GEFS/ECMWF wall-clock: NOT YET MEASURED at scale -- HRRR's 12-process eccodes throughput is our only real benchmark; others will be measured directly once launched, each source's own decode cost may differ (smaller GEFS/ECMWF grids likely faster per message).")


if __name__ == "__main__":
    main()
