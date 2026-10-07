# Decision Log

ADR-style record of the significant decisions made during this project so far. Dates are commit/session dates from git history where the decision was implemented; a few foundational decisions predate detailed dated tracking and are marked as such.

---

### Central Park / KNYC is the NYC weather target
- **Date**: project inception (2026-09-26/27, `config/bulk_download_spec.json`)
- **Decision**: Central Park, ISD station `725053-94728` (ICAO KNYC), is the primary forecast target for NYC daily maximum temperature.
- **Why**: it is the canonical, continuous, well-documented station for the area, and is the natural reference point for a "NYC daily high" market.
- **Alternatives**: LaGuardia, JFK, Newark (all three were kept as auxiliary predictor stations instead).
- **Consequences**: all numerical-model grid extraction targets Central Park's coordinates (40.7794, -73.9691); KLGA/KJFK/KEWR remain predictor-only, never the target.

### Auxiliary NYC stations remain separate, predictor-only
- **Date**: project inception
- **Decision**: KLGA, KJFK, KEWR are never merged into or treated as substitutes for the KNYC target series.
- **Why**: each station has its own microclimate; conflating them with the target would corrupt the target definition.
- **Alternatives**: an averaged "NYC-area" composite target -- rejected.
- **Consequences**: `data/observations.py::STATIONS` tags each station's `role` explicitly (`PRIMARY_TARGET_LOCATION` vs. `predictor`).

### The weather model is trained independently of Kalshi prices
- **Date**: project inception (`README.md`)
- **Decision**: Kalshi market prices are never used as a training input or feature for the probabilistic Tmax model.
- **Why**: the research question is whether the model finds information not already reflected in Kalshi prices -- using those prices as training input would make the question circular.
- **Alternatives**: joint training on weather + price data -- rejected as answering a different (and less interesting) question.
- **Consequences**: Kalshi prices are only introduced downstream, at the trading/economic-evaluation layer (see `architecture.md`).

### Event/day is the evaluation unit
- **Date**: project inception
- **Decision**: the model's unit of evaluation is a single NYC calendar day's maximum temperature, matching the Kalshi `KXHIGHNY` market structure.
- **Why**: matches the actual tradeable instrument.
- **Alternatives**: continuous temperature nowcasting -- not the target application.

