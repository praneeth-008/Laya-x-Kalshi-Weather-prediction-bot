# Live Information State: Canonical Architectural Rule

## ATOMIC WITHIN SOURCE, ASYNCHRONOUS ACROSS SOURCES

This is the canonical rule governing how `data/weather_state.py` composes a point-in-time model-facing state from multiple independent weather sources.

- **Atomic within source**: a single source's forecast run either contributes its COMPLETE required target-day information set, or it contributes nothing to the model-facing state. A partially-arrived run never replaces a previously complete run, is never mixed with another run's forecast hours, and is never treated as complete merely because some of its data has appeared.
- **Asynchronous across sources**: different sources (HRRR, GFS, NBM, GEFS, ECMWF) are not required to be at the same "freshness" as one another at any given query time `t`. Each source's `latest_usable_run` is computed independently, using that source's own schedule, own completeness rule, and own availability proxy. The model-facing state at time `t` is simply the collection of each source's own independently-computed state -- there is no cross-source synchronization requirement.

This rule is implemented per-source, not as one shared mechanism, because each source's atomicity boundary differs (see below).

## latest_seen_run vs. latest_usable_run

Every source-state function in `data/weather_state.py` (`get_latest_forecast` for deterministic sources, `get_ensemble_state` for ensemble sources) exposes three run references:

- **`latest_seen_run`**: the newest run for which ANY eligible (no-lookahead) row exists, regardless of completeness. This advances as soon as a source's newest run starts producing any visible data.
- **`latest_usable_run`**: the newest run that is fully `COMPLETE` for the required target-day information set. Only this run may provide canonical model-facing features (e.g. `predictions["hrrr_predicted_max_f"]`).
- **`previous_usable_run`**: the next-most-recent `COMPLETE` run before `latest_usable_run`, used to compute revisions (e.g. `revision["mean_daily_max_f"]`).

A source can have `latest_seen_run != latest_usable_run` for as long as its newest run remains incomplete -- this is the expected, correct state during a run's arrival window, not an error condition.

## Completeness rules, per source

| Source | Completeness dimension(s) | Function |
|---|---|---|
| HRRR, GFS, NBM, ECMWF deterministic | forecast-hour coverage only | `assess_deterministic_run_completeness` |
| GEFS, ECMWF ensemble | forecast-hour coverage AND ensemble-member coverage (conservative: ALL expected members must have ALL expected valid times) | `assess_ensemble_run_completeness` |

"Complete" always means complete for the REQUIRED TARGET-DAY information set (`expected_target_day_valid_times`), not the source's full maximum forecast horizon -- a run whose schedule doesn't reach far enough to cover the remainder of the target day is `NOT_APPLICABLE`, not `INCOMPLETE`.

## Structural missingness (never collapsed to "zero" or a single flat "missing")

Every source in this project distinguishes:

- **PRESENT**: the value was fetched and decoded from a real forecast message.
- **NOT_YET_AVAILABLE**: the row's resolved `available_time > query_t` under the no-lookahead policy -- it exists in principle but this state-at-time-t cannot see it yet.
- **STRUCTURALLY_NOT_APPLICABLE**: the source's own product structure does not produce this value at this forecast hour at all (e.g. GFS/GEFS/ECMWF's precipitation fields at forecast_hour=0; NBM's forecast_hour=0 entirely; ECMWF's `mx2t3`/`mn2t3` beyond F144, replaced by `mx2t6`/`mn2t6`; ECMWF's native 2m relative humidity and cloud cover, which do not exist in the product at any forecast hour).
- **MISSING_UNEXPECTEDLY**: a genuine, unexplained gap -- investigated and never silently treated as any of the above three when found.

`STRUCTURALLY_NOT_APPLICABLE` values are never fabricated as zero and never counted against a run's completeness assessment if they were never expected in the first place (`expected_target_day_valid_times` only enumerates forecast hours the source's own validated schedule actually produces).

## No-lookahead enforcement

`eligible_mask(df, t, policy)` is the single enforcement point across every source: a row is knowable at time `t` only if its resolved availability timestamp is `<= t`. This is checked per-row, and `validate_no_lookahead()` independently re-audits every run reference (`latest_seen_run`, `latest_usable_run`, `previous_usable_run`) a constructed state exposes.

## Source-specific notes on how atomicity manifests

- **HRRR/GFS/NBM**: genuine progressive within-run arrival (each forecast hour's byte-range fetch returns its own `Last-Modified`), lag grows measurably with forecast hour. Atomicity is enforced logically by the completeness functions above, not by the archive's own behavior.
- **GEFS**: genuine progressive arrival across BOTH forecast hours and the 31-member dimension -- the conservative complete-member rule (`assess_ensemble_run_completeness`) exists specifically because member arrival is real and independently observable (control member arrives measurably ahead of the perturbed-member batch; see `ecmwf_pilot_readiness.md`'s deterministic counterpart, `gefs_pilot_readiness.md`, for the full member-timing investigation).
- **ECMWF deterministic**: uniquely among this project's sources, the archive itself enforces near-atomicity mechanically -- an entire run's full forecast-hour set (F0 through F360) becomes available within a 37-59 second window, at an almost exactly reproducible ~514-minute lag from run_time (see `point_in_time.md` and `ecmwf_pilot_readiness.md` sections 19-22). This means `latest_seen_run` and `latest_usable_run` will in practice transition together almost immediately once a run's data appears at all -- the logical machinery (`get_latest_forecast`) still protects correctly against the narrow race window possible WITHIN that ~40-60 second burst (confirmed by 10 targeted tests, see `ecmwf_pilot_readiness.md` section 24), but this is a much narrower window than any NOAA source exhibits. ECMWF's availability is additionally tagged `availability_confidence="LOW"` (not because the signal is noisy, but because it cannot distinguish per-forecast-hour dissemination timing at all -- see `point_in_time.md`).

## Historical archive limitations affecting long-term coverage

ECMWF's currently-implemented archive path (`ifs/0p25`) only covers 2024-02-29 onward -- a materially shorter window than HRRR/GFS/NBM/GEFS's reach. This is a genuine, permanent limitation of the validated implementation, not a temporary gap: older ECMWF archive regimes exist but use different path structures, index formats, and possibly different grid/variable conventions that have not been independently verified. See `ecmwf_pilot_readiness.md` section 37 for the resulting five-year-backfill implications (ECMWF may need to participate in only the more recent blocks of a future long-history backfill, with a long-history-without-ECMWF vs. shorter-richer-with-ECMWF comparison as a future, not-yet-implemented experiment).
