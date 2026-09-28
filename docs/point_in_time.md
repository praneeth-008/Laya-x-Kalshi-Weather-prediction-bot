# Point-in-Time Integrity

This is the most important document in this repository. Every extraction and every model input depends on getting this right. Source: `data/weather_state.py` (the implemented logic) plus the extraction modules (`data/hrrr.py`, `data/gfs.py`, `data/pilot_extraction.py`).

## Core timestamp definitions

- **run_time**: when a numerical model's forecast cycle was initialized (e.g. HRRR's 06Z run). This is a *model* concept, not an observation of when data became usable.
- **forecast_hour**: the projection lead time from `run_time`, in hours (e.g. `forecast_hour=6` means "6 hours after this run started").
- **valid_time**: `run_time + forecast_hour` -- the real-world time the forecast value describes.
- **available_time**: when the data actually became retrievable by us. **This is never assumed equal to run_time.** See below.
- **received_at**: not yet implemented. In a future live system, this would be the wall-clock time a value was actually pulled into our own system, which can lag `available_time` further (network/processing delay on our side). The pilot's byte-range extraction fetches historical data long after the fact, so `received_at` is not meaningful for the pilot and is not currently recorded.

## run_time != available_time

A forecast run labeled `06Z` is not usable at `06:00 UTC`. Numerical weather models take real wall-clock time to run and publish; a 06Z HRRR run's first products typically don't appear until 40+ minutes to a few hours later, and GFS (a much larger global model) can take several hours. **Treating `run_time` as if it were `available_time` is a direct lookahead bug** -- it would let a backtest "see" a forecast before it could have existed in reality.

## Availability mechanisms, by source (as currently implemented)

| Source | Mechanism | Confidence |
|---|---|---|
| HRRR | `S3_LAST_MODIFIED_PROXY` | NORMAL |
| GFS | `S3_LAST_MODIFIED_PROXY` | NORMAL |
| GEFS | `S3_LAST_MODIFIED_PROXY` | NORMAL (substantially longer lag than other sources -- ~3.8h at FH3 growing to ~5.3-5.5h at FH240, confirmed genuine progressive release; control member arrives ~1-14 min before the perturbed-member batch. Validated at small-sample scale, full 40-day pilot not yet launched) |
| NBM | `S3_LAST_MODIFIED_PROXY` | NORMAL (~62-63 min observed lag; validated at small sample + benchmark scale, full 40-day pilot not yet launched) |
| ECMWF deterministic | `S3_LAST_MODIFIED_PROXY` | **LOW** -- measured ~514 min (~8.6h) average lag in feasibility testing, far larger than every other source (not yet extracted at pilot scale) |
| Observations (ISD) | `OBSERVATION_TIME_ONLY` | the ISD schema has no independent availability timestamp at all; `observation_time` is used as a proxy under the `proxy` policy only |
| NWS AFD | `ISSUANCE_TIME_ONLY` | the AFD schema's `available_time` column exists but is 100% null; `issuance_time` is used as a proxy under the `proxy` policy only |

### Limitations of the S3 Last-Modified proxy

For HRRR/GFS/GEFS/NBM/ECMWF, "available" is proxied by the S3 object's `Last-Modified` HTTP header, obtained from the same byte-range GET used to fetch the message (`fetch_byte_range()` in `data/hrrr.py` / `data/gfs.py`). This is **not an exact, independently-verifiable trader-observable publication timestamp**. Specifically:

- `Last-Modified` reflects when the *entire underlying GRIB2 file* was last written by NOAA/ECMWF's publishing pipeline, not when the specific message/byte-range we extracted was written. Since one file contains many messages (all variables, all levels), this is necessarily a coarser signal than "this exact value became available."
- Fetching a *different* candidate message from the same file (e.g. choosing the windowed vs. cumulative APCP product) does not change this timestamp at all -- confirmed during the HRRR APCP_1H work, since both live in the same S3 object.
- It is an observed proxy, not a documented guarantee from any provider. Confidence is marked `NORMAL` for HRRR/GFS/GEFS (consistent, plausible lag observed) and `LOW` for ECMWF (unusually large, variable lag observed in a small sample).
- `data/weather_state.py` never treats a null/unresolvable timestamp as "available" -- `eligible_mask()` excludes any row whose availability cannot be resolved, rather than assuming it.

## The no-lookahead rule

`eligible_mask(df, t, policy)` in `data/weather_state.py` is the single enforcement point: a row is only "knowable at time t" if its resolved availability timestamp is `<= t`. This is checked per-row, not per-run -- a run's data trickles in message by message, and only the messages that have actually appeared are eligible at a given `t`. `validate_no_lookahead()` audits every run reference a constructed state exposes (`latest_seen_run`, `latest_usable_run`, `previous_usable_run`) to confirm none of them reference the future relative to `t`.

