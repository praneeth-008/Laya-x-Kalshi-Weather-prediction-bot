"""Point-in-time weather information state builder for NYC, 2025-07-01.

Reads ONLY the already-collected test outputs from the seven source
feasibility tests (data/processed/weather/{hrrr,gfs,gefs,observations,nbm,
ecmwf,afd}/test/). Downloads NOTHING new. Builds the canonical, richly-
structured point-in-time state described in this project's architecture
task -- NOT a flattened model-input table, and NOT a Laya/Jev prompt.

CORE RULE: no information whose resolved availability time is later than
the query timestamp t may ever enter weather_state(t). This is enforced
in every state-building function below and independently re-checked by
validate_no_lookahead().

======================================================================
TIMING SEMANTICS (verified against the actual saved test files, not
assumed from prior reports -- see the inspection this module's docstring
is based on):

  run_time        -- nominal forecast model initialization time.
                      Present: HRRR, GFS, NBM, ECMWF-det, GEFS, ECMWF-ens.
  valid_time      -- timestamp a forecast VALUE refers to.
                      Present: same six sources as run_time.
  observation_time-- timestamp a surface observation was taken.
                      Present: Observations only.
  issuance_time   -- timestamp a human-authored product was issued.
                      Present: AFD only.
  available_time  -- OBSERVED, from the S3 object's Last-Modified header,
                      for HRRR/GFS/NBM/ECMWF-det/GEFS/ECMWF-ens. Column
                      EXISTS but is 100% null for AFD (schema carries the
                      field; no value was ever recoverable). Column does
                      NOT EXIST AT ALL for Observations (never claimed).
  retrieved_at    -- when THIS PROJECT'S test script fetched the row.
                      Present on every source. Never used for
                      availability filtering (that would be look-ahead
                      relative to the historical target day).

Nothing in this project's data is labeled EXACT. Every non-null
availability figure is a proxy of one of two kinds:
  S3_LAST_MODIFIED_PROXY  -- HRRR, GFS, NBM, GEFS, ECMWF (det + ens).
  OBSERVATION_TIME_ONLY   -- Observations (no proxy exists at all).
  ISSUANCE_TIME_ONLY      -- AFD (no proxy exists at all; available_time
                              column present but always null).

ECMWF's S3_LAST_MODIFIED_PROXY carries an extra caveat, OBSERVED in this
project's own ECMWF test: publication lag there averaged ~514 minutes
(~8.6h) vs. 40min-3.5h for every other GRIB source. This module tags
ECMWF rows with availability_confidence="LOW" rather than silently
treating that lag as equivalent to the other sources' proxies.
======================================================================

TIMING POLICIES (Part 3):
  STRICT_STATE -- only sources with an S3_LAST_MODIFIED_PROXY are
                  eligible; Observations and AFD are EXCLUDED entirely
                  (their only "timestamp" is self-reported, not an
                  independent availability signal).
  PROXY_STATE  -- STRICT_STATE's sources, PLUS Observations (proxied by
                  observation_time) and AFD (proxied by issuance_time),
                  each explicitly flagged with their proxy status. This
                  is a labeled ASSUMPTION, never rewritten as fact.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NYC_TZ = ZoneInfo("America/New_York")
TARGET_LOCAL_DATE = datetime(2025, 7, 1).date()

STRICT_STATE = "strict"
PROXY_STATE = "proxy"

AVAIL_S3_PROXY = "S3_LAST_MODIFIED_PROXY"
AVAIL_OBS_ONLY = "OBSERVATION_TIME_ONLY"
AVAIL_ISSUANCE_ONLY = "ISSUANCE_TIME_ONLY"
AVAIL_UNKNOWN = "UNKNOWN"

PATHS = {
    "hrrr": PROJECT_ROOT / "data/processed/weather/hrrr/test/hrrr_test_canonical.csv",
    "gfs": PROJECT_ROOT / "data/processed/weather/gfs/test/gfs_test_canonical.csv",
    "gefs": PROJECT_ROOT / "data/processed/weather/gefs/test/gefs_test_canonical.csv",
    "nbm": PROJECT_ROOT / "data/processed/weather/nbm/test/nbm_test_canonical.csv",
    "ecmwf_det": PROJECT_ROOT / "data/processed/weather/ecmwf/test/ecmwf_test_deterministic_canonical.csv",
    "ecmwf_ens": PROJECT_ROOT / "data/processed/weather/ecmwf/test/ecmwf_test_ensemble_sample_canonical.csv",
    "observations": PROJECT_ROOT / "data/processed/weather/observations/test/observations_test_canonical.csv",
    "afd": PROJECT_ROOT / "data/processed/weather/afd/test/afd_documents.parquet",
}

EXPECTED_GEFS_MEMBERS = 31
EXPECTED_ECMWF_ENS_MEMBERS = 51


def local_day_utc_bounds(local_date=TARGET_LOCAL_DATE, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# TARGET-DAY COMPLETENESS RULE.
#
# "Complete for target day" does NOT mean the full model run across its
# entire forecast horizon -- it means: every forecast valid-time this
# source's OWN native schedule would produce, from the run's own start
# through the remainder of the local target day, is present. Schedules
# below are taken directly from this project's actual downloader modules
# (data/hrrr.py, data/gfs.py, data/nbm.py, data/gefs.py) and from the
# ECMWF feasibility test's OBSERVED 3-hourly/6-hourly step -- not
# reinvented or assumed independently.
#
#   HRRR: hourly throughout. Max lead 48h for synoptic-hour runs
#         (00/06/12/18Z), else 18h (data/hrrr.py: EXTENDED_RUN_HOURS,
#         EXTENDED_MAX_FH=48, STANDARD_MAX_FH=18).
#   GFS:  hourly through +120h (data/gfs.py: HOURLY_MAX_FH=120) -- always
#         far enough for a next-day target, so effectively unconstrained
#         here.
#   NBM:  hourly through +36h, then 3-hourly through +192h (data/nbm.py:
#         HOURLY_MAX_FH=36, MID_STEP_HOURS=3, MID_MAX_FH=192). VERIFIED
#         live in this project's own canonical file: the 2025-06-30T12Z
#         NBM run shows BOTH a 1h and a 3h step within the same run,
#         exactly matching this schedule.
#   GEFS: 3-hourly throughout (data/gefs.py: FORECAST_STEP_HOURS=3).
#   ECMWF deterministic: 3-hourly through 144h, then 6-hourly beyond
#         (OBSERVED in the ECMWF feasibility test); irrelevant beyond
#         +144h for a next-day target.
#
# A run whose schedule produces ZERO required target-day valid times
# (e.g. a run_time already at/after day_end) is run_status=NOT_APPLICABLE
# -- there is nothing to assess, not "incomplete." Otherwise run_status is
# COMPLETE only when every expected point is present, else INCOMPLETE.
# Note: a run whose own forecast horizon cannot reach day_end (e.g. an
# 18h-capped intermediate-hour HRRR run queried very early in its life)
# will show sustained low coverage and stay INCOMPLETE for its whole
# lifetime in this dataset -- that is intentional and not a bug: it
# correctly signals "this run alone can never fully cover the target
# day," so the state builder keeps falling back to the previous usable
# run until a run that CAN reach full coverage arrives.
# ---------------------------------------------------------------------------

def _forecast_hour_candidates(source_name: str, run_time) -> list[int]:
    hour = run_time.hour
    if source_name == "hrrr":
        max_fh = 48 if hour in {0, 6, 12, 18} else 18
        return list(range(0, max_fh + 1))
    if source_name == "gfs":
        return list(range(0, 121))
    if source_name == "nbm":
        hours = list(range(1, 37))
        hours += list(range(39, 193, 3))
        return hours
    if source_name == "gefs":
        return list(range(0, 241, 3))
    if source_name in ("ecmwf_deterministic", "ecmwf_ensemble"):
        # Same model, same step cadence for det and ensemble.
        hours = list(range(0, 145, 3))
        hours += list(range(150, 361, 6))
        return hours
    raise ValueError(f"no forecast-hour schedule defined for source {source_name!r}")


def expected_target_day_valid_times(source_name: str, run_time, day_start_utc, day_end_utc) -> list:
    candidates = _forecast_hour_candidates(source_name, run_time)
    valid_times = [run_time + timedelta(hours=fh) for fh in candidates]
    return sorted(vt for vt in valid_times if day_start_utc <= vt < day_end_utc)


def assess_deterministic_run_completeness(source_name: str, run_time, run_rows_in_day: pd.DataFrame, day_start_utc, day_end_utc) -> dict:
    """Coverage of a single run's target-day valid times against its own
    native schedule. run_rows_in_day must already be restricted to this
    run and to valid_time within [day_start_utc, day_end_utc)."""
    expected = set(expected_target_day_valid_times(source_name, run_time, day_start_utc, day_end_utc))
    n_expected = len(expected)
    if n_expected == 0:
        return {
            "expected_target_day_forecast_points": 0, "available_target_day_forecast_points": 0,
            "coverage_pct": None, "run_status": "NOT_APPLICABLE", "usable_for_daily_max": False,
        }
    available = set(run_rows_in_day["valid_time"]) & expected
    n_available = len(available)
    coverage_pct = 100.0 * n_available / n_expected
    status = "COMPLETE" if n_available == n_expected else "INCOMPLETE"
    return {
        "expected_target_day_forecast_points": n_expected,
        "available_target_day_forecast_points": n_available,
        "coverage_pct": coverage_pct,
        "run_status": status,
        "usable_for_daily_max": status == "COMPLETE",
    }


def assess_ensemble_run_completeness(source_name: str, run_time, run_rows_in_day: pd.DataFrame, day_start_utc, day_end_utc, expected_members: int) -> dict:
    """Shared by GEFS and ECMWF ensemble -- two completeness dimensions
    (Part 7): forecast-hour coverage AND ensemble-member coverage. A run
    is usable only when EVERY present member has EVERY expected
    target-day valid time AND the full expected member count has arrived
    (conservative rule)."""
    expected_vts = set(expected_target_day_valid_times(source_name, run_time, day_start_utc, day_end_utc))
    n_expected_points = len(expected_vts)
    members_present = sorted(run_rows_in_day["ensemble_member"].dropna().unique()) if not run_rows_in_day.empty else []
    member_count_available = len(members_present)
    member_completeness_pct = 100.0 * member_count_available / expected_members

    if n_expected_points == 0:
        return {
            "member_count_available": member_count_available, "member_count_expected": expected_members,
            "member_completeness_pct": member_completeness_pct,
            "forecast_points_available": 0, "forecast_points_expected": 0, "forecast_completeness_pct": None,
            "run_status": "NOT_APPLICABLE", "usable_for_daily_max": False,
        }

    if members_present:
        per_member_available = [
            len(set(run_rows_in_day.loc[run_rows_in_day["ensemble_member"] == m, "valid_time"]) & expected_vts)
            for m in members_present
        ]
        forecast_points_available = min(per_member_available)
    else:
        forecast_points_available = 0
    forecast_completeness_pct = 100.0 * forecast_points_available / n_expected_points
    is_complete = forecast_points_available == n_expected_points and member_count_available == expected_members
    return {
        "member_count_available": member_count_available, "member_count_expected": expected_members,
        "member_completeness_pct": member_completeness_pct,
        "forecast_points_available": forecast_points_available, "forecast_points_expected": n_expected_points,
        "forecast_completeness_pct": forecast_completeness_pct,
        "run_status": "COMPLETE" if is_complete else "INCOMPLETE",
        "usable_for_daily_max": is_complete,
    }


def _run_usable_timestamp(all_rows_for_source: pd.DataFrame, source_name: str, run_time, day_start_utc, day_end_utc):
    """Historical moment (from the FULL, non-time-filtered dataset) this
    run's target-day coverage actually became complete -- used only to
    place *_USABLE_UPDATE events on the timeline, not to query a state at
    some t (that still goes through eligible_mask elsewhere). Returns
    None if this run never reaches completeness in this dataset."""
    g = all_rows_for_source[all_rows_for_source["run_time"] == run_time]
    in_day = g[(g["valid_time"] >= day_start_utc) & (g["valid_time"] < day_end_utc)]
    if source_name == "gefs":
        completeness = assess_ensemble_run_completeness(source_name, run_time, in_day, day_start_utc, day_end_utc, EXPECTED_GEFS_MEMBERS)
    elif source_name == "ecmwf_ensemble":
        completeness = assess_ensemble_run_completeness(source_name, run_time, in_day, day_start_utc, day_end_utc, EXPECTED_ECMWF_ENS_MEMBERS)
    else:
        completeness = assess_deterministic_run_completeness(source_name, run_time, in_day, day_start_utc, day_end_utc)
    if not completeness["usable_for_daily_max"]:
        return None
    expected = set(expected_target_day_valid_times(source_name, run_time, day_start_utc, day_end_utc))
    matching = in_day[in_day["valid_time"].isin(expected)]
    ts = matching["available_time_resolved"].max()
    return ts if pd.notna(ts) else None


# ---------------------------------------------------------------------------
# PART 1 -- schema inventory (callable, prints + returns a table).
# ---------------------------------------------------------------------------

def inspect_schemas() -> pd.DataFrame:
    rows = []
    for name, path in PATHS.items():
        if not path.exists():
            rows.append({"source": name, "file": str(path), "status": "MISSING"})
            continue
        if path.suffix == ".parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path)
        rows.append(
            {
                "source": name,
                "file": str(path.relative_to(PROJECT_ROOT)),
                "status": "OK",
                "row_count": len(df),
                "columns": list(df.columns),
                "has_run_time": "run_time" in df.columns,
                "has_valid_time": "valid_time" in df.columns,
                "has_observation_time": "observation_time" in df.columns,
                "has_issuance_time": "issuance_time" in df.columns,
                "has_available_time": "available_time" in df.columns,
                "available_time_nonnull_pct": (
                    round(100 * df["available_time"].notna().mean(), 1) if "available_time" in df.columns else None
                ),
                "has_ensemble_member": "ensemble_member" in df.columns,
                "has_station_id": "station_id" in df.columns,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Loading + availability resolution.
# ---------------------------------------------------------------------------

def _parse_s3_last_modified(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


@dataclass
class LoadedSources:
    hrrr: pd.DataFrame
    gfs: pd.DataFrame
    gefs: pd.DataFrame
    nbm: pd.DataFrame
    ecmwf_det: pd.DataFrame
    ecmwf_ens: pd.DataFrame
    observations: pd.DataFrame
    afd: pd.DataFrame


def load_all_sources() -> LoadedSources:
    def load_grib(name: str) -> pd.DataFrame:
        df = pd.read_csv(PATHS[name])
        df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
        df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
        df["available_time_resolved"] = _parse_s3_last_modified(df["available_time"])
        df["availability_status"] = AVAIL_S3_PROXY
        df["availability_confidence"] = "LOW" if name.startswith("ecmwf") else "NORMAL"
        df["source_name"] = name
        return df

    hrrr = load_grib("hrrr")
    gfs = load_grib("gfs")
    gefs = load_grib("gefs")
    nbm = load_grib("nbm")
    ecmwf_det = load_grib("ecmwf_det")
    ecmwf_ens = load_grib("ecmwf_ens")

    obs = pd.read_csv(PATHS["observations"])
    obs["observation_time"] = pd.to_datetime(obs["observation_time"], utc=True)
    obs["availability_status"] = AVAIL_OBS_ONLY
    obs["source_name"] = "observations"

    afd = pd.read_parquet(PATHS["afd"])
    afd["issuance_time"] = pd.to_datetime(afd["issuance_time"], utc=True)
    afd["availability_status"] = AVAIL_ISSUANCE_ONLY
    afd["source_name"] = "afd"

    return LoadedSources(hrrr, gfs, gefs, nbm, ecmwf_det, ecmwf_ens, obs, afd)


def _eligibility_time(row_availability_status: str, row: pd.Series) -> datetime | None:
    """The single timestamp used to decide 'is this row knowable at t',
    given what's actually available for its source."""
    if row_availability_status == AVAIL_S3_PROXY:
        return row["available_time_resolved"]
    if row_availability_status == AVAIL_OBS_ONLY:
        return row["observation_time"]
    if row_availability_status == AVAIL_ISSUANCE_ONLY:
        return row["issuance_time"]
    return None


