"""First compact model-input representation, compressed FROM weather_state(t).

Architecture (do not collapse these layers):
    RAW DATA -> weather_state(t) -> weather_input(t) -> Laya / Jev (not built here)

weather_state(t) (data/weather_state.py) stays rich and nested and is never
mutated by this module -- this file only READS an already-built state dict
and produces a strictly smaller, flatter structure. This is intentionally
ONE model-neutral representation (no Laya-specific or Jev-specific version):
scientific comparison between them must start from equivalent information.

NOT decided here (deferred to a later phase, per this task's explicit
scope): final prompt format (JSON/text/table/special tokens), the
temperature-bucket support, tokenization, or any LLM call.

NOT done here: LLM summarization, embeddings, sentiment/confidence scores,
or converting AFD's qualitative language into numeric temperatures. AFD
text is carried through verbatim, section-selected only.
"""

from pathlib import Path

import pandas as pd

from data import weather_state as ws

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AFD_SECTIONS_PATH = PROJECT_ROOT / "data/processed/weather/afd/test/afd_sections.parquet"

# Part 20: prefer these sections; a section is included if its name STARTS
# WITH one of these (real section names carry a variable time-window
# suffix, e.g. "NEAR TERM /THROUGH TONIGHT/" -- verified against the
# actual afd_sections.parquet, not assumed to be exact matches).
PREFERRED_AFD_SECTION_PREFIXES = ("SYNOPSIS", "NEAR TERM", "SHORT TERM")

_afd_sections_cache = None


def _round(x, n=2):
    return round(x, n) if x is not None and pd.notna(x) else None


def _minutes(td):
    return round(td.total_seconds() / 60, 1) if td is not None and pd.notna(td) else None


def _iso(ts):
    return ts.isoformat() if ts is not None and pd.notna(ts) else None


# ---------------------------------------------------------------------------
# Part 14 -- target/time features.
# ---------------------------------------------------------------------------

def _compress_target(state: dict) -> dict:
    tdp = state["target_day_progress"]
    return {
        "target_date": ws.TARGET_LOCAL_DATE.isoformat(),
        "state_time_local": _iso(tdp["state_time_local"]),
        "state_time_utc": _iso(tdp["state_time_utc"]),
        "local_hour": round(tdp["state_time_local"].hour + tdp["state_time_local"].minute / 60, 2),
        "hours_remaining_in_day": _round(tdp["hours_until_end_of_local_day"]),
    }


# ---------------------------------------------------------------------------
# Part 15 -- observation compression.
# ---------------------------------------------------------------------------

def _compress_observations(state: dict) -> dict:
    obs = state["observations"]
    stations = {}
    for sid, s in obs["stations"].items():
        label = ws.STATION_LABELS.get(sid, sid)
        if not s.get("available"):
            stations[label] = {"available": False}
            continue
        stations[label] = {
            "available": True,
            "latest_temperature_f": _round(s["current_temperature_f"]),
            "max_so_far_f": _round(s["max_temperature_observed_so_far_f"]),
            "observation_age_minutes": _minutes(s["observation_age"]),
            "temperature_change_previous_observation_f": _round(s["temperature_change_previous_observation_f"]),
            "temperature_change_approx_1h_f": _round(s["temperature_change_approx_1h_f"]),
        }
    cross = obs.get("cross_station_diagnostics", {})
    return {
        "stations": stations,
        "highest_temperature_so_far_f": _round(state["target_day_progress"].get("highest_observed_temperature_any_station_so_far_f")),
        "current_temperature_range_f": _round(cross.get("temperature_range_f")),
    }


# ---------------------------------------------------------------------------
# Part 16 -- deterministic forecast compression (HRRR/GFS/NBM/ECMWF-det).
# ---------------------------------------------------------------------------

