# NBM Pilot Production-Readiness — Findings

Status: production-readiness validation complete at small scale. **The full 40-day pilot has NOT been launched** (`scripts/pilot_phase5_nbm.py::main()` has a hard guard preventing accidental full-scale launch).

All findings below were established by direct, empirical investigation against the live archive during this validation phase -- not carried over unverified from the prior one-day feasibility test (`scripts/test_nbm_nyc_feasibility.py`) or the cadence investigation (`scripts/investigate_nbm_cadence.py`), though both are consistent with what's confirmed here.

## 1. Forecast-hour schedule: run-hour dependent (a real correction to prior code)

The prior assumption (baked into `data/nbm.py` before this phase, and into `data/weather_state.py`'s `_forecast_hour_candidates` for `"nbm"`) was that every run shares one fixed schedule out to F264. **This is false.** Verified across all 24 run hours: the schedule falls into three tiers keyed by `run_hour % 6`:

| Tier | `run_hour % 6` | Run hours | Schedule |
|---|---|---|---|
| full | 0, 1 | 0,1,6,7,12,13,18,19 | hourly F1-F36, 3-hourly F39-F192, 6-hourly F198-F264 |
| medium | 3 | 3,9,15,21 | hourly F1-F36, 3-hourly F39-F189 (stops there, no 6-hourly extension) |
| short | 2,4,5 | 2,4,5,8,10,11,14,16,17,20,22,23 | hourly F1-F36 only |

**`forecast_hour=0` does not exist at all** for NBM's core product at any run hour (confirmed 404 for every run/date tested) -- there is no degenerate F0 message to special-case, unlike HRRR (which has a zero-length APCP entry at F0) or GFS (which has `"anl"` instantaneous fields at F0). NBM simply starts at F1.

Implemented in `data/nbm.py::nbm_schedule_tier()` / `available_forecast_hours(run_hour)`. **`data/weather_state.py`'s NBM completeness branch was NOT modified** (out of scope for this phase, per instruction) -- it still hardcodes the full-tier schedule for every run hour, which means it will incorrectly expect forecast hours 39-192 for medium/short-tier runs that structurally never produce them. This fails *safe* (such runs will be assessed as perpetually `INCOMPLETE`/`usable_for_daily_max=False`, never as falsely complete), but it does mean `weather_state.py` currently cannot correctly recognize a genuinely complete short- or medium-tier NBM run. **Flagging for a decision, not fixing.**

## 2. Grid geometry

- Grid type: Lambert Conformal (confirmed via `gridType` on a real decoded message).
- Dimensions: `Ni=2345, Nj=1597`.
- Resolution: `Dx=Dy=2539.703` metres (~2.54km) -- the finest of the three sources extracted so far (HRRR ~3km, GFS ~28km).
- Grid fingerprint: `md5:5bb2a8d01d638075eaa2ff7236270d44` (via `md5GridSection`, confirmed reliably available) -- distinct from both HRRR's and GFS's fingerprints.
- Central Park center-point distance: 0.43km (close to, though not identical to, the prior feasibility estimate of ~0.47km -- consistent, not contradictory, small measurement/point differences expected).
- 3x3 patch (`+/-0.03deg`, same offsets as HRRR/GFS): produces **9 distinct grid cells** (confirmed, no collapse -- NBM's grid is fine enough, similar to HRRR's behavior, unlike GFS's full collapse). Max patch distance observed: 1.24km.
- Distance threshold: derived independently as **5.0 km**, from `0.5 * sqrt(Dx^2 + Dy^2) ~= 1.796km` (theoretical worst case for an equal-Dx/Dy Lambert grid) with a ~2.8x margin. **Not** HRRR's 10km or GFS's 20km -- a third, independently-derived value appropriate to NBM's own finer grid. Implemented as `data.nbm.MAX_PATCH_DISTANCE_KM`.
- Cached-vs-uncached: 0 index mismatches, 0 value mismatches across 24 real messages (4 dates x 6 variables), single stable fingerprint throughout.

## 3. Full variable / product inventory

| Variable | Level(s) seen | Duplicate candidates | Selection rule |
|---|---|---|---|
| TMP | `surface` (skin temp, NOT wanted) + `2 m above ground` (instant + ens std dev) | 3 total, level+stddev filtering needed | `find_message(var, level="2 m above ground", desc_excludes="ens std dev")` |
| DPT | `2 m above ground` (instant + ens std dev) | 2 | same pattern |
| RH | `2 m above ground` (instant only) | 1 -- no duplicate at all | unique match |
| WIND | `10 m above ground`, `30 m above ground`, `80 m above ground`, `surface - 610 m above ground` (a full boundary-layer product), each instant; `10 m above ground` also has an ens std dev variant | 5 total across 4 heights | `level="10 m above ground"`, `desc_excludes="ens std dev"` -- level must always be explicit, since the SAME variable name exists at 4 different heights and the idx lists the odd `surface-610m` layer product first |
| WDIR | same 4 heights as WIND, no stddev variant at any height | 4 | `level="10 m above ground"` |
| APCP | see section 5 | many (amount x2 windows x prob-vs-plain) | see section 5 |
| TCDC | 3x `typeOfLevel=unknown` ("reserved" in idx text -- likely low/mid cloud layers, eccodes cannot name them, NOT identified further, NOT needed for our variable set), 1x `highCloudLayer`, `surface` (instant + ens std dev) | 6 total | `level="surface"`, `desc_excludes="ens std dev"` -- **no averaged variant exists for NBM TCDC** (unlike GFS), only instantaneous |
| TMAX / TMIN | `2 m above ground`, 12-hour period windows, each with an ens std dev sibling | see section 4 | see section 4 |

**Unresolved**: the 3 `typeOfLevel=unknown` TCDC candidates (idx level text `"reserved"`) could not be identified precisely with the current eccodes/GRIB-table build -- flagged, not guessed at, and not needed for the core variable set.

## 4. TMP vs. direct TMAX — confirmed materially different, both retained

NBM provides both an hourly forecast temperature trajectory (`TMP`) and a direct 12-hour period-maximum product (`TMAX`). Compared directly (2025-06-06 12Z run, 0-12h window): **direct TMAX = 85.32F, max(hourly TMP over the same window) = 83.86F -- a 1.46F difference.** This is not measurement noise; it is consistent with TMAX being a distinctly post-processed/blended product rather than a simple max of the hourly field (matching this project's prior hypothesis, now confirmed rather than assumed). **Recommendation: retain both** `TMP` (hourly, always available) and the direct period `TMAX`/`TMIN` (only at 00Z/12Z, see below) as separate features -- do not treat one as a substitute for or reconstruction of the other.

**TMAX/TMIN exist only for run_hour in {0, 12}** -- verified directly across all 8 "full-tier" run hours (0,1,6,7,12,13,18,19): only 0Z and 12Z carry any TMAX/TMIN messages at all; 1Z/6Z/7Z/13Z/18Z/19Z have none, despite sharing the same forecast-hour-schedule tier. Which 12-hour period is "max" vs. "min" alternates and depends on which run hour is used (a 12Z run's first period `[0,12)` covers local afternoon -> TMAX; a 00Z run's first period `[0,12)` covers overnight -> TMIN) -- implemented via explicit `prefer="period_max"`/`"period_min"` selection (`select_message_explicit`), never assumed from a fixed alternation starting at TMAX.

## 5. APCP (precipitation) semantics

NBM's APCP idx entries fall into two entirely different kinds, both of which must be told apart:

1. **Probability-of-exceedance products** (`forecast_desc` contains a `"prob >0.254:prob fcst 255/255"` qualifier) -- these are probabilities that precipitation exceeds a threshold, NOT accumulation amounts. **Always excluded** from the amount extraction.
2. **Deterministic amount products** -- two windows exist:
   - A **1-hour trailing window**, present at every `forecast_hour >= 1` (e.g. `"38-39 hour acc fcst"`).
   - A **6-hour window**, present only when this forecast hour's valid_time falls on an absolute UTC synoptic hour (00/06/12/18Z) -- empirically this is `(run_hour + forecast_hour) % 6 == 0`, NOT simply `forecast_hour % 6 == 0` (that simpler check only coincides for runs that themselves start on a synoptic hour). The selector never hardcodes this alignment; it only returns whichever exactly-6-hour candidate the idx genuinely contains, so it is correct for every run hour without needing the alignment rule encoded anywhere.

NBM does **not** offer a native cumulative-since-run-start amount product (unlike HRRR and GFS, which both have one) -- only the two windowed products above. Both carry distinct information (1-hour: immediate recent precipitation; 6-hour: a coarser recent window) and **both are extracted** as separate features (`APCP_1H`, `APCP_6H`), consistent with the "report separately, don't arbitrarily choose" instruction. `APCP_6H` is legitimately absent (not failed) at most forecast hours.

Selection: `select_message_explicit(entries, "APCP", "surface", forecast_hour, prefer="shortest_window"|"sixhour_window")`, excluding any candidate whose qualifier contains `"prob"`.

## 6. Wind semantics

NBM's `WIND`/`WDIR` are **scalar wind speed and direction**, not U/V components -- confirmed by the complete absence of `UGRD`/`VGRD` entries in the core product's idx at any level. This is **not equivalent to HRRR/GFS's UGRD/VGRD representation**; combining an NBM wind feature with an HRRR/GFS wind feature downstream would require an explicit speed/direction <-> u/v conversion (`u = -speed*sin(dir)`, `v = -speed*cos(dir)`, meteorological convention), which is not implemented and should not be assumed equivalent without doing so. Canonical representation recommended: extract `WIND` and `WDIR` separately at `10 m above ground` (matching HRRR/GFS's 10m height), not converted to components at extraction time.

## 7. Cloud (TCDC) semantics

NBM's `TCDC` at `level="surface"` is **instantaneous only** -- confirmed via `stepType=instant` on direct GRIB inspection, and no averaged variant exists at this level (unlike GFS, which publishes both instant and averaged TCDC). Selection: `level="surface"`, exclude `"ens std dev"`. The three "reserved"/unknown-type candidates and the "high cloud layer" candidate are distinct products (likely cloud-layer breakdowns) not required for the core variable set and not further identified in this phase.

## 8. F0 / analysis-like edge cases

`forecast_hour=0` **does not exist** for NBM's core product (confirmed via direct 404 across every run hour and date tested) -- there is no message to select from and therefore no ambiguity to resolve. This is the cleanest of the three sources' F0 behavior: HRRR has a degenerate but present F0 APCP entry, GFS has `"anl"`-labeled instantaneous fields at F0, NBM has nothing at all. The pilot script's own work-item planning (`required_forecast_hours_for_run`) never generates an F0 work item for NBM, since `available_forecast_hours()` starts at 1.

## 9. Availability timestamps

Same architecture as HRRR/GFS: `S3_LAST_MODIFIED_PROXY`, one file (and therefore one `Last-Modified` timestamp) per `(run, product, forecast_hour)`, shared across every variable within that file. Confirmed progressive arrival within a single run: lag increased from 62.2 min (F1) to 62.9 min (F72) in one tested run -- small but real, consistent with the qualitative pattern already seen for HRRR/GFS/GEFS and with the prior feasibility estimate of "41-71 minutes." **Not** claimed as an exact publication timestamp -- same proxy-with-limitations treatment as every other source (see `point_in_time.md`).

## 10. Completeness rules

See section 1 above for the `data/weather_state.py` gap. The validated schedule (`available_forecast_hours(run_hour)`) is what the extraction path (`scripts/pilot_phase5_nbm.py`) actually uses for its own work-item planning -- it does NOT call `expected_target_day_valid_times("nbm", ...)`, since that function depends on the not-yet-corrected `_forecast_hour_candidates`. Progressive publication means `latest_seen_run` vs. `latest_usable_run` matters here exactly as much as for HRRR/GFS/GEFS -- a newer, still-arriving NBM run must never replace an older, already-COMPLETE one (the existing `assess_deterministic_run_completeness` mechanism, once given the correct schedule, would enforce this the same way it does for other sources).

## 11. Run cadence

Per instruction, all runs validated as producing genuine data are retained for the pilot -- no cadence reduction (24 -> 12/8/4 per day) is applied or recommended at this stage. That comparison is explicitly a later question the pilot's own data should answer.

## 12/13. Multiprocessing / extraction architecture / temporal metadata

`scripts/pilot_phase5_nbm.py` (new) mirrors the validated HRRR/GFS architecture exactly: `Checkpoint`/`run_concurrent` (shared, unmodified), per-process grid cache via the shared `decode_message_multi_point`, in-memory eccodes decoding (no temp files), NBM's own `MAX_PATCH_DISTANCE_KM`, explicit product selection for every variable with duplicate candidates, and `temporal_stat`/`temporal_window_hours` on every row. NBM introduces two temporal_stat values beyond HRRR/GFS's `instant`/`accum`/`average`: **`max`** and **`min`** (for the direct period products) -- used only where scientifically appropriate (TMAX_PERIOD/TMIN_PERIOD), not forced into the existing categories.

`main()` has a hard `RuntimeError` guard preventing a full-scale launch until explicitly approved.

## 14. Validation sample results

12 representative work items: multiple dates (Jan/Apr/Jun/Aug 2025), all three schedule tiers, F1, cadence boundaries (F36/F39, F189, F198), a TMAX-period run hour (00Z) and a TMIN-period run hour (12Z), a short-tier run hour (confirmed correctly stops at F36).

- Multiprocessing (8 workers): **PASS**.
- Checkpoint/resume: **PASS** (second run of the same 12 items did 0 new work).
- Cached-vs-uncached: **PASS** (0 mismatches, single fingerprint, across a separate 24-message test).
- Temp-file leaks: **0**, before and after.
- Schema consistency: **PASS**, 1 consistent schema across all output.
- Row counts verified exactly against the variable-presence rules above for every one of the 12 items (e.g. F1 -> 63 rows = 7 vars x 9 points; F12 (00Z) -> 81 rows = 9 vars x 9 points including TMIN_PERIOD; short-tier F36 -> 63 rows, no period product since run_hour=5 is not in {0,12}).
- 0 null values.

## 15/16. Documentation and reproducibility

Updated: `docs/data_sources.md` (NBM entry), `docs/variable_semantics.md` (NBM variable table), `docs/decisions.md` (NBM-specific decisions). `selected_days.csv` SHA-256 unchanged: `90355ade1277f7d8fcf10f3482c92ba193f09bf693dd5c2057b704bc048e9015` (not regenerated, not touched). A `manifest.json` for the eventual full NBM pilot would follow the same schema as HRRR/GFS's (see `reproducibility.md`) once launched -- not created yet, since no full pilot exists.

## 17. Pre-production phase (post-approval): completeness fix, benchmark, expanded APCP_6H validation

### Completeness logic fixed

`data/weather_state.py`'s NBM branch of `_forecast_hour_candidates()` previously hardcoded one fixed schedule (hourly to F36, then 3-hourly to F192) for every run hour. It now delegates directly to `data.nbm.available_forecast_hours(run_hour)` -- the same validated function the extraction path uses -- so there is exactly one source of truth for the schedule, not two independently-maintained copies. 31 targeted tests (`test_nbm_completeness.py`, not committed -- scratch) covering all three tiers at 10 representative run hours (00,01,02,03,06,09,14,19,21,23Z) and the boundary forecast hours (F35/36/37/39, F189, F192, F195/198, F264/265) all pass, including:
- A genuinely complete run is assessed `COMPLETE` / `usable_for_daily_max=True`.
- A run missing even one required valid_time is `INCOMPLETE` / `usable_for_daily_max=False`.
- A newer, still-arriving (incomplete) run does NOT become `latest_usable_run` while an older complete run exists; `latest_seen_run` advances to the newer run independently (confirmed with a synthetic older-complete / newer-incomplete NBM dataframe).
- No future information enters the state (every exposed run's `available_time <= query_t`).
- Short-tier runs never expect beyond F36; medium-tier runs never expect beyond F189; full-tier transitions correctly at every validated cadence boundary.

HRRR and GFS completeness regression-checked afterward: both schedules unchanged (`_forecast_hour_candidates` for `"hrrr"`/`"gfs"` untouched by this fix).

### Worker benchmark (real production extraction path)

20 representative items (mixed dates, all 3 tiers, short/medium/long horizons, TMAX/TMIN run hours), tested at 8/12/16/20/24 workers:

| workers | items/sec | errors | CPU avg | CPU peak |
|---|---|---|---|---|
| 8 | 1.40 | 0 | 24.8% | 76.9% |
| 12 | 1.66-1.72 (2 trials) | 0 | 28-32% | 81-99% |
| 16 | 1.45 | 0 | 33.8% | 100% |
| 20 | 1.49-1.82 (2 trials) | 0 | 33-38% | 100% |
| 24 | 1.43 | 0 | 41.2% | 100% |

**Zero errors and zero S3 throttling at every tested worker count.** Throughput is essentially flat across the whole 8-24 range (network-latency-bound, not CPU-bound -- CPU avg never exceeds ~41% even at 24 workers, unlike HRRR's clear CPU-saturation-driven peak). Per instruction not to choose purely on peak instantaneous throughput: **selected 20 workers** as the highest *stable* result across repeated trials, with comfortable CPU/memory headroom and no indication of throttling at that level or below.

### Storage / remote-byte / runtime estimate (labeled as estimates)

Using the benchmark's measured ~9.94 MB/work-item (consistent across all trials, since trials repeated the same base item set) and the independently-recomputed 31,578-item work-item count:

- **Estimated remote bytes read**: 31,578 x 9.94MB ~= **314 GB** (comparable order of magnitude to HRRR's actual 236.84GB, despite NBM having more work items, since NBM's per-item variable count is smaller).
- **Estimated local processed storage**: ~2.13M rows (31,578 items x ~7.5 avg variables/item x 9 patch points), at HRRR's observed ~15 bytes/row (Parquet-compressed) ~= **~32 MB**. Remote bytes read != local storage retained -- stream-and-discard, matching the project's existing policy.
- **Estimated runtime at 20 workers**: 31,578 / ~1.66 items/sec (average of the 20-worker trials) ~= **~5.3 hours**.

### Expanded APCP_6H validation

8 additional cases across 4 different run hours (00Z, 03Z, 06Z, 18Z) and multiple dates, explicitly covering both presence and absence: **every single case matched the "valid_time's hour-of-day is an absolute UTC synoptic hour (00/06/12/18Z)" rule exactly** -- 4 presence cases correctly found the product, 4 absence cases correctly returned `None` (never a fabricated value, never a checkpoint failure). Confirmed via `run_time + valid_time` semantics directly, not inferred from `forecast_hour` alone. Temporal metadata confirmed: `APCP_1H` -> `temporal_stat="accum"`, `temporal_window_hours=1`; `APCP_6H` -> `temporal_stat="accum"`, `temporal_window_hours=6`.

### TMAX/TMIN period representation -- schema already sufficient (demonstrated, not changed)

The existing schema already fully preserves the represented interval for any period product: `valid_time` is always the period's END, and `temporal_window_hours` is the period's LENGTH -- so `period_start = valid_time - temporal_window_hours` is always exactly recoverable downstream. Demonstrated directly on a real extracted row: a `TMIN_PERIOD` row with `valid_time=2025-01-15T12:00Z`, `temporal_window_hours=12` correctly derives `period=[2025-01-15T00:00Z, 2025-01-15T12:00Z]`. No schema change was needed; this same mechanism already applies identically to `APCP_1H`/`APCP_6H`'s windows.

## 18. Full 40-day production pilot -- STATUS: FINAL PASS

Launched 2026-09-27 at commit `cb09e8f` (+ an uncommitted script fix, see `manifest.json`'s provenance notes), 20 workers.

### Result

| Metric | Value |
|---|---|
| Planned work items | 31,578 |
| DONE | 31,578 |
| FAILED (final) | 0 |
| Initial transient failures (S3 `ConnectionResetError`, retried successfully) | 7 |
| Parquet parts | 1,265 |
| Total rows | 2,026,080 |
| Full-row duplicates | 0 |
| Logical-key duplicates | 0 |
| Checkpoint rows-sum <-> persisted rows | exact match |
| Zero-byte / corrupt Parquet files | 0 |
| Schema consistency | 1 schema across all parts |
| Grid fingerprint | `md5:5bb2a8d01d638075eaa2ff7236270d44` (matches small-sample validation; reconfirmed via a live post-run recompute) |
| Max observed patch distance | 1.238 km (threshold 5.0 km) |
| `forecast_hour=0` rows | 0 (confirmed structural absence) |
| APCP_6H synoptic-alignment compliance | 40,158/40,158 rows (100%) have `valid_time.hour % 6 == 0` |
| TMAX/TMIN run-hour restriction | only run_hour in {0, 12} present, as required |
| Selected-day coverage | 40/40 |
| Run-hour coverage | all 24 |
| Temp-file leaks | 0 |
| Decode failures | 0 |

### A data-hygiene finding, caught and fixed before manifest creation

The post-run audit found the checkpoint held 31,587 keys, not the planned 31,578. Root cause: an earlier production-readiness validation phase's 12-item small sample had been run through the same `Checkpoint`/`run_concurrent` machinery pointed at this same output directory (`data/processed/pilot/nbm/`) before this launch, so 9 of its items (648 rows, one date -- 2025-01-15 -- not even in the selected/lead-in set at all) were sitting in the checkpoint and in one Parquet part file alongside the real pilot's output. 3 of the old sample's 12 items happened to coincide with real pilot work items and were correctly left in place (checkpoint/resume behaved correctly -- no re-fetch, no duplication). The 648 out-of-scope rows were removed from the one affected part file, the 9 stray checkpoint keys were deleted, and the checkpoint-to-persisted-row reconciliation was re-verified to match exactly before the manifest was written. See `manifest.json`'s provenance notes for the full account.

### Actual vs. estimated (do not confuse the two)

| | Estimated (section 17, from a 20-item benchmark) | Actual (full 31,578-item production run) |
|---|---|---|
| Remote bytes read | ~314 GB | ~316.0 GB |
| Local processed storage | ~32 MB | ~28.0 MB |
| Runtime | ~5.3 hours | ~3.68 hours |
| Throughput | 1.66-1.82 items/sec (short repeated trials) | 2.386 items/sec (sustained full run) |

The estimate undershot on throughput because the benchmark's short repeated trials carried more per-trial startup overhead than a single long sustained run does; actual production throughput was consistently higher once warmed up.

## Unresolved issues carried forward

1. ~~`data/weather_state.py`'s NBM completeness branch does not reflect the validated schedule~~ -- **fixed**, see section 17.
2. The 3 `typeOfLevel=unknown` TCDC candidates are not identified (section 3) -- not needed for the core variable set, but a genuine gap if cloud-layer breakdowns are ever wanted.
3. ~~`MAX_WORKERS` not independently benchmarked for NBM~~ -- **benchmarked**, see section 17; 20 selected.
4. ~~Whether to include `APCP_6H`~~ -- **approved, both retained**.
5. The worker benchmark's flat throughput curve (8-24 workers all within ~1.4-1.8 items/sec) means the exact worker count is not highly consequential for NBM specifically -- unlike HRRR/GFS, there was no clear peak-then-decline signal to pin down a single "best" value with high confidence; 20 is a well-supported but not sharply-optimal choice.
