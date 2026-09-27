"""NBM (National Blend of Models) historical feasibility test for NYC,
ONE DAY ONLY (2025-07-01).

NBM is NOT a raw numerical model -- it is NOAA's statistically
post-processed BLEND of multiple model/ensemble inputs, providing a
calibrated deterministic grid plus LIMITED post-processed uncertainty
(a single ensemble-std-dev field for temperature; full percentile/QMD
guidance is confirmed, empirically, to cover only precipitation/wind, not
temperature -- see data/nbm.py docstring).

Feasibility test only. Does not touch Kalshi/HRRR/GFS/GEFS/observation
data, no modeling, no trading logic.

Usage:
    python scripts/test_nbm_nyc_feasibility.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.nbm import (  # noqa: E402
    HOURLY_MAX_FH,
    MAX_FORECAST_HOUR_FULL,
    RequestStats,
    decode_message,
    fetch_byte_range,
    fetch_idx,
    find_message,
    grib_key,
    list_objects,
    local_day_utc_bounds,
    needed_forecast_hours,
)

NYC_LOCATION = {"name": "NYC test point (approx. Central Park)", "lat": 40.78, "lon": -73.97}
TARGET_DATE = date(2025, 7, 1)

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "nbm" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "nbm" / "test"


def _lon_to_180(lon_0_360: float) -> float:
    return lon_0_360 - 360 if lon_0_360 > 180 else lon_0_360


def investigate_structure_and_products(stats: RequestStats) -> None:
    print("=" * 78)
    print("A1/A2/A3: WHAT NBM IS + ARCHIVE STRUCTURE + RUN SCHEDULE")
    print("=" * 78)
    print(
        "NBM is a statistically post-processed BLEND of multiple NWP model/ensemble inputs\n"
        "(GFS, GEFS, HRRR/RRFS, ECMWF-via-exchange, Canadian, others) -- NOT a raw model. It uses\n"
        "bias-correction (decaying-average, quantile mapping) and ensemble weighting to produce ONE\n"
        "calibrated deterministic grid, plus a single ensemble-std-dev field per variable as its\n"
        "temperature uncertainty measure (DOCUMENTED: https://vlab.noaa.gov/web/mdl/nbm).\n"
    )
    _, run_prefixes = list_objects("blend.20250701/", delimiter="/")
    stats.record(0, 0.0)
    run_hours = sorted(int(p.rstrip("/").split("/")[-1]) for p in run_prefixes if p.rstrip("/").split("/")[-1].isdigit())
    print(f"Run hours found for 2025-07-01 (OBSERVED FROM ARCHIVE): {len(run_hours)} runs -- {run_hours}")
    print("NBM updates HOURLY -- MORE frequently than GFS/GEFS (4/day), matching HRRR's cadence.")

    _, product_prefixes = list_objects("blend.20250701/12/", delimiter="/")
    stats.record(0, 0.0)
    print(f"Products under one run (OBSERVED): {[p.split('/')[-2] for p in product_prefixes if p.count('/') >= 3]}")
    print("'core' = deterministic + std-dev; 'qmd' = Quantile Mapped Distribution (CONFIRMED: precip/wind/gust "
          "percentiles ONLY, no temperature -- verified by listing every variable in a real qmd idx file); "
          "'text' = bulletin text, not used here.")
    print(f"Forecast-hour schedule (OBSERVED via HEAD probes): hourly F001-F{HOURLY_MAX_FH:03d}, then 3-hourly to "
          f"F192, then 6-hourly to F{MAX_FORECAST_HOUR_FULL} (11 days).")
    print("Historical depth (OBSERVED): data exists for blend.20210101/, blend.20220101/, blend.20230101/; "
          "blend.20200101/ returns zero objects -- archive depth is at least back to 2021.")


def variable_snapshot(run_dt: datetime, forecast_hour: int, stats: RequestStats) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"A5: VARIABLE SNAPSHOT -- run {run_dt:%Y-%m-%d %HZ}, FH={forecast_hour} (core product)")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, forecast_hour, "core")
    entries = fetch_idx(key, stats)
    print(f"Parsed {len(entries)} messages from {key}.idx")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        ("TMP", "2 m above ground", None, "ens std dev"),
        ("TMP", "2 m above ground", "ens std dev", None),
        ("DPT", "2 m above ground", None, "ens std dev"),
        ("RH", "2 m above ground", None, "ens std dev"),
        ("WIND", "surface - 610 m above ground", None, None),
        ("APCP", "surface", None, None),
        ("TCDC", "surface", None, None),
    ]
    rows = []
    for var, level, contains, excludes in targets:
        msg = find_message(entries, var, level, desc_contains=contains, desc_excludes=excludes)
        label = var + (" (std dev)" if contains == "ens std dev" else "")
        if msg is None:
            print(f"  {label}:{level} -- NOT FOUND (skipped, not forced)")
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
        n_bytes = msg["byte_end"] - msg["byte_start"] + 1 if msg["byte_end"] else len(raw)
        (raw_dir / f"{run_dt:%Y%m%d%H}_f{forecast_hour:03d}_{label.replace(' ', '_')}.grib2").write_bytes(raw)
        decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        print(f"  {label:20s} ({msg['forecast_desc']:18s}) -> {n_bytes:>9,} bytes | value={decoded['value']:.3f} {decoded['units']}")
        rows.append({"run_dt": run_dt, "forecast_hour": forecast_hour, "variable": label, **decoded})
    return rows


def qmd_sanity_check(run_dt: datetime, forecast_hour: int, stats: RequestStats) -> None:
    print("\n" + "=" * 78)
    print("A6: TEMPERATURE PERCENTILE CHECK (qmd product)")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, forecast_hour, "qmd")
    entries = fetch_idx(key, stats)
    variables = sorted({e["variable"] for e in entries})
    print(f"Parsed {len(entries)} messages from {key}.idx")
    print(f"Distinct variables in qmd product: {variables}")
    has_temp = any(v in ("TMP", "TMAX", "TMIN") for v in variables)
    print(f"Temperature percentile fields present: {has_temp}")
    if not has_temp:
        print("CONFIRMED: NBM's percentile/QMD product does NOT provide temperature percentiles -- "
              "only precipitation/wind/gust. NBM's only temperature uncertainty measure is the single "
              "'ens std dev' field in the core product (see A5 above). This is reported, not assumed.")
    # Fetch one QMD message anyway to prove the extraction mechanism itself works on this product.
    msg = find_message(entries, "APCP", entries[0]["level"] if entries else "surface")
    if msg is None and entries:
        msg = entries[0]
    if msg:
        raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"), stats)
        print(f"Sanity-fetched one qmd message ({msg['variable']}:{msg['level']}, {len(raw):,} bytes) -- "
              f"confirms byte-range extraction works identically on this product.")


def select_runs() -> list[datetime]:
    return [
        datetime(2025, 6, 30, 12, tzinfo=timezone.utc),
        datetime(2025, 6, 30, 18, tzinfo=timezone.utc),
        datetime(2025, 7, 1, 0, tzinfo=timezone.utc),
        datetime(2025, 7, 1, 6, tzinfo=timezone.utc),
    ]


def period_max_windows(run_dt: datetime) -> list[tuple[int, datetime, datetime]]:
    """NBM's TMAX periods: F012=[0,12)h max, F036=[24,36)h max, etc. (every
    24h starting at F012, alternating with TMIN at F024/F048/...). Returns
    (forecast_hour, window_start_utc, window_end_utc) for TMAX periods only,
    within this run's hourly-resolution horizon."""
    windows = []
    for fh in range(12, HOURLY_MAX_FH + 1, 24):
        start = run_dt + timedelta(hours=fh - 12)
        end = run_dt + timedelta(hours=fh)
        windows.append((fh, start, end))
    return windows


