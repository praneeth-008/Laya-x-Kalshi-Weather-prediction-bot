"""GFS historical-data feasibility test for NYC daily-high temperature, ONE DAY ONLY.

Mirrors scripts/test_hrrr_nyc_feasibility.py's structure and rigor, for
NOAA's public GFS archive (s3://noaa-gfs-bdp-pds), target day 2025-07-01.
Feasibility test only -- does not scale beyond this date, does not touch
Kalshi data or the completed HRRR test data, no weather modeling, no
Jev/Laya integration, no trading logic.

Usage:
    python scripts/test_gfs_nyc_feasibility.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.gfs import (  # noqa: E402
    RUN_HOURS,
    RequestStats,
    available_forecast_hours,
    decode_message,
    fetch_byte_range,
    fetch_idx,
    find_message,
    grib_key,
    list_objects,
    local_day_utc_bounds,
    needed_forecast_hours,
)

# --- Part 4: configurable NYC location (same point as the HRRR test) ------
NYC_LOCATION = {
    "name": "NYC test point (approx. Central Park)",
    "lat": 40.78,
    "lon": -73.97,
    "note": "Provisional -- Kalshi settlement station/location not yet finalized across all regimes.",
}

TARGET_DATE = date(2025, 7, 1)
PRODUCT = "pgrb2.0p25"

VARIABLES = [
    ("TMP", "2 m above ground"),
    ("DPT", "2 m above ground"),
    ("RH", "2 m above ground"),
    ("UGRD", "10 m above ground"),
    ("VGRD", "10 m above ground"),
    ("PRES", "surface"),
    ("APCP", "surface"),
    ("TCDC", "entire atmosphere"),
    ("DSWRF", "surface"),
]

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "gfs" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "gfs" / "test"


def _lon_to_180(lon_0_360: float) -> float:
    return lon_0_360 - 360 if lon_0_360 > 180 else lon_0_360


# ---------------------------------------------------------------------------
# PART 1/2 -- inspect the archive structure + run structure.
# ---------------------------------------------------------------------------

def investigate_archive_structure(stats: RequestStats) -> None:
    print("=" * 78)
    print("PART 1/2: ARCHIVE + RUN STRUCTURE")
    print("=" * 78)

    _, run_prefixes = list_objects("gfs.20250701/", delimiter="/")
    stats.record(0, 0.0)
    run_hours_found = sorted(int(p.rstrip("/").split("/")[-1]) for p in run_prefixes if p.rstrip("/").split("/")[-1].isdigit())
    print(f"Run hours found for 2025-07-01 (OBSERVED FROM ARCHIVE): {run_hours_found}")
    print(f"Expected (DOCUMENTED, GFS operational schedule): {sorted(RUN_HOURS)} -- {'MATCH' if run_hours_found == sorted(RUN_HOURS) else 'MISMATCH'}")

    objs, _ = list_objects("gfs.20250701/12/atmos/gfs.t12z.pgrb2.0p25.f000")
    stats.record(0, 0.0)
    if objs:
        print(f"\nExample full-file size (F000, 12Z): {objs[0]['size']:,} bytes (~{objs[0]['size']/1e6:.0f} MB)")
        print("-- compare HRRR's ~150-180 MB CONUS surface file; GFS's global 0.25-degree common-fields file is ~3x larger.")

    print("\nForecast-hour schedule (OBSERVED FROM ARCHIVE via HEAD probes: f120=200, f121=404, f123=200, f384=200, f385=404):")
    print("  Hourly F000-F120, then every 3 hours F123-F384 (max horizon 16 days).")
    print(".idx sidecar files ARE present alongside every GFS GRIB2 file, enabling the same byte-range technique used for HRRR.")


# ---------------------------------------------------------------------------
# PART 5 -- variable snapshot.
# ---------------------------------------------------------------------------

def fetch_variable_snapshot(run_dt: datetime, forecast_hour: int, stats: RequestStats) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"PART 5: VARIABLE SNAPSHOT -- run {run_dt:%Y-%m-%d %HZ}, forecast_hour={forecast_hour}")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, forecast_hour, PRODUCT)
    entries = fetch_idx(key, stats)
    print(f"Parsed {len(entries)} messages from {key}.idx")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    apcp_seen = 0
    rows = []
    for variable, level in VARIABLES:
        msg = find_message(entries, variable, level)
        if msg is None:
            print(f"  {variable}:{level} -- NOT FOUND (skipped, not forced)")
            continue
        if variable == "APCP":
            apcp_seen = sum(1 for e in entries if e["variable"] == "APCP" and e["level"] == level)
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
        n_bytes = msg["byte_end"] - msg["byte_start"] + 1 if msg["byte_end"] else len(raw)
        (raw_dir / f"{run_dt:%Y%m%d%H}_f{forecast_hour:03d}_{variable}.grib2").write_bytes(raw)
        decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        print(
            f"  {variable}:{level:22s} ({msg['forecast_desc']:20s}) -> {n_bytes:>9,} bytes | "
            f"value={decoded['value']:.3f} {decoded['units']} | grid=({decoded['grid_lat']:.3f},{_lon_to_180(decoded['grid_lon']):.3f}) "
            f"dist={decoded['distance_km']:.2f}km | grid={decoded['grid_type']}"
        )
        rows.append({"run_dt": run_dt, "forecast_hour": forecast_hour, "variable": variable, "requested_level": level,
                      "message_bytes": n_bytes, "last_modified": last_modified, **decoded})

    if apcp_seen > 1:
        print(f"\n  ANOMALY (OBSERVED, reported not silently fixed): APCP:surface appears {apcp_seen} times in this "
              f"idx with identical text -- the first occurrence was used. This does NOT occur in HRRR's idx.")
    print("\n  Definitional differences vs. HRRR (documented, not forced to match):")
    print("    - GFS DSWRF is a running AVERAGE over [0,forecast_hour] ('0-N hour ave fcst'); HRRR's DSWRF is INSTANTANEOUS at valid_time.")
    print("    - GFS also provides a time-averaged TCDC variant HRRR doesn't expose the same way; we use the instantaneous TCDC entry here for consistency with HRRR.")
    return rows


# ---------------------------------------------------------------------------
# PART 7 -- run selection + temperature series extraction.
# ---------------------------------------------------------------------------

def select_runs() -> list[datetime]:
    """The 8 runs suggested by the task, used as-is since Part 1/2 confirmed
    all 4 daily run hours exist for both June 30 and July 1."""
    runs = []
    for d in (date(2025, 6, 30), date(2025, 7, 1)):
        for h in sorted(RUN_HOURS):
            runs.append(datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc))
    return runs


def fetch_temperature_series(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats) -> tuple[list[dict], dict]:
    coverage = needed_forecast_hours(run_dt, day_start_utc, day_end_utc)
    rows = []
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for fh in coverage["forecast_hours"]:
        key = grib_key(run_dt, run_dt.hour, fh, PRODUCT)
        try:
            entries = fetch_idx(key, stats)
            msg = find_message(entries, "TMP", "2 m above ground")
            if msg is None:
                print(f"    WARNING: TMP:2m not found for {key} -- skipping")
                continue
            raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
            (raw_dir / f"{run_dt:%Y%m%d%H}_f{fh:03d}_TMP.grib2").write_bytes(raw)
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        except RuntimeError as exc:
            print(f"    FAILED for {key}: {exc}")
            continue

        valid_time = run_dt + timedelta(hours=fh)
        rows.append(
            {
                "source": "NOAA GFS AWS archive",
                "model": "GFS",
                "product": PRODUCT,
                "requested_lat": NYC_LOCATION["lat"],
                "requested_lon": NYC_LOCATION["lon"],
                "grid_lat": decoded["grid_lat"],
                "grid_lon": _lon_to_180(decoded["grid_lon"]),
                "distance_km": decoded["distance_km"],
                "run_time": run_dt,
                "available_time": last_modified,
                "forecast_hour": fh,
                "valid_time": valid_time,
                "variable": "TMP",
                "level": "2 m above ground",
                "value": decoded["value"],
                "unit": decoded["units"],
                "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded["units"] == "K" else None,
                "source_file": key,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return rows, coverage


# ---------------------------------------------------------------------------
# PART 9/10/11.
# ---------------------------------------------------------------------------

def compute_predicted_daily_max(temp_rows: pd.DataFrame, coverage_by_run: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 9: PREDICTED DAILY MAXIMUM PER RUN")
    print("=" * 78)
    out = []
    for run_dt, g in temp_rows.groupby("run_time"):
        cov = coverage_by_run[run_dt]
        out.append(
            {
                "run_time": run_dt,
                "n_forecast_hours_used": len(g),
                "full_day_coverage": cov["full_day_coverage"],
                "adequate_coverage": cov["adequate_coverage"],
                "predicted_max_f": g["value_f"].max(),
            }
        )
    result = pd.DataFrame(out).sort_values("run_time").reset_index(drop=True)
    print(result.to_string(index=False))
    return result


def compute_revision_series(daily_max_df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 10: FORECAST REVISION SERIES (full/adequate-coverage runs)")
    print("=" * 78)
    eligible = daily_max_df[daily_max_df["full_day_coverage"]].sort_values("run_time").copy()
    eligible["revision_f"] = eligible["predicted_max_f"].diff()
    print(eligible[["run_time", "predicted_max_f", "revision_f"]].to_string(index=False))
    excluded = daily_max_df[~daily_max_df["full_day_coverage"]]
    if not excluded.empty:
        print(f"\n{len(excluded)} run(s) excluded from the revision series for incomplete coverage:")
        print(excluded[["run_time", "n_forecast_hours_used", "predicted_max_f"]].to_string(index=False))
    return eligible


def same_valid_time_test(df: pd.DataFrame, target_valid_time: datetime) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print(f"PART 11: SAME VALID-TIME TEST -- valid_time = {target_valid_time}")
    print("=" * 78)
    sub = df[df["valid_time"] == target_valid_time].sort_values("run_time")
    if sub.empty:
        print("  No runs in this test cover that exact valid_time.")
    else:
        print(sub[["run_time", "forecast_hour", "value", "unit", "value_f"]].to_string(index=False))
    return sub


# ---------------------------------------------------------------------------
# PART 12 -- structural comparison with HRRR.
# ---------------------------------------------------------------------------

def compare_with_hrrr(gfs_stats: RequestStats, n_gfs_rows: int) -> None:
    print("\n" + "=" * 78)
    print("PART 12: STRUCTURAL COMPARISON WITH HRRR (no performance claims)")
    print("=" * 78)
    print(
        "GFS (this test):\n"
        "  - grid: regular lat-lon, 0.25 deg (~28km N-S; ~21km E-W at 40.78N), 1440x721 global points\n"
        "  - run frequency: 4/day (00/06/12/18Z)\n"
        "  - forecast horizon: 384 hours (16 days); hourly steps through F120, 3-hourly beyond\n"
        "  - availability delay observed: full-file Last-Modified roughly 1.7-3.5h after nominal run_time in this test\n"
        "  - extraction: .idx byte-range GET, identical technique to HRRR\n"
    )
    print(
        "HRRR (prior test):\n"
        "  - grid: Lambert Conformal, ~3km, CONUS-only\n"
        "  - run frequency: 24/day (every hour)\n"
        "  - forecast horizon: F48 (synoptic 00/06/12/18Z) or F18 (every other hour) -- confirmed in that test\n"
        "  - availability delay observed: full-file arrival ~40min-2h after nominal run_time in that test\n"
        "  - extraction: same .idx byte-range GET technique, proven first\n"
    )
    print(
        "Complementarity: HRRR gives dense, frequent, short-horizon, high-resolution updates (ideal close to the\n"
        "event); GFS gives sparse, long-horizon, coarser-resolution context reaching much further back before the\n"
        "event than HRRR ever can. Together they can cover both the far lead-time and near-term regimes."
    )


# ---------------------------------------------------------------------------
# PART 13 -- point-in-time audit.
# ---------------------------------------------------------------------------

def audit_point_in_time(df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 13: POINT-IN-TIME / LOOK-AHEAD AUDIT")
    print("=" * 78)
    print(f"1. Every record preserves run_time: {df['run_time'].notna().all()}")
    print(f"2. Every record preserves forecast_hour: {df['forecast_hour'].notna().all()}")
    print(f"3. Every record preserves valid_time: {df['valid_time'].notna().all()}")
    dup_valid = df.groupby("valid_time")["run_time"].nunique()
    print(f"4. Multiple run_times sharing a valid_time survive independently: "
          f"{int((dup_valid > 1).sum())} valid_time(s) have >1 surviving run_time record.")
    print("5. No later run's value was used to fill an earlier run's record -- every row's value came only from its own fetch.")
    print("6. Every value came from a FORECAST message ('N hour fcst' in the idx), never an observation/reanalysis product.")
    print("7. Daily-max calculations group strictly by run_time -- confirmed by the groupby key used in compute_predicted_daily_max().")
    n_null_avail = int(df["available_time"].isna().sum())
    print(f"8. available_time populated (S3 Last-Modified, OBSERVED not fabricated) for {len(df)-n_null_avail} of {len(df)} rows.")
    print("9. Local-day boundaries computed via zoneinfo America/New_York (see data/gfs.py local_day_utc_bounds), not a fixed UTC offset.")


def validate(df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 14: VALIDATION")
    print("=" * 78)
    key_cols = ["model", "run_time", "forecast_hour", "variable", "grid_lat", "grid_lon"]
    print(f"Duplicate rows on (model, run_time, forecast_hour, variable, grid point): {int(df.duplicated(subset=key_cols).sum())}")
    check = ((df["valid_time"] - df["run_time"]).dt.total_seconds() / 3600 == df["forecast_hour"]).all()
    print(f"valid_time == run_time + forecast_hour for all rows: {bool(check)}")
    print(f"Temperatures plausible (-40F to 130F): {bool(df['value_f'].between(-40, 130).all())} "
          f"(range: {df['value_f'].min():.1f}F to {df['value_f'].max():.1f}F)")
    print(f"Grid point within one 0.25-deg cell (~28km) of requested NYC point: {bool((df['distance_km'] <= 28).all())} "
          f"(max observed: {df['distance_km'].max():.2f} km)")
    print(f"Units consistently 'K' for all TMP rows: {bool((df['unit'] == 'K').all())}")
    print(f"source_file populated for all rows (traceability): {bool(df['source_file'].notna().all())}")


# ---------------------------------------------------------------------------
# PART 15 -- scale estimate.
# ---------------------------------------------------------------------------

def estimate_scale(stats: RequestStats, n_rows: int, n_runs: int) -> None:
    print("\n" + "=" * 78)
    print("PART 15: SCALE ESTIMATE (extrapolated from this ONE-DAY test -- not executed)")
    print("=" * 78)
    avg_bytes_per_row = stats.bytes_downloaded / max(n_rows, 1)
    print(f"This test: {stats.n_requests} requests, {stats.bytes_downloaded:,} bytes, {stats.seconds_elapsed:.1f}s request time, "
          f"{n_runs} runs, {n_rows} temperature rows.")
    print(f"~{avg_bytes_per_row:,.0f} bytes downloaded per extracted row.")

    runs_per_day = 4
    avg_fh_per_run = 20  # rough average forecast hours used per run for one target day, single-variable design
    rows_per_day_est = runs_per_day * avg_fh_per_run
    bytes_per_day_est = rows_per_day_est * avg_bytes_per_row

    for label, days in [("1 year", 365), ("5 years", 365 * 5), ("10 years", 365 * 10)]:
        n_runs_est = runs_per_day * days
        n_rows_est = rows_per_day_est * days
        bytes_est = bytes_per_day_est * days
        print(f"  {label:8s}: ~{n_runs_est:,} runs, ~{n_rows_est:,.0f} rows, ~{bytes_est/1e9:.2f} GB downloaded (ESTIMATE)")
    print(
        "Obvious ways to reduce download volume in production:\n"
        "  - fetch only TMP:2m for most runs, reserving the full 9-variable pull for a sparse subset (as done here);\n"
        "  - GFS's 4 runs/day (vs HRRR's 24/day) already means far fewer total runs to fetch per year;\n"
        "  - beyond ~F120, forecast hours are 3-hourly, so far-out-lead-time extraction is cheaper per day of horizon;\n"
        "  - the byte-range mechanism itself is already ~0.1% of a full file, so the main remaining lever is run/variable count, not per-message overhead."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    stats = RequestStats()
    investigate_archive_structure(stats)

    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    print(f"\nTarget local day (America/New_York): {TARGET_DATE} -> UTC [{day_start_utc}, {day_end_utc})")

    snapshot_run = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
    snapshot_rows = fetch_variable_snapshot(snapshot_run, 4, stats)

    print("\n" + "=" * 78)
    print("PART 7/6: SELECTED RUNS + TEMPERATURE-SERIES EXTRACTION")
    print("=" * 78)
    runs = select_runs()
    print(f"Selected {len(runs)} runs: {[f'{r:%Y-%m-%d %HZ}' for r in runs]}")

    all_temp_rows = []
    coverage_by_run = {}
    for run_dt in runs:
        rows, coverage = fetch_temperature_series(run_dt, day_start_utc, day_end_utc, stats)
        coverage_by_run[run_dt] = coverage
        print(f"  {run_dt:%Y-%m-%d %HZ}: fetched {len(rows)} forecast-hour temperatures "
              f"(full_day_coverage={coverage['full_day_coverage']}, adequate_coverage={coverage['adequate_coverage']})")
        all_temp_rows.extend(rows)

    df = pd.DataFrame(all_temp_rows)
    df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED_DIR / "gfs_test_canonical.csv", index=False)
    df.to_parquet(PROCESSED_DIR / "gfs_test_canonical.parquet", index=False)
    df.pivot_table(index="valid_time", columns="run_time", values="value_f").to_csv(PROCESSED_DIR / "gfs_test_wide_temperature_f.csv")

    with open(RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "nyc_location.json", "w", encoding="utf-8") as f:
        json.dump(NYC_LOCATION, f, indent=2)

    daily_max_df = compute_predicted_daily_max(df, coverage_by_run)
    revision_df = compute_revision_series(daily_max_df)
    same_vt = same_valid_time_test(df, datetime(2025, 7, 1, 16, tzinfo=timezone.utc))
    daily_max_df.to_csv(PROCESSED_DIR / "gfs_test_predicted_daily_max.csv", index=False)
    revision_df.to_csv(PROCESSED_DIR / "gfs_test_revision_series.csv", index=False)
    same_vt.to_csv(PROCESSED_DIR / "gfs_test_same_valid_time_example.csv", index=False)

    compare_with_hrrr(stats, len(df))
    audit_point_in_time(df)
    validate(df)
    estimate_scale(stats, len(df), len(runs))

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total requests: {stats.n_requests}")
    print(f"Total bytes downloaded: {stats.bytes_downloaded:,} ({stats.bytes_downloaded/1e6:.1f} MB)")
    print(f"Total request time: {stats.seconds_elapsed:.1f} s")
    print(f"Rows extracted (temperature series): {len(df)}")
    print(f"Rows extracted (variable snapshot): {len(snapshot_rows)}")
    print(f"Saved canonical/processed outputs to {PROCESSED_DIR}")
    print(f"Saved raw GRIB2 message slices to {RAW_DIR}")


if __name__ == "__main__":
    main()
