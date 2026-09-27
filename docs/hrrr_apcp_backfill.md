# HRRR APCP_1H Backfill

Status: investigation complete, backfill in progress. Additive to the frozen HRRR pilot -- no existing data deleted, replaced, or altered.

## Background

The frozen HRRR pilot (`scripts/pilot_phase3_hrrr.py`, 20,836 work items, 835 Parquet parts, complete) persisted `"APCP"` using `find_message()`, which takes the first `(variable, level)` match in HRRR's `.idx` file. HRRR publishes **two** APCP:surface candidates at every forecast hour beyond F0: a cumulative-since-run-start total (`"0-N hour/day acc fcst"`) and a direct 1-hour windowed accumulation (`"(N-1)-N hour acc fcst"`). Idx ordering happens to list the cumulative one first, so the frozen dataset's `"APCP"` column is cumulative precipitation since each run's own start -- verified by direct value comparison (not ordering inference) against 27 representative work items spanning F1-F46 and 9 dates.

Both quantities are legitimately useful and distinct for a Tmax model (recent/windowed precipitation as a point-in-time atmospheric-state signal; cumulative as a longer-memory surface-wetness signal) -- both are kept, additively.

## Selection rule (data/hrrr.py)

`select_message_explicit(entries, "APCP", "surface", forecast_hour, prefer=...)` parses every candidate's `forecast_desc` and selects by accumulation window, never by idx position:
- `prefer="cumulative_since_start"`: the candidate whose window starts at hour 0 (already persisted as `"APCP"`).
- `prefer="windowed_1h"`: the candidate whose window is exactly 1 hour ending at `forecast_hour` (the new `"APCP_1H"` feature).

At `forecast_hour=1`, both rules correctly resolve to the SAME message (verified byte-identical against real frozen work items) -- the cumulative-since-start and 1-hour-window definitions describe the same physical interval at F1.

## F0 (forecast_hour=0): structurally ineligible for APCP_1H

At F0, HRRR publishes exactly one APCP entry: `"0-0 day acc fcst"`, a zero-length window. **There is no preceding forecast interval at F0** -- a genuine "1-hour accumulation ending at F0" would require data from before the forecast even started, which does not exist.

**Rule: HRRR APCP_1H is defined only for forecast_hour >= 1, because F0 has no preceding forecast interval.**

The 840 F0 work items (out of the full 20,836 in the HRRR pilot) are excluded from the APCP_1H work-item universe *before* extraction is attempted -- they are never submitted to `run_concurrent`, never recorded as checkpoint `FAILED` entries, and never assigned a fabricated value of `0.0`. This is a structural ineligibility, not a missing value and not a physical zero. Eligible APCP_1H work items: `20,836 - 840 = 19,996`.

(An earlier attempt did submit these 840 items and let the explicit selector fail loudly on each -- correct behavior given what it knew, but the right fix is to never submit them at all. The 112 resulting checkpoint `FAILED` entries recorded during that attempt were removed from the APCP_1H checkpoint as a data-quality cleanup, per explicit approval -- the 2,913 genuine `DONE` entries from that same attempt were left untouched.)

## Why not derive APCP_1H by differencing cumulative values?

Empirically tested: differencing `cumulative(fh) - cumulative(fh-1)` against the directly-fetched 1-hour value across 19 consecutive-hour pairs in an active-precipitation run gave **10 exact matches, 9 mismatches (47%)**, with discrepancies of 0.001-0.004 kg/m^2 -- caused by NOAA independently quantizing/packing each accumulation-window GRIB message, not floating-point noise. The direct NOAA product is always fetched; APCP_1H is never reconstructed by differencing.

## Temporal metadata (conceptual; not backfilled onto the frozen schema)

| Feature | temporal_stat | temporal_window_hours |
|---|---|---|
| TMP, DPT, RH, UGRD, VGRD, PRES, TCDC, DSWRF (HRRR) | instant | 0 |
| APCP (existing, frozen, cumulative-since-start) | accum | forecast_hour (varies per row) |
| APCP_1H (this backfill) | accum | 1 |

At F0, cumulative and 1-hour APCP represent the same (zero-length) physical interval conceptually, but F0 has no APCP_1H row at all per the rule above -- there is nothing to assign these metadata values to at F0.

## Output location

Additive dataset at `data/processed/pilot/hrrr_apcp_1h/` (own checkpoint `pilot_hrrr_apcp_1h_checkpoint.json`, own `parts/`) -- the original `data/processed/pilot/hrrr/` tree (835 parts, 20,836-entry checkpoint) is never opened for writing by this backfill.