def eligible_mask(df: pd.DataFrame, t: datetime, policy: str) -> pd.Series:
    """Boolean mask: which rows of df are legitimately knowable at t under
    the given policy. Never allows a row through with a null/unresolvable
    timestamp -- an unresolvable timestamp means we cannot PROVE it was
    available, so it is excluded, not assumed available."""
    status = df["availability_status"].iloc[0] if len(df) else None
    if status == AVAIL_S3_PROXY:
        ts = df["available_time_resolved"]
    elif status == AVAIL_OBS_ONLY:
        if policy != PROXY_STATE:
            return pd.Series(False, index=df.index)
        ts = df["observation_time"]
    elif status == AVAIL_ISSUANCE_ONLY:
        if policy != PROXY_STATE:
            return pd.Series(False, index=df.index)
        ts = df["issuance_time"]
    else:
        return pd.Series(False, index=df.index)
    return ts.notna() & (ts <= pd.Timestamp(t))


# ---------------------------------------------------------------------------
# PART 4/5 -- normalized event schema + timeline.
# ---------------------------------------------------------------------------

EVENT_COLUMNS = [
    "event_id", "event_type", "source", "source_subtype", "event_time",
    "information_time", "available_time", "availability_status", "run_time",
    "valid_time", "station_id", "ensemble_member", "product_id", "payload_reference",
]


