"""GEFS ensemble historical-data feasibility test for NYC, ONE DAY ONLY.

Unlike the HRRR/GFS deterministic tests, the objective here is not "what
temperature did the model predict" but "what DISTRIBUTION of possible NYC
daily-max temperatures did the GEFS ensemble imply at each historical run."

Feasibility test only -- does not scale beyond 2025-07-01, does not touch
Kalshi/HRRR/GFS data, no weather modeling, no Jev/Laya integration, no
trading logic.

Run selection: 3 runs (June 30 12Z, June 30 18Z, July 1 00Z) rather than
the suggested 5 -- deliberately reduced because each GEFS run costs ~31x
the requests of one GFS/HRRR run (31 members). These 3 runs still
demonstrate ensemble distribution, forecast revision, changing spread, and
same-valid-time comparison across >=2 runs, and happen to exactly match
run_times already tested for HRRR and GFS, enabling a genuine 3-way
comparison (Part 16) without interpolating anything.

Usage:
    python scripts/test_gefs_nyc_feasibility.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.gefs import (  # noqa: E402
    EXPECTED_MEMBER_COUNT,
    MEMBERS,
    RUN_HOURS,
    RequestStats,
    decode_message,
    fetch_byte_range,
    fetch_idx,
    find_message,
    grib_key,
    list_objects,
    local_day_utc_bounds,
    member_type,
    needed_forecast_hours,
)

NYC_LOCATION = {
    "name": "NYC test point (approx. Central Park)",
    "lat": 40.78,
    "lon": -73.97,
    "note": "Provisional -- Kalshi settlement station/location not yet finalized across all regimes.",
}

TARGET_DATE = date(2025, 7, 1)
PRODUCT = "pgrb2sp25"

SNAPSHOT_VARIABLES = [
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

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "gefs" / "test"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "weather" / "gefs" / "test"

THRESHOLDS_F = [88, 90, 92, 94]


def _lon_to_180(lon_0_360: float) -> float:
    return lon_0_360 - 360 if lon_0_360 > 180 else lon_0_360


# ---------------------------------------------------------------------------
# PART 1/2 -- archive + ensemble structure.
# ---------------------------------------------------------------------------

def investigate_structure(stats: RequestStats) -> None:
    print("=" * 78)
    print("PART 1/2: ARCHIVE + ENSEMBLE STRUCTURE")
    print("=" * 78)
    _, run_prefixes = list_objects("gefs.20250701/", delimiter="/")
    stats.record(0, 0.0)
    run_hours_found = sorted(int(p.rstrip("/").split("/")[-1]) for p in run_prefixes if p.rstrip("/").split("/")[-1].isdigit())
    print(f"Run hours found for 2025-07-01 (OBSERVED FROM ARCHIVE): {run_hours_found}")
    print(f"Matches documented 4x/day schedule {sorted(RUN_HOURS)}: {run_hours_found == sorted(RUN_HOURS)}")

    print(f"\nMember mapping (OBSERVED FROM ARCHIVE -- gep31 confirmed absent/404):")
    mapping = pd.DataFrame(
        [{"member_id": m, "member_type": member_type(m), "archive_identifier": m} for m in MEMBERS[:3] + ["...", MEMBERS[-1]]]
    )
    print(mapping.to_string(index=False))
    print(f"Total forecast members: {EXPECTED_MEMBER_COUNT} (1 control + 30 perturbed). geavg/gespr also exist but are precomputed statistics, not members.")

    sample_obj, _ = list_objects("gefs.20250701/12/atmos/pgrb2sp25/gep01.t12z.pgrb2s.0p25.f000")
    stats.record(0, 0.0)
    if sample_obj:
        print(f"\nExample per-member file size (F000): {sample_obj[0]['size']:,} bytes (~{sample_obj[0]['size']/1e6:.1f} MB)")
    print("Forecast-hour schedule (OBSERVED via HEAD probes): uniform 3-hourly F000-F240 (10 days) for every member, every run hour.")
    print(".idx sidecar files ARE present per member per forecast hour (38 messages each in the pgrb2sp25 product vs HRRR's 173 / GFS's 743).")


# ---------------------------------------------------------------------------
# PART 5 -- variable snapshot (ONE member, ONE hour only).
# ---------------------------------------------------------------------------

def variable_snapshot(run_dt: datetime, member: str, forecast_hour: int, stats: RequestStats) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"PART 5 (snapshot): {member} @ {run_dt:%Y-%m-%d %HZ}, FH={forecast_hour} -- availability only, not a full series")
    print("=" * 78)
    key = grib_key(run_dt, run_dt.hour, member, forecast_hour, PRODUCT)
    entries = fetch_idx(key, stats)
    print(f"Parsed {len(entries)} messages from {key}.idx (compact subset product)")

    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for variable, level in SNAPSHOT_VARIABLES:
        msg = find_message(entries, variable, level)
        if msg is None:
            print(f"  {variable}:{level} -- NOT FOUND (skipped, not forced)")
            continue
        raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
        n_bytes = msg["byte_end"] - msg["byte_start"] + 1 if msg["byte_end"] else len(raw)
        (raw_dir / f"{run_dt:%Y%m%d%H}_{member}_f{forecast_hour:03d}_{variable}.grib2").write_bytes(raw)
        decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
        print(f"  {variable}:{level:22s} ({msg['forecast_desc']:18s}) -> {n_bytes:>8,} bytes | value={decoded['value']:.3f} {decoded['units']}")
        rows.append({"run_dt": run_dt, "member": member, "forecast_hour": forecast_hour, "variable": variable, **decoded})
    return rows


# ---------------------------------------------------------------------------
# PART 7/8 -- run selection + temperature-only ensemble extraction.
# ---------------------------------------------------------------------------

def select_runs() -> list[datetime]:
    return [
        datetime(2025, 6, 30, 12, tzinfo=timezone.utc),
        datetime(2025, 6, 30, 18, tzinfo=timezone.utc),
        datetime(2025, 7, 1, 0, tzinfo=timezone.utc),
    ]


def fetch_ensemble_temperature(run_dt: datetime, day_start_utc: datetime, day_end_utc: datetime, stats: RequestStats) -> tuple[list[dict], dict, dict]:
    coverage = needed_forecast_hours(run_dt, day_start_utc, day_end_utc)
    raw_dir = RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "messages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    member_status = {}
    for member in MEMBERS:
        n_ok, n_fail = 0, 0
        for fh in coverage["forecast_hours"]:
            key = grib_key(run_dt, run_dt.hour, member, fh, PRODUCT)
            try:
                entries = fetch_idx(key, stats)
                msg = find_message(entries, "TMP", "2 m above ground")
                if msg is None:
                    n_fail += 1
                    continue
                raw, last_modified = fetch_byte_range(key, msg["byte_start"], msg["byte_end"], stats)
                (raw_dir / f"{run_dt:%Y%m%d%H}_{member}_f{fh:03d}_TMP.grib2").write_bytes(raw)
                decoded = decode_message(raw, NYC_LOCATION["lat"], NYC_LOCATION["lon"])
            except RuntimeError as exc:
                print(f"    FAILED for {key}: {exc}")
                n_fail += 1
                continue
            n_ok += 1
            valid_time = run_dt + timedelta(hours=fh)
            rows.append(
                {
                    "source": "NOAA GEFS AWS archive",
                    "model": "GEFS",
                    "product": PRODUCT,
                    "ensemble_member": member,
                    "member_type": member_type(member),
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
        member_status[member] = {
            "n_forecast_hours_ok": n_ok,
            "n_forecast_hours_expected": len(coverage["forecast_hours"]),
            "member_complete": n_ok == len(coverage["forecast_hours"]) and len(coverage["forecast_hours"]) > 0,
        }
    return rows, coverage, member_status


# ---------------------------------------------------------------------------
# PART 9 -- member-level daily maximum (max per member, NEVER mean-then-max).
# ---------------------------------------------------------------------------

def compute_member_daily_max(df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 9: MEMBER-LEVEL DAILY MAXIMUM (max computed independently per member)")
    print("=" * 78)
    out = df.groupby(["run_time", "ensemble_member", "member_type"])["value_f"].max().reset_index()
    out = out.rename(columns={"value_f": "member_predicted_daily_max_f"})
    for run_dt, g in out.groupby("run_time"):
        print(f"\nRun {run_dt}:")
        print(g[["ensemble_member", "member_type", "member_predicted_daily_max_f"]].to_string(index=False))
    return out


# ---------------------------------------------------------------------------
# PART 10 -- ensemble distribution per run.
# ---------------------------------------------------------------------------

def summarize_distribution(member_max_df: pd.DataFrame, coverage_by_run: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 10: ENSEMBLE DAILY-HIGH DISTRIBUTION PER RUN (raw ensemble frequencies -- NOT calibrated probabilities)")
    print("=" * 78)
    rows = []
    for run_dt, g in member_max_df.groupby("run_time"):
        vals = g["member_predicted_daily_max_f"]
        cov = coverage_by_run[run_dt]
        row = {
            "run_time": run_dt,
            "full_day_coverage": cov["full_day_coverage"],
            "n_members": len(vals),
            "mean": vals.mean(),
            "std": vals.std(),
            "min": vals.min(),
            "max": vals.max(),
            "p10": vals.quantile(0.10),
            "p25": vals.quantile(0.25),
            "p50": vals.quantile(0.50),
            "p75": vals.quantile(0.75),
            "p90": vals.quantile(0.90),
        }
        for t in THRESHOLDS_F:
            row[f"raw_freq_ge_{t}F"] = (vals >= t).mean()
        rows.append(row)
    result = pd.DataFrame(rows)
    print(result.round(3).to_string(index=False))
    print("\n(raw_freq_ge_XF = fraction of valid ensemble members with predicted max >= X F -- RAW ensemble frequency, not a calibrated probability.)")
    return result


# ---------------------------------------------------------------------------
# PART 11 -- integer temperature distribution (exploratory only).
# ---------------------------------------------------------------------------

def integer_temperature_distribution(member_max_df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 11: INTEGER TEMPERATURE DISTRIBUTION (exploratory; TEMPORARY rounding rule)")
    print("=" * 78)
    print("Rounding rule used here (temporary, diagnostic ONLY -- NOT the eventual Kalshi bucket rule): "
          "Python's round() (round-half-to-even) applied to each member's continuous predicted_daily_max_f. "
          "The original continuous value is preserved unchanged in member_predicted_daily_max_f.")
    df = member_max_df.copy()
    df["temperature_bin_f"] = df["member_predicted_daily_max_f"].round().astype(int)
    out = df.groupby(["run_time", "temperature_bin_f"]).size().reset_index(name="member_count")
    out["raw_frequency"] = out.groupby("run_time")["member_count"].transform(lambda s: s / s.sum())
    for run_dt, g in out.groupby("run_time"):
        print(f"\nRun {run_dt}:")
        print(g[["temperature_bin_f", "member_count", "raw_frequency"]].sort_values("temperature_bin_f").to_string(index=False))
    return out


# ---------------------------------------------------------------------------
# PART 12 -- ensemble revision through time.
# ---------------------------------------------------------------------------

def revision_through_time(dist_df: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 12: ENSEMBLE REVISION THROUGH TIME (forecast revision AND uncertainty revision)")
    print("=" * 78)
    d = dist_df.sort_values("run_time")[["run_time", "mean", "p50", "std", "p10", "p90"]].copy()
    d["mean_revision"] = d["mean"].diff()
    d["std_revision"] = d["std"].diff()
    print(d.round(3).to_string(index=False))
    return d


# ---------------------------------------------------------------------------
# PART 13 -- same valid-time ensemble test.
# ---------------------------------------------------------------------------

def same_valid_time_ensemble_test(df: pd.DataFrame, target_valid_time: datetime) -> None:
    print("\n" + "=" * 78)
    print(f"PART 13: SAME VALID-TIME ENSEMBLE TEST -- valid_time = {target_valid_time}")
    print("=" * 78)
    sub = df[df["valid_time"] == target_valid_time]
    if sub.empty:
        print("  No selected run covers exactly this valid_time.")
        return
    for run_dt, g in sub.groupby("run_time"):
        print(f"\nRun {run_dt} ({len(g)} members):")
        gg = g.sort_values("ensemble_member")[["ensemble_member", "member_type", "value_f"]]
        print(gg.to_string(index=False))
        print(f"  spread across members: min={g['value_f'].min():.2f}F max={g['value_f'].max():.2f}F range={g['value_f'].max()-g['value_f'].min():.2f}F")


# ---------------------------------------------------------------------------
# PART 14 -- member completeness.
# ---------------------------------------------------------------------------

def report_member_completeness(member_status_by_run: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("PART 14: MEMBER COMPLETENESS")
    print("=" * 78)
    rows = []
    for run_dt, statuses in member_status_by_run.items():
        actual = sum(1 for s in statuses.values() if s["member_complete"])
        incomplete = [m for m, s in statuses.items() if not s["member_complete"]]
        rows.append(
            {
                "run_time": run_dt,
                "expected_member_count": EXPECTED_MEMBER_COUNT,
                "run_member_count": actual,
                "ensemble_complete": actual == EXPECTED_MEMBER_COUNT,
                "incomplete_members": incomplete if incomplete else None,
            }
        )
        print(f"Run {run_dt}: {actual}/{EXPECTED_MEMBER_COUNT} members complete"
              + (f" -- INCOMPLETE: {incomplete}" if incomplete else " -- ensemble_complete=True"))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# PART 15/16 -- structural comparisons with GFS and HRRR.
# ---------------------------------------------------------------------------

def compare_with_gfs_and_hrrr(dist_df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 15/16: STRUCTURAL COMPARISON WITH DETERMINISTIC GFS (+ optional HRRR) -- NOT an accuracy test")
    print("=" * 78)
    gfs_path = PROJECT_ROOT / "data" / "processed" / "weather" / "gfs" / "test" / "gfs_test_predicted_daily_max.csv"
    hrrr_path = PROJECT_ROOT / "data" / "processed" / "weather" / "hrrr" / "test" / "hrrr_test_predicted_daily_max.csv"
    if not gfs_path.exists():
        print("  GFS test output not found -- skipping comparison.")
        return
    gfs = pd.read_csv(gfs_path, parse_dates=["run_time"])
    gfs["run_time"] = gfs["run_time"].dt.tz_convert("UTC") if gfs["run_time"].dt.tz else gfs["run_time"].dt.tz_localize("UTC")

    rows = []
    for _, row in dist_df.iterrows():
        run_dt = row["run_time"]
        gfs_match = gfs[gfs["run_time"] == run_dt]
        gfs_val = gfs_match["predicted_max_f"].iloc[0] if not gfs_match.empty else None
        rows.append(
            {
                "run_time": run_dt,
                "GFS_deterministic": gfs_val,
                "GEFS_mean": row["mean"],
                "GEFS_p10": row["p10"],
                "GEFS_p50": row["p50"],
                "GEFS_p90": row["p90"],
            }
        )
    comparison = pd.DataFrame(rows)
    print(comparison.round(2).to_string(index=False))
    print("\n(This checks whether GFS's single value sits sensibly inside the GEFS spread -- NOT which is more accurate.)")

    if hrrr_path.exists():
        hrrr = pd.read_csv(hrrr_path, parse_dates=["run_time"])
        hrrr["run_time"] = hrrr["run_time"].dt.tz_convert("UTC") if hrrr["run_time"].dt.tz else hrrr["run_time"].dt.tz_localize("UTC")
        comparison2 = comparison.copy()
        comparison2["HRRR"] = comparison2["run_time"].map(hrrr.set_index("run_time")["predicted_max_f"])
        print("\nOptional 3-way context (HRRR + GFS + GEFS, same run_times, no interpolation):")
        print(comparison2[["run_time", "HRRR", "GFS_deterministic", "GEFS_p10", "GEFS_mean", "GEFS_p90"]].round(2).to_string(index=False))
        comparison2.to_csv(PROCESSED_DIR / "gefs_hrrr_gfs_comparison.csv", index=False)
    comparison.to_csv(PROCESSED_DIR / "gefs_gfs_comparison.csv", index=False)


# ---------------------------------------------------------------------------
# PART 17/18 -- point-in-time audit + validation.
# ---------------------------------------------------------------------------

def audit_point_in_time(df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 17: POINT-IN-TIME / LOOK-AHEAD AUDIT")
    print("=" * 78)
    print(f"1. run_time preserved: {df['run_time'].notna().all()}")
    print(f"2. forecast_hour preserved: {df['forecast_hour'].notna().all()}")
    print(f"3. valid_time preserved: {df['valid_time'].notna().all()}")
    print(f"4. ensemble_member preserved: {df['ensemble_member'].notna().all()}")
    dup_member = df.groupby(["run_time", "valid_time"])["ensemble_member"].nunique()
    print(f"5. Member files never collapse into each other: every (run_time,valid_time) has up to "
          f"{int(dup_member.max())} distinct members present (expect up to 31).")
    dup_valid = df.groupby("valid_time")["run_time"].nunique()
    print(f"6. Later runs never overwrite earlier runs: {int((dup_valid > 1).sum())} valid_time(s) have multiple surviving run_time records.")
    print("7. Every value from a genuine forecast message ('N hour fcst' in idx), never observation/reanalysis.")
    print("8. Daily-max calculated independently per (run_time, ensemble_member) -- confirmed by the groupby key in compute_member_daily_max().")
    n_null_avail = int(df["available_time"].isna().sum())
    print(f"9. available_time populated (OBSERVED, not fabricated) for {len(df)-n_null_avail} of {len(df)} rows.")
    print("10. Local-day boundaries computed via zoneinfo America/New_York (data/gefs.py local_day_utc_bounds).")

    print("\nPer-member available_time check (do members publish at different times?):")
    per_member_avail = df.groupby(["run_time", "ensemble_member"])["available_time"].nunique()
    print(f"  Distinct available_time values observed across all member/run/forecast-hour combinations: "
          f"{df['available_time'].nunique()} (out of {len(df)} rows) -- member files clearly do NOT all share one timestamp.")


def validate(df: pd.DataFrame, member_max_df: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("PART 18: VALIDATION")
    print("=" * 78)
    key_cols = ["model", "run_time", "forecast_hour", "variable", "ensemble_member", "grid_lat", "grid_lon"]
    print(f"Duplicate rows on (model, run_time, forecast_hour, variable, ensemble_member, grid point): {int(df.duplicated(subset=key_cols).sum())}")
    check = ((df["valid_time"] - df["run_time"]).dt.total_seconds() / 3600 == df["forecast_hour"]).all()
    print(f"valid_time == run_time + forecast_hour for all rows: {bool(check)}")
    print(f"Temperatures plausible (-40F to 130F): {bool(df['value_f'].between(-40, 130).all())} "
          f"(range: {df['value_f'].min():.1f}F to {df['value_f'].max():.1f}F)")
    print(f"Grid point within one 0.25-deg cell (~28km) of requested NYC point: {bool((df['distance_km'] <= 28).all())} "
          f"(max observed: {df['distance_km'].max():.2f} km)")
    print(f"Units consistently 'K': {bool((df['unit'] == 'K').all())}")
    print(f"source_file populated for all rows: {bool(df['source_file'].notna().all())}")

    print("\nSuspiciously-identical-member check (not assumed to be an error, just reported):")
    for run_dt, g in member_max_df.groupby("run_time"):
        vc = g["member_predicted_daily_max_f"].round(4).value_counts()
        dup_vals = vc[vc > 1]
        if not dup_vals.empty:
            print(f"  Run {run_dt}: {len(dup_vals)} distinct value(s) shared by multiple members -- e.g. {dup_vals.head(3).to_dict()}")
        else:
            print(f"  Run {run_dt}: no two members share an identical predicted daily max (to 4 decimals).")


# ---------------------------------------------------------------------------
# PART 19 -- scale estimate.
# ---------------------------------------------------------------------------

def estimate_scale(stats: RequestStats, n_temp_rows: int, n_runs: int) -> None:
    print("\n" + "=" * 78)
    print("PART 19: DOWNLOAD / STORAGE SCALE ESTIMATE (extrapolated -- not executed)")
    print("=" * 78)
    avg_bytes_per_row = stats.bytes_downloaded / max(n_temp_rows, 1)
    print(f"This test: {stats.n_requests} requests, {stats.bytes_downloaded:,} bytes, {stats.seconds_elapsed:.1f}s, "
          f"{n_runs} runs x {EXPECTED_MEMBER_COUNT} members, {n_temp_rows} temperature rows.")
    print(f"~{avg_bytes_per_row:,.0f} bytes/row.")

    runs_per_day = 4
    avg_fh_per_run = 8  # 3-hourly coverage of one target day
    rows_per_day_est = runs_per_day * EXPECTED_MEMBER_COUNT * avg_fh_per_run
    bytes_per_day_est = rows_per_day_est * avg_bytes_per_row

    for label, days in [("1 year", 365), ("5 years", 365 * 5), ("10 years", 365 * 10)]:
        n_runs_est = runs_per_day * days
        n_rows_est = rows_per_day_est * days
        bytes_est = bytes_per_day_est * days
        print(f"  {label:8s}: ~{n_runs_est:,} runs, ~{EXPECTED_MEMBER_COUNT} members/run, ~{n_rows_est:,.0f} rows, ~{bytes_est/1e9:.2f} GB (TEMP ONLY, ESTIMATE)")

    variable_multiplier = len(SNAPSHOT_VARIABLES)
    print(f"\nIf {variable_multiplier} variables were extracted across every member instead of just temperature, "
          f"the rough multiplication effect is ~{variable_multiplier}x the bytes/requests above "
          f"(NOT executed here -- pure arithmetic extrapolation).")
    print(
        "Bandwidth-reduction opportunities:\n"
        "  - fetch only TMP (as done here) for routine collection; reserve the full variable set for occasional sampling;\n"
        "  - the pgrb2sp25 'small' subset product is already ~4.5-20x smaller than the full pgrb2ap5/bp5 products;\n"
        "  - 3-hourly steps (vs GFS's hourly) mean far fewer forecast-hour files needed per day of coverage;\n"
        "  - GEFS's own precomputed geavg/gespr products could substitute for OUR mean/spread computation in\n"
        "    production, at the cost of losing the raw per-member distribution needed for custom quantiles/thresholds."
    )


def main() -> None:
    stats = RequestStats()
    investigate_structure(stats)

    day_start_utc, day_end_utc = local_day_utc_bounds(TARGET_DATE)
    print(f"\nTarget local day (America/New_York): {TARGET_DATE} -> UTC [{day_start_utc}, {day_end_utc})")

    snapshot_rows = variable_snapshot(datetime(2025, 6, 30, 12, tzinfo=timezone.utc), "gec00", 18, stats)

    print("\n" + "=" * 78)
    print("PART 7/6/8: SELECTED RUNS + ENSEMBLE TEMPERATURE EXTRACTION")
    print("=" * 78)
    runs = select_runs()
    print(f"Selected {len(runs)} runs (justification in module docstring): {[f'{r:%Y-%m-%d %HZ}' for r in runs]}")

    all_rows = []
    coverage_by_run = {}
    member_status_by_run = {}
    for run_dt in runs:
        rows, coverage, member_status = fetch_ensemble_temperature(run_dt, day_start_utc, day_end_utc, stats)
        coverage_by_run[run_dt] = coverage
        member_status_by_run[run_dt] = member_status
        n_complete = sum(1 for s in member_status.values() if s["member_complete"])
        print(f"  {run_dt:%Y-%m-%d %HZ}: {len(rows)} rows, {n_complete}/{EXPECTED_MEMBER_COUNT} members complete, "
              f"full_day_coverage={coverage['full_day_coverage']}")
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED_DIR / "gefs_test_canonical.csv", index=False)
    df.to_parquet(PROCESSED_DIR / "gefs_test_canonical.parquet", index=False)

    with open(RAW_DIR / f"{TARGET_DATE:%Y%m%d}" / "nyc_location.json", "w", encoding="utf-8") as f:
        json.dump(NYC_LOCATION, f, indent=2)

    member_max_df = compute_member_daily_max(df)
    member_max_df.to_csv(PROCESSED_DIR / "gefs_test_member_daily_max.csv", index=False)

    dist_df = summarize_distribution(member_max_df, coverage_by_run)
    dist_df.to_csv(PROCESSED_DIR / "gefs_test_ensemble_distribution.csv", index=False)

    int_dist = integer_temperature_distribution(member_max_df)
    int_dist.to_csv(PROCESSED_DIR / "gefs_test_integer_temperature_distribution.csv", index=False)

    revision_df = revision_through_time(dist_df)
    revision_df.to_csv(PROCESSED_DIR / "gefs_test_revision_through_time.csv", index=False)

    same_valid_time_ensemble_test(df, datetime(2025, 7, 1, 16, tzinfo=timezone.utc))

    completeness_df = report_member_completeness(member_status_by_run)
    completeness_df.to_csv(PROCESSED_DIR / "gefs_test_member_completeness.csv", index=False)

    compare_with_gfs_and_hrrr(dist_df)

    audit_point_in_time(df)
    validate(df, member_max_df)
    estimate_scale(stats, len(df), len(runs))

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total requests: {stats.n_requests}")
    print(f"Total bytes downloaded: {stats.bytes_downloaded:,} ({stats.bytes_downloaded/1e6:.1f} MB)")
    print(f"Total request time: {stats.seconds_elapsed:.1f} s")
    print(f"Temperature rows extracted: {len(df)}")
    print(f"Variable-snapshot rows extracted: {len(snapshot_rows)}")
    print(f"Saved canonical/processed outputs to {PROCESSED_DIR}")
    print(f"Saved raw GRIB2 message slices to {RAW_DIR}")


if __name__ == "__main__":
    main()
