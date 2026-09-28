# Data Source Registry

Status labels used below: **PILOT COMPLETE** (representative-day pilot run, FINAL PASS, integrity-audited), **COLLECTED** (raw/processed data gathered for the pilot window, not a numerical-model extraction), **NOT STARTED** (feasibility-tested at most; no pilot extraction exists). Do not read anything below as a claim about the full historical archive -- everything so far is a 40-selected-day pilot or a single pilot date window, not years of data (see `bulk_storage_estimate.md` for the deliberate reasons the full-scale extraction hasn't started).

---

## HRRR -- STATUS: PILOT COMPLETE

- **Provider / archive**: NOAA, `s3://noaa-hrrr-bdp-pds` (public, unauthenticated HTTPS).
- **Path pattern**: `hrrr.{YYYYMMDD}/conus/hrrr.t{HH}z.wrfsfcf{FFF}.grib2` (+ `.idx` sidecar).
- **Grid**: Lambert Conformal Conic, ~3km native spacing, CONUS only. `Ni=1799, Nj=1059`.
- **Run frequency**: 24 runs/day (every hour). Confirmed required, not optional -- reduces to 1 run/day was shown to hide real intraday forecast information.
- **Forecast horizon**: F00-F48 for synoptic runs (00/06/12/18Z), F00-F18 for all other hourly runs.
- **Forecast-time spacing**: hourly throughout.
- **Variables (CORE_VARS)**: TMP, DPT, UGRD, VGRD, PRES, TCDC, APCP, DSWRF (8), all at 2m/10m/surface/entire-atmosphere as applicable. RH is directly available (verified unique, `2 m above ground`) but not included in HRRR's CORE_VARS -- not added, since expanding HRRR's variable set was out of scope for this pilot.
- **Levels/units**: see `variable_semantics.md` for the full per-variable table.
- **Availability method**: `S3_LAST_MODIFIED_PROXY`, NORMAL confidence.
- **Grid-selection behavior**: 3x3 patch (+/-0.03deg around Central Park), 9 DISTINCT grid points -- HRRR's ~3km grid is fine enough that the patch resolves real spatial structure (unlike GFS, see below).
- **Validated grid fingerprint**: `md5:78367561440d7c7b608b8532a02e4780` (stable across the entire pilot; never changed).
- **Distance threshold**: 10 km (derived from ~3x HRRR's measured ~1km nearest-point distance / 3km native spacing).
- **Known edge cases**:
  - **APCP has two idx candidates from forecast_hour>=2 onward**: a cumulative-since-run-start total and a direct 1-hour windowed accumulation, in that idx order (cumulative first). A naive first-match selector silently returns the cumulative one. See `variable_semantics.md` for the full resolution.
  - **APCP at forecast_hour=0**: only a degenerate zero-length `"0-0 day acc fcst"` entry exists -- there is no preceding forecast interval, so no 1-hour product is defined there.
  - Windows-specific: a temp-file-based GRIB decode path could leave the underlying OS file lock held past `codes_release()`, making `os.unlink()` fail 100% of the time in testing. Fixed by decoding directly from the in-memory byte range (`eccodes.codes_new_from_message()`) instead of a temp file.
- **Completeness logic**: target-day-only forecast-hour selection (`expected_target_day_valid_times("hrrr", ...)` in `data/weather_state.py`); run-level completeness assessed separately by `assess_deterministic_run_completeness` (see `point_in_time.md`).
- **Temporal-product behavior**: TMP/DPT/UGRD/VGRD/PRES/TCDC/DSWRF are all instantaneous. APCP is accumulation-based with the two-candidate issue above.
- **Pilot result**: 20,836/20,836 work items DONE, 0 FAILED, 835 Parquet parts, 1,500,192 rows. APCP_1H backfill: 19,996/19,996 eligible items DONE (840 F0 items structurally excluded, not failed), 805 parts, 179,964 rows. See `hrrr_pilot_manifest.md` and `hrrr_apcp_backfill.md` for full detail.

## GFS -- STATUS: PILOT COMPLETE

- **Provider / archive**: NOAA, `s3://noaa-gfs-bdp-pds` (public, unauthenticated HTTPS).
- **Path pattern**: `gfs.{YYYYMMDD}/{HH}/atmos/gfs.t{HH}z.pgrb2.0p25.f{FFF}` (+ `.idx` sidecar).
- **Grid**: regular lat-lon, 0.25deg, global. `Ni=1440, Nj=721`. Structurally different from HRRR (not Lambert Conformal).
- **Run frequency**: 4 runs/day (00/06/12/18Z).
- **Forecast horizon**: F000-F384 (16 days).
- **Forecast-time spacing**: hourly through F120, then 3-hourly F123-F384.
- **Variables (CORE_VARS)**: TMP, DPT, RH, UGRD, VGRD, PRES, TCDC, APCP, DSWRF (9). RH included after production-readiness review found no reason for its earlier exclusion.
- **Availability method**: `S3_LAST_MODIFIED_PROXY`, NORMAL confidence. Observed lag ~1.7-3.6h after nominal run_time (larger than HRRR's, since GFS's global product takes longer to fully publish).
- **Grid-selection behavior**: 3x3 patch requested, but GFS's 0.25deg cell (~28km x ~21km at NYC's latitude) is far coarser than the +/-0.03deg patch span -- **all 9 patch points collapse to the SAME single grid cell.** The patch adds no spatial resolution for GFS; it is only extracted because doing so is near-zero additional cost.
- **Validated grid fingerprint**: `md5:45f3a4a8af23f33a77ab669d0fa1d813` (distinct from HRRR's; confirmed no collision).
- **Distance threshold**: 20 km, derived from the 0.25deg grid geometry (theoretical worst-case nearest-point distance ~17.4km; 20km gives ~15% margin). Not the same value as HRRR's -- verified as a distinct, source-aware parameter (`data.gfs.MAX_PATCH_DISTANCE_KM`), not blindly inherited.
- **Known edge cases**:
  - **APCP and TCDC each have two idx candidates** under the same `(variable, level)` key at nearly every forecast hour (a windowed/instantaneous product and a cumulative/averaged one). See `variable_semantics.md` for the resolved selection rules.
  - **forecast_hour=0 uses `forecast_desc="anl"`** (analysis) for every instantaneous variable, instead of `"0 hour fcst"`. `APCP` and `DSWRF` have **zero** idx entries at all at fh=0 (no accumulation/average is defined at the analysis instant) -- this is a legitimate absence, not a failure. See `variable_semantics.md`.
- **Completeness logic**: same target-day-only mechanism, source-aware (`expected_target_day_valid_times("gfs", ...)` branches explicitly for GFS's own schedule).
- **Pilot result**: 5,836/5,836 work items DONE, 0 FAILED (132 initial failures, all the fh=0 `"anl"`-format issue, resolved and retried), 241 Parquet parts, 470,340 rows.

## NBM -- STATUS: COMPLETE -- FINAL PASS

- **Provider / archive**: NOAA/MDL, `s3://noaa-nbm-grib2-pds` (public, unauthenticated HTTPS). NBM is a statistically post-processed BLEND of multiple NWP model/ensemble inputs, not a raw model.
- **Path pattern**: `blend.{YYYYMMDD}/{HH}/{product}/blend.t{HH}z.{product}.f{FFF}.co.grib2` (+ `.idx`). Products: `core` (deterministic + single ens-std-dev field), `qmd` (percentile guidance, precip/wind/gust ONLY, confirmed no temperature), `text` (bulletins, unused).
- **Grid**: Lambert Conformal, `Ni=2345, Nj=1597`, `Dx=Dy=2539.703m` (~2.54km) -- the finest grid of any source extracted so far.
- **Run frequency**: 24 runs/day, but the **forecast-hour schedule is run-hour dependent** (a real correction to prior code -- see `nbm_pilot_readiness.md` section 1): full tier (`run_hour%6` in {0,1}, 8 runs/day) reaches F264; medium tier (`run_hour%6==3`, 4 runs/day) reaches F189; short tier (the remaining 12 runs/day) reaches only F36.
- **Forecast-time spacing**: hourly F1-F36, then 3-hourly, then (full tier only) 6-hourly beyond F192. `forecast_hour=0` does not exist at all (confirmed 404 at every run hour/date tested).
- **Variables**: TMP, DPT, RH, WIND, WDIR (scalar speed+direction, NOT UGRD/VGRD components), TCDC, APCP_1H, APCP_6H, TMAX_PERIOD/TMIN_PERIOD (00Z/12Z runs only). See `variable_semantics.md`.
- **Availability method**: `S3_LAST_MODIFIED_PROXY`, NORMAL confidence, progressive arrival within a run (~62-63 min lag range observed, consistent with prior ~41-71 min feasibility estimate).
- **Grid-selection behavior**: 3x3 patch produces 9 distinct grid cells (max observed distance 1.24km) -- resolves real spatial structure, similar to HRRR, unlike GFS.
- **Validated grid fingerprint**: `md5:5bb2a8d01d638075eaa2ff7236270d44` (distinct from both HRRR's and GFS's).
- **Distance threshold**: 5.0 km, independently derived from NBM's own `Dx=Dy` grid geometry (theoretical worst case ~1.8km) -- not inherited from HRRR (10km) or GFS (20km).
- **Known edge cases**: APCP has probability-of-exceedance products that must be excluded (not just duplicate amount candidates like HRRR/GFS); WIND/WDIR exist at 4 different heights under the same variable name; TCDC has 3 unidentified `typeOfLevel=unknown` candidates alongside the wanted `surface` one; TMAX/TMIN exist only for 00Z/12Z runs with run-hour-dependent max/min alternation. Full detail in `nbm_pilot_readiness.md`.
- **Completeness logic**: `data/weather_state.py`'s NBM branch now delegates directly to `data.nbm.available_forecast_hours(run_hour)` (fixed 2026-09-27, see `decisions.md`) -- one source of truth shared with extraction, no duplicate hardcoded schedule. 31 targeted completeness tests pass (all 3 tiers, boundary forecast hours, no-lookahead behavior); HRRR/GFS completeness regression-checked as unaffected.
- **Worker count**: 20, independently benchmarked across 8/12/16/20/24 (workload is network-latency-bound, not CPU-bound -- CPU avg never exceeded ~41% even at 24 workers; throughput flat ~1.4-1.8 items/sec across the whole range with zero errors at every count). See `nbm_pilot_readiness.md` section 17.
- **Pilot result**: 31,578/31,578 work items DONE, 0 FAILED (7 initial transient S3 `ConnectionResetError`s, retried successfully), 1,265 Parquet parts, 2,026,080 rows, 0 duplicates, 0 corrupt/zero-byte files, grid fingerprint and distance threshold both reconfirmed at full scale. One data-hygiene finding (9 stray checkpoint entries / 648 rows left over from an earlier validation-sample run into the same output directory) was caught by the post-run audit and cleaned before finalizing -- see `nbm_pilot_readiness.md` section 18 and `data/processed/pilot/nbm/manifest.json`'s provenance notes for the full account. Actual: ~316.0GB remote bytes read, ~28.0MB local Parquet storage, ~3.68 hours runtime at 20 workers (faster than the ~5.3h pre-launch estimate -- sustained throughput was higher than the short benchmark trials suggested).

## GEFS -- STATUS: COMPLETE -- FINAL PASS

- Provider/archive: `s3://noaa-gefs-pds`, public, unauthenticated HTTPS. Ensemble: 31 members (`gec00` control + `gep01`-`gep30` perturbed), confirmed present and stable across 5 selected-pilot dates x 2 run hours (10 combinations, 310 direct probes). `gep31` confirmed absent; `geavg`/`gespr` (precomputed statistics) confirmed present but correctly excluded from the member set.
- Product: `pgrb2sp25` (0.25deg "small" compact product), path `gefs.{YYYYMMDD}/{HH}/atmos/pgrb2sp25/{member}.t{HH}z.pgrb2s.0p25.f{FFF}`.
- Run cadence: 4 runs/day (00/06/12/18Z), forecast schedule uniform 3-hourly F0-F240 for every member/run hour/date tested (not run-hour-dependent, unlike NBM).
- Grid: `regular_ll`, `Ni=1440, Nj=721`, 0.25deg, `md5GridSection=45f3a4a8af23f33a77ab669d0fa1d813` -- confirmed IDENTICAL to GFS's own grid (independently verified, not assumed). The +/-0.03deg 3x3 patch fully collapses to 1 grid cell (max observed distance 8.35km), same behavior as GFS.
- Distance threshold: 20.0km, independently derived from GEFS's own grid geometry -- equals GFS's value only because the grids are genuinely identical.
- Variables: TMP, DPT, RH, UGRD, VGRD, PRES (instant, present incl. F0 via `anl`), APCP, TCDC, DSWRF (accum/average, absent at F0, window resets every 6h synoptic mark for FH>6 -- see `variable_semantics.md`), TMAX, TMIN (period max/min, F0 degenerate and skipped). No duplicate products found for any variable at any forecast hour -- unlike HRRR/GFS/NBM's APCP, `find_message()`'s exact-match lookup is safe here.
- Availability: `S3_LAST_MODIFIED_PROXY`, substantially longer lag than other sources (~3.8h at FH3, growing to ~5.3-5.5h at FH240, confirmed genuine progressive release). Control member arrives ~1-14 min before the perturbed-member batch; perturbed members cluster tightly (~2-14 min spread). Per-row availability preserved at full member/FH granularity, never collapsed.
- Completeness: conservative complete-ensemble rule (`assess_ensemble_run_completeness` in `data/weather_state.py`, built in an earlier architecture phase, independently re-validated here against the confirmed real schedule -- no fix needed) -- a run is usable only when all 31 members have all expected target-day valid times.
- Work-item design: `(run_time, ensemble_member, forecast_hour)`. Worker count: 16 (independently benchmarked, network-latency-bound like NBM).
- **Pilot result**: 62,744/62,744 work items DONE, 0 FAILED (203 initial transient S3 connection errors, retried successfully), 2,519 Parquet parts, 6,027,516 rows, 0 duplicates, 0 corrupt/zero-byte files. Every one of 2,024 distinct (run, forecast_hour) combinations has exactly 31/31 members present -- 100% ensemble completeness across the entire production dataset. Canonical work-plan/checkpoint/persisted-data reconciliation exact (0 missing, 0 extra) -- dedicated output directory prevented the cross-contamination the NBM pilot encountered. Actual: ~402.8GB remote bytes read, ~51.1MB local Parquet storage, ~4.64 hours runtime at 16 workers (faster than the ~6.0h pre-launch estimate). See `gefs_pilot_readiness.md` section 22 and `data/processed/pilot/gefs/manifest.json` for full detail.

## ECMWF (deterministic) -- STATUS: COMPLETE -- FINAL PASS

- **Provider/archive**: `s3://ecmwf-forecasts` (AWS Open Data), public HTTPS. Stream: `oper` (HRES deterministic). Contradicts ECMWF's own stated ~2-3 day Open Data retention policy (still serves our full 2025 pilot range) -- treated as a real but unguaranteed capability, not a documented one.
- **Archive path regimes**: independently re-confirmed with exact boundaries (more precise than the prior "approximately" estimate): `0p4-beta` through 2024-01-31, coexisting with bare `0p25` through 2024-02-28, then `ifs/0p25` from **2024-02-29** onward (confirmed stable through 2026-09-27). `data/ecmwf.py`'s `grib_key()` only implements the modern `ifs/0p25` regime -- every one of this project's 40 selected pilot days (2025-01-02 to 2025-08-24) falls safely inside it.
- **Run cadence**: `oper` confirmed present ONLY at 00Z/12Z across all dates tested (06Z/18Z absent for `oper`; `enfo`/ensemble IS present at all 4 hours but is out of scope for this phase).
- **FH schedule**: 3-hourly F0-F144, then 6-hourly F150-F360, identical at both run hours and across every date tested.
- **Grid**: `regular_ll`, `Ni=1440, Nj=721`, 0.25deg -- same cell size as GFS/GEFS but a genuinely DIFFERENT grid fingerprint (`md5:265781b4edc06425746b46a5775244eb` vs. GFS/GEFS's `45f3a4a8af23f33a77ab669d0fa1d813`, different grid origin longitude convention) -- independently confirmed, not assumed from matching resolution. +/-0.03deg patch fully collapses to 1 cell, same 20km independently-derived distance threshold as GFS/GEFS (coincidence of identical cell size).
- **Variables**: 2t, 2d, 10u, 10v, sp (instant, incl. F0), tp, ssrd (accum, cumulative-since-run-start throughout the FULL 360h horizon -- unlike GEFS's 6h-reset pattern; skipped at F0), mx2t3/mn2t3 (rolling 3h window, F3-F144 only), mx2t6/mn2t6 (rolling 6h window, F150-F360 only -- REPLACES mx2t3/mn2t3, a real regime-switch caught and fixed during validation). **No native 2m relative humidity or cloud-cover field exists in this product at all** -- confirmed by exhaustive index scan, a genuine structural gap vs. every NOAA source in this project, not derived/worked around.
- **Availability**: `S3_LAST_MODIFIED_PROXY`, **LOW confidence** -- confirmed a nearly-exact, forecast-hour-independent 514-minute (~8.57h) lag, with ALL forecast hours of a run (F0 through F360) syncing within a 37-59 second window. This is best explained as a bulk archive-sync event, not genuine per-forecast-hour progressive dissemination like every NOAA source -- see `ecmwf_pilot_readiness.md` section 19-21 for full detail.
- **Completeness**: reuses the existing deterministic rule (`assess_deterministic_run_completeness`/`get_latest_forecast`, unchanged, already used by HRRR/GFS/NBM) -- validated correct against the confirmed near-atomic bulk-sync availability pattern via 10 targeted tests.
- **Work-item design**: `(run_time, forecast_hour)`. Worker count: 16 (independently benchmarked; this bucket shows substantially higher run-to-run throughput variance than any other source, attributable to more aggressive S3 rate-limiting).
- **Pilot result**: 904/904 work items DONE, 0 FAILED, 0 retries needed, 37 Parquet parts, 71,352 rows, 0 duplicates, 0 corrupt/zero-byte files. Canonical work-plan/checkpoint/persisted-data reconciliation exact (0 missing, 0 extra) -- dedicated output directory avoided any NBM-style contamination. This production run's target-day-only design never required forecast hours beyond F39, so `mx2t6`/`mn2t6` never appear in this specific dataset (validated separately in the small-sample phase). Audit found 2 of 144 runs (2025-02-24 12Z, 2025-02-25 00Z) with a genuine, occasional archive-sync delay (718/1436 min lag vs. the typical ~514 min) while remaining fully internally atomic -- correctly handled by the no-lookahead policy, not a defect. Actual: ~5.99GB remote bytes read, ~0.76MB local Parquet storage, 14.15 minutes runtime at 16 workers (1.065 items/sec sustained, faster than every benchmark trial). See `ecmwf_pilot_readiness.md` section 39 and `data/processed/pilot/ecmwf/manifest.json` for full detail.
- **Five-year-backfill implications**: the validated `ifs/0p25` regime only covers 2024-02-29 onward (under 2 years as of this pilot) -- materially shorter than the other sources' reach. Older regimes exist but require separate, unvalidated path/parsing logic. See `ecmwf_pilot_readiness.md` section 37.
- ECMWF ensemble (`enfo`, 51 members) remains explicitly out of scope, per this phase's instruction (deterministic-only validation).

## Surface observations (ISD) -- STATUS: COLLECTED (pilot window)

- **Provider**: NOAA Integrated Surface Database, yearly per-station CSV.GZ.
- **Stations**: KNYC (Central Park, primary target, station id `725053-94728`), KLGA, KJFK, KEWR (auxiliary predictors).
- **Window collected**: 2025-01-01 to 2025-08-24 (the pilot window; `PILOT_END_DATE` was discovered dynamically as the latest common usable date across all 4 stations, per `scripts/pilot_phase0_discover_end_date.py`).
- **Availability method**: `OBSERVATION_TIME_ONLY` -- ISD has no independent availability timestamp; `observation_time` itself is used as the proxy under the `proxy` policy only.
- **Raw storage policy**: `KEEP_RAW` (unlike the numerical sources, which are stream-and-discard).
- **Known gap**: Central Park has fragmented pre-2005 predecessor station IDs, not reconciled into the canonical `725053-94728` id. Pre-2005 history would require separate reconciliation work.

## NWS AFD -- STATUS: COLLECTED (pilot window)

- **Provider**: NOAA/NWS, via the Iowa Environmental Mesonet `nwstext` API.
- **Office/product**: OKX / AFDOKX (New York City forecast office's Area Forecast Discussion).
- **Window collected**: 2025-01-01 to 2025-08-24.
- **Availability method**: `ISSUANCE_TIME_ONLY` -- the schema has an `available_time` column but it is 100% null; `issuance_time` is used as the proxy under the `proxy` policy only.
- **Processing policy**: full raw text and parsed sections both preserved; explicitly no summarization, no embeddings, no LLM processing of the text (`config/bulk_download_spec.json`).
