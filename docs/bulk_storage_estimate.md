# Bulk Historical Storage Estimate (REVISED after archive-depth + cost audit)

Companion to [`bulk_download_spec.md`](bulk_download_spec.md). This revision
follows a dedicated audit (small metadata/listing/tiny-byte-range requests
only — no bulk downloads) that: (1) verified real archive depth per source
instead of assuming it, (2) discovered a genuine ECMWF product-path schema
change that shortens its safely-usable history, (3) quantified NBM's
hourly-run revision magnitude with real byte-range fetches, (4) confirmed
GRIB2 byte-range fetch behavior directly from the extraction code, and (5)
quantified three GEFS variable-reduction scenarios. Every number below is
still an **ESTIMATE** extrapolated from small measured samples — treat as
order-of-magnitude, not precise.

**Correction from the previous version:** today's date is 2026-09-26. The
previous version of this document assumed "present" ≈ 2025-07-01 (≈912-day
window from 2023-01-01). The real common window if extraction starts now is
**≈1,364 days** (2023-01-01 → 2026-09-26). Both figures are reported below:
the ≈912-day window is kept for an apples-to-apples OLD-vs-OPTIMIZED
comparison (Task 11), and the ≈1,364-day window shows the real number if
extraction actually starts today.

---

## 1. Archive-depth verification results (small listing requests only)

| Source | Earliest tested EXISTS | Latest tested MISSING (older) | 2023-01-01 safely covered? | Confidence |
|---|---|---|---|---|
| HRRR | ≤2015-01-15 (real GRIB2 surface product + idx confirmed at 2023-01-15, 2024-07-15, 2026-07-15) | not found in this probe range | **Yes**, with wide margin | Medium-High |
| GFS | 2021-06-15 EXISTS; **2021-01-15 MISSING** | 2021-01-15 and earlier | **Yes** — boundary is ~5 months before our earliest probe of the window, comfortable margin | Medium-High |
| GEFS | 2021-01-15 EXISTS; **2020-01-15 MISSING** | 2020-01-15 and earlier | **Yes**, comfortable margin | Medium-High |
| NBM | 2021-01-15 EXISTS; **2020-01-15 MISSING** | 2020-01-15 and earlier | **Yes**, comfortable margin | Medium-High |
| ECMWF deterministic | **MAJOR FINDING — see below** | | **NO, not at the currently-coded path** | High (directly observed) |
| Surface observations | 1973 (KLGA/KJFK/KEWR), 2005 (KNYC) — from `isd-history.csv`, previously measured | n/a | **Yes** | High |
| AFD | not independently re-tested this round (unchanged from prior report: UNRESOLVED depth, IEM archive documented to hold years of text products) | | Presumed yes | Low (unchanged) |

**Real surface GRIB2 product confirmed present** (not just some bucket
object) at 2023-01-15 / 2024-07-15 / 2026-07-15 for HRRR (`wrfsfcf01.grib2`
+ `.idx`), GFS (`pgrb2.0p25.f006` + `.idx`), GEFS (`gec00...pgrb2s.0p25.f006`
+ `.idx`), and NBM (`blend...core.f006.co.grib2` + `.idx`) — all EXIST with
both the data object and its `.idx` sidecar at all three dates.

### ECMWF — genuine product-path schema change discovered (important correction)

The previous report's "earliest observed object: 2023-01-18" referred to
*some* object existing in the `ecmwf-forecasts` bucket, not necessarily one
our current `data/ecmwf.py` code can read. This round's listing (with a
`delimiter` to see actual subfolder structure) found **three different path
schemes over time**:

| Period (observed boundaries) | Path structure | Our code compatible? |
|---|---|---|
| through ~2024-01 | `{date}/{run}z/0p4-beta/...` (lower-res beta product) | **No** |
| ~2024-02-01 → 2024-02-27 | `{date}/{run}z/0p25/...` (bare resolution folder, no model-name prefix) | **No** |
| **2024-02-28 → present (confirmed through 2026-07-15)** | `{date}/{run}z/ifs/0p25/{stream}/...` (current scheme, also added a parallel `aifs/` AI-model path) | **Yes — this is what `data/ecmwf.py`'s `grib_key()` already targets** |

**This means ECMWF deterministic's safely-usable history with our current
extraction code starts 2024-02-28, not 2023-01-01 and not 2023-01-18.**
Using 2023-01-01 for ECMWF would require either accepting a ~13-month gap
at the start of its history, or writing separate path-construction logic
for the two older schemes (not attempted here — flagged as a decision
requiring approval, not silently done).

