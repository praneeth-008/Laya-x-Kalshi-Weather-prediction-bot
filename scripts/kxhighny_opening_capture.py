"""One-shot, high-precision capture of a single KXHIGHNY event's opening
moments -- the exact first quote, not just "near" it.

Runs completely independently of the main recorder daemon
(scripts/kxhighny_recorder.py) and writes to a clearly separate
subdirectory (data/live_kalshi_weather/{date}/opening_capture/) so it can
NEVER collide with the daemon's own eventual recording of the same event
into data/live_kalshi_weather/{date}/{metadata,websocket,trades,logs}/ --
the daemon still discovers and records the event normally on its own
5-minute poll cycle; this script exists purely to get the first few
minutes with sub-second precision, which the daemon's poll interval
cannot guarantee.

Strategy:
  1. Sleep until shortly before the event's known/expected open_time
     (PRE_OPEN_LEAD_SECONDS).
  2. Poll Kalshi's /events endpoint at POLL_SECONDS (1s) intervals --
     far tighter than the daemon's 300s or the earlier pure-watcher's 10s
     -- until the target event is confirmed genuinely active.
  3. Connect, authenticate, and subscribe within milliseconds of
     confirming the event is live.
  4. Record raw messages (same lossless format as the main daemon) for
     CAPTURE_WINDOW_SECONDS, then disconnect cleanly and write a summary
     with the first quote/snapshot/trade for each market, extracted
     directly from the raw capture (never inferred).

This is READ-ONLY. No order-placement code exists here or anywhere in
this project.

Usage:
    python scripts/kxhighny_opening_capture.py --target-date 2026-10-07
"""
import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

import websockets

from data.kalshi_auth import load_credentials_from_env, build_ws_auth_headers
from scripts.kxhighny_recorder import (
    discover_active_events,
    derive_target_date,
    DiscoveryAmbiguousError,
    OUT_ROOT,
    SUBSCRIBE_CHUNK_SIZE,
)

POLL_SECONDS = 1.0
PRE_OPEN_LEAD_SECONDS = 5 * 60
DISCOVERY_TIMEOUT_MINUTES = 30
CAPTURE_WINDOW_SECONDS = 10 * 60


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [opening_capture] {msg}", flush=True)


def discover_target(target_date: date, max_minutes: float):
    """Polls at POLL_SECONDS intervals until the target event is found.
    Returns (event, discovery_time) or (None, None) on timeout."""
    deadline = time.monotonic() + max_minutes * 60
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            events = discover_active_events()
        except DiscoveryAmbiguousError as e:
            log(f"discovery ambiguous (attempt {attempts}): {e}")
            time.sleep(POLL_SECONDS)
            continue
        except Exception as e:
            log(f"discovery error (attempt {attempts}): {type(e).__name__}: {e}")
            time.sleep(POLL_SECONDS)
            continue
        for e in events:
            if derive_target_date(e) == target_date:
                return e, datetime.now(timezone.utc)
        time.sleep(POLL_SECONDS)
    return None, None


