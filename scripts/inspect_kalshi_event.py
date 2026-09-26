"""Deep inspection of ONE finalized KXHIGHNY event, end to end.

Purpose: before generalizing the pipeline to all 1,874 discovered events,
understand exactly what Kalshi's API gives us for a single NYC daily-high
event -- event metadata, every temperature-bucket market belonging to it,
its settlement outcome, and its full historical candlestick price series.

This script does NOT download the bulk history of all events, does NOT
touch weather data, and does NOT contain any trading logic. It reuses the
shared request helper and constants from data/kalshi.py rather than
duplicating API logic.

API endpoints used (verified against https://docs.kalshi.com/api-reference
on 2026-09-25):
- GET /events/{event_ticker}                                  (event + nested markets)
- GET /series/{series_ticker}/markets/{ticker}/candlesticks    (live candlesticks)
- GET /historical/markets/{ticker}/candlesticks                (fallback for markets
  Kalshi has archived out of the live candlestick dataset)

Usage:
    python scripts/inspect_kalshi_event.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Make the project root importable so we can reuse data/kalshi.py instead
# of duplicating its request/error-handling logic.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import BASE_URL, OUTPUT_PATH as EVENTS_JSON_PATH  # noqa: E402
from data.kalshi import SERIES_TICKER, derive_event_status, kalshi_get  # noqa: E402

# Where this script's outputs are written. Everything here lives under
# data/raw/, which is gitignored -- see .gitignore -- so none of it is
# ever committed.
SAMPLE_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "sample_event"
CANDLESTICK_DIR = SAMPLE_DIR / "candlesticks"

# Prefer 1-hour candlesticks for this initial inspection, per the task.
CANDLESTICK_PERIOD_INTERVAL_MINUTES = 60


# ---------------------------------------------------------------------------
# STEP 1 -- Select one recent finalized event that has nested markets.
# ---------------------------------------------------------------------------

def select_event(events_path: Path) -> dict:
    """Pick one recent, finalized KXHIGHNY event that has nested markets.

    Reads the already-saved bulk discovery file (data/kalshi.py's output)
    purely to choose a candidate -- no API call is made in this step.
    "Finalized" is derived the same way data/kalshi.py does: from the
    status of the event's first nested market, since Kalshi's Event object
    has no status field of its own.
    """
    with open(events_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    candidates = [
        e for e in events
        if e.get("markets") and derive_event_status(e) == "finalized"
    ]
    if not candidates:
        raise RuntimeError(
            f"No finalized events with nested markets found in {events_path}. "
            "Nothing to select -- stopping rather than guessing a fallback."
        )

    # Most recent by strike_date first.
    candidates.sort(key=lambda e: e.get("strike_date") or "", reverse=True)
    selected = candidates[0]

    print(f"Loaded {len(events)} cached events from {events_path}")
    print(f"Found {len(candidates)} finalized events with nested markets.")
    print(
        f"Selected: {selected['event_ticker']} "
        f"(strike_date={selected.get('strike_date')}, "
        f"{len(selected['markets'])} nested markets in cached file)"
    )
    return selected


# ---------------------------------------------------------------------------
# STEP 2 -- Fetch the full, live event detail and print event-level fields.
# ---------------------------------------------------------------------------

def fetch_event_detail(event_ticker: str) -> dict:
    """GET /events/{event_ticker}?with_nested_markets=true.

    We re-fetch live (rather than reusing the cached bulk file) so this
    inspection reflects the current, complete API response for this one
    event -- including every field Kalshi returns, not just what happened
    to be cached during the earlier bulk pass.
    """
    payload = kalshi_get(f"/events/{event_ticker}", params={"with_nested_markets": "true"})
    return payload["event"]


def print_event_level_fields(event: dict) -> None:
    markets = event.get("markets", [])
    print("\n=== EVENT-LEVEL FIELDS ===")
    print(f"event_ticker:       {event.get('event_ticker')}")
    print(f"series_ticker:      {event.get('series_ticker')}")
    print(f"title:              {event.get('title')}")
    print(f"sub_title:          {event.get('sub_title')}")
    print(f"strike_date:        {event.get('strike_date')}")
    print(f"mutually_exclusive: {event.get('mutually_exclusive')}")
    print(f"settlement_sources: {event.get('settlement_sources')}")
    # Kalshi's Event object itself has no open/close timestamp -- those
    # live on each nested Market. We report the range across all markets
    # in this event as the "relevant open/close dates".
    open_times = sorted({m.get("open_time") for m in markets if m.get("open_time")})
    close_times = sorted({m.get("close_time") for m in markets if m.get("close_time")})
    print(f"market open_time(s) across event:  {open_times}")
    print(f"market close_time(s) across event: {close_times}")
    print(f"number of markets:  {len(markets)}")


# ---------------------------------------------------------------------------
# STEP 3 -- Build a DataFrame of every market/bucket in the event.
# ---------------------------------------------------------------------------

# Requested field -> actual Kalshi field name on the Market object. Kalshi
# stores dollar amounts and contract counts as decimal *strings* with
# "_dollars" / "_fp" (fixed-point) suffixes rather than plain numbers, so
# we record the source field name here and convert to float when building
# the DataFrame. Verified against a live GET /events/{ticker} response.
REQUESTED_FIELD_MAP = {
    "ticker": "ticker",
    "title": "title",
    # Kalshi's Market object has no single "subtitle" field -- it splits
    # the bucket description into yes_sub_title / no_sub_title. We surface
    # yes_sub_title as "subtitle" below and keep the no_sub_title too.
    "subtitle": "yes_sub_title",
    "status": "status",
    "result": "result",
    "yes_bid": "yes_bid_dollars",
    "yes_ask": "yes_ask_dollars",
    "no_bid": "no_bid_dollars",
    "no_ask": "no_ask_dollars",
    "last_price": "last_price_dollars",
    "volume": "volume_fp",
    "open_interest": "open_interest_fp",
    "open_time": "open_time",
    "close_time": "close_time",
    "expiration_time": "expiration_time",
    "settlement_time": "settlement_ts",
    "floor_strike": "floor_strike",
    "cap_strike": "cap_strike",
    "strike_type": "strike_type",
    "settlement_value": "settlement_value_dollars",
}

DOLLAR_FIELDS = {"yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "settlement_value"}
COUNT_FIELDS = {"volume", "open_interest"}


def build_markets_dataframe(markets: list[dict]) -> pd.DataFrame:
    """Build a DataFrame covering every requested field, checking for gaps."""
    if not markets:
        raise RuntimeError("Event has no nested markets -- nothing to inspect.")

    # Report which requested fields Kalshi actually returned, instead of
    # assuming. A field is "present" if at least one market has that key.
    available_keys = set()
    for m in markets:
        available_keys.update(m.keys())

    print("\n=== FIELD AVAILABILITY CHECK (requested vs. actual API response) ===")
    missing = []
    for requested_name, source_field in REQUESTED_FIELD_MAP.items():
        present = source_field in available_keys
        print(f"  {requested_name:16s} -> {source_field:26s} {'OK' if present else 'MISSING'}")
        if not present:
            missing.append(requested_name)
    if missing:
        print(f"Missing fields not returned by the API: {missing}")
    else:
        print("All requested fields are present on the Market object.")

    # Also surface any extra fields the API returns that weren't asked
    # for, so nothing useful gets silently dropped.
    unrequested = sorted(available_keys - set(REQUESTED_FIELD_MAP.values()))
    print(f"\nAdditional fields returned by the API (not requested, kept in raw JSON only): {unrequested}")

    rows = []
    for m in markets:
        row = {}
        for requested_name, source_field in REQUESTED_FIELD_MAP.items():
            value = m.get(source_field)
            if requested_name in DOLLAR_FIELDS or requested_name in COUNT_FIELDS:
                row[requested_name] = float(value) if value not in (None, "") else None
            else:
                row[requested_name] = value
        rows.append(row)

    df = pd.DataFrame(rows)

    # Sort into logical temperature order using floor_strike, which Kalshi
    # provides on every bucket. The open-ended "less than" bucket has no
    # floor_strike (treated as -inf so it sorts first); the open-ended
    # "greater than" bucket has no cap_strike but does have a floor_strike,
    # so it naturally sorts last.
    # Note: floor_strike is None on the "less than" bucket, but pandas
    # stores that as NaN once the column is built as float64 -- so this
    # must check pd.isna(), not `is None`, or the open-low bucket would
    # sort to the end instead of the start.
    df["_sort_key"] = df["floor_strike"].apply(lambda v: float("-inf") if pd.isna(v) else v)
    df = df.sort_values("_sort_key").drop(columns="_sort_key").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# STEP 4 -- Verify the outcome structure.
# ---------------------------------------------------------------------------

def verify_outcome_structure(df: pd.DataFrame) -> None:
    print("\n=== OUTCOME STRUCTURE ===")
    n_buckets = len(df)
    print(f"Number of temperature buckets: {n_buckets}")

    yes_rows = df[df["result"] == "yes"]
    print(f"Markets that settled YES: {len(yes_rows)}")
    if len(yes_rows) == 1:
        winner = yes_rows.iloc[0]
        print(f"Winning bucket: {winner['ticker']} ({winner['subtitle']})")
    elif len(yes_rows) == 0:
        print("No market has settled YES yet (event may not be fully settled).")
    else:
        print("WARNING: more than one market settled YES -- not the expected mutually exclusive outcome.")

    # Settlement value, exactly as Kalshi returns it -- a per-contract
    # payout in dollars (0 or 1 for a binary market), NOT the observed
    # temperature. Kalshi does not expose the raw observed temperature
    # reading through this endpoint, so we do not infer one.
    print("\nsettlement_value (raw settlement_value_dollars per market, i.e. per-contract payout):")
    print(df[["ticker", "result", "settlement_value"]].to_string(index=False))
    print(
        "Note: settlement_value is the contract's dollar payout (0 or 1), not the "
        "actual recorded temperature. The API does not provide the raw temperature "
        "reading on this endpoint, so no temperature is inferred here."
    )

    # Mutual-exclusivity / collective-exhaustiveness check, using only the
    # floor_strike/cap_strike fields Kalshi provides -- no inferred values.
    # `df` is already sorted into temperature order by build_markets_dataframe.
    #
    # Important: floor/cap inclusivity is NOT uniform across strike_type --
    # verified against each market's own rules_primary text rather than
    # assumed:
    #   strike_type == "less"    -> value <  cap_strike   (strictly below)
    #   strike_type == "between" -> floor_strike <= value <= cap_strike (inclusive)
    #   strike_type == "greater" -> value >  floor_strike  (strictly above)
    # So a "less"/"greater" bucket's open (exclusive) edge should exactly
    # EQUAL the neighboring "between" bucket's inclusive edge (no +1 gap),
    # while two adjacent "between" buckets (both inclusive-inclusive on
    # integers) should differ by exactly 1 to abut without gap or overlap.
    print("\nBucket boundary check (using floor_strike/cap_strike as returned by the API):")
    boundaries_ok = True
    prev_cap = None
    prev_stype = None
    for _, row in df.iterrows():
        floor, cap, stype = row["floor_strike"], row["cap_strike"], row["strike_type"]
        print(f"  {row['ticker']:26s} strike_type={stype:8s} floor_strike={floor} cap_strike={cap}")
        if prev_cap is not None and not pd.isna(floor):
            both_inclusive = prev_stype == "between" and stype == "between"
            expected_floor = prev_cap + 1 if both_inclusive else prev_cap
            if floor != expected_floor:
                boundaries_ok = False
                print(f"    -> GAP/OVERLAP: expected floor_strike {expected_floor}, got {floor}")
        if not pd.isna(cap):
            prev_cap = cap
        prev_stype = stype
    if boundaries_ok:
        print(
            "Buckets tile with no gaps or overlaps across the floor_strike/cap_strike "
            "values provided -- consistent with a mutually exclusive, collectively "
            "exhaustive temperature partition (subject to the open ends of the first "
            "and last buckets, which the API expresses via a null floor/cap rather than "
            "an explicit value)."
        )
    else:
        print("Buckets do NOT tile cleanly based on the floor_strike/cap_strike values returned.")


# ---------------------------------------------------------------------------
# STEP 5 -- Fetch candlesticks for every market in the event.
# ---------------------------------------------------------------------------

def _parse_ts(value: str) -> int:
    """Parse a Kalshi ISO-8601 timestamp into a Unix timestamp.

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


def fetch_candlesticks_for_market(series_ticker: str, market: dict) -> tuple[list[dict], str]:
    """Fetch 1-hour candlesticks for one market, covering its full trading
    period (open_time through settlement, or close_time if not yet settled).

    Tries the live candlesticks endpoint first; if Kalshi has archived the
    market out of the live dataset, falls back to the documented historical
    endpoint. Returns (candlesticks, source) where source records which
    endpoint actually supplied the data, for transparency.
    """
    ticker = market["ticker"]
    if not market.get("open_time"):
        raise RuntimeError(f"Market {ticker} has no open_time -- cannot determine candlestick range.")

    start_ts = _parse_ts(market["open_time"])
    end_source = market.get("settlement_ts") or market.get("close_time")
    if not end_source:
        raise RuntimeError(f"Market {ticker} has no settlement_ts or close_time -- cannot determine candlestick range.")
    end_ts = _parse_ts(end_source)
    # Never request a future end timestamp.
    end_ts = min(end_ts, int(datetime.now(timezone.utc).timestamp()))

    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": CANDLESTICK_PERIOD_INTERVAL_MINUTES,
    }

    live_path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
    try:
        payload = kalshi_get(live_path, params=params)
        return payload.get("candlesticks", []), "live"
    except RuntimeError as live_error:
        print(f"  Live candlesticks request failed for {ticker}: {live_error}")
        print("  Falling back to the documented historical candlesticks endpoint ...")
        historical_path = f"/historical/markets/{ticker}/candlesticks"
        try:
            payload = kalshi_get(historical_path, params=params)
            return payload.get("candlesticks", []), "historical"
        except RuntimeError as historical_error:
            print(f"  Historical candlesticks request also failed for {ticker}: {historical_error}")
            return [], "failed"


