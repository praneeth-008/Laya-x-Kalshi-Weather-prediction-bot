"""PART B -- resumable bulk downloader for 1-HOUR KXHIGHNY/HIGHNY candlesticks.

Downloads and permanently stores raw event metadata, market metadata, and
hourly candlesticks for every NYC daily-high event in the existing cached
event universe (data/raw/kalshi/kxhighny_events.json). Does NOT touch
weather data, Laya integration, modeling, or trading logic.

Design for resumability (STEP B3/B4):
  - Every unit of work (one event's metadata, one event's market list, one
    market's candlesticks) is written to its own file, named deterministically
    from its ticker.
  - Before fetching anything, we check whether the target file already
    exists; if so we skip the API call entirely. Re-running this script
    after an interruption therefore resumes automatically -- completed
    work is never re-fetched or overwritten.
  - A candlestick file is only written on SUCCESS. A failed market is
    logged (not silently ignored) and simply has no file yet, so the next
    run retries exactly the markets that failed, nothing more.
  - checkpoint.json is rewritten after every event, recording per-event
    status (done / partial / failed) and any errors, for progress
    reporting and post-hoc auditing.

Rate limiting / retries / timeouts are centralized in data/kalshi.py's
kalshi_get() (exponential backoff + jitter on 429/5xx, 30s timeout) -- see
that module's docstring for the rate-limit documentation this follows.

Usage:
    python scripts/download_kalshi_hourly.py
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import (  # noqa: E402
    OUTPUT_PATH as EVENTS_JSON_PATH,
)
from data.kalshi import (  # noqa: E402
    classify_market_structure,
    fetch_event_markets,
    fetch_event_metadata,
    fetch_historical_cutoff,
    fetch_market_candlesticks,
    safe_filename,
)

RAW_HOURLY_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "hourly"
EVENTS_DIR = RAW_HOURLY_DIR / "events"
MARKETS_DIR = RAW_HOURLY_DIR / "markets"
CANDLES_DIR = RAW_HOURLY_DIR / "candlesticks"
CHECKPOINT_PATH = RAW_HOURLY_DIR / "checkpoint.json"
MANIFEST_PATH = RAW_HOURLY_DIR / "event_manifest.csv"

PERIOD_INTERVAL_MINUTES = 60


def _event_date(event_ticker: str):
    m = re.match(r"^(?:KX)?HIGHNY-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    try:
        return datetime.strptime(f"20{yy}-{mon}-{dd}", "%Y-%b-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_event_universe(events_path: Path) -> list[str]:
    with open(events_path, "r", encoding="utf-8") as f:
        events = json.load(f)
    tickers = [e["event_ticker"] for e in events]
    tickers.sort(key=lambda t: (_event_date(t) or datetime.min.replace(tzinfo=timezone.utc), t))
    return tickers


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"events": {}, "started_at": datetime.now(timezone.utc).isoformat()}


def save_checkpoint(checkpoint: dict) -> None:
    checkpoint["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    RAW_HOURLY_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CHECKPOINT_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2)
    # Windows can transiently deny a replace() if another process (e.g. a
    # concurrent read of checkpoint.json, an AV scan) has the target
    # briefly open -- this actually happened once during this project's
    # own download run. Retry a few times with a short backoff rather
    # than letting one transient lock kill an hours-long download.
    last_exc = None
    for attempt in range(5):
        try:
            tmp_path.replace(CHECKPOINT_PATH)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


def process_event(event_ticker: str, cutoff: dict) -> dict:
    """Download (or reuse already-downloaded) metadata/markets/candlesticks
    for one event. Returns a status record for the checkpoint."""
    event_file = EVENTS_DIR / f"{safe_filename(event_ticker)}.json"
    markets_file = MARKETS_DIR / f"{safe_filename(event_ticker)}.json"

    # --- Event metadata ---
    if event_file.exists():
        with open(event_file, "r", encoding="utf-8") as f:
            event_meta = json.load(f)
    else:
        try:
            event_meta = fetch_event_metadata(event_ticker)
        except RuntimeError as exc:
            return {"status": "failed", "error": f"event metadata: {exc}", "n_markets": 0, "n_failed_markets": 0}
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(event_file, "w", encoding="utf-8") as f:
            json.dump(event_meta, f, indent=2)

    # --- Markets list ---
    if markets_file.exists():
        with open(markets_file, "r", encoding="utf-8") as f:
            markets = json.load(f)
        market_source = "cached"
    else:
        try:
            markets, market_source = fetch_event_markets(event_ticker)
        except RuntimeError as exc:
            return {"status": "failed", "error": f"markets list: {exc}", "n_markets": 0, "n_failed_markets": 0}
        MARKETS_DIR.mkdir(parents=True, exist_ok=True)
        with open(markets_file, "w", encoding="utf-8") as f:
            json.dump(markets, f, indent=2)

    structure = classify_market_structure(markets)

    # --- Candlesticks, one market at a time ---
    CANDLES_DIR.mkdir(parents=True, exist_ok=True)
    failed_markets = []
    for m in markets:
        ticker = m["ticker"]
        candle_file = CANDLES_DIR / f"{safe_filename(ticker)}.json"
        if candle_file.exists():
            continue  # already downloaded successfully in a prior run
        candles, path, error = fetch_market_candlesticks(m, PERIOD_INTERVAL_MINUTES, cutoff)
        if error:
            failed_markets.append({"ticker": ticker, "endpoint": path, "error": error})
            continue  # do NOT write a file -- next resume will retry this market
        with open(candle_file, "w", encoding="utf-8") as f:
            json.dump(candles, f, indent=2)

    status = "done" if not failed_markets else ("partial" if len(failed_markets) < len(markets) else "failed")
    return {
        "status": status,
        "n_markets": len(markets),
        "market_source": market_source,
        "structure": structure,
        "n_failed_markets": len(failed_markets),
        "failed_markets": failed_markets,
        "event_meta": event_meta,
    }


def build_manifest(checkpoint: dict) -> None:
    """Rebuild the event-level manifest (STEP B1) from checkpoint + saved
    event metadata files -- cheap to redo on every run."""
    rows = []
    for event_ticker, record in checkpoint["events"].items():
        event_file = EVENTS_DIR / f"{safe_filename(event_ticker)}.json"
        event_meta = {}
        if event_file.exists():
            with open(event_file, "r", encoding="utf-8") as f:
                event_meta = json.load(f)
        settlement_sources = event_meta.get("settlement_sources") or []
        rows.append(
            {
                "event_ticker": event_ticker,
                "series_ticker": event_meta.get("series_ticker"),
                "event_date": (_event_date(event_ticker).date().isoformat() if _event_date(event_ticker) else None),
                "strike_date_raw": event_meta.get("strike_date"),
                "title": event_meta.get("title"),
                "mutually_exclusive": event_meta.get("mutually_exclusive"),
                "settlement_source": settlement_sources[0]["name"] if settlement_sources else None,
                "market_count": record.get("n_markets"),
                "market_structure": record.get("structure"),
                "status": record.get("status"),
                "n_failed_markets": record.get("n_failed_markets", 0),
            }
        )
    import pandas as pd

    pd.DataFrame(rows).to_csv(MANIFEST_PATH, index=False)


def main() -> None:
    print("Loading event universe from cached bulk file ...")
    tickers = load_event_universe(EVENTS_JSON_PATH)
    print(f"Event universe: {len(tickers)} events")

    print("Fetching GET /historical/cutoff once (used to route every market's candlesticks) ...")
    cutoff = fetch_historical_cutoff()
    print(json.dumps(cutoff, indent=2))

    checkpoint = load_checkpoint()
    already_done = {t for t, r in checkpoint["events"].items() if r.get("status") == "done"}
    print(f"Resuming: {len(already_done)} events already marked done in checkpoint.")

    start_time = time.time()
    n_processed_this_run = 0
    for i, event_ticker in enumerate(tickers, start=1):
        existing = checkpoint["events"].get(event_ticker)
        if existing and existing.get("status") == "done":
            continue  # fully resumed from a prior run, no API calls needed

        result = process_event(event_ticker, cutoff)
        checkpoint["events"][event_ticker] = {
            "status": result["status"],
            "n_markets": result.get("n_markets", 0),
            "structure": result.get("structure"),
            "n_failed_markets": result.get("n_failed_markets", 0),
            "failed_markets": result.get("failed_markets", []),
            "error": result.get("error"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_checkpoint(checkpoint)  # persist after every single event -- crash-safe resumability
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
    print("\n=== DOWNLOAD COMPLETE ===")
    print(f"Events by status: {statuses}")
    print(f"Manifest written to {MANIFEST_PATH}")
    print(f"Checkpoint at {CHECKPOINT_PATH}")


if __name__ == "__main__":
    main()
