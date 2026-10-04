# KXHIGHNY live L2 recorder

`scripts/kxhighny_recorder.py` is a continuous, **read-only** recorder for
Kalshi's `KXHIGHNY` series (NYC daily maximum-temperature event). It is
completely separate from the historical weather backfill
(`scripts/backfill_*.py`) and from any trading logic -- this project
contains no order-placement code anywhere, and this recorder has no
execution path to one.

## What it records

For the currently active KXHIGHNY event (and, during a day-rollover
overlap, the next one too), for every bucket market in that event:

- the full L2 order book: an initial `orderbook_snapshot` plus every
  subsequent `orderbook_delta`, via Kalshi's authenticated WebSocket
  (`orderbook_delta` channel);
- every public trade (`trade` channel);
- complete event + market discovery metadata, captured once at discovery
  time (bucket definitions are never assumed to repeat day to day).

Raw WebSocket messages are the source of truth and are written essentially
unmodified, before any parsing/validation happens. Nothing is discarded
merely because the recorder doesn't currently use a field.

## Output layout

```
data/live_kalshi_weather/{YYYY-MM-DD}/      # target_date, derived from the event ticker
    metadata/
        event.json              # raw event+markets as first discovered
        event_final.json        # raw event+markets as last seen at finalize time
        discovery_info.json     # recorder_start_utc, started_mid_event, market list
    websocket/
        websocket_raw.jsonl     # EVERY raw WS message: {local_receive_timestamp, raw_message}
    trades/
        trades_raw.jsonl        # convenience copy of just the trade-type raw messages
    logs/
        recorder.log            # human-readable operational log
        gaps.jsonl              # structured anomaly records (gaps/reconnects/malformed/etc.)
    validation/
        book_state_checkpoints.jsonl   # periodic in-memory book state, for sanity-checking only
    manifest.json                # written once the day's markets settle (or time out waiting)
```

This directory is entirely gitignored (`data/live_kalshi_weather/` in
`.gitignore`) -- only the recorder's code, tests, and this doc are
committed, never raw L2/trade data or manifests.

## Authentication

`data/kalshi_auth.py` implements Kalshi's documented signing scheme
(confirmed live 2026-10-04 against
`https://docs.kalshi.com/getting_started/api_keys` and
`.../quick_start_websockets`): headers `KALSHI-ACCESS-KEY` /
`KALSHI-ACCESS-SIGNATURE` / `KALSHI-ACCESS-TIMESTAMP`, signature over
`"{timestamp_ms}{METHOD}{path}"` (RSA-PSS/SHA-256 or Ed25519 depending on
key type), **with the path including the `/trade-api/v2` (REST) or literal
`/trade-api/ws/v2` (WebSocket) prefix** -- a bare sub-path alone produces
`INCORRECT_API_KEY_SIGNATURE` (caught during live testing; use
`build_rest_auth_headers()`/`build_ws_auth_headers()`, not
`build_auth_headers()` directly, to avoid re-introducing that mistake).
Credentials come from the existing `.env` (`KALSHI_KEY_ID`,
`KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV`); both `.env` and `*.pem`/
`private_key.pem` are gitignored.

## Order-book reconstruction and gap detection

`data/kalshi_orderbook.py`:

- `OrderBook` -- per-market L2 state (`yes_levels`/`no_levels` as
  `Decimal` price -> size), applying snapshots and deltas. Raises
  `OrderBookConsistencyError` on a delta before any snapshot or one that
  would drive a level negative.
- `SequenceTracker` -- tracks the expected next `seq` for one subscription
  (`sid`). **Confirmed live (not assumed from the spec): `seq` is scoped to
  the whole subscription, shared across every market multiplexed under
  that `sid`, not to an individual market.** An early version of this
  recorder tracked `seq` per-market and produced a continuous false-positive
  gap/resync storm against production traffic for exactly this reason --
  fixed before the recorder was ever left running unattended.

A sid-level `GAP` invalidates every market subscribed under that sid (not
just the one named in the message that revealed the gap), since the
missed message(s) could belong to any of them. An invalidated market's
`OrderBook.has_snapshot` is cleared (any further delta on it raises until a
fresh snapshot arrives), while `ever_had_snapshot` stays `True` so the
day's manifest can distinguish "never initialized" from "temporarily
invalidated, later resynced."

## Resync and reconnect behavior

- A sid-level gap triggers an `update_subscription` / `get_snapshot`
  command for the affected markets (no reconnect needed).
- A WebSocket error with a **terminal** code (10 "Channel error", 25
  "Subscription buffer overflow") or an actual disconnect triggers a full
  reconnect with exponential backoff, a fresh `subscribe` command, and
  marks every market for resync -- the recorder never pretends to have
  continuous data across an unobserved gap.
- Every disconnect/gap/malformed message/unknown-market message is logged
  to `logs/gaps.jsonl` with a timestamp and detail string.

## Day rollover

A top-level `RecorderDaemon` polls `/events?series_ticker=KXHIGHNY&status=open`
every 5 minutes, verifying every candidate event
(`verify_event_is_kxhighny_nyc_tmax()`: correct series ticker, ticker
prefix, and an NYC/temperature title) before ever subscribing to it --
discovery fails closed (raises `DiscoveryAmbiguousError`) rather than
guessing. Each discovered event gets its own `EventRecorder` task and its
own dated output directory; today's and tomorrow's events can run
concurrently without their messages ever mixing. An `EventRecorder`
retires once its event's markets settle (or a 2-hour post-close safety
timeout elapses), at which point it writes `manifest.json` and exits.

## Manifest `capture_completeness`

- `STARTED_MID_EVENT` -- the recorder (re)started more than 10 minutes
  after this event's own market open_time. Today's (2026-10-04) first-ever
  run is labeled this way, since the markets had already been open and
  trading for roughly 16 hours before recording began.
- `INVALID` -- zero messages captured, or at least one market never got an
  initial snapshot.
- `PARTIAL` -- at least one detected gap occurred, or settlement had not
  been confirmed by finalize time.
- `COMPLETE` -- full-event capture (not started mid-event), no detected
  gaps, every market got a snapshot, and settlement was confirmed.

## Resource safety

Raw messages are appended to disk immediately (never buffered in memory);
only small per-market book state and counters are kept in RAM. The daemon
checks free disk space every discovery cycle (`MIN_FREE_DISK_GB = 5.0`) and
stops all active recorders (rather than silently dropping data) if it
drops below that threshold.

## Running it

```
python scripts/kxhighny_recorder.py
```

Runs forever (until killed), handling day rollover automatically. See
`scripts/test_kalshi_live_recorder.py` for the signing and order-book/
gap-detection unit tests (no network calls, no real credentials).
