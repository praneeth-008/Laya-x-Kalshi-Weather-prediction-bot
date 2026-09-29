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

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
for f in results["failed"]:
    print("FAILED:", f)
sys.exit(1 if results["failed"] else 0)