Also newly confirmed: ECMWF run hours are **not** limited to `{0, 12}` as
the previous test's `RUN_HOURS = {0, 12}` comment assumed — the bucket
shows `00z/06z/12z/18z` subfolders present even back in 2023, meaning that
constraint was specific to the one day originally tested, not a real
limitation of the source. This should be corrected in `data/ecmwf.py`'s
comment (not changed here, since it doesn't affect this audit's numbers,
but flagged for the next code-touching phase).

---

## 2. Central Park / KNYC continuity for 2023-present (Task 2)

Re-confirmed via HEAD requests directly against the ISD archive:

| Year | KNYC | KLGA | KJFK | KEWR |
|---|---|---|---|---|
| 2022 | EXISTS (6.54 MB) | EXISTS (8.07 MB) | EXISTS (7.97 MB) | EXISTS (7.47 MB) |
| 2023 | EXISTS (6.69 MB) | EXISTS (8.08 MB) | EXISTS (8.00 MB) | EXISTS (7.62 MB) |
| 2024 | EXISTS (6.51 MB) | EXISTS (7.97 MB) | EXISTS (7.74 MB) | EXISTS (8.02 MB) |
| 2025 | EXISTS (4.31 MB) | EXISTS (5.23 MB) | EXISTS (5.23 MB) | EXISTS (5.26 MB) |
| **2026** | **MISSING** | **MISSING** | **MISSING** | **MISSING** |

All four stations show **continuous, stable, single-station-id annual
files for 2022-2025** — no station-ID change, no gap, within the proposed
window. **2026's annual file does not exist yet for any of the four
stations** (not just KNYC) — this is consistent with ISD publishing each
year's consolidated file only after the year is further along or complete,
not a Central-Park-specific problem, and not evidence the station stopped
reporting. This is flagged as **UNRESOLVED but not concerning**: the
practical implication is that "present" for observations effectively means
"through the most recently *published* annual file" (2025 currently), with
2026-to-date data likely needing a different access pattern (unverified,
out of scope for this audit) if truly current data is needed before the
2026 annual file is published.

**Conclusion: KNYC is fully continuous and safe for the entire proposed
2023-2025 window.** No change to the earlier station-continuity finding
(pre-2005 fragmentation remains irrelevant to this window).

---

## 3. NBM hourly-run cadence investigation (Task 3) — real measurements

Fetched 7 consecutive hourly NBM runs (06Z-12Z, 2025-07-01) via real
byte-range GETs (28 requests, 34.5 MB total — a small, targeted sample, not
a bulk download), all forecasting the **same fixed target valid time**
(20:00 UTC / 16:00 ET), to see how much the forecast for that one future
hour actually changes as each new hourly run arrives.

| Run | Lead (h) | Central Park temp (°F) | Change from previous run | Cloud cover (%) | Dewpoint (K) |
|---|---|---|---|---|---|
| 06Z | 14 | 84.11 | — | 61 | 294.34 |
| 07Z | 13 | 84.06 | -0.05 | 59 | 294.30 |
| 08Z | 12 | 84.06 | 0.00 | 63 | 294.60 |
| 09Z | 11 | 84.02 | -0.04 | 70 | 294.97 |
| 10Z | 10 | 83.32 | **-0.70** | 73 | 294.50 |
| 11Z | 9 | 84.04 | **+0.72** | 73 | 294.68 |
| 12Z | 8 | 83.98 | -0.05 | 76 | 294.48 |

**Findings (from this one-day, one-target-hour sample — real but limited):**
1. Yes, genuinely new/different forecast content every hour — values change
   run to run, confirming these are not cached/duplicate products.
2. **Temperature for a fixed future hour is remarkably stable hour-to-hour**:
   5 of 6 consecutive-run changes were ≤0.05°F; the largest was a real
   ±0.7°F swing (10Z→11Z).
3. **Cloud cover moved substantially more**: 61%→76% over just 6 hours,
   including a 7-percentage-point jump within a single 3-hour span
   (07Z→10Z: 59%→73%). Since cloud cover directly gates daytime heating
   (already CORE per the variable-selection work), this is the field most
   at risk of losing real information if cadence is cut too aggressively.
4. Dewpoint showed modest, real movement (~±0.3-0.6K ≈ ±0.5-1°F equivalent),
   consistent with genuine atmospheric evolution, not noise.