def safe_filename(ticker: str) -> str:
    """Turn a market ticker into a filesystem-safe filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


# ---------------------------------------------------------------------------
# STEP 6 -- Save event/market metadata and per-market candlesticks.
# ---------------------------------------------------------------------------

def save_event_and_markets(event: dict, markets_df: pd.DataFrame) -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SAMPLE_DIR / "event.json", "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2)
    with open(SAMPLE_DIR / "markets.json", "w", encoding="utf-8") as f:
        json.dump(event.get("markets", []), f, indent=2)
    markets_df.to_csv(SAMPLE_DIR / "markets.csv", index=False)
    print(f"\nSaved event.json, markets.json, markets.csv to {SAMPLE_DIR}")


def save_candlesticks(ticker: str, candlesticks: list[dict]) -> None:
    CANDLESTICK_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDLESTICK_DIR / f"{safe_filename(ticker)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(candlesticks, f, indent=2)


# ---------------------------------------------------------------------------
# STEP 7 -- Combine all candlesticks into one DataFrame and summarize.
# ---------------------------------------------------------------------------

def candlesticks_to_rows(ticker: str, bucket: str, candlesticks: list[dict]) -> list[dict]:
    """Flatten one market's candlesticks into rows for the combined DataFrame.

    Column mapping (documented because Kalshi's candlestick schema nests
    OHLC data rather than exposing flat fields):
      timestamp     <- end_period_ts (Unix seconds, end of the candle's period)
      yes_bid       <- yes_bid.close_dollars  (close-of-period YES bid quote)
      yes_ask       <- yes_ask.close_dollars  (close-of-period YES ask quote)
      price         <- price.close_dollars    (close-of-period traded price;
                                                null if no trades occurred
                                                during that period)
      volume        <- volume_fp              (contracts traded during the period)
    """
    rows = []
    for c in candlesticks:
        yes_bid = c.get("yes_bid") or {}
        yes_ask = c.get("yes_ask") or {}
        price = c.get("price") or {}
        rows.append(
            {
                "timestamp": c.get("end_period_ts"),
                "market_ticker": ticker,
                "bucket": bucket,
                "yes_bid": float(yes_bid["close_dollars"]) if yes_bid.get("close_dollars") not in (None, "") else None,
                "yes_ask": float(yes_ask["close_dollars"]) if yes_ask.get("close_dollars") not in (None, "") else None,
                "price": float(price["close_dollars"]) if price.get("close_dollars") not in (None, "") else None,
                "volume": float(c["volume_fp"]) if c.get("volume_fp") not in (None, "") else None,
            }
        )
    return rows


def summarize_candlesticks(combined_df: pd.DataFrame, tickers_with_no_data: list[str]) -> None:
    print("\n=== CANDLESTICK SUMMARY ===")
    if combined_df.empty:
        print("No candlestick rows retrieved for any market.")
    else:
        counts = combined_df.groupby("market_ticker").size()
        print("Candlesticks retrieved per market:")
        print(counts.to_string())
        print(f"\nEarliest timestamp: {datetime.fromtimestamp(combined_df['timestamp'].min(), tz=timezone.utc)}")
        print(f"Latest timestamp:   {datetime.fromtimestamp(combined_df['timestamp'].max(), tz=timezone.utc)}")

    if tickers_with_no_data:
        print(f"\nMarkets with NO candlestick data retrieved: {tickers_with_no_data}")
    else:
        print("\nEvery market in this event returned at least one candlestick.")


# ---------------------------------------------------------------------------
# STEP 8 -- Sanity-check bucket midpoint probabilities.
# ---------------------------------------------------------------------------

def sanity_check_probabilities(combined_df: pd.DataFrame, expected_bucket_count: int) -> None:
    print("\n=== BUCKET MIDPOINT PROBABILITY SANITY CHECK ===")
    if combined_df.empty:
        print("No candlestick data available -- skipping.")
        return

    df = combined_df.copy()
    # midpoint = (yes_bid + yes_ask) / 2, per the task -- NOT last trade price.
    df["midpoint"] = (df["yes_bid"] + df["yes_ask"]) / 2

    # Pivot to one row per timestamp, one column per bucket's midpoint.
    pivot = df.pivot_table(index="timestamp", columns="market_ticker", values="midpoint")

    # Only keep timestamps where every bucket in the event has a usable
    # (non-null) midpoint observation.
    complete = pivot.dropna(how="any")
    print(
        f"Timestamps with a midpoint for all {expected_bucket_count} buckets: "
        f"{len(complete)} out of {len(pivot)} total candlestick timestamps."
    )

    if complete.empty:
        print("No timestamp has usable midpoints across every bucket -- cannot sum.")
        return

    sums = complete.sum(axis=1)
    print("\nSample of sum_of_bucket_midpoints at fully-observed timestamps:")
    sample = sums.sample(min(5, len(sums)), random_state=0).sort_index()
    for ts, total in sample.items():
        readable = datetime.fromtimestamp(ts, tz=timezone.utc)
        print(f"  {readable}  ->  sum_of_bucket_midpoints = {total:.4f}")

    print(f"\nOverall: mean={sums.mean():.4f}, min={sums.min():.4f}, max={sums.max():.4f}, n={len(sums)}")
    print(
        "\nThe sum is not expected to equal exactly 1 even for a mutually exclusive, "
        "collectively exhaustive set of buckets, because:\n"
        "  - bid/ask spreads: each bucket's midpoint sits between its own bid and ask,\n"
        "    and those spreads don't cancel out across buckets;\n"
        "  - asynchronous updates: each market's quotes move independently, so an\n"
        "    hourly candle can mix a fresh quote in one bucket with a stale one in\n"
        "    another;\n"
        "  - liquidity: thinly traded buckets can have wide or one-sided quotes that\n"
        "    distort their contribution to the sum;\n"
        "  - stale quotes: a bucket with no recent activity can carry a boundary\n"
        "    quote (e.g. 0.01/1.00) that doesn't reflect current market belief.\n"
        "This is a data-quality observation only -- no trading interpretation is made here."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("STEP 1: Selecting one finalized KXHIGHNY event with nested markets")
    print("-" * 70)
    selected = select_event(EVENTS_JSON_PATH)
    event_ticker = selected["event_ticker"]

    print("\nSTEP 2: Fetching full live event detail")
    print("-" * 70)
    event = fetch_event_detail(event_ticker)
    print_event_level_fields(event)
    markets = event.get("markets", [])

    print("\nSTEP 3: Building the markets DataFrame")
    print("-" * 70)
    markets_df = build_markets_dataframe(markets)
    print("\nMarkets DataFrame (sorted in temperature order):")
    print(markets_df.to_string(index=False))

    print("\nSTEP 4: Verifying the outcome structure")
    print("-" * 70)
    verify_outcome_structure(markets_df)

    print("\nSTEP 5+6: Fetching and saving candlesticks per market")
    print("-" * 70)
    save_event_and_markets(event, markets_df)

    combined_rows = []
    tickers_with_no_data = []
    for m in markets:
        ticker = m["ticker"]
        bucket = m.get("yes_sub_title", "")
        print(f"Fetching candlesticks for {ticker} ({bucket}) ...")
        candlesticks, source = fetch_candlesticks_for_market(SERIES_TICKER, m)
        print(f"  -> {len(candlesticks)} candlesticks from the {source} endpoint")
        save_candlesticks(ticker, candlesticks)
        if not candlesticks:
            tickers_with_no_data.append(ticker)
        combined_rows.extend(candlesticks_to_rows(ticker, bucket, candlesticks))
    print(f"Saved per-market candlestick JSON files to {CANDLESTICK_DIR}")

    combined_df = pd.DataFrame(combined_rows)
    combined_csv_path = SAMPLE_DIR / "candlesticks_combined.csv"
    combined_df.to_csv(combined_csv_path, index=False)
    print(f"Saved combined candlestick DataFrame to {combined_csv_path}")

    print("\nSTEP 7: Candlestick summary")
    print("-" * 70)
    summarize_candlesticks(combined_df, tickers_with_no_data)
    print("\nSample of the combined DataFrame:")
    print(combined_df.head(10).to_string(index=False))

    print("\nSTEP 8: Bucket midpoint probability sanity check")
    print("-" * 70)
    sanity_check_probabilities(combined_df, expected_bucket_count=len(markets))


if __name__ == "__main__":
    main()