def extract_first_quotes(raw_path: Path) -> dict:
    """Re-derives the first snapshot's best bid/ask per market directly
    from the raw capture file -- never inferred, always read back from
    what was actually recorded."""
    quotes = {}
    with open(raw_path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            msg = json.loads(rec["raw_message"])
            if msg.get("type") != "orderbook_snapshot":
                continue
            m = msg["msg"]
            ticker = m["market_ticker"]
            if ticker in quotes:
                continue
            yes_levels = [Decimal(p) for p, _ in (m.get("yes_dollars_fp") or [])]
            no_levels = [Decimal(p) for p, _ in (m.get("no_dollars_fp") or [])]
            quotes[ticker] = {
                "local_receive_timestamp_iso": rec["local_receive_timestamp_iso"],
                "seq": msg.get("seq"),
                "best_yes_bid": str(max(yes_levels)) if yes_levels else None,
                "best_no_bid": str(max(no_levels)) if no_levels else None,
                "n_yes_levels": len(yes_levels),
                "n_no_levels": len(no_levels),
            }
    return quotes


def extract_first_trade(trades_path: Path) -> dict | None:
    if not trades_path.exists():
        return None
    with open(trades_path, encoding="utf-8") as f:
        first_line = f.readline()
    if not first_line:
        return None
    rec = json.loads(first_line)
    msg = json.loads(rec["raw_message"])
    recv_iso = datetime.fromtimestamp(rec["local_receive_timestamp"], tz=timezone.utc).isoformat()
    return {"local_receive_timestamp_iso": recv_iso, **msg.get("msg", {})}


async def capture(event: dict, key_id: str, private_key, ws_url: str, out_dir: Path, window_seconds: float) -> dict:
    market_tickers = sorted(m["ticker"] for m in event["markets"])
    (out_dir / "metadata").mkdir(parents=True, exist_ok=True)
    (out_dir / "websocket").mkdir(parents=True, exist_ok=True)
    (out_dir / "trades").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "metadata" / "event.json", "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2, default=str)

    raw_path = out_dir / "websocket" / "websocket_raw.jsonl"
    trades_path = out_dir / "trades" / "trades_raw.jsonl"

    headers = build_ws_auth_headers(key_id, private_key)
    connect_time = datetime.now(timezone.utc)
    n_messages = 0
    async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=20) as ws:
        sub_time = datetime.now(timezone.utc)
        for i in range(0, len(market_tickers), SUBSCRIBE_CHUNK_SIZE):
            chunk = market_tickers[i : i + SUBSCRIBE_CHUNK_SIZE]
            await ws.send(json.dumps({"id": i + 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": chunk}}))
        log(f"connected {connect_time.isoformat()}, subscribed {sub_time.isoformat()}, capturing for {window_seconds:.0f}s")

        deadline = time.monotonic() + window_seconds
        trade_seen = False
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            recv_ts = time.time()
            rec = {
                "local_receive_timestamp": recv_ts,
                "local_receive_timestamp_iso": datetime.fromtimestamp(recv_ts, tz=timezone.utc).isoformat(),
                "raw_message": raw,
            }
            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            n_messages += 1
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "trade":
                with open(trades_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"local_receive_timestamp": recv_ts, "raw_message": raw}) + "\n")
                trade_seen = True

    first_quotes = extract_first_quotes(raw_path)
    first_trade = extract_first_trade(trades_path) if trade_seen else None
    summary = {
        "event_ticker": event["event_ticker"],
        "market_tickers": market_tickers,
        "connect_time_utc": connect_time.isoformat(),
        "subscribe_time_utc": sub_time.isoformat(),
        "capture_window_seconds": window_seconds,
        "n_messages_captured": n_messages,
        "first_quote_by_market": first_quotes,
        "first_trade": first_trade,
        "note": (
            "This is a supplementary, short, high-precision capture of this event's "
            "opening moments only -- the main recorder daemon separately records the "
            "full event lifecycle into the sibling metadata/websocket/trades/logs "
            "directories under this same date, on its own ~5-minute discovery cycle. "
            "The two never write to the same files."
        ),
    }
    with open(out_dir / "metadata" / "opening_capture_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary


def main(target_date: date) -> None:
    expected_open_utc = datetime.combine(target_date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=14)
    lead_start = expected_open_utc - timedelta(seconds=PRE_OPEN_LEAD_SECONDS)
    now = datetime.now(timezone.utc)
    if now < lead_start:
        wait_s = (lead_start - now).total_seconds()
        log(f"expected open_time {expected_open_utc.isoformat()}; sleeping {wait_s:.0f}s until {lead_start.isoformat()}")
        time.sleep(wait_s)

    log(f"starting tight discovery polling (every {POLL_SECONDS}s) for target_date={target_date}")
    event, discovery_time = discover_target(target_date, DISCOVERY_TIMEOUT_MINUTES)
    if event is None:
        log(f"FAILED to discover target_date={target_date} within {DISCOVERY_TIMEOUT_MINUTES} minutes -- giving up")
        sys.exit(1)
    log(f"discovered {event['event_ticker']} at {discovery_time.isoformat()} (expected open_time {expected_open_utc.isoformat()}, "
        f"latency {(discovery_time - expected_open_utc).total_seconds():.2f}s)")

    key_id, private_key, env = load_credentials_from_env()
    from data.kalshi_auth import WS_BASE_URLS
    ws_url = WS_BASE_URLS[env]
    out_dir = OUT_ROOT / target_date.isoformat() / "opening_capture"

    summary = asyncio.run(capture(event, key_id, private_key, ws_url, out_dir, CAPTURE_WINDOW_SECONDS))
    log(f"capture complete: {summary['n_messages_captured']} messages, first_quote_by_market covers {len(summary['first_quote_by_market'])} markets")
    log(f"summary written to {out_dir / 'metadata' / 'opening_capture_summary.json'}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--target-date", required=True, help="YYYY-MM-DD")
    args = p.parse_args()
    main(date.fromisoformat(args.target_date))