def build_event_timeline(sources: LoadedSources, policy: str = PROXY_STATE) -> pd.DataFrame:
    """ONE event per genuinely NEW piece of information -- one per (source,
    run_time) for forecast sources (using the EARLIEST resolved
    availability within that run as event_time, i.e. when the run first
    began appearing), one per real observation, one per AFD product.
    Progressive per-forecast-hour / per-member availability is NOT
    collapsed away -- it is preserved separately (see
    per_row_availability()) for the state builder's finer-grained
    filtering, just not inflated into the primary event count.
    """
    events = []
    day_start_utc, day_end_utc = local_day_utc_bounds()

    def add_forecast_events(df: pd.DataFrame, source: str, event_type: str):
        if df.empty:
            return
        elig = df[eligible_mask(df, datetime(2100, 1, 1, tzinfo=timezone.utc), policy)]  # all resolvable rows
        for run_time, g in elig.groupby("run_time"):
            event_time = g["available_time_resolved"].min()
            events.append(
                {
                    "event_id": f"{source}:{run_time.isoformat()}",
                    "event_type": event_type,
                    "source": source,
                    "source_subtype": g["product"].iloc[0] if "product" in g.columns else None,
                    "event_time": event_time,
                    "information_time": run_time,
                    "available_time": event_time,
                    "availability_status": AVAIL_S3_PROXY,
                    "run_time": run_time,
                    "valid_time": None,
                    "station_id": None,
                    "ensemble_member": None,
                    "product_id": None,
                    "payload_reference": f"{source}_test_canonical:run_time=={run_time.isoformat()}",
                }
            )
            # USABLE FORECAST UPDATE (Part 6): a distinct, later event fired
            # only once this run's target-day coverage is actually
            # COMPLETE. Raw arrival timestamps above are untouched.
            usable_ts = _run_usable_timestamp(elig, source, run_time, day_start_utc, day_end_utc)
            if usable_ts is not None:
                events.append(
                    {
                        "event_id": f"{source}:{run_time.isoformat()}:usable",
                        "event_type": event_type.replace("_UPDATE", "_USABLE_UPDATE"),
                        "source": source,
                        "source_subtype": "usable_for_daily_max",
                        "event_time": usable_ts,
                        "information_time": run_time,
                        "available_time": usable_ts,
                        "availability_status": AVAIL_S3_PROXY,
                        "run_time": run_time,
                        "valid_time": None,
                        "station_id": None,
                        "ensemble_member": None,
                        "product_id": None,
                        "payload_reference": f"{source}_test_canonical:run_time=={run_time.isoformat()},usable_target_day_coverage",
                    }
                )

    add_forecast_events(sources.hrrr, "hrrr", "HRRR_UPDATE")
    add_forecast_events(sources.gfs, "gfs", "GFS_UPDATE")
    add_forecast_events(sources.nbm, "nbm", "NBM_UPDATE")
    add_forecast_events(sources.ecmwf_det, "ecmwf_deterministic", "ECMWF_UPDATE")
    add_forecast_events(sources.ecmwf_ens, "ecmwf_ensemble", "ECMWF_UPDATE")

    # GEFS: one raw-arrival event per run using the FIRST member's
    # availability (progressive member arrival is preserved separately),
    # plus a GEFS_USABLE_UPDATE once forecast-hour AND member completeness
    # both reach 100% (Part 7's conservative rule).
    if not sources.gefs.empty:
        elig_gefs = sources.gefs[eligible_mask(sources.gefs, datetime(2100, 1, 1, tzinfo=timezone.utc), policy)]
        for run_time, g in elig_gefs.groupby("run_time"):
            event_time = g["available_time_resolved"].min()
            events.append(
                {
                    "event_id": f"gefs:{run_time.isoformat()}",
                    "event_type": "GEFS_UPDATE",
                    "source": "gefs",
                    "source_subtype": "enfo_member_start",
                    "event_time": event_time,
                    "information_time": run_time,
                    "available_time": event_time,
                    "availability_status": AVAIL_S3_PROXY,
                    "run_time": run_time,
                    "valid_time": None,
                    "station_id": None,
                    "ensemble_member": None,
                    "product_id": None,
                    "payload_reference": f"gefs_test_canonical:run_time=={run_time.isoformat()}",
                }
            )
            usable_ts = _run_usable_timestamp(elig_gefs, "gefs", run_time, day_start_utc, day_end_utc)
            if usable_ts is not None:
                events.append(
                    {
                        "event_id": f"gefs:{run_time.isoformat()}:usable",
                        "event_type": "GEFS_USABLE_UPDATE",
                        "source": "gefs",
                        "source_subtype": "usable_for_daily_max",
                        "event_time": usable_ts,
                        "information_time": run_time,
                        "available_time": usable_ts,
                        "availability_status": AVAIL_S3_PROXY,
                        "run_time": run_time,
                        "valid_time": None,
                        "station_id": None,
                        "ensemble_member": None,
                        "product_id": None,
                        "payload_reference": f"gefs_test_canonical:run_time=={run_time.isoformat()},usable_target_day_coverage",
                    }
                )

    # Observations: one event per real (non-SOD/SOM) observation, per station.
    obs = sources.observations
    real_obs = obs[obs["is_real_observation"]] if "is_real_observation" in obs.columns else obs
    for _, row in real_obs.iterrows():
        events.append(
            {
                "event_id": f"obs:{row['station_id']}:{row['observation_time'].isoformat()}",
                "event_type": "OBSERVATION",
                "source": "observations",
                "source_subtype": row.get("report_type_raw"),
                "event_time": row["observation_time"],
                "information_time": row["observation_time"],
                "available_time": None,
                "availability_status": AVAIL_OBS_ONLY,
                "run_time": None,
                "valid_time": None,
                "station_id": row["station_id"],
                "ensemble_member": None,
                "product_id": None,
                "payload_reference": f"observations_test_canonical:station_id=={row['station_id']},observation_time=={row['observation_time'].isoformat()}",
            }
        )

    # AFD: one event per product.
    for _, row in sources.afd.iterrows():
        events.append(
            {
                "event_id": f"afd:{row['product_id']}",
                "event_type": "AFD_UPDATE",
                "source": "afd",
                "source_subtype": "AFD",
                "event_time": row["issuance_time"],
                "information_time": row["issuance_time"],
                "available_time": None,
                "availability_status": AVAIL_ISSUANCE_ONLY,
                "run_time": None,
                "valid_time": None,
                "station_id": None,
                "ensemble_member": None,
                "product_id": row["product_id"],
                "payload_reference": f"afd_documents:product_id=={row['product_id']}",
            }
        )

    df = pd.DataFrame(events, columns=EVENT_COLUMNS)
    df = df.dropna(subset=["event_time"]).sort_values("event_time").reset_index(drop=True)
    return df


