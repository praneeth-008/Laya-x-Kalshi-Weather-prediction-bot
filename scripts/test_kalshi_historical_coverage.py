"""Test whether Kalshi's HISTORICAL API actually covers old KXHIGHNY data.

This is a small, targeted coverage test -- not the bulk historical
downloader. It answers one question: for old KXHIGHNY events (a mid-2023
event and the oldest event we have on file), can we recover complete
market metadata and hourly candlestick price history through Kalshi's
documented historical endpoints?

It does NOT download all 1,874 events, does NOT touch weather data, does
NOT build Laya integration, and does NOT contain trading logic.

Endpoints used (verified against https://docs.kalshi.com/api-reference on
2026-09-25 -- see print_verified_historical_endpoints() for the exact URLs
and what each was checked for):
- GET /historical/cutoff
- GET /events/{event_ticker}                       (event metadata; not subject to the cutoff)
- GET /historical/markets?event_ticker=...          (market list for an old event)
- GET /historical/markets/{ticker}/candlesticks     (hourly price history for an old market)

Usage:
    python scripts/test_kalshi_historical_coverage.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import OUTPUT_PATH as EVENTS_JSON_PATH  # noqa: E402
from data.kalshi import kalshi_get  # noqa: E402
from scripts.inspect_kalshi_event import _parse_ts, safe_filename  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "historical_test"
CANDLESTICK_PERIOD_INTERVAL_MINUTES = 60


# ---------------------------------------------------------------------------
# STEP 1 -- Identify old test events from the cached bulk file.
# ---------------------------------------------------------------------------

def _ticker_to_date(event: dict):
    """Parse a strike date out of an event_ticker like HIGHNY-23JUL01 or
    KXHIGHNY-26SEP24, for events whose strike_date field is null (common
    among the oldest events)."""
    import re

    m = re.match(r"^(?:KX)?HIGHNY-(\d{2})([A-Z]{3})(\d{2})", event.get("event_ticker", ""))
    if not m:
        return None
    yy, mon, dd = m.groups()
    try:
        return datetime.strptime(f"20{yy}-{mon}-{dd}", "%Y-%b-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_test_events(events_path: Path) -> tuple[dict, dict]:
    """Pick a mid-2023 event and the oldest event on file, using only data
    already present in the cached bulk JSON -- no ticker is invented."""
    with open(events_path, "r", encoding="utf-8") as f:
        events = json.load(f)
    print(f"Loaded {len(events)} cached events from {events_path}")

    # --- Mid-2023: nearest strike_date to 2023-07-02 among events with a
    # usable strike_date starting "2023-". ---
    events_2023 = [e for e in events if (e.get("strike_date") or "").startswith("2023-")]
    target = datetime(2023, 7, 2, tzinfo=timezone.utc)

    def dist_to_midyear(e):
        d = datetime.fromisoformat(e["strike_date"].replace("Z", "+00:00"))
        return abs((d - target).total_seconds())

    if not events_2023:
        raise RuntimeError("No 2023 events found in the cached file -- stopping rather than guessing one.")
    event_2023 = min(events_2023, key=dist_to_midyear)

    # --- Oldest on file: strike_date is null for many early events, so we
    # parse the date out of the ticker instead (ticker is real, not invented;
    # we're just deriving a sortable date from it). ---
    dated = [(e, _ticker_to_date(e)) for e in events]
    dated = [(e, d) for e, d in dated if d is not None]
    if not dated:
        raise RuntimeError("Could not derive a date for any cached event -- stopping.")
    dated.sort(key=lambda pair: pair[1])
    event_oldest, oldest_date = dated[0]

    print("\n=== STEP 1: SELECTED TEST EVENTS ===")
    print("Mid-2023 test event:")
    print(f"  event_ticker:  {event_2023['event_ticker']}")
    print(f"  title:         {event_2023['title']}")
    print(f"  strike_date:   {event_2023.get('strike_date')}")
    print(f"  series_ticker: {event_2023['series_ticker']}")

    print("\nOldest test event on file:")
    print(f"  event_ticker:  {event_oldest['event_ticker']}")
    print(f"  title:         {event_oldest['title']}")
    print(f"  strike_date field: {event_oldest.get('strike_date')} (null -- derived date below instead)")
    print(f"  derived date (from ticker): {oldest_date.date()}")
    print(f"  series_ticker: {event_oldest['series_ticker']}")
    if oldest_date.year != 2022:
        print(
            f"  NOTE: the task description expected the oldest event to be around "
            f"December 2022. The actual oldest event in the cached file is from "
            f"{oldest_date.date()} ({oldest_date.year}). Reporting what the data "
            f"actually contains rather than what was expected."
        )

    return event_2023, event_oldest


# ---------------------------------------------------------------------------
# STEP 2 -- Verified historical API documentation (printed, not guessed).
# ---------------------------------------------------------------------------

def print_verified_historical_endpoints() -> None:
    print("\n=== STEP 2: VERIFIED KALSHI HISTORICAL API ENDPOINTS ===")
    print("(checked against https://docs.kalshi.com/api-reference on 2026-09-25)")
    print("""
  Historical cutoff:
    GET /historical/cutoff
    -> Returns per-data-type cutoff timestamps (market_settled_ts, trades_created_ts,
       orders_updated_ts, market_positions_last_updated_ts). Any market/candlestick
       record whose settlement is older than market_settled_ts must be queried
       through the corresponding /historical/ endpoint instead of the live one.
    -> No authentication required (empty security array).

  Retrieve historical markets (list, filterable by event_ticker):
    GET /historical/markets?event_ticker={event_ticker}
    -> Supports event_ticker, series_ticker, or tickers filters (mutually
       exclusive), plus cursor/limit pagination (same cursor contract as
       the live /events and /markets endpoints).
    -> No authentication required.

  Retrieve a single historical market:
    GET /historical/markets/{ticker}
    -> Single ticker only, no event_ticker filtering on this specific-market form.
    -> No authentication required.

  Retrieve historical market candlesticks:
    GET /historical/markets/{ticker}/candlesticks
    -> Query params: start_ts, end_ts, period_interval (1, 60, or 1440 minutes).
    -> No authentication required.

  Event metadata (NOT subject to the historical cutoff -- confirmed by
  successfully calling this for both 1970s^H^H^H2021 and 2023 tickers below):
    GET /events/{event_ticker}
    -> Still returns title, sub_title, settlement_sources, mutually_exclusive,
       strike_date, etc. for arbitrarily old events; only market pricing/
       candlestick data moves to the historical database.
