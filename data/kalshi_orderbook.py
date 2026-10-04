"""Deterministic L2 order-book reconstruction + sequence-gap detection for
Kalshi's orderbook_delta channel.

book(t) = snapshot (at t0) + ordered deltas (t0..t)

This module holds ONLY the current reconstructed book plus small
bookkeeping -- it never retains message history in memory. The raw message
stream is the source of truth and lives on disk (see
scripts/kxhighny_recorder.py); this module exists so that stream can be
validated for internal consistency as it arrives, and so book(t) can later
be replayed deterministically from the same raw stream.

IMPORTANT, confirmed empirically (2026-10-04) against the live production
WebSocket, not assumed from the spec text alone: the `seq` field is scoped
to the SUBSCRIPTION (`sid`), not to an individual market. One `subscribe`
command covering N market_tickers on the orderbook_delta channel gets back
ONE `sid`, and every orderbook_snapshot/orderbook_delta message for ANY of
those N markets shares that single, strictly-incrementing sequence counter
-- a first live test that tracked `seq` per-market produced a continuous
false-positive gap/resync storm, because e.g. market A's 2nd message might
legitimately be the subscription's 9th message overall (markets B..F's
messages interleaved in between). SequenceTracker below tracks one counter
per sid; OrderBook itself no longer touches `seq` at all. A genuine gap at
the sid level means ANY of the markets multiplexed under that sid may have
missed a delta, so a sid-level gap must invalidate (require a fresh
snapshot for) every market subscribed under it, not just the one named in
the message that happened to reveal the gap.

Schema reference (Kalshi AsyncAPI spec, fetched 2026-10-04):
  orderbook_snapshot.msg: {market_ticker, market_id, yes_dollars_fp?,
    no_dollars_fp?} where yes/no_dollars_fp is [[price_dollars, size_fp], ...]
  orderbook_delta.msg: {market_ticker, market_id, price_dollars, delta_fp,
    side ("yes"|"no"), ts_ms?, client_order_id?}
Both carry a top-level `seq` (sequenceNumber) and `sid` (subscriptionId),
shared across every market multiplexed under that one subscription.
"""
from dataclasses import dataclass, field
from decimal import Decimal


class OrderBookConsistencyError(ValueError):
    """Raised when a delta cannot be applied without violating a basic
    invariant (e.g. driving a price level's size negative), or when it
    arrives before this market has ever received a snapshot."""


@dataclass
class OrderBook:
    market_ticker: str
    yes_levels: dict[Decimal, Decimal] = field(default_factory=dict)
    no_levels: dict[Decimal, Decimal] = field(default_factory=dict)
    has_snapshot: bool = False
    ever_had_snapshot: bool = False  # sticky -- never cleared by invalidate(); distinguishes "never initialized" from "temporarily invalidated"
    segment_id: int = 0  # incremented every time a fresh snapshot starts a new reconstruction segment
    n_deltas_applied: int = 0

    def apply_snapshot(self, yes_dollars_fp: list | None, no_dollars_fp: list | None) -> None:
        self.yes_levels = {Decimal(p): Decimal(s) for p, s in (yes_dollars_fp or [])}
        self.no_levels = {Decimal(p): Decimal(s) for p, s in (no_dollars_fp or [])}
        self.has_snapshot = True
        self.ever_had_snapshot = True
        self.segment_id += 1
        self.n_deltas_applied = 0

    def apply_delta(self, side: str, price_dollars: str, delta_fp: str) -> None:
        if not self.has_snapshot:
            raise OrderBookConsistencyError(f"{self.market_ticker}: delta arrived before any snapshot")

        levels = self.yes_levels if side == "yes" else self.no_levels
        price = Decimal(price_dollars)
        new_size = levels.get(price, Decimal(0)) + Decimal(delta_fp)
        if new_size < 0:
            raise OrderBookConsistencyError(
                f"{self.market_ticker}: delta would make {side} level {price} negative "
                f"({levels.get(price, Decimal(0))} + {delta_fp} = {new_size})"
            )
        if new_size == 0:
            levels.pop(price, None)
        else:
            levels[price] = new_size
        self.n_deltas_applied += 1

    def invalidate(self) -> None:
        """Marks this book as no longer trustworthy (e.g. a sid-level
        sequence gap occurred) without discarding its last-known levels --
        callers should still request a fresh snapshot before trusting it
        again. has_snapshot=False forces any stray delta to raise rather
        than silently applying on top of a now-uncertain state."""
        self.has_snapshot = False

    def best_bid_ask(self) -> dict:
        """Audit/sanity helper only -- never used as the primary record."""
        return {
            "best_yes_bid": max(self.yes_levels) if self.yes_levels else None,
            "best_no_bid": max(self.no_levels) if self.no_levels else None,
            "n_yes_levels": len(self.yes_levels),
            "n_no_levels": len(self.no_levels),
        }


@dataclass
class SequenceTracker:
    """Tracks the expected next sequence number for ONE subscription stream
    (sid), shared across every market multiplexed under that sid. See the
    module docstring for why this must NOT be tracked per-market."""
    sid_label: str
    last_seq: int | None = None

    def check(self, seq: int) -> str | None:
        """Returns None if `seq` is the expected next number, "FIRST" if
        this is the very first message ever seen on this sid (not an
        anomaly), or an anomaly code: "DUPLICATE_OR_OLD" or "GAP"."""
        if self.last_seq is None:
            return "FIRST"
        if seq <= self.last_seq:
            return "DUPLICATE_OR_OLD"
        if seq > self.last_seq + 1:
            return "GAP"
        return None

    def advance(self, seq: int) -> None:
        self.last_seq = seq

    def reset(self) -> None:
        self.last_seq = None
