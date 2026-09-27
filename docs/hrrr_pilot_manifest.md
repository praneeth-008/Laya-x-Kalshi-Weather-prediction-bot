# HRRR Pilot Extraction — Final Manifest

Status: **COMPLETE — FINAL PASS**

## Target

- Location: Central Park, NYC (lat 40.7794, lon -73.9691)
- Extraction pattern: 3x3 patch, +/-0.03 degree offsets around the center point
- Variables (CORE_VARS): TMP, DPT, UGRD, VGRD, PRES, TCDC, APCP, DSWRF (2m/10m/surface/entire-atmosphere levels as applicable)
- Runs: all 24 UTC hours/day
- Forecast-hour selection: target-day-only (`expected_target_day_valid_times`, see `data/weather_state.py`)

## Pilot scope

- Selected representative days: 40 (systematic stratified sample, see `scripts/pilot_select_representative_days.py` and `data/processed/weather/pilot/selected_days.csv`)
- Date range represented: 2025-01-02 to 2025-08-24 (selected days; earliest lead-in run 2025-01-01)

## Final verified state

| Metric | Value |
|---|---|
| Planned work items | 20,836 |
| DONE | 20,836 |
| FAILED | 0 |
| Missing/unaccounted | 0 |
| Duplicate checkpoint entries | 0 |
| Parquet parts | 835 |
| Total rows | 1,500,192 |
| Duplicate logical keys | 0 |
| Corrupt/unreadable parquet files | 0 |
| Orphan outputs (no DONE entry) | 0 |
| Row-count mismatches (checkpoint vs. parquet) | 0 |
| Decode failures | 0 |
| Grid-cache invalidations | 0 |
| Temp-file leaks | 0 |
| Grid fingerprint | `md5:78367561440d7c7b608b8532a02e4780` (1 fingerprint observed for the entire pilot) |
| Max patch-point distance from requested coordinate | 1.72 km (sanity threshold: 10 km) |
| Extraction code version | commit `36ba8cb` (validated grid-index-cache implementation) |
| Production worker count used | 24 (runtime override; committed default is 12) |
| Scientific integrity status | **PASS** |

## Notes

- The 835 Parquet parts and the checkpoint's row-level detail are intentionally **not** stored in Git (see `.gitignore` — `data/processed/pilot/hrrr/parts/*` is excluded). Only this manifest, the checkpoint (`pilot_hrrr_checkpoint.json`), and the download report (`pilot_hrrr_download_report.json`) are tracked, as lightweight metadata.
- `data/pilot_extraction.py`'s grid-index cache (commit `36ba8cb`) was validated against the pre-cache implementation on real HRRR messages (bit-exact equivalence, zero index/value mismatches) before this production run, and the full post-run integrity audit found zero cache invalidations, zero grid drift, and a single stable grid fingerprint across all 1,500,192 rows.
- 2 work items required a retry due to ordinary transient S3 (`HTTP 503`) throttling during the initial resume; both succeeded on the next attempt using the unmodified production extraction path.
