"""Small, targeted investigation of NBM hourly run cadence: does each
consecutive hourly run produce a genuinely different Central Park forecast
for a FIXED future valid time? Reuses data/nbm.py's existing
fetch_idx/find_message/fetch_byte_range/decode_message -- no new download
logic, only a handful of small byte-range requests (a few messages/run).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.nbm import fetch_idx, find_message, fetch_byte_range, decode_message, grib_key, RequestStats

CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
RUN_DATE = datetime(2025, 7, 1)
RUN_HOURS = [6, 7, 8, 9, 10, 11, 12]  # 7 consecutive hourly runs
TARGET_VALID_HOUR_UTC = 20  # 20:00 UTC = 16:00 ET, reachable by all these runs

VAR_SPECS = [
    ("TMP", "2 m above ground"),
    ("DPT", "2 m above ground"),
    ("TCDC", "surface"),
]


def main():
    stats = RequestStats()
    results = []
    for run_hour in RUN_HOURS:
        run_time = RUN_DATE.replace(hour=run_hour, tzinfo=timezone.utc)
        target_valid = RUN_DATE.replace(hour=TARGET_VALID_HOUR_UTC, tzinfo=timezone.utc)
        forecast_hour = int((target_valid - run_time).total_seconds() / 3600)
        if forecast_hour < 1:
            continue
        key = grib_key(RUN_DATE, run_hour, forecast_hour)
        idx = fetch_idx(key, stats=stats)
        for var, level in VAR_SPECS:
            msg = find_message(idx, var, level, desc_excludes="ens std dev")
            if msg is None:
                results.append({"run_hour": run_hour, "forecast_hour": forecast_hour, "variable": var, "value": None})
                continue
            raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"), stats=stats)
            decoded = decode_message(raw, CENTRAL_PARK["lat"], CENTRAL_PARK["lon"])
            results.append({"run_hour": run_hour, "forecast_hour": forecast_hour, "variable": var, "value": decoded["value"], "units": decoded["units"]})

    print(f"Total requests: {stats.n_requests}, bytes downloaded: {stats.bytes_downloaded/1e6:.2f} MB")
    print(f"\nNBM forecasts for FIXED target valid_time = {TARGET_VALID_HOUR_UTC}:00 UTC (16:00 ET) from consecutive hourly runs:")
    print(f"{'run_hour':>8} {'fh':>3} {'variable':>8} {'value':>10} {'units':>6}")
    for r in results:
        print(f"{r['run_hour']:>8} {r['forecast_hour']:>3} {r['variable']:>8} {r['value']:>10} {r.get('units',''):>6}")

    print("\nTemperature (F) revision between consecutive runs:")
    tmp_by_run = {r["run_hour"]: r["value"] for r in results if r["variable"] == "TMP" and r["value"] is not None}
    prev_f = None
    for rh in sorted(tmp_by_run):
        f = tmp_by_run[rh] * 9 / 5 - 459.67
        delta = f - prev_f if prev_f is not None else None
        print(f"  run {rh:02d}Z: {f:.2f}F  (change from previous run: {delta if delta is None else round(delta,2)})")
        prev_f = f


if __name__ == "__main__":
    main()
