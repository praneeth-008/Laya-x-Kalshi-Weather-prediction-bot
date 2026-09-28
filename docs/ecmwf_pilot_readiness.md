# ECMWF Deterministic Pilot Readiness

STATUS: PRODUCTION-READINESS VALIDATED (small representative sample + benchmark only; full 40-day pilot NOT launched).

All findings below were independently re-verified against the live archive on 2026-09-28 across 5 selected-pilot dates spanning the full range (2025-01-02, 2025-03-19, 2025-06-15, 2025-07-22, 2025-08-24), both run hours, and forecast hours 0-360. Prior single-day (2025-07-01) feasibility findings (`scripts/test_ecmwf_nyc_feasibility.py`, `data/ecmwf.py`'s original docstring) were treated as hypotheses to confirm, not facts to assume.

## 1. Authoritative source/bucket

`s3://ecmwf-forecasts` (AWS Open Data registry, `arn:aws:s3:::ecmwf-forecasts`), public HTTPS, `eu-central-1`. Confirmed the same bucket used in the prior feasibility test remains live and accessible. As already documented in `data/ecmwf.py`'s module docstring: this contradicts ECMWF's own stated "~2-3 day retention" policy for Open Data, and should not be treated as a guaranteed capability -- this caveat is carried forward unchanged from the feasibility phase.

## 2. Deterministic stream

`oper` (HRES, suffix `fc`). Ensemble (`enfo`) exists alongside it but is explicitly OUT OF SCOPE for this phase, per instruction.

## 3. Archive/path regimes

Independently probed via direct HEAD requests across 21 dates spanning 2023-06 through 2026-09. Exact boundaries (more precise than the prior "approximately" estimates):

| Regime | Path | Confirmed range |
|---|---|---|
| `0p4-beta` | `{d}/00z/0p4-beta/oper/...` | through 2024-01-31 (exclusively) |
| `0p4-beta` + bare `0p25` (coexist) | `{d}/00z/0p25/oper/...` also present | 2024-02-01 through 2024-02-28 |
| `ifs/0p25` (modern, current) | `{d}/00z/ifs/0p25/oper/...` | **2024-02-29 onward**, confirmed stable through 2026-09-27 |

`data/ecmwf.py`'s `grib_key()` only implements the modern `ifs/0p25` regime. Every one of this project's 40 selected pilot days (2025-01-02 through 2025-08-24) falls safely and exclusively inside this single regime -- no mixed-regime handling is needed for this pilot. See section 37 for backfill implications of the older regimes.

## 4. Confirmed usable historical date range

For the modern `ifs/0p25` regime: 2024-02-29 through at least 2026-09-27 (today, confirmed live). This project's entire 40-day pilot date range is safely within this window. Older regimes (`0p4-beta`, bare `0p25`) exist back to at least mid-2023 but use different path structures, and their index format/grid/variable compatibility with this project's current parsing code was NOT independently verified (out of scope for this phase -- see section 37).

## 5. Run cadence

`oper` confirmed present ONLY at 00Z/12Z, across all 5 dates x both run hours tested (10 combinations). 06Z/18Z confirmed absent for `oper` at every date tested. (Incidental finding, not used here: `enfo`, the ensemble stream, IS present at all 4 run hours 00/06/12/18Z -- relevant for a future ensemble-readiness phase, not this one.)

## 6. FH schedule/horizon

3-hourly F0-F144, then 6-hourly F150-F360 -- confirmed identical at both run hours (00Z/12Z) and across multiple dates. `data/weather_state.py::_forecast_hour_candidates("ecmwf_deterministic")` already implements exactly this schedule (`range(0,145,3)+range(150,361,6)`) -- no fix needed, confirmed correct against the live archive.

## 7. F0 behavior

- Instant fields (2t, 2d, 10u, 10v, sp) ARE present and meaningful at F0.
- `tp` is present at F0 but structurally DEGENERATE: `stepType=accum`, `startStep=endStep=0` (zero-width window, value=0.0) -- not a real accumulation, explicitly skipped.
- `mx2t3`/`mn2t3` at F0 are ALSO present in the index but are DEGENERATE PLACEHOLDERS: declared window `[0,3]` (matching the eventual real F3 message) but decoded value = **0.0 Kelvin** (absolute zero -- a physically impossible temperature, an unambiguous fill/placeholder marker). Explicitly skipped; the first REAL mx2t3/mn2t3 value appears in the F3 file's own message.
- `ssrd` at F0 was not separately probed for degeneracy but is skipped at F0 by the same `step > 0` guard as `tp` (consistent with it also being an accumulated field, same convention).

## 8. Grid specification

`regular_ll`, `Ni=1440`, `Nj=721`, 0.25deg resolution -- same CELL SIZE as GFS/GEFS.

## 9. Grid fingerprint(s)

`md5GridSection = 265781b4edc06425746b46a5775244eb` -- confirmed **DIFFERENT** from GFS's/GEFS's `45f3a4a8af23f33a77ab669d0fa1d813`, despite identical 0.25deg resolution. Root cause: ECMWF's `longitudeOfFirstGridPointInDegrees=180.0` vs. GFS/GEFS's `0.0` -- a different global grid origin/alignment convention. Independently confirmed via direct decode, not assumed from matching resolution. Only one regime's grid was checked (the modern `ifs/0p25` regime, which is the only one this pilot needs); older regimes' grids were not verified (out of scope).

## 10. Central Park distance

Patch center grid cell at (40.75N, -74.00) in -180/180 convention, same as GFS/GEFS (same physical cell size at this latitude).

## 11. Patch behavior

The +/-0.03deg 3x3 patch **fully collapses to 1 distinct grid cell** (identical numeric behavior to GFS/GEFS since the cell size is the same) -- max observed distance from a patch point to that cell: 8.346 km. Distance threshold: 20.0 km, independently re-derived from ECMWF's own confirmed geometry (same formula, same result as GFS/GEFS because the cell size coincidentally matches -- not inherited without verification). Cached-vs-uncached equivalence confirmed bit-exact on a direct fresh-decode-then-cache-hit test.

## 12. Variable table

| Variable | Native param | Level | Temporal kind | Notes |
|---|---|---|---|---|
| 2m temperature | `2t` | sfc | instant | present every step incl. F0 |
| 2m dewpoint | `2d` | sfc | instant | present every step incl. F0 |
| 10m U wind | `10u` | sfc | instant | present every step incl. F0 |
| 10m V wind | `10v` | sfc | instant | present every step incl. F0 |
| surface pressure | `sp` | sfc | instant | present every step incl. F0 |
| total precipitation | `tp` | sfc | accum, cumulative-since-start | degenerate at F0, skipped |
| solar radiation down | `ssrd` | sfc | accum, cumulative-since-start | skipped at F0 (same convention as tp) |
| 3h max 2m temp | `mx2t3` | sfc | max, rolling 3h | F3-F144 only; F0 degenerate |
| 3h min 2m temp | `mn2t3` | sfc | min, rolling 3h | F3-F144 only; F0 degenerate |
| 6h max 2m temp | `mx2t6` | sfc | max, rolling 6h | F150-F360 only (replaces mx2t3) |
| 6h min 2m temp | `mn2t6` | sfc | min, rolling 6h | F150-F360 only (replaces mn2t3) |

**No native 2m relative humidity or cloud-cover field exists** in this product -- exhaustively confirmed by listing every distinct `param` in the index (`r` only appears at pressure levels, `typeOfLevel` never `sfc`; `tcc`/`hcc`/`mcc`/`lcc`/`cc` all absent at every level). Not derived here (would require a computed transform from 2t/2d for RH, never implemented for any other source in this project either, per the established "prefer native fields" convention). This is a genuine structural difference from every NOAA source in this project (all of which have native 2m RH), documented rather than worked around.

## 13. Duplicate-product findings

**None.** Every retained variable has exactly one candidate per (param, levtype) at every forecast hour tested -- confirmed by an explicit duplicate scan (excluding known multi-pressure-level fields like `t`/`u`/`v`/`q`/`r`/`gh`/`vo`/`d`/`w`, which legitimately repeat once per pressure level and are not used by this project). `find_message()`'s exact-match lookup is safe.

## 14. 2t semantics

Simple instantaneous snapshot at each forecast hour's valid time, `stepType=instant`, `startStep=endStep=step`. No ambiguity, matches HRRR/GFS/NBM/GEFS's instantaneous temperature convention.

## 15. mx2t3 semantics

Clean rolling window: `startStep=endStep-3` at every forecast hour in F3-F144 (e.g. F24: `[21,24]`). **Critical finding, caught during the validation sample and fixed before declaring PASS**: at F150 and beyond, `mx2t3` is silently ABSENT and REPLACED by `mx2t6` (rolling 6h window, `startStep=endStep-6`, e.g. F240: `[234,240]`). The original extraction script naively requested only `mx2t3` throughout and silently produced 0 rows for 3 of 10 non-F0 validation items (exactly the three at F150/F240/F360) before this was caught by inspecting per-item variable coverage. Fixed by selecting `mx2t3`/`mn2t3` for `step<=144` and `mx2t6`/`mn2t6` for `step>144`.

## 16. mn2t3 semantics

Symmetric to mx2t3 in every respect (window, F0 degeneracy, F150 regime switch to mn2t6).

## 17. tp semantics

**Pure cumulative-since-run-start at every forecast hour tested, including F360** (`startStep` always `0`) -- confirmed across the FULL 360h horizon, unlike GEFS's 6-hour-reset convention. This is the same situation HRRR's raw APCP was in before its APCP_1H backfill: a windowed/"recent precipitation" signal would require differencing consecutive `tp` values ourselves. Not implemented in this validation phase (native field only); flagged as a future design decision analogous to HRRR's backfill, should a windowed ECMWF precipitation feature ever be wanted.

## 18. Other retained-variable semantics

`ssrd` (solar radiation downward, the DSWRF-equivalent) follows the identical cumulative-since-start convention as `tp` (confirmed `startStep=0` at F24/F144/F360) -- same treatment, skipped at F0.

## 19. Availability findings (the central investigation of this phase)

**All forecast hours of a single run (F0 through F360, the full 15-day horizon) share Last-Modified timestamps within a 37-59 SECOND window**, confirmed across 8 different (date, run_hour) combinations. Lag from nominal `run_time` to F0's Last-Modified is essentially **EXACTLY 514.0 minutes (~8.57h)** in every case tested -- growing by under 1 minute all the way to F360. This is fundamentally different from every NOAA source in this project (HRRR/GFS/NBM/GEFS), where lag genuinely grows substantially with forecast hour, reflecting real progressive computation and release.

## 20. Last-Modified interpretation

The extreme precision and forecast-hour-independence of this ~514-minute figure (reproducible to the minute across 4 different calendar dates, both run hours) is best explained as a **single bulk archive-sync event**: the entire run's complete file set becomes available on this specific AWS mirror nearly atomically, roughly 8.57 hours after nominal run time. This number is in the right ballpark of ECMWF's publicly known HRES dissemination timelines (full-suite dissemination completing several hours after run time), lending some plausibility that it tracks genuine model+QC+dissemination completion -- but this was not independently verified against an official ECMWF-published dissemination schedule (would require an external lookup beyond this investigation's scope), and the complete absence of any forecast-hour correlation (F0 and F360 becoming visible within the same minute) means this specific AWS mirror's timestamp cannot distinguish "when F0 became available" from "when F360 became available" the way a genuine per-product dissemination signal would.

## 21. Availability-confidence classification

**LOW CONFIDENCE** (consistent with, and now much more precisely evidenced than, the pre-existing tag already present in `data/weather_state.py`'s module docstring). Not LOW because the signal is noisy -- it is in fact remarkably precise and reproducible -- but because:
1. it reflects a bulk mirror-sync event bundling all 360 forecast hours into one moment, discarding any real progressive-dissemination structure a live trader might have actually observed;
2. it is a third-party AWS mirror, not ECMWF's officially documented/guaranteed retention or timing behavior (the retention itself already contradicts ECMWF's stated policy, per `data/ecmwf.py`'s existing docstring);
3. it cannot be decomposed to genuine per-forecast-hour granularity.

It IS, however, a highly reliable **conservative floor**: by ~514 minutes after run_time, the entire run is reliably and reproducibly present, with no observed exceptions across every date/run-hour tested.

## 22. Progressive-arrival findings

Essentially NONE at the forecast-hour level within this archive -- see sections 19-21. The only genuine "partial arrival" state possible is a narrow race WITHIN the ~37-59 second bulk-sync burst itself (a few objects already written, the rest of the same burst not quite yet) -- modeled explicitly in the completeness tests (section 24).

## 23. Proposed atomic completeness rule

Reuse the existing deterministic completeness machinery unchanged: `assess_deterministic_run_completeness` + `get_latest_forecast` (the same functions already used by HRRR/GFS/NBM) -- `data/weather_state.py` already routes `ecmwf_deterministic` through these, no ensemble-style member tracking needed (single deterministic stream, no members). A run is `COMPLETE`/`usable_for_daily_max=True` only when every expected target-day valid time is present; `latest_usable_run` never mixes forecast hours from different runs (each `run_metadata()` call operates on rows filtered to a single `run_time`).

## 24. Completeness/no-lookahead test results

10 targeted tests (`test_ecmwf_completeness.py`, scratch, not committed) against the existing `assess_deterministic_run_completeness`/`get_latest_forecast` logic, using availability timestamps modeled on the REAL confirmed bulk-sync pattern (not a generic progressive-arrival assumption):
- Schedule sanity: matches the independently-confirmed 3h-to-144h-then-6h-to-360h archive schedule exactly.
- A. Genuinely complete run (all rows sharing one bulk-sync timestamp) -> `COMPLETE`, `usable_for_daily_max=True`.
- B. Run missing one required valid time -> `INCOMPLETE`.
- C. A newer run mid-burst (only 2 of its objects synced so far, modeled as a race within the same ~45-second burst) does NOT replace an older complete run as `latest_usable_run`.
- D. `latest_seen_run` correctly advances to the newer run (since some of its rows ARE eligible) while `latest_usable_run` stays on the older complete run.
- E. Atomic switch: once the newer run's full burst completes, it correctly becomes `latest_usable_run`.
- F. No forecast-hour mixing: the usable run's rows all share exactly one `run_time`.
- G. No-lookahead: every exposed run's `available_time <= query_t` in both states tested.

**10/10 passed.** No fix to `data/weather_state.py` was needed. An initial test attempt using a generic "mid-sync, hours-long partial arrival" scenario (copying the GEFS/NBM test pattern) correctly FAILED -- not because of a code bug, but because that scenario doesn't reflect how this specific archive actually behaves (all-or-nothing atomic bursts, not hours-long progressive arrival); the test was corrected to model the real confirmed behavior instead, at which point all cases passed.

## 25. Structural-missingness rules

- **PRESENT**: instant fields at any forecast hour; `tp`/`ssrd`/`mx2t3 or mx2t6`/`mn2t3 or mn2t6` at any forecast hour > 0.
- **STRUCTURALLY_NOT_APPLICABLE**: `tp`/`ssrd`/`mx2t3`/`mn2t3` at forecast_hour=0 (degenerate zero-width window or 0K placeholder -- not fetched, never fabricated as zero); `mx2t3`/`mn2t3` beyond F144 (replaced by mx2t6/mn2t6, not "missing"); `mx2t6`/`mn2t6` at or before F144 (replaced by mx2t3/mn2t3); native 2m RH and cloud-cover for the entire product (no such field exists at any forecast hour).
- **NOT_YET_AVAILABLE**: any row whose resolved `available_time > query_t` under the no-lookahead policy -- given the bulk-sync pattern, this state is binary per-run in practice (a run's entire required set becomes available within the same ~40-60 second window).
- **MISSING_UNEXPECTEDLY**: not observed in this validation phase (0 unexplained gaps across every date/variable/forecast-hour combination tested).

## 26. State-builder findings

No changes made to `data/weather_state.py`. Its `ecmwf_deterministic` handling (schedule via `_forecast_hour_candidates`, completeness via `assess_deterministic_run_completeness`/`get_latest_forecast`, `availability_confidence="LOW"` tagging already present in the module's docstring) already matches every validated finding above.

## 27. Processed-data schema

Every row carries (see `scripts/pilot_phase7_ecmwf.py::extract_one_run_step`): `source, model, stream, run_time, valid_time, available_time, availability_time_type, availability_confidence, forecast_hour, variable, temporal_stat, temporal_window_hours, requested_lat, requested_lon, grid_lat, grid_lon, distance_km, is_primary_central_park_grid, value, units, value_f, archive_regime, source_object`.

## 28. Work-item design

**`(run_time, forecast_hour/step)`** -- no member dimension (deterministic, single stream). One index fetch + up to 9 variable byte-range GETs per work item, matching every other source's per-item efficiency.

## 29. Cached-vs-uncached results

Confirmed bit-exact (identical grid_lat/grid_lon/value) on a direct fresh-decode-then-cache-hit test; 1 stable fingerprint (`md5:265781b4edc06425746b46a5775244eb`) observed throughout.

## 30. Multiprocessing benchmark

12 representative items (5 dates, both run hours, F0, short/medium/long horizons including the F144/F150 regime boundary), tested at 4/8/12/16/20 workers:

| workers | items/sec | errors |
|---|---|---|
| 4 | 0.365 | 0 |
| 8 | 0.448 | 0 |
| 12 | 0.750 | 0 |
| 16 | 0.939, then 0.461 (confirmation) | 0 |
| 20 | 0.792 | 0 |

Zero errors at every worker count, but **substantially higher run-to-run variance than any other source in this project** (16 workers ranged from 0.46 to 0.94 items/sec across trials) -- consistent with `data/ecmwf.py`'s own documented finding that this bucket rate-limits more aggressively than NOAA's, with backoff/retry absorbing the throttling transparently at variable cost rather than surfacing as errors. 20 workers showed an early sign of a throughput dip relative to 16, suggesting we are near this bucket's practical concurrency ceiling.

## 31. Proposed worker count

**16.** Best single-trial throughput observed, and avoids the dip seen at 20; the high variance itself (not the exact worker count) is the dominant characteristic of this source's performance profile.

## 32. Checkpoint/resume results

Confirmed working via the real `Checkpoint`/`run_concurrent` path on a 12-item validation sample: 12/12 DONE on first run, 0/12 pending (correctly all skipped) on an immediate re-run.

## 33. Temp-file status

0 leaks -- uses the same shared in-memory `decode_message_multi_point` as every other source.

## 34. Estimated 40-day work-item count

**904** `(run_time, forecast_hour)` items -- independently computed: 144 run_times (40 selected days + lead-in, x2 run hours) x average ~6.3 required forecast hours/run. By far the smallest pilot of any source in this project (vs. GEFS's 62,744, NBM's 31,578, HRRR's 20,836, GFS's 5,836), reflecting ECMWF's coarser 2-runs/day cadence and 3-hourly-then-6-hourly step schedule.

## 35. Estimated remote bytes

From the benchmark: ~74.2MB / 12 items ~= ~6.2MB/item average. 904 x ~6.2MB ~= **~5.6 GB** estimated remote bytes read for the full 40-day pilot.

## 36. Estimated local storage

From the validation sample (900 rows from 12 items, ~75 rows/item average): 904 x ~75 rows ~= ~67,800 rows; at other sources' observed ~12 bytes/row (Parquet-compressed) ~= **~0.8 MB** estimated local storage -- by far the smallest of any source in this project.

## 37. Five-year-backfill implications

ECMWF's `ifs/0p25` regime (this project's current, validated implementation) only covers 2024-02-29 onward -- **less than 2 years** of history as of this pilot, a materially SHORTER window than HRRR/GFS/NBM/GEFS's full ~40-day-representative-sample-over-a-multi-year range (those sources' archives extend much further back). For a future ~5-year historical backfill:
- **Blocks from 2024-02-29 onward**: ECMWF can participate cleanly using the current, validated code.
- **Blocks before 2024-02-29**: ECMWF would require separate, NOT-YET-IMPLEMENTED path/parsing logic for the `0p4-beta`/bare-`0p25` regimes (different path structure; their index format, grid, and variable-naming compatibility with this project's current parser were NOT verified in this phase). This is a real, separate engineering + validation effort, not a simple path-template change.
- **Recommendation**: do not force ECMWF into every historical block merely to match HRRR/GFS/NBM/GEFS's longer reach. A future comparison between a LONG-HISTORY model without ECMWF and a SHORTER, richer model with ECMWF (as the user's brief anticipates) is a legitimate and probably necessary framing -- not implemented in this phase, per explicit instruction.

## 38. Files changed

- `data/ecmwf.py`: added `MAX_PATCH_DISTANCE_KM=20.0`, `REGIME_IFS_0P25_START`, `temporal_metadata()` helper.
- `scripts/pilot_phase7_ecmwf.py`: new production-readiness validation script (launch guard in place, mirrors `pilot_phase3_hrrr.py`/.../`pilot_phase6_gefs.py`'s architecture).
- `docs/ecmwf_pilot_readiness.md`: this file (new).
- `docs/data_sources.md`, `docs/variable_semantics.md`, `docs/point_in_time.md`, `docs/live_information_state.md`, `docs/decisions.md`: updated (see each file's ECMWF section/entries).

No changes to `data/weather_state.py` (existing ECMWF deterministic logic validated as correct, no fix needed), no changes to frozen HRRR/GFS/NBM/GEFS processed data.

## Unresolved issues carried forward

1. No native 2m relative humidity or cloud-cover field exists in this product at all -- a genuine, permanent structural gap versus every NOAA source in this project (not a bug to fix).
2. `tp`/`ssrd` are pure cumulative-since-start throughout the full 360h horizon -- a windowed "recent precipitation/radiation" signal would require differencing consecutive values ourselves (same situation HRRR's APCP was in pre-backfill); not implemented in this phase.
3. Full 40-day pilot not yet launched -- all figures in sections 34-36 are estimates from a 12-item benchmark sample, not measurements.
4. The benchmark showed unusually high run-to-run throughput variance (0.37-0.94 items/sec at the same worker count) attributable to this bucket's aggressive, variable rate-limiting -- worker count is a secondary factor compared to this inherent variance; actual full-pilot runtime should be expected to vary correspondingly.
5. Availability confidence remains LOW by design (see section 21) -- this is a property of the archive, not something further investigation in this phase could resolve, short of contacting ECMWF directly about their official dissemination schedule (out of scope).
6. Older archive regimes (`0p4-beta`, bare `0p25`, pre-2024-02-29) were boundary-dated but not functionally validated (index format/grid/variables) -- required only if/when the five-year backfill effort (section 37) is undertaken.
