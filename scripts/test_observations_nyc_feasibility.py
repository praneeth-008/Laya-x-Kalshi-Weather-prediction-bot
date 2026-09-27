"""Historical surface-observation feasibility test for NYC, ONE DAY ONLY.

Unlike the HRRR/GFS/GEFS tests, this is not a forecast-model test. The
question is: "what weather had actually occurred around NYC by historical
timestamp t, and when could a trader realistically have known it?"

Source: NOAA NCEI Integrated Surface Database, Global Hourly (CSV),
s3://noaa-global-hourly-pds -- see data/observations.py for full
verification notes (DOCUMENTED/OBSERVED/INFERRED/UNKNOWN distinctions).

Feasibility test only. Does not touch Kalshi/HRRR/GFS/GEFS data, does not
decide a Kalshi settlement station, no modeling, no trading logic.

Usage:
    python scripts/test_observations_nyc_feasibility.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.observations import (  # noqa: E402
    NYC_TZ,
    STATIONS,
    RequestStats,
    fetch_station_year,
    haversine_km,
    is_real_observation,
    local_day_utc_bounds,
    parse_isd_row,
)

NYC_REFERENCE = {"lat": 40.78, "lon": -73.97}
TARGET_DATE = date(2025, 7, 1)
YEAR = 2025

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "observations" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "observations" / "test"


# ---------------------------------------------------------------------------
# PART 1/2 -- source selection + station discovery (mostly documented above;
# this prints the summary of what was verified live before this script ran).
# ---------------------------------------------------------------------------

def report_source_and_stations() -> pd.DataFrame:
    print("=" * 78)
    print("PART 1: SOURCE SELECTION")
    print("=" * 78)
    print(
        "Sources investigated:\n"
        "  - aviationweather.gov METAR API: LIVE only, ~30-day rolling retention (CONFIRMED via\n"
        "    official docs) -- unusable for a 2025-07-01 historical test from 2026.\n"
        "  - NCEI Local Climatological Data (LCD): more processed monthly/daily summaries;\n"
        "    better suited to a 'final settlement-style' value than to point-in-time raw reports.\n"
        "  - NCEI Integrated Surface Database (ISD), Global Hourly, CSV format on AWS\n"
        "    (s3://noaa-global-hourly-pds): SELECTED. Official NOAA/NCEI, verified live, contains\n"
        "    the finest-grained widely-available official surface reports (hourly METAR + irregular\n"
        "    SPECI), preserves per-report timestamps, and covers decades of history per station.\n"
        "  - ISD original fixed-width format (s3://noaa-isd-pds) exists but the CSV bucket is\n"
        "    functionally identical content in a far easier format -- no reason to prefer the raw one.\n"
    )
    print("Selected: NOAA NCEI Integrated Surface Database, Global Hourly (CSV), s3://noaa-global-hourly-pds")
    print("Key structure (OBSERVED FROM ARCHIVE): {year}/{USAF}{WBAN}.csv -- one file per station per YEAR.")

    print("\n" + "=" * 78)
    print("PART 2: STATION DISCOVERY (from official isd-history.csv, verified live)")
    print("=" * 78)
    rows = []
    for s in STATIONS:
        dist = haversine_km(NYC_REFERENCE["lat"], NYC_REFERENCE["lon"], s["lat"], s["lon"])
        rows.append({**s, "distance_from_reference_km": dist})
    df = pd.DataFrame(rows)
    print(df[["station_id", "name", "icao", "lat", "lon", "elevation_m", "distance_from_reference_km", "begin", "end"]].to_string(index=False))
    return df


# ---------------------------------------------------------------------------
# PART 3/4/5 -- fetch + parse each station's observations around the target day.
# ---------------------------------------------------------------------------

def fetch_and_parse_station(station: dict, stats: RequestStats) -> pd.DataFrame:
    text, key = fetch_station_year(YEAR, station["usaf"], station["wban"], stats)
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    import csv
    import io

    reader = csv.DictReader(io.StringIO(text))
    # Buffer well beyond the local-day UTC bounds to verify timezone handling (Part 3).
    window_start = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
    window_end = datetime(2025, 7, 2, 12, tzinfo=timezone.utc)

    rows = []
    raw_rows_in_window = []
    for row in reader:
        date_str = row.get("DATE", "")
        if not date_str:
            continue
        try:
            obs_dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (window_start <= obs_dt <= window_end):
            continue
        raw_rows_in_window.append(row)
        parsed = parse_isd_row(row)
        parsed["source"] = "NOAA NCEI Integrated Surface Database (Global Hourly)"
        parsed["network"] = "ISD"
        parsed["station_id"] = station["station_id"]
        parsed["station_name"] = station["name"]
        parsed["station_lat"] = station["lat"]
        parsed["station_lon"] = station["lon"]
        parsed["station_elevation_m"] = station["elevation_m"]
        parsed["source_file_or_endpoint"] = f"s3://noaa-global-hourly-pds/{key}"
        parsed["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        parsed["is_real_observation"] = is_real_observation(parsed["report_type_raw"])
        rows.append(parsed)

    # Save the raw window rows verbatim (as CSV), not the full-year file, per
    # "save separately, don't call this production" -- but preserving the
    # exact raw fields for these rows, unmodified.
    if raw_rows_in_window:
        pd.DataFrame(raw_rows_in_window).to_csv(raw_dir / f"{station['station_id']}_raw_window.csv", index=False)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# PART 9 -- station comparison at aligned timestamps.
# ---------------------------------------------------------------------------

def station_comparison(df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 9: STATION TEMPERATURE COMPARISON (nearest real observation to each hourly mark)")
    print("=" * 78)
    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    marks = [day_start_utc + timedelta(hours=h) for h in range(0, 24, 2)]  # every 2 hours through the local day

    real = df[df["is_real_observation"]]
    rows = []
    for mark in marks:
        row = {"target_utc": mark, "target_et": mark.astimezone(NYC_TZ)}
        for sid, g in real.groupby("station_id"):
            g = g.copy()
            g["delta"] = (g["observation_time"] - mark).abs()
            nearest = g.loc[g["delta"].idxmin()]
            row[f"{sid}_temp_f"] = nearest["temperature_f"]
            row[f"{sid}_obs_time"] = nearest["observation_time"]
        rows.append(row)
    result = pd.DataFrame(rows)

    temp_cols = [c for c in result.columns if c.endswith("_temp_f")]
    print(result[["target_et"] + temp_cols].round(2).to_string(index=False))
    print("\n(Each cell is the NEAREST actual reported observation to that mark -- not interpolated. "
          "See *_obs_time columns in the saved CSV for the exact actual timestamps used.)")

    result["temp_range_f"] = result[temp_cols].max(axis=1) - result[temp_cols].min(axis=1)
    result["temp_mean_f"] = result[temp_cols].mean(axis=1)
    result["temp_std_f"] = result[temp_cols].std(axis=1)
    print("\n" + "=" * 78)
    print("PART 12: STATION DISAGREEMENT (cross-station, at each comparison mark)")
    print("=" * 78)
    print(result[["target_et", "temp_range_f", "temp_mean_f", "temp_std_f"]].round(2).to_string(index=False))
    max_disagreement = result.loc[result["temp_range_f"].idxmax()]
    print(f"\nLargest disagreement observed: {max_disagreement['temp_range_f']:.2f}F range at {max_disagreement['target_et']}")

    return result


# ---------------------------------------------------------------------------
# PART 10 -- max-observed-so-far, strictly point-in-time.
# ---------------------------------------------------------------------------

def max_so_far(df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 10: MAXIMUM TEMPERATURE OBSERVED SO FAR (strictly point-in-time, per station)")
    print("=" * 78)
    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    real = df[df["is_real_observation"] & df["temperature_f"].notna()].copy()
    in_day = real[(real["observation_time"] >= day_start_utc) & (real["observation_time"] < day_end_utc)]
    in_day = in_day.sort_values(["station_id", "observation_time"])

    # cummax is inherently point-in-time as long as the frame is pre-sorted
    # by observation_time WITHIN each station and we never look ahead.
    in_day["max_so_far_f"] = in_day.groupby("station_id")["temperature_f"].cummax()

    for sid, g in in_day.groupby("station_id"):
        print(f"\n{sid} ({STATIONS[[s['station_id'] for s in STATIONS].index(sid)]['name']}) -- first 8 and last 4 rows:")
        show = pd.concat([g.head(8), g.tail(4)])
        print(show[["observation_time", "temperature_f", "max_so_far_f"]].to_string(index=False))
    return in_day


# ---------------------------------------------------------------------------
# PART 11 -- momentum feature diagnostic (small example only).
# ---------------------------------------------------------------------------

def momentum_diagnostic(in_day: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 11: OBSERVATION REVISION / MOMENTUM DIAGNOSTIC (small example, NOT a feature set)")
    print("=" * 78)
    one_station = in_day[in_day["station_id"] == STATIONS[0]["station_id"]].copy().sort_values("observation_time")
    one_station["temp_change_since_prev_f"] = one_station["temperature_f"].diff()
    one_station["minutes_since_prev"] = one_station["observation_time"].diff().dt.total_seconds() / 60
    one_station["dewpoint_change_f"] = one_station["dewpoint_f"].diff()
    one_station["pressure_change_hpa"] = one_station["station_pressure_hpa"].diff()
    idx_of_max = one_station["max_so_far_f"].idxmax()
    one_station["time_since_daily_max_min"] = (
        one_station["observation_time"] - one_station.loc[idx_of_max, "observation_time"]
    ).dt.total_seconds() / 60
    one_station.loc[one_station["time_since_daily_max_min"] < 0, "time_since_daily_max_min"] = np.nan

    cols = ["observation_time", "temperature_f", "temp_change_since_prev_f", "minutes_since_prev",
            "dewpoint_change_f", "pressure_change_hpa", "max_so_far_f", "time_since_daily_max_min"]
    print(f"Station: {STATIONS[0]['station_id']} ({STATIONS[0]['name']})")
    print(one_station[cols].round(2).head(12).to_string(index=False))
    print("\n(This confirms the raw data supports these features -- no feature set is being finalized here.)")
    return one_station


# ---------------------------------------------------------------------------
# PART 13 -- relation to HRRR/GFS/GEFS test data.
# ---------------------------------------------------------------------------

def relate_to_forecasts(in_day: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 13: RELATION TO EXISTING HRRR/GFS/GEFS TEST DATA (conceptual only, no modification)")
    print("=" * 78)
    hrrr_path = PROJECT_ROOT / "data" / "processed" / "weather" / "hrrr" / "test" / "hrrr_test_canonical.csv"
    gfs_path = PROJECT_ROOT / "data" / "processed" / "weather" / "gfs" / "test" / "gfs_test_canonical.csv"
    gefs_dist_path = PROJECT_ROOT / "data" / "processed" / "weather" / "gefs" / "test" / "gefs_test_ensemble_distribution.csv"
    if not (hrrr_path.exists() and gfs_path.exists() and gefs_dist_path.exists()):
        print("  One or more prior test outputs not found -- skipping this cross-reference.")
        return

    hrrr = pd.read_csv(hrrr_path, parse_dates=["run_time", "valid_time"])
    gfs = pd.read_csv(gfs_path, parse_dates=["run_time", "valid_time"])
    gefs_dist = pd.read_csv(gefs_dist_path, parse_dates=["run_time"])

    for check_time in [datetime(2025, 7, 1, 16, tzinfo=timezone.utc), datetime(2025, 7, 1, 20, tzinfo=timezone.utc)]:
        print(f"\n--- Representative timestamp: {check_time} ({check_time.astimezone(NYC_TZ)}) ---")

        obs_before = in_day[in_day["observation_time"] <= check_time]
        if not obs_before.empty:
            latest = obs_before.sort_values("observation_time").groupby("station_id").tail(1)
            print("Latest observed temps at/before this time (per station):")
            print(latest[["station_id", "observation_time", "temperature_f", "max_so_far_f"]].to_string(index=False))
        else:
            print("No observations at/before this time in this test's window.")

        hrrr_avail = hrrr[hrrr["run_time"] <= check_time]
        if not hrrr_avail.empty:
            latest_run = hrrr_avail["run_time"].max()
            print(f"Latest HRRR run with run_time <= this timestamp: {latest_run} "
                  f"(NOTE: whether it was ACTUALLY available by {check_time} depends on the "
                  f"unresolved publication-delay question from the HRRR test -- not asserted here).")
        gfs_avail = gfs[gfs["run_time"] <= check_time]
        if not gfs_avail.empty:
            print(f"Latest GFS run with run_time <= this timestamp: {gfs_avail['run_time'].max()} (same caveat).")
        gefs_avail = gefs_dist[gefs_dist["run_time"] <= check_time]
        if not gefs_avail.empty:
            row = gefs_avail.sort_values("run_time").iloc[-1]
            print(f"Latest GEFS run with run_time <= this timestamp: {row['run_time']} -- "
                  f"mean={row['mean']:.1f}F, P10-P90=[{row['p10']:.1f},{row['p90']:.1f}]F (same caveat).")

    print(
        "\nExact simultaneous availability across observations + all 3 forecast sources CANNOT be "
        "established from this test alone -- each source's own availability-time question (HRRR/GFS/GEFS: "
        "approximated via S3 Last-Modified; observations: UNKNOWN, see Part 6) remains only partially resolved."
    )


# ---------------------------------------------------------------------------
# PART 15 -- point-in-time audit.
# ---------------------------------------------------------------------------

def audit_point_in_time(df: pd.DataFrame, in_day: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 15: POINT-IN-TIME AUDIT")
    print("=" * 78)
    print(f"1. observation_time preserved for all rows: {df['observation_time'].notna().all()}")
    print("2. Source DATE treated as UTC (WMO METAR/SYNOP convention, DOCUMENTED) -- never assumed local.")
    print("3. Local-day boundaries computed via zoneinfo America/New_York (data/observations.py local_day_utc_bounds).")
    is_monotonic = in_day.groupby("station_id")["observation_time"].apply(lambda s: s.is_monotonic_increasing).all()
    print(f"4. max_so_far computed only after sorting ascending by observation_time per station, via cummax "
          f"(structurally cannot see future rows): sort verified monotonic = {bool(is_monotonic)}.")
    print(f"5. Stations kept independent: {df['station_id'].nunique()} distinct station_id values, never averaged into one series.")
    print(f"6. Corrections/revisions: QUALITY_CONTROL version preserved per row (see Part 7 in the final report) -- "
          f"not silently ignored, but ALSO not resolved into a raw-vs-final distinction (archive doesn't separate them).")
    print("7. No forward-filling anywhere in this script -- missing values stay null.")
    print("8. No interpolation anywhere in the canonical dataset -- station_comparison() uses nearest-actual-observation, "
          "explicitly not an interpolated value, and reports the real gap.")
    print("9. available_time is NOT included as a fabricated column -- see Part 6 (UNKNOWN).")
    print("10. Derived fields (max_so_far, momentum diagnostics) use only rows with observation_time <= t by construction (cummax/diff).")


# ---------------------------------------------------------------------------
# PART 16 -- data quality.
# ---------------------------------------------------------------------------

def data_quality(df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 16: DATA QUALITY")
    print("=" * 78)
    real = df[df["is_real_observation"]]
    rows = []
    for sid, g in real.groupby("station_id"):
        g = g.sort_values("observation_time")
        gaps = g["observation_time"].diff().dt.total_seconds() / 60
        dup_ts = g["observation_time"].duplicated().sum()
        implausible = ((g["temperature_f"] < -40) | (g["temperature_f"] > 130)).sum()
        rows.append(
            {
                "station_id": sid,
                "n_observations": len(g),
                "first_observation": g["observation_time"].min(),
                "last_observation": g["observation_time"].max(),
                "median_gap_min": gaps.median(),
                "p90_gap_min": gaps.quantile(0.9),
                "max_gap_min": gaps.max(),
                "temp_missing_pct": 100 * g["temperature_f"].isna().mean(),
                "pressure_missing_pct": 100 * g["station_pressure_hpa"].isna().mean(),
                "duplicate_timestamps": int(dup_ts),
                "implausible_temps": int(implausible),
            }
        )
    result = pd.DataFrame(rows)
    print(result.round(2).to_string(index=False))
    return result


# ---------------------------------------------------------------------------
# PART 17 -- daily max comparison.
# ---------------------------------------------------------------------------

def daily_max_comparison(in_day: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 17: MAXIMUM FOUND IN TESTED OBSERVATION DATASET (NOT the official Kalshi settlement value)")
    print("=" * 78)
    rows = []
    for sid, g in in_day.groupby("station_id"):
        idx = g["temperature_f"].idxmax()
        rows.append(
            {
                "station_id": sid,
                "station_name": STATIONS[[s["station_id"] for s in STATIONS].index(sid)]["name"],
                "max_temperature_found_f": g.loc[idx, "temperature_f"],
                "time_of_max_utc": g.loc[idx, "observation_time"],
                "time_of_max_et": g.loc[idx, "observation_time"].astimezone(NYC_TZ),
            }
        )
    result = pd.DataFrame(rows)
    print(result.round(2).to_string(index=False))
    return result


# ---------------------------------------------------------------------------
# PART 19 -- scale estimate.
# ---------------------------------------------------------------------------

def estimate_scale(stats: RequestStats, n_stations: int) -> None:
    print("\n" + "=" * 78)
    print("PART 19: SCALE ESTIMATE (extrapolated from this ONE-DAY test's actual full-year downloads)")
    print("=" * 78)
    avg_bytes_per_station_year = stats.bytes_downloaded / n_stations
    print(f"This test downloaded {n_stations} FULL-YEAR files (2025) -- {stats.bytes_downloaded:,} bytes total, "
          f"{stats.n_requests} requests, {stats.seconds_elapsed:.2f}s.")
    print(f"~{avg_bytes_per_station_year:,.0f} bytes per station-year (much lighter than any HRRR/GFS/GEFS estimate).")
    for label, years in [("1 year", 1), ("5 years", 5), ("10 years", 10)]:
        n_requests_est = n_stations * years
        bytes_est = avg_bytes_per_station_year * n_stations * years
        print(f"  {label:8s}: ~{n_requests_est} requests (1 file/station/year), ~{bytes_est/1e6:.1f} MB downloaded (ESTIMATE)")
    print("Processed storage would be smaller still (a filtered/typed Parquet of just these 4 stations, all years, "
          "is unlikely to exceed a few tens of MB even at full hourly+SPECI density for a decade).")


def report_live_feasibility() -> None:
    print("\n" + "=" * 78)
    print("PART 20: LIVE-SYSTEM FEASIBILITY (investigated only, not implemented)")
    print("=" * 78)
    print(
        "aviationweather.gov's METAR API (confirmed in Part 1 to have ~30-day retention) is the natural LIVE\n"
        "counterpart to this historical ISD archive: it serves the SAME underlying METAR/SPECI reports by ICAO\n"
        "station identifier (KNYC/KLGA/KJFK/KEWR -- identical to the icao field already preserved per station\n"
        "in this test's station registry), typically updated on the same ~hourly/irregular cadence as the\n"
        "historical archive. This gives a plausible historical<->live identifier mapping (via ICAO) without\n"
        "needing a separate crosswalk. Update frequency for a live bot would match METAR/SPECI cadence -- i.e.\n"
        "irregular, not a fixed polling-friendly interval, so a live design would need to poll frequently enough\n"
        "to catch SPECI reports, not just the routine hourly METAR. This is an investigation only -- no live\n"
        "integration is implemented here."
    )


def main() -> None:
    stats = RequestStats()
    station_df = report_source_and_stations()

    all_rows = []
    for station in STATIONS:
        print(f"\nFetching {station['station_id']} ({station['name']}) full-year 2025 file ...")
        df_station = fetch_and_parse_station(station, stats)
        n_real = int(df_station["is_real_observation"].sum())
        n_placeholder = len(df_station) - n_real
        print(f"  {len(df_station)} rows in window ({n_real} real observations, {n_placeholder} SOD/SOM placeholders)")
        print(f"  Report types seen: {sorted(df_station['report_type_raw'].unique())}")
        all_rows.append(df_station)

    df = pd.concat(all_rows, ignore_index=True)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED_DIR / "observations_test_canonical.csv", index=False)
    df.to_parquet(PROCESSED_DIR / "observations_test_canonical.parquet", index=False)

    print("\n" + "=" * 78)
    print("PART 6: OBSERVATION TIME VS AVAILABILITY TIME")
    print("=" * 78)
    print(
        "observation_time: preserved exactly, UTC (DOCUMENTED via WMO METAR/SYNOP convention).\n"
        "report_time: not distinguishable from observation_time in this archive -- set to null.\n"
        "archive/publication_time: this archive provides only a single file-level S3 Last-Modified for the\n"
        "  WHOLE YEAR file, which is NOT a meaningful per-observation availability proxy (unlike HRRR/GFS/GEFS's\n"
        "  per-forecast-hour files) -- NOT used, NOT fabricated.\n"
        "available_time: UNKNOWN. Not recoverable from this archive. Left null in the canonical dataset rather\n"
        "  than approximated with an invented delay.\n"
        "(General industry knowledge that live METAR/SPECI reports are typically disseminated within a few\n"
        "minutes of observation_time is INFERRED background context, not verified from this archive.)"
    )

    print("\n" + "=" * 78)
    print("PART 7: CORRECTIONS / REVISIONS")
    print("=" * 78)
    qc_versions = df["quality_control_version"].value_counts()
    print(f"QUALITY_CONTROL versions observed: {qc_versions.to_dict()}")
    print(
        "This confirms the archive is CATEGORY B (quality-controlled historical observations), not raw\n"
        "untouched real-time telegraphic reports (category A) -- values may have been edited by NOAA's QC\n"
        "process relative to what was originally transmitted. Per-field quality flags (e.g. the digit after\n"
        "each TMP/DEW/SLP value) are preserved verbatim in the canonical dataset. NOAA's own numeric flag-\n"
        "meaning table could NOT be fully extracted from their documentation PDF in this environment -- '9' is\n"
        "confirmed to mean missing (via the all-missing SOD/SOM rows); the full meaning of other digits (e.g. "
        "'5', seen on nearly every real value here) is reported as UNKNOWN rather than guessed."
    )

    comparison_df = station_comparison(df)
    comparison_df.to_csv(PROCESSED_DIR / "observations_test_station_comparison.csv", index=False)

    in_day = max_so_far(df)
    in_day.to_csv(PROCESSED_DIR / "observations_test_max_so_far.csv", index=False)

    momentum_diagnostic(in_day)

    relate_to_forecasts(in_day)

    audit_point_in_time(df, in_day)

    quality_df = data_quality(df)
    quality_df.to_csv(PROCESSED_DIR / "observations_test_data_quality.csv", index=False)

    daily_max_df = daily_max_comparison(in_day)
    daily_max_df.to_csv(PROCESSED_DIR / "observations_test_daily_max.csv", index=False)

    print("\n" + "=" * 78)
    print("PART 18: HISTORICAL DEPTH")
    print("=" * 78)
    print(station_df[["station_id", "name", "begin", "end"]].to_string(index=False))
    print(
        "\nStation-continuity finding (OBSERVED FROM ARCHIVE, isd-history.csv): Central Park has MULTIPLE\n"
        "historical station-id records with OVERLAPPING/gapped date ranges -- 725033-94728 (1943-1997),\n"
        "999999-94728 (1965-1997), 725060-94728 (2010-2012), and 725053-94728 (2005-2025, used in this test).\n"
        "This means a >20-year Central Park history would require joining across DIFFERENT station-id records,\n"
        "not one continuous file -- a real complication for historical depth, flagged rather than resolved here.\n"
        "LaGuardia/JFK/Newark each show one clean continuous 1973-2025 record by contrast."
    )

    estimate_scale(stats, len(STATIONS))
    report_live_feasibility()

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total requests: {stats.n_requests}")
    print(f"Total bytes downloaded: {stats.bytes_downloaded:,} ({stats.bytes_downloaded/1e6:.2f} MB)")
    print(f"Total request time: {stats.seconds_elapsed:.2f} s")
    print(f"Total rows (all stations, window): {len(df)}")
    print(f"Real observations (excluding SOD/SOM): {int(df['is_real_observation'].sum())}")
    print(f"Saved canonical/processed outputs to {PROCESSED_DIR}")
    print(f"Saved raw window CSVs to {RAW_DIR}")


if __name__ == "__main__":
    main()
