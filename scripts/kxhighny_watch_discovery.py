"""Lightweight, READ-ONLY discovery watcher for the next KXHIGHNY event's
opening. This is deliberately NOT a recorder: it never subscribes to the
WebSocket and never writes into data/live_kalshi_weather/{date}/ (the
directory structure owned exclusively by scripts/kxhighny_recorder.py's
EventRecorder). It exists purely to timestamp, with tight polling
precision, exactly when a new KXHIGHNY event for a given target date
becomes visible via Kalshi's public /events endpoint -- used to measure
discovery latency for the opening-capture classification, WITHOUT risking
any collision with the already-running recorder daemon.

Why a separate process instead of tightening the running daemon's own
5-minute discovery poll: the daemon (scripts/kxhighny_recorder.py) is
already running and already correctly handles multi-event overlap on its
own (it will discover and record the new event on its own next poll tick,
within DISCOVERY_POLL_SECONDS of it opening) -- restarting it to shorten
that interval was explicitly ruled out (not "absolutely necessary", and a
second independent recorder process writing into the SAME target-date
directory as the daemon's own eventual EventRecorder would risk colliding
sid/seq streams and raw-file corruption). This script only observes.

Output: data/live_kalshi_weather/_discovery_logs/{target_date}_watch.jsonl
(one line per poll) and {target_date}_discovery_summary.json (written once
the event is first confirmed).
"""
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.kalshi import kalshi_get, event_date

SERIES_TICKER = "KXHIGHNY"
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "live_kalshi_weather" / "_discovery_logs"
POLL_SECONDS = 10
POST_DISCOVERY_EXTRA_POLLS = 6  # keep polling briefly after discovery to confirm stability


def poll_once():
    return kalshi_get(
        "/events",
        params={"series_ticker": SERIES_TICKER, "limit": 50, "with_nested_markets": "true", "status": "open"},
    )


def run(target_date: date, max_minutes: float = 40.0):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    watch_path = OUT_DIR / f"{target_date.isoformat()}_watch.jsonl"
    summary_path = OUT_DIR / f"{target_date.isoformat()}_discovery_summary.json"

    first_discovery_attempt_time = datetime.now(timezone.utc)
    found_at = None
    found_event = None
    poll_count = 0
    extra_polls_remaining = None
    deadline = time.monotonic() + max_minutes * 60

    print(f"[watch] starting discovery watch for KXHIGHNY target_date={target_date} at {first_discovery_attempt_time.isoformat()}")

    while time.monotonic() < deadline:
        poll_count += 1
        poll_time = datetime.now(timezone.utc)
        try:
            payload = poll_once()
            events = payload.get("events", [])
        except Exception as e:
            with open(watch_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"poll_time_utc": poll_time.isoformat(), "poll_count": poll_count, "error": str(e)}) + "\n")
            time.sleep(POLL_SECONDS)
            continue

        matches = [e for e in events if event_date(e.get("event_ticker", "")) and event_date(e["event_ticker"]).date() == target_date]
        record = {
            "poll_time_utc": poll_time.isoformat(),
            "poll_count": poll_count,
            "n_open_events_total": len(events),
            "open_event_tickers": [e.get("event_ticker") for e in events],
            "match_found": bool(matches),
        }
        with open(watch_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        if matches and found_event is None:
            found_event = matches[0]
            found_at = poll_time
            print(f"[watch] FOUND {found_event.get('event_ticker')} at {found_at.isoformat()} (poll #{poll_count})")
            extra_polls_remaining = POST_DISCOVERY_EXTRA_POLLS

        if extra_polls_remaining is not None:
            extra_polls_remaining -= 1
            if extra_polls_remaining <= 0:
                break

        time.sleep(POLL_SECONDS)

    summary = {
        "target_date": target_date.isoformat(),
        "first_discovery_attempt_time_utc": first_discovery_attempt_time.isoformat(),
        "total_polls": poll_count,
        "found": found_event is not None,
    }
    if found_event is not None:
        markets = found_event.get("markets") or []
        open_times = [m.get("open_time") for m in markets if m.get("open_time")]
        summary.update({
            "first_api_response_with_event_time_utc": found_at.isoformat(),
            "event_ticker": found_event.get("event_ticker"),
            "event_title": found_event.get("title"),
            "series_ticker": found_event.get("series_ticker"),
            "n_bucket_markets": len(markets),
            "bucket_market_tickers": [m.get("ticker") for m in markets],
            "kalshi_reported_open_time_min": min(open_times) if open_times else None,
            "kalshi_reported_open_time_max": max(open_times) if open_times else None,
            "raw_event_metadata": found_event,
        })
        discovery_latency_note = (
            "discovery_latency_seconds = (first_api_response_with_event_time_utc - "
            "kalshi_reported_open_time_min), if open_time is in the past at discovery; "
            "if open_time is in the future, the event was visible via /events before "
            "its own open_time (pre-announced), which is a DIFFERENT and more favorable "
            "case for FULL_OPEN_CAPTURE classification."
        )
        summary["discovery_latency_note"] = discovery_latency_note
    else:
        print(f"[watch] did not find target_date={target_date} within {max_minutes} minutes ({poll_count} polls)")

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"[watch] summary written to {summary_path}")
    return summary


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--target-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--max-minutes", type=float, default=40.0)
    args = p.parse_args()
    run(date.fromisoformat(args.target_date), args.max_minutes)