### Point-in-time integrity is mandatory
- **Date**: project inception, reinforced throughout HRRR/GFS work (2026-09-27)
- **Decision**: every stored row must carry `run_time`, `valid_time`, and `available_time` separately, and no historical state reconstruction may use information not provably available by the query time.
- **Why**: a backtest that can see future information (a later forecast revision, an eventual observed outcome, a forecast run before it was actually published) is not a valid backtest.
- **Alternatives**: simplifying to run_time-only timestamps -- rejected explicitly and early (see `data/pilot_extraction.py`'s "CORE RULE" docstring).
- **Consequences**: `eligible_mask()` and `validate_no_lookahead()` in `data/weather_state.py`; the entire availability-proxy discussion in `point_in_time.md`.

### Incomplete runs cannot become model-facing
- **Date**: project inception, exercised concretely by the laptop-crash HRRR interruption (2026-09-27)
- **Decision**: a numerical model run is only usable once every expected target-day valid time has arrived; partial runs are excluded, never averaged/interpolated/substituted.
- **Why**: using partial data would bias predictions in a way that depends on extraction timing, not meteorology, and would make backtests non-reproducible.
- **Consequences**: `assess_deterministic_run_completeness()` / `assess_ensemble_run_completeness()`, `latest_seen_run` vs. `latest_usable_run` split.

### S3 Last-Modified is a proxy, not exact publication time
- **Date**: established during HRRR/GFS feasibility testing, reaffirmed in `point_in_time.md` (2026-09-27)
- **Decision**: treat `Last-Modified` as an observed proxy for availability, never as a documented guarantee, and mark ECMWF's confidence as LOW given its much larger observed lag (~8.6h avg).
- **Why**: it reflects whole-file write time, not the specific byte range fetched, and is not a documented provider commitment.
- **Alternatives**: treating run_time as available_time (rejected outright, see above); waiting for a documented publication-time field (does not exist for these sources).

### HRRR APCP cumulative is retained (not discarded or "fixed in place")
- **Date**: 2026-09-27
- **Decision**: after discovering the frozen pilot's `"APCP"` column is cumulative-since-run-start (not the recent-window product likely assumed), the existing values were kept as-is rather than deleted or silently reinterpreted.
- **Why**: the data is legitimate and well-defined; the issue was a naming/assumption gap, not a data-quality defect. Re-extracting or discarding validated data unnecessarily would waste already-correct work.
- **Alternatives**: delete and re-extract APCP entirely -- rejected as unnecessary once the direct windowed product could be added additively.

### Direct HRRR APCP_1H added, as a separate additive feature
- **Date**: 2026-09-27
- **Decision**: fetch HRRR's own direct 1-hour windowed accumulation product and store it as a new `APCP_1H` feature, alongside (not replacing) the cumulative `APCP`.
- **Why**: both cumulative and recent-window precipitation are independently useful signals for a Tmax model (see `variable_semantics.md`); only one was originally captured.
- **Consequences**: a new, separately-checkpointed, additive dataset (`data/processed/pilot/hrrr_apcp_1h/`), never touching the frozen original.

### HRRR F0 APCP_1H is N/A, not zero
- **Date**: 2026-09-27
- **Decision**: forecast_hour=0 work items are excluded from the APCP_1H work-item universe before extraction, rather than attempted and either failed or assigned a fabricated `0.0`.
- **Why**: there is no preceding forecast interval at F0 from which a genuine 1-hour accumulation could be defined -- this is a structural impossibility, not missing data.
- **Alternatives**: assign `0.0` (rejected -- would misrepresent "undefined" as "measured zero precipitation"); mark as checkpoint FAILED (rejected -- implies retriable, but retrying can never succeed).

### Direct APCP_1H preferred over cumulative differencing
- **Date**: 2026-09-27
- **Decision**: never construct APCP_1H by subtracting consecutive cumulative values; always fetch the direct NOAA product.
- **Why**: empirically tested -- differencing matched the direct product in only 10 of 19 tested consecutive-hour pairs (53%), with real (non-floating-point-noise) discrepancies of 0.001-0.004 kg/m^2 caused by independent GRIB packing/quantization of each accumulation-window message.
- **Alternatives**: differencing (rejected, would have avoided new downloads but sacrificed exactness).

### Explicit GRIB-product selection rather than idx ordering
- **Date**: 2026-09-27
- **Decision**: wherever a `(variable, level)` key has more than one idx candidate (HRRR APCP; GFS APCP, TCDC), select by parsing `forecast_desc` against an explicitly-stated rule, and fail loudly if the rule can't uniquely resolve -- never rely on which candidate the idx happens to list first.
- **Why**: idx ordering happened to select the *wrong* product for HRRR's APCP and the *right* one for GFS's APCP/TCDC, purely by coincidence of file layout -- proving that "it currently works" was never evidence that the selection was principled.
- **Consequences**: `select_message_explicit()` in both `data/hrrr.py` and `data/gfs.py`.

### Source-aware grid-distance thresholds
- **Date**: 2026-09-27
- **Decision**: the distance sanity-check ceiling is derived per-source from that source's own grid geometry (HRRR: 10km; GFS: 20km), not shared as one global constant.
- **Why**: HRRR's ~3km grid and GFS's ~28km grid have very different theoretical worst-case nearest-point distances (~a few km vs. ~17.4km); a single threshold calibrated for one would either be too loose for HRRR or falsely reject legitimate GFS mappings.
- **Consequences**: `data/hrrr.py::MAX_PATCH_DISTANCE_KM = 10.0`, `data/gfs.py::MAX_PATCH_DISTANCE_KM = 20.0`, threaded through `decode_message_multi_point(max_distance_km=...)`.

### HRRR/GFS DSWRF temporal semantics differ
- **Date**: 2026-09-27
- **Decision**: document (not force-align) that HRRR's DSWRF is instantaneous while GFS's DSWRF is a running average -- the same variable code, genuinely different physical quantities.
- **Why**: GFS's `pgrb2.0p25` product has no instantaneous DSWRF variant at all; forcing semantic equivalence would misrepresent the GFS feature.
- **Consequences**: `temporal_stat`/`temporal_window_hours` columns added to GFS output to make this explicit downstream.

### GFS F0 "anl" decision
- **Date**: 2026-09-27
- **Decision**: recognize `forecast_desc="anl"` as GFS's analysis-time (forecast_hour=0) label for instantaneous fields, mapped to `{kind: instant, end_hour: 0}`; APCP and DSWRF (which have zero idx entries at F0) are gracefully skipped for that one work item, not failed.
- **Why**: discovered via a real production run (132 initial failures, all `"Unrecognized GFS forecast_desc format: 'anl'"`, all at fh=0); GFS's own convention never uses `"anl"` for any other forecast hour, so the mapping is not a guess.
- **Consequences**: `parse_forecast_desc()` in `data/gfs.py`; all 132 retried successfully after the fix, final GFS pilot state 5,836/5,836 DONE.

### Numerical weather data will be fed as structured numerical features, not converted to text
- **Date**: project design intent (implicit throughout; stated explicitly here for the record)
- **Decision**: HRRR/GFS/etc. values remain structured numerical columns (value, units, temporal metadata) for model consumption -- they are not summarized or converted into natural-language text (unlike AFD, which is explicitly kept as raw text with no LLM processing).
- **Why**: numerical models should consume numerical features directly; text conversion would lose precision and add an unnecessary, unvalidated transformation step.

### Pilot data is pipeline validation, not final model training data
- **Date**: project inception, reaffirmed throughout
- **Decision**: the 40-selected-day pilot exists to validate the extraction pipeline's correctness, not to serve as the final training dataset for the Tmax model.
- **Why**: a systematic-but-small stratified sample is enough to surface edge cases cheaply (see `extraction_runbook.md`); full-history extraction is a separate, later decision to be made only after the pipeline is validated.
- **Consequences**: no model training should begin against the current 40-day datasets as if they were the intended final training set without an explicit decision to do so.

### NBM's forecast-hour schedule is run-hour dependent, not fixed
- **Date**: 2026-09-27
- **Decision**: corrected the assumption (in `data/nbm.py`) that every NBM run shares one schedule out to F264. `data/weather_state.py`'s NBM completeness branch was fixed in a follow-up pre-production phase (same date) to delegate to `data.nbm.available_forecast_hours(run_hour)` rather than carry its own duplicate, hardcoded schedule.
- **Why**: empirically verified across all 24 run hours -- a 3-tier split keyed by `run_hour % 6` (full/medium/short), with most run hours (12 of 24) never reaching past F36 at all. The completeness branch is fixed to reuse the extraction module's schedule rather than duplicate it, so there is exactly one source of truth.
- **Alternatives**: none -- this is a factual correction, not a design choice.
- **Consequences**: `data/nbm.py::available_forecast_hours(run_hour)` is now run-hour-aware; `data/weather_state.py::_forecast_hour_candidates()`'s NBM branch now calls it directly. 31 targeted tests (all 3 tiers, boundary forecast hours F35/36/37/39/189/192/195/198/264/265, latest-seen-vs-latest-usable no-lookahead behavior) pass; HRRR/GFS completeness regression-checked as unaffected.

### NBM worker count benchmarked independently at 20
- **Date**: 2026-09-27
- **Decision**: use 20 workers for the full NBM pilot (not GFS's or HRRR's counts, and not assumed to transfer).
- **Why**: NBM's workload is network-latency-bound rather than CPU-bound -- CPU usage never exceeded ~41% average even at 24 workers, unlike HRRR's clear CPU-saturation peak. Throughput across 8-24 workers was flat (~1.4-1.8 items/sec) with zero errors and no S3 throttling at any tested count; 20 was the highest *stable* value across repeated trials, not a sharply-optimal peak.
- **Alternatives**: 24 (marginally lower average throughput, more CPU with no benefit); 12 (comparable throughput, less headroom).
- **Consequences**: full pilot launch should use `MAX_WORKERS=20`; the flat curve means this choice is not highly sensitive, so it is not worth further tuning before launch.

### NBM APCP_1H and APCP_6H are both retained; no cumulative-since-start product exists
- **Date**: 2026-09-27
- **Decision**: extract both NBM precipitation windows (1-hour, always present; 6-hour, present at synoptic-aligned forecast hours) as separate features, and explicitly exclude probability-of-exceedance products from the amount extraction.
- **Why**: both windows carry distinct information; NBM's amount product structurally has no cumulative-since-run-start candidate to select even if wanted (unlike HRRR/GFS).
- **Consequences**: `APCP_1H`/`APCP_6H` as separate canonical features; `APCP_6H` is legitimately absent (not failed) at most forecast hours.

### NBM direct TMAX/TMIN retained alongside hourly TMP
- **Date**: 2026-09-27
- **Decision**: retain both NBM's hourly `TMP` and its direct 12-hour period `TMAX`/`TMIN` products as separate features.
- **Why**: empirically confirmed a 1.46F difference between direct TMAX and max(hourly TMP) over the same window -- not measurement noise, and consistent with TMAX being genuinely post-processed rather than a simple reconstruction.
- **Consequences**: `TMAX_PERIOD`/`TMIN_PERIOD` extracted only for run_hour in {0, 12} (the only run hours that publish them), with run-hour-dependent max/min alternation handled explicitly, never assumed.

### Full 40-day NBM pilot: FINAL PASS, with a caught-and-fixed data-hygiene issue
- **Date**: 2026-09-28
- **Decision**: launched and completed the full 31,578-item, 40-day NBM pilot at commit `cb09e8f` (plus an uncommitted-then-committed script fix removing the deliberate launch guard). Declared FINAL PASS after the integrity audit.
- **Why**: all pre-production checks passed (see `nbm_pilot_readiness.md` sections 1-17); the audit found 0 duplicates, 0 corrupt files, exact checkpoint/persisted-row reconciliation, grid/distance/F0/APCP_6H/TMAX-TMIN semantics all verified at full production scale.
- **A finding along the way**: the post-run audit caught 9 stray checkpoint entries (648 rows) left over from an earlier validation-phase small sample that had written into the same output directory before this launch. This was investigated (root cause: shared output path across two separate runs, not an extraction defect -- checkpoint/resume itself behaved correctly, with 0 duplication of the 3 items that legitimately overlapped), cleaned (stray rows removed from the one affected Parquet part, stray checkpoint keys deleted), and re-verified before the manifest was written. See `nbm_pilot_readiness.md` section 18 and the manifest's provenance notes.
- **Consequences**: `data/processed/pilot/nbm/manifest.json` is the production provenance record; future pilots should use a dedicated output directory per validation phase rather than reusing the eventual production path, to avoid this class of contamination recurring.

### GEFS: no duplicate-product selection needed; window varies instead
- **Date**: 2026-09-28
- **Decision**: use plain `find_message()` (exact var/level match) for every GEFS variable rather than building a `select_message_explicit`/`prefer` mechanism like HRRR/GFS/NBM's APCP.
- **Why**: empirically confirmed across 21 forecast hours, multiple members/run hours -- GEFS's `pgrb2sp25` product has exactly ONE candidate per (variable, level) at every forecast hour. What varies instead is the single candidate's declared accumulation/average/period window (cumulative-since-start for FH<=6, then resets every 6h synoptic mark). `parse_forecast_desc()` parses this directly from the observed text; no formula is hardcoded into extraction.
- **Consequences**: simpler extraction code for GEFS than for HRRR/GFS/NBM's APCP; the correctness burden shifts entirely to correct window PARSING rather than product SELECTION.

### GEFS distance threshold independently derived, found identical to GFS's
- **Date**: 2026-09-28
- **Decision**: derive `MAX_PATCH_DISTANCE_KM=20.0` for GEFS from its own grid geometry, not copied from GFS.
- **Why**: GEFS's `pgrb2sp25` grid was independently decoded and its `md5GridSection` fingerprint (`45f3a4a8af23f33a77ab669d0fa1d813`) found to be genuinely identical to GFS's -- both use the same NCEP 0.25deg global lat/lon grid. The independently-derived worst-case-distance formula applied to this confirmed-identical geometry naturally produces the same 20km value GFS uses.
- **Consequences**: the equality is a confirmed fact about the archive, not an assumption carried over between sources.

### GEFS ensemble completeness: conservative complete-member rule, no code change needed
- **Date**: 2026-09-28
- **Decision**: keep the existing `assess_ensemble_run_completeness` logic in `data/weather_state.py` (built in an earlier architecture phase, before this session) unchanged -- a GEFS run is usable only when all 31 members have all expected target-day valid times present.
- **Why**: 14 targeted tests (schedule sanity, full-completeness, one-member-missing, one-valid-time-missing, newer-partial-run-does-not-replace-older-complete-run, latest_seen-advances-independently, no-lookahead, member-distribution-preserved) all passed against this pre-existing code once independently validated against the real archive's confirmed 3-hourly F0-F240 schedule. No evidence was found to justify relaxing to a partial-member rule for the pilot.
- **Consequences**: `data/weather_state.py` required no GEFS-related changes in this phase, unlike NBM's completeness gap which did need a fix.

### GEFS work-item granularity: (run, member, forecast_hour)
- **Date**: 2026-09-28
- **Decision**: use `(run_time, ensemble_member, forecast_hour)` as the checkpoint/work-item key for the GEFS pilot extraction, rather than fetching all 31 members inside one `(run, forecast_hour)` worker call.
- **Why**: the archive itself partitions storage this way (one GRIB2 file per member per forecast hour); matching that partitioning keeps one idx-fetch-plus-byte-ranges per work item (same efficiency as every other source) while giving per-member checkpoint granularity and independent retry -- a single slow/failed member never blocks an entire run's other 30 members.
- **Consequences**: ~62,744 work items for the full 40-day pilot (vs. HRRR's 20,836 / GFS's 5,836 / NBM's 31,578) -- roughly 2x NBM's count, reflecting the 31-member multiplier offset by GEFS's coarser 3-hourly cadence.

### GEFS completeness test: fixed a case-sensitivity typo, no production change
- **Date**: 2026-09-29
- **Decision**: fixed `scripts/test_gefs_completeness.py`'s one assertion that checked for the lowercase substring `"not calibrated probabilities"` against production's actual (correct, capitalized-for-emphasis) string `"...are NOT calibrated probabilities."` -- the assertion never matched, producing a spurious FAIL unrelated to any real GEFS behavior. Promoted this test from an ephemeral scratch script into the committed repository (`scripts/test_gefs_completeness.py`) as part of the pre-backfill green test baseline.
- **Why**: the other 13 checks in this suite (schedule, completeness, atomic switch, no-lookahead, member-distribution preservation) all exercise real logic and passed throughout; only this one string-matching assertion was wrong. `data/weather_state.py`'s GEFS completeness/ensemble-state logic, the 31-member requirement, forecast schedule, and availability semantics were NOT touched.
- **Consequences**: GEFS completeness suite now 14/14 (previously reported 13/14). No GEFS frozen data or methodology changed.

### Full 40-day GEFS pilot: FINAL PASS, no contamination this time
- **Date**: 2026-09-28
- **Decision**: launched and completed the full 62,744-item, 40-day, 31-member GEFS pilot at commit `35f0d69` -- fully committed BEFORE extraction, unlike every prior pilot's provenance. Declared FINAL PASS after the integrity audit.
- **Why**: all pre-production checks passed; the audit found 0 duplicates, 0 corrupt files, exact checkpoint/persisted-row/canonical-work-plan reconciliation (62,744=62,744=62,744, 0 missing, 0 extra in every direction), 100% ensemble completeness (every one of 2,024 (run,forecast_hour) combinations has all 31 members), grid/distance/F0/window-semantics all verified at full production scale, and the completeness/no-lookahead logic verified against real production rows (not just synthetic data).
- **Consequences**: `data/processed/pilot/gefs/manifest.json`'s `provenance_status` is `"exact"` (not "historical provenance partially reconstructed" like GFS's or NBM's) -- the first pilot in this project where production code needed zero post-commit fixes. The NBM pilot's earlier lesson (never share a validation-sample output directory with the eventual production path) was applied proactively here: `data/processed/pilot/gefs/` was confirmed empty before launch and no cleanup was needed afterward.

### ECMWF availability classified LOW confidence: bulk archive-sync, not progressive dissemination
- **Date**: 2026-09-28
- **Decision**: classify ECMWF's S3 Last-Modified availability proxy as LOW confidence, refining (not overturning) the pre-existing tag already in `data/weather_state.py`'s docstring.
- **Why**: independently confirmed across 8 (date, run_hour) combinations that ALL forecast hours of a run (F0 through F360) share Last-Modified timestamps within a 37-59 second window, at an almost exactly reproducible 514-minute lag -- essentially zero forecast-hour correlation, unlike every NOAA source in this project. This is best explained as a bulk archive-sync event, not genuine per-forecast-hour progressive dissemination. The classification is LOW not because the signal is noisy (it is remarkably precise) but because it cannot resolve per-forecast-hour timing at all.
- **Consequences**: `data/weather_state.py` requires no code change (the LOW tag and deterministic completeness logic were already correct); the completeness/no-lookahead tests for ECMWF were built to reflect this real near-atomic bulk-sync pattern rather than a generic hours-long progressive-arrival assumption (an initial test using the generic pattern correctly failed, revealing the modeling mismatch, not a code bug).

### ECMWF mx2t3/mn2t3 silently replaced by mx2t6/mn2t6 beyond F144 -- caught before declaring PASS
- **Date**: 2026-09-28
- **Decision**: select `mx2t3`/`mn2t3` for forecast hours <=144 and `mx2t6`/`mn2t6` for forecast hours >144, rather than requesting one fixed param name throughout the full horizon.
- **Why**: an initial validation-sample extraction naively requested only `mx2t3`/`mn2t3` at every forecast hour and silently produced 0 rows for exactly the three long-horizon items tested (F150/F240/F360) -- caught only by inspecting per-item variable coverage rather than trusting a "0 errors, 12/12 done" result. The two 6-hour fields were confirmed to exist with a symmetric rolling-6h window at every 6-hourly-regime forecast hour tested.
- **Consequences**: a reminder that "0 errors" from `find_message()` silently returning `None` is not sufficient evidence of correctness -- per-item variable-coverage inspection is now an explicit part of this project's validation checklist for any source with regime-dependent field naming.

### ECMWF archive path-regime boundaries precisely re-dated, not assumed from "approximately"
- **Date**: 2026-09-28
- **Decision**: use exact regime boundaries (0p4-beta through 2024-01-31, coexisting with bare 0p25 through 2024-02-28, ifs/0p25 from 2024-02-29 onward) rather than the prior feasibility work's "approximately 2024-02-28" estimate.
- **Why**: direct HEAD-probe verification across 21 dates spanning 2023-06 through 2026-09 found a clean, unambiguous one-day boundary (2024-02-28 is the last day the older regimes work; 2024-02-29 is the first day the modern regime takes over) -- more precise than needed to assert "our entire pilot's dates are safe," but cheap to obtain and worth recording exactly.
- **Consequences**: confirms with certainty that all 40 selected pilot days (2025-01-02 to 2025-08-24) are safely within the single modern `ifs/0p25` regime -- no mixed-regime handling was needed for this pilot's production code.

### Full 40-day ECMWF pilot: FINAL PASS, final numerical-weather source frozen
- **Date**: 2026-09-28
- **Decision**: launched and completed the full 904-item, 40-day ECMWF deterministic pilot at commit `c95b54d` -- fully committed BEFORE extraction, matching GEFS's exact-provenance precedent. Declared FINAL PASS after the integrity audit. This is the final planned numerical-weather source for the current pilot (HRRR/GFS/NBM/GEFS/ECMWF all now frozen).
- **Why**: all pre-production checks passed; the audit found 0 duplicates, 0 corrupt files, exact checkpoint/persisted-row/canonical-work-plan reconciliation, and 0 extraction failures (0 retries needed, better than anticipated given this bucket's known rate-limiting). The completeness/no-lookahead logic was verified against real production rows for 2025-02-25 -- deliberately chosen because it was one of 2 dates (out of 144 runs) that showed a genuine, occasional archive-sync delay (718/1436 min lag vs. the typical ~514 min), confirming the no-lookahead policy handles real-world timing variance correctly rather than only the easy/typical case.
- **Consequences**: `data/processed/pilot/ecmwf/manifest.json`'s `provenance_status` is `"exact"` (matching GEFS, not GFS's/NBM's "historical provenance partially reconstructed"). This production run's target-day-only design never required forecast hours beyond F39, so the `mx2t3`-to-`mx2t6` regime transition was validated only in the small-sample phase, not re-exercised at this production scale -- worth re-confirming if a future extraction ever needs longer ECMWF horizons.

### NBM WIND/WDIR kept as scalar speed/direction, not converted to U/V
- **Date**: 2026-09-27
- **Decision**: extract NBM's `WIND`/`WDIR` as-is (scalar speed and direction at 10m) rather than converting to U/V components to match HRRR/GFS's representation.
- **Why**: NBM's core product has no native UGRD/VGRD fields at all; a conversion would introduce an unvalidated trigonometric transformation step rather than reflecting the source's actual native product.
- **Consequences**: downstream code combining NBM wind with HRRR/GFS wind must perform an explicit, documented conversion -- never assume direct comparability.

### NWS CLINYC adopted as the canonical historical supervised Tmax label; ISD demoted to feature-only
- **Date**: 2026-10-04
- **Decision**: the canonical historical supervised daily-Tmax label is now `y_d = finalized NWS CLINYC Central Park daily Tmax` (`canonical_tmax_label_f`), selected via `data/clinyc.py`'s `select_canonical_label()`: only a FINAL report (never the afternoon PRELIMINARY "TODAY" report) is eligible; when multiple finals exist for one target date (benign reissue or an explicit `-CCA`-style correction), the latest-issued one wins; selection fails closed (raises `ClinycSelectionError`, never guesses) on an irreducible same-timestamp value tie, a latest-final reporting "MM" while an earlier final had a real value, or a "final" issued on/before its own target date (a no-lookahead safeguard). Genuinely missing days get `canonical_label_status = LABEL_UNAVAILABLE_CLINYC` and `canonical_tmax_label_f = None` -- never silently filled from ISD, a preliminary report, another station, interpolation, or a model estimate. NO bias-correction is applied toward ISD. The existing ISD-derived `label_tmax_f`/`label_source` columns are left completely unmodified and are also exposed under the explicit alias `isd_sampled_tmax_f`; ISD remains a point-in-time FEATURE/audit source only, never the supervised target.
- **Why**: a Phase 1 overlap validation against the already-frozen 2025H1 block (2025-01-01 to 2025-08-24, see `scripts/test_clinyc.py` and `scripts/backfill_clinyc.py`'s validated behavior) found CLINYC and ISD reference the identical physical station (Central Park / KNYC) but differ by a real, modest, systematic amount (178/181 days with both labels in 2025H1; mean CLINYC-minus-ISD +0.54F, max absolute discrepancy 4.0F, occurring on a date where an original "MM" final was later corrected to a real value). This is attributable to CLINYC's climate-report methodology (near-continuous sensor max detection) versus ISD's discrete hourly-observation sampling, which can miss a between-observation peak -- not a location mismatch, not noise, and not a parsing defect. The user explicitly approved treating this discrepancy as meaningful and preserving it rather than correcting it away. A Phase 2 coverage check additionally confirmed 183/184 days (99.46%) for 2025H2 and 181/181 days (100%) for 2026H1, both well within the "isolated gaps acceptable, structural unavailability is not" bar.
- **Consequences**: `scripts/backfill_clinyc.py --block <id>` builds the per-block canonical label layer (`data/processed/backfill/{block}/clinyc/clinyc_labels.parquet` + `clinyc_validation_report.json`, raw reports cached under `clinyc/raw/` for audit); `scripts/clinyc_harmonize_block.py --block <id>` attaches it to that block's already-built `integrated/state_index.parquet` and `integrated/structured_features.parquet` (adding `canonical_tmax_label_f`/`canonical_label_source`/`canonical_label_status` plus, on structured_features only, `isd_sampled_tmax_f`, `clinyc_minus_isd_f`, and CLINYC provenance columns) without touching any numerical-source extraction or any existing column's values, and records a `clinyc_label_harmonization` section in that block's `block_manifest.json` without altering its original freeze fields. Already-frozen blocks (2025H1, and 2024H2 once it freezes) get this attached via an explicit label-only harmonization pass; every block from 2025H2 onward gets it as a normal step in the per-block pipeline. Three single-day archive gaps are documented and expected to recur historically: 2025-06-02, 2025-06-03, 2025-06-18 (within 2025H1), and 2025-11-13 (within 2025H2) -- all four remain `LABEL_UNAVAILABLE_CLINYC`, not silently filled.

### ISD observation-freshness fix: a 6-hour threshold, never carry a stale reading forward as "current"
- **Date**: 2026-10-07
- **Decision**: `data/weather_state.py`'s `get_observation_state()` now distinguishes "latest observation knowable by query time" (no-lookahead, `eligible_mask()`'s job, unchanged) from "latest observation usable AS a current reading" (freshness, new). An observation older than **`OBSERVATION_FRESHNESS_THRESHOLD_HOURS = 6.0`** is never exposed as `current_temp_f`/dewpoint/wind/pressure (these become `None`, never zero-filled) and `available` becomes `False`, while `latest_observation_time`/`observation_age` are ALWAYS preserved for audit regardless of freshness. Tmax-so-far was independently confirmed already-correct (strictly day-scoped via `in_day`, never affected by this bug) and is explicitly unchanged. Cross-station current-observation diffs (`knyc_minus_klga_temp_f` etc.) already required both sides non-null, so they inherit the fix automatically. `scripts/audit_integrated_block.py`'s ISD-label and per-station-availability checks were updated to a "clean contiguous prefix" invariant (full coverage, or a gap only at the block's end where the archive hasn't caught up -- never sporadic/scattered gaps) instead of a blind 100%/>90% assumption, since CLINYC (not ISD) is the canonical label and a block can have a real, documented partial-ISD-coverage window.
- **Why**: discovered via 2025H2's integrated sample inspection -- KNYC's real ISD content stops at 2025-08-27 03:51 UTC (68.0F), and because `eligible_mask()` only ever enforced `observation_time <= query_time` with no upper bound on staleness, that single reading was being silently re-served as "the current observation" for every state through the rest of the block (into December), marked `available=True`. The 6h threshold is empirically derived, not arbitrary: measured across the two fully-healthy 6-month blocks (2025H1, 2024H2; ~730 station-days, 2190 sampled query times/station), the worst documented NORMAL reporting gap across all 4 stations is 5 hours (KLGA); 6h gives a ~20% margin above that single worst case while remaining orders of magnitude below any genuine archive-coverage failure (2025H2's gap is 1000+ hours) -- it cleanly separates "a station had one bad reporting hour" from "the archive stopped."
- **Consequences**: a read-only regression check that actually recomputed the new logic against both already-frozen blocks (not just estimated from the gap stats) found **0 differences across 4,380 states** in 2025H1 and 2024H2 -- the fix is a complete no-op for both, exactly as expected since neither block's worst gap (5h) exceeds the new 6h threshold. Neither frozen block was touched. New audit-only columns added to `structured_features.parquet` going forward: `{ICAO}_latest_observation_time_utc`, `{ICAO}_observation_stale`. 26 new regression tests added (`scripts/test_observation_freshness.py`) covering the boundary, multi-month-stale, Tmax-so-far day-scoping, cross-station gating, and no-lookahead-intact cases.

### The Weather Company (TWC) investigated as a possible ISD supplement for 2026H1 -- rejected, investigation closed
- **Date**: 2026-10-07
- **Decision**: do NOT integrate TWC or any TWC product (including the "History on Demand" / gCOD dataset) into this project, not as a replacement for missing ISD observations and not spliced alongside ISD into one feature. A genuine ISD coverage gap (e.g. 2025H2 past 2025-08-27, and anticipated for 2026H1) is represented as legitimate missing feature data (`available=False`, nulled current-observation fields, per the freshness fix above) -- never silently backfilled from a different source or station.
- **Why**: desk research (no account created, no data downloaded) found TWC's only true deep-historical product ("History on Demand", built on the "gCOD" dataset) is explicitly described by TWC's own docs as "blended modeled/observational data... generated by blending surface observations, radar, satellite, lightning, and short-term forecast models" at 4km grid resolution -- a MODELED/interpolated estimate, not a raw station reading, even at a point exactly at Central Park's coordinates. No publication/availability timestamp distinct from valid time was found in public docs, meaning a historical query could plausibly reflect information blended in after the fact (a point-in-time integrity risk, not just a measurement-process difference from ISD). No academic/research licensing terms exist; access would require a commercial trial/subscription of unclear scope. Splicing in a source with a different measurement process (grid-blended-with-models vs. raw station) would risk introducing an undetected regime change exactly at the splice point.
- **Consequences**: 2026H1 (and any future block with a real ISD gap) proceeds with ISD as a partial-coverage feature source only, gated by the same 6-hour freshness rule; CLINYC remains the sole canonical label, unaffected by this decision either way. This investigation is closed; revisiting it would require a fresh methodology review, not a silent reversal.
