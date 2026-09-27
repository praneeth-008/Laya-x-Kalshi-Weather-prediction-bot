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

## NBM -- STATUS: NOT STARTED

- Feasibility-tested only (`scripts/test_nbm_nyc_feasibility.py`, `scripts/investigate_nbm_cadence.py`).
- Prior findings (from `config/bulk_download_spec.json`, marked `PENDING_APPROVAL`, not re-verified in this pilot): archive back to ~2021-01-15; recommended cadence 8 runs/day (every 3h) based on a one-day cloud-cover-swing investigation, explicitly flagged as needing re-checking against 2-3 more active-weather days before finalizing; forecast-hour schedule hourly to +36h then 3-hourly to +192h; nearest grid point measured ~0.47km from Central Park (finest of all sources tested); a documented `forecast_desc` join bug in `data/nbm.py` (`':'.join(parts[5:])`) already fixed in that module.
- Do not treat any of the above as validated -- they are prior feasibility-test observations, not pilot-validated facts. No grid fingerprint, no distance threshold, no explicit product-selection audit exists for NBM yet.

## GEFS -- STATUS: NOT STARTED

- Feasibility-tested only (`scripts/test_gefs_nyc_feasibility.py`).
- Ensemble source: 31 members (`gec00` control + `gep01`-`gep30`), verified present in one 2025-07-01 test.
- Product: `pgrb2sp25` (0.25deg "small" compact product).
- `config/bulk_download_spec.json` records a `PENDING_APPROVAL` "INTERMEDIATE" variable scenario (TMP+DPT for all 31 members; UGRD/VGRD/PRES/APCP for the control member only) as a cost-reduction proposal -- not implemented, not re-verified.
- Nearest grid point measured ~4.18km from Central Park in feasibility testing (same as GFS, consistent with a similar-resolution grid).
- Completeness for ensembles requires BOTH forecast-hour coverage AND full member-count coverage (`assess_ensemble_run_completeness` in `data/weather_state.py` already implements this generically, but has not been exercised against any real GEFS pilot data).

## ECMWF (deterministic) -- STATUS: NOT STARTED

- Feasibility-tested only (`scripts/test_ecmwf_nyc_feasibility.py`).
- A real product-path schema change was found during feasibility work: `0p4-beta` (through ~2024-01) -> bare `0p25` (Feb 2024) -> `ifs/0p25` + `aifs/` (2024-02-28 onward, stable through at least 2026-07-15). `data/ecmwf.py`'s `grib_key()` only constructs the *current* scheme's paths -- using a start date before 2024-02-28 would silently construct wrong paths.
- Availability confidence: **LOW** -- measured ~514 min (~8.6h) average lag, far larger than every other source. Never to be treated as a true dissemination time (see `point_in_time.md`).
- ECMWF ensemble (51 members) is explicitly `DEFERRED` in `config/bulk_download_spec.json` -- the one feasibility sample was intentionally tiny (3 of ~8 target-day steps, 1 run) and was never demonstrated usable end-to-end.

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
