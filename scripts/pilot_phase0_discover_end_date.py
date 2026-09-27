"""PILOT Phase 0 -- discover PILOT_END_DATE: the latest common usable ISD
observation date across KNYC/KLGA/KJFK/KEWR, starting from 2025-01-01.
Downloads only the 2025 annual file per station (already small, ~4-8MB,
one request each per data/observations.py's existing fetch_station_year)
-- this is the KEEP_RAW policy source, not a bulk numerical extraction.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.observations import STATIONS, fetch_station_year, parse_isd_row, is_real_observation, RequestStats

PILOT_START_DATE = "2025-01-01"
OUT_DIR = Path(__file__).resolve().parents[1] / "data/processed/pilot/observations"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR = Path(__file__).resolve().parents[1] / "data/raw/pilot/observations"
RAW_DIR.mkdir(parents=True, exist_ok=True)


def main():
    stats = RequestStats()
    per_station = {}
    for st in STATIONS:
        icao = st["icao"] if "icao" in st else st.get("name")
        print(f"Fetching 2025 annual file for {icao} ({st['station_id']})...")
        text, key = fetch_station_year(2025, st["usaf"], st["wban"], stats=stats)
        raw_path = RAW_DIR / f"{st['station_id']}_2025.csv"
        raw_path.write_text(text, encoding="utf-8")

        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(text)))
        parsed = [parse_isd_row(r) for r in rows]
        real = [p for p in parsed if is_real_observation(p["report_type_raw"])]
        temp_obs = [p for p in real if p.get("temperature_c") == p.get("temperature_c")]  # not NaN

        obs_times = sorted(p["observation_time"] for p in real if p.get("observation_time"))
        temp_times = sorted(p["observation_time"] for p in temp_obs if p.get("observation_time"))

        last_obs = obs_times[-1] if obs_times else None
        last_temp = temp_times[-1] if temp_times else None
        last_full_date = last_temp.date() if last_temp else None

        per_station[st["station_id"]] = {
            "icao": icao,
            "total_rows": len(rows),
            "real_observations": len(real),
            "temperature_observations": len(temp_obs),
            "first_observation_time": str(obs_times[0]) if obs_times else None,
            "last_observation_time": str(last_obs) if last_obs else None,
            "last_temperature_observation_time": str(last_temp) if last_temp else None,
            "last_full_date_with_temp": str(last_full_date) if last_full_date else None,
        }
        print(f"  rows={len(rows)} real_obs={len(real)} temp_obs={len(temp_obs)} last_temp_time={last_temp}")

    print(f"\nTotal requests: {stats.n_requests}, bytes downloaded: {stats.bytes_downloaded/1e6:.2f} MB")

    # PILOT_END_DATE = latest date such that EVERY station has temperature
    # data for that FULL calendar date (i.e., min across stations of their
    # last complete date, conservatively using the date of last temp obs
    # minus 1 day to avoid a partially-covered final day).
    from datetime import timedelta

    candidate_dates = []
    for sid, info in per_station.items():
        if info["last_full_date_with_temp"] is None:
            candidate_dates.append(None)
        else:
            from datetime import date
            y, m, d = map(int, info["last_full_date_with_temp"].split("-"))
            candidate_dates.append(date(y, m, d))

    if any(c is None for c in candidate_dates):
        print("ERROR: at least one station has no temperature observations in 2025 -- cannot determine PILOT_END_DATE")
        pilot_end_date = None
    else:
        min_last_full_date = min(candidate_dates)
        # Conservative: back off one day from the minimum last-observed date,
        # since the final day in an annual file may be only partially reported.
        pilot_end_date = min_last_full_date - timedelta(days=1)

    result = {
        "pilot_start_date": PILOT_START_DATE,
        "pilot_end_date": str(pilot_end_date) if pilot_end_date else None,
        "per_station": per_station,
        "method": "min over stations of (last date with a real temperature observation in the 2025 ISD annual file), minus 1 day as a conservative margin against a partially-reported final day",
    }
    with open(OUT_DIR / "pilot_end_date_discovery.json", "w") as f:
        json.dump(result, f, indent=2, default=str)

    print(f"\nPILOT_START_DATE = {PILOT_START_DATE}")
    print(f"PILOT_END_DATE = {pilot_end_date}")
    print(f"Discovery record written to {OUT_DIR / 'pilot_end_date_discovery.json'}")


def _split_csv_line(line: str) -> list[str]:
    """Minimal CSV split respecting double-quoted fields (ISD fields can
    contain commas inside quotes, e.g. some remarks fields)."""
    import csv
    import io

    return next(csv.reader(io.StringIO(line)))


if __name__ == "__main__":
    main()
