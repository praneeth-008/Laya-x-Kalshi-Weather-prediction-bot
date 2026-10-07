"""HISTORICAL BACKFILL -- ISD surface observations for KNYC/KLGA/KJFK/KEWR,
one six-month calendar block. Generalizes scripts/pilot_phase1_observations.py
(approved baseline 8af8d30) from the fixed 2025-01-01..2025-08-24 pilot window
to an arbitrary block's [start, end] range. Each block is within a single
calendar year (Jan-Jun or Jul-Dec halves), so exactly one annual ISD file per
station is fetched per block (idempotent -- reused if already downloaded for
a sibling block in the same year).

KNYC remains the sole target station. KLGA/KJFK/KEWR are auxiliary only,
never averaged into the target.

Usage: python scripts/backfill_observations.py --block 2026H1
"""
import json
import sys
import csv
import io
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import requests

from data.observations import STATIONS, fetch_station_year, parse_isd_row, is_real_observation, RequestStats
from scripts.backfill_common import block_arg_parser, get_block, block_out_dir

SOURCE = "observations"
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data/raw/backfill/observations"
RAW_DIR.mkdir(parents=True, exist_ok=True)


def load_or_fetch_year(station, year, stats):
    """Returns the raw annual ISD CSV text, or None if the archive confirms
    (via HTTP 404) that this year's file does not exist yet -- a real,
    anticipated structural-currency limitation (see scripts/backfill_common.py's
    module docstring), NOT an extraction failure. Any OTHER HTTP error (500,
    etc.) still propagates and fails loudly -- only a confirmed 404 is ever
    treated as "doesn't exist", never silently swallowed for other errors."""
    raw_path = RAW_DIR / f"{station['station_id']}_{year}.csv"
    if raw_path.exists():
        return raw_path.read_text(encoding="utf-8")
    try:
        text, _ = fetch_station_year(year, station["usaf"], station["wban"], stats=stats)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    raw_path.write_text(text, encoding="utf-8")
    return text


def empty_observation_frame() -> pd.DataFrame:
    """A zero-row DataFrame with the exact schema parse_isd_row() produces,
    for a station-year confirmed structurally unavailable -- never a
    hand-maintained/guessed column list, so it can't silently drift out of
    sync with the real parser."""
    return pd.DataFrame([parse_isd_row({})]).iloc[0:0]


def main():
    args = block_arg_parser("observations").parse_args()
    block = get_block(args.block)
    out_dir = block_out_dir(SOURCE, block.block_id)
    assert block.start.year == block.end.year, "block spans two calendar years -- observations fetch assumes single-year ISD files"
    year = block.start.year

    stats = RequestStats()
    all_frames = []
    manifest = []
    coverage_rows = []

    n_days = (block.end - block.start).days + 1
    all_dates = [block.start + timedelta(days=i) for i in range(n_days)]

    for st in STATIONS:
        text = load_or_fetch_year(st, year, stats)

        if text is None:
            # Confirmed structural absence (HTTP 404 on the whole annual file) --
            # a real, anticipated archive-currency limitation, not a failure.
            # Every day in the block window is explicitly marked unavailable;
            # nothing is fabricated, substituted, or forward-filled.
            manifest.append(
                {
                    "station_id": st["station_id"], "icao": st["icao"],
                    "total_rows_in_block_window": 0, "real_observation_rows": 0,
                    "status": "STRUCTURALLY_UNAVAILABLE",
                    "reason": f"NOAA ISD annual file for {year} returned HTTP 404 (not yet published) -- confirmed structural absence, not an extraction failure.",
                }
            )
            for d in all_dates:
                coverage_rows.append(
                    {
                        "station_id": st["station_id"], "date": str(d),
                        "observation_count": 0, "temperature_observation_count": 0,
                        "first_timestamp": None, "last_timestamp": None,
                        "max_temperature_f": None, "time_of_max": None,
                        "largest_gap_minutes": None, "flag": "STRUCTURALLY_UNAVAILABLE_ARCHIVE_YEAR",
                    }
                )
            all_frames.append(empty_observation_frame().assign(
                station_id=pd.Series(dtype="object"), station_icao=pd.Series(dtype="object"),
                station_lat=pd.Series(dtype="float64"), station_lon=pd.Series(dtype="float64"),
                station_elevation_m=pd.Series(dtype="float64"), is_real_observation=pd.Series(dtype="bool"),
                obs_date=pd.Series(dtype="object"),
            ))
            print(f"[{block.block_id}] {st['icao']}: STRUCTURALLY UNAVAILABLE (annual ISD file for {year} not yet published, HTTP 404)")
            continue

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
        in_window = df[(df["obs_date"] >= block.start) & (df["obs_date"] <= block.end)].copy()
        real = in_window[in_window["is_real_observation"]]

        all_frames.append(in_window)

        manifest.append(
            {
                "station_id": st["station_id"], "icao": st["icao"],
                "total_rows_in_block_window": len(in_window),
                "real_observation_rows": len(real),
                "status": "DONE",
            }
        )

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
        print(f"[{block.block_id}] {st['icao']}: {len(in_window)} rows in block window, {len(real)} real observations")

    combined = pd.concat(all_frames, ignore_index=True)
    combined.to_parquet(out_dir / "backfill_observations_canonical.parquet", index=False)

    coverage_df = pd.DataFrame(coverage_rows)
    coverage_df.to_csv(out_dir / "backfill_observation_coverage_audit.csv", index=False)

    with open(out_dir / "backfill_observation_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    consistency = {}
    for st in STATIONS:
        sub = combined[combined["station_id"] == st["station_id"]]
        consistency[st["station_id"]] = {
            "distinct_lat_lon_pairs": sub[["station_lat", "station_lon"]].drop_duplicates().to_dict("records"),
        }

    knyc_flags = coverage_df[(coverage_df["station_id"] == "725053-94728") & (coverage_df["flag"] != "OK")]
    kewr_flags = coverage_df[(coverage_df["station_id"] == "725020-14734") & (coverage_df["flag"] != "OK")]

    report = {
        "block_id": block.block_id, "block_start": str(block.start), "block_end": str(block.end),
        "n_block_days": (block.end - block.start).days + 1,
        "total_requests": stats.n_requests,
        "total_bytes_downloaded": stats.bytes_downloaded,
        "station_manifest": manifest,
        "station_coordinate_consistency": consistency,
        "knyc_flagged_days_count": len(knyc_flags),
        "knyc_flagged_days": knyc_flags.to_dict("records"),
        "kewr_flagged_days_count": len(kewr_flags),
        "status": "PASS" if len(knyc_flags) == 0 else "WARNING",
    }
    with open(out_dir / "backfill_observation_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n[{block.block_id}] KNYC flagged days: {len(knyc_flags)} / {report['n_block_days']}")
    print(f"[{block.block_id}] KEWR flagged days: {len(kewr_flags)} / {report['n_block_days']}")
    print(f"[{block.block_id}] Overall status: {report['status']}")
    print(f"[{block.block_id}] Combined canonical rows: {len(combined)}")
    print("Outputs written to", out_dir)


if __name__ == "__main__":
    main()
