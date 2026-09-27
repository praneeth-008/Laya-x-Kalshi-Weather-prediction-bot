"""Resumable bulk downloader for 1-MINUTE KXHIGHNY/HIGHNY candlesticks,
2023-01-01 through the latest available event.

Reuses event/market metadata already downloaded by
scripts/download_kalshi_hourly.py (data/raw/kalshi/hourly/{events,markets}/)
instead of re-fetching it -- only the per-market candlestick call
(period_interval=1) is new work here. Does NOT touch weather data, Laya
integration, modeling, backtesting, or risk management.

Resumability / hardening (reused from data/kalshi.py, the same pattern
that survived the hourly download's Windows checkpoint-lock crash):
  - load_checkpoint_json / save_checkpoint_json (atomic write + retry).
  - A candlestick file is only written on SUCCESS; a failed market is
    logged and left without a file, so the next run retries exactly the
    markets that failed.
  - Every market failure is logged to failures.jsonl with event_ticker,
    market_ticker, endpoint, and the exact error (status + body, from
    kalshi_get's RuntimeError message) -- one bad market never aborts the
    run.

Endpoint routing (verified against docs.kalshi.com, see data/kalshi.py's
route_candlestick_endpoint docstring): GET /historical/cutoff is fetched
once and used to route each market to the live or historical candlestick
endpoint, exactly as for the hourly download -- period_interval=1 is
supported on both (verified in scripts/test_minute_coverage.py).

Usage:
    python scripts/download_kalshi_minute.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import (  # noqa: E402
    classify_market_structure,
    event_date,
    fetch_historical_cutoff,
    fetch_market_candlesticks,
    load_checkpoint_json,
    safe_filename,
    save_checkpoint_json,
)

HOURLY_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "hourly"
HOURLY_EVENTS_DIR = HOURLY_DIR / "events"
HOURLY_MARKETS_DIR = HOURLY_DIR / "markets"

RAW_MINUTE_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "minute"
CHECKPOINT_PATH = RAW_MINUTE_DIR / "checkpoint.json"
FAILURE_LOG_PATH = RAW_MINUTE_DIR / "failures.jsonl"
MANIFEST_PATH = RAW_MINUTE_DIR / "event_manifest.csv"

PERIOD_INTERVAL_MINUTES = 1
START_DATE = datetime(2023, 1, 1, tzinfo=timezone.utc)


def load_event_universe() -> list[str]:
    """2023-present event tickers, sourced from the already-downloaded
    hourly event metadata -- no new /events calls needed for this."""
    tickers = []
    for f in HOURLY_EVENTS_DIR.glob("*.json"):
        with open(f, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        ticker = meta.get("event_ticker", f.stem)
        d = event_date(ticker)
        if d and d >= START_DATE:
            tickers.append(ticker)
    tickers.sort(key=lambda t: (event_date(t) or datetime.min.replace(tzinfo=timezone.utc), t))
    return tickers


def log_failure(event_ticker: str, market_ticker: str, endpoint: str, error: str) -> None:
    RAW_MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "event_ticker": event_ticker,
                    "market_ticker": market_ticker,
                    "endpoint": endpoint,
                    "error": error,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )


def process_event(event_ticker: str, cutoff: dict) -> dict:
    markets_file = HOURLY_MARKETS_DIR / f"{safe_filename(event_ticker)}.json"
    if not markets_file.exists():
        return {"status": "failed", "error": "no cached markets file from hourly download", "n_markets": 0, "n_failed_markets": 0}

    with open(markets_file, "r", encoding="utf-8") as f:
        markets = json.load(f)
    structure = classify_market_structure(markets)

    year = event_date(event_ticker).year
    candle_dir = RAW_MINUTE_DIR / str(year) / "candlesticks"
    candle_dir.mkdir(parents=True, exist_ok=True)

    failed_markets = []
    for m in markets:
        ticker = m["ticker"]
        candle_file = candle_dir / f"{safe_filename(ticker)}.json"
        if candle_file.exists():
            continue  # already downloaded successfully in a prior run

        candles, path, error = fetch_market_candlesticks(m, PERIOD_INTERVAL_MINUTES, cutoff)
        if error:
            log_failure(event_ticker, ticker, path, error)
            failed_markets.append({"ticker": ticker, "endpoint": path, "error": error})
            continue  # do NOT write a file -- next resume retries this market
        with open(candle_file, "w", encoding="utf-8") as f:
            json.dump(candles, f, indent=2)

    status = "done" if not failed_markets else ("partial" if len(failed_markets) < len(markets) else "failed")
    return {
        "status": status,
        "n_markets": len(markets),
        "structure": structure,
        "year": year,
        "n_failed_markets": len(failed_markets),
    }


def build_manifest(checkpoint: dict) -> None:
    import pandas as pd

    rows = []
    for event_ticker, record in checkpoint["events"].items():
        d = event_date(event_ticker)
        rows.append(
            {
                "event_ticker": event_ticker,
                "event_date": d.date().isoformat() if d else None,
                "year": record.get("year"),
                "market_count": record.get("n_markets"),
                "market_structure": record.get("structure"),
                "status": record.get("status"),
                "n_failed_markets": record.get("n_failed_markets", 0),
            }
        )
    pd.DataFrame(rows).to_csv(MANIFEST_PATH, index=False)


def main() -> None:
    print("Loading 2023-present event universe from already-downloaded hourly metadata ...")
    tickers = load_event_universe()
    print(f"Event universe (2023-present): {len(tickers)} events")

    print("Fetching GET /historical/cutoff once ...")
    cutoff = fetch_historical_cutoff()
    print(json.dumps(cutoff, indent=2))

    checkpoint = load_checkpoint_json(CHECKPOINT_PATH)
    already_done = {t for t, r in checkpoint["events"].items() if r.get("status") == "done"}
    print(f"Resuming: {len(already_done)} events already marked done in checkpoint.")

    start_time = time.time()
    n_processed_this_run = 0
    for i, event_ticker in enumerate(tickers, start=1):
        existing = checkpoint["events"].get(event_ticker)
        if existing and existing.get("status") == "done":
            continue

        result = process_event(event_ticker, cutoff)
        checkpoint["events"][event_ticker] = {
            "status": result["status"],
            "n_markets": result.get("n_markets", 0),
            "structure": result.get("structure"),
            "year": result.get("year"),
            "n_failed_markets": result.get("n_failed_markets", 0),
            "error": result.get("error"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_checkpoint_json(CHECKPOINT_PATH, checkpoint)
        n_processed_this_run += 1

        if result["status"] != "done":
            print(f"  [{i}/{len(tickers)}] {event_ticker}: {result['status']} "
                  f"({result.get('n_failed_markets', 0)} market failures / {result.get('error')})")

        if i % 50 == 0 or i == len(tickers):
            elapsed = time.time() - start_time
            rate = n_processed_this_run / elapsed if elapsed > 0 else 0
            remaining = len(tickers) - i
            eta_min = (remaining / rate / 60) if rate > 0 else float("nan")
            print(
                f"Progress: {i}/{len(tickers)} events ({100*i/len(tickers):.1f}%) | "
                f"{n_processed_this_run} processed this run in {elapsed/60:.1f} min | "
                f"ETA ~{eta_min:.1f} min"
            )

    build_manifest(checkpoint)

    statuses = {}
    for r in checkpoint["events"].values():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print("\n=== MINUTE DOWNLOAD COMPLETE ===")
    print(f"Events by status: {statuses}")
    print(f"Manifest written to {MANIFEST_PATH}")
    print(f"Checkpoint at {CHECKPOINT_PATH}")
    if FAILURE_LOG_PATH.exists():
        n_failures = sum(1 for _ in open(FAILURE_LOG_PATH, encoding="utf-8"))
        print(f"Total logged market failures: {n_failures} (see {FAILURE_LOG_PATH})")


if __name__ == "__main__":
    main()
