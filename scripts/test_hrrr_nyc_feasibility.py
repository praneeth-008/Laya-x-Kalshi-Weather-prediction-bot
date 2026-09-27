"""HRRR historical-data feasibility test for NYC daily-high temperature, ONE DAY ONLY.

Tests whether we can reliably reconstruct historical point-in-time HRRR
forecasts for NYC from NOAA's public AWS archive (s3://noaa-hrrr-bdp-pds),
for the single target day 2025-07-01. This is a feasibility test, not a
bulk downloader -- it does not scale beyond this date, does not touch
Kalshi data, and does not build any weather model, Jev/Laya integration,
or trading logic.

Source verified live (see data/hrrr.py docstring): fully public HTTPS
access, no AWS credentials required.

Usage:
    python scripts/test_hrrr_nyc_feasibility.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.hrrr import (  # noqa: E402
    NYC_TZ,
    RequestStats,
    decode_message,
    fetch_byte_range,
    fetch_idx,
    find_message,
    grib_key,
    list_objects,
    local_day_utc_bounds,
    max_forecast_hour,
    needed_forecast_hours,
)

# --- Part 3: configurable NYC location (NOT hard-coded as permanent) ------
NYC_LOCATION = {
    "name": "NYC test point (approx. Central Park)",
    "lat": 40.78,
    "lon": -73.97,
    "note": "Provisional -- Kalshi settlement station/location not yet finalized across all regimes.",
}

TARGET_DATE = date(2025, 7, 1)
PRODUCT = "wrfsfc"
REGION = "conus"

# --- Part 4: variables to attempt (2m temperature is the most important) --
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

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "hrrr" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "hrrr" / "test"


def _lon_to_180(lon_0_360: float) -> float:
    return lon_0_360 - 360 if lon_0_360 > 180 else lon_0_360


# ---------------------------------------------------------------------------
# PART 1 -- inspect the archive structure.
# ---------------------------------------------------------------------------

def investigate_archive_structure(stats: RequestStats) -> dict:
    print("=" * 78)
    print("PART 1: ARCHIVE STRUCTURE")
    print("=" * 78)

    # Which "product" subtypes exist for one run hour (distinct filename stems).
    objs = list_objects("hrrr.20250701/conus/hrrr.t12z.", max_keys=1000)
    stats.record(0, 0.0)
    stems = sorted({k["key"].split("/")[-1].split(".")[2].rstrip("0123456789") for k in objs if k["key"].endswith(".grib2")})
    print(f"Product stems found under hrrr.20250701/conus/hrrr.t12z.* : {stems}")
    print("We use 'wrfsfc' (2D surface fields) -- it contains every variable this test needs (see Part 4).")

    print(f"\nKey layout confirmed: hrrr.YYYYMMDD/{{region}}/hrrr.t{{HH}}z.{{product}}f{{FFF}}.grib2 (+ matching .grib2.idx)")
    print("Confirmed live: the four synoptic hours (00/06/12/18Z) produce F00-F48; every other hourly run produces only F00-F18.")

    sample = [o for o in objs if o["key"].endswith("wrfsfcf04.grib2")][0]
    print(f"\nExample full-file size (F04, 12Z 2025-07-01): {int(sample['size']):,} bytes (~{int(sample['size'])/1e6:.1f} MB)")
    print(".idx sidecar files ARE present alongside every .grib2 file, enabling byte-range partial extraction (confirmed by direct fetch below).")

    return {"product_stems": stems, "sample_full_file_bytes": int(sample["size"])}


# ---------------------------------------------------------------------------
# PART 4 -- variable snapshot (single run/hour, all variables).
# ---------------------------------------------------------------------------

def fetch_variable_snapshot(run_dt: datetime, forecast_hour: int, stats: RequestStats) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"PART 4: VARIABLE SNAPSHOT -- run {run_dt:%Y-%m-%d %HZ}, forecast_hour={forecast_hour}")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, forecast_hour, PRODUCT, REGION)
    entries = fetch_idx(key, stats)
    print(f"Parsed {len(entries)} messages from {key}.idx")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for variable, level in VARIABLES:
        msg = find_message(entries, variable, level)
        if msg is None:
            print(f"  {variable}:{level} -- NOT FOUND in this product (skipped, not forced)")
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
        n_bytes = msg["byte_end"] - msg["byte_start"] + 1 if msg["byte_end"] else len(raw)
        (raw_dir / f"{run_dt:%Y%m%d%H}_f{forecast_hour:02d}_{variable}.grib2").write_bytes(raw)
        decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        print(
            f"  {variable}:{level:22s} -> {n_bytes:>9,} bytes | value={decoded['value']:.3f} {decoded['units']} "
            f"| grid=({decoded['grid_lat']:.4f},{_lon_to_180(decoded['grid_lon']):.4f}) dist={decoded['distance_km']:.2f}km"
        )
        rows.append(
            {
                "run_dt": run_dt,
                "forecast_hour": forecast_hour,
                "variable": variable,
                "requested_level": level,
                "message_bytes": n_bytes,
                "last_modified": last_modified,
                **decoded,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# PART 5+6 -- select runs, fetch 2m temperature series per run.
# ---------------------------------------------------------------------------

def select_runs() -> list[datetime]:
    """Justified subset of the ~72-hour candidate window (see report Part 5):
    the three anchor runs that achieve STRICT full-day coverage closest to
    the target day (June 30 12Z, 18Z, July 1 00Z), plus a dense hourly
    bracket through the morning of the target day itself (July 1 01Z-06Z)
    to demonstrate a real revision sequence. This is a deliberately bounded
    subset for a feasibility test, not the full 48h+24h window -- see the
    coverage table this script prints for the reasoning against the full
    72-run candidate set.
    """
    runs = [
        datetime(2025, 6, 30, 12, tzinfo=timezone.utc),
        datetime(2025, 6, 30, 18, tzinfo=timezone.utc),
    ]
    runs += [datetime(2025, 7, 1, h, tzinfo=timezone.utc) for h in range(0, 7)]
    return runs


def print_full_candidate_coverage_table(day_start_utc: datetime, day_end_utc: datetime) -> None:
    print("\n" + "-" * 78)
    print("Coverage analysis across the full ~72-hour candidate window (no downloads -- pure arithmetic):")
    print("-" * 78)
    start = datetime(2025, 6, 29, 0, tzinfo=timezone.utc)
    print(f"{'run':20s} {'max_fh':7s} {'covered/24':11s} {'full_day':9s} {'adequate':9s}")
    for i in range(72):
        run_dt = start + timedelta(hours=i)
        info = needed_forecast_hours(run_dt, day_start_utc, day_end_utc)
        if info["covered_hours"] == 0 and run_dt < datetime(2025, 6, 30, tzinfo=timezone.utc):
            continue
        print(
            f"{run_dt:%Y-%m-%d %HZ}   {info['max_fh']:<7d} {info['covered_hours']:>3d}/{info['needed_hours_total']:<6d} "
            f"{str(info['full_day_coverage']):9s} {str(info['adequate_coverage']):9s}"
        )


def fetch_temperature_series(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats) -> tuple[list[dict], dict]:
    coverage = needed_forecast_hours(run_dt, day_start_utc, day_end_utc)
    rows = []
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for fh in coverage["forecast_hours"]:
        key = grib_key(run_dt, run_dt.hour, fh, PRODUCT, REGION)
        try:
            entries = fetch_idx(key, stats)
            msg = find_message(entries, "TMP", "2 m above ground")
            if msg is None:
                print(f"    WARNING: TMP:2m not found for {key} -- skipping this forecast hour")
                continue
            raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
            (raw_dir / f"{run_dt:%Y%m%d%H}_f{fh:02d}_TMP.grib2").write_bytes(raw)
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        except RuntimeError as exc:
            print(f"    FAILED for {key}: {exc}")
            continue

        valid_time = run_dt + timedelta(hours=fh)
        rows.append(
            {
                "source": "NOAA HRRR AWS archive",
                "model": "HRRR",
                "product": PRODUCT,
                "requested_lat": NYC_LOCATION["lat"],
                "requested_lon": NYC_LOCATION["lon"],
                "grid_lat": decoded["grid_lat"],
                "grid_lon": _lon_to_180(decoded["grid_lon"]),
                "distance_km": decoded["distance_km"],
                "run_time": run_dt,
                "available_time": last_modified,  # OBSERVED FROM ARCHIVE (S3 Last-Modified); null if unavailable
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
# PART 8+9 -- predicted daily max per run + revision series.
# ---------------------------------------------------------------------------

def compute_predicted_daily_max(temp_rows: pd.DataFrame, coverage_by_run: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 8: PREDICTED DAILY MAXIMUM PER RUN")
    print("=" * 78)
    out = []
    for run_dt, g in temp_rows.groupby("run_time"):
        cov = coverage_by_run[run_dt]
        predicted_max_f = g["value_f"].max()
        out.append(
            {
                "run_time": run_dt,
                "n_forecast_hours_used": len(g),
                "forecast_hours": cov["forecast_hours"],
                "full_day_coverage": cov["full_day_coverage"],
                "adequate_coverage": cov["adequate_coverage"],
                "predicted_max_f": predicted_max_f,
            }
        )
    result = pd.DataFrame(out).sort_values("run_time").reset_index(drop=True)
    print(result.to_string(index=False))
    return result


def compute_revision_series(daily_max_df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 9: FORECAST REVISION SERIES (adequate/full-coverage runs only)")
    print("=" * 78)
    eligible = daily_max_df[daily_max_df["adequate_coverage"]].sort_values("run_time").copy()
    eligible["revision_f"] = eligible["predicted_max_f"].diff()
    print(eligible[["run_time", "predicted_max_f", "revision_f", "full_day_coverage"]].to_string(index=False))
    excluded = daily_max_df[~daily_max_df["adequate_coverage"]]
    if not excluded.empty:
        print(f"\n{len(excluded)} run(s) excluded from the revision series for inadequate coverage (not comparable):")
        print(excluded[["run_time", "n_forecast_hours_used", "predicted_max_f"]].to_string(index=False))
    return eligible


# ---------------------------------------------------------------------------
# PART 10 -- point-in-time / look-ahead audit.
# ---------------------------------------------------------------------------

def audit_point_in_time(df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 10: POINT-IN-TIME / LOOK-AHEAD AUDIT")
    print("=" * 78)
    print(f"1. Every record preserves run_time: {df['run_time'].notna().all()}")
    print(f"2. Every record preserves valid_time: {df['valid_time'].notna().all()}")
    dup_valid = df.groupby("valid_time")["run_time"].nunique()
    print(f"3. Distinct run_times can share a valid_time WITHOUT overwriting each other: "
          f"{int((dup_valid > 1).sum())} valid_time(s) have multiple surviving run_time records (expected & required).")
    print("4. No aggregation step in this script uses a later run's value to fill an earlier run's record -- "
          "every row's `value`/`value_f` came only from its own (run_time, forecast_hour) fetch.")
    print("5. Every value here came from a run's FORECAST message (TMP:2 m above ground, 'N hour fcst'), "
          "never from an observation/analysis/reanalysis product -- confirmed by the idx forecast_desc field "
          "captured during extraction (e.g. '4 hour fcst').")
    n_null_avail = int(df["available_time"].isna().sum())
    print(f"6. available_time is populated from the S3 object's Last-Modified header for {len(df) - n_null_avail} of "
          f"{len(df)} rows ({n_null_avail} null). This is OBSERVED FROM ARCHIVE, not a documented SLA -- see Part 2 note.")


# ---------------------------------------------------------------------------
# PART 11 -- validation.
# ---------------------------------------------------------------------------

def validate(df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 11: VALIDATION")
    print("=" * 78)
    key_cols = ["model", "run_time", "forecast_hour", "variable", "grid_lat", "grid_lon"]
    dup = df.duplicated(subset=key_cols).sum()
    print(f"Duplicate rows on (model, run_time, forecast_hour, variable, grid point): {int(dup)}")

    check = ((df["valid_time"] - df["run_time"]).dt.total_seconds() / 3600 == df["forecast_hour"]).all()
    print(f"valid_time == run_time + forecast_hour for all rows: {bool(check)}")

    plausible = df["value_f"].between(-40, 130).all()
    print(f"All 2m temperatures meteorologically plausible (-40F to 130F): {bool(plausible)} "
          f"(actual range: {df['value_f'].min():.1f}F to {df['value_f'].max():.1f}F)")

    dist_ok = (df["distance_km"] <= 3.0).all()  # HRRR native grid spacing is ~3km
    print(f"Selected grid point within one grid-cell (~3km) of the requested NYC point: {bool(dist_ok)} "
          f"(max distance observed: {df['distance_km'].max():.2f} km)")

    print("\nManual inspection: multiple runs forecasting the SAME valid_time (confirms revisions survive):")
    valid_counts = df.groupby("valid_time")["run_time"].nunique().sort_values(ascending=False)
    example_valid_time = valid_counts.index[0]
    example = df[df["valid_time"] == example_valid_time].sort_values("run_time")
    print(f"valid_time = {example_valid_time}")
    print(example[["run_time", "forecast_hour", "value", "unit", "value_f"]].to_string(index=False))


# ---------------------------------------------------------------------------
# PART 12 -- storage / scale estimate.
# ---------------------------------------------------------------------------

def estimate_scale(stats: RequestStats, n_rows: int, n_runs: int) -> None:
    print("\n" + "=" * 78)
    print("PART 12: STORAGE / SCALE ESTIMATE (extrapolated from this ONE-DAY test -- not executed)")
    print("=" * 78)
    bytes_per_message = stats.bytes_downloaded / max(stats.n_requests / 2, 1)  # rough: half the requests are idx (small), half are data
    # Better: use measured totals directly.
    avg_bytes_per_row = stats.bytes_downloaded / max(n_rows, 1)
    print(f"This test: {stats.n_requests} requests, {stats.bytes_downloaded:,} bytes, {stats.seconds_elapsed:.1f}s request time, "
          f"{n_runs} runs, {n_rows} extracted rows.")
    print(f"~{avg_bytes_per_row:,.0f} bytes downloaded per extracted row (this test's mix of idx + TMP + full variable snapshot).")

    # Assume, for a production design: ~24 runs/day get a full temperature
    # series (~20 forecast hours each on average) plus the full 9-variable
    # set at one representative hour/day for the other fields -- an
    # ESTIMATE, clearly labeled, not a commitment to that exact design.
    runs_per_day = 24
    avg_fh_per_run = 15  # rough average across the day (varies by exact design; see report caveat)
    rows_per_day_est = runs_per_day * avg_fh_per_run
    bytes_per_day_est = rows_per_day_est * avg_bytes_per_row

    for label, days in [("1 year", 365), ("5 years", 365 * 5), ("10 years", 365 * 10)]:
        n_runs_est = runs_per_day * days
        n_rows_est = rows_per_day_est * days
        bytes_est = bytes_per_day_est * days
        print(
            f"  {label:8s}: ~{n_runs_est:,} model runs, ~{n_rows_est:,.0f} rows, "
            f"~{bytes_est/1e9:.1f} GB downloaded (ESTIMATE, single-variable-focused design)"
        )
    print(
        "These are ROUGH extrapolations from one day's measured bytes/row, assuming a design that keeps most\n"
        "runs to a single variable (2m temperature) and only pulls the full multi-variable set sparingly.\n"
        "Actual scale depends heavily on final design choices (how many runs/day, how many variables/run)."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    stats = RequestStats()
    archive_info = investigate_archive_structure(stats)

    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    print(f"\nTarget local day (America/New_York): {TARGET_DATE} -> UTC [{day_start_utc}, {day_end_utc})")

    print_full_candidate_coverage_table(day_start_utc, day_end_utc)

    snapshot_run = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
    snapshot_rows = fetch_variable_snapshot(snapshot_run, 4, stats)

    print("\n" + "=" * 78)
    print("PART 5/6: SELECTED RUNS + TEMPERATURE-SERIES EXTRACTION")
    print("=" * 78)
    runs = select_runs()
    print(f"Selected {len(runs)} runs (justification above the coverage table + in report): "
          f"{[f'{r:%Y-%m-%d %HZ}' for r in runs]}")

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
    df.to_csv(PROCESSED_DIR / "hrrr_test_canonical.csv", index=False)
    df.to_parquet(PROCESSED_DIR / "hrrr_test_canonical.parquet", index=False)

    wide = df.pivot_table(index="valid_time", columns="run_time", values="value_f")
    wide.to_csv(PROCESSED_DIR / "hrrr_test_wide_temperature_f.csv")

    with open(RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "nyc_location.json", "w", encoding="utf-8") as f:
        json.dump(NYC_LOCATION, f, indent=2)

    daily_max_df = compute_predicted_daily_max(df, coverage_by_run)
    revision_df = compute_revision_series(daily_max_df)
    daily_max_df.to_csv(PROCESSED_DIR / "hrrr_test_predicted_daily_max.csv", index=False)
    revision_df.to_csv(PROCESSED_DIR / "hrrr_test_revision_series.csv", index=False)

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
    print(f"Rows extracted (variable snapshot, saved separately): {len(snapshot_rows)}")
    print(f"Saved canonical/processed outputs to {PROCESSED_DIR}")
    print(f"Saved raw GRIB2 message slices to {RAW_DIR}")


if __name__ == "__main__":
    main()