def _compress_deterministic(state: dict, key: str) -> dict:
    s = state.get(key, {})
    if not s.get("usable_for_daily_max"):
        seen = s.get("latest_seen_run")
        out = {
            "available": bool(s.get("available")),
            "usable_for_daily_max": False,
            "reason": s.get("reason", "no complete run yet"),
            "newer_run_seen_coverage_pct": _round(seen.get("coverage_pct")) if seen else None,
        }
        if key == "nbm":
            out["note"] = "NBM daily max means reconstructed maximum from the canonical hourly TMP representation, not the direct NBM TMAX product (known unresolved discrepancy, not solved here)."
        return out

    usable = s["latest_usable_run"]
    previous = s.get("previous_usable_run")
    state_time = pd.Timestamp(state["state_time"])
    trajectory = [
        {"local_hour": round(pd.Timestamp(pt["valid_time"]).tz_convert(ws.NYC_TZ).hour
                              + pd.Timestamp(pt["valid_time"]).tz_convert(ws.NYC_TZ).minute / 60, 2),
         "temp_f": _round(pt["value_f"])}
        for pt in usable["target_day_temperature_path"]
        if pd.Timestamp(pt["valid_time"]) >= state_time
    ]
    out = {
        "available": True,
        "usable_for_daily_max": True,
        "predicted_daily_max_f": _round(usable["predicted_daily_max_f"]),
        "previous_predicted_daily_max_f": _round(previous["predicted_daily_max_f"]) if previous else None,
        "revision_f": _round(s.get("revision_f")),
        "forecast_age_minutes": _minutes(usable["age_since_run"]),
        "run_time": _iso(usable["run_time"]),
        "coverage_pct": _round(usable["coverage_pct"]),
        "remaining_target_day_trajectory": trajectory,
        "newer_run_arriving": s.get("newer_run_arriving"),
    }
    if key == "nbm":
        out["note"] = "NBM daily max means reconstructed maximum from the canonical hourly TMP representation, not the direct NBM TMAX product (known unresolved discrepancy, not solved here)."
    return out


# ---------------------------------------------------------------------------
# Part 17 -- GEFS ensemble compression.
# ---------------------------------------------------------------------------

def _compress_gefs(state: dict) -> dict:
    s = state.get("gefs", {})
    if not s.get("usable_for_daily_max"):
        seen = s.get("latest_seen_run")
        return {
            "available": bool(s.get("available")),
            "usable_for_daily_max": False,
            "member_completeness_pct": _round(seen.get("member_completeness_pct")) if seen else None,
            "forecast_completeness_pct": _round(seen.get("forecast_completeness_pct")) if seen else None,
            "reason": "no complete ensemble run yet -- continuing to use the previous complete run's summary is not possible here because none exists yet" if seen is None else "newer run incomplete; falling back handled upstream in weather_state",
        }
    usable = s["latest_usable_run"]
    dist = usable.get("distribution", {})
    revision = s.get("revision")
    return {
        "available": True,
        "usable_for_daily_max": True,
        "member_count": usable.get("member_count_available"),
        "mean_daily_max_f": _round(dist.get("mean_daily_max_f")),
        "median_daily_max_f": _round(dist.get("median_daily_max_f")),
        "std_daily_max_f": _round(dist.get("std_daily_max_f")),
        "p10_daily_max_f": _round(dist.get("p10")),
        "p25_daily_max_f": _round(dist.get("p25")),
        "p75_daily_max_f": _round(dist.get("p75")),
        "p90_daily_max_f": _round(dist.get("p90")),
        "min_daily_max_f": _round(dist.get("min_daily_max_f")),
        "max_daily_max_f": _round(dist.get("max_daily_max_f")),
        "ensemble_age_minutes": _minutes(usable["age_since_run"]),
        "revision_mean_f": _round(revision["mean_daily_max_f"]) if revision else None,
        "revision_median_f": _round(revision["median_daily_max_f"]) if revision else None,
        "newer_run_arriving": s.get("newer_run_arriving"),
    }


# ---------------------------------------------------------------------------
# Part 18 -- ECMWF ensemble compression (intentionally incomplete sample).
# ---------------------------------------------------------------------------