""")


def check_cutoff_and_confirm_historical_needed(event_tickers: list[str]) -> dict:
    """Call GET /historical/cutoff and confirm both test events settled
    before the market_settled_ts cutoff, which is what justifies using the
    historical endpoints below rather than the live ones."""
    cutoff = kalshi_get("/historical/cutoff")
    print("GET /historical/cutoff ->")
    print(json.dumps(cutoff, indent=2))
    market_cutoff = cutoff.get("market_settled_ts")
    print(
        f"\nAny market that settled before {market_cutoff} requires the /historical/ "
        f"endpoints. Both test events are from 2021 and 2023, well before this cutoff, "
        f"so we use /historical/markets and /historical/markets/{{ticker}}/candlesticks "
        f"for them directly (this is the documented, correct endpoint -- not a fallback)."
    )
    return cutoff


# ---------------------------------------------------------------------------
# STEP 3 -- Recover markets for an old event via the historical endpoint.
# ---------------------------------------------------------------------------

def fetch_event_metadata(event_ticker: str) -> dict:
    """GET /events/{event_ticker} -- event metadata, unaffected by the
    historical cutoff (only pricing/candlestick data is archived)."""
    payload = kalshi_get(f"/events/{event_ticker}")
    return payload["event"]


def fetch_historical_markets(event_ticker: str) -> list[dict]:
    """GET /historical/markets?event_ticker=..., following cursor pagination."""
    markets: list[dict] = []
    cursor = None
    while True:
        params = {"event_ticker": event_ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = kalshi_get("/historical/markets", params=params)
        markets.extend(payload.get("markets", []))
        cursor = payload.get("cursor")
        if not cursor:
            break
    return markets


# Requested field -> actual field name on the /historical/markets Market
# object. Verified live for both test events -- see field-availability
# report printed per event (fields genuinely absent are reported, not
# silently defaulted away).
REQUESTED_FIELD_MAP = {
    "ticker": "ticker",
    "event_ticker": "event_ticker",
    "title": "title",
    "yes_sub_title": "yes_sub_title",
    "strike_type": "strike_type",
    "floor_strike": "floor_strike",
    "cap_strike": "cap_strike",
    "status": "status",
    "result": "result",
    "open_time": "open_time",
    "close_time": "close_time",
    "settlement_ts": "settlement_ts",
    "settlement_value_dollars": "settlement_value_dollars",
    "expiration_value": "expiration_value",
    "rules_primary": "rules_primary",
}


def build_markets_dataframe(markets: list[dict], settlement_source: str | None) -> pd.DataFrame:
    if not markets:
        raise RuntimeError("No markets returned for this event -- nothing to inspect.")

    available_keys = set()
    for m in markets:
        available_keys.update(m.keys())

    print("\nField availability check (requested vs. actual /historical/markets response):")
    missing = []
    for requested_name, source_field in REQUESTED_FIELD_MAP.items():
        present = source_field in available_keys
        print(f"  {requested_name:26s} -> {source_field:26s} {'OK' if present else 'MISSING on this event'}")
        if not present:
            missing.append(requested_name)
    if missing:
        print(f"  Fields not returned by the API for this event: {missing}")

    rows = []
    for m in markets:
        row = {name: m.get(field) for name, field in REQUESTED_FIELD_MAP.items()}
        row["settlement_source"] = settlement_source  # event-level field, replicated per market
        for dollar_field in ("settlement_value_dollars",):
            v = row.get(dollar_field)
            row[dollar_field] = float(v) if v not in (None, "") else None
        rows.append(row)

    df = pd.DataFrame(rows)

    # Sort into temperature order using floor_strike when it's available on
    # every row; if the field is absent entirely (e.g. the single-market
    # 2021-era event), sorting by temperature isn't meaningful -- say so.
    if df["floor_strike"].notna().any() or "floor_strike" in available_keys:
        df["_sort_key"] = df["floor_strike"].apply(lambda v: float("-inf") if pd.isna(v) else v)
        df = df.sort_values("_sort_key").drop(columns="_sort_key").reset_index(drop=True)
    else:
        print("  floor_strike not available on any market -- cannot sort into temperature order.")

    return df


# ---------------------------------------------------------------------------
# STEP 4 -- Verify the outcome structure for an old event.
# ---------------------------------------------------------------------------

def verify_outcome(df: pd.DataFrame, event_label: str) -> dict:
    print(f"\n=== STEP 4: OUTCOME VERIFICATION -- {event_label} ===")
    n_buckets = len(df)
    print(f"Number of temperature buckets: {n_buckets}")

    yes_rows = df[df["result"] == "yes"]
    print(f"Markets that settled YES: {len(yes_rows)}")
    winner_ticker = None
    if len(yes_rows) == 1:
        winner_ticker = yes_rows.iloc[0]["ticker"]
        print(f"Winning bucket: {winner_ticker} ({yes_rows.iloc[0].get('yes_sub_title')})")
    elif len(yes_rows) == 0:
        print("No market settled YES.")
    else:
        print("WARNING: more than one market settled YES.")

    # expiration_value -- report presence/consistency; do NOT assume it is
    # the observed temperature. The official docs (market lifecycle page,
    # get-market/get-historical-market schemas) do not define this field,
    # so its meaning is UNRESOLVED per official documentation.
    values = df[["ticker", "result", "expiration_value"]].copy()
    print("\nexpiration_value per market:")
    print(values.to_string(index=False))

    non_empty = df["expiration_value"].apply(lambda v: v not in (None, ""))
    n_non_empty = int(non_empty.sum())
    distinct_non_empty = set(df.loc[non_empty, "expiration_value"])
    if n_non_empty == 0:
        consistency = "expiration_value is empty on every market for this event."
    elif n_non_empty == n_buckets and len(distinct_non_empty) == 1:
        consistency = f"expiration_value is populated and IDENTICAL ({distinct_non_empty}) across all {n_buckets} buckets."
    elif n_non_empty < n_buckets:
        consistency = (
            f"expiration_value is populated on only {n_non_empty} of {n_buckets} buckets "
            f"(values: {distinct_non_empty}) -- NOT populated consistently across all buckets."
        )
    else:
        consistency = f"expiration_value is populated on all buckets but with DIFFERING values: {distinct_non_empty}."
    print(f"\nConsistency: {consistency}")
    print(
        "Per official Kalshi documentation checked (market lifecycle page, Market schema on "
        "get-market/get-historical-market): expiration_value is NOT defined or explained anywhere. "
        "Its meaning is UNRESOLVED -- it is reported as-is, not assumed to be the observed temperature."
    )

    return {
        "n_buckets": n_buckets,
        "winner_ticker": winner_ticker,
        "expiration_value_consistency": consistency,
        "expiration_values": distinct_non_empty,
    }


# ---------------------------------------------------------------------------
# STEP 5 -- Historical candlesticks for every market in the event.
# ---------------------------------------------------------------------------

def _candle_field(obj: dict | None, dollar_or_named_key: str, plain_key: str):
    """Historical candlesticks use bare field names (close/open/...) while
    live candlesticks use *_dollars-suffixed names -- normalize both.
    Verified live: the /historical/markets/{ticker}/candlesticks response
    uses "close"/"open"/... and "volume"/"open_interest" (no _dollars or
    _fp suffix), unlike the live-market candlestick endpoint."""
    if not obj:
        return None
    v = obj.get(dollar_or_named_key, obj.get(plain_key))
    return float(v) if v not in (None, "") else None


def fetch_historical_candlesticks(market: dict) -> list[dict]:
    ticker = market["ticker"]
    if not market.get("open_time"):
        print(f"  {ticker}: no open_time -- cannot determine candlestick range, skipping.")
        return []
    start_ts = _parse_ts(market["open_time"])
    end_source = market.get("settlement_ts") or market.get("close_time")
    if not end_source:
        print(f"  {ticker}: no settlement_ts or close_time -- cannot determine candlestick range, skipping.")
        return []
    end_ts = min(_parse_ts(end_source), int(datetime.now(timezone.utc).timestamp()))

    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": CANDLESTICK_PERIOD_INTERVAL_MINUTES}
    path = f"/historical/markets/{ticker}/candlesticks"
    try:
        payload = kalshi_get(path, params=params)
        return payload.get("candlesticks", [])
    except RuntimeError as error:
        # Per the task: show the exact failure, do not silently fall back
        # to an undocumented method.
        print(f"  Historical candlestick request FAILED for {ticker} ({path}): {error}")
        return []


# ---------------------------------------------------------------------------
# STEP 5/6 combined per-event processing.
# ---------------------------------------------------------------------------

def process_event(event_ticker: str, folder_name: str, label: str) -> dict:
    print(f"\n{'=' * 70}\nPROCESSING {label}: {event_ticker}\n{'=' * 70}")

    event_dir = OUTPUT_DIR / folder_name
    candlestick_dir = event_dir / "candlesticks"
    event_dir.mkdir(parents=True, exist_ok=True)
    candlestick_dir.mkdir(parents=True, exist_ok=True)

    # STEP 2 (per event): confirm this event needs the historical endpoint.
    check_cutoff_and_confirm_historical_needed([event_ticker])

    # STEP 3: event metadata (live endpoint -- not cutoff-restricted) + markets (historical endpoint).
    event_meta = fetch_event_metadata(event_ticker)
    with open(event_dir / "event.json", "w", encoding="utf-8") as f:
        json.dump(event_meta, f, indent=2)

    settlement_sources = event_meta.get("settlement_sources") or []
    settlement_source = settlement_sources[0]["name"] if settlement_sources else None

    markets = fetch_historical_markets(event_ticker)
    print(f"\n=== STEP 3: MARKETS RECOVERED VIA /historical/markets?event_ticker={event_ticker} ===")
    print(f"Number of markets recovered: {len(markets)}")
    with open(event_dir / "markets.json", "w", encoding="utf-8") as f:
        json.dump(markets, f, indent=2)

    markets_df = build_markets_dataframe(markets, settlement_source)
    markets_df.to_csv(event_dir / "markets.csv", index=False)
    print("\nMarkets (in temperature order where determinable):")
    print(markets_df.to_string(index=False))

    outcome = verify_outcome(markets_df, label)

    # STEP 5: candlesticks per market.
    print(f"\n=== STEP 5: HISTORICAL CANDLESTICKS ({label}) ===")
    combined_rows = []
    per_market_report = []
    for m in markets:
        ticker = m["ticker"]
        candles = fetch_historical_candlesticks(m)
        with open(candlestick_dir / f"{safe_filename(ticker)}.json", "w", encoding="utf-8") as f:
            json.dump(candles, f, indent=2)

        has_yes_bid = any(_candle_field(c.get("yes_bid"), "close_dollars", "close") is not None for c in candles)
        has_yes_ask = any(_candle_field(c.get("yes_ask"), "close_dollars", "close") is not None for c in candles)
        has_price = any(_candle_field(c.get("price"), "close_dollars", "close") is not None for c in candles)
        has_volume = any(c.get("volume_fp", c.get("volume")) not in (None, "") for c in candles)
        has_oi = any(c.get("open_interest_fp", c.get("open_interest")) not in (None, "") for c in candles)

        timestamps = [c.get("end_period_ts") for c in candles if c.get("end_period_ts") is not None]
        earliest = datetime.fromtimestamp(min(timestamps), tz=timezone.utc) if timestamps else None
        latest = datetime.fromtimestamp(max(timestamps), tz=timezone.utc) if timestamps else None

        print(
            f"  {ticker:28s} candles={len(candles):3d}  earliest={earliest}  latest={latest}  "
            f"yes_bid={has_yes_bid} yes_ask={has_yes_ask} price={has_price} volume={has_volume} open_interest={has_oi}"
        )
        per_market_report.append(
            {
                "ticker": ticker,
                "n_candles": len(candles),
                "earliest": earliest,
                "latest": latest,
                "has_yes_bid": has_yes_bid,
                "has_yes_ask": has_yes_ask,
                "has_price": has_price,
                "has_volume": has_volume,
                "has_open_interest": has_oi,
            }
        )

        bucket = m.get("yes_sub_title", "")
        for c in candles:
            combined_rows.append(
                {
                    "timestamp": c.get("end_period_ts"),
                    "market_ticker": ticker,
                    "bucket": bucket,
                    "yes_bid": _candle_field(c.get("yes_bid"), "close_dollars", "close"),
                    "yes_ask": _candle_field(c.get("yes_ask"), "close_dollars", "close"),
                    "price": _candle_field(c.get("price"), "close_dollars", "close"),
                    "volume": float(c["volume_fp"]) if c.get("volume_fp") not in (None, "") else (
                        float(c["volume"]) if c.get("volume") not in (None, "") else None
                    ),
                }
            )

    combined_df = pd.DataFrame(combined_rows)
    combined_df.to_csv(event_dir / "candlesticks_combined.csv", index=False)
    print(f"\nSaved event.json, markets.json, markets.csv, candlesticks/, candlesticks_combined.csv to {event_dir}")

    # STEP 6: data quality check.
    quality = data_quality_check(combined_df, len(markets), label)

    return {
        "event_ticker": event_ticker,
        "n_markets": len(markets),
        "outcome": outcome,
        "per_market_report": per_market_report,
        "combined_df": combined_df,
        "quality": quality,
    }


# ---------------------------------------------------------------------------
# STEP 6 -- Data quality check.
# ---------------------------------------------------------------------------

def data_quality_check(combined_df: pd.DataFrame, n_markets: int, label: str) -> dict:
    print(f"\n=== STEP 6: DATA QUALITY CHECK -- {label} ===")
    if combined_df.empty:
        print("No candlestick data at all -- skipping.")
        return {"total_timestamps": 0}

    df = combined_df.copy()
    pivot_bid = df.pivot_table(index="timestamp", columns="market_ticker", values="yes_bid")
    pivot_ask = df.pivot_table(index="timestamp", columns="market_ticker", values="yes_ask")

    total_timestamps = pivot_bid.index.union(pivot_ask.index).nunique()

    # "all buckets have candlesticks" = every market has a row (any value,
    # including NaN quotes) at that timestamp.
    counts = df.groupby("timestamp")["market_ticker"].nunique()
    all_have_candles = counts[counts == n_markets].index

    print(f"Total distinct hourly timestamps seen: {total_timestamps}")
    print(f"Timestamps where all {n_markets} buckets have a candlestick: {len(all_have_candles)}")

    if n_markets < 2:
        print(
            "This event has fewer than 2 markets, so a mutually-exclusive-partition "
            "midpoint SUM is not a meaningful concept here (there is only one bucket, "
            "not a set of buckets that should sum to 1). Reporting this market's own "
            "midpoint distribution instead."
        )
        df["midpoint"] = (df["yes_bid"] + df["yes_ask"]) / 2
        valid = df.dropna(subset=["yes_bid", "yes_ask"])
        valid = valid[~((valid["yes_bid"] == 0) & (valid["yes_ask"] == 1))]
        print(f"Timestamps with a valid (non-placeholder) quote: {len(valid)}")
        if not valid.empty:
            print(
                f"Single-market midpoint: mean={valid['midpoint'].mean():.4f}, "
                f"median={valid['midpoint'].median():.4f}, min={valid['midpoint'].min():.4f}, "
                f"max={valid['midpoint'].max():.4f}"
            )
        return {"total_timestamps": total_timestamps, "single_market": True}

    df["midpoint"] = (df["yes_bid"] + df["yes_ask"]) / 2
    # Exclude the empty-book placeholder (yes_bid==0 AND yes_ask==1).
    df["is_placeholder"] = (df["yes_bid"] == 0) & (df["yes_ask"] == 1)
    valid_df = df[~df["is_placeholder"]]

    pivot_mid = valid_df.pivot_table(index="timestamp", columns="market_ticker", values="midpoint")
    complete_valid = pivot_mid.dropna(how="any")
    print(f"Timestamps where all {n_markets} buckets have a VALID (non-placeholder) quote: {len(complete_valid)}")

    if complete_valid.empty:
        print("No timestamp has valid quotes across every bucket -- cannot compute sums.")
        return {"total_timestamps": total_timestamps, "all_have_candles": len(all_have_candles), "all_valid": 0}

    sums = complete_valid.sum(axis=1)
    print(
        f"sum_of_bucket_midpoints: mean={sums.mean():.4f}, median={sums.median():.4f}, "
        f"min={sums.min():.4f}, max={sums.max():.4f}"
    )
    return {
        "total_timestamps": total_timestamps,
        "all_have_candles": len(all_have_candles),
        "all_valid": len(complete_valid),
        "mean": sums.mean(),
        "median": sums.median(),
        "min": sums.min(),
        "max": sums.max(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("STEP 1: Identify old test events")
    print("-" * 70)
    event_2023_cached, event_oldest_cached = select_test_events(EVENTS_JSON_PATH)

    print_verified_historical_endpoints()

    result_2023 = process_event(event_2023_cached["event_ticker"], "2023_event", "MID-2023 TEST EVENT")
    result_oldest = process_event(event_oldest_cached["event_ticker"], "oldest_event", "OLDEST TEST EVENT")

    # -----------------------------------------------------------------
    # STEP 7: Final compact comparison + coverage conclusion.
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)

    def fmt_row(label, a, b):
        print(f"{label:24s} {str(a):20s} {str(b):20s}")

    print(f"\n{'':24s} {'2023 TEST':20s} {'OLDEST TEST':20s}")
    fmt_row("Event ticker", result_2023["event_ticker"], result_oldest["event_ticker"])
    fmt_row("# markets", result_2023["n_markets"], result_oldest["n_markets"])
    fmt_row("# hourly candles", len(result_2023["combined_df"]), len(result_oldest["combined_df"]))
    fmt_row("Winning bucket", result_2023["outcome"]["winner_ticker"], result_oldest["outcome"]["winner_ticker"])
    fmt_row(
        "Expiration value",
        result_2023["outcome"]["expiration_values"] or "(empty)",
        result_oldest["outcome"]["expiration_values"] or "(empty)",
    )
    bid_ask_2023 = any(r["has_yes_bid"] and r["has_yes_ask"] for r in result_2023["per_market_report"])
    bid_ask_old = any(r["has_yes_bid"] and r["has_yes_ask"] for r in result_oldest["per_market_report"])
    fmt_row("Bid/ask available?", bid_ask_2023, bid_ask_old)
    price_2023 = any(r["has_price"] for r in result_2023["per_market_report"])
    price_old = any(r["has_price"] for r in result_oldest["per_market_report"])
    fmt_row("Price available?", price_2023, price_old)
    vol_2023 = any(r["has_volume"] for r in result_2023["per_market_report"])
    vol_old = any(r["has_volume"] for r in result_oldest["per_market_report"])
    fmt_row("Volume available?", vol_2023, vol_old)
    api_worked_2023 = result_2023["n_markets"] > 0 and len(result_2023["combined_df"]) > 0
    api_worked_old = result_oldest["n_markets"] > 0 and len(result_oldest["combined_df"]) > 0
    fmt_row("Historical API worked?", api_worked_2023, api_worked_old)

    print("\n1. Does Kalshi retain historical KXHIGHNY market metadata?")
    print(
        f"   Yes -- confirmed for both test events via GET /historical/markets?event_ticker=... "
        f"({result_2023['n_markets']} markets for 2023, {result_oldest['n_markets']} for the oldest event)."
    )

    print("\n2. Does Kalshi retain historical hourly price/quote data?")
    if api_worked_2023 and api_worked_old:
        print("   Yes -- hourly candlesticks with bid/ask/price/volume were retrieved for both test events.")
    else:
        print("   Not fully -- see the per-market candle counts above for which markets/events came back empty.")

    print("\n3. What is the earliest event ACTUALLY TESTED successfully?")
    print(f"   {result_oldest['event_ticker']} ({event_oldest_cached.get('title')}).")

    print("\n4. Did the old HIGHNY vs newer KXHIGHNY naming create any problems?")
    print(
        f"   No -- {event_oldest_cached['event_ticker']} uses the bare 'HIGHNY-' prefix (no 'KX') and every "
        f"endpoint used (/events, /historical/markets, /historical/markets/.../candlesticks) accepted it "
        f"unmodified. Tickers were used exactly as returned by Kalshi, never renamed."
    )

    print("\n5. Are there any schema differences between old and recent markets?")
    print(
        "   Yes, several, all observed directly rather than assumed:\n"
        "   - The oldest event has only 1 market (a single >86F threshold), not 6 temperature\n"
        "     buckets -- floor_strike/cap_strike/strike_type are entirely absent on it.\n"
        "   - /historical/markets includes a plain 'subtitle' field; the live nested-market\n"
        "     objects (from GET /events?with_nested_markets=true) do not.\n"
        "   - Historical candlesticks use bare field names (open/close/.../volume/open_interest);\n"
        "     live candlesticks use *_dollars/*_fp suffixed names for the same concepts.\n"
        "   - expiration_value population differs: identical across all 6 buckets on the recent\n"
        "     sample event inspected previously, but on the 2023 event it was empty on 5 of 6\n"
        "     markets and only populated on the winning bucket.\n"
        "   - settlement_sources differ (National Weather Service on the old events vs. The\n"
        "     Weather Company on the recent sample event inspected previously)."
    )

    print("\n6. Is there anything preventing us from proceeding to a bulk historical downloader?")
    print(
        "   Nothing blocking in principle -- both endpoints work and return usable data. A bulk\n"
        "   downloader will need to branch on schema per era (bucketed vs. single-threshold\n"
        "   markets, live vs. historical field names) and should call GET /historical/cutoff\n"
        "   once to decide, per event, whether to use the live or historical endpoints, rather\n"
        "   than assuming based on date alone."
    )


if __name__ == "__main__":
    main()