def filter_timeline_to_july1(events: pd.DataFrame) -> pd.DataFrame:
    """Restrict to events relevant to reconstructing July 1 -- i.e. events
    whose event_time falls on/before the end of the local July 1 day.
    Does NOT restrict by which day they FORECAST (an event from June 30
    that predicts July 1 is still included); DOES exclude anything after
    the local day ends (never include later information merely because
    it also happens to describe July 1)."""
    _, day_end_utc = local_day_utc_bounds()
    return events[events["event_time"] <= day_end_utc].copy()


# ---------------------------------------------------------------------------
# PART 7 -- deterministic forecast-source state.
# ---------------------------------------------------------------------------

def get_latest_forecast(df: pd.DataFrame, t: datetime, policy: str, source_name: str) -> dict:
    """Returns latest_seen_run (whatever run has most recently begun
    arriving, regardless of completeness -- raw provenance only, NEVER
    used to compute predicted_daily_max_f) separately from
    latest_usable_run / previous_usable_run (the most recent runs whose
    target-day coverage is actually COMPLETE -- the only ones a model
    should ever read a forecast value from)."""
    day_start_utc, day_end_utc = local_day_utc_bounds()
    mask = eligible_mask(df, t, policy)
    elig = df[mask]
    if elig.empty:
        return {
            "source": source_name, "available": False, "usable_for_daily_max": False,
            "reason": "no eligible rows at this state_time",
            "latest_seen_run": None, "latest_usable_run": None, "previous_usable_run": None,
        }

    run_times = sorted(elig["run_time"].unique(), reverse=True)

    def run_metadata(run_time):
        g = elig[elig["run_time"] == run_time]
        in_day = g[(g["valid_time"] >= day_start_utc) & (g["valid_time"] < day_end_utc)]
        completeness = assess_deterministic_run_completeness(source_name, run_time, in_day, day_start_utc, day_end_utc)
        run_available_time = g["available_time_resolved"].min()
        path = in_day.sort_values("valid_time")[["valid_time", "forecast_hour", "value_f"]].to_dict("records")
        predicted_max = in_day["value_f"].max() if not in_day.empty else None
        return {
            "run_time": run_time,
            "run_available_time": run_available_time,
            "age_since_run": t - run_time.to_pydatetime(),
            "age_since_available": (t - run_available_time.to_pydatetime()) if pd.notna(run_available_time) else None,
            "target_day_temperature_path": path,
            "predicted_daily_max_f": predicted_max,
            "source_rows_used": g.index.tolist(),
            **completeness,
        }

    all_metas = [run_metadata(rt) for rt in run_times]
    latest_seen_run = all_metas[0]
    usable_metas = [m for m in all_metas if m["usable_for_daily_max"]]  # already ordered by run_time desc
    latest_usable_run = usable_metas[0] if usable_metas else None
    previous_usable_run = usable_metas[1] if len(usable_metas) > 1 else None

    revision_f = None
    if (
        latest_usable_run and previous_usable_run
        and latest_usable_run["predicted_daily_max_f"] is not None
        and previous_usable_run["predicted_daily_max_f"] is not None
    ):
        revision_f = latest_usable_run["predicted_daily_max_f"] - previous_usable_run["predicted_daily_max_f"]

    newer_run_arriving = latest_usable_run is None or latest_seen_run["run_time"] != latest_usable_run["run_time"]

    return {
        "source": source_name,
        "available": True,
        "usable_for_daily_max": latest_usable_run is not None,
        "state_time": t,
        "latest_seen_run": latest_seen_run,
        "latest_usable_run": latest_usable_run,
        "previous_usable_run": previous_usable_run,
        "revision_f": revision_f,
        "newer_run_arriving": newer_run_arriving,
        "provenance": {
            "source_file": str(PATHS[source_name if source_name != "ecmwf_deterministic" else "ecmwf_det"].relative_to(PROJECT_ROOT)),
        },
    }


