"""Loads the FROZEN, production-scale pilot datasets (HRRR/GFS/NBM/GEFS/
ECMWF/observations/AFD) for the point-in-time integration layer.

This is intentionally separate from data/weather_state.py's PATHS/
load_all_sources(), which read the much smaller single-day FEASIBILITY-TEST
canonical CSVs (data/processed/weather/{source}/test/...) -- those were
appropriate for the pre-pilot architecture design phase, but the 40-day
integration must consume the actual frozen, validated, production-scale
pilot output (data/processed/pilot/{source}/parts/*.parquet), which did not
exist yet when weather_state.py's loading layer was first written.

Only the primary Central Park grid point (is_primary_central_park_grid=True)
is loaded for every gridded source -- the 3x3 patch's other 8 points exist
for grid-validation purposes (already audited per-source) and are not needed
for point-in-time feature construction at Central Park.
"""
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PILOT_ROOT = PROJECT_ROOT / "data/processed/pilot"

GRIDDED_SOURCES = ["hrrr", "gfs", "nbm", "gefs", "ecmwf"]


def _load_gridded(name: str) -> pd.DataFrame:
    path = PILOT_ROOT / name / "parts"
    df = pd.read_parquet(path, filters=[("is_primary_central_park_grid", "==", True)])
    df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
    df["available_time_resolved"] = pd.to_datetime(df["available_time"], utc=True)
    df["availability_status"] = "S3_LAST_MODIFIED_PROXY"
    if "availability_confidence" not in df.columns:
        df["availability_confidence"] = "LOW" if name == "ecmwf" else "NORMAL"
    if "temporal_stat" not in df.columns:
        df["temporal_stat"] = None
        df["temporal_window_hours"] = None
    if "ensemble_member" not in df.columns:
        df["ensemble_member"] = None
        df["member_type"] = None
    df["source_name"] = "ecmwf_deterministic" if name == "ecmwf" else name
    return df


def _load_hrrr_with_rh() -> pd.DataFrame:
    """HRRR's main pilot (data/processed/pilot/hrrr/) plus the additive RH
    backfill (data/processed/pilot/hrrr_rh/, scripts/pilot_phase3c_hrrr_rh.py,
    2026-09-29 pipeline-survival fix) -- concatenated into ONE dataframe so
    every downstream function (eligible_mask, atmospheric-trajectory
    extraction) sees RH as just another native HRRR variable, identical in
    every other respect (schema, availability methodology, grid) to
    TMP/DPT/UGRD/etc. Never merged with or used to derive GFS/NBM/GEFS RH --
    each source's RH is independently extracted and kept source-tagged."""
    base = _load_gridded("hrrr")
    rh_dir = PILOT_ROOT / "hrrr_rh" / "parts"
    if not rh_dir.exists() or not any(rh_dir.glob("*.parquet")):
        return base
    rh = pd.read_parquet(rh_dir, filters=[("is_primary_central_park_grid", "==", True)])
    rh["run_time"] = pd.to_datetime(rh["run_time"], utc=True)
    rh["valid_time"] = pd.to_datetime(rh["valid_time"], utc=True)
    rh["available_time_resolved"] = pd.to_datetime(rh["available_time"], utc=True)
    rh["availability_status"] = "S3_LAST_MODIFIED_PROXY"
    rh["availability_confidence"] = "NORMAL"
    rh["ensemble_member"] = None
    rh["member_type"] = None
    rh["source_name"] = "hrrr"
    # Align columns exactly (both frames must have identical columns to concat cleanly).
    for col in base.columns:
        if col not in rh.columns:
            rh[col] = None
    rh = rh[base.columns]
    return pd.concat([base, rh], ignore_index=True)


def _load_observations() -> pd.DataFrame:
    df = pd.read_parquet(PILOT_ROOT / "observations" / "pilot_observations_canonical.parquet")
    df["observation_time"] = pd.to_datetime(df["observation_time"], utc=True)
    df = df[df["is_real_observation"] == True].copy()  # noqa: E712 -- exclude SOD/SOM placeholder rows
    df["availability_status"] = "OBSERVATION_TIME_ONLY"
    df["source_name"] = "observations"
    return df


def _load_afd() -> pd.DataFrame:
    df = pd.read_parquet(PILOT_ROOT / "afd" / "pilot_afd_documents.parquet")
    df["issuance_time"] = pd.to_datetime(df["issuance_time"], utc=True)
    df["availability_status"] = "ISSUANCE_TIME_ONLY"
    df["source_name"] = "afd"
    return df


@dataclass
class PilotSources:
    hrrr: pd.DataFrame
    gfs: pd.DataFrame
    nbm: pd.DataFrame
    gefs: pd.DataFrame
    ecmwf_det: pd.DataFrame
    observations: pd.DataFrame
    afd: pd.DataFrame


_CACHE: PilotSources | None = None


def load_pilot_sources(use_cache: bool = True) -> PilotSources:
    """Loads every frozen pilot source once. Cached in-process (this is
    ~1.1M rows total across all gridded sources, ~70s to load cold) --
    re-running build_integrated_pilot.py within one process should not
    re-read from disk for every one of the 480 states."""
    global _CACHE
    if use_cache and _CACHE is not None:
        return _CACHE
    sources = PilotSources(
        hrrr=_load_hrrr_with_rh(),
        gfs=_load_gridded("gfs"),
        nbm=_load_gridded("nbm"),
        gefs=_load_gridded("gefs"),
        ecmwf_det=_load_gridded("ecmwf"),
        observations=_load_observations(),
        afd=_load_afd(),
    )
    _CACHE = sources
    return sources


STATION_ICAO_TARGET = "KNYC"
STATION_ICAO_AUX = ["KLGA", "KJFK", "KEWR"]
