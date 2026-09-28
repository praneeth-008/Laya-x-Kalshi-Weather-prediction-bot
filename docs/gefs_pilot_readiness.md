# GEFS Pilot Readiness

STATUS: PRODUCTION-READINESS VALIDATED (small representative sample + benchmark only; full 40-day pilot NOT launched).

All findings below were independently re-verified against the live archive on 2026-09-28 across 5 selected-pilot dates spanning the full range (2025-01-02, 2025-03-19, 2025-06-15, 2025-07-22, 2025-08-24), all 4 run hours, control + several perturbed members, and forecast hours 0-240. Prior single-day (2025-07-01) feasibility findings (`scripts/test_gefs_nyc_feasibility.py`, `data/gefs.py`'s original docstring) were treated as hypotheses to confirm, not facts to assume -- see each section for what changed vs. what was confirmed as-is.

## 1. Archive / product structure

- Source: `s3://noaa-gefs-pds`, public, unauthenticated HTTPS. Confirmed live and unchanged.
- Path pattern: `gefs.{YYYYMMDD}/{HH}/atmos/pgrb2sp25/{member}.t{HH}z.pgrb2s.0p25.f{FFF}` (+ `.idx`).
- Product: `pgrb2sp25` (0.25deg compact "small" subset) -- confirmed to contain every variable needed (TMP/DPT/RH/UGRD/VGRD/PRES/APCP/TCDC/DSWRF/TMAX/TMIN, 26-38 messages depending on forecast hour).
- Run cadence: 4 runs/day (00/06/12/18Z), confirmed identical across all 5 dates tested.

## 2. Member structure

- 31 members: `gec00` (control) + `gep01`-`gep30` (30 perturbed), confirmed present and stable across all 5 dates x 2 run hours tested (10 combinations, 310 direct HTTP HEAD probes, 31/31 present every time).
- **Correction to my own investigation tooling, not the archive**: an initial check using S3's `ListObjectsV2` bucket listing appeared to show only 7/31 members present -- this was purely an artifact of AWS's 1000-key-per-response truncation (the listing was cut off alphabetically before reaching `gep06`+), not a real gap. Direct per-member HTTP HEAD probes (the same lookup method the real extraction pipeline uses -- deterministic key construction, never directory listing) confirm all 31 members present. Documented here as a caught-and-corrected methodology error, not an archive finding.
- `gep31`: confirmed absent (404) at every date tested.
- `geavg`/`gespr` (NOAA-precomputed ensemble mean/spread): confirmed present but correctly excluded from `MEMBERS` -- they are derived statistics, not members.
- Member identity is independently cross-checkable from GRIB-internal metadata: `perturbationNumber` (gec00=0, gepNN=N) and `typeOfEnsembleForecast` (1=control, 3=perturbed) both match the filename-derived member id exactly -- confirmed via direct decode of 3 members.

## 3. F0 behavior

- Instantaneous fields (TMP, DPT, RH, UGRD, VGRD, PRES) ARE present and meaningful at F0, using `forecast_desc='anl'` (analysis-time label, same convention as GFS's own F0 fix from the GFS pilot phase).
- APCP, TCDC, DSWRF are **structurally absent at F0 entirely** -- 0 idx entries, confirmed at every date/member tested. Not a selection ambiguity; there is no message to select.
- TMAX/TMIN at F0 use a **degenerate zero-width-window** format (`'0-0 day max fcst'` / `'0-0 day min fcst'`, `lengthOfTimeRange=0`) -- decodes to a real, non-null value equal to the instantaneous F0 TMP reading, NOT a genuine period max/min. Production code (`scripts/pilot_phase6_gefs.py`) explicitly skips TMAX/TMIN at `forecast_hour=0` rather than fetch this degenerate case, consistent with how HRRR's/GFS's other degenerate F0 accumulation fields are handled.

## 4. Grid specification / fingerprint

- `gridType=regular_ll`, `Ni=1440`, `Nj=721`, `0.25deg` resolution, confirmed via direct decode of a real message.
- `md5GridSection = 45f3a4a8af23f33a77ab669d0fa1d813` -- **identical to GFS's own grid fingerprint**. This was independently confirmed (not assumed from "both use 0.25deg"): both sources genuinely share the exact same NCEP global 0.25deg lat/lon grid definition.

## 5. Central Park distance / patch behavior

- The +/-0.03deg 3x3 patch **fully collapses to 1 distinct grid cell** (same behavior as GFS, different from HRRR/NBM's finer grids) -- confirmed via direct multi-point decode: all 9 requested patch points resolve to the identical grid cell (40.75N, -74.00 in -180/180 convention).
- Max observed distance from a patch point to that cell: 8.35 km.
- **Distance threshold**: 20.0 km, independently derived from GEFS's own grid geometry (`data.gefs.MAX_PATCH_DISTANCE_KM`): Dy=27.75km, Dx=27.75km*cos(40.7deg)=21.0km at NYC's latitude, worst-case distance from any point in a cell to its center = 0.5*sqrt(Dx^2+Dy^2) ~= 17.4km, 20km chosen. This equals GFS's chosen value, but only because the grids are genuinely identical -- not inherited without verification.
- Cached-vs-uncached equivalence: confirmed bit-exact (identical grid_lat/grid_lon/value) on a direct fresh-decode-then-cache-hit test.

## 6. Variable table

| Variable | Level | Temporal kind | Notes |
|---|---|---|---|
| TMP | 2 m above ground | instant | K; present every FH incl. F0 (`anl`) |
| DPT | 2 m above ground | instant | present every FH incl. F0 |
| RH | 2 m above ground | instant | present every FH incl. F0 |
| UGRD | 10 m above ground | instant | present every FH incl. F0 |
| VGRD | 10 m above ground | instant | present every FH incl. F0 |
| PRES | surface | instant | present every FH incl. F0 |
| APCP | surface | accum | absent at F0; window varies by FH (section 7) |
| TCDC | entire atmosphere | average | absent at F0; window varies by FH |
| DSWRF | surface | average | absent at F0; window varies by FH |
| TMAX | 2 m above ground | max (period) | F0 degenerate, skipped; window varies by FH |
| TMIN | 2 m above ground | min (period) | F0 degenerate, skipped; window varies by FH |

**No duplicate products found for any variable at any forecast hour tested** -- unlike HRRR/GFS/NBM's APCP, GEFS's `pgrb2sp25` product has exactly ONE candidate per (variable, level) at every forecast hour. `find_message()`'s exact-match lookup is therefore safe (not idx-ordering-dependent, since there is only ever one candidate); `parse_forecast_desc()` is still used on every message to explicitly record the observed temporal_stat/window, never assumed.

## 7. Duplicate-product / APCP / cloud-radiation semantics (shared window rule)

APCP, TCDC, DSWRF, TMAX, and TMIN all follow the **same accumulation/average window convention**, empirically traced across FH 3 through 240 (21 forecast hours tested, 2 members, 2 run hours):

- For FH <= 6: window = `[0, FH]` (cumulative since run start).
- For FH > 6: window resets at every 6-hour synoptic mark: `window_start = 6 * floor((FH-1)/6)`, `window_end = FH` -- giving alternating 3-hour and 6-hour windows (e.g. F009=[6,9], F012=[6,12], F015=[12,15], F018=[12,18], ...).

This formula is documentation/validation only -- production code (`data.gefs.parse_forecast_desc`) always parses the actual observed `forecast_desc` text, never computes the window from the formula. Confirmed identical behavior across every member and run hour tested (no member-specific or run-hour-specific deviation).

Do not assume GFS's APCP semantics (shortest-recent-window selected from 2 duplicate candidates) apply here -- GEFS has no duplicates to select between; the single candidate's window itself varies instead.

## 8. Availability findings (critical section)

- Overall lag from `run_time` is substantially larger than HRRR/GFS/NBM: ~227 min (~3.8h) at FH3, growing monotonically to ~317-331 min (~5.3-5.5h) at FH240 (8 sampled members, 1 representative run). Monotonic growth with forecast hour confirms genuine progressive within-run release, not a single batch write.
- **Progressive-arrival / member-staggering finding**: the control member (`gec00`) systematically arrives ~1-14 minutes EARLIER than the perturbed-member batch at every forecast hour tested. The 30 perturbed members arrive within a tight cluster of each other (spread grows from ~2 min at FH3 to ~14 min at FH240) -- consistent with NOAA writing the control member first, then all perturbed members in one batch shortly after.
- Per-row `available_time` (from each row's own byte-range GET's `Last-Modified` header) is preserved at full member/forecast-hour granularity in the processed schema -- **never collapsed to one artificial run-level timestamp**, per the explicit requirement. This required no special design: it is the natural consequence of each `emit()` call fetching its own byte range independently, exactly as HRRR/GFS/NBM already do.
- S3 Last-Modified remains a PROXY (not an exact publication time), same caveat as every other source in this project.

## 9. Proposed completeness rule

**Conservative complete-ensemble rule** (already implemented in `data/weather_state.py::assess_ensemble_run_completeness`, built in an earlier architecture phase and independently re-validated here, not modified): a run is `COMPLETE`/`usable_for_daily_max=True` only when ALL 31 expected members have ALL expected target-day valid times present. A run missing even one member, or missing one valid time for even one member, is `INCOMPLETE`. This matches the user's explicit preference for the pilot ("prefer the conservative complete-member rule unless the source structure gives a compelling reason otherwise") -- no compelling reason to relax it was found.

## 10. Completeness / no-lookahead test results

14 targeted tests (`test_gefs_completeness.py`, scratch, not committed) against the existing `assess_ensemble_run_completeness`/`get_ensemble_state` logic in `data/weather_state.py`:
- Schedule sanity: 3-hourly F0-F240 (81 points), matches the validated real-archive schedule exactly.
- A. All 31 members + all expected valid times present -> `COMPLETE`, `usable_for_daily_max=True`.
- B. One member missing entirely -> `INCOMPLETE` (`member_count_available=30`).
- C. One valid time missing across ALL members -> `INCOMPLETE` (`member_count_available` still 31, but `forecast_points_available` short by 1).
- D. A newer run with only 5/31 members arrived does NOT become `latest_usable_run` while an older fully-complete run exists.
- E. `latest_seen_run` advances independently to the newer (still-incomplete) run.
- F. No future information entered the state (every exposed run's `available_time <= query_t`).
- Member-level distribution (`member_daily_max_f`) exposed with all 31 entries, never collapsed to summary-only.
- Raw-frequency-not-probability note present on every state.

**14/14 passed.** No fix to `data/weather_state.py` was needed -- its existing GEFS logic (built before this session, in an earlier architecture phase) was already correct against the now-independently-confirmed real archive structure. HRRR/GFS/NBM schedules regression-checked afterward and confirmed unchanged (48/18h HRRR, 120h GFS, 264/36/189h NBM tiers, all matching their previously-validated values).

## 11. State-builder findings

No changes made to `data/weather_state.py`. Its GEFS-specific logic (`EXPECTED_GEFS_MEMBERS=31`, `_forecast_hour_candidates("gefs")=range(0,241,3)`, `assess_ensemble_run_completeness`, `get_ensemble_state`) already matches every validated finding above. `_forecast_hour_candidates` for GEFS does not distinguish run hours (all 4 run hours share the identical F0-F240 3-hourly schedule) -- confirmed correct, unlike NBM's run-hour-dependent tiers.

## 12. Processed-data schema

Every row carries (see `scripts/pilot_phase6_gefs.py::extract_one_member_hour`):
`source, run_time, valid_time, available_time, availability_time_type, forecast_hour, ensemble_member, member_type, variable, level, temporal_stat, temporal_window_hours, requested_lat, requested_lon, grid_lat, grid_lon, distance_km, is_primary_central_park_grid, value, units, value_f, source_object`.

`ensemble_member` and `member_type` are always populated -- member identity is a first-class column, never dropped or replaced by a pre-aggregated summary.

## 13. Ensemble-summary sanity checks (validation only, never a replacement for raw rows)

Full 31-member target-day extraction for 2025-06-15 (lead-in run 2025-06-14 18Z, 8 required forecast hours, 248 work items, 0 failures, 24,552 rows):
- 31/31 members had all 8 TMP forecast hours present.
- Member daily-max distribution (Central Park primary grid point): mean=68.09F, std=3.29F, min=61.61F, max=72.79F, p10=63.70F, p50=68.99F, p90=71.94F -- physically plausible spread (~11F range) for an 18-hour-out mid-June NYC forecast.
- Control member (gec00) daily max = 71.61F, within the ensemble's [min,max] range, as expected.
- These summary statistics were computed only for this validation check -- the canonical processed dataset stores the 24,552 raw member-level rows, not this summary.
- Raw ensemble member frequencies (e.g. "X/31 members predict >=90F") are explicitly NOT calibrated probabilities -- documented in code (`get_ensemble_state`'s `note` field) and here.

## 14. Selected work-item design

**`(run_time, ensemble_member, forecast_hour)`** -- one idx fetch + up to 11 variable byte-range GETs per work item, matching every other source's per-item efficiency (one archive object = one work item), with `ensemble_member` as the added dimension the archive itself already partitions on (one GRIB2 file per member per forecast hour, identical partitioning to HRRR/GFS/NBM's one-file-per-forecast-hour). Rejected alternative: `(run_time, forecast_hour)` fetching all 31 members inside one worker -- would reduce checkpoint granularity (a single slow/failed member blocks the whole run's forecast-hour entry) and complicate retry semantics for no efficiency gain, since each member's file is a genuinely separate S3 object regardless.

## 15. Benchmark results

20 representative items (5 dates, all 4 run hours, F0, short/medium/long horizons, control + several perturbed members), tested at 8/12/16/20/24 workers:

| workers | items/sec | errors | CPU avg / peak |
|---|---|---|---|
| 8 | 2.13 | 0 | 16.2% / 52.5% |
| 12 | 2.89 | 0 | 20.5% / 78.8% |
| 16 | 2.36, then 3.37 (confirmation, 60 items) | 0 | 23.5-23.8% / 98.5-99.0% |
| 20 | 3.09, then 2.72 (confirmation, 60 items) | 0 | 32.6-23.6% / 99.8-100% |
| 24 | 2.96 | 0 | 36.7% / 100.0% |

Zero errors and zero S3 throttling at every worker count tested. Throughput is flat/noisy across the whole range (network-latency-bound, like NBM's benchmark -- CPU avg never exceeds ~37% even at 24 workers). 16 and 20 workers' confirmation-trial averages are within noise of each other (~2.87 vs ~2.90 items/sec).

## 16. Proposed worker count

**16.** Equivalent sustained throughput to 20 workers within measurement noise, but noticeably lower CPU usage and more headroom -- preferred per the instruction to choose stable sustained throughput over the largest worker count. As with NBM, the flat curve means this choice is not highly consequential.

## 17. Estimated 40-day work-item count

**62,744** `(run_time, member, forecast_hour)` items -- independently computed: 288 run_times (40 selected days + lead-in, x4 run hours) x average 7.03 required forecast hours/run x 31 members. Roughly 2x NBM's item count, consistent with GEFS's coarser 3-hourly cadence but 31x member multiplier vs. NBM's single deterministic stream.

## 18. Estimated remote bytes / local storage / runtime (ESTIMATES -- not measurements)

From the benchmark: ~126.1MB / 20 items = ~6.31 MB/item average (F0 items smaller, ~54 rows; later items larger, ~99 rows).

- **Estimated remote bytes read**: 62,744 x ~6.31MB ~= **~396 GB**.
- **Estimated local processed storage**: from the full-ensemble sample (24,552 rows from 248 items = ~99 rows/item, consistent with non-F0 items), scaling to 62,744 items x ~86 avg rows/item (blended F0/non-F0) ~= ~5.4M rows; at HRRR's/NBM's observed ~12 bytes/row (Parquet-compressed) ~= **~65 MB**.
- **Estimated runtime**: 62,744 / ~2.9 items/sec (16-worker average) ~= **~6.0 hours**.

These are pre-launch estimates only, clearly distinct from actual production measurements which do not yet exist (full pilot not launched).

## 19. Checkpoint/resume results

Not separately re-tested in this phase -- `Checkpoint`/`run_concurrent` in `data/pilot_extraction.py` are shared, unmodified code already validated across HRRR/GFS/NBM's full production runs (including genuine crash/resume events during this project). The GEFS validation sample and full-ensemble sample both ran via direct process-pool calls (not through `run_concurrent`) for this investigation phase; checkpoint/resume behavior itself is architecturally identical for GEFS as for every other source (same `Checkpoint` class, same key-based skip-if-DONE logic) and will be exercised for real at the small-sample-via-`run_concurrent` stage before any full launch.

## 20. Temp-file status

**0 leaks.** Uses the same shared in-memory `decode_message_multi_point` (no temp files) as every other source -- confirmed via a clean Windows TEMP directory scan after all validation/benchmark runs in this phase.

## 21. Files changed

- `data/gefs.py`: added `import re`, `MAX_PATCH_DISTANCE_KM=20.0`, `parse_forecast_desc()`.
- `scripts/pilot_phase6_gefs.py`: new production-readiness validation script (launch guard in place, mirrors `pilot_phase3_hrrr.py`/`pilot_phase4_gfs.py`/`pilot_phase5_nbm.py`'s architecture).
- `docs/gefs_pilot_readiness.md`: this file (new).
- `docs/data_sources.md`, `docs/variable_semantics.md`, `docs/point_in_time.md`, `docs/decisions.md`: updated (see each file's GEFS section/entries).

No changes to `data/weather_state.py` (existing GEFS logic validated as correct, no fix needed), no changes to frozen HRRR/GFS/NBM processed data.

## Unresolved issues carried forward

1. Full 40-day pilot not yet launched -- all figures in sections 17-18 are estimates from a 20-item benchmark sample, not measurements.
2. The benchmark's flat/noisy throughput curve (like NBM's) means the exact worker count is not sharply determined; 16 is well-supported but not a clear peak.
3. `Checkpoint`/`run_concurrent`'s crash/resume behavior was not separately re-exercised for GEFS specifically in this phase (architecturally shared/unchanged code, already validated at HRRR/GFS/NBM production scale) -- should be observed directly during the eventual full launch's monitoring phase, same as every prior source.