# ---------------------------------------------------------------------------
# PART 8 -- GEFS ensemble state.
# ---------------------------------------------------------------------------

def get_ensemble_state(df: pd.DataFrame, t: datetime, policy: str, expected_members: int = EXPECTED_GEFS_MEMBERS, source_name: str = "gefs") -> dict:
    """Same latest_seen_run / latest_usable_run / previous_usable_run split
    as get_latest_forecast(), but usability additionally requires full
    ensemble-member completeness (Part 7's conservative rule)."""
    day_start_utc, day_end_utc = local_day_utc_bounds()
    mask = eligible_mask(df, t, policy)
    elig = df[mask]
    if elig.empty:
        return {
            "source": source_name, "available": False, "usable_for_daily_max": False,
            "reason": "no eligible rows at this state_time",
            "latest_seen_run": None, "latest_usable_run": None, "previous_usable_run": None,
        }

    run_times = sorted(elig["run_time"].unique(), reverse=True)

    def run_metadata(run_time):
        g = elig[elig["run_time"] == run_time]
        in_day = g[(g["valid_time"] >= day_start_utc) & (g["valid_time"] < day_end_utc)]
        completeness = assess_ensemble_run_completeness(source_name, run_time, in_day, day_start_utc, day_end_utc, expected_members)
        run_available_time = g["available_time_resolved"].min()
        member_max = in_day.groupby("ensemble_member")["value_f"].max()
        vals = member_max.dropna()
        stats = {}
        if not vals.empty:
            stats = {
                "mean_daily_max_f": vals.mean(), "median_daily_max_f": vals.median(), "std_daily_max_f": vals.std(),
                "min_daily_max_f": vals.min(), "max_daily_max_f": vals.max(),
                "p10": vals.quantile(0.10), "p25": vals.quantile(0.25), "p75": vals.quantile(0.75), "p90": vals.quantile(0.90),
            }
        return {
            "run_time": run_time,
            "run_available_time": run_available_time,
            "age_since_run": t - run_time.to_pydatetime(),
            "age_since_available": (t - run_available_time.to_pydatetime()) if pd.notna(run_available_time) else None,
            "member_daily_max_f": member_max.to_dict(),
            "distribution": stats,
            **completeness,
        }

    all_metas = [run_metadata(rt) for rt in run_times]
    latest_seen_run = all_metas[0]
    usable_metas = [m for m in all_metas if m["usable_for_daily_max"]]
    latest_usable_run = usable_metas[0] if usable_metas else None
    previous_usable_run = usable_metas[1] if len(usable_metas) > 1 else None

    revision = None
    if latest_usable_run and previous_usable_run and latest_usable_run["distribution"] and previous_usable_run["distribution"]:
        revision = {
            "mean_daily_max_f": latest_usable_run["distribution"]["mean_daily_max_f"] - previous_usable_run["distribution"]["mean_daily_max_f"],
            "median_daily_max_f": latest_usable_run["distribution"]["median_daily_max_f"] - previous_usable_run["distribution"]["median_daily_max_f"],
        }

    newer_run_arriving = latest_usable_run is None or latest_seen_run["run_time"] != latest_usable_run["run_time"]

    return {
        "source": source_name,
        "available": True,
        "usable_for_daily_max": latest_usable_run is not None,
        "state_time": t,
        "latest_seen_run": latest_seen_run,
        "latest_usable_run": latest_usable_run,
        "previous_usable_run": previous_usable_run,
        "revision": revision,
        "newer_run_arriving": newer_run_arriving,
        "note": "Raw ensemble member frequencies are NOT calibrated probabilities.",
    }


