"""Driver for the compact model-input compressor (Objective 2).

Reads the same July-1 test data through weather_state(t), then compresses
each state with models.weather_input.build_weather_input(). Produces:
  data/processed/weather/model_input/test/compact_inputs.json
  data/processed/weather/model_input/test/compression_report.json
  data/processed/weather/model_input/test/information_preservation_audit.json

No downloads, no Laya/Jev calls, no training.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import weather_state as ws
from models import weather_input as wi

OUT_DIR = Path(__file__).resolve().parents[1] / "data/processed/weather/model_input/test"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def json_default(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, (timedelta, pd.Timedelta)):
        return o.total_seconds()
    try:
        if pd.isna(o):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def json_size(obj) -> int:
    return len(json.dumps(obj, default=json_default))


def count_numeric_fields(obj) -> int:
    if isinstance(obj, dict):
        return sum(count_numeric_fields(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_numeric_fields(v) for v in obj)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return 1
    return 0


def main():
    print("Loading sources and building states...")
    sources = ws.load_all_sources()
    ny_tz = ws.NYC_TZ

    labels_times = {}
    for hr in [6, 8, 10, 12, 14, 16]:
        t_local = datetime(2025, 7, 1, hr, 0, tzinfo=ny_tz)
        labels_times[f"{hr:02d}00_ET"] = t_local.astimezone(timezone.utc)

    # Immediately-after states around real usable/observation/AFD events (Part 22).
    events = ws.build_event_timeline(sources, policy=ws.PROXY_STATE)
    events_july1 = ws.filter_timeline_to_july1(events)

    def first_event_after(event_type, after_utc=None):
        subset = events_july1[events_july1["event_type"] == event_type]
        if after_utc is not None:
            subset = subset[subset["event_time"] >= pd.Timestamp(after_utc)]
        return subset.iloc[0]["event_time"] if not subset.empty else None

    hrrr_usable_t = first_event_after("HRRR_USABLE_UPDATE", datetime(2025, 7, 1, 2, 30, tzinfo=timezone.utc))
    gefs_usable_t = first_event_after("GEFS_USABLE_UPDATE", datetime(2025, 7, 1, 0, 0, tzinfo=timezone.utc))
    afd_t = first_event_after("AFD_UPDATE", datetime(2025, 7, 1, 12, 0, tzinfo=timezone.utc))
    obs_t = first_event_after("OBSERVATION", datetime(2025, 7, 1, 12, 0, tzinfo=timezone.utc))

    for label, evt_time in [
        ("after_hrrr_usable_update", hrrr_usable_t),
        ("after_gefs_usable_update", gefs_usable_t),
        ("after_afd_update", afd_t),
        ("after_observation_update", obs_t),
    ]:
        if evt_time is not None:
            labels_times[label] = (pd.Timestamp(evt_time) + timedelta(minutes=1)).to_pydatetime()

    canonical_states = {}
    compact_inputs = {}
    for label, t in labels_times.items():
        state = ws.get_weather_state(sources, t, policy=ws.PROXY_STATE)
        audit = ws.validate_no_lookahead(state)
        if audit["overall"] != "PASS":
            print(f"STOP: no-lookahead audit FAILED for {label}: {audit}")
            return
        canonical_states[label] = state
        compact_inputs[label] = wi.build_weather_input(state)

    with open(OUT_DIR / "compact_inputs.json", "w") as f:
        json.dump(compact_inputs, f, default=json_default, indent=2)

    print("\nPart 23: size comparison...")
    size_rows = []
    for label in labels_times:
        canon = canonical_states[label]
        compact = compact_inputs[label]
        canon_size = json_size(canon)
        compact_size = json_size(compact)
        trajectory_points = sum(
            len(compact.get(k, {}).get("remaining_target_day_trajectory", []))
            for k in ["hrrr", "gfs", "nbm"]
        )
        afd_chars = compact.get("afd", {}).get("selected_section_character_count", 0)
        approx_tokens = json_size(compact) // 4  # rough chars/4 heuristic for the WHOLE rendered compact input
        size_rows.append(
            {
                "state_label": label,
                "canonical_state_bytes": canon_size,
                "compact_input_bytes": compact_size,
                "reduction_pct": round(100 * (1 - compact_size / canon_size), 1),
                "numeric_field_count": count_numeric_fields(compact),
                "trajectory_points": trajectory_points,
                "afd_selected_characters": afd_chars,
                "approx_total_tokens_if_rendered_as_text": approx_tokens,
                "fits_512_tokens": approx_tokens <= 512,
                "fits_1024_tokens": approx_tokens <= 1024,
            }
        )
    size_df = pd.DataFrame(size_rows)
    with open(OUT_DIR / "compression_report.json", "w") as f:
        json.dump(size_rows, f, default=json_default, indent=2)
    print(size_df.to_string())

    print("\nPart 24: information-preservation audit...")
    preservation_rows = []
    for label in labels_times:
        canon = canonical_states[label]
        compact = compact_inputs[label]
        checks = {}
        # Retains latest usable deterministic forecast + revision + age + completeness.
        for key in ["hrrr", "gfs", "nbm"]:
            canon_usable = canon[key].get("usable_for_daily_max")
            compact_usable = compact[key].get("usable_for_daily_max")
            checks[f"{key}_usable_flag_matches"] = canon_usable == compact_usable
            if canon_usable:
                checks[f"{key}_predicted_max_matches"] = (
                    round(canon[key]["latest_usable_run"]["predicted_daily_max_f"], 2) == compact[key]["predicted_daily_max_f"]
                )
                checks[f"{key}_revision_present"] = "revision_f" in compact[key]
                checks[f"{key}_age_present"] = compact[key].get("forecast_age_minutes") is not None
                checks[f"{key}_coverage_present"] = compact[key].get("coverage_pct") is not None
        # GEFS distribution retained.
        if canon["gefs"].get("usable_for_daily_max"):
            checks["gefs_distribution_retained"] = all(
                compact["gefs"].get(k) is not None for k in ["mean_daily_max_f", "median_daily_max_f", "std_daily_max_f", "p10_daily_max_f", "p90_daily_max_f"]
            )
            checks["gefs_raw_members_excluded"] = "member_daily_max_f" not in compact["gefs"]
        # Observation max-so-far / recency retained.
        canon_stations_by_label = {
            ws.STATION_LABELS.get(sid, sid): s for sid, s in canon["observations"]["stations"].items()
        }
        day_start_utc, _ = ws.local_day_utc_bounds()
        target_day_has_started = pd.Timestamp(canon["state_time"]) >= day_start_utc
        checks["observation_max_so_far_retained"] = (not target_day_has_started) or all(
            (s.get("max_so_far_f") is not None) or (not canon_stations_by_label[label_].get("available"))
            for label_, s in compact["observations"]["stations"].items()
        )
        checks["observation_recency_retained"] = all(
            (s.get("observation_age_minutes") is not None) or (not canon_stations_by_label[label_].get("available"))
            for label_, s in compact["observations"]["stations"].items()
        )
        checks["cross_station_disagreement_retained"] = compact["observations"].get("current_temperature_range_f") is not None
        checks["cross_model_disagreement_retained"] = len(compact["cross_model"].get("pairwise_disagreement_f", {})) > 0 or compact["cross_model"].get("forecast_count", 0) < 2
        # AFD text + age retained.
        checks["afd_text_retained"] = compact["afd"].get("available") is False or len(compact["afd"].get("sections", [])) > 0
        checks["afd_age_retained"] = compact["afd"].get("available") is False or compact["afd"].get("age_minutes") is not None
        # Time remaining in target day retained.
        checks["hours_remaining_retained"] = compact["target"].get("hours_remaining_in_day") is not None
        # Removed: raw provenance, all raw ensemble members, large obs history, irrelevant AFD sections, unused metadata.
        checks["no_provenance_block_in_compact"] = all("provenance" not in compact.get(k, {}) for k in ["hrrr", "gfs", "nbm"])
        checks["no_raw_gefs_members_in_compact"] = "member_daily_max_f" not in compact.get("gefs", {})
        checks["no_full_afd_text_in_compact"] = "raw_text" not in compact.get("afd", {})
        checks["no_source_rows_used_in_compact"] = all("source_rows_used" not in str(compact.get(k, {}).keys()) for k in ["hrrr", "gfs", "nbm"])

        all_pass = all(v for v in checks.values())
        preservation_rows.append({"state_label": label, "all_checks_pass": all_pass, "checks": checks})

    with open(OUT_DIR / "information_preservation_audit.json", "w") as f:
        json.dump(preservation_rows, f, default=json_default, indent=2)

    for row in preservation_rows:
        print(f"  {row['state_label']}: {'PASS' if row['all_checks_pass'] else 'FAIL'}")
        if not row["all_checks_pass"]:
            for k, v in row["checks"].items():
                if not v:
                    print(f"    FAILED: {k}")

    print("\nDone. Outputs written to", OUT_DIR)


if __name__ == "__main__":
    main()
