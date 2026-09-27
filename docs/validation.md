# Validation Checklist

The standard checklist required before declaring `SOURCE PILOT: FINAL PASS`. Split into two categories that must **both** pass independently -- this split exists because HRRR's APCP passed every structural check while still having an unresolved scientific/semantic issue (see `decisions.md`).

## Structural validation

Verifies the pipeline ran correctly and every byte was handled cleanly. Does **not** verify that the scientifically intended product was selected.

- Planned work items (independently recomputed from the selected-day list and the source's own schedule, not just read from a report)
- DONE count
- FAILED count
- Missing count (planned but absent from checkpoint entirely)
- Duplicate checkpoint entries (raw JSON key duplication)
- Parquet part count
- Total rows
- Expected rows per work item (variables x patch points; account for any variable legitimately absent at some forecast hours, e.g. GFS APCP/DSWRF at fh=0)
- Zero-byte files
- Corrupt/unreadable Parquet files
- Schema consistency across all parts
- Full-row duplicates
- Logical-key duplicates (source, run_time, valid_time, forecast_hour, variable, requested patch point)
- Checkpoint-row-sum <-> persisted-row reconciliation
- Grid fingerprint (must match the source's own previously-validated value; a NEW source/city should expect a NEW fingerprint, not the same one)
- Max grid distance vs. the source's own derived threshold
- Cache invalidations (periodic revalidation mismatches -- expect 0; any nonzero count needs investigation, not dismissal)
- Cached-vs-uncached equivalence (bit-exact index/value match on an independent sample)
- Decode failures
- Temp-file leaks (before AND after a real multi-worker run, not just a single-threaded test)
- Variable coverage (every intended variable present at every forecast hour where it should legitimately exist)
- Run/forecast-hour/date coverage (spans the full selected-day set, all run hours, the full forecast-hour schedule)

## Scientific / semantic validation

Verifies that what was extracted actually means what it's assumed to mean. This is the category that HRRR's original APCP work skipped, and it is not optional.

- Temporal semantics explicitly determined and recorded for every variable (instant / accum / average, and window length) -- never left as "whatever the first idx match happened to be"
- Duplicate-product selection explicitly verified: for every variable with more than one idx candidate, confirm which one is selected, WHY (by an explicit rule, not idx position), and that the rule is fail-loud when it can't uniquely resolve
- Availability timestamp coverage AND semantics (not just "is the column populated" but "does it actually mean what we say it means" -- see `point_in_time.md`'s discussion of the S3 Last-Modified proxy's limitations)
- No-lookahead behavior (every row's resolved availability is provably `<= t` for any query time `t` it's used at)
- Regression check against any previously-frozen source or dataset in the same repository (confirm nothing else silently changed)

## Why both categories are required

The HRRR APCP investigation is the concrete example: the original 20,836-item pilot passed every structural check in this document (zero corruption, zero duplicate rows, zero missing work items, exact row reconciliation, a single stable grid fingerprint, zero decode failures) and was declared `FINAL PASS` on that basis. It was still recording the wrong precipitation product relative to what most downstream uses would assume ("APCP" without qualification reads as "recent precipitation," but the frozen column is cumulative-since-run-start). No structural check could have caught this -- it required deliberately investigating the idx's duplicate-candidate behavior and comparing extracted values against the source's own alternative product. **A `FINAL PASS` on structural validation alone is not sufficient to declare a source ready; both categories must pass.**
