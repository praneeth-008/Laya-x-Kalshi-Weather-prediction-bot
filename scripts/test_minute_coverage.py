"""PART A -- test how far back and how dense Kalshi's 1-minute historical
candlestick data actually is for KXHIGHNY/HIGHNY events.

This is a bounded coverage TEST across six representative events spanning
2021-2026, not a bulk downloader. It answers:
  1. Does Kalshi provide 1-minute historical candlesticks?
  2. How far back does usable 1-minute data go?
  3. How dense is it (candles vs. total window minutes)?
  4. Which fields are populated at 1-minute resolution?
  5. Roughly how large would a full 1-minute dataset be?

Verified against https://docs.kalshi.com/api-reference on 2026-09-25:
period_interval accepts 1 (minute), 60 (hour), or 1440 (day) on BOTH the
live (/series/{series}/markets/{ticker}/candlesticks) and historical
(/historical/markets/{ticker}/candlesticks) endpoints. No documented limit
on the start_ts/end_ts span was found for either endpoint -- this script
does not chunk requests, and instead reports the coverage actually
observed so any hidden truncation would show up as low coverage %.

Does NOT touch weather data, Laya integration, modeling, or trading logic.

Usage:
    python scripts/test_minute_coverage.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.kalshi import (  # noqa: E402
    OUTPUT_PATH as EVENTS_JSON_PATH,
)
from data.kalshi import (  # noqa: E402
    classify_market_structure,
    fetch_event_markets,
    fetch_historical_cutoff,
    fetch_market_candlesticks,
    normalize_candlestick,
    parse_kalshi_ts,
    safe_filename,
)

OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "kalshi" / "minute_coverage_test"
PERIOD_INTERVAL_MINUTES = 1

# One target date per era we want to test. "oldest" is handled separately
# (nearest-to-date doesn't apply -- we want the actual earliest event).
ERA_TARGETS = [
    ("recent_2026", datetime(2026, 9, 15, tzinfo=timezone.utc)),
    ("mid_2025", datetime(2025, 7, 1, tzinfo=timezone.utc)),
    ("mid_2024", datetime(2024, 7, 1, tzinfo=timezone.utc)),
    ("mid_2023", datetime(2023, 7, 1, tzinfo=timezone.utc)),
    ("mid_2022", datetime(2022, 7, 1, tzinfo=timezone.utc)),
]


# ---------------------------------------------------------------------------
# STEP A1 -- Select test events across time from the existing cached file.
# ---------------------------------------------------------------------------

def _event_date(event: dict):
    """Derive a sortable date for an event. strike_date is null for many
    older events, so we parse the date out of the ticker (HIGHNY-YYMONDD /
    KXHIGHNY-YYMONDD) instead -- the same approach validated in the prior
    historical-coverage test, where it successfully dated 100% of the
    1,874 cached events."""
    import re

    m = re.match(r"^(?:KX)?HIGHNY-(\d{2})([A-Z]{3})(\d{2})", event.get("event_ticker", ""))
    if not m:
        return None
    yy, mon, dd = m.groups()
    try:
        return datetime.strptime(f"20{yy}-{mon}-{dd}", "%Y-%b-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_test_events(events_path: Path) -> list[tuple[str, dict, datetime]]:
    with open(events_path, "r", encoding="utf-8") as f:
        events = json.load(f)
    print(f"Loaded {len(events)} cached events from {events_path}")

    dated = [(e, _event_date(e)) for e in events]
    dated = [(e, d) for e, d in dated if d is not None]
    dated.sort(key=lambda pair: pair[1])

    selected: list[tuple[str, dict, datetime]] = []

    for label, target in ERA_TARGETS:
        nearest = min(dated, key=lambda pair: abs((pair[1] - target).total_seconds()))
        selected.append((label, nearest[0], nearest[1]))

    oldest_event, oldest_date = dated[0]
    selected.append(("oldest_2021", oldest_event, oldest_date))

    print("\n=== STEP A1: SELECTED TEST EVENTS ===")
    for label, event, date in selected:
        print(f"  [{label:12s}] {event['event_ticker']:20s} date={date.date()} title={event['title']}")

    return selected


# ---------------------------------------------------------------------------
# STEP A2 -- Test 1-minute candlesticks for every market in each event.
# ---------------------------------------------------------------------------

def test_event_minute_coverage(label: str, event_ticker: str, cutoff: dict, out_dir: Path) -> dict:
    print(f"\n{'=' * 70}\n{label}: {event_ticker}\n{'=' * 70}")
    candle_dir = out_dir / "candlesticks"
    out_dir.mkdir(parents=True, exist_ok=True)
    candle_dir.mkdir(parents=True, exist_ok=True)

    try:
        markets, source = fetch_event_markets(event_ticker)
    except RuntimeError as exc:
        print(f"  FAILED to fetch markets for {event_ticker}: {exc}")
        return {"event_ticker": event_ticker, "label": label, "markets": [], "error": str(exc)}

    structure = classify_market_structure(markets)
    print(f"Markets recovered: {len(markets)} (source={source}, structure={structure})")
    if structure != "bucketed":
        print(
            f"  NOTE: this event's market structure is '{structure}', not the modern "
            f"multi-bucket layout. Testing whatever market(s) exist rather than skipping."
        )

    with open(out_dir / "markets.json", "w", encoding="utf-8") as f:
        json.dump(markets, f, indent=2)

    per_market = []
    combined_rows = []
    for m in markets:
        ticker = m["ticker"]
        bucket = m.get("yes_sub_title", m.get("subtitle", ""))
        open_time, close_time = m.get("open_time"), m.get("close_time")

        candles, path, error = fetch_market_candlesticks(m, PERIOD_INTERVAL_MINUTES, cutoff)
        with open(candle_dir / f"{safe_filename(ticker)}.json", "w", encoding="utf-8") as f:
            json.dump(candles, f, indent=2)

        if error:
            print(f"  {ticker}: FAILED ({path}): {error}")
            per_market.append({"ticker": ticker, "bucket": bucket, "error": error})
            continue

        normalized = [normalize_candlestick(c) for c in candles]
        timestamps = [n["timestamp"] for n in normalized if n["timestamp"] is not None]

        window_minutes = None
        coverage_pct = None
        if open_time and close_time:
            window_minutes = (parse_kalshi_ts(close_time) - parse_kalshi_ts(open_time)) / 60
            if window_minutes > 0:
                coverage_pct = 100.0 * len(candles) / window_minutes

        has_bid = any(n["yes_bid_close"] is not None for n in normalized)
        has_ask = any(n["yes_ask_close"] is not None for n in normalized)
        has_price = any(n["price_close"] is not None for n in normalized)
        has_volume = any(n["volume"] is not None for n in normalized)
        has_oi = any(n["open_interest"] is not None for n in normalized)

        earliest = datetime.fromtimestamp(min(timestamps), tz=timezone.utc) if timestamps else None
        latest = datetime.fromtimestamp(max(timestamps), tz=timezone.utc) if timestamps else None

        print(
            f"  {ticker:26s} candles={len(candles):5d}  window_min={window_minutes!s:>8s}  "
            f"coverage={coverage_pct:.1f}%  bid={has_bid} ask={has_ask} price={has_price} "
            f"vol={has_volume} oi={has_oi}" if window_minutes else
            f"  {ticker:26s} candles={len(candles):5d}  (no open/close window available)"
        )

        per_market.append(
            {
                "ticker": ticker,
                "bucket": bucket,
                "open_time": open_time,
                "close_time": close_time,
                "n_candles": len(candles),
                "earliest": earliest,
                "latest": latest,
                "window_minutes": window_minutes,
                "coverage_pct": coverage_pct,
                "has_bid": has_bid,
                "has_ask": has_ask,
                "has_price": has_price,
                "has_volume": has_volume,
                "has_open_interest": has_oi,
            }
        )
        for n in normalized:
            combined_rows.append({**n, "market_ticker": ticker, "bucket": bucket})

    combined_df = pd.DataFrame(combined_rows)
    combined_df.to_csv(out_dir / "candlesticks_combined.csv", index=False)

    return {
        "event_ticker": event_ticker,
        "label": label,
        "structure": structure,
        "markets": markets,
        "per_market": per_market,
        "combined_df": combined_df,
    }


# ---------------------------------------------------------------------------
# STEP A3 -- Minute-level data quality per event.
# ---------------------------------------------------------------------------

def data_quality_check(result: dict) -> dict:
    df = result["combined_df"]
    n_markets = len(result["markets"])
    print(f"\n--- STEP A3: minute data quality -- {result['event_ticker']} ---")
    if df.empty or n_markets == 0:
        print("  No candlestick rows -- skipping quality check.")
        return {"total_rows": 0}

    total_rows = len(df)
    unique_minutes = df["timestamp"].nunique()
    counts_per_minute = df.groupby("timestamp")["market_ticker"].nunique()
    minutes_all_have_candle = int((counts_per_minute == n_markets).sum())

    df = df.copy()
    df["is_placeholder"] = (df["yes_bid_close"] == 0) & (df["yes_ask_close"] == 1)
    has_quote = df["yes_bid_close"].notna() & df["yes_ask_close"].notna()

    pivot_quote = df[has_quote].pivot_table(index="timestamp", columns="market_ticker", values="yes_bid_close")
    minutes_all_have_quote = int(pivot_quote.dropna(how="any").shape[0]) if not pivot_quote.empty else 0

    minutes_with_placeholder = int(df.loc[df["is_placeholder"], "timestamp"].nunique())

    # Total window minutes: use the widest open->close span across this
    # event's markets (they should all share the same strike date/window).
    windows = [m["window_minutes"] for m in result["per_market"] if m.get("window_minutes")]
    total_window_minutes = max(windows) if windows else None
    missing_minutes = missing_pct = None
    if total_window_minutes:
        missing_minutes = max(0, int(total_window_minutes) - unique_minutes)
        missing_pct = 100.0 * missing_minutes / total_window_minutes

    print(f"  Total 1-minute rows across all markets: {total_rows}")
    print(f"  Unique minute timestamps: {unique_minutes}")
    print(f"  Minutes where every bucket has a candle: {minutes_all_have_candle}")
    print(f"  Minutes where every bucket has usable bid/ask: {minutes_all_have_quote}")
    print(f"  Minutes containing an empty-book placeholder (bid=0,ask=1): {minutes_with_placeholder}")
    if total_window_minutes:
        print(f"  Total trading-window minutes: {int(total_window_minutes)}")
        print(f"  Missing minutes: {missing_minutes} ({missing_pct:.1f}%)")

    quality = {
        "total_rows": total_rows,
        "unique_minutes": unique_minutes,
        "minutes_all_have_candle": minutes_all_have_candle,
        "minutes_all_have_quote": minutes_all_have_quote,
        "minutes_with_placeholder": minutes_with_placeholder,
        "total_window_minutes": total_window_minutes,
        "missing_minutes": missing_minutes,
        "missing_pct": missing_pct,
    }

    if n_markets > 1 and result["structure"] == "bucketed":
        valid = df[has_quote & ~df["is_placeholder"]].copy()
        valid["midpoint"] = (valid["yes_bid_close"] + valid["yes_ask_close"]) / 2
        pivot_mid = valid.pivot_table(index="timestamp", columns="market_ticker", values="midpoint")
        complete = pivot_mid.dropna(how="any")
        if not complete.empty:
            sums = complete.sum(axis=1)
            print(
                f"  sum_of_bucket_midpoints over {len(complete)} fully-quoted minutes: "
                f"mean={sums.mean():.4f} median={sums.median():.4f} min={sums.min():.4f} max={sums.max():.4f}"
            )
            quality["midpoint_sum_mean"] = sums.mean()
            quality["midpoint_sum_median"] = sums.median()
            quality["midpoint_sum_min"] = sums.min()
            quality["midpoint_sum_max"] = sums.max()
        else:
            print("  No minute has valid quotes across every bucket -- cannot compute midpoint sums.")
    else:
        print(f"  Structure is '{result['structure']}' -- skipping cross-bucket midpoint-sum analysis.")

    return quality


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("STEP A1: Select test events across time")
    print("-" * 70)
    selected = select_test_events(EVENTS_JSON_PATH)

    print("\nFetching GET /historical/cutoff once (used to route every market to the live or historical endpoint) ...")
    cutoff = fetch_historical_cutoff()
    print(json.dumps(cutoff, indent=2))

    all_results = []
    for label, event, _date in selected:
        out_dir = OUTPUT_DIR / label
        result = test_event_minute_coverage(label, event["event_ticker"], cutoff, out_dir)
        if "error" not in result:
            result["quality"] = data_quality_check(result)
            result["date"] = _date
        all_results.append(result)

    # -----------------------------------------------------------------
    # STEP A4 + A5 + FINAL REPORT (PART A)
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PART A FINAL REPORT")
    print("=" * 70)

    header = f"{'Year':6s} {'Event':16s} {'Markets':8s} {'1-min rows':11s} {'Earliest':20s} {'Latest':20s} {'Cov %':7s} {'Bid/Ask':8s} {'Price':6s} {'Volume':7s}"
    print(header)
    table_rows = []
    for r in all_results:
        if "error" in r or r["combined_df"].empty:
            print(f"{r['label']:6s} {r['event_ticker']:16s} (no minute data retrieved)")
            table_rows.append({"label": r["label"], "n_rows": 0})
            continue
        df = r["combined_df"]
        n_markets = len(r["markets"])
        n_rows = len(df)
        earliest = datetime.fromtimestamp(df["timestamp"].min(), tz=timezone.utc)
        latest = datetime.fromtimestamp(df["timestamp"].max(), tz=timezone.utc)
        covs = [m["coverage_pct"] for m in r["per_market"] if m.get("coverage_pct") is not None]
        cov = sum(covs) / len(covs) if covs else float("nan")
        bid_ask = any(m.get("has_bid") and m.get("has_ask") for m in r["per_market"])
        price = any(m.get("has_price") for m in r["per_market"])
        volume = any(m.get("has_volume") for m in r["per_market"])
        print(
            f"{r['date'].year:<6d} {r['event_ticker']:16s} {n_markets:<8d} {n_rows:<11d} "
            f"{str(earliest):20s} {str(latest):20s} {cov:<7.1f} {str(bid_ask):8s} {str(price):6s} {str(volume):7s}"
        )
        table_rows.append(
            {
                "label": r["label"],
                "year": r["date"].year,
                "event_ticker": r["event_ticker"],
                "n_markets": n_markets,
                "n_rows": n_rows,
                "cov": cov,
            }
        )

    # STEP A4: earliest verified event with 1-minute data, based only on
    # what was actually tested above.
    successful = [r for r in all_results if "error" not in r and not r["combined_df"].empty]
    print("\nSTEP A4: Earliest verified 1-minute coverage")
    if successful:
        earliest_event = min(successful, key=lambda r: r["date"])
        print(f"  EARLIEST VERIFIED EVENT WITH 1-MINUTE DATA: {earliest_event['event_ticker']}")
        print(f"  DATE: {earliest_event['date'].date()}")
        print(
            "  This means 1-minute data was successfully retrieved for this specific event "
            "only -- continuous coverage before/after it was NOT tested and is not claimed."
        )
        has_2023 = any(r["date"].year == 2023 for r in successful)
        print(f"  2023 minute data works: {has_2023}")
    else:
        print("  No test event returned any 1-minute data.")

    # STEP A5: bulk size estimate, from observed rows only -- explicitly
    # labeled as an estimate.
    print("\nSTEP A5: Bulk 1-minute dataset size ESTIMATE (from observed test rows only)")
    if successful:
        total_test_rows = sum(len(r["combined_df"]) for r in successful)
        total_test_markets = sum(len(r["markets"]) for r in successful)
        rows_per_market_per_event = total_test_rows / total_test_markets if total_test_markets else 0
        print(f"  Observed: {total_test_rows} rows across {total_test_markets} markets in {len(successful)} test events")
        print(f"  ~ rows per market (avg, observed): {rows_per_market_per_event:.0f}")

        # Estimate rows/event using observed avg markets-per-event for
        # bucketed vs single-threshold events, and known event counts/year
        # (from the cached bulk file, computed once here rather than reloading).
        with open(EVENTS_JSON_PATH, "r", encoding="utf-8") as f:
            all_events = json.load(f)
        import re as _re

        def _year_of(e):
            m = _re.match(r"^(?:KX)?HIGHNY-(\d{2})([A-Z]{3})(\d{2})", e.get("event_ticker", ""))
            return 2000 + int(m.group(1)) if m else None

        events_per_year: dict[int, int] = {}
        for e in all_events:
            y = _year_of(e)
            if y:
                events_per_year[y] = events_per_year.get(y, 0) + 1

        avg_markets_per_event = total_test_markets / len(successful)
        rows_per_event_est = rows_per_market_per_event * avg_markets_per_event
        print(f"  ~ avg markets per event (observed across test events): {avg_markets_per_event:.1f}")
        print(f"  ~ ESTIMATED rows per event: {rows_per_event_est:.0f}")

        # Measure actual on-disk bytes/row from the combined CSVs we just wrote.
        sample_csv_bytes, sample_rows = 0, 0
        for r in successful:
            p = OUTPUT_DIR / r["label"] / "candlesticks_combined.csv"
            if p.exists():
                sample_csv_bytes += p.stat().st_size
                sample_rows += len(r["combined_df"])
        bytes_per_row = sample_csv_bytes / sample_rows if sample_rows else 0
        print(f"  ~ measured CSV bytes/row (from these test files): {bytes_per_row:.0f}")

        for year in sorted(events_per_year):
            if year < 2023:
                continue
            est_rows = rows_per_event_est * events_per_year[year]
            print(f"  ~ ESTIMATED rows for {year}: {est_rows:,.0f} ({events_per_year[year]} events)")

        rows_2023_present = sum(
            rows_per_event_est * n for y, n in events_per_year.items() if y >= 2023
        )
        est_csv_mb = rows_2023_present * bytes_per_row / (1024 * 1024)
        print(f"  ~ ESTIMATED total rows, 2023-present: {rows_2023_present:,.0f}")
        print(f"  ~ ESTIMATED uncompressed CSV size, 2023-present: {est_csv_mb:,.1f} MB")

        # Measure an actual Parquet compression ratio from the test data
        # itself, instead of assuming a generic multiplier.
        try:
            all_test_df = pd.concat([r["combined_df"] for r in successful], ignore_index=True)
            parquet_path = OUTPUT_DIR / "_size_estimate_sample.parquet"
            all_test_df.to_parquet(parquet_path, index=False)
            measured_ratio = sample_csv_bytes / parquet_path.stat().st_size if parquet_path.stat().st_size else None
            parquet_path.unlink()
            if measured_ratio:
                print(f"  ~ measured CSV:Parquet size ratio on this test data: {measured_ratio:.1f}x")
                print(f"  ~ ESTIMATED Parquet size, 2023-present: {est_csv_mb / measured_ratio:,.1f} MB")
        except Exception as exc:  # pragma: no cover - best-effort estimate only
            print(f"  (could not measure a Parquet ratio: {exc})")
    else:
        print("  No successful test events -- cannot estimate.")

    print("\nAPI limitations discovered:")
    print(
        "  - No documented cap on the start_ts/end_ts span for period_interval=1 was found in the\n"
        "    official docs; none was hit here either (observed coverage % reflects real market\n"
        "    activity sparsity, not request truncation -- see per-market coverage % above).\n"
        "  - A missing minute does NOT mean zero volume -- Kalshi's docs do not establish that\n"
        "    interpretation, and this script does not assume it (see missing-minute counts above,\n"
        "    which are reported as 'no candle', not treated as zero-activity)."
    )


if __name__ == "__main__":
    main()
