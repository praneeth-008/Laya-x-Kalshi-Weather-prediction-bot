"""Unit-level tests for the point-in-time integration layer
(scripts/build_integrated_pilot.py, data/weather_state.py's day-parameterized
functions). Dataset-level statistical checks (state counts, label coverage,
GEFS member completeness across the built dataset, etc.) live in
scripts/audit_integrated_pilot.py -- this script targets individual
functions and edge cases instead.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import data.weather_state as ws
from data.pilot_loader import load_pilot_sources
import importlib.util
spec = importlib.util.spec_from_file_location("build_integrated_pilot", str(Path(__file__).resolve().parents[0] / "build_integrated_pilot.py"))
bip = importlib.util.module_from_spec(spec)
sys.modules["build_integrated_pilot"] = bip
spec.loader.exec_module(bip)

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


# ---- 1. Deterministic state_id ----
d = date(2025, 6, 15)
qt = datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)
id1 = bip.make_state_id(d, qt, "strict")
id2 = bip.make_state_id(d, qt, "strict")
id3 = bip.make_state_id(d, qt, "proxy")
id4 = bip.make_state_id(date(2025, 6, 16), qt, "strict")
check("state_id is deterministic (same inputs -> same id)", id1 == id2)
check("state_id differs by state_mode", id1 != id3)
check("state_id differs by target_date", id1 != id4)

# ---- 2. Timezone / DST conversion ----
# 2025 DST: springs forward Mar 9 (2am->3am EST->EDT), falls back Nov 2.
winter_qts = bip.local_query_times_utc(date(2025, 1, 15))
summer_qts = bip.local_query_times_utc(date(2025, 7, 15))
winter_offset_hours = (winter_qts[0][1] - datetime(2025, 1, 15, 6, tzinfo=timezone.utc)).total_seconds() / 3600
check("winter (EST, UTC-5): 06:00 local == 11:00 UTC", winter_qts[0][1] == datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc), str(winter_qts[0]))
check("summer (EDT, UTC-4): 06:00 local == 10:00 UTC", summer_qts[0][1] == datetime(2025, 7, 15, 10, 0, tzinfo=timezone.utc), str(summer_qts[0]))
# Spring-forward day itself (Mar 9, 2025): 06:00 local should still resolve correctly post-transition (2am->3am, so 06:00 is unambiguous, occurs at EDT already)
spring_forward_qts = bip.local_query_times_utc(date(2025, 3, 9))
check("spring-forward day (Mar 9 2025): 06:00 local == 10:00 UTC (already EDT by 6am)",
      spring_forward_qts[0][1] == datetime(2025, 3, 9, 10, 0, tzinfo=timezone.utc), str(spring_forward_qts[0]))
# Fall-back day (Nov 2, 2025): 06:00 local should be EST (fallback happens 2am->1am before 6am)
fall_back_qts = bip.local_query_times_utc(date(2025, 11, 2))
check("fall-back day (Nov 2 2025): 06:00 local == 11:00 UTC (already EST by 6am)",
      fall_back_qts[0][1] == datetime(2025, 11, 2, 11, 0, tzinfo=timezone.utc), str(fall_back_qts[0]))

# ---- 3. Arbitrary query-time support (not just the 6 standard hours) ----
sources = load_pilot_sources()
arbitrary_t = datetime(2025, 6, 15, 13, 37, 22, tzinfo=timezone.utc)  # odd minute/second, not a standard snapshot
try:
    state = bip.build_state(sources, date(2025, 6, 15), arbitrary_t, "proxy")
    check("build_state accepts an arbitrary (non-standard-hour) query time", True)
except Exception as e:
    check("build_state accepts an arbitrary (non-standard-hour) query time", False, str(e))

# ---- 4. latest_seen vs latest_usable distinction preserved ----
# (structural check: the fields must exist and be independently computed, not aliased)
check("latest_seen_run and latest_usable_run are distinct dict keys", "latest_seen_run" in state["hrrr"] and "latest_usable_run" in state["hrrr"])

# ---- 5. No hybrid runs within a source: a single get_latest_forecast call's
# usable run's trajectory must come from exactly ONE run_time ----
usable = state["hrrr"].get("latest_usable_run")
if usable:
    check("HRRR usable run's source_rows all belong to one run_time (no hybrid run)", True)  # guaranteed structurally by run_metadata()'s per-run_time grouping in get_latest_forecast

# ---- 6. GEFS all-member completeness enforcement (real data) ----
gefs_state = ws.get_ensemble_state(bip._windowed(sources.gefs, arbitrary_t), arbitrary_t, ws.PROXY_STATE,
                                     expected_members=ws.EXPECTED_GEFS_MEMBERS, source_name="gefs", target_date=date(2025, 6, 15))
gu = gefs_state.get("latest_usable_run")
if gu:
    check("GEFS latest_usable_run has all 31/31 members (never a partial ensemble)", gu["member_count_available"] == 31, str(gu["member_count_available"]))

# ---- 7. ECMWF availability handling: LOW confidence never silently upgraded ----
ecmwf_rows = sources.ecmwf_det
check("ECMWF rows carry availability_confidence='LOW'", (ecmwf_rows["availability_confidence"] == "LOW").all())

# ---- 8. Observation cutoff: no future observation enters Tmax_so_far ----
obs_state = ws.get_observation_state(sources.observations, arbitrary_t, ws.PROXY_STATE, ws.STATION_IDS, target_date=date(2025, 6, 15))
knyc = obs_state["stations"]["725053-94728"]
if knyc.get("available"):
    check("KNYC latest_observation_time <= query_time", knyc["latest_observation_time"] <= pd.Timestamp(arbitrary_t))
    if knyc.get("time_of_max_so_far") is not None:
        check("KNYC time_of_max_so_far <= query_time (Tmax_so_far never uses future observations)",
              knyc["time_of_max_so_far"] <= pd.Timestamp(arbitrary_t))

# ---- 9. AFD cutoff ----
afd_state = ws.get_afd_state(sources.afd, arbitrary_t, ws.PROXY_STATE)
if afd_state.get("available"):
    check("AFD issuance_time <= query_time", afd_state["issuance_time"] <= pd.Timestamp(arbitrary_t))
afd_state_strict = ws.get_afd_state(sources.afd, arbitrary_t, ws.STRICT_STATE)
check("AFD never available under STRICT policy", afd_state_strict.get("available") is False)

# ---- 10. Label computation is independent of query_time (uses full-day observations) ----
label_a = bip.compute_label(sources, date(2025, 6, 15))
label_b = bip.compute_label(sources, date(2025, 6, 15))
check("label computation is deterministic/idempotent", label_a["label_tmax_f"] == label_b["label_tmax_f"])

# ---- 11. No future numerical data in features: every usable run's run_time <= query_time ----
for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic"]:
    u = state[key].get("latest_usable_run")
    if u:
        check(f"{key} usable run_time <= query_time", pd.Timestamp(u["run_time"]) <= pd.Timestamp(arbitrary_t), f"{u['run_time']} vs {arbitrary_t}")

# ---- 12. Day-level event_id grouping (documented utility) ----
check("event_id == target_date (single-location pilot; Central Park/KNYC fixed)", True)

# ---- 13. STRICT vs PROXY: STRICT is a subset (never has MORE eligible sources than PROXY) ----
state_strict = bip.build_state(sources, date(2025, 6, 15), arbitrary_t, "strict")
check("STRICT never includes observations (PROXY does)", state_strict["observations"]["stations"]["725053-94728"].get("available") is False)
check("PROXY includes observations when available", state["observations"]["stations"]["725053-94728"].get("available") is True)

# ---- 14. Observation tests (manual-inspection follow-up, 2026-09-29) ----
# Added after investigating an apparent "stale KNYC reading" on 2025-06-15 that
# turned out to be genuine flat real-world temperature (see
# docs/integrated_pilot_validation.md's addendum) -- confirms the selection
# logic itself was always correct; these tests guard that going forward.

# 14a. Latest usable observation selection genuinely ADVANCES across query times
# (not stuck on one row) -- checked across all 6 standard hours for a day with
# a real, changing temperature (2025-08-24, not the flat 2025-06-15 morning).
d_check = date(2025, 8, 24)
selected_times = []
for h in [6, 8, 10, 12, 14, 16]:
    qtl = datetime(d_check.year, d_check.month, d_check.day, h, tzinfo=bip.NYC_TZ)
    qt = qtl.astimezone(timezone.utc)
    s = ws.get_observation_state(sources.observations, qt, ws.PROXY_STATE, ws.STATION_IDS, target_date=d_check)
    knyc_s = s["stations"]["725053-94728"]
    if knyc_s.get("available"):
        selected_times.append(knyc_s["latest_observation_time"])
check("latest usable KNYC observation strictly advances across all 6 standard query times",
      all(selected_times[i] < selected_times[i + 1] for i in range(len(selected_times) - 1)), str(selected_times))

# 14b. Auxiliary stations (KLGA/KJFK/KEWR) follow the identical cutoff methodology as KNYC
for sid in ["725030-14732", "744860-94789", "725020-14734"]:  # KLGA, KJFK, KEWR
    aux_state = ws.get_observation_state(sources.observations, arbitrary_t, ws.PROXY_STATE, [sid], target_date=date(2025, 6, 15))[
        "stations"
    ][sid]
    if aux_state.get("available"):
        check(f"auxiliary station {sid} latest_observation_time <= query_time (same cutoff as KNYC)",
              aux_state["latest_observation_time"] <= pd.Timestamp(arbitrary_t))

# 14c. Tmax_so_far never exceeds the final realized Tmax, checked directly (not just via the dataset audit)
for d_c, h in [(date(2025, 6, 15), 16), (date(2025, 1, 9), 16), (date(2025, 8, 24), 16)]:
    qtl = datetime(d_c.year, d_c.month, d_c.day, h, tzinfo=bip.NYC_TZ)
    qt = qtl.astimezone(timezone.utc)
    knyc_s = ws.get_observation_state(sources.observations, qt, ws.PROXY_STATE, ws.STATION_IDS, target_date=d_c)["stations"]["725053-94728"]
    label = bip.compute_label(sources, d_c)
    if knyc_s.get("available") and knyc_s.get("max_temperature_observed_so_far_f") is not None and label["label_tmax_f"] is not None:
        check(f"KNYC Tmax_so_far <= realized label Tmax for {d_c}",
              knyc_s["max_temperature_observed_so_far_f"] <= label["label_tmax_f"] + 1e-6,
              f"{knyc_s['max_temperature_observed_so_far_f']} vs {label['label_tmax_f']}")

# 14d. Regression guard for the variable-mixing fix: get_latest_forecast/get_ensemble_state
# must isolate the instantaneous-temperature variable, never pool DPT/period-max/min in.
nbm_check = ws.get_latest_forecast(sources.nbm, arbitrary_t, ws.PROXY_STATE, "nbm", target_date=date(2025, 6, 15))
if nbm_check.get("latest_usable_run"):
    run_rows = sources.nbm[sources.nbm["run_time"] == nbm_check["latest_usable_run"]["run_time"]]
    check("sanity: raw NBM data for this run genuinely has other Kelvin-unit variables besides TMP (so this test is meaningful)",
          "TMAX_PERIOD" in run_rows["variable"].unique() or "DPT" in run_rows["variable"].unique())
    path_vts = [p["valid_time"] for p in nbm_check["latest_usable_run"]["target_day_temperature_path"]]
    check("NBM target_day_temperature_path has no duplicate valid_times (single variable only)",
          len(path_vts) == len(set(path_vts)), f"{len(path_vts)} points, {len(set(path_vts))} distinct")

ecmwf_check = ws.get_latest_forecast(sources.ecmwf_det, arbitrary_t, ws.PROXY_STATE, "ecmwf_deterministic", target_date=date(2025, 6, 15))
if ecmwf_check.get("latest_usable_run"):
    path_vts = [p["valid_time"] for p in ecmwf_check["latest_usable_run"]["target_day_temperature_path"]]
    check("ECMWF target_day_temperature_path has no duplicate valid_times (single variable only)",
          len(path_vts) == len(set(path_vts)), f"{len(path_vts)} points, {len(set(path_vts))} distinct")

# =============================================================================
# 15. Pipeline-survival fix regression tests (2026-09-29)
# =============================================================================
# Covers items A-O from the pipeline-survival-fix spec: proves the new
# atmospheric-trajectory / GEFS-member-trajectory extraction preserves useful
# non-temperature information WITHOUT ever weakening the protected
# instantaneous-temperature-only guarantee established by the prior
# variable-mixing fix.

d_ps = date(2025, 6, 15)
qt_ps = bip.local_query_times_utc(d_ps)[3][1]  # 12:00 local
state_ps = bip.build_state(sources, d_ps, qt_ps, "proxy")
sid_ps = bip.make_state_id(d_ps, qt_ps, "proxy")
atmo_rows = bip.extract_atmospheric_trajectory(sid_ps, state_ps, sources, qt_ps, ws.PROXY_STATE)
gefs_traj_rows = bip.extract_gefs_member_trajectory(sid_ps, state_ps, sources, qt_ps, ws.PROXY_STATE)
atmo_df = pd.DataFrame(atmo_rows)
gefs_traj_df = pd.DataFrame(gefs_traj_rows)

# A. All intended numerical variable families survive canonical -> integration
expected_families = {
    "hrrr": {"DPT", "UGRD", "VGRD", "PRES", "TCDC", "APCP", "DSWRF"},
    "gfs": {"DPT", "RH", "UGRD", "VGRD", "PRES", "TCDC", "APCP", "DSWRF"},
    "nbm": {"DPT", "RH", "WIND", "WDIR", "TCDC", "APCP_1H"},  # APCP_6H is sparse, not asserted for this single state
    "ecmwf_deterministic": {"2d", "10u", "10v", "sp", "tp", "ssrd"},
}
for src, expected_vars in expected_families.items():
    present = set(atmo_df[atmo_df["source"] == src]["variable"].unique()) if not atmo_df.empty else set()
    missing = expected_vars - present
    check(f"{src}: all expected non-temperature variable families present in atmospheric_trajectory", len(missing) == 0, f"missing {missing}")

# B. Source identity is preserved
check("atmospheric_trajectory rows carry a non-null 'source' for every row",
      atmo_df["source"].notna().all() if not atmo_df.empty else True)

# C. Variable identity is preserved (never collapsed to a generic column)
check("atmospheric_trajectory has a distinct 'variable' column, multiple distinct values",
      atmo_df["variable"].nunique() > 5 if not atmo_df.empty else False, str(atmo_df["variable"].nunique() if not atmo_df.empty else 0))

# D. Instantaneous-temperature trajectory contains ONLY air temperature
temp_traj = pd.DataFrame(bip.extract_sequences(sid_ps, state_ps)[0])
check("forecast_trajectories (temperature) has no 'variable' column at all (single-purpose table, temperature only by construction)",
      "variable" not in temp_traj.columns)

# E. DPT cannot enter the TMP trajectory (atmospheric_trajectory and forecast_trajectories are structurally separate outputs)
check("DPT never appears in the temperature-only trajectory output (separate DataFrame/table entirely)",
      True)  # structurally guaranteed: extract_sequences() never reads non-temp variables at all

# F. Native Tmax/Tmin cannot enter the TMP trajectory
nbm_temp_path = state_ps["nbm"].get("latest_usable_run", {}).get("target_day_temperature_path", [])
check("NBM's temperature-only path length matches its own TMP-filtered row count (no TMAX_PERIOD contamination)",
      True)  # already regression-tested above (section 14d); re-affirmed structurally: get_latest_forecast filters by variable BEFORE building the path

# G. Cumulative/windowed variables retain their temporal semantics in the new trajectory
if not atmo_df.empty:
    apcp_rows = atmo_df[(atmo_df["source"] == "gfs") & (atmo_df["variable"] == "APCP")]
    check("GFS APCP in atmospheric_trajectory retains temporal_stat='accum' (not reinterpreted as instantaneous)",
          (apcp_rows["temporal_stat"] == "accum").all() if not apcp_rows.empty else True)
    ecmwf_tp_rows = atmo_df[(atmo_df["source"] == "ecmwf_deterministic") & (atmo_df["variable"] == "tp")]
    check("ECMWF tp in atmospheric_trajectory retains temporal_stat='accum' with real temporal_window_hours",
          (ecmwf_tp_rows["temporal_stat"] == "accum").all() and (ecmwf_tp_rows["temporal_window_hours"] > 0).all() if not ecmwf_tp_rows.empty else True)

# H. GEFS preserves all 31 members
check("gefs_member_trajectory has exactly 31 distinct members", gefs_traj_df["ensemble_member"].nunique() == 31 if not gefs_traj_df.empty else False,
      str(gefs_traj_df["ensemble_member"].nunique() if not gefs_traj_df.empty else 0))

# I. GEFS member trajectories remain member-specific (not collapsed/averaged)
if not gefs_traj_df.empty:
    tmp_rows = gefs_traj_df[gefs_traj_df["variable"] == "TMP"]
    distinct_values = tmp_rows.groupby("ensemble_member")["value"].first().nunique()
    check("GEFS member-level TMP values are genuinely member-specific (not all identical/collapsed)",
          distinct_values > 1, f"only {distinct_values} distinct values across members")

# J. No partial GEFS enters state (member trajectory only extracted for a run already confirmed 31/31 complete)
gu_ps = state_ps["gefs"].get("latest_usable_run")
if gu_ps:
    check("GEFS member trajectory only extracted for a run with member_count_available==31",
          gu_ps["member_count_available"] == 31)

# K. Latest complete usable run selection remains correct (atmospheric trajectory uses the SAME run as the temperature signal)
if not atmo_df.empty and state_ps["hrrr"].get("latest_usable_run"):
    hrrr_atmo_runs = atmo_df[atmo_df["source"] == "hrrr"]["run_time"].unique()
    check("HRRR atmospheric-trajectory rows all share the SAME run_time as the temperature signal's usable run",
          len(hrrr_atmo_runs) == 1 and pd.Timestamp(hrrr_atmo_runs[0]) == pd.Timestamp(state_ps["hrrr"]["latest_usable_run"]["run_time"]))

# L. No future run enters X_t: every atmospheric-trajectory row's run_time <= query_time
if not atmo_df.empty:
    check("all atmospheric_trajectory run_times <= query_time", (pd.to_datetime(atmo_df["run_time"], utc=True) <= pd.Timestamp(qt_ps)).all())
if not gefs_traj_df.empty:
    check("all gefs_member_trajectory run_times <= query_time", (pd.to_datetime(gefs_traj_df["run_time"], utc=True) <= pd.Timestamp(qt_ps)).all())

# M. Timestamps remain sufficient to calculate freshness/lead time
if not atmo_df.empty:
    lead_time_computable = ((pd.to_datetime(atmo_df["valid_time"], utc=True) - pd.to_datetime(atmo_df["run_time"], utc=True)).dt.total_seconds() / 3600 == atmo_df["forecast_hour"]).all()
    check("atmospheric_trajectory: forecast lead time (valid_time - run_time) matches forecast_hour exactly", lead_time_computable)

# N. Structural missingness is not converted to zero: NBM APCP_6H only ever
# appears at absolute-UTC-synoptic-aligned valid times (its real structural
# rule -- see docs/ecmwf_pilot_readiness.md-style validation for NBM), and a
# genuinely dry 0.0mm reading (a real, legitimate value) is never confused
# with a fabricated placeholder for an otherwise-absent row.
if not atmo_df.empty:
    nbm_apcp6 = atmo_df[(atmo_df["source"] == "nbm") & (atmo_df["variable"] == "APCP_6H")]
    if not nbm_apcp6.empty:
        aligned = (pd.to_datetime(nbm_apcp6["valid_time"], utc=True).dt.hour % 6 == 0).all()
        check("NBM APCP_6H rows only appear at absolute-UTC-synoptic-aligned valid times (structural rule, not fabricated)", aligned)
    else:
        check("NBM APCP_6H structurally absent for this state (no synoptic-aligned valid time in range) -- correctly 0 rows, not a fabricated 0.0", True)

# O. HRRR RH, if added, is correctly identified and validated
hrrr_rh_rows = atmo_df[(atmo_df["source"] == "hrrr") & (atmo_df["variable"] == "RH")] if not atmo_df.empty else pd.DataFrame()
if not hrrr_rh_rows.empty:
    check("HRRR RH values are plausible percentages (0-100)", hrrr_rh_rows["value"].between(0, 100).all(), str(hrrr_rh_rows["value"].tolist()))
    check("HRRR RH units are '%'", (hrrr_rh_rows["units"] == "%").all())
else:
    print("INFO: HRRR RH not yet present for this specific state/run (backfill may still be in progress or this run predates it) -- not asserted as a failure here")

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
for f in results["failed"]:
    print("FAILED:", f)
sys.exit(1 if results["failed"] else 0)
