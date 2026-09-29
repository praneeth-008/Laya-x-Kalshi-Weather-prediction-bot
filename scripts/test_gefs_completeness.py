"""GEFS ensemble completeness/no-lookahead regression tests -- validates
data/weather_state.py's assess_ensemble_run_completeness()/get_ensemble_state()
against the GEFS-specific conservative complete-member rule (see
docs/gefs_pilot_readiness.md sections 9-10). Originally developed and run as
a scratch script during the GEFS pilot's production-readiness phase;
promoted into the repository here so it is part of the reproducible,
committed test baseline before historical backfill begins.

Usage:
    python scripts/test_gefs_completeness.py
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import data.weather_state as ws
from data.gefs import MEMBERS, EXPECTED_MEMBER_COUNT

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


TARGET_DATE = ws.TARGET_LOCAL_DATE
day_start_utc, day_end_utc = ws.local_day_utc_bounds(TARGET_DATE)
print(f"Target day (NYC local {TARGET_DATE}) UTC bounds: [{day_start_utc}, {day_end_utc})\n")

# ---- Schedule sanity: matches the validated real-archive 3-hourly F0-F240 ----
candidates = ws._forecast_hour_candidates("gefs", datetime(2025, 6, 30, 0, tzinfo=timezone.utc))
check("schedule is 3-hourly F0-F240 (81 points)", candidates == list(range(0, 241, 3)), f"got {len(candidates)} points")

run_time = datetime(2025, 6, 30, 0, tzinfo=timezone.utc)  # 1 day before target -- can fully cover 2025-07-01
expected_vts = ws.expected_target_day_valid_times("gefs", run_time, day_start_utc, day_end_utc)
print(f"Expected target-day valid times for 00Z run covering {TARGET_DATE}: {len(expected_vts)} -> {expected_vts}\n")


def build_rows(members, valid_times):
    rows = []
    for m in members:
        for vt in valid_times:
            rows.append({
                "run_time": pd.Timestamp(run_time), "valid_time": pd.Timestamp(vt),
                "ensemble_member": m, "value_f": 75.0,
                "forecast_hour": int((vt - run_time).total_seconds() / 3600),
                "available_time_resolved": pd.Timestamp(run_time) + timedelta(hours=6),
            })
    return pd.DataFrame(rows)


# ---- A. all expected members/times present -> COMPLETE ----
complete_rows = build_rows(MEMBERS, expected_vts)
result_a = ws.assess_ensemble_run_completeness("gefs", run_time, complete_rows, day_start_utc, day_end_utc, EXPECTED_MEMBER_COUNT)
check("A. all members+times present -> COMPLETE", result_a["run_status"] == "COMPLETE", str(result_a))
check("A. usable_for_daily_max True", result_a["usable_for_daily_max"] is True)

# ---- B. one member missing entirely -> INCOMPLETE ----
rows_b = build_rows(MEMBERS[:-1], expected_vts)  # drop last member entirely
result_b = ws.assess_ensemble_run_completeness("gefs", run_time, rows_b, day_start_utc, day_end_utc, EXPECTED_MEMBER_COUNT)
check("B. one member missing -> INCOMPLETE", result_b["run_status"] == "INCOMPLETE", str(result_b))
check("B. member_count_available == 30", result_b["member_count_available"] == 30, str(result_b))
check("B. usable_for_daily_max False", result_b["usable_for_daily_max"] is False)

# ---- C. one required valid_time missing (for ALL members) -> INCOMPLETE ----
rows_c = build_rows(MEMBERS, expected_vts[:-1])  # every member missing the same last valid_time
result_c = ws.assess_ensemble_run_completeness("gefs", run_time, rows_c, day_start_utc, day_end_utc, EXPECTED_MEMBER_COUNT)
check("C. one valid_time missing across all members -> INCOMPLETE", result_c["run_status"] == "INCOMPLETE", str(result_c))
check("C. member_count_available still 31 (members present, just incomplete each)", result_c["member_count_available"] == 31, str(result_c))
check("C. forecast_points_available == expected-1", result_c["forecast_points_available"] == len(expected_vts) - 1, str(result_c))

# ---- D/E. newer partially-arriving run does not replace older complete run; latest_seen_run advances independently ----
older_run = datetime(2025, 6, 30, 6, tzinfo=timezone.utc)
newer_run = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
older_expected = ws.expected_target_day_valid_times("gefs", older_run, day_start_utc, day_end_utc)
newer_expected = ws.expected_target_day_valid_times("gefs", newer_run, day_start_utc, day_end_utc)

rows = []
for m in MEMBERS:  # older run: FULLY complete (all members, all valid times)
    for vt in older_expected:
        rows.append({"run_time": pd.Timestamp(older_run), "valid_time": pd.Timestamp(vt), "ensemble_member": m,
                     "value_f": 74.0, "forecast_hour": int((vt - older_run).total_seconds() / 3600),
                     "available_time_resolved": pd.Timestamp(older_run) + timedelta(hours=6)})
for m in MEMBERS[:5]:  # newer run: only 5 of 31 members have arrived so far (incomplete)
    for vt in newer_expected:
        rows.append({"run_time": pd.Timestamp(newer_run), "valid_time": pd.Timestamp(vt), "ensemble_member": m,
                     "value_f": 76.0, "forecast_hour": int((vt - newer_run).total_seconds() / 3600),
                     "available_time_resolved": pd.Timestamp(newer_run) + timedelta(hours=6)})
df = pd.DataFrame(rows)
df["availability_status"] = "S3_LAST_MODIFIED_PROXY"
df["source_name"] = "gefs"

query_t = pd.Timestamp(newer_run) + timedelta(hours=8)
state = ws.get_ensemble_state(df, query_t, "strict", expected_members=EXPECTED_MEMBER_COUNT, source_name="gefs")

print("\nlatest_seen_run:", state["latest_seen_run"]["run_time"] if state.get("latest_seen_run") else None)
print("latest_usable_run:", state["latest_usable_run"]["run_time"] if state.get("latest_usable_run") else None)

check("D. newer partial-member run does NOT become latest_usable_run",
      state["latest_usable_run"] is not None and state["latest_usable_run"]["run_time"] == pd.Timestamp(older_run),
      f"latest_usable_run={state.get('latest_usable_run')}")
check("E. latest_seen_run advances independently to the newer run",
      state["latest_seen_run"] is not None and state["latest_seen_run"]["run_time"] == pd.Timestamp(newer_run),
      f"latest_seen_run={state.get('latest_seen_run')}")

# ---- F. no-lookahead ----
no_lookahead_ok = True
for meta_key in ["latest_seen_run", "latest_usable_run"]:
    meta = state.get(meta_key)
    if meta and pd.notna(meta.get("run_available_time")):
        if meta["run_available_time"] > query_t:
            no_lookahead_ok = False
check("F. no future information entered the state (available_time <= query_t)", no_lookahead_ok)

# ---- member-preservation sanity: raw member distribution is exposed, not collapsed ----
check("member_daily_max_f dict has per-member entries (not collapsed to summary only)",
      isinstance(state["latest_usable_run"]["member_daily_max_f"], dict) and len(state["latest_usable_run"]["member_daily_max_f"]) == 31,
      str(state["latest_usable_run"]["member_daily_max_f"]))
# NOTE: production text is "...are NOT calibrated probabilities." (capitalized
# NOT, for emphasis) -- this check previously searched for the lowercase
# substring "not calibrated probabilities", which never matched and caused a
# spurious FAIL despite the note being present and correct (case fixed here;
# see docs/decisions.md).
check("raw-frequency-not-probability note present", "NOT calibrated probabilities" in state.get("note", ""))

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
for f in results["failed"]:
    print("FAILED:", f)
sys.exit(1 if results["failed"] else 0)