5. This is a **single day's sample** — NBM cadence behavior may differ on
   more dynamic weather days; this finding should be treated as suggestive,
   not definitive, of the general pattern.

### Cost by cadence (measured per-run message size × forecast-hour count)

| Cadence | Runs/day | Remote bytes/day | Remote (912-day window) | Remote (1,364-day window) |
|---|---|---|---|---|
| 24/day (hourly) | 24 | ~4.06 GB | ~3.7 TB | ~5.5 TB |
| 12/day (every 2h) | 12 | ~2.03 GB | ~1.85 TB | ~2.77 TB |
| 8/day (every 3h) | 8 | ~1.35 GB | ~1.23 TB | ~1.84 TB |
| 4/day (00/06/12/18Z) | 4 | ~0.68 GB | ~0.62 TB | ~0.92 TB |

### Recommendation (Task 3/D/E/F)

**8 runs/day (every 3 hours: 00/03/06/09/12/15/18/21Z) — PENDING APPROVAL.**
Reasoning: the measured cloud-cover swing (7 points within 3 hours) means a
6-hour gap (4/day) could plausibly straddle and hide a comparable
real revision, while temperature itself barely needs sub-3-hourly
resolution given the measured ≤0.05-0.7°F hour-to-hour stability. 8/day is
a 3× reduction from 24/day (66.7% remote-byte savings for NBM specifically)
while keeping resolution finer than the cloud-cover swings actually
observed. **This is a recommendation based on one day's sample, not a
final decision** — it should be approved explicitly, and ideally re-checked
against 2-3 more active-weather days before bulk extraction begins.

---

## 4. Reconstructing the ~13 TB estimate mathematically (Task 4/6)

For every GRIB2 source, remote bytes ≈

```
days × runs/day × forecast_hours/run × variables × members × avg_message_size_MB
```

Spatial extraction (1 point vs. a 3×3 patch) does **not** appear in this
formula — confirmed directly from the extraction code (§5 below): a
byte-range fetch pulls one whole compressed grid message regardless of how
many points are later read from the decoded grid.

| Source | days | runs/day | fh/run (avg, measured) | variables | members | avg msg size | = Remote bytes |
|---|---|---|---|---|---|---|---|
| HRRR | 912 | 24 | 20.3 (measured: 183 rows / 9 runs test, scaled) | 8 | 1 | 1.554 MB | **~5.5 TB** |
| GFS | 912 | 4 | 21.0 (measured: 168 rows / 8 runs) | 8 | 1 | 0.737 MB | **~0.45 TB** |
| GEFS | 912 | 4 | 8 (measured, exact) | 6 | 31 | 0.593 MB | **~3.2 TB** |
| NBM (24/day, old) | 912 | 24 | 22.75 (measured: 91 rows / 4 runs) | 5 | 1 | 1.487 MB | **~3.7 TB** |
| ECMWF det (old, 2023-01-18 start) | 890 | 2 | 8 (measured, exact) | 7 | 1 | 0.743 MB | **~0.07 TB** |

Sum ≈ 5.5+0.45+3.2+3.7+0.07 = **~12.9 TB ≈ 13 TB** — this reproduces the
previous estimate and confirms it wasn't an arithmetic error; it's a direct
consequence of `days × runs/day × forecast_hours × variables × members`,
dominated by **HRRR's 24 runs/day** and **GEFS's 31-member multiplier**.

---

## 5. GRIB byte-range behavior, verified directly from the code (Task 5/6)

Inspected `data/hrrr.py`, `data/gefs.py`: each variable is a **separate
GRIB2 message** inside the run's file, located via a `.idx` sidecar
(one line per message: number, byte offset, variable, level, forecast
description). `fetch_byte_range()` issues one HTTP `Range` GET per message.
**We are doing (A): downloading each GRIB message independently** — and a
real inspection of message layout shows this is the *correct* choice, not
an inefficiency:

Real HRRR `.idx` layout (run 2025-07-01 12Z, forecast hour 6, 173 total
messages): our 8 wanted surface variables (msg #62, 64, 71, 74, 77, 78, 84,
119, 126) are **scattered among 173 messages spanning ~37.8 MB to ~94.4 MB**,
interleaved with ~30 *unwanted* upper-air messages (TMP/DPT/UGRD/VGRD at
250/300/500/700/850/925/1000 mb, 80m wind, cloud-layer variants, etc.). A
single combined range from our first wanted message to our last would pull
**~56.6 MB of unwanted data** — dramatically *more* than fetching our 8
messages independently. **Task 6's "combine adjacent ranges" optimization
does NOT apply favorably here** — verified, not assumed.

