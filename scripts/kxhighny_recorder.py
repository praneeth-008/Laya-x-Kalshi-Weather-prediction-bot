"""Live, continuous, READ-ONLY L2 order-book + trade recorder for Kalshi's
KXHIGHNY series (NYC daily maximum-temperature event).

This process:
  - discovers the currently-open KXHIGHNY event(s) via Kalshi's public
    /events endpoint (data/kalshi.py), verifying series/title/ticker before
    ever subscribing (fails closed on ambiguity -- see
    verify_event_is_kxhighny_nyc_tmax());
  - connects to Kalshi's authenticated market-data WebSocket
    (wss://external-api-ws.kalshi.com/trade-api/ws/v2, auth scheme in
    data/kalshi_auth.py, confirmed live 2026-10-04) and subscribes to the
    orderbook_delta and trade channels for every bucket market in that
    event;
  - writes every raw message to data/live_kalshi_weather/{date}/websocket/
    websocket_raw.jsonl (append-only, source of truth) before doing
    anything else with it;
  - maintains an in-memory OrderBook per market (data/kalshi_orderbook.py)
    purely to validate internal consistency as messages arrive -- NOT as
    the primary record;
  - detects and logs gaps/reconnects/duplicates/malformed messages/
    terminal channel errors, and resyncs (fresh snapshot via
    update_subscription/get_snapshot, or a full reconnect) rather than
    silently continuing across an unobserved period;
  - rolls over across KXHIGHNY days automatically, finalizing each day's
    manifest once its markets settle and discovering the next day's event
    without any manual restart;
  - NEVER calls, imports, or references any order-placement endpoint. This
    file contains no execution path to submitting/cancelling/modifying an
    order.

Usage:
    python scripts/kxhighny_recorder.py
"""
import asyncio
import hashlib
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from data.kalshi import kalshi_get, event_date
from data.kalshi_auth import load_credentials_from_env, build_ws_auth_headers, WS_BASE_URLS
from data.kalshi_orderbook import OrderBook, OrderBookConsistencyError, SequenceTracker

SERIES_TICKER = "KXHIGHNY"
OUT_ROOT = Path(__file__).resolve().parents[1] / "data" / "live_kalshi_weather"

DISCOVERY_POLL_SECONDS = 300
SETTLEMENT_POLL_SECONDS = 300
RECONNECT_BACKOFF_BASE = 2
RECONNECT_BACKOFF_MAX = 60
MIN_FREE_DISK_GB = 5.0
TERMINAL_ERROR_CODES = {10, 25}  # per Kalshi's AsyncAPI spec: channel error / subscription buffer overflow
SUBSCRIBE_CHUNK_SIZE = 20  # defensive: today has 6 buckets, but never assume that holds for every future day
CLOSE_GRACE_SECONDS = 300  # keep the WS open this long past close_time in case trailing messages arrive
MAX_SETTLEMENT_WAIT_SECONDS = 2 * 60 * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class DiscoveryAmbiguousError(RuntimeError):
    """Raised when a candidate event cannot be unambiguously confirmed as
    the genuine KXHIGHNY NYC max-temperature event -- discovery fails
    closed rather than guessing."""


def verify_event_is_kxhighny_nyc_tmax(event: dict) -> None:
    if event.get("series_ticker") != SERIES_TICKER:
        raise DiscoveryAmbiguousError(f"series_ticker {event.get('series_ticker')!r} != {SERIES_TICKER!r}")
    if not (event.get("event_ticker") or "").startswith("KXHIGHNY-"):
        raise DiscoveryAmbiguousError(f"event_ticker does not start with KXHIGHNY-: {event.get('event_ticker')!r}")
    title = (event.get("title") or "").lower()
    if "new york" not in title or "temperature" not in title:
        raise DiscoveryAmbiguousError(f"event title does not look like NYC max temperature: {event.get('title')!r}")
    markets = event.get("markets") or []
    if not markets:
        raise DiscoveryAmbiguousError(f"{event.get('event_ticker')}: no nested markets returned")


def derive_target_date(event: dict):
    d = event_date(event["event_ticker"])
    if d is None:
        raise DiscoveryAmbiguousError(f"could not derive a target date from ticker {event.get('event_ticker')!r}")
    return d.date()