def get_ecmwf_ensemble_state(df: pd.DataFrame, t: datetime, policy: str) -> dict:
    """Same latest_seen_run/latest_usable_run mechanics as get_ensemble_state
    (reusing assess_ensemble_run_completeness's member+forecast-point logic,
    since ECMWF ensemble is structurally identical: members x valid times).
    This project's ECMWF ensemble test intentionally sampled only 3 steps
    for 1 run against an expected 8 target-day 3-hourly marks, so
    usable_for_daily_max will correctly and automatically come back False
    -- never silently masqueraded as a complete distribution. The extra
    'completeness' field (COMPLETE/PARTIAL/INSUFFICIENT) is a coarser,
    always-present label built from the SEEN run's raw coverage, since
    latest_usable_run will typically be None for this source."""
    if df.empty or df[eligible_mask(df, t, policy)].empty:
        return {"source": "ecmwf_ensemble", "available": False, "usable_for_daily_max": False, "completeness": "INSUFFICIENT", "reason": "no eligible rows"}

    result = get_ensemble_state(df, t, policy, expected_members=EXPECTED_ECMWF_ENS_MEMBERS, source_name="ecmwf_ensemble")
    seen = result["latest_seen_run"]
    if seen is None:
        result["completeness"] = "INSUFFICIENT"
        return result

    result["n_target_day_hours_sampled"] = seen["forecast_points_available"]
    result["n_target_day_hours_possible"] = seen["forecast_points_expected"]
    result["completeness"] = (
        "COMPLETE" if seen["run_status"] == "COMPLETE"
        else "PARTIAL" if seen["forecast_points_available"] > 0
        else "INSUFFICIENT"
    )
    result["note"] += (
        " ECMWF ensemble test data covers only a SMALL SAMPLE of the target day "
        f"({seen['forecast_points_available']} of {seen['forecast_points_expected']} expected 3-hourly marks, "
        f"{seen['member_count_available']} of {seen['member_count_expected']} members) -- never treated as a "
        "complete distribution. Do not compare directly to GEFS's fuller reconstruction."
    )
    return result