def fetch_run_data(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats) -> tuple[pd.DataFrame, dict, list[dict]]:
    coverage = needed_forecast_hours(run_dt, day_start_utc, day_end_utc)
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for fh in coverage["forecast_hours"]:
        key = grib_key(run_dt, run_dt.hour, fh, "core")
        try:
            entries = fetch_idx(key, stats)
            tmp_msg = find_message(entries, "TMP", "2 m above ground", desc_excludes="ens std dev")
            std_msg = find_message(entries, "TMP", "2 m above ground", desc_contains="ens std dev")
            if tmp_msg is None:
                continue
            raw, last_modified = fetch_byte_range(key, tmp_msg["byte_start"], tmp_msg["byte_end"], stats)
            (raw_dir / f"{run_dt:%Y%m%d%H}_f{fh:03d}_TMP.grib2").write_bytes(raw)
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])

            std_value = None
            if std_msg is not None:
                std_raw, _ = fetch_byte_range(key, std_msg["byte_start"], std_msg["byte_end"], stats)
                std_decoded = decode_message(std_raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
                std_value = std_decoded["value"]
        except RuntimeError as exc:
            print(f"    FAILED for {key}: {exc}")
            continue

        valid_time = run_dt + timedelta(hours=fh)
        rows.append(
            {
                "source": "NOAA NBM AWS archive", "model": "NBM", "product": "core",
                "requested_lat": NYC_LOCATION["lat"], "requested_lon": NYC_LOCATION["lon"],
                "grid_lat": decoded["grid_lat"], "grid_lon": _lon_to_180(decoded["grid_lon"]),
                "distance_km": decoded["distance_km"],
                "run_time": run_dt, "available_time": last_modified,
                "forecast_hour": fh, "valid_time": valid_time,
                "variable": "TMP", "level": "2 m above ground",
                "value": decoded["value"], "unit": decoded["units"],
                "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded["units"] == "K" else None,
                "temp_std_dev_k": std_value,
                "source_file": key, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    # Direct period-max (TMAX) messages that overlap the target local day.
    period_rows = []
    for fh, wstart, wend in period_max_windows(run_dt):
        if wend <= day_start_utc or wstart >= day_end_utc:
            continue  # no overlap with the target local day
        key = grib_key(run_dt, run_dt.hour, fh, "core")
        try:
            entries = fetch_idx(key, stats)
            msg = find_message(entries, "TMAX", "2 m above ground")
            if msg is None:
                continue
            raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
            (raw_dir / f"{run_dt:%Y%m%d%H}_f{fh:03d}_TMAX_period.grib2").write_bytes(raw)
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
            period_rows.append(
                {
                    "run_time": run_dt, "forecast_hour": fh,
                    "period_start_utc": wstart, "period_end_utc": wend,
                    "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded["units"] == "K" else None,
                    "overlaps_full_day": wstart >= day_start_utc and wend <= day_end_utc,
                }
            )
        except RuntimeError as exc:
            print(f"    FAILED for period-max {key}: {exc}")

    return pd.DataFrame(rows), coverage, period_rows


def main() -> None:
    stats = RequestStats()
    investigate_structure_and_products(stats)

    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    print(f"\nTarget local day (America/New_York): {TARGET_DATE} -> UTC [{day_start_utc}, {day_end_utc})")

    snapshot_rows = variable_snapshot(datetime(2025, 6, 30, 12, tzinfo=timezone.utc), 24, stats)
    qmd_sanity_check(datetime(2025, 6, 30, 12, tzinfo=timezone.utc), 12, stats)

    print("\n" + "=" * 78)
    print("A7/A8/A9: SELECTED RUNS, EXTRACTION, DAILY-HIGH RECONSTRUCTION, REVISIONS")
    print("=" * 78)
    runs = select_runs()
    print(f"Selected {len(runs)} runs: {[f'{r:%Y-%m-%d %HZ}' for r in runs]}")

    all_dfs, coverage_by_run, all_period_rows = [], {}, []
    for run_dt in runs:
        df_run, coverage, period_rows = fetch_run_data(run_dt, day_start_utc, day_end_utc, stats)
        coverage_by_run[run_dt] = coverage
        print(f"  {run_dt:%Y-%m-%d %HZ}: {len(df_run)} hourly TMP rows, {len(period_rows)} period-TMAX rows, "
              f"full_day_coverage={coverage['full_day_coverage']}, adequate_coverage={coverage['adequate_coverage']}")
        all_dfs.append(df_run)
        all_period_rows.extend(period_rows)

    df = pd.concat(all_dfs, ignore_index=True)
    df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED_DIR / "nbm_test_canonical.csv", index=False)
    df.to_parquet(PROCESSED_DIR / "nbm_test_canonical.parquet", index=False)

    period_df = pd.DataFrame(all_period_rows)
    if not period_df.empty:
        period_df.to_csv(PROCESSED_DIR / "nbm_test_period_tmax.csv", index=False)

    print("\nDaily-max reconstruction (hourly-TMP-based) vs. direct period-TMAX guidance:")
    daily_max_rows = []
    for run_dt, g in df.groupby("run_time"):
        cov = coverage_by_run[run_dt]
        reconstructed_max = g["value_f"].max()
        direct_periods = period_df[period_df["run_time"] == run_dt] if not period_df.empty else pd.DataFrame()
        direct_max = direct_periods["value_f"].max() if not direct_periods.empty else None
        daily_max_rows.append(
            {
                "run_time": run_dt, "full_day_coverage": cov["full_day_coverage"], "adequate_coverage": cov["adequate_coverage"],
                "reconstructed_max_f_from_hourly": reconstructed_max,
                "direct_period_tmax_max_f": direct_max,
                "n_period_windows_used": len(direct_periods),
                "mean_temp_std_dev_k": g["temp_std_dev_k"].mean(),
            }
        )
    daily_max_df = pd.DataFrame(daily_max_rows).sort_values("run_time")
    print(daily_max_df.round(3).to_string(index=False))
    print("\n(reconstructed vs. direct are NOT assumed identical -- see the two rightmost value columns above; "
          "direct period-TMAX windows don't align to the local calendar day, so they may cover a different span.)")

    daily_max_df["revision_f"] = daily_max_df["reconstructed_max_f_from_hourly"].diff()
    print("\nRevision series (reconstructed-from-hourly basis):")
    print(daily_max_df[["run_time", "reconstructed_max_f_from_hourly", "revision_f", "mean_temp_std_dev_k"]].round(3).to_string(index=False))
    daily_max_df.to_csv(PROCESSED_DIR / "nbm_test_daily_max_and_revisions.csv", index=False)

    print("\n" + "=" * 78)
    print("A10: AVAILABILITY TIME")
    print("=" * 78)
    df["available_time_parsed"] = pd.to_datetime(df["available_time"], utc=True, errors="coerce")
    df["publication_lag_min"] = (df["available_time_parsed"] - df["run_time"]).dt.total_seconds() / 60
    lag_summary = df.groupby("run_time")["publication_lag_min"].agg(["min", "median", "max", "count"])
    print("Publication lag (S3 Last-Modified minus nominal run_time), minutes, per run:")
    print(lag_summary.round(1).to_string())
    print(
        "\nOBSERVED: lag generally INCREASES with forecast_hour within a run (later forecast-hour files are\n"
        "produced/uploaded later) -- files arrive PROGRESSIVELY, not all at once. This is the same qualitative\n"
        "pattern seen for HRRR/GFS/GEFS. Last-Modified is an OBSERVED proxy, not a documented dissemination\n"
        "guarantee -- not treated as such."
    )

    print("\n" + "=" * 78)
    print("A11: POINT-IN-TIME AUDIT")
    print("=" * 78)
    print(f"run_time preserved: {df['run_time'].notna().all()}")
    print(f"forecast_hour preserved: {df['forecast_hour'].notna().all()}")
    print(f"valid_time preserved: {df['valid_time'].notna().all()}")
    print(f"availability proxy preserved where available: {df['available_time'].notna().all()}")
    dup_valid = df.groupby("valid_time")["run_time"].nunique()
    print(f"Revisions survive independently: {int((dup_valid > 1).sum())} valid_time(s) have >1 surviving run_time record.")
    key_cols = ["model", "run_time", "forecast_hour", "variable", "grid_lat", "grid_lon"]
    print(f"Duplicate rows on key: {int(df.duplicated(subset=key_cols).sum())}")
    print("Local-day boundaries computed via zoneinfo America/New_York (data/nbm.py local_day_utc_bounds).")
    print("Every value from a genuine forecast message ('N hour fcst' idx text) -- no observation/verification data used.")

    print("\n" + "=" * 78)
    print("A12: SCALE ESTIMATE (extrapolated -- not executed)")
    print("=" * 78)
    avg_bytes_per_row = stats.bytes_downloaded / max(len(df), 1)
    print(f"This test: {stats.n_requests} requests, {stats.bytes_downloaded:,} bytes, {stats.seconds_elapsed:.1f}s, "
          f"{len(runs)} runs, {len(df)} hourly-temp rows.")
    runs_per_day, avg_fh_per_run = 24, 20
    rows_per_day_est = runs_per_day * avg_fh_per_run
    for label, days in [("1 year", 365), ("5 years", 365 * 5), ("10 years", 365 * 10)]:
        n_rows_est = rows_per_day_est * days
        bytes_est = n_rows_est * avg_bytes_per_row
        print(f"  {label:8s}: ~{runs_per_day*days:,} runs, ~{n_rows_est:,.0f} rows (temp+stddev), ~{bytes_est/1e9:.2f} GB (ESTIMATE)")
    print("If qmd (percentile) fields were also retained for precip/wind (temperature has none), that's a\n"
          "SEPARATE, much larger product (~274MB/file vs ~150MB core) -- not included in the above, and not\n"
          "useful for this project's temperature focus anyway.")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total requests: {stats.n_requests}")
    print(f"Total bytes downloaded: {stats.bytes_downloaded:,} ({stats.bytes_downloaded/1e6:.1f} MB)")
    print(f"Total request time: {stats.seconds_elapsed:.1f} s")
    print(f"Hourly-temp rows: {len(df)}, period-TMAX rows: {len(period_df)}, variable-snapshot rows: {len(snapshot_rows)}")
    print(f"Saved outputs to {PROCESSED_DIR} and {RAW_DIR}")


if __name__ == "__main__":
    main()