Two policies exist: `strict` (only sources with a real availability mechanism are used) and `proxy` (also allows `OBSERVATION_TIME_ONLY`/`ISSUANCE_TIME_ONLY` sources, using their own timestamp as a stand-in). The distinction exists because observations and AFD have no independent availability signal at all -- `proxy` is a deliberate, documented compromise, not an oversight.

## Completeness: why a run must be complete before it can be used

A run only produces its first message, then more over time, until (ideally) every forecast hour needed to cover the target day has arrived. `assess_deterministic_run_completeness()` (deterministic sources) and `assess_ensemble_run_completeness()` (GEFS/ECMWF ensemble, which additionally requires every *member* to have arrived) each classify a run's target-day coverage as one of:

- `NOT_APPLICABLE` -- the run's own schedule produces zero required target-day valid times (nothing to assess).
- `COMPLETE` -- every expected target-day point is present. `usable_for_daily_max = True`.
- `INCOMPLETE` -- some expected points are missing. `usable_for_daily_max = False`.

**A run is only used if it is COMPLETE.** A partially-arrived run is never averaged, interpolated, or otherwise substituted for a complete one -- it is excluded entirely from anything model-facing until it either completes or is superseded by a later run.

### latest_seen_run vs. latest_usable_run vs. previous_usable_run

`get_latest_forecast()` and `get_ensemble_state()` expose three distinct run references, and this distinction is deliberate and load-bearing:

- **latest_seen_run**: whatever run has most recently begun arriving, regardless of completeness. Raw provenance only. **Never used to compute a predicted daily max.**
- **latest_usable_run**: the most recent run whose target-day coverage is actually `COMPLETE`. This is the only run a model should ever read a forecast value from.
- **previous_usable_run**: the usable run before that, used to compute forecast revisions (`revision_f = latest_usable - previous_usable`).

`compute_cross_model_diagnostics()` only ever reads from `latest_usable_run` across every source -- an incomplete `latest_seen_run` must never leak into a cross-model comparison, by construction.

### Why partially-arrived runs cannot replace a complete run

Using a partial run's data (e.g. only the first 6 of 24 needed hourly HRRR forecast values) would silently understate or bias the predicted daily maximum, and -- worse -- the bias would be a function of exactly how much of the run happened to arrive before the query time `t`, an artifact of extraction/download timing rather than anything meteorological. Treating a partial run as if it were complete would make backtests non-reproducible (the "seen" data would depend on exactly when the historical extraction happened to run), which directly conflicts with the reproducibility goal of this whole documentation phase.

## The HRRR partial-run failure mode we discovered, concretely

During HRRR extraction, the laptop running the pilot restarted unexpectedly mid-run, interrupting the extraction at roughly 30% completion (see git history: `e409a4a` migrated this partial state to the desktop). Two independent safeguards prevented this interruption from corrupting anything:

1. **Checkpoint-based extraction resume** (`data/pilot_extraction.py`'s `Checkpoint` class): each `(run_time, forecast_hour)` work item's status is durably recorded, and Parquet output is flushed every 25 completed items via atomic temp-file-then-rename. On resume, `checkpoint.is_done()` skips anything already `DONE`, and nothing already flushed to disk is re-fetched or duplicated. This is an *extraction*-level safeguard: it guarantees the eventual dataset is exactly what a from-scratch run would have produced, regardless of how many times the process was interrupted and resumed.
2. **Run-level completeness assessment** (`assess_deterministic_run_completeness`, above): even if extraction had been consumed by a model *during* the interruption (which it was not, in this pilot), any run whose target-day coverage was incomplete at that moment would have been marked `INCOMPLETE` / `usable_for_daily_max=False` and excluded from `latest_usable_run` -- it could not have silently masqueraded as a complete forecast.

These are two different layers of defense (extraction-completeness vs. model-facing-run-completeness) and both matter: the first guarantees the *data* ends up correct once extraction eventually finishes; the second guarantees that even a partially-extracted-so-far state is never *consumed* as if it were complete.

## Historical state reconstruction philosophy

Building `weather_state(t)` for any historical `t` means: take every row whose availability timestamp resolves to `<= t` (via `eligible_mask`), then apply the same completeness/usability rules a live system would have to apply in real time. Nothing about historical reconstruction is allowed to "look up" what actually happened after `t` -- not the eventual observed Tmax, not a later, more-complete forecast run, not a forecast revision that hadn't happened yet. This is what makes point-in-time-correct backtesting possible at all.