def discover_active_events() -> list[dict]:
    """Every currently open/active KXHIGHNY event (0, 1, or occasionally 2
    during a day-rollover overlap). Verifies each one before returning it."""
    payload = kalshi_get(
        "/events",
        params={"series_ticker": SERIES_TICKER, "limit": 50, "with_nested_markets": "true", "status": "open"},
    )
    events = payload.get("events", [])
    for e in events:
        verify_event_is_kxhighny_nyc_tmax(e)
    return events


def refresh_event_markets(event_ticker: str) -> dict:
    payload = kalshi_get(f"/events/{event_ticker}", params={"with_nested_markets": "true"})
    return payload["event"]


def earliest_open_time(event: dict) -> datetime | None:
    times = [m.get("open_time") for m in event.get("markets", []) if m.get("open_time")]
    if not times:
        return None
    from data.kalshi import parse_kalshi_ts
    return min(datetime.fromtimestamp(parse_kalshi_ts(t), tz=timezone.utc) for t in times)


def latest_close_time(event: dict) -> datetime | None:
    times = [m.get("close_time") for m in event.get("markets", []) if m.get("close_time")]
    if not times:
        return None
    from data.kalshi import parse_kalshi_ts
    return max(datetime.fromtimestamp(parse_kalshi_ts(t), tz=timezone.utc) for t in times)


def all_markets_settled(event: dict) -> bool:
    markets = event.get("markets") or []
    return bool(markets) and all(m.get("status") == "settled" for m in markets)


@dataclass
class RecorderCounts:
    raw_messages: int = 0
    snapshots: int = 0
    deltas: int = 0
    trades: int = 0
    reconnects: int = 0
    gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    malformed: int = 0
    ws_errors: int = 0
    resyncs: int = 0
    unknown_market_messages: int = 0


