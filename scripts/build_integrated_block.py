"""Build the point-in-time integration dataset for ONE HISTORICAL BACKFILL
six-month block, from the frozen, validated block sources (HRRR, GFS, NBM,
GEFS, ECMWF deterministic, observations, AFD).

Generalizes scripts/build_integrated_pilot.py (the approved baseline
8af8d30) from the 40-day pilot sample to a block's full continuous
calendar-day range. EVERY function below (build_state,
extract_structured_features, extract_sequences, extract_atmospheric_trajectory,
extract_gefs_member_trajectory, compute_label) is copied VERBATIM from the
pilot script -- same completeness/eligibility logic (data/weather_state.py,
unmodified), same three/five-layer output architecture, same temperature-
purity guarantee. The ONLY generalization: selected_dates() now returns
every calendar day in the block instead of the 40-day sample, and
load_pilot_sources()/OUT_DIR are replaced with their block-scoped
equivalents (data/backfill_loader.py, data/processed/backfill/{block_id}/).

Usage:
    python scripts/build_integrated_block.py --block 2025H1
"""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data.weather_state as ws
from data.backfill_loader import load_backfill_sources
from scripts.backfill_common import block_arg_parser, get_block, calendar_days, BACKFILL_ROOT

NYC_TZ = ZoneInfo("America/New_York")
STANDARD_LOCAL_HOURS = [6, 8, 10, 12, 14, 16]
STATE_MODES = ["strict", "proxy"]

STATION_IDS = ws.STATION_IDS
STATION_LABELS = ws.STATION_LABELS
KNYC_STATION_ID = "725053-94728"

DET_SOURCES = [("hrrr", "hrrr"), ("gfs", "gfs"), ("nbm", "nbm"), ("ecmwf_deterministic", "ecmwf_det")]
STATE_LOOKBACK = timedelta(days=3)


def local_query_times_utc(target_date) -> list:
    out = []
    for h in STANDARD_LOCAL_HOURS:
        local_dt = datetime(target_date.year, target_date.month, target_date.day, h, 0, tzinfo=NYC_TZ)
        out.append((h, local_dt.astimezone(timezone.utc)))
    return out


def make_state_id(target_date, query_time_utc: pd.Timestamp, state_mode: str) -> str:
    raw = f"{target_date.isoformat()}|{query_time_utc.isoformat()}|{state_mode}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_label(sources, target_date) -> dict:
    day_start_utc, day_end_utc = ws.local_day_utc_bounds(target_date)
    obs = sources.observations
    g = obs[(obs["station_id"] == KNYC_STATION_ID) & (obs["observation_time"] >= day_start_utc) & (obs["observation_time"] < day_end_utc)]
    if g.empty or g["temperature_f"].notna().sum() == 0:
        return {"label_tmax_f": None, "label_source": "KNYC_ISD_observations", "label_n_observations": 0, "label_quality": "NO_DATA"}
    idx = g["temperature_f"].idxmax()
    return {
        "label_tmax_f": float(g.loc[idx, "temperature_f"]),
        "label_time_utc": g.loc[idx, "observation_time"],
        "label_source": "KNYC_ISD_observations",
        "label_n_observations": int(g["temperature_f"].notna().sum()),
        "label_quality": "OK" if g["temperature_f"].notna().sum() >= 12 else "SPARSE",
    }


