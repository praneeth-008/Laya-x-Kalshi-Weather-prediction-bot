# Canonical Feature Dictionary

This is the authoritative reference for what every stored value actually means. It exists because two identically-named variables from two different sources (`APCP`, `DSWRF`) turned out to have different temporal semantics, discovered only through direct investigation -- see `decisions.md` for the full history. **Read this before consuming any HRRR or GFS column downstream.**

Two additive metadata columns carry temporal meaning explicitly wherever they exist:
- `temporal_stat`: `instant` | `accum` | `average`
- `temporal_window_hours`: `0` for instantaneous values; the accumulation/average window length (hours) otherwise.

These columns exist on GFS output (all rows) and on the HRRR APCP_1H backfill output, but **not** on the original frozen HRRR pilot output (see the HRRR APCP entry below for why, and the note at the end of this file for how to interpret HRRR's schema by convention until/unless it is extended).

---

## HRRR

| Canonical feature | GRIB id | Level | Units | temporal_stat | temporal_window_hours | Selection rule |
|---|---|---|---|---|---|---|
| TMP | `t` | 2 m above ground | K | instant | 0 | unique idx match |
| DPT | `dpt` | 2 m above ground | K | instant | 0 | unique idx match |
| UGRD | `u` | 10 m above ground | m/s | instant | 0 | unique idx match |
| VGRD | `v` | 10 m above ground | m/s | instant | 0 | unique idx match |
| PRES | `sp` | surface | Pa | instant | 0 | unique idx match |
| TCDC | `tcc` | entire atmosphere | % | instant | 0 | unique idx match (HRRR, unlike GFS, does not publish a second averaged TCDC variant) |
| **APCP (cumulative)** | `tp` | surface | kg/m^2 | accum | = forecast_hour (varies per row) | see below |
| **APCP_1H** | `tp` | surface | kg/m^2 | accum | 1 | see below |
| DSWRF | `sdswrf` | surface | W/m^2 | **instant** | 0 | unique idx match |

### APCP (cumulative) -- the original, frozen pilot feature

Accumulation from forecast initialization (`run_time`, i.e. hour 0) to `valid_time`. This is what the original 20,836-item HRRR pilot's `"APCP"` column contains, confirmed by direct value comparison (not idx-order inference) against 27 representative work items spanning F1-F46 and 9 dates.

**Investigation finding**: HRRR's idx lists two APCP candidates at every forecast_hour >= 2: `"0-N hour/day acc fcst"` (cumulative-since-start) and `"(N-1)-N hour acc fcst"` (the direct 1-hour windowed product), cumulative listed first. The original extraction used a first-match selector (`find_message()`), so it picked up the cumulative product. This value is **retained as-is** -- it is legitimate, well-defined data, just not the only APCP quantity worth having.

### APCP_1H -- the additive backfill feature

Direct NOAA 1-hour accumulation ending at `valid_time`, fetched from its own distinct byte range (never derived by differencing the cumulative series). **Only defined for forecast_hour >= 1.**

**At HRRR forecast_hour=0**: APCP_1H is **NOT APPLICABLE**. Explicitly:
- NOT zero.
- NOT a failed extraction.
- NOT missing due to data loss.

The rule: *HRRR APCP_1H is defined only for forecast_hour >= 1, because F0 has no preceding forecast interval.* HRRR's idx has only a degenerate zero-length `"0-0 day acc fcst"` entry at F0 -- there is no "hour before forecast start" from which a genuine 1-hour accumulation could be defined. The 840 F0 work items (of the full 20,836) are excluded from the APCP_1H work-item universe *before* extraction is attempted -- never submitted, never recorded as a checkpoint `FAILED` entry, never assigned a fabricated `0.0`.

**Selection mechanism**: `data/hrrr.py`'s `select_message_explicit(entries, "APCP", "surface", forecast_hour, prefer=...)` parses every candidate's `forecast_desc` (via `parse_forecast_desc()`) and selects by parsed accumulation window, never by idx position:
- `prefer="cumulative_since_start"`: the candidate whose window starts at hour 0.
- `prefer="windowed_1h"`: the candidate whose window is exactly 1 hour ending at `forecast_hour`.

At `forecast_hour=1`, both rules resolve to the **same message** (verified byte-identical) -- the two definitions describe the same physical interval there. This is intentional, not a special case in the selection code.

**Why direct extraction, not differencing**: empirically tested `cumulative(fh) - cumulative(fh-1)` against the directly-fetched 1-hour value across 19 consecutive-hour pairs in an active-precipitation run. Result: **10 exact matches, 9 mismatches (47%)**, discrepancies of 0.001-0.004 kg/m^2. Cause: NOAA independently quantizes/packs each accumulation-window GRIB message -- the cumulative and windowed products are not derived from each other at the packing stage, so their difference is not guaranteed to reproduce the windowed value's own packed precision. Direct extraction is therefore always used; differencing is never used to construct APCP_1H.

**Backfill result**: additive dataset at `data/processed/pilot/hrrr_apcp_1h/` (own checkpoint, own Parquet parts, gitignored). 19,996/19,996 eligible items DONE, 0 FAILED, 805 parts, 179,964 rows. The original 835-part, 1,500,192-row HRRR dataset is untouched.

### DSWRF (HRRR) -- instantaneous

Confirmed via direct GRIB metadata inspection (`"N hour fcst"`, `stepType=instant`, no accumulation window) -- a single instantaneous value at `valid_time`, unlike GFS's DSWRF (see below).

---

## GFS

| Canonical feature | GRIB id | Level | Units | temporal_stat | temporal_window_hours | Selection rule |
|---|---|---|---|---|---|---|
| TMP | `2t` | 2 m above ground | K | instant | 0 | unique idx match; `"anl"` at fh=0 |
| DPT | `2d` | 2 m above ground | K | instant | 0 | unique idx match; `"anl"` at fh=0 |
| RH | `2r` | 2 m above ground | % | instant | 0 | unique idx match; `"anl"` at fh=0 |
| UGRD | `10u` | 10 m above ground | m/s | instant | 0 | unique idx match; `"anl"` at fh=0 |
| VGRD | `10v` | 10 m above ground | m/s | instant | 0 | unique idx match; `"anl"` at fh=0 |
| PRES | `sp` | surface | Pa | instant | 0 | unique idx match; `"anl"` at fh=0 |
| **TCDC** | `tcc` | entire atmosphere | % | instant | 0 | **explicit selection**, see below |
| **APCP** | `tp` | surface | kg/m^2 | accum | varies (1-6, occasionally more) | **explicit selection**, see below; **absent** at fh=0 |
| **DSWRF** | `sdswrf` | surface | W/m^2 | **average** | varies (1-6) | unique idx match (only one product exists); **absent** at fh=0 |

### GFS APCP -- final explicit selection semantics

GFS publishes two distinct products under the identical `(APCP, surface)` idx key at nearly every forecast hour: a windowed accumulation resetting on a rolling boundary (`"N-M hour acc fcst"`) and a cumulative-since-run-start total (`"0-N hour/day acc fcst"`). `data/gfs.py`'s `select_message_explicit(prefer="shortest_window")` parses every candidate and picks the one with the **smallest** window ending at the target `forecast_hour` -- the "how much precipitation fell recently" signal, matching the same intent as HRRR's APCP_1H (though the exact window length differs by forecast hour, since GFS's windowed product resets every 6 hours rather than every 1 hour).

At forecast_hour <= 6, the two candidates' windows coincide (`[0, fh]` for both) and GFS emits this as two separate GRIB messages with byte-for-byte identical metadata and values -- confirmed by direct inspection (`startStep`/`endStep`/`stepRange`/`typeOfStatisticalProcessing` all identical). `select_message_explicit()` treats a tie between candidates sharing the same parsed window as safe; a tie between candidates with **different** windows still fails loudly.

**At GFS forecast_hour=0**: APCP has **zero** idx entries at all (verified directly) -- no accumulation is defined at the analysis instant. `select_message_explicit()` returns `None`, and the calling code (`scripts/pilot_phase4_gfs.py`) skips the variable gracefully for that one work item. This is a legitimate absence, not a failure -- the pilot's final GFS row counts confirm APCP has exactly 51,336 rows (5,704 x 9) vs. 52,524 (5,836 x 9) for variables present at every forecast hour, i.e. exactly the 132 fh=0 items missing.

### GFS TCDC -- final explicit instantaneous selection

GFS also publishes two TCDC products: an instantaneous entry (`"N hour fcst"`) and a period-averaged one (`"N-M hour ave fcst"`). `select_message_explicit(prefer="instant")` explicitly selects the instantaneous variant, consistent with HRRR's own instantaneous TCDC and this project's point-in-time `weather_state(t)` philosophy (an atmospheric-state snapshot, not a running average).

**At GFS forecast_hour=0**: TCDC has exactly one entry, `"anl"` (see below) -- no averaged variant exists there (averaging requires elapsed time). `select_message_explicit()` correctly resolves this single candidate.

### GFS DSWRF -- averaged product, NOT semantically identical to HRRR's instantaneous DSWRF

GFS's `pgrb2.0p25` product **only ever publishes a running-average DSWRF** -- there is no instantaneous variant at any forecast hour (verified: exactly one idx entry per forecast hour, always `stepType=avg`). This is genuinely a different physical quantity from HRRR's instantaneous DSWRF despite the identical variable code. `temporal_stat="average"` on every GFS DSWRF row makes this explicit rather than assumed; **do not treat HRRR DSWRF and GFS DSWRF as interchangeable** in any downstream feature that combines both sources.

**At GFS forecast_hour=0**: DSWRF has zero idx entries (verified directly, same as APCP) -- no average is defined at the analysis instant. Handled the same way: variable skipped for that one work item, not a failure.

### GFS RH -- included after production-readiness validation

Not originally in GFS's `CORE_VARS`, despite being tested successfully in earlier GFS feasibility work. Investigated: unique idx entry at `2 m above ground` (no duplicate-product ambiguity), no scientific or technical reason found for its exclusion. Added to GFS's `CORE_VARS` only -- **not** added to HRRR (RH is also directly and uniquely available in HRRR, but adding it there was out of scope; source identity is kept explicit rather than forcing schema symmetry across sources).

### GFS forecast_hour=0: the final `"anl"` solution

At GFS's forecast_hour=0, every instantaneous variable's idx entry has `forecast_desc="anl"` (analysis) instead of `"0 hour fcst"` -- this is GFS's own convention for the model's initial state, before any forecast projection. The implemented solution (`data/gfs.py`'s `parse_forecast_desc()`) explicitly recognizes `"anl"` and maps it to `{"kind": "instant", "start_hour": None, "end_hour": 0}` -- **preserving the analysis semantics** (the code comment and this document both record that `"anl"` means "the analysis time, i.e. forecast_hour=0", not that GFS simply labeled fh=0 as `"0 hour fcst"` under a different string). This mapping is safe because GFS's own convention never uses `"anl"` for any forecast_hour other than 0.

This was discovered via a real production run: the initial 40-day GFS pilot run produced 132 failures, all `"Unrecognized GFS forecast_desc format: 'anl'"`, all at forecast_hour=0. After the fix, all 132 were retried successfully with the corrected parser. Final GFS pilot state: 5,836/5,836 DONE, 0 FAILED.

---

## Interpreting HRRR's existing schema (no columns added retroactively)

The frozen HRRR pilot output (835 parts, 1,500,192 rows) does not have `temporal_stat`/`temporal_window_hours` columns -- adding them would require rewriting already-validated, already-frozen data, which was deliberately avoided. Until/unless that schema is extended, downstream code reading HRRR rows directly (not through the APCP_1H backfill) should apply this convention:

- `source="hrrr"`, `variable` in {TMP, DPT, UGRD, VGRD, PRES, TCDC, DSWRF} -> `temporal_stat="instant"`, `temporal_window_hours=0`.
- `source="hrrr"`, `variable="APCP"` -> `temporal_stat="accum"`, `temporal_window_hours=forecast_hour` (the cumulative-since-start quantity, NOT a fixed small window).
- The separate `hrrr_apcp_1h` dataset's `variable="APCP_1H"` rows already carry `temporal_stat="accum"`, `temporal_window_hours=1` explicitly.

---

## NBM

Status: production-readiness validated (small sample), full pilot not launched -- see `nbm_pilot_readiness.md` for the complete investigation. Table reflects the validated extraction design in `scripts/pilot_phase5_nbm.py`.

| Canonical feature | GRIB id | Level | Units | temporal_stat | temporal_window_hours | Selection rule |
|---|---|---|---|---|---|---|
| TMP | `t` | 2 m above ground | K | instant | 0 | `level="2 m above ground"` excludes a `surface` (skin temp) duplicate and an `ens std dev` sibling |
| DPT | `dpt` | 2 m above ground | K | instant | 0 | exclude `ens std dev` |
| RH | `r` | 2 m above ground | % | instant | 0 | unique, no duplicate at all |
| WIND | (scalar speed) | 10 m above ground | m/s | instant | 0 | `level="10 m above ground"` explicitly -- the SAME variable name exists at 30m/80m/a full boundary-layer product; level must always be stated |
| WDIR | (scalar direction) | 10 m above ground | degrees | instant | 0 | same explicit-level requirement as WIND |
| TCDC | `tcc` | surface | % | instant | 0 | `level="surface"`, exclude `ens std dev` -- **no averaged variant exists** (unlike GFS) |
| APCP_1H | `tp` | surface | kg/m^2 | accum | 1 | `select_message_explicit(prefer="shortest_window")`, excluding any candidate with a `"prob"` qualifier (probability-of-exceedance, not an amount) -- present at every forecast_hour >= 1 |
| APCP_6H | `tp` | surface | kg/m^2 | accum | 6 | `select_message_explicit(prefer="sixhour_window")` -- present only when this forecast hour's valid_time falls on an absolute UTC synoptic hour (00/06/12/18Z); legitimately absent otherwise, never a failure |
| TMAX_PERIOD | `tmax` | 2 m above ground | K | **max** | 12 | `select_message_explicit(prefer="period_max")` -- exists ONLY for run_hour in {0, 12}; which 12h period is max vs. min alternates by run hour (see below) |
| TMIN_PERIOD | `tmin` | 2 m above ground | K | **min** | 12 | `select_message_explicit(prefer="period_min")` -- same run-hour restriction |

### NBM does not have a native cumulative-since-run-start APCP product

Unlike HRRR and GFS (both of which publish a `"0-N hour/day acc fcst"` cumulative candidate), NBM's deterministic APCP amount only ever offers the 1-hour and (when aligned) 6-hour windowed products -- there is no cumulative-since-start amount to select even if one were wanted. This is a genuine structural difference from HRRR/GFS's APCP behavior, not an oversight in selection.

### TMP vs. TMAX -- confirmed materially different, both retained

Direct TMAX and `max(hourly TMP)` over the same window differed by 1.46F in a tested case (2025-06-06 12Z, 0-12h window: TMAX=85.32F vs. reconstructed=83.86F) -- confirmed via real data, not assumed. Both `TMP` (hourly) and `TMAX_PERIOD`/`TMIN_PERIOD` (00Z/12Z only) are retained as separate, non-substitutable features.

### WIND/WDIR are not equivalent to HRRR/GFS's UGRD/VGRD

NBM's core product has no `UGRD`/`VGRD` entries at all -- wind is published only as scalar speed (`WIND`) and direction (`WDIR`). Converting to u/v components for cross-source comparison would require an explicit trigonometric transform (`u = -speed*sin(dir)`, `v = -speed*cos(dir)`), which is not implemented; do not treat NBM wind and HRRR/GFS wind as directly comparable without doing so explicitly.

### F0 does not exist

`forecast_hour=0` is absent from NBM's schedule entirely (confirmed 404 at every run hour/date tested) -- there is no message to select and therefore no ambiguity, unlike HRRR's degenerate F0 APCP entry or GFS's `"anl"`-labeled F0 fields.

## GEFS

### No duplicate products -- unlike HRRR/GFS/NBM's APCP

GEFS's `pgrb2sp25` product has exactly ONE candidate per (variable, level) at every forecast hour, confirmed across 21 forecast hours (F3-F240), multiple members and run hours. `find_message()`'s exact-match lookup is safe here (not idx-ordering-dependent, since there is only ever one candidate to find) -- no `select_message_explicit`/`prefer` mechanism is needed, unlike every other GRIB source in this project.

### The single APCP/TCDC/DSWRF/TMAX/TMIN candidate's WINDOW varies by forecast hour

Since there's no duplicate to select between, the semantic challenge here is not selection but correct window PARSING: the same (variable, level) message's accumulation/average/period window changes with forecast hour, following one shared rule across all 5 of these fields:
- FH<=6: window=[0,FH] (cumulative since run start).
- FH>6: resets at every 6-hour synoptic mark: `window_start = 6*floor((FH-1)/6)`, `window_end = FH` (alternating 3h/6h windows, e.g. F009=[6,9], F012=[6,12], F015=[12,15]).

This formula is documentation only -- `data.gefs.parse_forecast_desc()` always parses the actual observed `forecast_desc` text (e.g. `"6-9 hour acc fcst"`), never computes the window from the formula. Do not assume GFS's "shortest-recent-window selected from duplicates" logic applies here; GEFS has no duplicates, just one candidate whose declared window shifts.

### F0: instant fields present, accumulation fields absent, TMAX/TMIN degenerate

TMP/DPT/RH/UGRD/VGRD/PRES ARE present and meaningful at F0 (`forecast_desc='anl'`, same convention as GFS's analysis-time label). APCP/TCDC/DSWRF are structurally absent at F0 entirely (0 idx entries -- not a selection ambiguity, there is no message). TMAX/TMIN at F0 use a degenerate `'0-0 day max/min fcst'` zero-width-window format (`lengthOfTimeRange=0`) that decodes to a real value equal to the instantaneous F0 TMP reading, NOT a genuine period max/min -- explicitly skipped in production code rather than fetched, consistent with HRRR's/GFS's other degenerate-F0 handling.

### Member identity is a first-class column, never collapsed

Every row carries `ensemble_member`/`member_type` -- the canonical processed dataset stores per-member rows, never a pre-aggregated mean/std/quantile summary. Member identity is independently cross-checkable from GRIB-internal metadata (`perturbationNumber`, `typeOfEnsembleForecast`) against the filename-derived member id, confirmed matching. Raw ensemble member frequencies (e.g. "X/31 members predict >=90F") are explicitly NOT calibrated probabilities.

### Grid is identical to GFS's -- confirmed, not assumed

`md5GridSection=45f3a4a8af23f33a77ab669d0fa1d813`, matching GFS exactly (both use the same NCEP 0.25deg global lat/lon grid). The distance threshold (20.0km) was independently re-derived from GEFS's own geometry and happens to equal GFS's value as a consequence, not an inheritance.

## ECMWF (deterministic, `oper` stream)

### Grid resolution matches GFS/GEFS but the fingerprint does NOT

`md5GridSection=265781b4edc06425746b46a5775244eb` -- a DIFFERENT fingerprint from GFS's/GEFS's `45f3a4a8af23f33a77ab669d0fa1d813`, despite the identical 0.25deg cell size, because ECMWF's `longitudeOfFirstGridPointInDegrees=180.0` differs from GFS/GEFS's `0.0` (a different global grid origin/alignment convention). Confirmed by direct decode, not assumed from matching resolution -- do not treat "same resolution" as "same grid."

### tp/ssrd are pure cumulative-since-run-start throughout the ENTIRE forecast horizon

Unlike GEFS's APCP (which resets every 6h synoptic mark beyond FH=6) or GFS's APCP (which offers a shortest-recent-window duplicate candidate), ECMWF's `tp` and `ssrd` have exactly ONE candidate, and its declared window is ALWAYS `[0, step]` -- confirmed at every forecast hour tested including F360, the full 15-day horizon. This is the same situation HRRR's original raw APCP was in before its APCP_1H backfill: a windowed "recent precipitation/radiation" signal would require differencing consecutive values, not implemented for ECMWF in this validation phase.

### mx2t3/mn2t3 are silently REPLACED by mx2t6/mn2t6 beyond F144

`mx2t3`/`mn2t3` (rolling 3-hour period max/min) exist only for forecast hours in the 3-hourly schedule regime (F3-F144). At F150 and beyond (the 6-hourly regime), they are structurally absent and replaced by `mx2t6`/`mn2t6` (a symmetric rolling 6-hour window). This was caught during the production-readiness validation sample -- an initial extraction naively requesting only `mx2t3`/`mn2t3` silently produced zero rows for the three long-horizon items tested, discovered only by inspecting per-item variable coverage rather than trusting a "0 errors" result. Fixed by selecting the param name based on which schedule regime the forecast hour falls in.

### F0: instant fields real, accumulation fields degenerate

2t/2d/10u/10v/sp are present and meaningful at F0. `tp` at F0 has a zero-width declared window (`startStep=endStep=0`) and decodes to 0.0 -- not a real accumulation. `mx2t3`/`mn2t3` at F0 are even more subtly degenerate: they carry the SAME declared window as the eventual real F3 message (`[0,3]`) but decode to **0.0 Kelvin** -- a physically impossible temperature (absolute zero), an unambiguous placeholder/fill value rather than a genuine reading. All three are explicitly skipped at forecast_hour=0.

### No native 2m relative humidity or cloud-cover field exists

Confirmed by exhaustively listing every distinct `param` across the full message index: pressure-level relative humidity (`r`) exists, but no `2r`/surface-level RH field does; no `tcc`/`hcc`/`mcc`/`lcc`/`cc` (cloud cover, any layer) exists at any level. This is a genuine, permanent structural gap versus every NOAA source in this project (all of which have native 2m RH) -- not derived via a computed transform from 2t/2d, consistent with this project's established preference for native fields over derived ones.

### Availability: a bulk archive-sync event, not genuine progressive dissemination

Every forecast hour of a single run (F0 through F360) shares an S3 Last-Modified timestamp within a 37-59 second window, at an almost exactly reproducible 514-minute (~8.57h) lag from run_time -- confirmed across 8 different (date, run_hour) combinations, never varying by more than a minute. Unlike HRRR/GFS/NBM/GEFS (where lag genuinely grows with forecast hour, reflecting real progressive model computation and release), ECMWF's lag here shows essentially zero forecast-hour dependence. This is classified LOW CONFIDENCE as a per-forecast-hour availability signal (it cannot distinguish "F0 available" from "F360 available"), though it is a highly reliable conservative floor. See `point_in_time.md` and `ecmwf_pilot_readiness.md` sections 19-21 for full detail.
