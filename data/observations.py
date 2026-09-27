"""Shared helpers for reading NOAA NCEI's Integrated Surface Database (ISD)
"Global Hourly" archive -- historical surface weather OBSERVATIONS (not
forecasts) for NYC-area stations.

Source (verified live 2025-09-26): s3://noaa-global-hourly-pds (CSV format
of ISD), fully public HTTPS, no AWS credentials required.
Registry: https://registry.opendata.aws/noaa-isd/
Format docs consulted (only partially extractable in this environment --
see module-level notes below on what could and could not be verified):
  https://www.ncei.noaa.gov/data/global-hourly/doc/CSV_HELP.pdf
Station master list (official, verified live):
  https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv

This module only READS the archive. It does not touch Kalshi/HRRR/GFS/GEFS
data, Jev, Laya, or any trading logic, and it does not decide a Kalshi
settlement station.

Key structure (OBSERVED FROM ARCHIVE, verified live for 2025):
  s3://noaa-global-hourly-pds/{year}/{USAF}{WBAN}.csv
  -- ONE CSV per station per YEAR (not per day), covering all report types
  for that station for the whole year. No .idx/byte-range mechanism exists
  or is needed here: these files are already small (2-6 MB for a full
  year), unlike HRRR/GFS/GEFS's multi-hundred-MB GRIB2 files.

Field encoding (DOCUMENTED, confirmed via CSV_HELP.pdf where it rendered,
cross-checked against real 2025 data for these stations):
  TMP, DEW, SLP: "<value>,<quality_flag>", value in TENTHS of the stated
    unit (Celsius for TMP/DEW, hectopascals for SLP), missing = +9999 (or
    -9999 for SLP-style fields).
  WND: "<direction_deg>,<dir_quality>,<type_code>,<speed_tenths_mps>,<speed_quality>"
  MA1: "<altimeter_tenths_hPa>,<quality>,<station_pressure_tenths_hPa>,<quality>"
  GA1: "<sky_cover_code>,<quality>,<base_height_tenths_m>,<quality>,<cloud_type>,<quality>"
  AA1: "<period_hours>,<depth_tenths_mm>,<condition_code>,<quality>"
  OC1: "<gust_speed_tenths_mps>,<quality>" (often blank -- gust is only
    reported when one occurred)
  RH1: relative humidity additional-data group -- OBSERVED EMPTY for every
    row tested at all 4 NYC-area stations (US ASOS/METAR-sourced reports
    apparently don't populate it here); relative_humidity is therefore
    left null in the canonical dataset rather than derived from temp/dewpoint,
    per the "prefer null to fabrication / label derived fields" rule.

Report types (DOCUMENTED via WMO international code-form designators, a
standard independent of NOAA): FM-15 = METAR (routine, ~hourly), FM-16 =
SPECI (special, irregular timing, triggered by significant changes).
SOD/SOM ("Summary of Day"/"Summary of Month") are NOAA/ISD administrative
placeholder rows, OBSERVED to carry all-missing sentinel values for every
field at these stations -- excluded from "real observation" counts here.

Quality control (OBSERVED + PARTIALLY DOCUMENTED): the QUALITY_CONTROL
column (e.g. "V020") identifies the NOAA QC *software version* applied to
the whole file -- this confirms the archive is QUALITY-CONTROLLED, not raw
untouched telegraphic reports (this project's Part 7 category B, not A).
Per-field quality flag digits (e.g. "5", "9" appended to TMP/DEW/etc.)
are preserved verbatim; a full numeric flag-meaning table could NOT be
extracted from NOAA's own PDF documentation in this environment (it did
not render as text), so beyond "9 = missing" (empirically confirmed via
the all-missing SOD/SOM rows) the exact meaning of every digit is reported
as UNKNOWN rather than guessed.

Observation timestamps (DOCUMENTED via WMO METAR/SYNOP convention -- these
report formats transmit time in UTC by international standard): the DATE
column is UTC, not local time. This module never assumes otherwise.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BUCKET = "noaa-global-hourly-pds"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"

NYC_TZ = ZoneInfo("America/New_York")

# Discovered live from https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv
# (official NOAA station master list) on 2025-09-26. These are the CURRENT
# (2025-active) station records for each airport/park; note several of
# these locations have HAD OTHER station-id records historically that
# ended before this one began -- see Part 18 in the test script for the
# station-continuity finding this produced for Central Park specifically.
STATIONS = [
    {
        "usaf": "725053", "wban": "94728", "station_id": "725053-94728",
        "name": "CENTRAL PARK", "icao": "KNYC",
        "lat": 40.779, "lon": -73.969, "elevation_m": 42.7,
        "begin": "2005-01-01", "end": "2025-08-27",
    },
    {
        "usaf": "725030", "wban": "14732", "station_id": "725030-14732",
        "name": "LA GUARDIA AIRPORT", "icao": "KLGA",
        "lat": 40.779, "lon": -73.880, "elevation_m": 3.0,
        "begin": "1973-01-01", "end": "2025-08-27",
    },
    {
        "usaf": "744860", "wban": "94789", "station_id": "744860-94789",
        "name": "JOHN F KENNEDY INTERNATIONAL AIRPORT", "icao": "KJFK",
        "lat": 40.639, "lon": -73.764, "elevation_m": 2.7,
        "begin": "1973-01-01", "end": "2025-08-27",
    },
    {
        "usaf": "725020", "wban": "14734", "station_id": "725020-14734",
        "name": "NEWARK LIBERTY INTERNATIONAL AP", "icao": "KEWR",
        "lat": 40.683, "lon": -74.169, "elevation_m": 2.0,
        "begin": "1973-01-01", "end": "2025-08-25",
    },
]

MISSING_INT_SENTINELS = {9999, -9999, 999, -999}


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def station_key(year: int, usaf: str, wban: str) -> str:
    return f"{year}/{usaf}{wban}.csv"


def fetch_station_year(year: int, usaf: str, wban: str, stats: RequestStats | None = None) -> str:
    """Fetch one station's full-year CSV. No partial-access mechanism
    exists (or is needed) for this archive -- files are already small."""
    key = station_key(year, usaf, wban)
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/{key}", timeout=60)
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    r.raise_for_status()
    return r.text, key


def _scaled(raw: str, scale: float = 10.0):
    """Parse a signed integer ISD sub-field, applying the documented /10
    scaling, returning NaN (not None) for the documented missing sentinel.

    ISD's missing sentinel is "all nines" at whatever digit-width that
    particular sub-field uses -- confirmed empirically against this
    project's own known-all-missing SOD/SOM placeholder rows: TMP/DEW/WND-
    speed use 9999, SLP/MA1 pressure fields use 99999, VIS uses 999999.
    An earlier version of this function used a single `abs(v) >= 9999`
    threshold, which incorrectly flagged legitimate 5-digit pressure
    values (e.g. 10071 tenths-hPa = 1007.1 hPa) as missing -- checking for
    "all nines" instead of a magnitude threshold fixes this generally,
    without needing a separate hardcoded sentinel per field width.

    NaN (not None) keeps pandas columns float64 so downstream
    .diff()/.cummax() work without silently becoming object-dtype.
    """
    if raw is None:
        return math.nan
    raw = raw.strip()
    if not raw:
        return math.nan
    digits_only = raw.lstrip("+-")
    if digits_only and set(digits_only) == {"9"}:
        return math.nan
    try:
        v = int(raw)
    except ValueError:
        return math.nan
    return v / scale


def parse_isd_row(row: dict) -> dict:
    """Decode one raw ISD CSV row into the fields this project's canonical
    schema needs, preserving every raw source string alongside the decoded
    value. Returns None fields (never fabricated values) where a field is
    blank/missing in the source."""
    date_str = row.get("DATE", "")
    observation_time = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc) if date_str else None

    def split_flag(raw_field: str, idx_value: int = 0, idx_flag: int = 1):
        parts = raw_field.split(",")
        if len(parts) <= max(idx_value, idx_flag):
            return None, None
        return parts[idx_value], parts[idx_flag]

    tmp_raw = row.get("TMP", "")
    tmp_val_raw, tmp_flag = split_flag(tmp_raw)
    temperature_c = _scaled(tmp_val_raw) if tmp_val_raw is not None else math.nan

    dew_raw = row.get("DEW", "")
    dew_val_raw, dew_flag = split_flag(dew_raw)
    dewpoint_c = _scaled(dew_val_raw) if dew_val_raw is not None else math.nan

    wnd_parts = row.get("WND", "").split(",")
    wind_direction = math.nan
    wind_speed_ms = math.nan
    if len(wnd_parts) >= 5:
        try:
            wd = int(wnd_parts[0])
            wind_direction = wd if wd not in MISSING_INT_SENTINELS else math.nan
        except ValueError:
            pass
        wind_speed_ms = _scaled(wnd_parts[3])

    slp_raw = row.get("SLP", "")
    slp_val_raw, slp_flag = split_flag(slp_raw)
    sea_level_pressure_hpa = _scaled(slp_val_raw) if slp_val_raw is not None else math.nan

    ma1_parts = row.get("MA1", "").split(",")
    station_pressure_hpa = math.nan
    if len(ma1_parts) >= 4:
        station_pressure_hpa = _scaled(ma1_parts[2])

    oc1_parts = row.get("OC1", "").split(",")
    wind_gust_ms = _scaled(oc1_parts[0]) if oc1_parts and oc1_parts[0] not in ("", None) else math.nan

    aa1_parts = row.get("AA1", "").split(",")
    precip_mm = math.nan
    if len(aa1_parts) >= 2:
        precip_mm = _scaled(aa1_parts[1])

    vis_parts = row.get("VIS", "").split(",")
    visibility_m = _scaled(vis_parts[0], scale=1.0) if vis_parts and vis_parts[0] not in ("", None) else math.nan

    return {
        "observation_time": observation_time,
        "report_type_raw": row.get("REPORT_TYPE", "").strip(),
        "quality_control_version": row.get("QUALITY_CONTROL", "").strip(),
        "source_code": row.get("SOURCE", "").strip(),
        "temperature_raw": tmp_raw,
        "temperature_c": temperature_c,
        "temperature_f": (temperature_c * 9 / 5 + 32) if temperature_c is not None and not math.isnan(temperature_c) else math.nan,
        "temperature_qc_flag": tmp_flag,
        "dewpoint_raw": dew_raw,
        "dewpoint_c": dewpoint_c,
        "dewpoint_f": (dewpoint_c * 9 / 5 + 32) if dewpoint_c is not None and not math.isnan(dewpoint_c) else math.nan,
        "dewpoint_qc_flag": dew_flag,
        "relative_humidity": None,  # not directly reported -- see module docstring
        "wind_direction_deg": wind_direction,
        "wind_speed_ms": wind_speed_ms,
        "wind_gust_ms": wind_gust_ms,
        "station_pressure_hpa": station_pressure_hpa,
        "sea_level_pressure_hpa": sea_level_pressure_hpa,
        "precipitation_mm": precip_mm,
        "cloud_cover_raw": row.get("GA1", "").strip() or None,
        "visibility_m": visibility_m,
        "weather_codes_raw": (row.get("AW1", "").strip() or row.get("MW1", "").strip() or None),
        "quality_flags_raw": {"tmp": tmp_flag, "dew": dew_flag, "slp": slp_flag},
    }


def is_real_observation(report_type_raw: str) -> bool:
    """SOD/SOM are administrative daily/monthly summary placeholders, not
    real point-in-time observations -- OBSERVED to carry all-missing
    sentinel values at every NYC-area station tested."""
    return report_type_raw not in ("SOD", "SOM")


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