def _windowed(df: pd.DataFrame, t: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    return df[(df["run_time"] >= pd.Timestamp(t) - STATE_LOOKBACK) & (df["run_time"] <= pd.Timestamp(t))]


def build_state(sources, target_date, query_time_utc: pd.Timestamp, state_mode: str) -> dict:
    policy = ws.STRICT_STATE if state_mode == "strict" else ws.PROXY_STATE
    t = query_time_utc.to_pydatetime() if isinstance(query_time_utc, pd.Timestamp) else query_time_utc

    state = {"state_time": t, "policy": policy, "target_date": target_date}
    for key, attr in DET_SOURCES:
        state[key] = ws.get_latest_forecast(_windowed(getattr(sources, attr), t), t, policy, key, target_date=target_date)
    state["gefs"] = ws.get_ensemble_state(_windowed(sources.gefs, t), t, policy, expected_members=ws.EXPECTED_GEFS_MEMBERS, source_name="gefs", target_date=target_date)
    state["observations"] = ws.get_observation_state(sources.observations, t, policy, STATION_IDS, target_date=target_date)
    state["afd"] = ws.get_afd_state(sources.afd, t, policy)
    state["target_day_progress"] = ws.get_target_day_progress(t, target_date=target_date)

    state["cross_model_diagnostics"] = ws.compute_cross_model_diagnostics(state)
    highest_obs = [
        s["max_temperature_observed_so_far_f"]
        for s in state["observations"]["stations"].values()
        if s.get("available") and s.get("max_temperature_observed_so_far_f") is not None
    ]
    state["target_day_progress"]["highest_observed_temperature_any_station_so_far_f"] = max(highest_obs) if highest_obs else None
    return state


def extract_structured_features(state_id: str, state: dict, label: dict) -> dict:
    row = {
        "state_id": state_id,
        "target_date": state["target_date"].isoformat(),
        "query_time_utc": pd.Timestamp(state["state_time"]),
        "query_time_local": pd.Timestamp(state["state_time"]).tz_convert(NYC_TZ),
        "state_mode": state["policy"],
        "local_hour": pd.Timestamp(state["state_time"]).tz_convert(NYC_TZ).hour,
        "day_of_year": state["target_date"].timetuple().tm_yday,
        "month": state["target_date"].month,
        "hours_until_end_of_local_day": state["target_day_progress"]["hours_until_end_of_local_day"],
        **label,
    }

    for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic"]:
        s = state[key]
        usable = s.get("latest_usable_run")
        seen = s.get("latest_seen_run")
        prefix = key
        row[f"{prefix}_available"] = s.get("available", False)
        row[f"{prefix}_usable_for_daily_max"] = s.get("usable_for_daily_max", False)
        row[f"{prefix}_newer_run_arriving"] = s.get("newer_run_arriving")
        row[f"{prefix}_run"] = usable["run_time"] if usable else None
        row[f"{prefix}_usable_since"] = usable.get("usable_since") if usable else None
        row[f"{prefix}_age_hours"] = usable["age_since_run"].total_seconds() / 3600 if usable else None
        row[f"{prefix}_forecast_tmax_f"] = usable["predicted_daily_max_f"] if usable else None
        row[f"{prefix}_previous_run"] = s.get("previous_usable_run", {}).get("run_time") if s.get("previous_usable_run") else None
        row[f"{prefix}_previous_forecast_tmax_f"] = s.get("previous_usable_run", {}).get("predicted_daily_max_f") if s.get("previous_usable_run") else None
        row[f"{prefix}_revision_f"] = s.get("revision_f")
        row[f"{prefix}_latest_seen_run"] = seen["run_time"] if seen else None
        row[f"{prefix}_latest_seen_status"] = seen["run_status"] if seen else None

    g = state["gefs"]
    gu = g.get("latest_usable_run")
    row["gefs_available"] = g.get("available", False)
    row["gefs_usable_for_daily_max"] = g.get("usable_for_daily_max", False)
    row["gefs_run"] = gu["run_time"] if gu else None
    row["gefs_usable_since"] = gu.get("usable_since") if gu else None
    row["gefs_age_hours"] = gu["age_since_run"].total_seconds() / 3600 if gu else None
    dist = gu.get("distribution") if gu else None
    stat_map = {
        "mean_daily_max_f": "mean_f", "median_daily_max_f": "p50_f", "std_daily_max_f": "std_f",
        "min_daily_max_f": "min_f", "max_daily_max_f": "max_f",
        "p10": "p10_f", "p25": "p25_f", "p75": "p75_f", "p90": "p90_f",
    }
    for src_stat, out_suffix in stat_map.items():
        row[f"gefs_tmax_{out_suffix}"] = dist.get(src_stat) if dist else None
    row["gefs_revision_mean_f"] = g.get("revision", {}).get("mean_daily_max_f") if g.get("revision") else None
    row["gefs_latest_seen_run"] = g["latest_seen_run"]["run_time"] if g.get("latest_seen_run") else None
    row["gefs_member_count_available"] = gu.get("member_count_available") if gu else None

    obs = state["observations"]["stations"]
    for sid, label_name in STATION_LABELS.items():
        s = obs.get(sid, {})
        prefix = label_name
        row[f"{prefix}_available"] = s.get("available", False)
        row[f"{prefix}_current_temp_f"] = s.get("current_temperature_f")
        row[f"{prefix}_current_dewpoint_f"] = s.get("current_dewpoint_f")
        row[f"{prefix}_current_wind_speed_ms"] = s.get("current_wind_speed_ms")
        row[f"{prefix}_current_pressure_hpa"] = s.get("current_station_pressure_hpa")
        row[f"{prefix}_tmax_so_far_f"] = s.get("max_temperature_observed_so_far_f")
        row[f"{prefix}_time_of_tmax_so_far"] = s.get("time_of_max_so_far")
        row[f"{prefix}_observation_age_minutes"] = s["observation_age"].total_seconds() / 60 if s.get("available") else None
    row["knyc_minus_klga_temp_f"] = (
        row["KNYC_current_temp_f"] - row["KLGA_current_temp_f"]
        if row.get("KNYC_current_temp_f") is not None and row.get("KLGA_current_temp_f") is not None else None
    )
    row["knyc_minus_kjfk_temp_f"] = (
        row["KNYC_current_temp_f"] - row["KJFK_current_temp_f"]
        if row.get("KNYC_current_temp_f") is not None and row.get("KJFK_current_temp_f") is not None else None
    )
    row["knyc_minus_kewr_temp_f"] = (
        row["KNYC_current_temp_f"] - row["KEWR_current_temp_f"]
        if row.get("KNYC_current_temp_f") is not None and row.get("KEWR_current_temp_f") is not None else None
    )

    cmd = state["cross_model_diagnostics"]
    row["cross_model_forecast_count"] = cmd.get("forecast_count")
    row["cross_model_mean_tmax_f"] = cmd.get("forecast_mean_f")
    row["cross_model_median_tmax_f"] = cmd.get("forecast_median_f")
    row["cross_model_std_tmax_f"] = cmd.get("forecast_std_f")
    row["cross_model_range_tmax_f"] = cmd.get("forecast_range_f")

    afd = state["afd"]
    row["afd_available"] = afd.get("available", False)
    row["afd_issuance_time"] = afd.get("issuance_time")
    row["afd_age_hours"] = afd["afd_age"].total_seconds() / 3600 if afd.get("available") else None
    row["afd_product_id"] = afd.get("latest_product_id")

    return row


def extract_sequences(state_id: str, state: dict) -> tuple[list, list]:
    traj_rows = []
    for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic"]:
        s = state[key]
        usable = s.get("latest_usable_run")
        if not usable:
            continue
        for point in usable["target_day_temperature_path"]:
            traj_rows.append({
                "state_id": state_id, "source": key, "run_time": usable["run_time"],
                "valid_time": point["valid_time"], "forecast_hour": point["forecast_hour"],
                "temperature_f": point["value_f"],
            })

    gefs_rows = []
    gu = state["gefs"].get("latest_usable_run")
    if gu:
        for member, tmax in gu.get("member_daily_max_f", {}).items():
            gefs_rows.append({"state_id": state_id, "run_time": gu["run_time"], "ensemble_member": member, "member_daily_max_f": tmax})

    return traj_rows, gefs_rows


def extract_atmospheric_trajectory(state_id: str, state: dict, sources, t: datetime, policy: str) -> list:
    rows = []
    for key, attr in DET_SOURCES:
        usable = state[key].get("latest_usable_run")
        if not usable:
            continue
        run_time = usable["run_time"]
        temp_var = ws.INSTANTANEOUS_TEMPERATURE_VARIABLE.get(key, ws.DEFAULT_INSTANTANEOUS_TEMPERATURE_VARIABLE)
        day_start_utc, day_end_utc = ws.local_day_utc_bounds(state["target_date"])

        raw = getattr(sources, attr)
        if raw.empty:
            continue
        elig = raw[ws.eligible_mask(raw, t, policy)]
        g = elig[(elig["run_time"] == run_time) & (elig["variable"] != temp_var)]
        in_day = g[(g["valid_time"] >= day_start_utc) & (g["valid_time"] < day_end_utc)]

        for _, r in in_day.iterrows():
            rows.append({
                "state_id": state_id, "source": key, "variable": r["variable"], "level": r.get("level"),
                "run_time": run_time, "valid_time": r["valid_time"], "forecast_hour": r["forecast_hour"],
                "value": r["value"], "units": r.get("units"),
                "temporal_stat": r.get("temporal_stat"), "temporal_window_hours": r.get("temporal_window_hours"),
            })
    return rows


def extract_gefs_member_trajectory(state_id: str, state: dict, sources, t: datetime, policy: str) -> list:
    gu = state["gefs"].get("latest_usable_run")
    if not gu:
        return []
    run_time = gu["run_time"]
    day_start_utc, day_end_utc = ws.local_day_utc_bounds(state["target_date"])

    raw = sources.gefs
    if raw.empty:
        return []
    elig = raw[ws.eligible_mask(raw, t, policy)]
    g = elig[elig["run_time"] == run_time]
    in_day = g[(g["valid_time"] >= day_start_utc) & (g["valid_time"] < day_end_utc)]

    rows = []
    for _, r in in_day.iterrows():
        rows.append({
            "state_id": state_id, "run_time": run_time, "ensemble_member": r["ensemble_member"], "member_type": r.get("member_type"),
            "variable": r["variable"], "level": r.get("level"),
            "valid_time": r["valid_time"], "forecast_hour": r["forecast_hour"],
            "value": r["value"], "units": r.get("units"),
            "temporal_stat": r.get("temporal_stat"), "temporal_window_hours": r.get("temporal_window_hours"),
        })
    return rows


def main():
    args = block_arg_parser("integration build").parse_args()
    block = get_block(args.block)
    out_dir = BACKFILL_ROOT / block.block_id / "integrated"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading frozen block sources for {block.block_id}...")
    sources = load_backfill_sources(block.block_id)
    dates = calendar_days(block.start, block.end)
    print(f"{len(dates)} calendar days, {len(STANDARD_LOCAL_HOURS)} query times/day, {len(STATE_MODES)} state modes")

    labels_by_date = {d: compute_label(sources, d) for d in dates}

    state_index_rows, structured_rows, traj_rows_all, gefs_rows_all = [], [], [], []
    atmo_rows_all, gefs_member_traj_rows_all = [], []
    no_lookahead_failures = []

    for d in dates:
        label = labels_by_date[d]
        for local_hour, qt_utc in local_query_times_utc(d):
            for mode in STATE_MODES:
                state = build_state(sources, d, qt_utc, mode)
                sid = make_state_id(d, qt_utc, mode)
                policy = ws.STRICT_STATE if mode == "strict" else ws.PROXY_STATE
                t = qt_utc.to_pydatetime() if isinstance(qt_utc, pd.Timestamp) else qt_utc

                nl = ws.validate_no_lookahead(state)
                nl_pass = nl["overall"] == "PASS"
                if not nl_pass:
                    no_lookahead_failures.append({"state_id": sid, "failures": {k: v for k, v in nl["checks"].items() if v == "FAIL"}})

                state_index_rows.append({
                    "state_id": sid, "target_date": d.isoformat(), "query_time_utc": qt_utc,
                    "query_time_local_hour": local_hour, "state_mode": mode,
                    "label_tmax_f": label["label_tmax_f"], "no_lookahead_pass": nl_pass,
                })
                structured_rows.append(extract_structured_features(sid, state, label))
                traj_rows, gefs_rows = extract_sequences(sid, state)
                traj_rows_all.extend(traj_rows)
                gefs_rows_all.extend(gefs_rows)
                atmo_rows_all.extend(extract_atmospheric_trajectory(sid, state, sources, t, policy))
                gefs_member_traj_rows_all.extend(extract_gefs_member_trajectory(sid, state, sources, t, policy))
        if d.day == 1 or d == dates[-1]:
            print(f"  {d}: done")

    state_index = pd.DataFrame(state_index_rows)
    structured = pd.DataFrame(structured_rows)
    trajectories = pd.DataFrame(traj_rows_all)
    gefs_members = pd.DataFrame(gefs_rows_all)
    atmospheric_trajectories = pd.DataFrame(atmo_rows_all)
    gefs_member_trajectories = pd.DataFrame(gefs_member_traj_rows_all)

    dup_ids = state_index["state_id"].duplicated().sum()
    print(f"\nTotal states: {len(state_index)}  duplicate state_ids: {dup_ids}")
    print(f"no_lookahead failures: {len(no_lookahead_failures)}")

    state_index.to_parquet(out_dir / "state_index.parquet", index=False)
    structured.to_parquet(out_dir / "structured_features.parquet", index=False)
    trajectories.to_parquet(out_dir / "forecast_trajectories.parquet", index=False)
    gefs_members.to_parquet(out_dir / "gefs_members.parquet", index=False)
    atmospheric_trajectories.to_parquet(out_dir / "atmospheric_trajectories.parquet", index=False)
    gefs_member_trajectories.to_parquet(out_dir / "gefs_member_trajectories.parquet", index=False)
    with open(out_dir / "no_lookahead_failures.json", "w") as f:
        json.dump(no_lookahead_failures, f, indent=2, default=str)

    human_cols = [
        "state_id", "target_date", "query_time_local", "state_mode", "label_tmax_f",
        "KNYC_current_temp_f", "KNYC_tmax_so_far_f",
        "hrrr_run", "hrrr_age_hours", "hrrr_forecast_tmax_f",
        "gfs_run", "gfs_age_hours", "gfs_forecast_tmax_f",
        "nbm_run", "nbm_age_hours", "nbm_forecast_tmax_f",
        "gefs_run", "gefs_age_hours", "gefs_tmax_mean_f", "gefs_tmax_std_f", "gefs_tmax_p10_f", "gefs_tmax_p50_f", "gefs_tmax_p90_f",
        "ecmwf_deterministic_run", "ecmwf_deterministic_age_hours", "ecmwf_deterministic_forecast_tmax_f",
        "cross_model_mean_tmax_f", "cross_model_std_tmax_f", "cross_model_range_tmax_f",
        "afd_issuance_time", "afd_age_hours",
    ]
    human = structured[[c for c in human_cols if c in structured.columns]].copy()
    human.to_csv(out_dir / "human_readable_states.csv", index=False)
    human.to_parquet(out_dir / "human_readable_states.parquet", index=False)

    print(f"\nWrote outputs to {out_dir}")
    print(f"state_index: {len(state_index)} rows")
    print(f"structured_features: {len(structured)} rows, {len(structured.columns)} columns")
    print(f"forecast_trajectories: {len(trajectories)} rows")
    print(f"gefs_members: {len(gefs_members)} rows")
    print(f"atmospheric_trajectories: {len(atmospheric_trajectories)} rows")
    print(f"gefs_member_trajectories: {len(gefs_member_trajectories)} rows")
    print(f"human_readable_states: {len(human)} rows, {len(human.columns)} columns")


if __name__ == "__main__":
    main()