# ---------------------------------------------------------------------------
# PART 10 -- observation state.
# ---------------------------------------------------------------------------

def get_observation_state(obs_df: pd.DataFrame, t: datetime, policy: str, station_ids: list[str]) -> dict:
    day_start_utc, day_end_utc = local_day_utc_bounds()
    real = obs_df[obs_df["is_real_observation"]] if "is_real_observation" in obs_df.columns else obs_df
    state = {}
    for sid in station_ids:
        g = real[real["station_id"] == sid].copy()
        g["_elig_mask"] = eligible_mask(g, t, policy)
        elig = g[g["_elig_mask"]].sort_values("observation_time")
        if elig.empty:
            state[sid] = {"available": False, "reason": "no eligible observation at this state_time"}
            continue
        latest = elig.iloc[-1]
        prev = elig.iloc[-2] if len(elig) > 1 else None
        near_1h = elig[elig["observation_time"] <= latest["observation_time"] - timedelta(hours=1)]
        obs_1h_ago = near_1h.iloc[-1] if not near_1h.empty else None

        in_day = elig[(elig["observation_time"] >= day_start_utc) & (elig["observation_time"] < day_end_utc)]
        max_so_far = None
        time_of_max = None
        if not in_day.empty and in_day["temperature_f"].notna().any():
            idx = in_day["temperature_f"].idxmax()
            max_so_far = in_day.loc[idx, "temperature_f"]
            time_of_max = in_day.loc[idx, "observation_time"]

        state[sid] = {
            "available": True,
            "latest_observation_time": latest["observation_time"],
            "observation_age": t - latest["observation_time"].to_pydatetime(),
            "current_temperature_f": latest["temperature_f"],
            "current_dewpoint_f": latest.get("dewpoint_f"),
            "current_wind_speed_ms": latest.get("wind_speed_ms"),
            "current_station_pressure_hpa": latest.get("station_pressure_hpa"),
            "max_temperature_observed_so_far_f": max_so_far,
            "time_of_max_so_far": time_of_max,
            "temperature_change_previous_observation_f": (
                latest["temperature_f"] - prev["temperature_f"] if prev is not None else None
            ),
            "temperature_change_approx_1h_f": (
                latest["temperature_f"] - obs_1h_ago["temperature_f"] if obs_1h_ago is not None else None
            ),
            "provenance": {
                "source_file": str(PATHS["observations"].relative_to(PROJECT_ROOT)),
                "n_eligible_rows": len(elig),
                "latest_row_index": int(latest.name),
            },
        }
    available_temps = {sid: s["current_temperature_f"] for sid, s in state.items() if s.get("available") and s["current_temperature_f"] is not None}
    cross_station = {}
    if available_temps:
        vals = pd.Series(available_temps.values())
        cross_station = {
            "station_count_available": len(available_temps),
            "temperature_mean_f": vals.mean(), "temperature_median_f": vals.median(),
            "temperature_min_f": vals.min(), "temperature_max_f": vals.max(),
            "temperature_range_f": vals.max() - vals.min(), "temperature_std_f": vals.std(),
        }
    return {"stations": state, "cross_station_diagnostics": cross_station}


# ---------------------------------------------------------------------------
# PART 11 -- AFD state.
# ---------------------------------------------------------------------------

def get_afd_state(afd_df: pd.DataFrame, t: datetime, policy: str) -> dict:
    mask = eligible_mask(afd_df, t, policy)
    elig = afd_df[mask].sort_values("issuance_time")
    if elig.empty:
        return {"available": False, "reason": "no eligible AFD at this state_time (policy=strict excludes AFD entirely)" if policy == STRICT_STATE else "no AFD issued yet"}
    latest = elig.iloc[-1]
    previous = elig.iloc[-2] if len(elig) > 1 else None
    return {
        "available": True,
        "latest_product_id": latest["product_id"],
        "issuance_time": latest["issuance_time"],
        "afd_age": t - latest["issuance_time"].to_pydatetime(),
        "raw_text": latest["raw_text"],
        "previous_product_id": previous["product_id"] if previous is not None else None,
        "previous_issuance_time": previous["issuance_time"] if previous is not None else None,
        "previous_raw_text": previous["raw_text"] if previous is not None else None,
        "availability_status": AVAIL_ISSUANCE_ONLY,
        "note": "issuance_time used as a labeled ASSUMPTION-proxy for availability (PROXY_STATE only); "
                "never treated as a proven historical availability time.",
        "provenance": {"source_file": str(PATHS["afd"].relative_to(PROJECT_ROOT)), "row_index": int(latest.name)},
    }


# ---------------------------------------------------------------------------
# PART 12/13 -- cross-model diagnostics.
# ---------------------------------------------------------------------------

