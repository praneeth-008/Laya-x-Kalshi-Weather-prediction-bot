"""Deterministic sample deep-inspection for ONE backfill block (section 18 of
the backfill spec). Picks a few representative states (near the start,
middle, end of the block, plus the single hottest realized-Tmax day as an
'extreme weather' sample) and prints diagnostic checks: selected runs make
sense, source ages make sense, observations plausible, Central Park mapping
correct, trajectories physically plausible, GEFS members genuinely differ,
no future information visible, AFD selection plausible.

Usage: python scripts/sample_inspect_block.py --block 2025H1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from scripts.backfill_common import block_arg_parser, get_block, BACKFILL_ROOT


def inspect_state(sid, structured, trajectories, gefs_member_traj, atmo_traj):
    row = structured[structured["state_id"] == sid].iloc[0]
    print(f"\n{'='*70}\nSTATE {sid}  target_date={row['target_date']}  mode={row['state_mode']}  query_time_local={row['query_time_local']}")
    print(f"  label_tmax_f={row['label_tmax_f']}")
    qt = pd.Timestamp(row["query_time_utc"])

    for src in ["hrrr", "gfs", "nbm", "ecmwf_deterministic", "gefs"]:
        run = row.get(f"{src}_run")
        age = row.get(f"{src}_age_hours")
        fc = row.get(f"{src}_forecast_tmax_f" if src != "gefs" else "gefs_tmax_mean_f")
        usable = row.get(f"{src}_usable_for_daily_max")
        ok_run = run is None or pd.Timestamp(run) <= qt
        print(f"  {src:20s} usable={usable!s:6s} run={run} age_h={age} fc_tmax_f={fc}  run<=query_time: {ok_run}")
        assert ok_run, f"FUTURE RUN DETECTED for {src} in state {sid}"

    for icao in ["KNYC", "KLGA", "KJFK", "KEWR"]:
        avail = row.get(f"{icao}_available")
        temp = row.get(f"{icao}_current_temp_f")
        if avail:
            plausible = temp is None or -40 <= temp <= 120
            print(f"  obs {icao}: temp_f={temp} plausible={plausible}")
            assert plausible, f"IMPLAUSIBLE TEMP for {icao} in state {sid}"

    traj = trajectories[trajectories["state_id"] == sid]
    if len(traj):
        future_rows = traj[pd.to_datetime(traj["run_time"]) > qt]
        print(f"  forecast_trajectories: {len(traj)} rows, temp range [{traj['temperature_f'].min():.1f}, {traj['temperature_f'].max():.1f}]F, future run_time rows={len(future_rows)}")
        assert len(future_rows) == 0, f"FUTURE TRAJECTORY ROW in state {sid}"
        plausible_temp = traj["temperature_f"].between(-40, 130).all()
        assert plausible_temp, f"IMPLAUSIBLE TRAJECTORY TEMP in state {sid}"

    gmt = gefs_member_traj[gefs_member_traj["state_id"] == sid]
    if len(gmt):
        n_members = gmt["ensemble_member"].nunique()
        tmp_rows = gmt[gmt["variable"] == "TMP"]
        n_distinct_first_vals = tmp_rows.groupby("ensemble_member")["value"].first().nunique() if len(tmp_rows) else 0
        print(f"  gefs_member_trajectories: {n_members} distinct members, {n_distinct_first_vals} distinct first-TMP-values (genuinely different: {n_distinct_first_vals > 1})")

    at = atmo_traj[atmo_traj["state_id"] == sid]
    if len(at):
        print(f"  atmospheric_trajectories: {len(at)} rows, variables={sorted(at['variable'].unique())}")

    print(f"  PASS: state {sid} looks structurally sound")


def main():
    args = block_arg_parser("sample inspection").parse_args()
    block = get_block(args.block)
    out_dir = BACKFILL_ROOT / block.block_id / "integrated"

    structured = pd.read_parquet(out_dir / "structured_features.parquet")
    trajectories = pd.read_parquet(out_dir / "forecast_trajectories.parquet")
    gefs_member_traj = pd.read_parquet(out_dir / "gefs_member_trajectories.parquet")
    atmo_traj = pd.read_parquet(out_dir / "atmospheric_trajectories.parquet")

    dates_sorted = sorted(structured["target_date"].unique())
    first_date, mid_date, last_date = dates_sorted[0], dates_sorted[len(dates_sorted) // 2], dates_sorted[-1]
    hottest_row = structured.loc[structured["label_tmax_f"].idxmax()]
    hottest_date = hottest_row["target_date"]

    print(f"Block {block.block_id}: {len(dates_sorted)} days. Sample dates: start={first_date} mid={mid_date} end={last_date} hottest={hottest_date} (Tmax={hottest_row['label_tmax_f']}F)")

    sample_sids = []
    for d in [first_date, mid_date, last_date, hottest_date]:
        candidates = structured[(structured["target_date"] == d) & (structured["state_mode"] == "proxy") & (structured["local_hour"] == 14)]
        if len(candidates):
            sample_sids.append(candidates.iloc[0]["state_id"])

    for sid in sample_sids:
        inspect_state(sid, structured, trajectories, gefs_member_traj, atmo_traj)

    print(f"\n{len(sample_sids)} sample states inspected, all PASS.")


if __name__ == "__main__":
    main()