def _compress_ecmwf_ensemble(state: dict) -> dict:
    s = state.get("ecmwf_ensemble", {})
    completeness = s.get("completeness", "INSUFFICIENT")
    result = {"status": completeness}
    seen = s.get("latest_seen_run")
    if seen and seen.get("distribution"):
        result["partial_sample_stats_f"] = {k: _round(v) for k, v in seen["distribution"].items()}
        result["n_target_day_hours_sampled"] = s.get("n_target_day_hours_sampled")
        result["n_target_day_hours_possible"] = s.get("n_target_day_hours_possible")
        result["note"] = "PARTIAL SAMPLE ONLY -- not a complete ensemble distribution; never present as full uncertainty."
    return result


# ---------------------------------------------------------------------------
# Part 19 -- cross-model diagnostics.
# ---------------------------------------------------------------------------

def _compress_cross_model(state: dict) -> dict:
    diag = state.get("cross_model_diagnostics", {})
    return {
        "individual_predictions_f": {k: _round(v) for k, v in diag.get("individual_predictions_f", {}).items()},
        "forecast_count": diag.get("forecast_count"),
        "forecast_mean_f": _round(diag.get("forecast_mean_f")),
        "forecast_median_f": _round(diag.get("forecast_median_f")),
        "forecast_min_f": _round(diag.get("forecast_min_f")),
        "forecast_max_f": _round(diag.get("forecast_max_f")),
        "forecast_range_f": _round(diag.get("forecast_range_f")),
        "forecast_std_f": _round(diag.get("forecast_std_f")),
        "pairwise_disagreement_f": {k: _round(v) for k, v in diag.get("pairwise_disagreement_f", {}).items()},
    }


# ---------------------------------------------------------------------------
# Part 20/21 -- AFD section extraction (no LLM summarization/embeddings).
# ---------------------------------------------------------------------------

def _load_afd_sections() -> pd.DataFrame:
    global _afd_sections_cache
    if _afd_sections_cache is None:
        _afd_sections_cache = pd.read_parquet(AFD_SECTIONS_PATH)
    return _afd_sections_cache


def _select_afd_sections(product_id: str) -> list[dict]:
    df = _load_afd_sections()
    rows = df[df["product_id"] == product_id]
    selected = rows[rows["section_name"].str.startswith(PREFERRED_AFD_SECTION_PREFIXES)]
    return selected[["section_name", "section_text"]].to_dict("records")


def _compress_afd(state: dict) -> dict:
    s = state.get("afd", {})
    if not s.get("available"):
        return {"available": False, "reason": s.get("reason")}
    sections = _select_afd_sections(s["latest_product_id"])
    full_len = len(s.get("raw_text") or "")
    selected_len = sum(len(sec["section_text"]) for sec in sections)
    return {
        "available": True,
        "product_id": s["latest_product_id"],
        "issuance_time": _iso(s["issuance_time"]),
        "age_minutes": _minutes(s["afd_age"]),
        "sections": sections,
        "full_afd_character_count": full_len,
        "selected_section_character_count": selected_len,
        "approx_token_count_selected": round(selected_len / 4),
    }


# ---------------------------------------------------------------------------
# Top-level compressor.
# ---------------------------------------------------------------------------

def build_weather_input(state: dict) -> dict:
    """weather_state(t) -> weather_input(t). Pure compression: reads only,
    never mutates state. Same function for both Laya and Jev (Part 25)."""
    return {
        "target": _compress_target(state),
        "observations": _compress_observations(state),
        "hrrr": _compress_deterministic(state, "hrrr"),
        "gfs": _compress_deterministic(state, "gfs"),
        "nbm": _compress_deterministic(state, "nbm"),
        "ecmwf": {
            "deterministic": _compress_deterministic(state, "ecmwf_deterministic"),
            "ensemble": _compress_ecmwf_ensemble(state),
        },
        "gefs": _compress_gefs(state),
        "cross_model": _compress_cross_model(state),
        "afd": _compress_afd(state),
    }
