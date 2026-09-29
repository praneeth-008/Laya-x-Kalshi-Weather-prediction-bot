# Variable Inventory and Model-Feature Readiness Audit

Derived directly from the actual frozen, persisted pilot Parquet files (2026-09-29), not from documentation alone. Documentation was used only for cross-checking apparent discrepancies. Baseline: commit `f972424596d50bdf5181d7ccba860a7bc06d775c`.

## 1. HRRR variable inventory

Source: `data/processed/pilot/hrrr/parts/` (166,688 primary-grid rows) + `data/processed/pilot/hrrr_apcp_1h/parts/` (19,996 rows, separate backfill dataset).

| Variable | Provider name | Level | Units | Temporal stat | Rows | Non-null |
|---|---|---|---|---|---|---|
| Temperature | `TMP` | 2 m above ground | K | INSTANTANEOUS | 20,836 | 100% |
| Dewpoint | `DPT` | 2 m above ground | K | INSTANTANEOUS | 20,836 | 100% |
| U wind | `UGRD` | 10 m above ground | m/s | INSTANTANEOUS | 20,836 | 100% |
| V wind | `VGRD` | 10 m above ground | m/s | INSTANTANEOUS | 20,836 | 100% |
| Pressure | `PRES` | surface | Pa | INSTANTANEOUS | 20,836 | 100% |
| Cloud cover | `TCDC` | entire atmosphere | % | INSTANTANEOUS | 20,836 | 100% |
| Precipitation (cumulative) | `APCP` | surface | kg/m^2 | CUMULATIVE (since run start) | 20,836 | 100% (F0 degenerate, not fabricated) |
| Solar radiation | `DSWRF` | surface | W/m^2 | INSTANTANEOUS (HRRR's own convention, unlike GFS's average) | 20,836 | 100% |
| Precipitation (windowed) | `APCP_1H` | surface | kg/m^2 | ACCUMULATION, 1h window | 19,996 | 100% |

Run-hour coverage: all 24 UTC hours. Forecast-hour range: 0-46 (target-day-only bounded). F0: present for instant fields (6,720 rows); `APCP_1H` structurally absent at F0 (0 rows, by design -- 1-hour window needs a prior hour).

