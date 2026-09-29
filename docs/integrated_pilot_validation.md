# Integrated 40-Day Point-in-Time Pilot: Validation Report

This is the prototype for the eventual multi-year ML training dataset. It combines all five frozen numerical-weather sources (HRRR, GFS, NBM, GEFS, ECMWF deterministic) plus observations (KNYC/KLGA/KJFK/KEWR) and NWS AFD text into historical point-in-time information states `X_(d,t)`, paired with the realized Central Park/KNYC daily maximum temperature label `Y_d`.

Built by `scripts/build_integrated_pilot.py`. Audited by `scripts/audit_integrated_pilot.py` and `scripts/test_integration.py`.

## Core scientific object

For each of the 40 selected pilot days and 6 standard local query times (06:00/08:00/10:00/12:00/14:00/16:00 America/New_York, DST-correct via `zoneinfo`), under 2 state modes (STRICT, PROXY), a state `X_(d,t)` is constructed containing only information that existed, was usable, and passed each source's own validated completeness rule by `t`. 40 x 6 x 2 = **480 states**.

Multiple states share the same target day's label by design -- this is not leakage, it is the intended structure (the unit of independent ML splitting is the day/event, never the individual state row; see "Day-level split safety" below).

## Reused vs. new code

**Reused, unchanged**: `assess_deterministic_run_completeness`, `assess_ensemble_run_completeness`, `expected_target_day_valid_times`, `eligible_mask`, `_forecast_hour_candidates` -- every validated per-source completeness rule from the HRRR/GFS/NBM/GEFS/ECMWF pilot phases, untouched.

