"""PART B (continued) -- normalize the raw hourly download into ONE
standardized schema and store it as partitioned Parquet.

Reads the immutable raw files written by scripts/download_kalshi_hourly.py
under data/raw/kalshi/hourly/{events,markets,candlesticks}/ and produces a
single processed dataset under data/processed/kalshi/. Raw files are never
modified. Does NOT touch weather data, Laya integration, modeling, or
trading logic.

Partitioning choice (STEP B6): by year only (Hive-style year=YYYY/ dirs).
NYC daily-high events run ~365/year with up to ~6 markets and a few dozen
hourly candles each, so a full year is at most a few hundred thousand
rows -- comfortably one manageable Parquet file per year. Partitioning by
individual event (as originally suggested) would create ~1,800+ tiny
partition directories for a dataset this size, which is worse for both
storage overhead and later point-in-time queries; year is the coarsest
partition that still lets a query for "just 2024" or "2023-present" skip
reading the rest of the dataset.

Point-in-time rule (STEP B8): this script does not forward-fill,
back-fill, interpolate, or otherwise fabricate a missing Kalshi quote.
Every OHLC value is either what Kalshi returned or NaN. A `yes_bid=0,
yes_ask=1` empty-book placeholder is flagged via `is_empty_book`, not
replaced or reinterpreted as a 50% probability.

Usage:
    python scripts/build_kalshi_hourly_dataset.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import (  # noqa: E402
    SERIES_TICKER,
    classify_market_structure,
    fetch_historical_cutoff,
    normalize_candlestick,
    route_candlestick_endpoint,
    safe_filename,
)
from scripts.download_kalshi_hourly import (  # noqa: E402
    CANDLES_DIR,
    CHECKPOINT_PATH,
    EVENTS_DIR,
    MARKETS_DIR,
)

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "kalshi"


def _event_date(event_ticker: str):
    m = re.match(r"^(?:KX)?HIGHNY-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    try:
        return datetime.strptime(f"20{yy}-{mon}-{dd}", "%Y-%b-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_rows(checkpoint: dict, cutoff: dict) -> tuple[list[dict], dict]:
    """Walk every downloaded event/market/candlestick file and build the
    standardized row list. Also returns a small stats dict used later in
    the validation report (things easiest to compute while walking)."""
    rows = []
    stats = {
        "events_seen": 0,
        "markets_with_candles": 0,
        "markets_without_candles": 0,
        "source_endpoint_path_cache": {},
    }

    for event_ticker, record in checkpoint["events"].items():
        stats["events_seen"] += 1
        event_file = EVENTS_DIR / f"{safe_filename(event_ticker)}.json"
        markets_file = MARKETS_DIR / f"{safe_filename(event_ticker)}.json"
        if not event_file.exists() or not markets_file.exists():
            continue  # this event never got far enough to have anything to read

        event_meta = _load_json(event_file)
        markets = _load_json(markets_file)
        structure = classify_market_structure(markets)
        event_date = _event_date(event_ticker)
        settlement_sources = event_meta.get("settlement_sources") or []
        settlement_source = settlement_sources[0]["name"] if settlement_sources else None

        for market in markets:
            ticker = market["ticker"]
            candle_file = CANDLES_DIR / f"{safe_filename(ticker)}.json"
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
            settlement_value = (
                float(settlement_value_raw) if settlement_value_raw not in (None, "") else None
            )
            expiration_value_raw = market.get("expiration_value")
            try:
                expiration_value = (
                    float(expiration_value_raw) if expiration_value_raw not in (None, "") else None
                )
            except ValueError:
                expiration_value = None  # non-numeric text -- do not invent a number

            for c in candles:
                n = normalize_candlestick(c)
                is_empty_book = None
                if n["yes_bid_close"] is not None and n["yes_ask_close"] is not None:
                    is_empty_book = bool(n["yes_bid_close"] == 0 and n["yes_ask_close"] == 1)

                rows.append(
                    {
                        "event_ticker": event_ticker,
                        "market_ticker": ticker,
                        "event_date": event_date.date().isoformat() if event_date else None,
                        "year": event_date.year if event_date else None,
                        "timestamp": n["timestamp"],
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
                    }
                )
    return rows, stats


def validate(df: pd.DataFrame, checkpoint: dict) -> None:
    print("\n=== STEP B7: VALIDATION ===")

    n_events_total = len(checkpoint["events"])
    status_counts = {}
    structure_counts = {}
    for r in checkpoint["events"].values():
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        structure_counts[r.get("structure")] = structure_counts.get(r.get("structure"), 0) + 1

    print(f"Total events discovered (event universe): {n_events_total}")
    print(f"Events by download status: {status_counts}")
    print(f"Events by market_structure: {structure_counts}")

    total_markets = sum(r.get("n_markets", 0) for r in checkpoint["events"].values())
    print(f"Total markets discovered: {total_markets}")
    print(f"Distinct markets present in the processed dataset: {df['market_ticker'].nunique()}")

    if df.empty:
        print("Processed dataset is EMPTY -- nothing further to validate.")
        return

    print(f"\nTotal hourly candlestick rows: {len(df)}")
    earliest = datetime.fromtimestamp(df["timestamp"].min(), tz=timezone.utc)
    latest = datetime.fromtimestamp(df["timestamp"].max(), tz=timezone.utc)
    print(f"Earliest candle timestamp: {earliest}")
    print(f"Latest candle timestamp:   {latest}")

    print("\nRows by year:")
    print(df.groupby("year").size().to_string())
    print("\nEvents by year:")
    print(df.groupby("year")["event_ticker"].nunique().to_string())

    print("\nSettlement sources and event-date ranges:")
    src_ranges = df.groupby("settlement_source")["event_date"].agg(["min", "max", "nunique"])
    print(src_ranges.to_string())

    print("\nMissingness by important field (% null):")
    important_fields = [
        "yes_bid_close", "yes_ask_close", "price_close", "volume", "open_interest",
        "result", "settlement_value", "expiration_value", "floor_strike", "cap_strike", "strike_type",
    ]
    missingness = (df[important_fields].isna().mean() * 100).round(1)
    print(missingness.to_string())

    dup_count = int(df.duplicated(subset=["market_ticker", "timestamp"]).sum())
    print(f"\nDuplicate rows on (market_ticker, timestamp): {dup_count}")

    # Exactly-one-YES check for finalized bucketed events with result data.
    print("\nExactly-one-YES check (finalized bucketed events with result data):")
    bucketed = df[df["market_structure"] == "bucketed"]
    has_result = bucketed[bucketed["result"].isin(["yes", "no"])]
    per_event_markets = has_result.drop_duplicates(subset=["market_ticker"])
    yes_counts = per_event_markets[per_event_markets["result"] == "yes"].groupby("event_ticker").size()
    all_bucketed_events = per_event_markets.groupby("event_ticker").size()
    violations = []
    for event_ticker in all_bucketed_events.index:
        n_yes = int(yes_counts.get(event_ticker, 0))
        if n_yes != 1:
            violations.append((event_ticker, n_yes))
    print(f"  Bucketed events with settlement data checked: {len(all_bucketed_events)}")
    if violations:
        print(f"  VIOLATIONS (event_ticker, n_yes_markets) -- {len(violations)} found, NOT auto-fixed:")
        for event_ticker, n_yes in violations[:50]:
            print(f"    {event_ticker}: {n_yes} YES markets")
        if len(violations) > 50:
            print(f"    ... and {len(violations) - 50} more")
    else:
        print("  No violations -- every checked bucketed event has exactly one YES market.")


def save_partitioned_parquet(df: pd.DataFrame) -> None:
    # pyarrow's partitioned writer appends new part-files rather than
    # replacing a partition's contents, so a rerun of this script without
    # clearing the target first would silently duplicate every row. This
    # script is meant to be safely re-runnable as the raw download
    # progresses, so we rebuild the processed dataset from scratch each time.
    import shutil

    if PROCESSED_DIR.exists():
        shutil.rmtree(PROCESSED_DIR)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PROCESSED_DIR, partition_cols=["year"], index=False, engine="pyarrow")
    print(f"\nSaved partitioned Parquet dataset to {PROCESSED_DIR} (partitioned by year=YYYY/)")


def _dir_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    if not CHECKPOINT_PATH.exists():
        raise RuntimeError(
            f"No checkpoint found at {CHECKPOINT_PATH} -- run scripts/download_kalshi_hourly.py first."
        )
    checkpoint = _load_json(CHECKPOINT_PATH)

    print("Fetching GET /historical/cutoff (used to reconstruct which endpoint served each market's "
          "candlesticks -- see module docstring on why this is safe to recompute rather than having "
          "been persisted per file).")
    cutoff = fetch_historical_cutoff()

    print("Building standardized rows from raw downloaded files ...")
    rows, walk_stats = build_rows(checkpoint, cutoff)
    print(f"Markets with candlestick data: {walk_stats['markets_with_candles']}")
    print(f"Markets discovered but with NO candlestick data yet: {walk_stats['markets_without_candles']}")

    df = pd.DataFrame(rows)
    validate(df, checkpoint)

    if not df.empty:
        save_partitioned_parquet(df)

    raw_dir = PROJECT_ROOT / "data" / "raw" / "kalshi" / "hourly"
    if raw_dir.exists():
        raw_bytes = _dir_size_bytes(raw_dir)
        print(f"\nApproximate raw data size: {raw_bytes / (1024 * 1024):.1f} MB ({raw_dir})")
    if PROCESSED_DIR.exists():
        processed_bytes = _dir_size_bytes(PROCESSED_DIR)
        print(f"Approximate processed Parquet size: {processed_bytes / (1024 * 1024):.1f} MB ({PROCESSED_DIR})")


if __name__ == "__main__":
    main()