GEFS's compact `pgrb2sp25` product is much smaller (38 total messages) and
our 8 wanted variables are more clustered (msg 4-30), but still not
byte-adjacent to each other — combining would still pull some unwanted
messages, though far less (a smaller absolute waste given the file's
overall size). Not worth the added code complexity for the bytes saved.

**Caching that already exists and matters:** the `.idx` file (one small
text/JSON file per run+forecast-hour) is fetched **once** per
(run, forecast_hour) and reused for every variable's `find_message()` call
— already implemented this way in every source module and every feasibility
script. No further idx-caching optimization is available.

**Multiple grid points from one message: confirmed free.** `decode_message()`
runs `eccodes.codes_grib_find_nearest()` (or equivalent) against the
already-downloaded, already-decoded grid in memory — reading N nearby
points instead of 1 adds no additional network request.

---

## 6. Spatial patch cost, verified (Task 7)

Directly confirmed by the code inspection above: a 3×3 patch adds
**zero additional remote-read bytes** (same GRIB2 message, decoded once,
sampled at 9 nearby points instead of 1) and only a **local processed-row**
increase (9× the row count for that source, still tiny in absolute terms —
e.g. HRRR's patch adds ~0.6 GB local storage over the 912-day window vs.
point-only, still under 1% of that source's remote-read volume). The
earlier claim is confirmed, not merely assumed.

---

## 7. GEFS cost optimization (Task 8) — three scenarios, real numbers

Current per-message sizes (measured): TMP 0.44 MB, DPT 0.436 MB, UGRD
0.837 MB, VGRD 0.822 MB, PRES 0.738 MB, APCP 0.286 MB (all per member, per
forecast-hour). 4 runs/day × 8 forecast-hours/run × 31 members = 992
(run,fh,member) combinations/day.

| Scenario | Variables, all 31 members | Variables, reduced representation | Remote bytes/day | Remote (912-day) | Remote (1,364-day) | Savings vs. FULL |
|---|---|---|---|---|---|---|
| **GEFS FULL** (current spec) | TMP, DPT, UGRD, VGRD, PRES, APCP | — | ~3.53 GB | **~3.2 TB** | **~4.8 TB** | — |
| **GEFS TEMPERATURE-FOCUSED** | TMP only | — | ~0.44 GB | **~0.40 TB** | **~0.60 TB** | **-87.6%** |
| **GEFS INTERMEDIATE (recommended, PENDING APPROVAL)** | TMP, DPT (all 31 members) | UGRD, VGRD, PRES, APCP (control member `gec00` only, 32 combos/day) | ~0.95 GB | **~0.87 TB** | **~1.30 TB** | **-72.8%** |

**Recommendation:** INTERMEDIATE. Temperature *and* dewpoint (the two
fields most directly tied to Tmax and heat-index-style moist-heating
effects) keep their full 31-member ensemble uncertainty, never reduced;
wind/pressure/precip — useful mainly as deterministic context, and already
present in HRRR/GFS/ECMWF at full fidelity — drop to a single (control)
member for GEFS specifically. **This changes the specification and
requires explicit approval** — it is not applied automatically here.

---

## 8. Forecast-hour optimization (Task 9)

Reviewed each source's forecast-hour selection against the goal of
reconstructing states at 06/08/10/12/14/16 ET plus arbitrary/event-driven
times. **Finding: there is little further optimization available on the
forecast-hour axis beyond what the original BALANCED plan already
specified** (target-day-valid-times-only, via
`expected_target_day_valid_times()`). Two points confirmed, not changed:

- **HRRR's 24 runs/day must be kept** — this was directly proven necessary
  by this project's own completeness-architecture work (a single run/day
  would have hidden the real, materially-different 91.44°F vs. 87.5°F vs.
  the false 77.2°F intraday transition demonstrated in the prior phase).
  Cutting HRRR runs to "save" forecast-hour cost would reintroduce exactly
  the problem that architecture was built to solve.
- **GFS's 4 runs/day is already its real synoptic cadence** — nothing to
  cut.
- The genuinely large, available savings are on the **NBM run-cadence**
  (§3) and **GEFS variable/member** (§7) axes, not forecast-hour trimming.

No change to forecast-hour rules from the original spec for
HRRR/GFS/GEFS/NBM/ECMWF-det.

---

## 9. Revised LEAN / BALANCED / RICH (Task 11)

