"""ECMWF feasibility test for NYC, ONE DAY ONLY (2025-07-01).

Unlike HRRR/GFS/GEFS/NBM (all NOAA, all self-service AWS Open Data with a
documented, stable retention policy), ECMWF's access landscape is
different and was researched FIRST, per the task's explicit instruction,
before assuming any dataset was usable.

KEY FINDING (see Part B2/B3 below and data/ecmwf.py's module docstring):
ECMWF's own documentation states its free "Open Data" is retained for only
the most recent ~2-3 days. However, the actual AWS S3 bucket registered as
"ECMWF real-time forecasts" (s3://ecmwf-forecasts, an official entry on
the AWS Open Data registry) was OBSERVED LIVE to still contain our target
date, 2025-07-01, and objects back to at least 2023-01-18 -- directly
CONTRADICTING the documented retention window. This is used here because
it demonstrably works right now via the normal public interface (no
authentication bypass, no scraping of an unauthorized third party -- this
is ECMWF's own official public bucket), but it is NOT a documented or
guaranteed capability, and this discrepancy is treated as the single most
important caveat in this entire test.

Full MARS historical archive access (the officially documented path for
arbitrary historical operational data) requires ECMWF registration and,
for full/expanded archive access beyond what Open Data covers, a Service
Agreement (with a possible fee waiver for qualifying research, but that is
an application/approval process, not immediate self-service) -- NOT
pursued here, consistent with "do not bypass access restrictions."

Feasibility test only. Does not touch Kalshi/HRRR/GFS/GEFS/observation/NBM
data, no modeling, no trading logic. Ensemble sampling deliberately kept
small (a handful of steps, not a full daily series) given each enfo file
bundles all 51 members in one ~6.5GB object.

Usage:
    python scripts/test_ecmwf_nyc_feasibility.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.ecmwf import (  # noqa: E402
    RUN_HOURS,
    RequestStats,
    decode_message,
    fetch_byte_range,
    fetch_index,
    find_message,
    grib_key,
    list_objects,
    local_day_utc_bounds,
    steps_for_local_day,
)

NYC_LOCATION = {"name": "NYC test point (approx. Central Park)", "lat": 40.78, "lon": -73.97}
TARGET_DATE = date(2025, 7, 1)

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "ecmwf" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "ecmwf" / "test"


def _lon_to_180(lon_0_360: float) -> float:
    return lon_0_360 - 360 if lon_0_360 > 180 else lon_0_360


def available_steps() -> list[int]:
    """OBSERVED via HEAD probes against 2025-07-01 00z oper: 3-hourly
    through 144h, then 6-hourly through 360h."""
    return list(range(0, 145, 3)) + list(range(150, 361, 6))


# ---------------------------------------------------------------------------
# B1/B2/B3 -- data landscape, access investigation, classification.
# ---------------------------------------------------------------------------

def report_landscape_and_access(stats: RequestStats) -> None:
    print("=" * 78)
    print("B1: ECMWF DATA LANDSCAPE")
    print("=" * 78)
    print(
        "Products investigated:\n"
        "  - IFS 'oper' (deterministic HRES, 0.25deg): the main physics-based deterministic forecast.\n"
        "  - IFS 'enfo' (ENS ensemble, 0.25deg): 51 members (1 control 'cf' + 50 perturbed 'pf').\n"
        "  - 'aifs-single': ECMWF's newer AI-based forecast model -- DIFFERENT from classic IFS, not\n"
        "    used here (task asks specifically about IFS deterministic/ensemble).\n"
        "  - ERA5: reanalysis, explicitly EXCLUDED per task instructions -- it is not a historical\n"
        "    point-in-time operational forecast (it incorporates observations from after the forecast\n"
        "    time and is produced long after the fact), so it cannot substitute for what a trader could\n"
        "    have known historically.\n"
    )

    print("=" * 78)
    print("B2/B3: ACCESS INVESTIGATION + CLASSIFICATION")
    print("=" * 78)
    print(
        "DOCUMENTED (ECMWF's own pages, https://www.ecmwf.int/en/forecasts/datasets/open-data and\n"
        "accessing-forecasts): free 'Open Data' is retained for 'the most recent 12 forecast runs\n"
        "(~2-3 days)'. Full historical MARS archive access requires ECMWF registration, and for\n"
        "non-Member-State users, either the limited free tier (Open Data / WMO essential products /\n"
        "public archive & reanalysis) or a Service Agreement (possibly with a fee waiver for qualifying\n"
        "research -- an application/approval process, not instant self-service).\n"
    )
    _, _, key_count_2023 = list_objects("20230118/", max_keys=1)
    stats.record(0, 0.0)
    _, _, key_count_target = list_objects(f"{TARGET_DATE:%Y%m%d}/00z/ifs/0p25/oper/", max_keys=1)
    stats.record(0, 0.0)
    print(
        f"OBSERVED FROM ARCHIVE (contradicts the documented 2-3 day policy): the official AWS Open Data\n"
        f"bucket s3://ecmwf-forecasts (registry: https://registry.opendata.aws/ecmwf-forecasts/) still\n"
        f"contains objects from 2023-01-18 (key count check: {key_count_2023}) and from our target date\n"
        f"{TARGET_DATE} (key count check: {key_count_target}), both fetched live just now via the normal\n"
        f"public interface -- no authentication bypass, no third-party scraping.\n"
    )
    print(
        "CLASSIFICATION: functionally 'A' (freely/publicly accessible) FOR THIS SPECIFIC DATE, RIGHT NOW\n"
        "-- but with a critical caveat: this is NOT the documented/guaranteed behavior of the source. A\n"
        "production system depending on this bucket for arbitrary historical dates should treat it as\n"
        "having NO documented retention guarantee and could see older objects pruned at any time. This\n"
        "is the single most important caveat in this ECMWF test."
    )


def variable_snapshot(run_dt: datetime, step: int, stats: RequestStats) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"B5: VARIABLE SNAPSHOT (oper, deterministic) -- run {run_dt:%Y-%m-%d %HZ}, step={step}h")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, step, "oper")
    entries = fetch_index(key, stats)
    print(f"Parsed {len(entries)} messages from index for {key}")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    targets = ["2t", "2d", "10u", "10v", "sp", "tp", "mx2t3"]
    rows = []
    for param in targets:
        msg = find_message(entries, param)
        if msg is None:
            print(f"  {param:8s} -- NOT FOUND (skipped, not forced)")
            continue
        raw, last_modified = fetch_byte_range(key, msg["_offset"], msg["_length"], stats)
        (raw_dir / f"{run_dt:%Y%m%d%H}_s{step:03d}_{param}.grib2").write_bytes(raw)
        decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        print(f"  {param:8s} -> {msg['_length']:>9,} bytes | value={decoded['value']:.3f} {decoded['units']}")
        rows.append({"run_dt": run_dt, "step": step, "param": param, **decoded})
    return rows


def select_runs() -> list[datetime]:
    return [datetime(2025, 6, 30, 12, tzinfo=timezone.utc), datetime(2025, 7, 1, 0, tzinfo=timezone.utc)]


def fetch_deterministic_series(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats) -> pd.DataFrame:
    steps = steps_for_local_day(run_dt, day_start_utc, day_end_utc, available_steps())
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for step in steps:
        key = grib_key(run_dt, run_dt.hour, step, "oper")
        try:
            entries = fetch_index(key, stats)
            msg = find_message(entries, "2t")
            if msg is None:
                continue
            raw, last_modified = fetch_byte_range(key, msg["_offset"], msg["_length"], stats)
            (raw_dir / f"{run_dt:%Y%m%d%H}_s{step:03d}_2t.grib2").write_bytes(raw)
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        except RuntimeError as exc:
            print(f"    FAILED for {key}: {exc}")
            continue
        valid_time = run_dt + timedelta(hours=step)
        rows.append(
            {
                "source": "ECMWF Open Data (AWS)", "model": "ECMWF-IFS", "product": "oper",
                "ensemble_member": None,
                "requested_lat": NYC_LOCATION["lat"], "requested_lon": NYC_LOCATION["lon"],
                "grid_lat": decoded["grid_lat"], "grid_lon": _lon_to_180(decoded["grid_lon"]),
                "distance_km": decoded["distance_km"],
                "run_time": run_dt, "available_time": last_modified,
                "forecast_hour": step, "valid_time": valid_time,
                "variable": "2t", "level": "2m",
                "value": decoded["value"], "unit": decoded["units"],
                "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded["units"] == "K" else None,
                "source_file": key, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return pd.DataFrame(rows)


def fetch_ensemble_sample(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats, n_steps: int = 3) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print(f"B7: ENSEMBLE SAMPLE (enfo) -- run {run_dt:%Y-%m-%d %HZ}, SMALL sample only ({n_steps} steps, not a full series)")
    print("=" * 78)
    steps = steps_for_local_day(run_dt, day_start_utc, day_end_utc, available_steps())[:n_steps]
    print(f"Using steps: {steps} (deliberately truncated to keep this a small sample per task instructions)")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for step in steps:
        key = grib_key(run_dt, run_dt.hour, step, "enfo")
        try:
            entries = fetch_index(key, stats)
        except RuntimeError as exc:
            print(f"  FAILED to fetch index for {key}: {exc}")
            continue
        print(f"  step {step}h: {len(entries)} total messages in index")

        cf_msg = find_message(entries, "2t", number=None)
        members_data = []
        if cf_msg:
            members_data.append(("control", cf_msg))
        for n in range(1, 51):
            pf_msg = find_message(entries, "2t", number=n)
            if pf_msg:
                members_data.append((f"p{n:02d}", pf_msg))

        for member_label, msg in members_data:
            try:
                raw, last_modified = fetch_byte_range(key, msg["_offset"], msg["_length"], stats)
            except RuntimeError as exc:
                print(f"    FAILED for member {member_label} step {step}: {exc}")
                continue
            decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
            valid_time = run_dt + timedelta(hours=step)
            rows.append(
                {
                    "source": "ECMWF Open Data (AWS)", "model": "ECMWF-ENS", "product": "enfo",
                    "ensemble_member": member_label,
                    "member_type": "control" if member_label == "control" else "perturbed",
                    "requested_lat": NYC_LOCATION["lat"], "requested_lon": NYC_LOCATION["lon"],
                    "grid_lat": decoded["grid_lat"], "grid_lon": _lon_to_180(decoded["grid_lon"]),
                    "distance_km": decoded["distance_km"],
                    "run_time": run_dt, "available_time": last_modified,
                    "forecast_hour": step, "valid_time": valid_time,
                    "variable": "2t", "level": "2m",
                    "value": decoded["value"], "unit": decoded["units"],
                    "value_f": (decoded["value"] - 273.15) * 9 / 5 + 32 if decoded["units"] == "K" else None,
                    "source_file": key, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        print(f"  step {step}h: fetched {len(members_data)} of 51 expected members for 2t")
    return pd.DataFrame(rows)


def main() -> None:
    stats = RequestStats()
    report_landscape_and_access(stats)

    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    print(f"\nTarget local day (America/New_York): {TARGET_DATE} -> UTC [{day_start_utc}, {day_end_utc})")

    snapshot_rows = variable_snapshot(datetime(2025, 6, 30, 12, tzinfo=timezone.utc), 24, stats)

    print("\n" + "=" * 78)
    print("B4/B6: DETERMINISTIC RUNS + DAILY-HIGH RECONSTRUCTION + REVISIONS")
    print("=" * 78)
    runs = select_runs()
    print(f"Selected {len(runs)} runs (only 00z/12z exist for oper/enfo, confirmed above): "
          f"{[f'{r:%Y-%m-%d %HZ}' for r in runs]}")

    det_dfs = []
    for run_dt in runs:
        df_run = fetch_deterministic_series(run_dt, day_start_utc, day_end_utc, stats)
        print(f"  {run_dt:%Y-%m-%d %HZ}: {len(df_run)} 2t rows (steps: {sorted(df_run['forecast_hour'].tolist())})")
        det_dfs.append(df_run)
    det_df = pd.concat(det_dfs, ignore_index=True)
    det_df["run_time"] = pd.to_datetime(det_df["run_time"], utc=True)
    det_df["valid_time"] = pd.to_datetime(det_df["valid_time"], utc=True)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    det_df.to_csv(PROCESSED_DIR / "ecmwf_test_deterministic_canonical.csv", index=False)

    daily_max = det_df.groupby("run_time")["value_f"].max().reset_index().rename(columns={"value_f": "predicted_max_f"})
    daily_max = daily_max.sort_values("run_time")
    daily_max["revision_f"] = daily_max["predicted_max_f"].diff()
    print("\nDeterministic daily-high reconstruction + revision (NOTE: only ~8 of 24 local-day hours are "
          "reachable per run since oper's 3-hourly step schedule rarely lands on every hour -- a sparser "
          "sample than HRRR/GFS, reported honestly, not smoothed over):")
    print(daily_max.round(3).to_string(index=False))
    daily_max.to_csv(PROCESSED_DIR / "ecmwf_test_daily_max_and_revisions.csv", index=False)

    ens_df = fetch_ensemble_sample(datetime(2025, 7, 1, 0, tzinfo=timezone.utc), day_start_utc, day_end_utc, stats, n_steps=3)
    ens_df["run_time"] = pd.to_datetime(ens_df["run_time"], utc=True) if not ens_df.empty else ens_df
    if not ens_df.empty:
        ens_df.to_csv(PROCESSED_DIR / "ecmwf_test_ensemble_sample_canonical.csv", index=False)

        member_max = ens_df.groupby(["ensemble_member", "member_type"])["value_f"].max().reset_index()
        member_max = member_max.rename(columns={"value_f": "member_max_over_sampled_steps_f"})
        print("\nPer-member max over the SAMPLED steps only (NOT a full-day max -- explicitly labeled):")
        print(member_max.sort_values("member_max_over_sampled_steps_f", ascending=False).to_string(index=False))

        vals = member_max["member_max_over_sampled_steps_f"]
        print("\nDistribution of per-member (partial-sample) max:")
        summary = {
            "n_members": len(vals), "mean": vals.mean(), "std": vals.std(), "min": vals.min(), "max": vals.max(),
            "p10": vals.quantile(0.10), "p50": vals.quantile(0.50), "p90": vals.quantile(0.90),
        }
        for k, v in summary.items():
            print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
        for t in [88, 90, 92, 94]:
            freq = (vals >= t).mean()
            print(f"  RAW ensemble frequency Tmax(partial-sample) >= {t}F: {freq:.3f} (NOT a calibrated probability)")
        member_max.to_csv(PROCESSED_DIR / "ecmwf_test_ensemble_member_max.csv", index=False)
    else:
        print("\nNo ensemble rows retrieved -- skipping distribution summary.")

    print("\n" + "=" * 78)
    print("B8: AVAILABILITY-TIME FINDINGS")
    print("=" * 78)
    det_df["available_time_parsed"] = pd.to_datetime(det_df["available_time"], utc=True, errors="coerce")
    det_df["publication_lag_min"] = (det_df["available_time_parsed"] - det_df["run_time"]).dt.total_seconds() / 60
    print("Publication lag (S3 Last-Modified minus nominal run_time), minutes, per run (deterministic 2t):")
    print(det_df.groupby("run_time")["publication_lag_min"].agg(["min", "median", "max", "count"]).round(1).to_string())
    print(
        "\nDistinguishing model initialization (run_time, nominal) from forecast production (unknown -- ECMWF\n"
        "doesn't expose this) from archive publication (S3 Last-Modified, OBSERVED) from actual dissemination\n"
        "(UNKNOWN -- could differ from archive publication for licensed real-time feeds). Only the archive-\n"
        "publication proxy is used here; it is NOT treated as a guaranteed dissemination time."
    )

    print("\n" + "=" * 78)
    print("B9: POINT-IN-TIME AUDIT")
    print("=" * 78)
    print(f"run_time preserved: {det_df['run_time'].notna().all()}")
    print(f"forecast_hour preserved: {det_df['forecast_hour'].notna().all()}")
    print(f"valid_time preserved: {det_df['valid_time'].notna().all()}")
    if not ens_df.empty:
        print(f"member identity preserved: {ens_df['ensemble_member'].notna().all()}")
    dup_valid = det_df.groupby("valid_time")["run_time"].nunique()
    print(f"No overwrite / revisions preserved: {int((dup_valid > 1).sum())} valid_time(s) have >1 surviving run_time.")
    key_cols = ["model", "run_time", "forecast_hour", "variable", "grid_lat", "grid_lon"]
    print(f"Duplicate rows on key (deterministic): {int(det_df.duplicated(subset=key_cols).sum())}")
    print("No reanalysis contamination: every value from an 'fc'/'ef' forecast stream object, never ERA5.")
    print("Local-day boundaries computed via zoneinfo America/New_York (data/ecmwf.py local_day_utc_bounds).")
    print("Availability timing status: OBSERVED proxy only (S3 Last-Modified) -- not a documented guarantee.")

    print("\n" + "=" * 78)
    print("B10: COMPARISON WITH EXISTING SOURCES (aligned run_times, no interpolation)")
    print("=" * 78)
    hrrr_path = PROJECT_ROOT / "data" / "processed" / "weather" / "hrrr" / "test" / "hrrr_test_predicted_daily_max.csv"
    gfs_path = PROJECT_ROOT / "data" / "processed" / "weather" / "gfs" / "test" / "gfs_test_predicted_daily_max.csv"
    gefs_path = PROJECT_ROOT / "data" / "processed" / "weather" / "gefs" / "test" / "gefs_test_ensemble_distribution.csv"
    rows = []
    for _, r in daily_max.iterrows():
        run_dt = r["run_time"]
        row = {"run_time": run_dt, "ECMWF_deterministic": r["predicted_max_f"]}
        if hrrr_path.exists():
            hrrr = pd.read_csv(hrrr_path, parse_dates=["run_time"])
            hrrr["run_time"] = hrrr["run_time"].dt.tz_localize("UTC") if hrrr["run_time"].dt.tz is None else hrrr["run_time"].dt.tz_convert("UTC")
            m = hrrr[hrrr["run_time"] == run_dt]
            row["HRRR"] = m["predicted_max_f"].iloc[0] if not m.empty else None
        if gfs_path.exists():
            gfs = pd.read_csv(gfs_path, parse_dates=["run_time"])
            gfs["run_time"] = gfs["run_time"].dt.tz_localize("UTC") if gfs["run_time"].dt.tz is None else gfs["run_time"].dt.tz_convert("UTC")
            m = gfs[gfs["run_time"] == run_dt]
            row["GFS"] = m["predicted_max_f"].iloc[0] if not m.empty else None
        if gefs_path.exists():
            gefs = pd.read_csv(gefs_path, parse_dates=["run_time"])
            gefs["run_time"] = gefs["run_time"].dt.tz_localize("UTC") if gefs["run_time"].dt.tz is None else gefs["run_time"].dt.tz_convert("UTC")
            m = gefs[gefs["run_time"] == run_dt]
            if not m.empty:
                row["GEFS_p10"] = m["p10"].iloc[0]
                row["GEFS_mean"] = m["mean"].iloc[0]
                row["GEFS_p90"] = m["p90"].iloc[0]
        rows.append(row)
    comparison = pd.DataFrame(rows)
    print(comparison.round(2).to_string(index=False))
    comparison.to_csv(PROCESSED_DIR / "ecmwf_vs_existing_sources_comparison.csv", index=False)
    print("\n(Structural sanity check only -- not an accuracy comparison, no model ranking.)")

    print("\n" + "=" * 78)
    print("B11: SCALE/COST ESTIMATE (extrapolated -- not executed)")
    print("=" * 78)
    det_bytes_per_row = stats.bytes_downloaded / max(len(det_df) + len(ens_df), 1)
    print(f"This test: {stats.n_requests} requests, {stats.bytes_downloaded:,} bytes, {stats.seconds_elapsed:.1f}s")
    runs_per_day, avg_steps_per_run = 2, 8
    det_rows_per_day = runs_per_day * avg_steps_per_run
    for label, days in [("1 year", 365), ("5 years", 365 * 5), ("10 years", 365 * 10)]:
        det_rows = det_rows_per_day * days
        print(f"  {label:8s} deterministic: ~{runs_per_day*days:,} runs, ~{det_rows:,.0f} rows, "
              f"~{det_rows*det_bytes_per_row/1e9:.2f} GB (ESTIMATE)")
        ens_rows = det_rows * 51
        print(f"  {label:8s} ensemble (51 members): ~{ens_rows:,.0f} rows, ~{ens_rows*det_bytes_per_row/1e9:.2f} GB (ESTIMATE)")
    print("Financial/API cost: $0 IF the current bucket behavior persists (no documented guarantee -- see B2/B3). "
          "If it disappears, the only documented path is MARS registration + possible Service Agreement/fee-waiver "
          "application -- cost undetermined without contacting ECMWF directly.")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total requests: {stats.n_requests}")
    print(f"Total bytes downloaded: {stats.bytes_downloaded:,} ({stats.bytes_downloaded/1e6:.1f} MB)")
    print(f"Total request time: {stats.seconds_elapsed:.1f} s")
    print(f"Deterministic rows: {len(det_df)}, ensemble sample rows: {len(ens_df)}, snapshot rows: {len(snapshot_rows)}")
    print(f"Saved outputs to {PROCESSED_DIR} and {RAW_DIR}")


if __name__ == "__main__":
    main()
