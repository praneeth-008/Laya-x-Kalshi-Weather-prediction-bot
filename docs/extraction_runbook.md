# Extraction Runbook

The reproducible procedure this project actually followed for HRRR and GFS, generalized for the next source or city. Follow this in order; do not skip steps because a step "probably" behaves the same as a previous source -- HRRR and GFS disagreed on almost every one of these (see `data_sources.md`).

1. **Feasibility test.** A single-day, single-purpose script (`scripts/test_{source}_nyc_feasibility.py`) that lists the archive structure, confirms run/product schedule, fetches a handful of real messages, and records raw observations. No pilot-scale extraction yet.
2. **Inspect native products.** For every intended variable, list every idx candidate under its `(variable, level)` key -- do not assume one entry per key. This is how HRRR's and GFS's APCP/TCDC duplicate-product issues were found, not by anticipating them.
3. **Identify duplicate variable products.** If more than one candidate exists, determine *why* (genuinely different products vs. a file-generation artifact -- verify with GRIB metadata like `startStep`/`endStep`/`stepRange`/`typeOfStatisticalProcessing`, not just the idx text).
4. **Explicitly define temporal semantics.** For every variable, decide and document: instantaneous, or accumulated/averaged over what window. Never rely on idx ordering to pick between candidates -- write an explicit, fail-loud selector (`select_message_explicit()` pattern) that raises when the intended product can't be uniquely identified.
5. **Verify grid geometry.** Decode a real message and read `gridType`, `Ni`/`Nj`, and either `md5GridSection` or the relevant projection parameters. Do not assume a new source's grid resolution or projection matches a previously-validated source.
6. **Establish a source-aware distance threshold.** Derive it from the source's own grid cell geometry (e.g. GFS: `0.5 * sqrt(cell_lat_km^2 + cell_lon_km^2)` for a regular lat-lon grid), not by reusing another source's value. This is a safety ceiling on plausibility, never a mechanism for choosing between grid points.
7. **Validate the availability timestamp mechanism.** Confirm what `Last-Modified` (or equivalent) actually reflects for this source (whole-file vs. per-message), and measure the typical lag empirically rather than assuming it matches another source.
8. **Test cached vs. uncached extraction.** Before trusting the grid-index cache for a new source/grid, run bit-exact equivalence tests: index mismatches, value mismatches, max abs diff, across multiple dates/runs/forecast-hours/variables. Independently re-derive fingerprints and indices rather than trusting the cache's own bookkeeping.
9. **Run a small representative sample.** A handful of real work items (not synthetic), spanning multiple dates/run-hours/forecast-hour regimes (including boundary cases like the very first and very last forecast hour) and both active and quiet conditions where relevant (e.g. precipitation vs. none).
10. **Validate checkpoint/resume.** Run a tiny multi-item batch twice: confirm the second run does zero new work, and that Parquet part counts don't change (no regeneration).
11. **Validate multiprocessing.** Confirm the actual production `run_concurrent`/`Checkpoint` calling convention works end-to-end -- a stale calling convention (mismatched signature, an unpicklable argument) will crash silently until actually exercised. This exact failure mode was found in `scripts/pilot_phase4_gfs.py` before GFS's pilot could run.
12. **Check temp-file leakage.** Decode a batch of real messages and confirm zero leaked temp files afterward, especially on Windows (a temp-file-based GRIB handle was found to leave its OS-level lock held past `codes_release()`, making cleanup fail 100% of the time -- fixed by decoding directly from the in-memory byte range instead).
13. **Launch the representative-day pilot**, not the full historical archive (see "why not immediately download years of data" below).
14. **Final integrity audit** -- the full checklist in `validation.md`, both structural and scientific/semantic.
15. **Freeze the source only after FINAL PASS -- and only after generating and committing its dataset manifest.** Do not treat "no errors during the run" as sufficient -- the HRRR APCP discovery passed every structural check while still having a real semantic issue undetected until specifically investigated. **A source pilot is not considered frozen until its manifest exists and is committed** (see "Mandatory manifest rule" below).
16. **Scale historical extraction in validated blocks**, only after a full pilot has reached FINAL PASS and any discovered issues (like HRRR APCP) have been resolved or explicitly accepted -- never extend an unresolved pilot's scope while an open question remains.

## Mandatory manifest rule

**Every future source pilot and historical extraction block must generate a dataset manifest (see `reproducibility.md` for the schema) before being declared `FINAL PASS`.** No exceptions -- this closes the exact reproducibility gap that made the HRRR and GFS manifests need to be reconstructed after the fact instead of recorded exactly.

Preferred workflow, in order:

```
validated code
    -> commit
    -> record the commit hash
    -> run extraction
    -> validate (validation.md checklist)
    -> freeze the dataset manifest (reproducibility.md schema)
    -> commit the manifest/report
```

Recording the commit hash **before** extraction, not reconstructing it afterward, is what makes provenance exact rather than "partially reconstructed." Both the HRRR APCP_1H backfill and part of the GFS pilot (the 132 forecast_hour=0 retries, after the `"anl"` fix) were extracted against code that was uncommitted at the time -- their manifests had to say so honestly rather than claim a false exact commit. Following the order above avoids that gap going forward.

Manifests are lightweight (a single JSON file per dataset) and are committed to Git; the bulk processed Parquet output is never required to be committed (see `.gitignore`'s per-source `manifest.json`-only exceptions under `data/processed/pilot/`).

## Why we do not immediately download years of data

Two independent reasons, both already applied in this project:

1. **Cost/scale.** `bulk_storage_estimate.md`'s own numbers show full-archive extraction (multi-year, all sources) running into hundreds of GB to multiple TB depending on scenario -- committing to that scale before validating correctness would multiply the cost of any mistake by however many years were already downloaded.
2. **Correctness-before-scale.** Every edge case found so far (HRRR's APCP duplicate, HRRR's F0 degenerate case, GFS's APCP/TCDC duplicates, GFS's F0 `"anl"` format) was discovered during a 40-representative-day pilot, not during feasibility testing on a single day. A pilot at representative scale is what actually surfaces these issues cheaply; skipping straight to full-history extraction would have multiplied every one of these bugs across years of data before anyone noticed.

The representative-day pilot (40 selected days, systematic stratified sampling across season/temperature/weather-regime buckets -- see `scripts/pilot_select_representative_days.py`) exists specifically to surface these issues at a small, cheap, quickly-re-extractable scale before committing to full-history extraction.