Using the corrected ECMWF window (2024-02-28 start) and the NBM/GEFS
recommendations from §3/§7. Two window lengths shown: the original ~912-day
window (for apples-to-apples comparison with the prior estimate) and the
real ~1,364-day window (2023-01-01 → 2026-09-26, if extraction starts now).

| | LEAN | **BALANCED (optimized, recommended)** | RICH |
|---|---|---|---|
| Sources | Obs + AFD + HRRR + GFS + NBM (4/day) | + GEFS (INTERMEDIATE) + ECMWF det (corrected window) | + ECMWF ensemble (still deferred) + wider patches |
| NBM cadence | 4/day | **8/day (recommended, pending approval)** | 8/day |
| GEFS variant | excluded | **INTERMEDIATE (pending approval)** | FULL |
| Remote bytes, 912-day window | ~1.6 TB | **~8.1 TB** | ~15+ TB (GEFS FULL + wider patches) |
| Remote bytes, 1,364-day window (real, if starting now) | ~2.4 TB | **~12.1 TB** | ~22+ TB |
| Local storage, 912-day window | ~0.3 GB | **~1.6 GB** | ~4-5 GB |
| Local storage, 1,364-day window | ~0.45 GB | **~2.4 GB** | ~6-7 GB |

---

## 10. OLD vs. OPTIMIZED BALANCED (Task 11/13, apples-to-apples at the SAME 912-day window)

| | OLD BALANCED | OPTIMIZED BALANCED | Change |
|---|---|---|---|
| HRRR | 5.5 TB / 1.10 GB | 5.5 TB / 1.10 GB | unchanged (required for point-in-time fidelity) |
| GFS | 0.45 TB / 0.19 GB | 0.45 TB / 0.19 GB | unchanged (already minimal) |
| NBM | 3.7 TB / 0.78 GB (24/day, unresolved) | **1.23 TB / 0.26 GB (8/day, recommended)** | **-66.7%** |
| GEFS | 3.2 TB / 0.19 GB (FULL, 6 vars × 31 members) | **0.87 TB / 0.067 GB (INTERMEDIATE)** | **-72.8%** |
| ECMWF det | 0.07 TB / 0.004 GB (wrong start date: 2023-01-18) | **0.04 TB / 0.002 GB (corrected start: 2024-02-28)** | -42% (a correction, not an optimization) |
| Obs + AFD | 0.025 TB / 0.27 GB | 0.025 TB / 0.27 GB | unchanged |
| **TOTAL** | **≈13.0 TB / ≈2.6 GB** | **≈8.1 TB / ≈1.6 GB** | **≈-37% remote, ≈-38% local** |

**A ~37% reduction, at the same window length** — driven entirely by the
NBM cadence decision and the GEFS variable-reduction decision, both marked
PENDING APPROVAL, not automatically applied. HRRR and GFS were
deliberately left unchanged because this audit found no defensible way to
cut them without reintroducing point-in-time reconstruction problems this
project has already solved once.

---

## 11. Is 13 TB real? (Task 12)

**Moderate reduction possible — confirmed quantitatively, not "major" and
not "already correct."** The original ~13 TB figure was **not** the result
of inefficient GRIB access technique (§5 confirms per-variable independent
fetching is already close to optimal given how variables are scattered
through each file) — it was the direct, correctly-calculated consequence of
choosing NBM's cadence and GEFS's variable set at their most conservative
(most information-preserving) settings. Deliberately trading a **measured,
quantified amount** of NBM run-density and GEFS variable breadth for a
~37% reduction is real and defensible; further reduction would require
cutting HRRR's run cadence or GFS's variables, which this audit found no
justification for.

---

## 12. Historical model-state count (unchanged methodology, updated window)

At the real ~1,364-day window: **1,364 target days × 6 fixed times =
8,184 fixed-time states**; event-driven states scale proportionally from
the prior estimate (~30,000-40,000 at 912 days) to roughly **~45,000-60,000**
genuinely-new-information events, or up to ~415,000 raw candidate states if
every observation tick were materialized (still not recommended, per the
prior report).

---

## Notes on confidence

Archive-depth findings (§1) are **Medium-High confidence** — directly
observed via listing/HEAD requests, not extrapolated. The ECMWF path-scheme
finding is **High confidence** — directly observed, reproducible. The NBM
cadence and GEFS-scenario numbers are **Medium confidence** for the
*mechanism* (message sizes and combo counts are measured) but **Low-Medium**
for the *day-to-day representativeness* (based on one day's sample each).
