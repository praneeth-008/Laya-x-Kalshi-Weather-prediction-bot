"""Select ~40 representative TARGET DAYS from the already-downloaded 2025
observation dataset (KNYC primary), using a systematic stratified procedure
-- NOT manual cherry-picking. Uses ONLY already-downloaded ISD data (no new
downloads). Realized weather is used ONLY to pick which days to later fetch
NUMERICAL FORECAST data for -- it never enters weather_state(t) itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OBS_PATH = ROOT / "data/processed/pilot/observations/pilot_observations_canonical.parquet"
OUT_DIR = ROOT / "data/processed/weather/pilot"
OUT_DIR.mkdir(parents=True, exist_ok=True)

KNYC_ID = "725053-94728"
TARGET_N_DAYS = 40
DAYS_PER_SEASON = 8


def season_bucket(month: int) -> str:
    if month in (1, 2):
        return "winter_jan_feb"
    if month in (3, 4, 5):
        return "spring"
    if month == 6:
        return "early_summer"
    if month == 7:
        return "midsummer"
    return "august"


def temperature_bucket(tmax_f: float) -> str:
    if tmax_f < 25:
        return "very_cold"
    if tmax_f < 45:
        return "cold"
    if tmax_f < 65:
        return "moderate"
    if tmax_f < 85:
        return "warm"
    return "very_hot"


def cloud_code(ga1_raw) -> int | None:
    if not ga1_raw or not isinstance(ga1_raw, str):
        return None
    try:
        return int(ga1_raw.split(",")[0])
    except (ValueError, IndexError):
        return None


def weather_regime(precip_flag: bool, storm_flag: bool, cloud_codes: list[int]) -> str:
    if storm_flag:
        return "active_storm"
    if precip_flag:
        return "precipitation"
    valid_codes = [c for c in cloud_codes if c is not None and c <= 9]
    if valid_codes and (sum(c >= 5 for c in valid_codes) / len(valid_codes)) > 0.4:
        return "cloudy"
    return "clear_dry"


def main():
    df = pd.read_parquet(OBS_PATH)
    knyc = df[(df["station_id"] == KNYC_ID) & (df["is_real_observation"])].copy()
    knyc["obs_date"] = pd.to_datetime(knyc["observation_time"]).dt.date

    rows = []
    for d, g in knyc.groupby("obs_date"):
        g = g.sort_values("observation_time")
        temp_g = g[g["temperature_f"].notna()]
        if temp_g.empty:
            continue
        tmax = temp_g["temperature_f"].max()
        tmin = temp_g["temperature_f"].min()
        precip_flag = bool((g["precipitation_mm"].fillna(0) > 0).any())
        weather_codes = g["weather_codes_raw"].dropna().astype(str)
        storm_flag = bool(weather_codes.str.contains("TS", na=False).any())
        cloud_codes = [cloud_code(x) for x in g["cloud_cover_raw"]]
        gaps = g["observation_time"].diff().dt.total_seconds().dropna() / 60
        rows.append(
            {
                "date": str(d), "month": d.month,
                "season_bucket": season_bucket(d.month),
                "temperature_bucket": temperature_bucket(tmax),
                "observed_tmax_f": round(float(tmax), 2),
                "observed_tmin_f": round(float(tmin), 2),
                "daily_temperature_range_f": round(float(tmax - tmin), 2),
                "precipitation_flag": precip_flag,
                "weather_regime": weather_regime(precip_flag, storm_flag, cloud_codes),
                "observation_count": len(g),
                "largest_gap_minutes": float(gaps.max()) if not gaps.empty else None,
            }
        )
    daily = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    daily.to_csv(OUT_DIR / "knyc_daily_diagnostics_full_period.csv", index=False)
    print(f"Computed daily diagnostics for {len(daily)} KNYC days.")
    print(daily["season_bucket"].value_counts())
    print(daily["temperature_bucket"].value_counts())
    print(daily["weather_regime"].value_counts())

    # Systematic stratified selection: within each season bucket, pick
    # DAYS_PER_SEASON days EVENLY SPACED BY RANK of observed Tmax (a
    # standard systematic/quantile sampling method -- not manual
    # cherry-picking). Then, if the season's picks contain no
    # precipitation/storm day but such days exist that season, swap the
    # pick closest to the median Tmax rank for the precipitation/storm day
    # closest to that same rank -- documented, not arbitrary.
    selected = []
    for season, g in daily.groupby("season_bucket"):
        g = g.sort_values("observed_tmax_f").reset_index(drop=True)
        n = len(g)
        k = min(DAYS_PER_SEASON, n)
        if k == 0:
            continue
        idxs = np.linspace(0, n - 1, k).round().astype(int)
        idxs = sorted(set(idxs))
        picks = g.loc[idxs].copy()
        picks["selection_reason"] = "systematic_tmax_rank_evenly_spaced"

        has_precip_or_storm = picks["weather_regime"].isin(["precipitation", "active_storm"]).any()
        if not has_precip_or_storm:
            candidates = g[g["weather_regime"].isin(["precipitation", "active_storm"])]
            if not candidates.empty:
                median_rank = n // 2
                candidates = candidates.copy()
                candidates["rank_in_season"] = candidates.index
                swap_in = candidates.iloc[(candidates["rank_in_season"] - median_rank).abs().argsort().iloc[0]]
                # Swap out the pick nearest the median rank.
                nearest_pick_pos = (picks.index.to_series() - median_rank).abs().idxmin()
                picks = picks.drop(index=nearest_pick_pos)
                swap_row = swap_in.drop(labels=["rank_in_season"]).to_dict()
                swap_row["selection_reason"] = "swapped_in_for_precip_storm_coverage"
                picks = pd.concat([picks, pd.DataFrame([swap_row])], ignore_index=True)
        selected.append(picks)

    selected_df = pd.concat(selected, ignore_index=True).sort_values("date").reset_index(drop=True)

    # Trim/pad to as close to TARGET_N_DAYS as the systematic method allows.
    if len(selected_df) > TARGET_N_DAYS:
        # Trim evenly across the combined set by date order to preserve spread.
        trim_idxs = np.linspace(0, len(selected_df) - 1, TARGET_N_DAYS).round().astype(int)
        selected_df = selected_df.iloc[sorted(set(trim_idxs))].reset_index(drop=True)

    selected_df.to_csv(OUT_DIR / "selected_days.csv", index=False)

    print(f"\nSelected {len(selected_df)} representative target days.")
    print("\nSeason distribution:")
    print(selected_df["season_bucket"].value_counts())
    print("\nTemperature bucket distribution:")
    print(selected_df["temperature_bucket"].value_counts())
    print("\nWeather regime distribution:")
    print(selected_df["weather_regime"].value_counts())
    print("\nSelected dates:")
    print(selected_df["date"].tolist())
    print(f"\nWritten to {OUT_DIR / 'selected_days.csv'}")


if __name__ == "__main__":
    main()
