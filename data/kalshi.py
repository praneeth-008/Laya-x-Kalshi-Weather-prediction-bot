"""Kalshi historical market discovery for the KXHIGHNY series.

This script queries Kalshi's public Predictions API (no authentication
required for this endpoint) to discover every event that has ever existed
in the KXHIGHNY series -- the daily NYC maximum-temperature market.

It only *discovers* events (via GET /events). It does NOT download
candlesticks, trades, or any weather data, and it does NOT contain any
trading logic. Those come in later stages of the pipeline.

Note on "event status": Kalshi's Event object has no native `status`
field -- only the Market objects nested inside an event do (e.g. "open",
"closed", "settled"). Because every market within a single KXHIGHNY event
shares the same strike date and therefore the same lifecycle, we request
markets via `with_nested_markets=true` and derive an event-level status
from its first nested market. This is a derived field, not one Kalshi
returns directly -- it is documented here so it is never mistaken for an
official API field.

API reference used (fetched 2026-09-25):
- https://docs.kalshi.com/api-reference/events/get-events
- https://docs.kalshi.com/getting_started/pagination

Usage:
    python data/kalshi.py
"""

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Kalshi's public Predictions API base URL.
BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# The series we care about: NYC daily maximum temperature markets.
SERIES_TICKER = "KXHIGHNY"

# Kalshi's documented maximum page size for /events.
PAGE_LIMIT = 200

# Where to save the raw, unmodified API responses (this directory is
# gitignored -- see .gitignore -- so these files are never committed).
OUTPUT_PATH = Path(__file__).resolve().parent / "raw" / "kalshi" / "kxhighny_events.json"

# HTTP statuses worth retrying: 429 (rate limited) and the 5xx transient
# server errors. Per Kalshi's official rate-limit docs (checked 2026-09-25,
# https://docs.kalshi.com/getting_started/rate_limits), 429 responses carry
# no Retry-After or X-RateLimit-* headers, so we back off blindly with
# exponential delay + jitter rather than reading a hint that doesn't exist.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# A small fixed pacing delay after every successful request, applied here
# so every script that goes through kalshi_get() is polite to the API by
# default -- most important for the bulk downloader, harmless elsewhere.
REQUEST_PACING_SECONDS = 0.05


def _backoff_delay(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)


def kalshi_get(path: str, params: dict | None = None, max_retries: int = MAX_RETRIES) -> dict:
    """GET a path under Kalshi's public API base URL and return the JSON body.

    Shared low-level request helper -- every script that talks to Kalshi's
    API should call through here so error handling and rate-limit backoff
    stay in one place. Transient failures (429/5xx) are retried with
    exponential backoff + jitter, up to max_retries times. Any other
    non-200 response (e.g. 400/404 -- not something a retry would fix)
    raises immediately with the exact status code and response body,
    rather than silently retrying or working around it.
    """
    url = f"{BASE_URL}{path}"
    last_status, last_text = None, None
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Kalshi API request to {url} failed after {max_retries} retries: {exc}")
            time.sleep(_backoff_delay(attempt))
            continue

        if response.status_code == 200:
            time.sleep(REQUEST_PACING_SECONDS)
            return response.json()

        last_status, last_text = response.status_code, response.text
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            time.sleep(_backoff_delay(attempt))
            continue
        raise RuntimeError(
            f"Kalshi API request to {response.url} failed with status "
            f"{response.status_code}: {response.text}"
        )

    raise RuntimeError(f"Kalshi API request to {url} failed with status {last_status}: {last_text}")


def fetch_all_events(series_ticker: str) -> list[dict]:
    """Fetch every event in a series, following cursor pagination.

    Kalshi's /events endpoint returns a page of events plus a `cursor`
    string. Passing that cursor back in the next request retrieves the
    following page. An empty/missing cursor means there are no more pages.
    """
    events: list[dict] = []
    cursor = None

    while True:
        params = {
            "series_ticker": series_ticker,
            "limit": PAGE_LIMIT,
            # Include nested Market objects so we can derive an
            # event-level status (see module docstring) -- the Event
            # object itself does not carry a status field.
            "with_nested_markets": "true",
        }
        # Only include the cursor once we actually have one -- the first
        # request must be made without it.
        if cursor:
            params["cursor"] = cursor

        payload = kalshi_get("/events", params=params)
        events.extend(payload.get("events", []))

        cursor = payload.get("cursor")
        if not cursor:
            # No cursor in the response means we've reached the last page.
            break

    return events


def derive_event_status(event: dict) -> str:
    """Return this event's status, derived from its first nested market.

    Kalshi's Event object has no `status` field of its own; every market
    nested inside a given KXHIGHNY event shares the same strike date and
    moves through open/closed/settled together, so the first market's
    status is representative of the event as a whole.
    """
    markets = event.get("markets") or []
    if not markets:
        return "unknown"
    return markets[0].get("status", "unknown")