def compute_cross_model_diagnostics(state: dict) -> dict:
    """Only ever reads from latest_usable_run -- an incomplete
    latest_seen_run must never leak into cross-model comparison."""
    predictions = {}
    for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic"]:
        s = state.get(key)
        if s and s.get("usable_for_daily_max") and s.get("latest_usable_run", {}).get("predicted_daily_max_f") is not None:
            predictions[key] = s["latest_usable_run"]["predicted_daily_max_f"]
    gefs = state.get("gefs")
    if gefs and gefs.get("usable_for_daily_max") and gefs.get("latest_usable_run", {}).get("distribution"):
        predictions["gefs_median"] = gefs["latest_usable_run"]["distribution"].get("median_daily_max_f")

    diag = {"individual_predictions_f": predictions}
    if predictions:
        vals = pd.Series(predictions.values())
        diag["forecast_count"] = len(vals)
        diag["forecast_mean_f"] = vals.mean()
        diag["forecast_median_f"] = vals.median()
        diag["forecast_min_f"] = vals.min()
        diag["forecast_max_f"] = vals.max()
        diag["forecast_range_f"] = vals.max() - vals.min()
        diag["forecast_std_f"] = vals.std()

    pairwise = {}
    keys = list(predictions.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            pairwise[f"{a}_minus_{b}"] = predictions[a] - predictions[b]
    diag["pairwise_disagreement_f"] = pairwise
    return diag


# ---------------------------------------------------------------------------
# PART 14 -- target-day progress.
# ---------------------------------------------------------------------------

def get_target_day_progress(t: datetime) -> dict:
    day_start_utc, day_end_utc = local_day_utc_bounds()
    t_local = t.astimezone(NYC_TZ)
    local_midnight = datetime(t_local.year, t_local.month, t_local.day, 0, 0, tzinfo=NYC_TZ)
    return {
        "state_time_utc": t,
        "state_time_local": t_local,
        "minutes_since_local_midnight": (t_local - local_midnight).total_seconds() / 60,
        "hours_until_end_of_local_day": (day_end_utc - t).total_seconds() / 3600,
    }


# ---------------------------------------------------------------------------
# PART 6 -- top-level weather_state(t).
# ---------------------------------------------------------------------------

STATION_IDS = ["725053-94728", "725030-14732", "744860-94789", "725020-14734"]
STATION_LABELS = {"725053-94728": "KNYC", "725030-14732": "KLGA", "744860-94789": "KJFK", "725020-14734": "KEWR"}


def get_weather_state(sources: LoadedSources, t: datetime, policy: str = PROXY_STATE) -> dict:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)

    state = {
        "state_time": t,
        "policy": policy,
        "hrrr": get_latest_forecast(sources.hrrr, t, policy, "hrrr"),
        "gfs": get_latest_forecast(sources.gfs, t, policy, "gfs"),
        "nbm": get_latest_forecast(sources.nbm, t, policy, "nbm"),
        "ecmwf_deterministic": get_latest_forecast(sources.ecmwf_det, t, policy, "ecmwf_deterministic"),
        "gefs": get_ensemble_state(sources.gefs, t, policy),
        "ecmwf_ensemble": get_ecmwf_ensemble_state(sources.ecmwf_ens, t, policy),
        "observations": get_observation_state(sources.observations, t, policy, STATION_IDS),
        "afd": get_afd_state(sources.afd, t, policy),
        "target_day_progress": get_target_day_progress(t),
    }
    state["cross_model_diagnostics"] = compute_cross_model_diagnostics(state)
    highest_obs = [
        s["max_temperature_observed_so_far_f"]
        for s in state["observations"]["stations"].values()
        if s.get("available") and s.get("max_temperature_observed_so_far_f") is not None
    ]
    state["target_day_progress"]["highest_observed_temperature_any_station_so_far_f"] = max(highest_obs) if highest_obs else None
    return state


# ---------------------------------------------------------------------------
# PART 16 -- no-lookahead audit.
# ---------------------------------------------------------------------------

def validate_no_lookahead(state: dict) -> dict:
    """Checks EVERY run reference the state exposes -- latest_seen_run
    (raw provenance, still must not be from the future) as well as
    latest_usable_run / previous_usable_run -- plus the new
    'a run was actually COMPLETE by t' requirement Part 11 adds."""
    t = state["state_time"]
    checks = {}

    def check(name, ok):
        checks[name] = "PASS" if ok else "FAIL"

    def check_run_meta(prefix, meta):
        if meta is None:
            return
        check(f"{prefix}_run_time_le_t", meta["run_time"] <= pd.Timestamp(t))
        avail = meta.get("run_available_time")
        check(f"{prefix}_available_time_le_t", pd.isna(avail) or avail <= pd.Timestamp(t))

    for key in ["hrrr", "gfs", "nbm", "ecmwf_deterministic", "gefs", "ecmwf_ensemble"]:
        s = state.get(key, {})
        check_run_meta(f"{key}_latest_seen", s.get("latest_seen_run"))
        check_run_meta(f"{key}_latest_usable", s.get("latest_usable_run"))
        check_run_meta(f"{key}_previous_usable", s.get("previous_usable_run"))
        usable = s.get("latest_usable_run")
        if usable is not None:
            check(f"{key}_latest_usable_was_actually_complete", usable.get("usable_for_daily_max") is True)

    for sid, s in state.get("observations", {}).get("stations", {}).items():
        if s.get("available"):
            check(f"observation_{sid}_time_le_t", s["latest_observation_time"] <= pd.Timestamp(t))

    afd = state.get("afd", {})
    if afd.get("available"):
        check("afd_issuance_time_le_t", afd["issuance_time"] <= pd.Timestamp(t))

    all_pass = all(v == "PASS" for v in checks.values())
    return {"state_time": t, "overall": "PASS" if all_pass else "FAIL", "checks": checks}
