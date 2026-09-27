"""Driver for the point-in-time weather_state architecture task.

Uses ONLY already-collected test data (no downloads). Produces:
  data/processed/weather/state/test/event_timeline.parquet
  data/processed/weather/state/test/representative_states.json
  data/processed/weather/state/test/state_audit.json
  data/processed/weather/state/test/schema_inventory.csv
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import weather_state as ws

OUT_DIR = Path(__file__).resolve().parents[1] / "data/processed/weather/state/test"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def json_default(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, timedelta):
        return o.total_seconds()
    if isinstance(o, pd.Timedelta):
        return o.total_seconds()
    if pd.isna(o):
        return None
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def main():
    print("Loading sources...")
    sources = ws.load_all_sources()

    print("Part 1: schema inventory")
    schema_df = ws.inspect_schemas()
    schema_df.drop(columns=["columns"]).to_csv(OUT_DIR / "schema_inventory.csv", index=False)
    print(schema_df.drop(columns=["columns"]).to_string())

    print("\nPart 4/5: building event timeline...")
    events = ws.build_event_timeline(sources, policy=ws.PROXY_STATE)
    events_july1 = ws.filter_timeline_to_july1(events)
    events_july1.to_parquet(OUT_DIR / "event_timeline.parquet", index=False)
    print(f"Total events (all time, resolvable): {len(events)}")
    print(f"Events relevant to July 1 (event_time <= end of local July 1): {len(events_july1)}")
    print(events_july1["source"].value_counts())
    print("\nEvent timeline (July 1 relevant):")
    print(events_july1[["event_time", "event_type", "source", "run_time", "station_id", "product_id"]].to_string())

    print("\nPart 27: usable-event timeline (model-relevant events only)...")
    usable_relevant_types = [t for t in events_july1["event_type"].unique() if t.endswith("_USABLE_UPDATE")]
    usable_timeline = events_july1[
        events_july1["event_type"].isin(usable_relevant_types + ["OBSERVATION", "AFD_UPDATE"])
    ].copy()
    usable_timeline.to_parquet(OUT_DIR / "usable_event_timeline.parquet", index=False)
    print(f"Usable/observation/AFD events: {len(usable_timeline)} (vs {len(events_july1)} total raw+usable+obs+afd events)")

    print("\nPart 10: reproducing the previously-problematic HRRR transition...")
    hrrr_usable = events_july1[(events_july1["source"] == "hrrr") & (events_july1["event_type"] == "HRRR_USABLE_UPDATE")].sort_values("run_time")
    hrrr_raw = events_july1[(events_july1["source"] == "hrrr") & (events_july1["event_type"] == "HRRR_UPDATE")].sort_values("run_time")
    demo_run_time = pd.Timestamp("2025-07-01T02:00:00+00:00")
    raw_row = hrrr_raw[hrrr_raw["run_time"] == demo_run_time]
    usable_row = hrrr_usable[hrrr_usable["run_time"] == demo_run_time]
    hrrr_transition_demo = {}
    if not raw_row.empty and not usable_row.empty:
        raw_t = raw_row.iloc[0]["event_time"]
        usable_t = usable_row.iloc[0]["event_time"]
        checkpoints = {
            "before_02z_run_arrives": raw_t - timedelta(minutes=5),
            "just_after_02z_raw_arrival_still_incomplete": raw_t + timedelta(minutes=1),
            "mid_arrival_still_incomplete": raw_t + (usable_t - raw_t) / 2,
            "just_after_02z_becomes_usable": usable_t + timedelta(minutes=1),
        }
        for label, t in checkpoints.items():
            t = t.to_pydatetime()
            state = ws.get_weather_state(sources, t, policy=ws.PROXY_STATE)
            h = state["hrrr"]
            hrrr_transition_demo[label] = {
                "state_time": t,
                "latest_seen_run_time": h["latest_seen_run"]["run_time"] if h["latest_seen_run"] else None,
                "latest_seen_run_status": h["latest_seen_run"]["run_status"] if h["latest_seen_run"] else None,
                "latest_seen_run_coverage_pct": h["latest_seen_run"]["coverage_pct"] if h["latest_seen_run"] else None,
                "latest_seen_run_raw_predicted_max_f_NEVER_USED": h["latest_seen_run"]["predicted_daily_max_f"] if h["latest_seen_run"] else None,
                "latest_usable_run_time": h["latest_usable_run"]["run_time"] if h["latest_usable_run"] else None,
                "predicted_daily_max_f_MODEL_FACING": h["latest_usable_run"]["predicted_daily_max_f"] if h["latest_usable_run"] else None,
                "newer_run_arriving": h["newer_run_arriving"],
            }
            audit = ws.validate_no_lookahead(state)
            hrrr_transition_demo[label]["no_lookahead_audit"] = audit["overall"]
        print("HRRR 02Z-run transition demonstration (BEFORE FIX this run's incomplete arrival produced a false")
        print("77.2F daily-max the instant it started appearing; AFTER FIX the model-facing value stays on the")
        print("previous complete 01Z run's 87.5F until the 02Z run is fully complete):")
        for label, d in hrrr_transition_demo.items():
            print(f"  {label}: seen_run={d['latest_seen_run_time']} status={d['latest_seen_run_status']} "
                  f"cov={d['latest_seen_run_coverage_pct']} raw_max(never_used)={d['latest_seen_run_raw_predicted_max_f_NEVER_USED']} "
                  f"| MODEL-FACING max={d['predicted_daily_max_f_MODEL_FACING']} (from usable run {d['latest_usable_run_time']}) "
                  f"audit={d['no_lookahead_audit']}")
    with open(OUT_DIR / "hrrr_transition_demo.json", "w") as f:
        json.dump(hrrr_transition_demo, f, default=json_default, indent=2)

    print("\nPart 15: representative states...")
    ny_tz = ws.NYC_TZ
    rep_times_local = [6, 8, 10, 12, 14, 16]
    rep_states = {}
    audits = {}

    for hr in rep_times_local:
        t_local = datetime(2025, 7, 1, hr, 0, tzinfo=ny_tz)
        t_utc = t_local.astimezone(timezone.utc)
        label = f"{hr:02d}00_ET"
        state = ws.get_weather_state(sources, t_utc, policy=ws.PROXY_STATE)
        rep_states[label] = state
        audits[label] = ws.validate_no_lookahead(state)

    # Before/after states around real information events found in the July-1 timeline.
    bracket_targets = []
    for evt_type, label_prefix in [("HRRR_UPDATE", "hrrr"), ("AFD_UPDATE", "afd"), ("OBSERVATION", "obs")]:
        subset = events_july1[events_july1["event_type"] == evt_type]
        if not subset.empty:
            mid_row = subset.iloc[len(subset) // 2]
            bracket_targets.append((label_prefix, mid_row["event_time"]))

    for label_prefix, evt_time in bracket_targets:
        before_t = (evt_time - timedelta(minutes=1)).to_pydatetime()
        after_t = (evt_time + timedelta(minutes=1)).to_pydatetime()
        before_state = ws.get_weather_state(sources, before_t, policy=ws.PROXY_STATE)
        after_state = ws.get_weather_state(sources, after_t, policy=ws.PROXY_STATE)
        rep_states[f"{label_prefix}_event_before"] = before_state
        rep_states[f"{label_prefix}_event_after"] = after_state
        audits[f"{label_prefix}_event_before"] = ws.validate_no_lookahead(before_state)
        audits[f"{label_prefix}_event_after"] = ws.validate_no_lookahead(after_state)

    with open(OUT_DIR / "representative_states.json", "w") as f:
        json.dump(rep_states, f, default=json_default, indent=2)

    with open(OUT_DIR / "state_audit.json", "w") as f:
        json.dump(audits, f, default=json_default, indent=2)

    print("\nPart 4/7: completeness audit across representative states...")
    completeness_rows = []
    for label, state in rep_states.items():
        for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic", "gefs", "ecmwf_ensemble"]:
            s = state.get(key, {})
            seen = s.get("latest_seen_run")
            usable = s.get("latest_usable_run")
            completeness_rows.append(
                {
                    "state_label": label,
                    "source": key,
                    "usable_for_daily_max": s.get("usable_for_daily_max"),
                    "newer_run_arriving": s.get("newer_run_arriving"),
                    "latest_seen_run_time": seen["run_time"] if seen else None,
                    "latest_seen_run_status": seen.get("run_status") if seen else None,
                    "latest_seen_coverage_pct": seen.get("coverage_pct") if seen else None,
                    "latest_usable_run_time": usable["run_time"] if usable else None,
                    "completeness_label": state.get(key, {}).get("completeness"),
                }
            )
    completeness_df = pd.DataFrame(completeness_rows)
    with open(OUT_DIR / "completeness_audit.json", "w") as f:
        json.dump(completeness_rows, f, default=json_default, indent=2)
    print(completeness_df.to_string())

    print("\nPart 16: no-lookahead audit results:")
    for label, audit in audits.items():
        print(f"  {label}: {audit['overall']}")
        if audit["overall"] == "FAIL":
            for check, result in audit["checks"].items():
                if result == "FAIL":
                    print(f"    FAILED CHECK: {check}")

    # Also run STRICT_STATE for comparison at one representative time.
    t_utc = datetime(2025, 7, 1, 12, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    strict_state = ws.get_weather_state(sources, t_utc, policy=ws.STRICT_STATE)
    strict_audit = ws.validate_no_lookahead(strict_state)
    with open(OUT_DIR / "strict_state_1200ET_example.json", "w") as f:
        json.dump({"state": strict_state, "audit": strict_audit}, f, default=json_default, indent=2)
    print(f"\nSTRICT_STATE example at 12:00 ET -- observations available: {strict_state['observations']['stations']}")
    print(f"STRICT_STATE example at 12:00 ET -- AFD available: {strict_state['afd'].get('available')}")
    print(f"STRICT_STATE audit: {strict_audit['overall']}")

    print("\nDone. Outputs written to", OUT_DIR)


if __name__ == "__main__":
    main()