def summarize(events: list[dict]) -> None:
    """Print a human-readable summary of the retrieved events."""
    print(f"\nTotal KXHIGHNY events found: {len(events)}")

    if not events:
        print("No events returned -- nothing further to summarize.")
        return

    # Sort a copy by strike_date so we can report earliest/latest and
    # first/last 5 chronologically. Some events may have a null
    # strike_date; push those to the end so they don't break sorting.
    def sort_key(event: dict):
        return event.get("strike_date") or ""

    sorted_events = sorted(events, key=sort_key)

    earliest = sorted_events[0]
    latest = sorted_events[-1]
    print(f"Earliest event: {earliest.get('event_ticker')} (strike_date={earliest.get('strike_date')})")
    print(f"Latest event:   {latest.get('event_ticker')} (strike_date={latest.get('strike_date')})")

    # Count events by (derived) status -- see derive_event_status().
    status_counts: dict[str, int] = {}
    for event in events:
        status = derive_event_status(event)
        status_counts[status] = status_counts.get(status, 0) + 1
    print("\nEvents by status (derived from nested market status):")
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    def print_event_row(event: dict) -> None:
        print(
            f"  {event.get('event_ticker')} | strike_date={event.get('strike_date')} "
            f"| status={derive_event_status(event)} | title={event.get('title')}"
        )

    print("\nFirst 5 events (chronological):")
    for event in sorted_events[:5]:
        print_event_row(event)

    print("\nLast 5 events (chronological):")
    for event in sorted_events[-5:]:
        print_event_row(event)


def parse_kalshi_ts(value: str) -> int:
    """Parse a Kalshi ISO-8601 timestamp into a Unix timestamp (seconds).

    Kalshi timestamps use a trailing "Z" and sometimes a fractional-seconds
    component that isn't exactly 3 or 6 digits (e.g. ".10454"), which
    Python's datetime.fromisoformat() rejects on this Python version. We
    normalize any fractional-seconds component to exactly 6 digits before
    parsing, rather than guessing at the value.
    """
    normalized = value.replace("Z", "+00:00")
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(\+00:00)$", normalized)
    if match:
        whole, frac, offset = match.groups()
        frac = (frac + "000000")[:6]  # pad/truncate to exactly 6 digits (microseconds)
        normalized = f"{whole}.{frac}{offset}"
    return int(datetime.fromisoformat(normalized).timestamp())