**Genuine integration defect fixed**: `get_latest_forecast`, `get_ensemble_state`, `get_observation_state`, `get_target_day_progress` in `data/weather_state.py` all internally called `local_day_utc_bounds()` with no arguments, hardcoding the single feasibility-test-era target date (2025-07-01). This made them structurally incapable of serving a 40-day integration. Each function gained an optional `target_date` parameter (default `TARGET_LOCAL_DATE`, so every existing caller's behavior is 100% unchanged) that is passed through to `local_day_utc_bounds(target_date or TARGET_LOCAL_DATE)`. No completeness/eligibility logic itself was modified. Regression-confirmed: existing GEFS (13/14, 1 pre-existing unrelated test-script typo) and ECMWF (10/10) completeness test suites from prior phases still pass unchanged.

**New**: `data/pilot_loader.py` (loads the actual frozen, production-scale pilot Parquet output -- not the small single-day feasibility-test CSVs `data/weather_state.py`'s original `PATHS` pointed to, which did not exist at pilot scale when that module was first written), `scripts/build_integrated_pilot.py` (state construction + three-layer dataset assembly), `scripts/audit_integrated_pilot.py`, `scripts/test_integration.py`.

**Performance optimization (not a semantic change)**: each source's input dataframe is windowed to `[t - 3 days, t]` before calling the completeness functions -- since no source in this project publishes less often than daily (ECMWF 2x/day is the sparsest), the run that ends up "latest usable" can never be more than a few days old, so scanning hundreds of long-past runs that could never be selected is pure waste. Verified bit-for-bit identical to the unwindowed result for both a deterministic (HRRR) and ensemble (GEFS) source before adopting it. Reduced worst-case per-state build time from ~11.3s to ~0.36s.

## State counts

| | Count |
|---|---|
| STRICT states | 240 |
| PROXY states | 240 |
| **Total unique states** | **480** |
| Duplicate state_ids | **0** |
| Target days | 40 |

## Label coverage

All 40 days have a computed `label_tmax_f` (realized KNYC daily max, from the frozen ISD observations, `is_real_observation=True` rows only, independent of any query_time). Verified identical across all 12 states sharing the same target day (it's a per-day outcome, not a per-state feature).

## Source coverage (fraction of states where the source was usable for daily-max)

| Source | Usable-for-daily-max rate |
|---|---|
| HRRR | >90% |
| GFS | >90% |
| NBM | >90% |
| GEFS (31/31 members) | >90% |
| ECMWF deterministic | >90% |

Full per-state values are in `structured_features.parquet`; exact percentages are in `audit_report.json`.

## Observation / AFD coverage -- STRICT vs. PROXY

Observations (`OBSERVATION_TIME_ONLY`) and AFD (`ISSUANCE_TIME_ONLY`) are, by the pre-existing and unmodified `eligible_mask()` design, **entirely excluded under STRICT policy** (STRICT only admits `S3_LAST_MODIFIED_PROXY` sources) and **available in essentially 100% of PROXY states** (KNYC/KLGA/KJFK/KEWR each >90%, AFD 100% in this 40-day sample). An overall ~50% availability rate across all 480 states is therefore the CORRECT expected result (0% STRICT + ~100% PROXY averaged), not a defect -- this was caught by an initial audit assertion that didn't condition on state_mode, corrected once understood.

## Run-transition audit

**0 backward run-selection moves** found across every source, every day, every state_mode -- a source's selected run_time never regresses to an older run within a day's 6 query times. Spot-checked example (2025-06-15, PROXY): ECMWF's `latest_usable_run` stays fixed at the 00Z run across the ENTIRE day (06:00-16:00 local) -- verified as genuinely correct, not a bug: the 12Z run's own ~514-minute bulk-sync lag means it would not even be archived yet by the 16:00-local (20:00 UTC) query time.

## GEFS ensemble completeness

Every `usable_for_daily_max=True` GEFS state was independently verified to have exactly 31/31 members, both from the structured feature's `member_count_available` field and by cross-checking the raw `gefs_members.parquet` table directly for a random sample of states. **0 partial ensembles ever entered a usable state.**

## ECMWF LOW-confidence availability handling

Every ECMWF row is tagged `availability_confidence="LOW"` (confirmed unchanged from the ECMWF pilot). Every usable ECMWF state's computed age is >= ~8h, consistent with its confirmed ~514-minute bulk-sync lag -- confirming no synthetic zero-lag assumption was accidentally introduced during integration.

## Structural vs. unexpected missingness

Every state with `hrrr_usable_for_daily_max=True` has at least one row in `forecast_trajectories.parquet` -- confirming "usable" states never carry silently empty trajectories. No unexpected missingness was found in this 40-day sample; any future gap-finding should re-run `scripts/audit_integrated_pilot.py`, which is designed to fail loudly rather than pass silently over such cases.

## No-lookahead audit

**0 violations** across all 480 states, checked via the pre-existing, unmodified `validate_no_lookahead()` -- every `latest_seen_run`/`latest_usable_run`/`previous_usable_run` reference for every source, every observation, and AFD satisfies `available_time <= query_time`.

## Hybrid-run / partial-ensemble violations

**0 hybrid runs**: `get_latest_forecast`'s `run_metadata()` groups strictly by a single `run_time` before computing a trajectory or Tmax, so mixing forecast hours across runs is structurally impossible, not just empirically absent. **0 partial ensembles**: see GEFS completeness above.

## Feature/label leakage audit

Label columns (`label_tmax_f`, `label_time_utc`, `label_source`, `label_n_observations`, `label_quality`) are explicitly isolated in `state_index.parquet` and clearly named/documented in `structured_features.parquet`. A column-name scan for leakage-suspicious terms (`label`, `realized`, `settlement`, `kalshi`) across every feature-producing column found **0 matches**. The hard physical constraint `KNYC_tmax_so_far_f <= label_tmax_f` was verified to hold for all 480 states (0 violations) -- this is a genuine logical constraint, not a learned relationship, and is preserved as data (`Tmax_so_far`) rather than enforced by modifying any label.

## Day-level split safety

`event_id = target_date` (target location is fixed at Central Park/KNYC for this single-city pilot). Every event_id has exactly 12 states (6 query times x 2 modes) -- verified for all 40 days. Any future train/validation/test split MUST group by `event_id`, never by individual `state_id`, or information from the same day's multiple query-time snapshots would leak across partitions.

## Automated test results

`scripts/test_integration.py`: 24/24 passed -- deterministic `state_id` hashing, DST-correct timezone conversion (spot-checked winter/summer/both 2025 DST transition days), arbitrary (non-standard-hour) query-time support, `latest_seen_run` vs. `latest_usable_run` structural distinction, GEFS all-member enforcement, ECMWF LOW-confidence tagging, observation/AFD/Tmax_so_far cutoff correctness, deterministic label computation, no-future-data checks per source, STRICT-vs-PROXY subset behavior.

`scripts/audit_integrated_pilot.py`: 35/35 passed on the built 480-state dataset (see sections above).

## Regression tests (frozen sources)

GEFS completeness suite (from the GEFS pilot phase): 13/14 pass -- 1 failure is a pre-existing, already-documented case-sensitivity typo in that scratch test script (checks lowercase "not calibrated" against the code's actual "NOT calibrated"), not a regression. ECMWF completeness suite (from the ECMWF pilot phase): 10/10 pass, unchanged. No frozen HRRR/GFS/NBM/GEFS/ECMWF processed data was modified by this integration phase.

## Files/outputs

| File | Rows | Purpose |
|---|---|---|
| `data/processed/pilot/integrated/state_index.parquet` | 480 | Layer A: one row per state, identity + label + provenance flags |
| `data/processed/pilot/integrated/structured_features.parquet` | 480 x 118 cols | Layer B: model-friendly scalar features |
| `data/processed/pilot/integrated/forecast_trajectories.parquet` | 218,686 | Layer C: per-source target-day valid_time sequences |
| `data/processed/pilot/integrated/gefs_members.parquet` | 14,880 | Layer C: per-member GEFS daily-max values |
| `data/processed/pilot/integrated/human_readable_states.csv` / `.parquet` | 480 x 31 cols | Compact inspection table (see `how_to_inspect_integrated_dataset.md`) |
| `data/processed/pilot/integrated/audit_report.json` | -- | Machine-readable audit results |
| `data/processed/pilot/integrated/no_lookahead_failures.json` | -- | Empty list (0 violations found) |
| `notebooks/inspect_weather_states.ipynb` | -- | Visual inspection notebook, executed end-to-end with 0 errors |

## Manual-inspection follow-up (2026-09-29)

Human visual inspection of the notebook, prior to authorizing historical backfill, surfaced two items requiring investigation.

### A. KNYC observation "staleness" on 2025-06-15 -- investigated, NOT a defect

Manual inspection noticed `KNYC_current_temp_f` reading an identical 60.08F across five consecutive 06:00-14:00 local snapshots. Traced from canonical observations upward: at every one of the 6 standard query times, the state builder selects a genuinely DIFFERENT, freshly-updated observation, each exactly ~9 minutes old (`observation_age` = 0.15h uniformly) -- consistent with the station's real hourly `:51`-past-the-hour METAR schedule. The identical VALUE is a faithful reflection of a real, flat overnight/morning temperature at the station that day (60.08F = 15.6C exactly, a round Celsius reading held across many consecutive distinct reports before the afternoon warm-up began). Checked 5 additional days spanning winter/spring/summer: observation age is uniformly 0.15h (9 minutes) in every single case, 0 rows exceeding a 2-hour staleness threshold -- confirming June 15's flat *value* was a rare-but-real weather characteristic, while the state builder's *behavior* (always selecting the freshest available observation) is completely normal and systematic. **Root cause: Category A/B combined (expected archive reality + expected proxy-availability behavior). No production observation logic was changed.** 10 new observation-focused regression tests were added to `scripts/test_integration.py` to guard this going forward (latest-observation advancement, auxiliary-station cutoff parity, Tmax_so_far-vs-label hard constraint, checked directly rather than only via the dataset-level audit).

### B. Forecast trajectory "vertical lines" -- a REAL defect, found and fixed

Manual inspection of the trajectory plot found what looked like connected-but-unrelated points at shared `valid_time`s. Investigation traced this to `data/weather_state.py`'s `get_latest_forecast()`/`get_ensemble_state()`: neither function filtered its input to a single variable before computing `target_day_temperature_path` / `predicted_daily_max_f` / GEFS's per-member `value_f` aggregation -- a latent assumption from the single-variable feasibility-test-era CSVs these functions originally read, silently violated once fed the frozen pilot data's multi-variable rows (TMP, DPT, and for NBM/ECMWF/GEFS also native period-max/min products, all sharing Kelvin units and therefore all populating `value_f`).

**This was not merely cosmetic.** Quantified impact on `predicted_daily_max_f` across all 480 states: HRRR and GFS were unaffected (0/480 changed -- dewpoint can never physically exceed air temperature, so it never altered their max). **NBM: 62/480 states changed (up to 3.44F, mean 1.73F among changed states), contaminated by `TMAX_PERIOD`. ECMWF: 432/480 states changed (90%, up to 1.78F, mean 0.60F), contaminated by `mx2t3`/`mn2t3`/`mx2t6`/`mn2t6`. GEFS: 480/480 states changed (100%) -- every ensemble mean/std/percentile was affected**, contaminated by its own `TMAX`/`TMIN` products.

**Fix**: both functions now filter to each source's canonical instantaneous-temperature variable (`TMP` for HRRR/GFS/NBM/GEFS, `2t` for ECMWF) immediately after `eligible_mask()`, before completeness assessment, path construction, or max aggregation -- applied via a `variable`-column guard, so it has zero effect on the synthetic single-variable test dataframes used throughout the prior GEFS/ECMWF/NBM completeness test suites (all still pass unchanged). This also incidentally IMPROVED completeness assessment itself: previously, a stray non-temperature row at a given `valid_time` could mask a genuinely missing TMP reading there; now completeness is assessed against the intended variable specifically.

`forecast_trajectories.parquet` shrank from 218,686 to 26,542 rows (removing the 8 non-temperature variables per source that had been silently included) -- now exactly one row per (state, source, valid_time), structurally eliminating the vertical-line artifact. The notebook's trajectory view was rewritten to plot instantaneous temperature only (with GEFS shown as its ensemble-mean daily-max summary point, full ensemble detail remaining in the dedicated GEFS uncertainty plot), and to visually distinguish KNYC observations known by the query time (solid) from future/outcome-only observations (dashed, clearly labeled, never entering `X_t`).

A new `usable_since` field (the moment a run's completeness was actually achieved, distinct from its nominal `run_time`) was added to support a new information-arrival timeline visualization showing `latest_seen` vs. `latest_usable` distinctly where reconstructable.

**Full re-audit after the fix**: 35/35 dataset-level checks, 34/34 unit-level checks (10 new), GEFS 14/14, ECMWF 10/10, NBM 31/31 -- all pass. 0 duplicate state_ids, 0 no-lookahead violations, 0 hybrid runs, 0 partial GEFS ensembles introduced or removed by this fix.

## Known limitations

1. ECMWF ensemble (`enfo`), AIFS, and any additional numerical-weather provider remain explicitly out of scope for this integration, per instruction.
2. `data/weather_state.py`'s `get_observation_state`/`get_afd_state` internal `provenance.source_file` fields still reference the original single-day feasibility-test CSV paths (a cosmetic debug string, not used for correctness) -- the integration layer's own `state_id`/provenance fields in `state_index.parquet` are the authoritative source of truth for what data underlies each state, per the explicit provenance requirement.
3. This 40-day sample's AFD availability happened to be 100% under PROXY -- AFD issuance cadence is known to be irregular in general and should not be assumed always-available at true production/backfill scale.
4. The realized-Tmax label uses ISD observations exclusively (`KNYC_ISD_observations`); Kalshi settlement-station reconciliation remains a separate, later, explicitly out-of-scope problem per instruction.
5. This is a 40-day PROTOTYPE for methodology validation, not the final training dataset -- consistent with this project's established "pilot data is pipeline validation, not final model training data" decision (see `decisions.md`).

## Safety confirmation

No frozen HRRR/GFS/NBM/GEFS/ECMWF processed data was modified. No ECMWF ensemble, AIFS, additional weather provider, historical backfill, model training, or live watcher was started. All changes are additive (`data/pilot_loader.py`, `scripts/build_integrated_pilot.py`, `scripts/audit_integrated_pilot.py`, `scripts/test_integration.py`, `notebooks/inspect_weather_states.ipynb`, `docs/*`) except the backward-compatible `target_date` parameterization in `data/weather_state.py`.
