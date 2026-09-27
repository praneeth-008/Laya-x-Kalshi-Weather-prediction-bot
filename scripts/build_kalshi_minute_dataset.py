"""Normalize the raw 1-minute download into ONE standardized schema and
store it as partitioned Parquet, mirroring build_kalshi_hourly_dataset.py.

Reads the immutable raw files written by scripts/download_kalshi_minute.py
under data/raw/kalshi/minute/{year}/candlesticks/, plus the event/market
metadata already downloaded by scripts/download_kalshi_hourly.py (reused,
not duplicated). Raw files are never modified. Does NOT touch weather
data, Laya integration, modeling, backtesting, or risk management.

Partitioning: year=YYYY/ (same reasoning as the hourly dataset -- see that
script's docstring -- but even more relevant here since 1-minute data is
~50x the row count of hourly data per year).

Point-in-time rule (spec Part 9): no forward-filling, no interpolation, no
synthetic minutes. Every row here is a candle Kalshi actually returned.
`is_empty_book` flags the yes_bid=0/yes_ask=1 placeholder; it does not
replace or reinterpret the raw quote.

Usage:
    python scripts/build_kalshi_minute_dataset.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import (  # noqa: E402
    SERIES_TICKER,
    classify_market_structure,
    event_date,
    fetch_historical_cutoff,
    normalize_candlestick,
    route_candlestick_endpoint,
    safe_filename,
)
from scripts.download_kalshi_minute import (  # noqa: E402
    CHECKPOINT_PATH,
    HOURLY_EVENTS_DIR,
    HOURLY_MARKETS_DIR,
    RAW_MINUTE_DIR,
)

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "kalshi" / "minute"


def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_rows(checkpoint: dict, cutoff: dict) -> tuple[list[dict], dict]:
    rows = []
    stats = {"markets_with_candles": 0, "markets_without_candles": 0}

    for event_ticker, record in checkpoint["events"].items():
        markets_file = HOURLY_MARKETS_DIR / f"{safe_filename(event_ticker)}.json"
        event_file = HOURLY_EVENTS_DIR / f"{safe_filename(event_ticker)}.json"
        if not markets_file.exists() or not event_file.exists():
            continue

        markets = _load_json(markets_file)
        event_meta = _load_json(event_file)
        structure = classify_market_structure(markets)
        d = event_date(event_ticker)
        settlement_sources = event_meta.get("settlement_sources") or []
        settlement_source = settlement_sources[0]["name"] if settlement_sources else None

        year = record.get("year") or (d.year if d else None)
        candle_dir = RAW_MINUTE_DIR / str(year) / "candlesticks"

        for market in markets:
            ticker = market["ticker"]
            candle_file = candle_dir / f"{safe_filename(ticker)}.json"
            if not candle_file.exists():
                stats["markets_without_candles"] += 1
                continue
            stats["markets_with_candles"] += 1

            candles = _load_json(candle_file)
            bucket = market.get("yes_sub_title") or market.get("subtitle")
            route = route_candlestick_endpoint(market, cutoff)
            source_endpoint = (
                f"/historical/markets/{ticker}/candlesticks"
                if route == "historical"
                else f"/series/{SERIES_TICKER}/markets/{ticker}/candlesticks"
            )

            settlement_value_raw = market.get("settlement_value_dollars")
            settlement_value = float(settlement_value_raw) if settlement_value_raw not in (None, "") else None
            expiration_value_raw = market.get("expiration_value")
            try:
                expiration_value = float(expiration_value_raw) if expiration_value_raw not in (None, "") else None
            except ValueError:
                expiration_value = None

            for c in candles:
                n = normalize_candlestick(c)
                is_empty_book = None
                if n["yes_bid_close"] is not None and n["yes_ask_close"] is not None:
                    is_empty_book = bool(n["yes_bid_close"] == 0 and n["yes_ask_close"] == 1)

                rows.append(
                    {
                        "event_ticker": event_ticker,
                        "market_ticker": ticker,
                        "event_date": d.date().isoformat() if d else None,
                        "timestamp": n["timestamp"],
                        "year": year,
                        "bucket": bucket,
                        "market_structure": structure,
                        "yes_bid_open": n["yes_bid_open"],
                        "yes_bid_high": n["yes_bid_high"],
                        "yes_bid_low": n["yes_bid_low"],
                        "yes_bid_close": n["yes_bid_close"],
                        "yes_ask_open": n["yes_ask_open"],
                        "yes_ask_high": n["yes_ask_high"],
                        "yes_ask_low": n["yes_ask_low"],
                        "yes_ask_close": n["yes_ask_close"],
                        "price_open": n["price_open"],
                        "price_high": n["price_high"],
                        "price_low": n["price_low"],
                        "price_close": n["price_close"],
                        "price_mean": n["price_mean"],
                        "volume": n["volume"],
                        "open_interest": n["open_interest"],
                        "result": market.get("result"),
                        "settlement_value": settlement_value,
                        "expiration_value": expiration_value,
                        "settlement_source": settlement_source,
                        "floor_strike": market.get("floor_strike"),
                        "cap_strike": market.get("cap_strike"),
                        "strike_type": market.get("strike_type"),
                        "source_endpoint": source_endpoint,
                        "is_empty_book": is_empty_book,
                        # kept only for validation (open/close bounds check), dropped before saving
                        "_open_time": market.get("open_time"),
                        "_close_time": market.get("close_time"),
                    }
                )
    return rows, stats


def save_partitioned_parquet(df: pd.DataFrame) -> None:
    import shutil

    if PROCESSED_DIR.exists():
        shutil.rmtree(PROCESSED_DIR)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.drop(columns=["_open_time", "_close_time"]).to_parquet(
        PROCESSED_DIR, partition_cols=["year"], index=False, engine="pyarrow"
    )
    print(f"\nSaved partitioned Parquet dataset to {PROCESSED_DIR} (partitioned by year=YYYY/)")


def _dir_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    if not CHECKPOINT_PATH.exists():
        raise RuntimeError(f"No checkpoint found at {CHECKPOINT_PATH} -- run scripts/download_kalshi_minute.py first.")
    checkpoint = _load_json(CHECKPOINT_PATH)

    cutoff = fetch_historical_cutoff()

    print("Building standardized minute rows from raw downloaded files ...")
    rows, walk_stats = build_rows(checkpoint, cutoff)
    print(f"Markets with candlestick data: {walk_stats['markets_with_candles']}")
    print(f"Markets discovered but with NO candlestick data yet: {walk_stats['markets_without_candles']}")

    df = pd.DataFrame(rows)
    print(f"Total rows built: {len(df)}")

    if not df.empty:
        # Save a copy of the boundary columns for analyze_kalshi_minute_data.py
        # before dropping them from the Parquet output.
        bounds_path = RAW_MINUTE_DIR / "_market_bounds_cache.parquet"
        df[["market_ticker", "_open_time", "_close_time"]].drop_duplicates().to_parquet(bounds_path, index=False)
        save_partitioned_parquet(df)

    raw_dir = PROJECT_ROOT / "data" / "raw" / "kalshi" / "minute"
    if raw_dir.exists():
        print(f"\nApproximate raw minute data size: {_dir_size_bytes(raw_dir) / (1024 * 1024):.1f} MB ({raw_dir})")
    if PROCESSED_DIR.exists():
        print(f"Approximate processed minute Parquet size: {_dir_size_bytes(PROCESSED_DIR) / (1024 * 1024):.1f} MB ({PROCESSED_DIR})")


if __name__ == "__main__":
    main()
