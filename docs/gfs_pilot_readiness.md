# GFS Pilot Production-Readiness — Decisions & Findings

Status: production-readiness fixes implemented and tested at small scale. **Full 40-day pilot (5,836 work items) has NOT been run.**

## 1. Grid-index caching

Validated safe for GFS with zero code changes to the caching mechanism itself (`data/pilot_extraction.py`'s cache is generic, keyed by a live `md5GridSection` fingerprint plus the requested patch points). GFS's fingerprint (`md5:45f3a4a8af23f33a77ab669d0fa1d813`, grid `regular_ll` 1440x721 0.25deg) is independently established and distinct from HRRR's (`md5:78367561440d7c7b608b8532a02e4780`, Lambert Conformal ~3km). Cached-vs-uncached: 0 index/value mismatches across 40+ real messages spanning 5 dates/runs/forecast-hours x 9 variables, bit-exact.

## 2. Source-aware distance sanity threshold

`decode_message_multi_point()` now accepts `max_distance_km` (default 10.0, unchanged for every existing HRRR call site). GFS passes its own derived constant, `data.gfs.MAX_PATCH_DISTANCE_KM = 20.0`.

**Derivation**: GFS's 0.25deg regular_ll grid at Central Park's latitude (~40.78N) has cell dimensions ~27.75km (lat, latitude-independent) x ~21.0km (lon, scaled by cos(40.78deg)). The theoretical worst-case nearest-grid-point distance for any query point is half the cell diagonal: `0.5 * sqrt(27.75^2 + 21.0^2) ~= 17.4km`. 20km gives ~15% margin above that theoretical worst case — enough to never falsely reject a legitimately correct GFS mapping, while still tight enough to catch a genuinely wrong grid (e.g. a stale/incorrect fingerprint mapping to a point tens of km away). This is a plausibility ceiling, not a mechanism for choosing between candidate grid points.

HRRR's existing 10km threshold is unchanged and was re-verified via the full HRRR regression suite (identical fingerprint, identical cached indices, all 10 safety-mechanism tests still pass).

## 3. Explicit APCP / TCDC product selection

GFS publishes **multiple distinct products** under the same `(variable, level)` .idx key for these two variables (verified: no other CORE_VARS entry has this issue). The previous code (`find_message()`, first-match) relied on idx ordering — this is now replaced for APCP/TCDC by `select_message_explicit()` in `data/gfs.py`, which parses every candidate's `forecast_desc` and selects based on an explicitly stated product-semantic rule, never ordering.

**APCP** — intended semantic: *a period/window precipitation quantity representing recent precipitation*, not a cumulative-since-run-start total (which would conflate all precipitation since forecast init and lose the ability to see whether precipitation is currently ongoing). Rule: among candidates whose accumulation window ends at the target forecast hour, select the one with the **smallest window**. Verified across F1, F6, F12, F18, F23, F24, F25, F48, F72, F120, and F123 (the >F120 3-hourly regime) — correctly selects the windowed product at every forecast hour.

**TCDC** — GFS provides both an instantaneous variant (`"N hour fcst"`) and a period-averaged variant (`"N-M hour ave fcst"`) at every forecast hour tested. We select the **instantaneous** variant, consistent with HRRR's own instantaneous TCDC and this project's point-in-time `weather_state(t)` philosophy (an atmospheric-state snapshot, not a running average). Verified across the same forecast-hour set.

**The "duplicate" mechanism, explained**: at forecast_hour <= 6, APCP's windowed-since-last-reset and cumulative-since-start definitions are mathematically identical (both cover `[0, fh]`), and GFS's pipeline emits this as **two separate GRIB messages** (different byte offsets) rather than one. Direct GRIB metadata inspection (startStep/endStep/stepRange/typeOfStatisticalProcessing) and value comparison confirmed these two messages are byte-for-byte identical in content — a file-generation artifact, not a second distinct product. `select_message_explicit()` treats a tie between candidates sharing the same accumulation window as safe; a tie between candidates with **different** windows still fails loudly (`RuntimeError`, with full diagnostic detail), matching the requirement that ambiguity must never be silently resolved.

## 4. DSWRF semantic representation

GFS's DSWRF is **always a running average** over an accumulation window (no instantaneous variant exists in this product at any forecast hour) — a genuinely different physical quantity from HRRR's instantaneous DSWRF, despite sharing the same variable code.

**Chosen representation** (least disruptive to the existing schema, per the explicit options considered): two new row-level metadata columns, populated for every GFS row (not just DSWRF):
- `temporal_stat`: `"instant"` | `"accum"` | `"average"` (derived from the message's own parsed `forecast_desc`)
- `temporal_window_hours`: `0` for instantaneous rows; the accumulation/average window length otherwise

This was **not** implemented as a renamed variable (e.g. `DSWRF_avg`) to avoid breaking cross-source variable-name consistency, and **not** implemented as a new schema version, since it's purely additive.

**HRRR is not modified.** The existing 1,500,192-row frozen HRRR dataset does not have these columns. Downstream consumers must treat their absence, for `source="hrrr"` rows, as the implicit convention: `TMP/DPT/UGRD/VGRD/PRES/TCDC/DSWRF` = instantaneous, **except APCP, which is NOT instantaneous and requires the separate investigation below.**

## 5. RH

Investigated: RH is directly available and uniquely selectable in GFS at `2 m above ground` (`shortName=2r`, `units=%`, `stepType=instant`) with no duplicate-entry ambiguity — the same level string already used for TMP/DPT. No scientific or technical reason was found for its earlier exclusion from `CORE_VARS`; it was very likely dropped only to match HRRR's variable list. Per instruction, added to GFS's `CORE_VARS` only (not to HRRR, preserving explicit source identity). Cached-vs-uncached: 0 mismatches across 4 dates/forecast-hours (36 points), bit-exact. RH is also directly available in HRRR (verified, unique entry, not added there).

## 6. CRITICAL — HRRR's own APCP has the same duplicate-entry issue, unresolved

While investigating GFS's APCP duplicates for comparison, the same investigation was applied read-only to HRRR's own `.idx` files (no HRRR code or data touched). **HRRR's APCP also has two idx entries at every forecast hour beyond F1** — but in the OPPOSITE order from GFS:

```
HRRR fh=6:  ['0-6 hour acc fcst', '5-6 hour acc fcst']
HRRR fh=12: ['0-12 hour acc fcst', '11-12 hour acc fcst']
HRRR fh=24: ['0-1 day acc fcst', '23-24 hour acc fcst']
```

HRRR's `find_message()` (in `data/hrrr.py`, a separate function from GFS's, untouched by this phase) also takes the first idx match — which here is the **cumulative-since-run-start** entry, not the 1-hour windowed one. This means the already-committed, "FINAL PASS" 20,836-item HRRR dataset's APCP column is very likely recording cumulative precipitation since each run's own start, not a recent/windowed signal — the same class of problem this phase fixed for GFS, just silently going the other way for HRRR because idx ordering happened to differ.

**This was NOT part of the approved scope for this phase and has NOT been fixed.** No HRRR code or data was modified. This needs its own explicit decision: whether to accept HRRR's APCP as "cumulative since run start" (a legitimate, well-defined quantity, just not the one likely assumed), add a corresponding fix + documented semantic column to HRRR (would not require re-running the pilot, only reinterpreting/relabeling existing data if the underlying values are already usable), or re-extract HRRR's APCP with an explicit selector mirroring GFS's fix. **Flagging for your decision, not resolving.**

## 7. Unresolved items carried forward

- HRRR's APCP semantic issue (above) — highest-priority open item.
- `data/hrrr.py`'s own `decode_message()` (single-point, used only by old feasibility test scripts, not the production path) still uses the temp-file approach and was not touched.
- No downstream consumer code (weather_state.py, model input builders) has been updated to read `temporal_stat`/`temporal_window_hours` yet — these columns exist in the schema but nothing consumes them downstream so far.