**RH**: NOT extracted for HRRR. Confirmed available and uniquely selectable in the raw archive (per `docs/variable_semantics.md`'s GFS RH entry: "RH is also directly available in HRRR ... adding it there was out of scope; source identity is kept explicit"). A deliberate, documented decision, not an oversight or defect.

## 2. GFS variable inventory

Source: `data/processed/pilot/gfs/parts/` (52,260 primary-grid rows).

| Variable | Provider name | Level | Units | Temporal stat | Rows | Non-null |
|---|---|---|---|---|---|---|
| Temperature | `TMP` | 2 m above ground | K | INSTANTANEOUS | 5,836 | 100% |
| Dewpoint | `DPT` | 2 m above ground | K | INSTANTANEOUS | 5,836 | 100% |
| Relative humidity | `RH` | 2 m above ground | % | INSTANTANEOUS | 5,836 | 100% |
| U wind | `UGRD` | 10 m above ground | m/s | INSTANTANEOUS | 5,836 | 100% |
| V wind | `VGRD` | 10 m above ground | m/s | INSTANTANEOUS | 5,836 | 100% |
| Pressure | `PRES` | surface | Pa | INSTANTANEOUS | 5,836 | 100% |
| Cloud cover | `TCDC` | entire atmosphere | % | INSTANTANEOUS (explicit selection, duplicate candidates resolved) | 5,836 | 100% |
| Precipitation | `APCP` | surface | kg/m^2 | ACCUMULATION, shortest-recent-window (explicit selection among duplicates) | 5,704 | 97.7% (F0 structurally absent) |
| Solar radiation | `DSWRF` | surface | W/m^2 | AVERAGE OVER WINDOW (GFS's own convention, unlike HRRR's instantaneous) | 5,704 | 97.7% (F0 structurally absent) |

Run-hour coverage: 00/06/12/18Z only. Forecast-hour range: 0-46. F0: 924 rows total (instant fields present via `forecast_desc='anl'`; APCP/DSWRF structurally absent). **All 9 expected variables confirmed present -- exact match to the documented intended inventory.**

## 3. NBM variable inventory

Source: `data/processed/pilot/nbm/parts/` (225,120 primary-grid rows).

| Variable | Provider name | Level | Units | Temporal stat | Rows | Non-null |
|---|---|---|---|---|---|---|
| Temperature | `TMP` | 2 m above ground | K | INSTANTANEOUS | 31,480 | 100% |
| Dewpoint | `DPT` | 2 m above ground | K | INSTANTANEOUS | 31,480 | 100% |
| Relative humidity | `RH` | 2 m above ground | % | INSTANTANEOUS | 31,480 | 100% |
| Wind speed | `WIND` | 10 m above ground | m/s | INSTANTANEOUS | 31,480 | 100% |
| Wind direction | `WDIR` | 10 m above ground | deg | INSTANTANEOUS | 31,480 | 100% |
| Cloud cover | `TCDC` | surface | % | INSTANTANEOUS | 31,480 | 100% |
| Precipitation (1h) | `APCP_1H` | surface | kg/m^2 | ACCUMULATION, 1h window | 31,578 | 100% (F0 structurally absent, not counted here) |
| Precipitation (6h) | `APCP_6H` | surface | kg/m^2 | ACCUMULATION, 6h window, present only at absolute-UTC-synoptic-aligned valid times | 4,462 | 100% of the times it structurally applies |
| Period max temp | `TMAX_PERIOD` | 2 m above ground | K | MAXIMUM OVER WINDOW, 12h | 120 | 100% (only run_hour in {0,12}) |
| Period min temp | `TMIN_PERIOD` | 2 m above ground | K | MINIMUM OVER WINDOW, 12h | 80 | 100% (only run_hour in {0,12}) |

Run-hour coverage: all 24 UTC hours (3-tier schedule). Forecast-hour range: 1-45 (F0 does not exist for NBM at all). **All 10 expected variables confirmed present -- exact match to the documented intended inventory.**

## 4. GEFS variable inventory

Source: `data/processed/pilot/gefs/parts/` (669,724 primary-grid rows, 31 members).

| Variable | Provider name | Level | Units | Temporal stat | Rows | Non-null |
|---|---|---|---|---|---|---|
| Temperature | `TMP` | 2 m above ground | K | INSTANTANEOUS | 62,744 | 100% |
| Dewpoint | `DPT` | 2 m above ground | K | INSTANTANEOUS | 62,744 | 100% |
| Relative humidity | `RH` | 2 m above ground | % | INSTANTANEOUS | 62,744 | 100% |
| U wind | `UGRD` | 10 m above ground | m/s | INSTANTANEOUS | 62,744 | 100% |
| V wind | `VGRD` | 10 m above ground | m/s | INSTANTANEOUS | 62,744 | 100% |
| Pressure | `PRES` | surface | Pa | INSTANTANEOUS | 62,744 | 100% |
| Cloud cover | `TCDC` | entire atmosphere | % | INSTANTANEOUS | 58,652 | structurally absent at F0 |
| Precipitation | `APCP` | surface | kg/m^2 | ACCUMULATION, resets every 6h synoptic mark | 58,652 | structurally absent at F0 |
| Solar radiation | `DSWRF` | surface | W/m^2 | ACCUMULATION, cumulative-since-start (same convention as tp) | 58,652 | structurally absent at F0 |
| Period max temp | `TMAX` | 2 m above ground | K | MAXIMUM OVER WINDOW | 58,652 | structurally absent at F0 |
| Period min temp | `TMIN` | 2 m above ground | K | MINIMUM OVER WINDOW | 58,652 | structurally absent at F0 |
| **Ensemble member identity** | `ensemble_member`/`member_type` | -- | -- | -- | 669,724 | 100%, always populated |

Run-hour coverage: 00/06/12/18Z. Forecast-hour range: 0-45. Member coverage: exactly 31/31 (gec00 control + gep01-gep30 perturbed) at every one of 2,024 distinct (run,forecast_hour) combinations audited during the GEFS pilot. **All 11 expected weather variables plus ensemble member identity confirmed present -- exact match to the documented intended inventory.**

## 5. ECMWF deterministic variable inventory

Source: `data/processed/pilot/ecmwf/parts/` (7,928 primary-grid rows).

| Variable | Provider name | Units | Temporal stat | Rows | Non-null |
|---|---|---|---|---|---|
| Temperature | `2t` | K | INSTANTANEOUS | 904 | 100% |
| Dewpoint | `2d` | K | INSTANTANEOUS | 904 | 100% |
| 10m U wind | `10u` | m/s | INSTANTANEOUS | 904 | 100% |
| 10m V wind | `10v` | m/s | INSTANTANEOUS | 904 | 100% |
| Surface pressure | `sp` | Pa | INSTANTANEOUS | 904 | 100% |
| Precipitation | `tp` | m | CUMULATIVE (since run start, entire 360h horizon) | 852 | structurally absent at F0 |
| Solar radiation | `ssrd` | J/m^2 | CUMULATIVE (since run start) | 852 | structurally absent at F0 |
| 3h period max temp | `mx2t3` | K | MAXIMUM OVER WINDOW, 3h (F3-F144 only) | 852 | 100% within its regime |
| 3h period min temp | `mn2t3` | K | MINIMUM OVER WINDOW, 3h (F3-F144 only) | 852 | 100% within its regime |

**`mx2t6`/`mn2t6` (6h period max/min, the F150+ regime's fields): 0 rows in this dataset.** Confirmed NOT a discrepancy: this production run's forecast_hour range is 0-39 only (target-day-only extraction design), never reaching the F150+ regime where these fields would appear -- already documented in `docs/ecmwf_pilot_readiness.md`. Validated separately (present and correct) during the ECMWF pilot's small-sample validation phase.

**No native 2m relative humidity or cloud-cover field exists in this product at any forecast hour** -- confirmed structural absence (not extracted because it cannot be), documented since the ECMWF pilot phase.

## 6. Observation variable inventory (per station)

Source: `data/processed/pilot/observations/pilot_observations_canonical.parquet`, `is_real_observation=True` rows only.

| Field | KNYC | KLGA | KJFK | KEWR | Units |
|---|---|---|---|---|---|
| Row count | 7,413 | 8,512 | 8,415 | 8,292 | -- |
| Temperature | 100% | 100% | 100% | 100% | F |
| Dewpoint | 100% | 100% | 100% | 100% | F |
| **Relative humidity** | **0%** | **0%** | **0%** | **0%** | % (column exists, never populated) |
| Wind direction | 44.3% | 93.7% | 95.9% | 90.7% | deg |
| Wind speed | 90.8% | 99.9% | 100.0% | 100.0% | m/s |
| Wind gust | 19.6% | 20.9% | 16.5% | 21.5% | m/s |
| Station pressure | 98.0% | 99.1% | 98.6% | 98.1% | hPa |
| Sea-level pressure | 73.1% | 88.2% | 89.1% | 90.1% | hPa |
| Precipitation | 83.3% | 77.1% | 75.5% | 76.1% | mm |
| Cloud cover (raw code) | 98.0% | 92.5% | 91.8% | 91.8% | coded string |
| Visibility | 99.2% | 100.0% | 100.0% | 100.0% | m |
| Weather codes | 27.5% | 19.3% | 19.9% | 18.5% | coded string |
| Quality-control flags | 100% | 100% | 100% | 100% | per-field codes |
| Observation timestamp | 100% | 100% | 100% | 100% | UTC |
| Availability/proxy timestamp | N/A (no independent proxy; `observation_time` itself is the proxy) | | | | |

**Relative humidity is 0% populated at every station** -- the ISD schema carries a placeholder column that this raw data source never fills; RH is derivable from the always-100%-present temperature+dewpoint pair via a standard humidity formula, not natively reported. Not a defect: a structural characteristic of the ISD/METAR data source itself. Wind gust, weather codes, and (at KNYC specifically) wind direction are genuinely partially missing -- consistent with normal METAR reporting practice (gusts/weather codes only reported when conditions warrant; KNYC's own station history shows a wind-direction sensor gap for part of the pilot window).

## 7. AFD inventory

Source: `data/processed/pilot/afd/pilot_afd_documents.parquet` (2,269 documents) + `pilot_afd_sections.parquet` (23,962 parsed section rows).

- **Full text**: preserved for 100% of documents (`raw_text`, non-null for all 2,269; length 3,543-14,110 characters, mean 7,762).
- **Issuance time**: 100% populated.
- **Product identifier**: 100% populated (`product_id`, e.g. `202506151508-KOKX-FXUS61-AFDOKX`).
- **Office**: `OKX` (NWS New York/Upton) for all documents.
- **Availability proxy**: `available_time` column exists but is 100% null (as documented) -- `issuance_time` is used as the labeled PROXY under `ISSUANCE_TIME_ONLY`/PROXY-mode-only availability.
- **Section structure**: already parsed into a separate table (`section_name`/`section_text` per document), enabling structured access without re-parsing raw text.

**Confirmed: the complete historical text remains fully accessible for later NLP work** -- it has NOT been reduced to an unrecoverable reference. The integration layer's `structured_features.parquet` carries only `afd_product_id`/`afd_issuance_time`/`afd_age_hours` (a reference), deliberately NOT duplicating the ~7.7KB average text into the tabular layer -- the full text is one lookup away in the already-frozen, unmodified canonical AFD table.

## 8. Variable-family matrix

| Family | HRRR | GFS | NBM | GEFS | ECMWF | OBS |
|---|---|---|---|---|---|---|
| Temperature | YES | YES | YES | YES | YES | YES |
| Dewpoint | YES | YES | YES | YES | YES | YES |
| Relative humidity | STRUCTURAL N/A (available but not extracted) | YES | YES | YES | STRUCTURAL N/A (not native to product) | STRUCTURAL N/A (column exists, never populated; derivable from temp+dewpoint) |
| Wind speed/components | YES | YES | YES | YES | YES | YES |
| Wind direction | STRUCTURAL N/A (HRRR extracts components only) | STRUCTURAL N/A (GFS extracts components only) | YES | STRUCTURAL N/A (GEFS extracts components only) | STRUCTURAL N/A (ECMWF extracts components only) | YES (partial) |
| Pressure | YES | YES | STRUCTURAL N/A (not extracted) | YES | YES | YES |
| Precipitation | YES (cumulative + 1h) | YES | YES (1h + 6h) | YES | YES (cumulative) | YES |
| Cloud cover | YES | YES | YES | YES | STRUCTURAL N/A (not native to product) | YES (raw coded) |
| Solar radiation | YES | YES | STRUCTURAL N/A (not extracted) | YES | YES | NO |
| Visibility | NO | NO | NO | NO | NO | YES |
| Weather codes | NO | NO | NO | NO | NO | YES (partial) |
| Native period Tmax | STRUCTURAL N/A (not native to product) | STRUCTURAL N/A (not native to product) | YES | YES | YES | STRUCTURAL N/A (Tmax_so_far is derived from instantaneous readings, not a native period-max report) |
| Native period Tmin | STRUCTURAL N/A | STRUCTURAL N/A | YES | YES | YES | STRUCTURAL N/A |
| Ensemble information | STRUCTURAL N/A (deterministic) | STRUCTURAL N/A (deterministic) | STRUCTURAL N/A (deterministic) | YES (31 members) | STRUCTURAL N/A (ensemble deliberately out of scope this phase) | STRUCTURAL N/A |

All `YES` entries verified against actual persisted row counts above, not assumed.

## 9. Pipeline-survival audit (the critical finding)

| Source | Variable | Present in canonical | Selected at point-in-time (`pilot_loader`/`get_latest_forecast`) | Present in integrated dataset | Model-accessible today | Notes |
|---|---|---|---|---|---|---|
| HRRR/GFS/NBM/GEFS/ECMWF | Temperature (instantaneous) | YES | YES | YES (`forecast_trajectories.parquet`, `{src}_forecast_tmax_f`) | **YES** | Fixed 2026-09-29 (previously contaminated by other variables; now isolated) |
| HRRR/GFS/NBM/GEFS/ECMWF | **Dewpoint** | YES | YES (loaded into memory) | **NO** | **NO** | Never threaded past `pilot_loader.py`'s in-memory dataframe into any integration output |
| HRRR/GFS/GEFS | **Wind (U/V)** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| NBM | **Wind (speed/direction)** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| ECMWF | **Wind (10u/10v)** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| HRRR/GFS/GEFS/ECMWF | **Pressure** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| HRRR/GFS/NBM/GEFS | **Cloud cover** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| HRRR/GFS/NBM/GEFS/ECMWF | **Precipitation** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| HRRR/GFS/GEFS/ECMWF | **Solar radiation** | YES | YES (loaded into memory) | **NO** | **NO** | Same gap |
| NBM/GEFS/ECMWF | **Native period Tmax/Tmin** | YES | YES (loaded into memory) | **NO** | **NO** | Correctly EXCLUDED from the temperature series by the 2026-09-29 fix (as it should be), but not preserved anywhere else either |
| GEFS | Member daily-max (scalar) | YES | YES | YES (`gefs_members.parquet`) | **YES** | |
| GEFS | **Member-level TRAJECTORY** (per-valid_time, per-member) | YES | YES (loaded into memory) | **NO** | **NO** | Only the pre-collapsed daily max per member is preserved, not the full trajectory |
| AFD | Full text | YES | YES | YES (referenced; full text in the unmodified canonical AFD table) | **YES** | |
| Observations | Temp/dewpoint/wind/pressure (current + Tmax_so_far) | YES | YES | YES (`structured_features.parquet`) | **YES** | Observations ARE fully threaded through -- only the NUMERICAL FORECAST sources have this gap |

**This is a real, material pipeline gap, not a documentation issue.** Every one of the "NO" rows above reflects information that is correctly extracted, frozen, and validated in the canonical source data (confirmed in sections 1-5), and is even loaded into memory during integration (`data/pilot_loader.py` loads every variable, not just temperature) -- but `scripts/build_integrated_pilot.py`'s `extract_structured_features()`/`extract_sequences()` only ever reads the temperature-isolated output of `get_latest_forecast()`/`get_ensemble_state()`, which (by design, both before and after the 2026-09-29 fix) only ever surfaces ONE variable's trajectory and one scalar summary. This gap PRE-DATES the 2026-09-29 fix -- that fix corrected a case of the WRONG variables leaking into the temperature series, but it did not (and was not asked to) add a pathway for the excluded variables to reach the integrated dataset through any other channel.

## 10. Temperature-semantics regression check

Re-confirmed directly against the corrected `forecast_trajectories.parquet` (26,542 rows): exactly one row per (state_id, source, valid_time) for every one of HRRR/GFS/NBM/ECMWF -- structurally impossible for period Tmax/Tmin or dewpoint to be mixed in, since only the canonical instantaneous-temperature variable (`TMP`/`2t`) is ever read. **PASS.**

## 11. Non-temperature information preservation

**NOT preserved in the model-accessible integrated dataset.** See section 9's pipeline-survival audit. Dewpoint, wind, pressure, precipitation, cloud cover, solar radiation, and native period Tmax/Tmin all exist correctly in canonical source data but are not currently accessible to the eventual model through any integration-layer output. **This is reported as a feature-pipeline gap, per the explicit instruction not to authorize backfill if one is found.**

## 12. Timing/freshness information

For every numerical source, `structured_features.parquet` preserves: `{src}_run` (run_time), `{src}_usable_since` (when completeness was achieved), `{src}_age_hours` (age since run_time), `{src}_latest_seen_run` (raw first-arrival run, distinct from usable), `{src}_latest_seen_status`. `valid_time` and `forecast_hour` are preserved per-row in `forecast_trajectories.parquet`. Query time is preserved in `state_index.parquet`. **Sufficient to derive forecast lead time (valid_time - run_time), age since latest usable run, and (via `{src}_previous_run`/`{src}_previous_forecast_tmax_f`/`{src}_revision_f`) the previous usable run and its revision -- all already computed without lookahead. PASS**, for the temperature signal specifically (the only variable currently threaded through).

## 13. Forecast-revision support

`{src}_previous_run`, `{src}_previous_forecast_tmax_f`, `{src}_revision_f` (deterministic sources) and `gefs_revision_mean_f` (GEFS) are already computed as scalar features, using only information available by query_time (verified via the no-lookahead audit, 0 violations). **PASS** for temperature; not applicable to other variables since they are not yet threaded through at all.

## 14. GEFS ensemble-information result

**PARTIAL.** All 31 member identities are preserved in canonical data and confirmed complete at every usable state (31/31, cross-verified in the GEFS pilot audit). Ensemble summary statistics (mean/median/std/min/max/p10/p25/p75/p90 of daily Tmax) ARE already computed and stored in `structured_features.parquet`, enabling mean/median/std/quantiles/range/disagreement/change-in-mean/change-in-spread to be derived directly without re-downloading. **However, member-level TRAJECTORIES (temperature at each valid_time, per member) are NOT preserved** -- only each member's own pre-collapsed daily max (`gefs_members.parquet`). A future sequence-level GEFS feature (e.g. "trajectory shape disagreement across members") would require re-deriving from the frozen canonical GEFS Parquet files (which do retain this, so no re-download would be needed) rather than reading it from the integrated dataset directly.

## 15. Source-disagreement support

Source identity is fully preserved (never averaged away) in `structured_features.parquet` -- `hrrr_forecast_tmax_f`, `gfs_forecast_tmax_f`, `nbm_forecast_tmax_f`, `ecmwf_deterministic_forecast_tmax_f`, and `gefs_tmax_mean_f` are all separate columns, plus pre-computed `cross_model_mean_tmax_f`/`cross_model_median_tmax_f`/`cross_model_std_tmax_f`/`cross_model_range_tmax_f`. Any pairwise disagreement (HRRR-vs-GFS, GFS-vs-ECMWF, deterministic-vs-GEFS, etc.) can be computed directly from these columns. **PASS** for temperature; limited to temperature since other variables are not yet threaded through.

## 16. Missingness-semantics result

Confirmed the dataset distinguishes all four required states without collapsing any to zero:
- **PRESENT**: a real extracted value.
- **NOT_YET_AVAILABLE**: `eligible_mask()`'s no-lookahead filter (0 violations, verified across all 480 states).
- **STRUCTURALLY_NOT_APPLICABLE**: e.g. NBM's F0 (doesn't exist), GFS/GEFS/ECMWF's F0 accumulation fields, ECMWF's missing RH/cloud-cover, observations' 0%-populated RH column -- all documented, never fabricated as zero.
- **MISSING_UNEXPECTEDLY**: 0 found in this pilot's audits to date.

**PASS.**

## 17. ML architecture readiness

| Architecture | Status | Why |
|---|---|---|
| A. Traditional ML baseline (XGBoost/LightGBM) | **PARTIALLY READY** | Temperature-centric tabular features (forecast Tmax per source, ages, revisions, cross-model stats, GEFS ensemble stats, observation Tmax_so_far, time features) are complete and correct today. Missing: dewpoint/wind/pressure/cloud/precipitation/radiation as tabular features, which would likely improve a Tmax model (e.g. dewpoint depression, cloud cover, and wind are all physically relevant to daily Tmax). |
| B. MLP using engineered weather-state features | **PARTIALLY READY** | Same reasoning as A -- usable today on the existing temperature-centric feature set, limited by the same missing variable families. |
| C. Sequence model using forecast trajectories | **PARTIALLY READY** | Clean, single-variable (instantaneous temperature) trajectories are available and correct for HRRR/GFS/NBM/ECMWF today. A richer multivariate sequence (temperature+dewpoint+wind+cloud together per valid_time) is not currently available. |
| D. GEFS ensemble-derived features | **PARTIALLY READY** | Ensemble summary statistics of daily Tmax are complete and correct. Member-level trajectory-based features are not currently available in the integrated dataset (though recoverable from frozen canonical GEFS data without re-download). |
| E. Later AFD BERT/ModernBERT text encoder | **READY** | Full raw text preserved for 100% of documents, with issuance time, product ID, and section structure already available; nothing currently blocks starting this work when authorized. |

## 18. Potentially useful variables not currently extracted

Based only on variables actually confirmed available in the already-validated source products (not speculative):

**HIGH POTENTIAL VALUE**
- HRRR relative humidity -- confirmed directly available and uniquely selectable in the raw archive (same product family as GFS's already-validated RH), not currently extracted for HRRR by deliberate choice (source-identity-preservation decision, not a technical barrier).
- Dewpoint depression / RH as an engineered feature from existing temp+dewpoint pairs (all sources already have both) -- no new extraction needed, a feature-engineering opportunity using data already in hand.

**POSSIBLY USEFUL**
- NBM/GEFS/ECMWF native period Tmax/Tmin as an EXPLICIT separate feature (not mixed into temperature, but preserved alongside it) -- these products are specifically engineered by their providers to capture intra-period peaks that hourly snapshots can miss; currently extracted into canonical data but not threaded into the integration layer at all (see section 9).
- Cloud cover as a same-source cross-check against solar radiation (HRRR/GFS/GEFS all have both; physically correlated, could help validate either signal).

**LOW PRIORITY**
- Wind gust (HRRR/GFS/NBM/GEFS/ECMWF do not report gust as a distinct forecast field in the currently-extracted product set; only present in observations, and even there only 16-22% populated).
- Visibility / weather codes -- observation-only, low completeness (18-28% for weather codes), unlikely to meaningfully inform a daily Tmax model.

No new extraction, no schedule changes, and no methodology changes were made based on this section -- purely informational, per instruction.

## 19. Decision gate

**NEEDS REVIEW.**

The gate requires "no unexplained pipeline variable loss" and "useful non-temperature variables remain preserved" for a PASS. Section 9's pipeline-survival audit found a genuine, material gap: dewpoint, wind, pressure, cloud cover, precipitation, solar radiation, and native period Tmax/Tmin are all correctly extracted and present in canonical frozen data for every numerical source, but none of them currently reach `structured_features.parquet`, `forecast_trajectories.parquet`, or the human-readable table -- only temperature survives the full pipeline today. GEFS member-level trajectories (as opposed to per-member daily-max scalars) have the same gap.

This is NOT a regression from the 2026-09-29 variable-mixing fix (that fix correctly REMOVED contamination from the temperature series; it never added a pathway for the other variables to reach the integrated dataset, because no such pathway existed before either). It is a pre-existing scope limitation of `scripts/build_integrated_pilot.py`'s current feature extraction, now made explicit by this audit.

Per instruction, no code was changed to address this in this audit phase -- source methodology, extraction, and the already-validated temperature pipeline were left untouched.
