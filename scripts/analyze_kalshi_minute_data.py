"""Validation + descriptive statistics for the processed 1-minute dataset,
plus a full-dataset gap/quote-staleness investigation.

Reads data/processed/kalshi/minute/ (built by
scripts/build_kalshi_minute_dataset.py) ONLY -- it is never modified.
Analysis outputs (aggregated summary tables, not a row-level duplicate of
the 5.34M-row dataset) are written under data/analysis/kalshi/minute/.

This file has two halves:
  1. The original validation + liquidity/density + market-structure checks
     from the minute-dataset build stage.
  2. A dedicated investigation into what a MISSING 1-minute candle likely
     represents -- built around one explicit research question: whether
     missing minutes, especially in the sparse 2023-2024 period, are
     associated with a stale/unchanged order book. This is investigated,
     not assumed, and every claim below is labeled DOCUMENTED, OBSERVED,
     INFERRED, or UNKNOWN. In particular, "missing candle = unchanged
     quote" is never asserted as fact anywhere in this module.

This is descriptive only -- it does not decide which years are "tradable",
does not build a predictive model, and does not touch weather data, Jev,
Laya, or trading logic.

Usage:
    python scripts/analyze_kalshi_minute_data.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import parse_kalshi_ts  # noqa: E402
from scripts.download_kalshi_minute import FAILURE_LOG_PATH, RAW_MINUTE_DIR  # noqa: E402
from scripts.build_kalshi_minute_dataset import PROCESSED_DIR  # noqa: E402

ANALYSIS_DIR = PROJECT_ROOT / "data" / "analysis" / "kalshi" / "minute"

GAP_BUCKET_BINS = [0, 1, 2, 5, 15, 60, 180, float("inf")]
GAP_BUCKET_LABELS = ["1", "2", "3-5", "6-15", "16-60", "61-180", ">180"]
LIFECYCLE_ORDER = ["<1h", "1-3h", "3-6h", "6-12h", "12-24h", ">24h"]


def _bucket_missing_minutes(missing_minutes: pd.Series) -> pd.Series:
    return pd.cut(missing_minutes, bins=GAP_BUCKET_BINS, labels=GAP_BUCKET_LABELS, right=True)


# ---------------------------------------------------------------------------
# Original validation / liquidity-density / structure checks.
# ---------------------------------------------------------------------------

def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_parquet(PROCESSED_DIR, engine="pyarrow")
    # The year Hive-partition column comes back as pandas categorical dtype,
    # which triggers groupby FutureWarnings and sorts oddly -- a plain int
    # is what every groupby below actually wants.
    df["year"] = df["year"].astype(int)
    bounds = pd.read_parquet(RAW_MINUTE_DIR / "_market_bounds_cache.parquet", engine="pyarrow")
    return df, bounds


def validate(df: pd.DataFrame, bounds: pd.DataFrame) -> None:
    print("=== VALIDATION (dataset build checks) ===")

    dup_count = int(df.duplicated(subset=["market_ticker", "timestamp"]).sum())
    print(f"1. Duplicate rows on (market_ticker, timestamp): {dup_count}")

    def is_sorted(s: pd.Series) -> bool:
        return bool((s.diff().dropna() >= 0).all())

    ordering_ok = df.groupby("market_ticker", sort=False)["timestamp"].apply(is_sorted)
    n_unordered = int((~ordering_ok).sum())
    print(f"2. Markets with out-of-order timestamps (as returned by the API): {n_unordered} of {len(ordering_ok)}")

    bounds = bounds.copy()
    bounds["open_ts"] = bounds["_open_time"].apply(lambda v: parse_kalshi_ts(v) if v else None)
    bounds["close_ts"] = bounds["_close_time"].apply(lambda v: parse_kalshi_ts(v) if v else None)
    merged = df.merge(bounds[["market_ticker", "open_ts", "close_ts"]], on="market_ticker", how="left")
    before_open = merged["timestamp"] < merged["open_ts"]
    after_close = merged["timestamp"] > merged["close_ts"]
    print(f"3. Candles timestamped before market open_time: {int(before_open.sum())}")
    print(f"   Candles timestamped after market close_time: {int(after_close.sum())} "
          f"(some trailing candles shortly after close are expected -- settlement isn't instantaneous)")

    important_fields = [
        "yes_bid_close", "yes_ask_close", "price_close", "volume", "open_interest",
        "result", "settlement_value", "expiration_value", "floor_strike", "cap_strike", "strike_type",
    ]
    print("4. Missingness by important field (% null):")
    print((df[important_fields].isna().mean() * 100).round(1).to_string())

    has_flag = df["is_empty_book"].notna()
    empty_pct = 100 * df.loc[has_flag, "is_empty_book"].mean() if has_flag.any() else float("nan")
    print(f"5. Empty-book rate (bid=0,ask=1) among quoted candles: {empty_pct:.2f}%")

    print("6. Rows by year:")
    print(df.groupby("year").size().to_string())
    print("7. Events by year:")
    print(df.groupby("year")["event_ticker"].nunique().to_string())
    print("8. Markets by year:")
    print(df.groupby("year")["market_ticker"].nunique().to_string())

    print(f"9. Total rows: {len(df)}")
    print(f"10. Earliest timestamp: {datetime.fromtimestamp(df['timestamp'].min(), tz=timezone.utc)}")
    print(f"11. Latest timestamp: {datetime.fromtimestamp(df['timestamp'].max(), tz=timezone.utc)}")

    n_failures = 0
    if FAILURE_LOG_PATH.exists():
        n_failures = sum(1 for _ in open(FAILURE_LOG_PATH, encoding="utf-8"))
    print(f"12. Number of failed API requests EVER LOGGED (cumulative, includes retried-and-resolved): {n_failures}")

    print("13. Minute-gap distribution per year (minutes between consecutive returned candles, per market):")
    df_sorted = df.sort_values(["market_ticker", "timestamp"])
    gap_minutes = df_sorted.groupby("market_ticker")["timestamp"].diff() / 60.0
    gap_df = pd.DataFrame({"year": df_sorted["year"], "gap_minutes": gap_minutes}).dropna()
    pct = gap_df.groupby("year")["gap_minutes"].quantile([0.50, 0.75, 0.90, 0.95, 0.99]).unstack()
    pct.columns = ["p50_median", "p75", "p90", "p95", "p99"]
    print(pct.round(1).to_string())


def liquidity_density(df: pd.DataFrame, bounds: pd.DataFrame) -> pd.DataFrame:
    print("\n=== LIQUIDITY / DENSITY (descriptive only) ===")

    bounds = bounds.copy()
    bounds["open_ts"] = bounds["_open_time"].apply(lambda v: parse_kalshi_ts(v) if v else None)
    bounds["close_ts"] = bounds["_close_time"].apply(lambda v: parse_kalshi_ts(v) if v else None)
    bounds["window_minutes"] = (bounds["close_ts"] - bounds["open_ts"]) / 60.0
    market_to_window = bounds.set_index("market_ticker")["window_minutes"]

    rows_per_event = df.groupby(["year", "event_ticker"]).size()
    has_quote = df["yes_bid_close"].notna() & df["yes_ask_close"].notna()
    df = df.copy()
    df["has_quote"] = has_quote
    df["spread"] = df["yes_ask_close"] - df["yes_bid_close"]
    valid_quote = has_quote & (~df["is_empty_book"].fillna(False))

    volume_per_event = df.groupby(["year", "event_ticker"])["volume"].sum(min_count=1)

    summary_rows = []
    for year, g in df.groupby("year"):
        markets_this_year = g["market_ticker"].unique()
        possible_minutes = market_to_window.reindex(markets_this_year).sum()
        coverage_pct = 100 * len(g) / possible_minutes if possible_minutes else float("nan")
        summary_rows.append(
            {
                "year": year,
                "events": g["event_ticker"].nunique(),
                "markets": g["market_ticker"].nunique(),
                "minute_rows": len(g),
                "avg_rows_per_event": rows_per_event.loc[year].mean(),
                "median_rows_per_event": rows_per_event.loc[year].median(),
                "coverage_pct_of_possible_market_minutes": coverage_pct,
                "valid_bidask_pct": 100 * g["has_quote"].mean(),
                "empty_book_pct": 100 * g["is_empty_book"].fillna(False).mean(),
                "median_spread": g.loc[valid_quote.loc[g.index], "spread"].median(),
                "total_volume": g["volume"].sum(min_count=1),
                "median_event_volume": volume_per_event.loc[year].median(),
            }
        )

    summary = pd.DataFrame(summary_rows).set_index("year")
    print(summary.round(3).to_string())
    return summary


def market_structures_by_year(df: pd.DataFrame) -> None:
    print("\n=== MARKET STRUCTURES (2023-present) ===")
    counts = df.groupby(["year", "market_structure"])["event_ticker"].nunique().unstack(fill_value=0)
    print(counts.to_string())
    print("\nOverall (distinct events, 2023-present):")
    print(df.groupby("market_structure")["event_ticker"].nunique().to_string())


# ---------------------------------------------------------------------------
# Gap / quote-staleness investigation.
# ---------------------------------------------------------------------------

def build_pairs(df: pd.DataFrame, bounds: pd.DataFrame) -> pd.DataFrame:
    """One row per (market_ticker, consecutive-returned-candle pair),
    sorted by timestamp within market. This is the shared base for every
    gap / consecutive-candle analysis below. It is NOT persisted to disk
    in full (only aggregated summaries are) -- see module docstring.
    """
    df_sorted = df.sort_values(["market_ticker", "timestamp"]).reset_index(drop=True)
    g = df_sorted.groupby("market_ticker", sort=False)
    shifted = g[["timestamp", "yes_bid_close", "yes_ask_close", "is_empty_book"]].shift(1)

    bounds = bounds.copy()
    bounds["close_ts"] = bounds["_close_time"].apply(lambda v: parse_kalshi_ts(v) if v else None)
    close_ts_map = bounds.set_index("market_ticker")["close_ts"]

    pairs = pd.DataFrame(
        {
            "market_ticker": df_sorted["market_ticker"].values,
            "event_ticker": df_sorted["event_ticker"].values,
            "year": df_sorted["year"].values,
            "pre_ts": shifted["timestamp"].values,
            "post_ts": df_sorted["timestamp"].values,
            "pre_bid_close": shifted["yes_bid_close"].values,
            "pre_ask_close": shifted["yes_ask_close"].values,
            "pre_is_empty": shifted["is_empty_book"].values,
            "post_bid_open": df_sorted["yes_bid_open"].values,
            "post_bid_high": df_sorted["yes_bid_high"].values,
            "post_bid_low": df_sorted["yes_bid_low"].values,
            "post_bid_close": df_sorted["yes_bid_close"].values,
            "post_ask_open": df_sorted["yes_ask_open"].values,
            "post_ask_high": df_sorted["yes_ask_high"].values,
            "post_ask_low": df_sorted["yes_ask_low"].values,
            "post_ask_close": df_sorted["yes_ask_close"].values,
            "post_volume": df_sorted["volume"].values,
            "post_is_empty": df_sorted["is_empty_book"].values,
        }
    )
    pairs["close_ts"] = pairs["market_ticker"].map(close_ts_map)
    # The first candle of each market has no predecessor -- drop those rows.
    pairs = pairs.dropna(subset=["pre_ts"]).copy()

    pairs["elapsed_seconds"] = pairs["post_ts"] - pairs["pre_ts"]
    pairs["elapsed_minutes"] = pairs["elapsed_seconds"] / 60.0
    pairs["missing_minutes"] = pairs["elapsed_minutes"] - 1

    # Null-safe equality (Part 2 requirement): a comparison only counts as
    # "eligible" when BOTH sides are present; pandas' `==` already returns
    # False for NaN vs anything, but we track eligibility explicitly so
    # "not eligible" and "compared and different" are never conflated.
    pairs["bid_valid"] = pairs["pre_bid_close"].notna() & pairs["post_bid_open"].notna()
    pairs["ask_valid"] = pairs["pre_ask_close"].notna() & pairs["post_ask_open"].notna()
    pairs["quote_eligible"] = pairs["bid_valid"] & pairs["ask_valid"]
    pairs["same_bid"] = pairs["bid_valid"] & (pairs["pre_bid_close"] == pairs["post_bid_open"])
    pairs["same_ask"] = pairs["ask_valid"] & (pairs["pre_ask_close"] == pairs["post_ask_open"])
    pairs["same_quote"] = pairs["quote_eligible"] & pairs["same_bid"] & pairs["same_ask"]

    pairs["bid_change"] = np.where(pairs["bid_valid"], pairs["post_bid_open"] - pairs["pre_bid_close"], np.nan)
    pairs["ask_change"] = np.where(pairs["ask_valid"], pairs["post_ask_open"] - pairs["pre_ask_close"], np.nan)
    pairs["abs_bid_change"] = np.abs(pairs["bid_change"])
    pairs["abs_ask_change"] = np.abs(pairs["ask_change"])
    pre_mid = np.where(pairs["quote_eligible"], (pairs["pre_bid_close"] + pairs["pre_ask_close"]) / 2, np.nan)
    post_mid = np.where(pairs["quote_eligible"], (pairs["post_bid_open"] + pairs["post_ask_open"]) / 2, np.nan)
    pairs["abs_mid_change"] = np.abs(post_mid - pre_mid)

    pairs["post_flat_zero_volume"] = (
        (pairs["post_volume"] == 0)
        & (pairs["post_bid_open"] == pairs["post_bid_high"])
        & (pairs["post_bid_high"] == pairs["post_bid_low"])
        & (pairs["post_bid_low"] == pairs["post_bid_close"])
        & (pairs["post_ask_open"] == pairs["post_ask_high"])
        & (pairs["post_ask_high"] == pairs["post_ask_low"])
        & (pairs["post_ask_low"] == pairs["post_ask_close"])
    )

    pairs["touches_empty_book"] = (
        pairs["pre_is_empty"].fillna(False).astype(bool) | pairs["post_is_empty"].fillna(False).astype(bool)
    )
    pairs["post_close"] = pairs["pre_ts"] >= pairs["close_ts"]
    hours_before_close = (pairs["close_ts"] - pairs["pre_ts"]) / 3600.0
    pairs["hours_before_close"] = hours_before_close

    conditions = [
        pairs["close_ts"].isna(),
        pairs["post_close"],
        hours_before_close > 24,
        hours_before_close >= 12,
        hours_before_close >= 6,
        hours_before_close >= 3,
        hours_before_close >= 1,
    ]
    choices = ["unknown", "post_close", ">24h", "12-24h", "6-12h", "3-6h", "1-3h"]
    pairs["lifecycle_bucket"] = np.select(conditions, choices, default="<1h")

    return pairs


def analyze_gaps(gaps: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 1: IDENTIFY GAPS")
    print("=" * 78)
    by_year = gaps.groupby("year")["missing_minutes"].agg(
        n_gaps="size",
        total_missing_minutes="sum",
        median="median",
        mean="mean",
        p75=lambda s: s.quantile(0.75),
        p90=lambda s: s.quantile(0.90),
        p95=lambda s: s.quantile(0.95),
        p99=lambda s: s.quantile(0.99),
        max="max",
    )
    print("Gap statistics by year (elapsed_minutes > 1; missing_minutes = elapsed_minutes - 1):")
    print(by_year.round(2).to_string())

    bucket_counts = (
        gaps.groupby(["year", "missing_bucket"], observed=True).size().unstack(fill_value=0)
    )
    bucket_counts = bucket_counts[[c for c in GAP_BUCKET_LABELS if c in bucket_counts.columns]]
    print("\nGap counts by missing-minute bucket:")
    print(bucket_counts.to_string())

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    by_year.to_csv(ANALYSIS_DIR / "gap_stats_by_year.csv")
    bucket_counts.to_csv(ANALYSIS_DIR / "gap_bucket_counts_by_year.csv")
    return by_year


def analyze_gap_quotes(gaps: pd.DataFrame, label: str) -> pd.DataFrame:
    print(f"\n--- Same-quote gap test ({label}) ---")
    print(
        "NOTE: 'same_quote' means the last observed quote BEFORE the gap equals the "
        "first observed quote AFTER it. It does NOT mean the quote is known to have "
        "stayed constant for the full gap duration -- we have no observations from "
        "inside the gap."
    )
    eligible = gaps[gaps["quote_eligible"]]
    by_year = eligible.groupby("year").agg(
        total_eligible_gaps=("same_quote", "size"),
        same_bid_pct=("same_bid", lambda s: 100 * s.mean()),
        same_ask_pct=("same_ask", lambda s: 100 * s.mean()),
        same_quote_pct=("same_quote", lambda s: 100 * s.mean()),
    )
    by_year["changed_quote_pct"] = 100 - by_year["same_quote_pct"]
    print(by_year.round(2).to_string())

    print(f"\nSame-quote % by year x gap-length bucket ({label}):")
    pivot = (eligible.groupby(["year", "missing_bucket"], observed=True)["same_quote"].mean() * 100).unstack()
    pivot = pivot[[c for c in GAP_BUCKET_LABELS if c in pivot.columns]]
    print(pivot.round(1).to_string())

    print(f"\nQuote distance after gaps, by year x gap-length bucket ({label}):")
    dist = eligible.groupby(["year", "missing_bucket"], observed=True).agg(
        median_abs_bid_change=("abs_bid_change", "median"),
        median_abs_ask_change=("abs_ask_change", "median"),
        median_abs_mid_change=("abs_mid_change", "median"),
        p90_abs_mid_change=("abs_mid_change", lambda s: s.quantile(0.90)),
    )
    print(dist.round(4).to_string())

    safe_label = label.replace(" ", "_")
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    by_year.to_csv(ANALYSIS_DIR / f"same_quote_gap_by_year__{safe_label}.csv")
    pivot.to_csv(ANALYSIS_DIR / f"same_quote_pct_by_year_bucket__{safe_label}.csv")
    dist.to_csv(ANALYSIS_DIR / f"quote_distance_by_year_bucket__{safe_label}.csv")
    return by_year


def analyze_flat_candles(df: pd.DataFrame, consec: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 4: DO CANDLES EXIST WHEN NOTHING CHANGES?")
    print("=" * 78)
    df = df.copy()
    df["flat_zero_volume"] = (
        (df["volume"] == 0)
        & (df["yes_bid_open"] == df["yes_bid_high"])
        & (df["yes_bid_high"] == df["yes_bid_low"])
        & (df["yes_bid_low"] == df["yes_bid_close"])
        & (df["yes_ask_open"] == df["yes_ask_high"])
        & (df["yes_ask_high"] == df["yes_ask_low"])
        & (df["yes_ask_low"] == df["yes_ask_close"])
    )
    df["zero_volume"] = df["volume"] == 0

    consec = consec.copy()
    consec["fully_unchanged"] = (
        consec["post_flat_zero_volume"]
        & (consec["pre_bid_close"] == consec["post_bid_open"])
        & (consec["pre_ask_close"] == consec["post_ask_open"])
    )

    by_year = pd.DataFrame(
        {
            "total_candles": df.groupby("year").size(),
            "zero_volume_candles": df.groupby("year")["zero_volume"].sum(),
            "flat_zero_volume_candles": df.groupby("year")["flat_zero_volume"].sum(),
        }
    )
    by_year["eligible_consecutive_pairs"] = consec.groupby("year").size()
    by_year["fully_unchanged_consecutive"] = consec.groupby("year")["fully_unchanged"].sum()
    by_year["pct_consecutive_fully_unchanged"] = (
        100 * by_year["fully_unchanged_consecutive"] / by_year["eligible_consecutive_pairs"]
    )
    print(by_year.round(2).to_string())

    if by_year["fully_unchanged_consecutive"].sum() > 0:
        print(
            "\n=> Kalshi DOES sometimes emit consecutive 1-minute candles with volume=0, flat "
            "bid/ask OHLC, and an unchanged quote from the previous candle. The hypothesis "
            "'Kalshi only emits a candle when something changes' is therefore FALSE/incomplete "
            "-- OBSERVED directly above, not inferred."
        )
    else:
        print("\n=> No fully-unchanged consecutive candles were observed in this dataset.")

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    by_year.to_csv(ANALYSIS_DIR / "flat_candle_stats_by_year.csv")
    return df, consec, by_year


def analyze_zero_volume_quote_changes(consec: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 5: QUOTE CHANGES WITHOUT TRADES (consecutive 1-min candles)")
    print("=" * 78)
    zero_vol = consec[consec["post_volume"] == 0]
    eligible = zero_vol[zero_vol["quote_eligible"]]
    changed = eligible[~(eligible["same_bid"] & eligible["same_ask"])]

    by_year = pd.DataFrame(
        {
            "zero_volume_consecutive": zero_vol.groupby("year").size(),
            "eligible_zero_volume_consecutive": eligible.groupby("year").size(),
            "changed_quote_count": changed.groupby("year").size(),
        }
    ).fillna(0)
    by_year["changed_quote_pct_of_eligible"] = (
        100 * by_year["changed_quote_count"] / by_year["eligible_zero_volume_consecutive"]
    )
    by_year["median_abs_mid_change_when_changed"] = changed.groupby("year")["abs_mid_change"].median()
    by_year["p90_abs_mid_change_when_changed"] = changed.groupby("year")["abs_mid_change"].quantile(0.90)
    print(by_year.round(4).to_string())
    print(
        "\n=> Rows above with changed_quote_pct_of_eligible > 0 confirm (OBSERVED) that the "
        "order book reprices without any trade in that same minute -- consistent with the "
        "small-sample finding from the earlier coverage test, now checked dataset-wide."
    )

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    by_year.to_csv(ANALYSIS_DIR / "zero_volume_quote_change_by_year.csv")
    return by_year


def analyze_gap_endpoint_patterns(gaps: pd.DataFrame, label: str) -> None:
    print("\n" + "=" * 78)
    print(f"GAP ANALYSIS -- PART 6: GAP ENDPOINT PATTERNS ({label})")
    print("=" * 78)
    eligible = gaps[gaps["quote_eligible"]].copy()
    conditions = [
        eligible["same_bid"] & eligible["same_ask"],
        eligible["same_bid"] & ~eligible["same_ask"],
        ~eligible["same_bid"] & eligible["same_ask"],
    ]
    choices = ["A_same_bid_same_ask", "B_same_bid_changed_ask", "C_changed_bid_same_ask"]
    eligible["pattern"] = np.select(conditions, choices, default="D_changed_bid_changed_ask")

    by_year = eligible.groupby(["year", "pattern"]).size().unstack(fill_value=0)
    by_year_pct = by_year.div(by_year.sum(axis=1), axis=0) * 100
    print("Counts:")
    print(by_year.to_string())
    print("\nPercentages by year:")
    print(by_year_pct.round(1).to_string())

    by_bucket = eligible.groupby(["year", "missing_bucket", "pattern"], observed=True).size().unstack(fill_value=0)
    by_bucket_pct = by_bucket.div(by_bucket.sum(axis=1), axis=0).replace([np.inf, -np.inf], np.nan) * 100
    print("\nPercentages by year x gap-length bucket:")
    print(by_bucket_pct.round(1).to_string())

    safe_label = label.replace(" ", "_")
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    by_year.to_csv(ANALYSIS_DIR / f"gap_endpoint_pattern_counts_by_year__{safe_label}.csv")
    by_bucket_pct.to_csv(ANALYSIS_DIR / f"gap_endpoint_pattern_pct_by_year_bucket__{safe_label}.csv")


def analyze_lifecycle(gaps: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 7: EVENT-LIFECYCLE EFFECT")
    print("=" * 78)
    g = gaps.copy()
    g["pre_spread"] = np.where(
        g["pre_bid_close"].notna() & g["pre_ask_close"].notna(), g["pre_ask_close"] - g["pre_bid_close"], np.nan
    )

    gap_count = g.groupby(["year", "lifecycle_bucket"]).size().rename("gap_count")
    median_gap = g.groupby(["year", "lifecycle_bucket"])["missing_minutes"].median().rename("median_gap")
    median_pre_spread = g.groupby(["year", "lifecycle_bucket"])["pre_spread"].median().rename("median_pre_spread")
    eligible = g[g["quote_eligible"]]
    same_quote_pct = (eligible.groupby(["year", "lifecycle_bucket"])["same_quote"].mean() * 100).rename(
        "same_quote_pct"
    )
    summary = pd.concat([gap_count, median_gap, median_pre_spread, same_quote_pct], axis=1)

    bucket_level = summary.index.get_level_values("lifecycle_bucket")
    primary = summary[bucket_level.isin(LIFECYCLE_ORDER)]
    post_close = summary[bucket_level == "post_close"]
    unknown = summary[bucket_level == "unknown"]

    print("PRIMARY (pre-close) lifecycle buckets:")
    print(primary.round(3).to_string())
    print("\nPOST-CLOSE gaps -- reported SEPARATELY, NOT part of the primary pre-close conclusions:")
    if post_close.empty:
        print("  (none)")
    else:
        print(post_close.round(3).to_string())
    if not unknown.empty:
        print(f"\n({int(unknown['gap_count'].sum())} gaps had no resolvable close_ts and are excluded from lifecycle buckets)")

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    primary.to_csv(ANALYSIS_DIR / "lifecycle_primary_by_year.csv")
    post_close.to_csv(ANALYSIS_DIR / "lifecycle_post_close_by_year.csv")


def analyze_market_level(df: pd.DataFrame, gaps: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 9: MARKET-LEVEL DISTRIBUTION")
    print("=" * 78)
    df = df.copy()
    valid_quote = df["yes_bid_close"].notna() & df["yes_ask_close"].notna() & (~df["is_empty_book"].fillna(False))
    df["spread"] = np.where(valid_quote, df["yes_ask_close"] - df["yes_bid_close"], np.nan)

    candle_stats = df.groupby(["year", "market_ticker"]).agg(
        candle_count=("timestamp", "size"),
        median_spread=("spread", "median"),
        total_volume=("volume", lambda s: s.sum(min_count=1)),
    ).reset_index()

    eligible_gaps = gaps[gaps["quote_eligible"]]
    gap_stats = pd.DataFrame(
        {
            "gap_count": gaps.groupby("market_ticker").size(),
            "median_gap": gaps.groupby("market_ticker")["missing_minutes"].median(),
            "max_gap": gaps.groupby("market_ticker")["missing_minutes"].max(),
        }
    )
    gap_stats["pct_gaps_same_quote"] = eligible_gaps.groupby("market_ticker")["same_quote"].mean() * 100
    gap_stats = gap_stats.reset_index()

    market_summary = candle_stats.merge(gap_stats, on="market_ticker", how="left")
    market_summary["gap_count"] = market_summary["gap_count"].fillna(0)

    dist = market_summary.groupby("year").agg(
        median_candle_count=("candle_count", "median"),
        p25_candle_count=("candle_count", lambda s: s.quantile(0.25)),
        p75_candle_count=("candle_count", lambda s: s.quantile(0.75)),
        p90_candle_count=("candle_count", lambda s: s.quantile(0.90)),
        median_gap_count=("gap_count", "median"),
        p25_gap_count=("gap_count", lambda s: s.quantile(0.25)),
        p75_gap_count=("gap_count", lambda s: s.quantile(0.75)),
        p90_gap_count=("gap_count", lambda s: s.quantile(0.90)),
        median_pct_same_quote=("pct_gaps_same_quote", "median"),
        p25_pct_same_quote=("pct_gaps_same_quote", lambda s: s.quantile(0.25)),
        p75_pct_same_quote=("pct_gaps_same_quote", lambda s: s.quantile(0.75)),
        p90_pct_same_quote=("pct_gaps_same_quote", lambda s: s.quantile(0.90)),
        median_of_median_gap=("median_gap", "median"),
        median_max_gap=("max_gap", "median"),
        median_spread=("median_spread", "median"),
        median_total_volume=("total_volume", "median"),
    )
    print(dist.round(2).to_string())

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    market_summary.to_csv(ANALYSIS_DIR / "market_level_summary.csv", index=False)
    dist.to_csv(ANALYSIS_DIR / "market_level_distribution_by_year.csv")
    return market_summary


def analyze_liquidity_relationship(market_summary: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 10: RELATIONSHIP WITH LIQUIDITY (descriptive only -- no causal claim, no model)")
    print("=" * 78)
    corr_rows = []
    for year, g in market_summary.groupby("year"):
        g = g.dropna(subset=["total_volume"])
        if len(g) < 5:
            continue
        corr_rows.append(
            {
                "year": year,
                "n_markets": len(g),
                "spearman_volume_vs_gap_count": g["total_volume"].corr(g["gap_count"], method="spearman"),
                "spearman_volume_vs_median_gap": g["total_volume"].corr(g["median_gap"], method="spearman"),
                "spearman_volume_vs_pct_same_quote": g["total_volume"].corr(g["pct_gaps_same_quote"], method="spearman"),
                "spearman_volume_vs_spread": g["total_volume"].corr(g["median_spread"], method="spearman"),
            }
        )
    corr_df = pd.DataFrame(corr_rows).set_index("year")
    print("Spearman rank correlations (market total_volume vs. gap/quote metrics):")
    print(corr_df.round(3).to_string())

    tercile_rows = []
    for year, g in market_summary.groupby("year"):
        g = g.dropna(subset=["total_volume"]).copy()
        if len(g) < 9:
            continue
        g["volume_tercile"] = pd.qcut(g["total_volume"], 3, labels=["low", "mid", "high"], duplicates="drop")
        t = g.groupby("volume_tercile", observed=True).agg(
            n=("market_ticker", "size"),
            median_gap_count=("gap_count", "median"),
            median_of_median_gap=("median_gap", "median"),
            median_pct_same_quote=("pct_gaps_same_quote", "median"),
            median_spread=("median_spread", "median"),
        )
        t["year"] = year
        tercile_rows.append(t.reset_index())
    tercile_df = pd.concat(tercile_rows, ignore_index=True) if tercile_rows else pd.DataFrame()
    print("\nVolume-tercile comparison (within year, low/mid/high total_volume):")
    print(tercile_df.round(3).to_string(index=False))

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    corr_df.to_csv(ANALYSIS_DIR / "liquidity_correlations_by_year.csv")
    tercile_df.to_csv(ANALYSIS_DIR / "liquidity_tercile_comparison.csv", index=False)


def validate_gap_methodology(df: pd.DataFrame, gaps: pd.DataFrame, consec: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 14: METHODOLOGY VALIDATION / SPOT CHECKS")
    print("=" * 78)

    check = bool(np.allclose(gaps["missing_minutes"], gaps["elapsed_minutes"] - 1))
    print(f"missing_minutes == elapsed_minutes - 1 for all gaps: {check}")

    rng_markets = gaps["market_ticker"].drop_duplicates()
    sample_markets = rng_markets.sample(min(2, len(rng_markets)), random_state=0)
    for mt in sample_markets:
        sub = df[df["market_ticker"] == mt].sort_values("timestamp").head(10)
        readable = pd.to_datetime(sub["timestamp"], unit="s", utc=True)
        diffs = sub["timestamp"].diff() / 60.0
        print(f"\nManual gap check -- {mt} (first candles, elapsed-minutes from previous row):")
        check_df = pd.DataFrame({"timestamp_utc": readable.values, "elapsed_minutes_from_prev": diffs.values})
        print(check_df.to_string(index=False))

    n_null_eligible = int((~gaps["quote_eligible"]).sum())
    print(f"\nGaps with a null pre/post quote value (excluded from same-quote stats): {n_null_eligible} of {len(gaps)}")

    n_all = len(gaps)
    n_excl = int((~gaps["touches_empty_book"]).sum())
    print(f"Gaps total: {n_all}; gaps NOT touching an empty-book candle: {n_excl} "
          f"({n_all - n_excl} touch an empty-book candle on at least one side)")

    print(f"Gaps starting at/after market close (post_close): {int(gaps['post_close'].sum())} of {len(gaps)}")

    cols = ["market_ticker", "pre_ts", "post_ts", "missing_minutes",
            "pre_bid_close", "pre_ask_close", "post_bid_open", "post_ask_open"]

    same_mask = gaps["same_quote"]
    if same_mask.any():
        print("\nSpot-check: example SAME-quote gaps:")
        ex = gaps[same_mask].sample(min(3, int(same_mask.sum())), random_state=1)
        print(ex[cols].to_string(index=False))

    changed_mask = gaps["quote_eligible"] & ~gaps["same_quote"]
    if changed_mask.any():
        print("\nSpot-check: example CHANGED-quote gaps:")
        ex = gaps[changed_mask].sample(min(3, int(changed_mask.sum())), random_state=2)
        print(ex[cols].to_string(index=False))

    fu_mask = consec["fully_unchanged"] if "fully_unchanged" in consec.columns else pd.Series(dtype=bool)
    if fu_mask.any():
        print("\nSpot-check: example fully-unchanged consecutive candles:")
        ex = consec[fu_mask].sample(min(3, int(fu_mask.sum())), random_state=3)
        print(
            ex[["market_ticker", "pre_ts", "post_ts", "pre_bid_close", "pre_ask_close",
                "post_bid_open", "post_ask_open", "post_volume"]].to_string(index=False)
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    df, bounds = load_data()
    validate(df, bounds)
    summary = liquidity_density(df, bounds)
    market_structures_by_year(df)
    RAW_MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(RAW_MINUTE_DIR / "liquidity_density_summary.csv")

    print("\n\n" + "#" * 78)
    print("# GAP / QUOTE-STALENESS INVESTIGATION")
    print("# Hypothesis under test: many missing minutes, especially 2023-2024, may reflect")
    print("# a stale/unchanged order book. This section tries to FALSIFY that, not prove it.")
    print("#" * 78)

    pairs = build_pairs(df, bounds)
    gaps = pairs[pairs["elapsed_seconds"] > 60].copy()
    gaps["missing_bucket"] = _bucket_missing_minutes(gaps["missing_minutes"])
    consec = pairs[pairs["elapsed_seconds"] == 60].copy()
    del pairs

    gap_by_year = analyze_gaps(gaps)

    print("\n" + "-" * 78)
    print("Including ALL valid bid/ask observations (empty-book candles included):")
    print("-" * 78)
    same_quote_incl = analyze_gap_quotes(gaps, "including_empty_book")

    gaps_excl = gaps[~gaps["touches_empty_book"]].copy()
    print("\n" + "-" * 78)
    print("Excluding is_empty_book candles -- PRIMARY pre-close analysis (Part 8):")
    print("-" * 78)
    same_quote_excl = analyze_gap_quotes(gaps_excl, "excluding_empty_book")

    incl_overall = gaps[gaps["quote_eligible"]]["same_quote"].mean() * 100
    excl_overall = gaps_excl[gaps_excl["quote_eligible"]]["same_quote"].mean() * 100
    print(
        f"\nMateriality check (Part 8): overall same_quote% including empty-book = {incl_overall:.2f}%, "
        f"excluding empty-book = {excl_overall:.2f}% (difference = {incl_overall - excl_overall:.2f} points). "
        f"{'This is a material difference.' if abs(incl_overall - excl_overall) >= 1 else 'This is NOT a material difference.'}"
    )

    df, consec, flat_by_year = analyze_flat_candles(df, consec)
    zvqc_by_year = analyze_zero_volume_quote_changes(consec)
    analyze_gap_endpoint_patterns(gaps_excl, "excluding_empty_book")
    analyze_lifecycle(gaps)
    market_summary = analyze_market_level(df, gaps_excl)
    analyze_liquidity_relationship(market_summary)
    validate_gap_methodology(df, gaps, consec)

    print("\n" + "=" * 78)
    print("GAP ANALYSIS -- PART 12: YEARLY MARKET REGIME COMPARISON")
    print("=" * 78)
    final = pd.DataFrame(
        {
            "coverage_pct": summary["coverage_pct_of_possible_market_minutes"],
            "median_spread": summary["median_spread"],
            "total_volume": summary["total_volume"],
            "median_gap": gap_by_year["median"],
            "p95_gap": gap_by_year["p95"],
            "same_quote_gap_pct_excl_empty": same_quote_excl["same_quote_pct"],
            "fully_unchanged_consec_pct": flat_by_year["pct_consecutive_fully_unchanged"],
            "zero_volume_quote_change_pct": zvqc_by_year["changed_quote_pct_of_eligible"],
            "median_market_level_same_quote_pct": market_summary.groupby("year")["pct_gaps_same_quote"].median(),
        }
    )
    print(final.round(3).to_string())
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    final.to_csv(ANALYSIS_DIR / "yearly_regime_comparison.csv")

    print(f"\nAll gap-analysis summary tables saved under {ANALYSIS_DIR}")
    print("(Raw and processed datasets were only read, never modified.)")


if __name__ == "__main__":
    main()