class EventRecorder:
    def __init__(self, event: dict, key_id: str, private_key, ws_url: str, started_mid_event: bool):
        self.event = event
        self.event_ticker = event["event_ticker"]
        self.target_date = derive_target_date(event)
        self.market_tickers = sorted(m["ticker"] for m in event["markets"])
        self.key_id = key_id
        self.private_key = private_key
        self.ws_url = ws_url
        self.started_mid_event = started_mid_event
        self.out_dir = OUT_ROOT / self.target_date.isoformat()
        self.books: dict[str, OrderBook] = {t: OrderBook(t) for t in self.market_tickers}
        self.counts = RecorderCounts()
        self.start_time = datetime.now(timezone.utc)
        self.end_time: datetime | None = None
        self._stop_requested = False
        self._cmd_id = 0
        self._orderbook_sid: int | None = None
        self._trade_sid: int | None = None
        self._seq_trackers: dict[int, SequenceTracker] = {}
        self._pending_resync: set[str] = set()
        self._finalized = False
        self._first_snapshot_time_by_market: dict[str, datetime] = {}
        self._setup_dirs()
        self._write_discovery_metadata()

    # ---- setup / IO helpers ----

    def _setup_dirs(self) -> None:
        for sub in ("metadata", "websocket", "trades", "logs", "validation"):
            (self.out_dir / sub).mkdir(parents=True, exist_ok=True)
        self.raw_path = self.out_dir / "websocket" / "websocket_raw.jsonl"
        self.trades_path = self.out_dir / "trades" / "trades_raw.jsonl"
        self.gaps_path = self.out_dir / "logs" / "gaps.jsonl"
        self.recorder_log_path = self.out_dir / "logs" / "recorder.log"
        self.validation_path = self.out_dir / "validation" / "book_state_checkpoints.jsonl"

    def _log(self, msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat()} [{self.event_ticker}] {msg}"
        with open(self.recorder_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logging.info(line)

    def _write_discovery_metadata(self) -> None:
        meta_path = self.out_dir / "metadata" / "event.json"
        if not meta_path.exists():
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(self.event, f, indent=2, default=str)
        info_path = self.out_dir / "metadata" / "discovery_info.json"
        info = {
            "event_ticker": self.event_ticker,
            "target_date": self.target_date.isoformat(),
            "market_tickers": self.market_tickers,
            "recorder_start_utc": self.start_time.isoformat(),
            "started_mid_event": self.started_mid_event,
            "note": (
                "STARTED_MID_EVENT: recording began after this event's markets had already "
                "been open for a meaningful period; this is not a full-event capture."
                if self.started_mid_event else
                "Recording began at or near this event's own market open_time."
            ),
        }
        # Never overwrite an earlier recorder_start_utc/started_mid_event if this process restarted
        # mid-day and rediscovered the same event -- the FIRST observed start is what matters for labeling.
        if info_path.exists():
            with open(info_path, encoding="utf-8") as f:
                existing = json.load(f)
            info["recorder_start_utc"] = existing.get("recorder_start_utc", info["recorder_start_utc"])
            info["started_mid_event"] = existing.get("started_mid_event", info["started_mid_event"])
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, default=str)

    def _write_raw(self, raw_text: str, recv_ts: float) -> None:
        rec = {
            "local_receive_timestamp": recv_ts,
            "local_receive_timestamp_iso": datetime.fromtimestamp(recv_ts, tz=timezone.utc).isoformat(),
            "raw_message": raw_text,
        }
        with open(self.raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.counts.raw_messages += 1

    def _write_trade_copy(self, raw_text: str, recv_ts: float) -> None:
        rec = {"local_receive_timestamp": recv_ts, "raw_message": raw_text}
        with open(self.trades_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def _log_gap(self, kind: str, detail: str, market_ticker: str | None = None) -> None:
        rec = {"ts_utc": datetime.now(timezone.utc).isoformat(), "kind": kind, "detail": detail, "market_ticker": market_ticker}
        with open(self.gaps_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self._log(f"GAP/{kind} ({market_ticker}): {detail}")

    def _write_validation_checkpoint(self) -> None:
        rec = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "books": {t: {**b.best_bid_ask(), "has_snapshot": b.has_snapshot, "segment_id": b.segment_id} for t, b in self.books.items()},
            "seq_trackers": {str(sid): tr.last_seq for sid, tr in self._seq_trackers.items()},
            "counts": vars(self.counts),
        }
        with open(self.validation_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _next_cmd_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    # ---- message handling ----

    def _handle_message(self, data: dict) -> None:
        mtype = data.get("type")
        msg = data.get("msg") or {}
        seq = data.get("seq")

        if mtype in ("orderbook_snapshot", "orderbook_delta", "trade"):
            sid = data.get("sid")
            tracker = self._seq_trackers.setdefault(sid, SequenceTracker(sid_label=f"{mtype}:{sid}"))
            anomaly = tracker.check(seq) if seq is not None else None
            if anomaly == "DUPLICATE_OR_OLD":
                self.counts.duplicates += 1
                return
            if anomaly == "GAP":
                self.counts.gaps += 1
                self._log_gap("GAP", f"sid={sid} seq={seq} last_seq={tracker.last_seq}")
                tracker.advance(seq)
                # A gap on a shared sid means ANY market multiplexed under it may have
                # missed a delta -- invalidate every market on this sid, not just the
                # one named in the message that happened to reveal the gap.
                affected = self.market_tickers if sid == self._orderbook_sid else []
                for t in affected:
                    self.books[t].invalidate()
                self._pending_resync.update(affected)
            elif seq is not None:
                tracker.advance(seq)

        if mtype == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            book = self.books.get(ticker)
            if book is None:
                self.counts.unknown_market_messages += 1
                self._log_gap("unknown_market_in_snapshot", str(ticker))
                return
            is_resync = book.has_snapshot
            book.apply_snapshot(msg.get("yes_dollars_fp"), msg.get("no_dollars_fp"))
            self.counts.snapshots += 1
            self._pending_resync.discard(ticker)
            if ticker not in self._first_snapshot_time_by_market:
                self._first_snapshot_time_by_market[ticker] = datetime.now(timezone.utc)
            if is_resync:
                self.counts.resyncs += 1
                self._log(f"resync complete for {ticker} (new segment {book.segment_id})")

        elif mtype == "orderbook_delta":
            ticker = msg.get("market_ticker")
            book = self.books.get(ticker)
            if book is None:
                self.counts.unknown_market_messages += 1
                self._log_gap("unknown_market_in_delta", str(ticker))
                return
            if ticker in self._pending_resync:
                return  # already known-invalid pending a fresh snapshot; don't try to apply on top of it
            try:
                book.apply_delta(msg.get("side"), msg.get("price_dollars"), msg.get("delta_fp"))
                self.counts.deltas += 1
            except OrderBookConsistencyError as e:
                self.counts.gaps += 1
                self._log_gap("consistency_error", str(e), ticker)
                self._pending_resync.add(ticker)

        elif mtype == "trade":
            self.counts.trades += 1

        elif mtype == "subscribed":
            channel, sid = msg.get("channel"), msg.get("sid")
            if channel == "orderbook_delta":
                self._orderbook_sid = sid
            elif channel == "trade":
                self._trade_sid = sid
            self._log(f"subscribed: channel={channel} sid={sid}")

        elif mtype == "error":
            code = msg.get("code")
            self.counts.ws_errors += 1
            self._log_gap("ws_error", f"code={code} msg={msg.get('msg')}")
            if code in TERMINAL_ERROR_CODES:
                raise ConnectionClosed(None, None)

        # Any other/unknown message type is still preserved via _write_raw() before this
        # function is ever called -- nothing is discarded merely because we don't special-case it.

    # ---- connection lifecycle ----

    async def _subscribe(self, ws) -> None:
        for i in range(0, len(self.market_tickers), SUBSCRIBE_CHUNK_SIZE):
            chunk = self.market_tickers[i : i + SUBSCRIBE_CHUNK_SIZE]
            msg = {"id": self._next_cmd_id(), "cmd": "subscribe", "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": chunk}}
            await ws.send(json.dumps(msg))

    async def _request_resyncs(self, ws) -> None:
        if not self._pending_resync or self._orderbook_sid is None:
            return
        tickers = sorted(self._pending_resync)
        msg = {
            "id": self._next_cmd_id(), "cmd": "update_subscription",
            "params": {"sids": [self._orderbook_sid], "market_tickers": tickers, "action": "get_snapshot"},
        }
        await ws.send(json.dumps(msg))
        self._log(f"requested resync (get_snapshot) for {tickers}")

    def _should_stop_connection(self) -> bool:
        close_time = latest_close_time(self.event)
        if close_time is None:
            return False
        return datetime.now(timezone.utc) > close_time + timedelta(seconds=CLOSE_GRACE_SECONDS)

    async def _connect_and_record(self) -> None:
        headers = build_ws_auth_headers(self.key_id, self.private_key)
        async with websockets.connect(self.ws_url, additional_headers=headers, ping_interval=20, ping_timeout=20) as ws:
            self._log(f"connected, subscribing to {len(self.market_tickers)} markets")
            await self._subscribe(ws)
            last_checkpoint = time.monotonic()
            while not self._stop_requested:
                if self._should_stop_connection():
                    self._log("close_time + grace elapsed, ending WS recording for this event")
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if self._pending_resync:
                        await self._request_resyncs(ws)
                    continue
                recv_ts = time.time()
                self._write_raw(raw, recv_ts)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    self.counts.malformed += 1
                    self._log_gap("malformed_message", f"{e}: {raw[:300]!r}")
                    continue
                if data.get("type") == "trade":
                    self._write_trade_copy(raw, recv_ts)
                try:
                    self._handle_message(data)
                except ConnectionClosed:
                    raise
                except Exception as e:
                    self._log_gap("handler_exception", f"{type(e).__name__}: {e}")
                if self._pending_resync:
                    await self._request_resyncs(ws)
                if time.monotonic() - last_checkpoint > 60:
                    self._write_validation_checkpoint()
                    last_checkpoint = time.monotonic()

    async def run(self) -> None:
        attempt = 0
        while not self._stop_requested:
            try:
                await self._connect_and_record()
                if self._should_stop_connection():
                    break
                attempt = 0
            except (ConnectionClosed, WebSocketException, OSError) as e:
                self.counts.reconnects += 1
                delay = min(RECONNECT_BACKOFF_BASE**attempt, RECONNECT_BACKOFF_MAX)
                self._log_gap("disconnect", f"{type(e).__name__}: {e}; reconnecting in {delay}s")
                attempt += 1
                # Mark every market as needing a fresh snapshot once reconnected -- never
                # pretend continuity across an unobserved disconnect period.
                self._pending_resync |= set(self.market_tickers)
                await asyncio.sleep(delay)
            except Exception as e:
                self.counts.reconnects += 1
                self._log_gap("unexpected_error", f"{type(e).__name__}: {e}")
                await asyncio.sleep(RECONNECT_BACKOFF_MAX)

        await self._wait_for_settlement_and_finalize()

    async def _wait_for_settlement_and_finalize(self) -> None:
        self.end_time = datetime.now(timezone.utc)
        self._log("WS recording ended for this event; waiting for settlement before finalizing manifest")
        deadline = self.end_time + timedelta(seconds=MAX_SETTLEMENT_WAIT_SECONDS)
        settled = False
        while datetime.now(timezone.utc) < deadline:
            try:
                fresh = refresh_event_markets(self.event_ticker)
                self.event = fresh
                if all_markets_settled(fresh):
                    settled = True
                    break
            except Exception as e:
                self._log(f"settlement poll failed (will retry): {e}")
            await asyncio.sleep(SETTLEMENT_POLL_SECONDS)
        self._finalize(settled)

    def _sha256_of(self, path: Path) -> str | None:
        if not path.exists():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _opening_capture_assessment(self) -> dict:
        """Classifies how close this recording started to the event's own
        market open_time, based only on objectively recorded timestamps --
        never inferred from wall-clock proximity to a scheduled time alone.
        Separate from capture_completeness (which covers the whole day)."""
        open_time = earliest_open_time(self.event)
        if open_time is None:
            return {"opening_capture_classification": "INVALID", "opening_capture_reason": "no open_time available on this event's markets"}
        if len(self._first_snapshot_time_by_market) < len(self.market_tickers):
            return {
                "opening_capture_classification": "INVALID" if not self._first_snapshot_time_by_market else "PARTIAL",
                "opening_capture_reason": "not every market received an initial snapshot",
                "kalshi_reported_open_time_utc": open_time.isoformat(),
                "first_snapshot_time_by_market": {k: v.isoformat() for k, v in self._first_snapshot_time_by_market.items()},
            }
        last_first_snapshot = max(self._first_snapshot_time_by_market.values())
        gap_seconds = (last_first_snapshot - open_time).total_seconds()
        if self.started_mid_event:
            classification = "STARTED_AFTER_OPEN"
        elif gap_seconds <= 60:
            classification = "FULL_OPEN_CAPTURE"
        elif gap_seconds <= 600:
            classification = "NEAR_OPEN_CAPTURE"
        else:
            classification = "STARTED_AFTER_OPEN"
        return {
            "opening_capture_classification": classification,
            "kalshi_reported_open_time_utc": open_time.isoformat(),
            "recorder_start_utc": self.start_time.isoformat(),
            "first_snapshot_time_by_market": {k: v.isoformat() for k, v in self._first_snapshot_time_by_market.items()},
            "last_first_snapshot_time_utc": last_first_snapshot.isoformat(),
            "gap_seconds_open_to_full_snapshot_coverage": gap_seconds,
        }

    def _finalize(self, settled: bool) -> None:
        missing_snapshot = [t for t, b in self.books.items() if not b.ever_had_snapshot]
        has_gaps = self.counts.gaps > 0
        has_malformed = self.counts.malformed > 0

        if self.started_mid_event:
            completeness = "STARTED_MID_EVENT"
        elif missing_snapshot or self.counts.raw_messages == 0:
            completeness = "INVALID"
        elif has_gaps or not settled:
            completeness = "PARTIAL"
        else:
            completeness = "COMPLETE"

        manifest = {
            "target_date": self.target_date.isoformat(),
            "event_ticker": self.event_ticker,
            "market_tickers": self.market_tickers,
            "recorder_start_utc": self.start_time.isoformat(),
            "recorder_end_utc": self.end_time.isoformat() if self.end_time else None,
            "message_count": self.counts.raw_messages,
            "snapshot_count": self.counts.snapshots,
            "delta_count": self.counts.deltas,
            "trade_count": self.counts.trades,
            "reconnect_count": self.counts.reconnects,
            "detected_gap_count": self.counts.gaps,
            "malformed_message_count": self.counts.malformed,
            "duplicate_count": self.counts.duplicates,
            "resync_count": self.counts.resyncs,
            "ws_error_count": self.counts.ws_errors,
            "unknown_market_message_count": self.counts.unknown_market_messages,
            "markets_missing_any_snapshot": missing_snapshot,
            "settled_at_finalize_time": settled,
            "raw_file_size_bytes": self.raw_path.stat().st_size if self.raw_path.exists() else 0,
            "raw_file_sha256": self._sha256_of(self.raw_path),
            "trades_file_size_bytes": self.trades_path.stat().st_size if self.trades_path.exists() else 0,
            "trades_file_sha256": self._sha256_of(self.trades_path),
            "started_mid_event": self.started_mid_event,
            "capture_completeness": completeness,
            **self._opening_capture_assessment(),
        }
        with open(self.out_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        self._write_metadata_final_event_snapshot()
        self._finalized = True
        self._log(f"FINALIZED day {self.target_date} as {completeness} (settled={settled})")

    def _write_metadata_final_event_snapshot(self) -> None:
        with open(self.out_dir / "metadata" / "event_final.json", "w", encoding="utf-8") as f:
            json.dump(self.event, f, indent=2, default=str)

    def request_stop(self) -> None:
        self._stop_requested = True


def check_disk_safety() -> bool:
    usage = shutil.disk_usage(OUT_ROOT if OUT_ROOT.exists() else OUT_ROOT.parent)
    free_gb = usage.free / (1 << 30)
    if free_gb < MIN_FREE_DISK_GB:
        logging.error(f"DISK SAFETY: only {free_gb:.2f} GB free (< {MIN_FREE_DISK_GB} GB threshold)")
        return False
    return True


class RecorderDaemon:
    def __init__(self):
        self.key_id, self.private_key, self.env = load_credentials_from_env()
        self.ws_url = WS_BASE_URLS[self.env]
        self.active: dict[str, EventRecorder] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._stop = False

    async def _discovery_tick(self) -> None:
        try:
            events = discover_active_events()
        except DiscoveryAmbiguousError as e:
            logging.error(f"discovery ambiguous this cycle, not subscribing to anything new: {e}")
            return
        except Exception as e:
            logging.error(f"discovery failed this cycle (will retry next poll): {type(e).__name__}: {e}")
            return

        for event in events:
            ticker = event["event_ticker"]
            if ticker in self.active:
                continue
            open_time = earliest_open_time(event)
            started_mid_event = open_time is not None and (datetime.now(timezone.utc) - open_time) > timedelta(minutes=10)
            rec = EventRecorder(event, self.key_id, self.private_key, self.ws_url, started_mid_event)
            self.active[ticker] = rec
            self.tasks[ticker] = asyncio.create_task(self._run_and_cleanup(rec))
            logging.info(f"started recording {ticker} (target_date={rec.target_date}, started_mid_event={started_mid_event}, markets={len(rec.market_tickers)})")

    async def _run_and_cleanup(self, rec: EventRecorder) -> None:
        try:
            await rec.run()
        finally:
            self.active.pop(rec.event_ticker, None)
            self.tasks.pop(rec.event_ticker, None)

    async def run(self) -> None:
        logging.info(f"KXHIGHNY recorder daemon starting (env={self.env}, ws_url={self.ws_url})")
        while not self._stop:
            if not check_disk_safety():
                logging.error("stopping all recording due to disk safety threshold -- data integrity preserved, no silent drop")
                for rec in self.active.values():
                    rec.request_stop()
                await asyncio.sleep(60)
                continue
            await self._discovery_tick()
            await asyncio.sleep(DISCOVERY_POLL_SECONDS)


async def main() -> None:
    daemon = RecorderDaemon()
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
