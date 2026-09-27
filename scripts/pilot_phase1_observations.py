"""PILOT Phase 1 -- ISD surface observations for KNYC/KLGA/KJFK/KEWR,
2025-01-01 to PILOT_END_DATE. Reuses the raw annual files already fetched
in Phase 0 (idempotent -- does not re-download). Builds the canonical
processed Parquet restricted to the pilot window and the required
observation validation audit.
"""
import json
import sys
import csv
import io
import math
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.observations import STATIONS, fetch_station_year, parse_isd_row, is_real_observation, RequestStats

PILOT_START_DATE = date(2025, 1, 1)
PILOT_END_DATE = date(2025, 8, 24)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data/raw/pilot/observations"
OUT_DIR = ROOT / "data/processed/pilot/observations"
RAW_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_or_fetch_year(station, year, stats):
    raw_path = RAW_DIR / f"{station['station_id']}_{year}.csv"
    if raw_path.exists():
        return raw_path.read_text(encoding="utf-8")
    text, _ = fetch_station_year(year, station["usaf"], station["wban"], stats=stats)
    raw_path.write_text(text, encoding="utf-8")
    return text


def main():
    stats = RequestStats()
    all_frames = []
    manifest = []
    coverage_rows = []

    for st in STATIONS:
        text = load_or_fetch_year(st, 2025, stats)
        rows = list(csv.DictReader(io.StringIO(text)))
        parsed = [parse_isd_row(r) for r in rows]
        for p in parsed:
            p["station_id"] = st["station_id"]
            p["station_icao"] = st["icao"]
            p["station_lat"] = st["lat"]
            p["station_lon"] = st["lon"]
            p["station_elevation_m"] = st["elevation_m"]
            p["is_real_observation"] = is_real_observation(p["report_type_raw"])

        df = pd.DataFrame(parsed)
        df = df[df["observation_time"].notna()]
        df["obs_date"] = df["observation_time"].dt.date
        in_window = df[(df["obs_date"] >= PILOT_START_DATE) & (df["obs_date"] <= PILOT_END_DATE)].copy()
        real = in_window[in_window["is_real_observation"]]

        all_frames.append(in_window)

        manifest.append(
            {
                "station_id": st["station_id"], "icao": st["icao"],
                "total_rows_in_pilot_window": len(in_window),
                "real_observation_rows": len(real),
                "status": "DONE",
            }
        )

        # Per-day audit.
        n_days = (PILOT_END_DATE - PILOT_START_DATE).days + 1
        all_dates = [PILOT_START_DATE + timedelta(days=i) for i in range(n_days)]
        real_by_day = {d: g for d, g in real.groupby("obs_date")}
        for d in all_dates:
            g = real_by_day.get(d)
            if g is None or g.empty:
                coverage_rows.append(
                    {
                        "station_id": st["station_id"], "date": str(d),
                        "observation_count": 0, "temperature_observation_count": 0,
                        "first_timestamp": None, "last_timestamp": None,
                        "max_temperature_f": None, "time_of_max": None,
                        "largest_gap_minutes": None, "flag": "ZERO_OBSERVATION_DAY",
                    }
                )
                continue
            g = g.sort_values("observation_time")
            temp_g = g[g["temperature_f"].notna()]
            gaps = g["observation_time"].diff().dt.total_seconds().dropna() / 60
            max_gap = gaps.max() if not gaps.empty else None
            max_temp_row = temp_g.loc[temp_g["temperature_f"].idxmax()] if not temp_g.empty else None
            flags = []
            if len(g) < 12:
                flags.append("SPARSE_DAY")
            if temp_g.empty:
                flags.append("ZERO_TEMPERATURE_DAY")
            if max_temp_row is not None and (max_temp_row["temperature_f"] > 120 or max_temp_row["temperature_f"] < -40):
                flags.append("IMPLAUSIBLE_TEMPERATURE")
            if max_gap is not None and max_gap > 180:
                flags.append("LARGE_GAP")
            dup_count = g.duplicated(subset=["observation_time"]).sum()
            if dup_count > 0:
                flags.append(f"DUPLICATES({dup_count})")
            coverage_rows.append(
                {
                    "station_id": st["station_id"], "date": str(d),
                    "observation_count": len(g), "temperature_observation_count": len(temp_g),
                    "first_timestamp": str(g["observation_time"].iloc[0]),
                    "last_timestamp": str(g["observation_time"].iloc[-1]),
                    "max_temperature_f": float(max_temp_row["temperature_f"]) if max_temp_row is not None else None,
                    "time_of_max": str(max_temp_row["observation_time"]) if max_temp_row is not None else None,
                    "largest_gap_minutes": float(max_gap) if max_gap is not None else None,
                    "flag": ";".join(flags) if flags else "OK",
                }
            )
        print(f"{st['icao']}: {len(in_window)} rows in pilot window, {len(real)} real observations")

    combined = pd.concat(all_frames, ignore_index=True)
    combined.to_parquet(OUT_DIR / "pilot_observations_canonical.parquet", index=False)

    coverage_df = pd.DataFrame(coverage_rows)
    coverage_df.to_csv(OUT_DIR / "pilot_observation_coverage_audit.csv", index=False)

    with open(OUT_DIR / "pilot_observation_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    # Station-coordinate/ID consistency check within the pilot window.
    consistency = {}
    for st in STATIONS:
        sub = combined[combined["station_id"] == st["station_id"]]
        consistency[st["station_id"]] = {
            "distinct_lat_lon_pairs": sub[["station_lat", "station_lon"]].drop_duplicates().to_dict("records"),
        }

    knyc_flags = coverage_df[(coverage_df["station_id"] == "725053-94728") & (coverage_df["flag"] != "OK")]
    kewr_flags = coverage_df[(coverage_df["station_id"] == "725020-14734") & (coverage_df["flag"] != "OK")]

    report = {
        "pilot_start_date": str(PILOT_START_DATE),
        "pilot_end_date": str(PILOT_END_DATE),
        "n_pilot_days": (PILOT_END_DATE - PILOT_START_DATE).days + 1,
        "total_requests": stats.n_requests,
        "total_bytes_downloaded": stats.bytes_downloaded,
        "station_manifest": manifest,
        "station_coordinate_consistency": consistency,
        "knyc_flagged_days_count": len(knyc_flags),
        "knyc_flagged_days": knyc_flags.to_dict("records"),
        "kewr_flagged_days_count": len(kewr_flags),
        "status": "PASS" if len(knyc_flags) == 0 else "WARNING",
    }
    with open(OUT_DIR / "pilot_observation_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nKNYC flagged days: {len(knyc_flags)} / {report['n_pilot_days']}")
    print(f"KEWR flagged days: {len(kewr_flags)} / {report['n_pilot_days']}")
    print(f"Overall status: {report['status']}")
    print(f"\nCombined canonical rows: {len(combined)}")
    print("Outputs written to", OUT_DIR)


if __name__ == "__main__":
    main()
