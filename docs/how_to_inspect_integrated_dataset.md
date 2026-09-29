# How to Inspect the Integrated Weather Dataset (Beginner-Friendly)

This guide assumes you have never worked with Parquet files or ML datasets before. It explains exactly what this dataset is and how to look at it.

## 1. Where is the human-readable file?

`data/processed/pilot/integrated/human_readable_states.csv`

This is a plain CSV file -- you can open it in Excel, Google Sheets, Numbers, or any text editor.

## 2. How do I open it?

Double-click it, or open Excel/Sheets and use File -> Open. Every row is one "state" (one historical snapshot). There are 480 rows.

## 3. What does one row mean?

One row = "here is everything our system knew about a specific NYC weather day, as of one specific historical moment in time."

For example, one row might represent: *"As of 12:00 PM local time on June 15, 2025, here is what HRRR/GFS/NBM/GEFS/ECMWF were forecasting, what the actual observed temperature was so far that day, and what the AFD forecaster had written."*

The `target_date` column says which day the row is ABOUT. The `query_time_local` column says WHEN we're pretending to stand in history and ask "what do we know right now?"

## 4. What do the major column families mean?

| Column group | Meaning |
|---|---|
| `state_id`, `target_date`, `query_time_local`, `state_mode` | Identity: which day, which moment, which "mode" (see below) |
| `label_tmax_f` | **The answer** -- the actual realized highest temperature that day at Central Park (KNYC). This is what we're eventually trying to predict. It is the SAME for every row of the same `target_date`. |
| `KNYC_current_temp_f`, `KNYC_tmax_so_far_f` | What the Central Park weather station had ACTUALLY measured by that moment (current reading, and the highest reading seen so far that day) |
| `hrrr_*`, `gfs_*`, `nbm_*`, `gefs_*`, `ecmwf_deterministic_*` | What each weather model was forecasting for that day's high temperature, as of that moment, plus how old (`_age_hours`) that forecast was |
| `cross_model_*` | Simple statistics comparing the 5 models to each other (average, spread, etc.) |
| `afd_*` | Information about the human-written NWS forecast discussion available at that moment |

## 5. What is STRICT vs. PROXY (`state_mode`)?

Some information sources in this project have a very precise, trustworthy timestamp for "when did this become knowable" (the 5 weather models -- HRRR/GFS/NBM/GEFS/ECMWF). Others (real observations, the AFD text) only have an approximate stand-in timestamp.

- **STRICT** rows only use the 5 weather models. Observations and AFD columns will be empty/unavailable.
- **PROXY** rows use everything, including observations and AFD, accepting their less-precise timestamps as a reasonable approximation.

Use PROXY if you want the fullest picture. Use STRICT if you want to be maximally conservative about what "definitely known by this exact instant" means.

## 6. How do I launch the notebook?

1. Open a terminal in the project folder.
2. Run: `jupyter notebook notebooks/inspect_weather_states.ipynb` (or open it in VS Code / JupyterLab).
3. Run every cell from top to bottom (in Jupyter: Cell -> Run All, or in VS Code: "Run All" button at the top of the notebook).

## 7. How do I change which day I'm looking at?

Near the top of the notebook, there is a cell that looks like:

```python
TARGET_DATE = "2025-06-15"
STATE_MODE = "proxy"
```

Change `"2025-06-15"` to any of the 40 selected pilot days (see `data/processed/weather/pilot/selected_days.csv` for the full list), then re-run all the cells below it.

## 8. How do I change STRICT vs. PROXY?

Change `STATE_MODE = "proxy"` to `STATE_MODE = "strict"` (or back), then re-run the cells.

## 9. How do I inspect one specific historical moment in detail?

In the notebook, find the cell that calls:

```python
inspect_state(TARGET_DATE, 12, STATE_MODE)
```

The `12` means "the 12:00 PM local snapshot." Change it to `6`, `8`, `10`, `14`, or `16` to inspect a different time of day. This prints a full readable summary: which forecast run each model was using, how old it was, the observed temperature, and the AFD status.

## 10. How do I read each plot?

- **Day timeline**: shows, for each of the 6 times of day, which forecast "run" (e.g. "06Z" meaning the 6 AM UTC model run) each source was using. Dots moving right over time show information updating through the day.
- **Forecast Tmax evolution**: each colored line is one weather model's guess at the day's high temperature, tracked through the day. The black dashed line is the ACTUAL final answer (for reference only -- the models never got to see this line while forecasting).
- **GEFS uncertainty**: GEFS is special -- it runs the weather model 31 different times with slightly different starting conditions ("ensemble members"). This plot shows the middle guess (median) and the shaded range where 80% of those 31 guesses fell.
- **Observation evolution**: the red line is what the Central Park thermometer was actually reading throughout the day. The orange line is the running "highest temperature seen so far today" at each moment.
- **Forecast trajectory view**: for ONE specific moment, shows what each model thought the ENTIRE day's temperature curve would look like (not just the daily high, but hour-by-hour).
- **Missingness matrix**: a colored grid (green = available, red = not) showing, across all 40 days and all 6 times, whether each source had usable data. Useful for spotting systematic gaps.
- **Freshness**: shows how "old" each source's selected forecast typically was when used. ECMWF is expected to look noticeably older/staler than the others -- see `docs/ecmwf_pilot_readiness.md` for why.

## 11. What is a FEATURE versus a LABEL?

- A **feature** is anything the model is allowed to see when making a prediction -- everything except the `label_*` columns. Features only ever contain information that existed by the query time.
- The **label** (`label_tmax_f`) is the answer key -- what actually happened. It is allowed to use information from LATER in the day (since we're not trying to predict the temperature at 8 AM, we're trying to predict what the whole day's high will turn out to be), but it must NEVER be copied into any feature column. This project keeps the label columns clearly separated and named (`label_tmax_f`, `label_time_utc`, `label_source`) specifically so this distinction is never ambiguous.

## Where to go next

- Full validation results: `docs/integrated_pilot_validation.md`
- How each individual weather source was validated: `docs/hrrr_pilot_manifest.md`, `docs/gfs_pilot_readiness.md`, `docs/nbm_pilot_readiness.md`, `docs/gefs_pilot_readiness.md`, `docs/ecmwf_pilot_readiness.md`
- The point-in-time rules this dataset follows: `docs/point_in_time.md`, `docs/live_information_state.md`
