"""Loads the FROZEN, block-scoped HISTORICAL BACKFILL sources (HRRR/GFS/NBM/
GEFS/ECMWF/observations/AFD) for the point-in-time integration layer.

Mirrors data/pilot_loader.py EXACTLY (same column normalization, same
HRRR+RH merge, same station/variable handling) -- the ONLY difference is
reading from data/processed/backfill/{block_id}/{source}/ instead of
data/processed/pilot/{source}/. This is a deliberate duplication, not a
refactor of pilot_loader.py: the pilot's own loader must stay byte-for-byte
unchanged (it is still the frozen, approved reference implementation this
module is required to match), per "preserve the approved source methodology
from 8af8d30."

HRRR APCP_1H is intentionally NOT merged into `hrrr` here, matching
pilot_loader.py's own existing scope (it also never merges APCP_1H) -- this
preserves the already-approved pipeline-survival-fix scope exactly, without
silently expanding it during the backfill.

ECMWF may be entirely or partially absent for blocks before its validated
archive-path regime (2024-02-29, see scripts/backfill_common.ECMWF_VALID_FROM)
-- _load_gridded() returns a correctly-shaped EMPTY dataframe in that case
(never raises), so every downstream function sees "no rows available" (the
same shape as any other source's ordinary "not published yet" case) rather
than crashing.
"""
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKFILL_ROOT = PROJECT_ROOT / "data/processed/backfill"

GRIDDED_SOURCES = ["hrrr", "gfs", "nbm", "gefs", "ecmwf"]

_EMPTY_GRIDDED_COLUMNS = [
    "source", "run_time", "valid_time", "available_time", "availability_time_type",
    "forecast_hour", "variable", "level", "temporal_stat", "temporal_window_hours",
    "requested_lat", "requested_lon", "grid_lat", "grid_lon", "distance_km",
    "is_primary_central_park_grid", "value", "units", "value_f", "source_object",
]


def _empty_gridded(name: str) -> pd.DataFrame:
    df = pd.DataFrame(columns=_EMPTY_GRIDDED_COLUMNS)
    df["run_time"] = pd.to_datetime(df["run_time"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
    df["available_time_resolved"] = pd.to_datetime(pd.Series(dtype="object"), utc=True)
    df["availability_status"] = "S3_LAST_MODIFIED_PROXY"
    df["availability_confidence"] = "LOW" if name == "ecmwf" else "NORMAL"
    df["ensemble_member"] = None
    df["member_type"] = None
    df["source_name"] = "ecmwf_deterministic" if name == "ecmwf" else name
    return df


def _load_gridded(block_id: str, name: str) -> pd.DataFrame:
    path = BACKFILL_ROOT / block_id / name / "parts"
    if not path.exists() or not any(path.glob("*.parquet")):
        return _empty_gridded(name)
    df = pd.read_parquet(path, filters=[("is_primary_central_park_grid", "==", True)])
    if df.empty:
        return _empty_gridded(name)
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


def _load_hrrr_with_rh(block_id: str) -> pd.DataFrame:
    base = _load_gridded(block_id, "hrrr")
    rh_dir = BACKFILL_ROOT / block_id / "hrrr_rh" / "parts"
    if not rh_dir.exists() or not any(rh_dir.glob("*.parquet")):
        return base
    rh = pd.read_parquet(rh_dir, filters=[("is_primary_central_park_grid", "==", True)])
    if rh.empty:
        return base
    rh["run_time"] = pd.to_datetime(rh["run_time"], utc=True)
    rh["valid_time"] = pd.to_datetime(rh["valid_time"], utc=True)
    rh["available_time_resolved"] = pd.to_datetime(rh["available_time"], utc=True)
    rh["availability_status"] = "S3_LAST_MODIFIED_PROXY"
    rh["availability_confidence"] = "NORMAL"
    rh["ensemble_member"] = None
    rh["member_type"] = None
    rh["source_name"] = "hrrr"
    for col in base.columns:
        if col not in rh.columns:
            rh[col] = None
    rh = rh[base.columns]
    return pd.concat([base, rh], ignore_index=True)


def _load_observations(block_id: str) -> pd.DataFrame:
    path = BACKFILL_ROOT / block_id / "observations" / "backfill_observations_canonical.parquet"
    df = pd.read_parquet(path)
    df["observation_time"] = pd.to_datetime(df["observation_time"], utc=True)
    df = df[df["is_real_observation"] == True].copy()  # noqa: E712
    df["availability_status"] = "OBSERVATION_TIME_ONLY"
    df["source_name"] = "observations"
    return df


def _load_afd(block_id: str) -> pd.DataFrame:
    path = BACKFILL_ROOT / block_id / "afd" / "backfill_afd_documents.parquet"
    df = pd.read_parquet(path)
    df["issuance_time"] = pd.to_datetime(df["issuance_time"], utc=True)
    df["availability_status"] = "ISSUANCE_TIME_ONLY"
    df["source_name"] = "afd"
    return df


@dataclass
class BackfillSources:
    hrrr: pd.DataFrame
    gfs: pd.DataFrame
    nbm: pd.DataFrame
    gefs: pd.DataFrame
    ecmwf_det: pd.DataFrame
    observations: pd.DataFrame
    afd: pd.DataFrame


_CACHE: dict[str, BackfillSources] = {}


def load_backfill_sources(block_id: str, use_cache: bool = True) -> BackfillSources:
    if use_cache and block_id in _CACHE:
        return _CACHE[block_id]
    sources = BackfillSources(
        hrrr=_load_hrrr_with_rh(block_id),
        gfs=_load_gridded(block_id, "gfs"),
        nbm=_load_gridded(block_id, "nbm"),
        gefs=_load_gridded(block_id, "gefs"),
        ecmwf_det=_load_gridded(block_id, "ecmwf"),
        observations=_load_observations(block_id),
        afd=_load_afd(block_id),
    )
    _CACHE[block_id] = sources
    return sources


STATION_ICAO_TARGET = "KNYC"
STATION_ICAO_AUX = ["KLGA", "KJFK", "KEWR"]
