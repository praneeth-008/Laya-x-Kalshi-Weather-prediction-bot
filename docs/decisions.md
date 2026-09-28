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

### NBM WIND/WDIR kept as scalar speed/direction, not converted to U/V
- **Date**: 2026-09-27
- **Decision**: extract NBM's `WIND`/`WDIR` as-is (scalar speed and direction at 10m) rather than converting to U/V components to match HRRR/GFS's representation.
- **Why**: NBM's core product has no native UGRD/VGRD fields at all; a conversion would introduce an unvalidated trigonometric transformation step rather than reflecting the source's actual native product.
- **Consequences**: downstream code combining NBM wind with HRRR/GFS wind must perform an explicit, documented conversion -- never assume direct comparability.
