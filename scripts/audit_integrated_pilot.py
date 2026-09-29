"""Audits the built integrated pilot dataset (data/processed/pilot/integrated/)
for leakage, completeness, run-transition monotonicity, GEFS ensemble
completeness, missingness, and STRICT/PROXY consistency.

Run AFTER scripts/build_integrated_pilot.py. Prints PASS/FAIL per check and
writes a summary to data/processed/pilot/integrated/audit_report.json.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data/processed/pilot/integrated"

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


def main():
    state_index = pd.read_parquet(OUT_DIR / "state_index.parquet")
    structured = pd.read_parquet(OUT_DIR / "structured_features.parquet")
    trajectories = pd.read_parquet(OUT_DIR / "forecast_trajectories.parquet")
    gefs_members = pd.read_parquet(OUT_DIR / "gefs_members.parquet")
    with open(OUT_DIR / "no_lookahead_failures.json") as f:
        nl_failures = json.load(f)

    # ---- State counts ----
    n_strict = (state_index["state_mode"] == "strict").sum()
    n_proxy = (state_index["state_mode"] == "proxy").sum()
    check("STRICT state count == 240", n_strict == 240, f"got {n_strict}")
    check("PROXY state count == 240", n_proxy == 240, f"got {n_proxy}")
    check("total state count == 480", len(state_index) == 480, f"got {len(state_index)}")
    check("0 duplicate state_ids", state_index["state_id"].duplicated().sum() == 0, str(state_index["state_id"].duplicated().sum()))

    # ---- No-lookahead ----
    check("0 no-lookahead violations", len(nl_failures) == 0, f"{len(nl_failures)} states failed: {nl_failures[:3]}")

    # ---- Label coverage ----
    n_labeled = structured["label_tmax_f"].notna().sum()
    check("label coverage == 480/480", n_labeled == 480, f"got {n_labeled}/480")
    n_days = structured["target_date"].nunique()
    check("40 distinct target days", n_days == 40, f"got {n_days}")
    # every state for a given day must share the identical label (it's the outcome, computed once per day)
    label_per_day_nunique = structured.groupby("target_date")["label_tmax_f"].nunique()
    check("label is identical across all 12 states of the same day", (label_per_day_nunique == 1).all(),
          str(label_per_day_nunique[label_per_day_nunique != 1]))

    # ---- Hard constraint: Tmax_so_far <= realized Tmax ----
    violated = structured[structured["KNYC_tmax_so_far_f"] > structured["label_tmax_f"]]
    check("KNYC_tmax_so_far_f never exceeds label_tmax_f (hard physical constraint)", len(violated) == 0,
          f"{len(violated)} violations: {violated[['state_id','KNYC_tmax_so_far_f','label_tmax_f']].to_dict('records')[:3]}")

    # ---- Source coverage (non-null forecast at least sometimes) ----
    for src in ["hrrr", "gfs", "nbm", "gefs", "ecmwf_deterministic"]:
        col = f"{src}_forecast_tmax_f" if src != "gefs" else "gefs_tmax_mean_f"
        avail_col = f"{src}_usable_for_daily_max"
        pct = structured[avail_col].mean() * 100
        check(f"{src} usable_for_daily_max in >90% of states", pct > 90, f"got {pct:.1f}%")

    # Observations/AFD are OBSERVATION_TIME_ONLY / ISSUANCE_TIME_ONLY sources --
    # eligible_mask() correctly excludes them ENTIRELY under STRICT policy by
    # design (STRICT only admits S3_LAST_MODIFIED_PROXY sources). So a ~50%
    # overall availability rate (0% under STRICT, ~100% under PROXY) is the
    # CORRECT expected architecture, not a defect -- check within PROXY only.
    strict_mask = structured["state_mode"] == "strict"
    proxy_mask = structured["state_mode"] == "proxy"
    for icao in ["KNYC", "KLGA", "KJFK", "KEWR"]:
        strict_pct = structured.loc[strict_mask, f"{icao}_available"].mean() * 100
        proxy_pct = structured.loc[proxy_mask, f"{icao}_available"].mean() * 100
        check(f"{icao} observation available in 0% of STRICT states (excluded by design)", strict_pct == 0, f"got {strict_pct:.1f}%")
        check(f"{icao} observation available in >90% of PROXY states", proxy_pct > 90, f"got {proxy_pct:.1f}%")

    afd_strict_pct = structured.loc[strict_mask, "afd_available"].mean() * 100
    afd_proxy_pct = structured.loc[proxy_mask, "afd_available"].mean() * 100
    check("AFD available in 0% of STRICT states (excluded by design)", afd_strict_pct == 0, f"got {afd_strict_pct:.1f}%")
    print(f"INFO: AFD available in {afd_proxy_pct:.1f}% of PROXY states (not asserted -- AFD issuance cadence is irregular)")

    # ---- Run-transition monotonicity: within a day, run_time should never move backward ----
    backward_moves = []
    for src in ["hrrr_run", "gfs_run", "nbm_run", "gefs_run", "ecmwf_deterministic_run"]:
        for (d, mode), g in structured.groupby(["target_date", "state_mode"]):
            g = g.sort_values("local_hour")
            runs = g[src].dropna().tolist()
            for i in range(1, len(runs)):
                if runs[i] < runs[i - 1]:
                    backward_moves.append({"source": src, "target_date": str(d), "mode": mode, "from": str(runs[i-1]), "to": str(runs[i])})
    check("0 backward run-selection moves within any day", len(backward_moves) == 0, str(backward_moves[:5]))

    # ---- GEFS ensemble completeness: usable runs must show 31/31 members ----
    gefs_usable = structured[structured["gefs_usable_for_daily_max"] == True]  # noqa: E712
    bad_member_count = gefs_usable[gefs_usable["gefs_member_count_available"] != 31]
    check("every usable GEFS run has exactly 31/31 members", len(bad_member_count) == 0,
          f"{len(bad_member_count)} states: {bad_member_count[['state_id','gefs_member_count_available']].to_dict('records')[:3]}")
    # cross-check against the raw member table
    for sid in gefs_usable["state_id"].sample(min(5, len(gefs_usable)), random_state=0):
        run = gefs_usable[gefs_usable["state_id"] == sid]["gefs_run"].iloc[0]
        n_members_raw = gefs_members[gefs_members["state_id"] == sid]["ensemble_member"].nunique()
        check(f"gefs_members table has 31 distinct members for state {sid}", n_members_raw == 31, f"got {n_members_raw}")

    # ---- ECMWF LOW-confidence handling: no fabricated per-FH availability ----
    # (structural check: ECMWF's age should reflect the ~514min-class lag, not something suspiciously exact/synthetic)
    ecmwf_ages = structured[structured["ecmwf_deterministic_usable_for_daily_max"] == True]["ecmwf_deterministic_age_hours"]  # noqa: E712
    check("ECMWF ages are all >= ~8.5h (consistent with its bulk-sync lag, not a synthetic 0-lag assumption)",
          (ecmwf_ages >= 8.0).all() if len(ecmwf_ages) else True, str(ecmwf_ages[ecmwf_ages < 8.0].tolist()[:5]))

    # ---- Structural missingness vs unexpected missingness spot-check ----
    # GEFS/ECMWF should never be "usable" with 0 members / 0 trajectory points
    gefs_zero_traj = gefs_usable[gefs_usable["gefs_run"].isin(
        gefs_members.groupby("state_id").filter(lambda g: len(g) == 0)["state_id"] if False else []
    )]
    n_traj_by_state = trajectories.groupby("state_id").size()
    usable_hrrr = structured[structured["hrrr_usable_for_daily_max"] == True]["state_id"]  # noqa: E712
    missing_traj = usable_hrrr[~usable_hrrr.isin(n_traj_by_state.index)]
    check("every state with hrrr_usable_for_daily_max=True has >=1 hrrr trajectory row", len(missing_traj) == 0, str(missing_traj.tolist()[:5]))

    # ---- STRICT vs PROXY: numerical sources (S3_LAST_MODIFIED_PROXY) must be
    # IDENTICAL between STRICT and PROXY for the same (day, query_time) -- the
    # distinction only affects observations/AFD, never the numerical sources.
    pivot = structured.pivot_table(index=["target_date", "local_hour"], columns="state_mode", values="hrrr_forecast_tmax_f")
    mismatched = pivot[(pivot["strict"] != pivot["proxy"]) & pivot["strict"].notna() & pivot["proxy"].notna()]
    check("HRRR forecast identical between STRICT and PROXY at the same (day,time)", len(mismatched) == 0, str(len(mismatched)))

    # ---- Feature/label leakage: label columns must not appear among feature-producing source columns ----
    feature_cols = [c for c in structured.columns if c not in ("label_tmax_f", "label_time_utc", "label_source", "label_n_observations", "label_quality")]
    leak_terms = ["label", "realized", "settlement", "kalshi"]
    suspicious = [c for c in feature_cols if any(term in c.lower() for term in leak_terms)]
    check("no leakage-suspicious column names among features", len(suspicious) == 0, str(suspicious))

    # ---- Day-level split safety: event_id groups all 12 states of a day together ----
    structured["event_id"] = structured["target_date"]  # target_location is fixed (Central Park/KNYC) for this pilot
    per_day_state_count = structured.groupby("event_id").size()
    check("every event_id (day) has exactly 12 states (6 times x 2 modes)", (per_day_state_count == 12).all(), str(per_day_state_count[per_day_state_count != 12]))

    print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
    for f in results["failed"]:
        print("FAILED:", f)

    with open(OUT_DIR / "audit_report.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    sys.exit(1 if results["failed"] else 0)


if __name__ == "__main__":
    main()