def safe_filename(ticker: str) -> str:
    """Turn a market/event ticker into a filesystem-safe filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


def fetch_historical_cutoff() -> dict:
    """GET /historical/cutoff.

    Returns the per-data-type cutoff timestamps that mark the boundary
    between Kalshi's live and historical databases (verified 2026-09-25,
    https://docs.kalshi.com/getting_started/historical_data). Any market
    whose settlement time (market_settled_ts) is older than the returned
    cutoff must be queried through the /historical/ endpoints instead of
    the live ones. The cutoff advances over time, so callers should fetch
    it fresh rather than hardcoding a date.
    """
    return kalshi_get("/historical/cutoff")


def fetch_event_metadata(event_ticker: str) -> dict:
    """GET /events/{event_ticker} -- event-level metadata.

    Not subject to the historical cutoff: only per-market pricing/
    candlestick data is archived out of the live database, so this call
    works the same way for a 2021 event as for a 2026 one.
    """
    payload = kalshi_get(f"/events/{event_ticker}")
    return payload["event"]


def fetch_event_markets(event_ticker: str) -> tuple[list[dict], str]:
    """Recover every market belonging to an event, live or historical.

    Tries GET /events/{event_ticker}?with_nested_markets=true first (the
    documented way to get nested markets on a live/recent event). If that
    comes back with no markets -- which is the normal case for most older
    events, confirmed during the historical-coverage test in this project
    -- falls back to the documented GET /historical/markets?event_ticker=...
    list endpoint, following its cursor pagination. Both are official
    endpoints; this is not an undocumented workaround.

    Returns (markets, source) where source is "live_nested",
    "historical_list", or "none" if neither endpoint returned any markets.
    """
    payload = kalshi_get(f"/events/{event_ticker}", params={"with_nested_markets": "true"})
    markets = (payload.get("event") or {}).get("markets") or []
    if markets:
        return markets, "live_nested"

    markets = []
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
    return markets, ("historical_list" if markets else "none")


def classify_market_structure(markets: list[dict]) -> str:
    """Classify an event's market structure as "bucketed", "single_threshold",
    or "unknown".

    This is a classification WE derive for this project -- Kalshi's API has
    no "market_structure" field of its own. The rule, based on what was
    actually observed across eras during the historical-coverage test:
    modern events have multiple markets each carrying a strike_type field
    (>1 market, "bucketed"); the earliest-era events have exactly one
    market with no strike_type/floor_strike/cap_strike at all
    ("single_threshold"). Anything that doesn't cleanly match either
    pattern is reported as "unknown" rather than forced into one.
    """
    if not markets:
        return "unknown"
    if len(markets) == 1:
        return "single_threshold"
    if all("strike_type" in m for m in markets):
        return "bucketed"
    return "unknown"


def route_candlestick_endpoint(market: dict, cutoff: dict) -> str:
    """Decide whether a market's candlesticks must come from the live or
    the historical endpoint, using the cutoff returned by
    fetch_historical_cutoff() -- never a hardcoded date.

    A market is routed to the historical endpoint if its settlement time
    (or close_time, if not yet settled) is older than the cutoff's
    market_settled_ts. An unsettled/still-open market has no settlement_ts
    and a close_time that hasn't passed, so it naturally routes to live.
    """
    reference_ts = market.get("settlement_ts") or market.get("close_time")
    cutoff_ts = cutoff.get("market_settled_ts")
    if not reference_ts or not cutoff_ts:
        return "live"
    return "historical" if parse_kalshi_ts(reference_ts) < parse_kalshi_ts(cutoff_ts) else "live"


def fetch_market_candlesticks(
    market: dict, period_interval: int, cutoff: dict, series_ticker: str = SERIES_TICKER
) -> tuple[list[dict], str, str | None]:
    """Fetch candlesticks for one market, routed to the live or historical
    endpoint per route_candlestick_endpoint(), covering the market's full
    open_time -> settlement_ts/close_time window.

    Returns (candlesticks, endpoint_path, error). On failure, candlesticks
    is [] and error holds the exact exception message -- callers are
    expected to log it and continue rather than let one bad market abort
    an entire batch (see scripts/download_kalshi_hourly.py).
    """
    ticker = market["ticker"]
    if not market.get("open_time"):
        return [], "", "no open_time on this market"
    start_ts = parse_kalshi_ts(market["open_time"])

    end_source = market.get("settlement_ts") or market.get("close_time")
    if not end_source:
        return [], "", "no settlement_ts or close_time on this market"
    end_ts = min(parse_kalshi_ts(end_source), int(datetime.now(timezone.utc).timestamp()))

    route = route_candlestick_endpoint(market, cutoff)
    path = (
        f"/historical/markets/{ticker}/candlesticks"
        if route == "historical"
        else f"/series/{series_ticker}/markets/{ticker}/candlesticks"
    )
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    try:
        payload = kalshi_get(path, params=params)
        return payload.get("candlesticks", []), path, None
    except RuntimeError as exc:
        return [], path, str(exc)


def normalize_candlestick(candle: dict) -> dict:
    """Flatten one candlestick record from either the live or historical
    candlestick endpoint into one consistent schema.

    Verified live against both endpoints: the live endpoint nests OHLC
    data under keys like "open_dollars"/"close_dollars" and reports
    volume/open_interest as "volume_fp"/"open_interest_fp"; the historical
    endpoint uses the bare names "open"/"close"/"volume"/"open_interest"
    for the exact same concepts. Both are handled here so downstream code
    never has to care which endpoint a candle came from.
    """

    def _f(obj: dict | None, dollar_key: str, plain_key: str):
        if not obj:
            return None
        v = obj.get(dollar_key, obj.get(plain_key))
        return float(v) if v not in (None, "") else None

    def _count(dollar_key: str, plain_key: str):
        v = candle.get(dollar_key, candle.get(plain_key))
        return float(v) if v not in (None, "") else None

    yes_bid, yes_ask, price = candle.get("yes_bid") or {}, candle.get("yes_ask") or {}, candle.get("price") or {}
    return {
        "timestamp": candle.get("end_period_ts"),
        "yes_bid_open": _f(yes_bid, "open_dollars", "open"),
        "yes_bid_high": _f(yes_bid, "high_dollars", "high"),
        "yes_bid_low": _f(yes_bid, "low_dollars", "low"),
        "yes_bid_close": _f(yes_bid, "close_dollars", "close"),
        "yes_ask_open": _f(yes_ask, "open_dollars", "open"),
        "yes_ask_high": _f(yes_ask, "high_dollars", "high"),
        "yes_ask_low": _f(yes_ask, "low_dollars", "low"),
        "yes_ask_close": _f(yes_ask, "close_dollars", "close"),
        "price_open": _f(price, "open_dollars", "open"),
        "price_high": _f(price, "high_dollars", "high"),
        "price_low": _f(price, "low_dollars", "low"),
        "price_close": _f(price, "close_dollars", "close"),
        "price_mean": _f(price, "mean_dollars", "mean"),
        "volume": _count("volume_fp", "volume"),
        "open_interest": _count("open_interest_fp", "open_interest"),
    }


def main() -> None:
    print(f"Fetching all events for series '{SERIES_TICKER}' from {BASE_URL}/events ...")
    events = fetch_all_events(SERIES_TICKER)

    # For every event, we retain the full raw record as returned by the
    # API (this includes event_ticker, series_ticker, title, sub_title,
    # strike_date, status, settlement_sources, mutually_exclusive, and
    # any other fields Kalshi provides) so nothing is lost or guessed.
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
    print(f"\nSaved {len(events)} raw event records to {OUTPUT_PATH}")

    summarize(events)


if __name__ == "__main__":
    main()
