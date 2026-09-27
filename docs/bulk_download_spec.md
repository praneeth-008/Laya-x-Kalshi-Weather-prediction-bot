# Bulk Historical Download Specification

Status: **DESIGN ONLY — not yet approved for execution.** No bulk download has
started. This document is the result of inspecting the repository's existing
feasibility work, source modules, and measured test outputs (July 1, 2025
single-day tests only) — it does not itself contain new downloaded data.

**Revision note (2026-09-26):** a dedicated archive-depth and cost audit
(small listing/metadata/tiny-byte-range requests only) has since verified
several items this document originally flagged UNRESOLVED, and found two
material corrections: (1) ECMWF's product path scheme changed on
2024-02-28 — our current extraction code cannot read anything before that
date, not 2023-01-01 or 2023-01-18 as previously thought; (2) HRRR/GFS/GEFS
/NBM archive depth is now confirmed comfortably covering 2023-01-01 via
real GRIB2-product-plus-idx existence checks, not just generic bucket
objects. See [`bulk_storage_estimate.md`](bulk_storage_estimate.md) for
the full audit and the revised NBM-cadence and GEFS-variable
recommendations (both still PENDING APPROVAL, not silently applied).

Companion documents:
- [`docs/bulk_storage_estimate.md`](bulk_storage_estimate.md) — the numeric
  appendix (remote bytes read, local storage, request counts, historical
  state-count estimate).
- [`config/bulk_download_spec.json`](../config/bulk_download_spec.json) —
  machine-readable version of the **recommended (BALANCED)** plan below,
  for a future downloader to consume. Not implemented yet.

Every factual claim below is labeled:
- **MEASURED** — directly observed from this repository's own feasibility
  tests, source code, or on-disk test outputs.
- **PROPOSED** — a recommendation for the bulk extraction, not yet executed.
- **UNRESOLVED** — a real open question this document does not resolve;
  flagged rather than guessed at.

---

## 1. Target definition

**MEASURED / DEFINED (per this task's explicit instruction):**

- Weather prediction target: **Central Park, New York City, daily maximum
  temperature.**
- Primary observation station: **KNYC / Central Park**
  (`station_id = 725053-94728`, `lat=40.779, lon=-73.969, elevation=42.7m` —
  MEASURED from `data/observations.py`'s `STATIONS` list, itself pulled live
  from NOAA's `isd-history.csv` master station registry).
- KLGA, KJFK, KEWR are **predictors only** — never averaged together or
  substituted for KNYC.
- The eventual model target is `P(Tmax_CentralPark = x | information at t)`.
  This document does not build that model; it specifies the predictor
  dataset only.
- **Trading/settlement target (Kalshi) is explicitly a separate, later
  concern.** Central Park observed Tmax is NOT assumed identical to any
  historical Kalshi settlement value. No Kalshi prices or outcomes are used
  anywhere in this document.

---

## 2. Repository artifacts inspected (Part 1)

- Source/extraction modules: `data/hrrr.py`, `data/gfs.py`, `data/gefs.py`,
  `data/nbm.py`, `data/ecmwf.py`, `data/observations.py`, `data/afd.py`.
- Architecture modules: `data/weather_state.py`, `models/weather_input.py`.
- Feasibility test scripts: `scripts/test_{hrrr,gfs,gefs,nbm,ecmwf,observations,afd}_nyc_feasibility.py`.
- Generated test outputs: `data/raw/weather/*/test/`, `data/processed/weather/*/test/`,
  `data/processed/weather/state/test/`, `data/processed/weather/model_input/test/`.
- Repository conventions: `.gitignore` (excludes `data/raw/*` and
  `data/processed/*` from version control — bulk data was never meant to be
  committed), `config.py` (currently an empty placeholder — no existing
  config-file convention to follow, hence the new `config/` directory).
- Measured file sizes on disk for every raw/processed test directory (see
  §storage estimate doc) and per-GRIB2-message sizes broken out by variable
  for HRRR/GFS/GEFS/NBM/ECMWF (measured directly from the cached test
  message files, not estimated from documentation).

---

## 3. Source-by-source historical availability (Part 4)

**UPDATE (2026-09-26 audit): archive depth has now been directly verified**
via small bucket-listing requests (not full downloads) for HRRR, GFS, GEFS,
and NBM — see [`bulk_storage_estimate.md`](bulk_storage_estimate.md) §1 for
the exact dates tested and objects found. ECMWF's usable history was found
to be **shorter than previously assumed** due to a real product-path schema
change. The table below reflects the audited findings.

| Source | Earliest realistic date | Archive verified by us? | Version/consistency notes | Recommended bulk period | Reason |
|---|---|---|---|---|---|
| Kalshi KXHIGHNY (context only, not re-downloaded here) | ~2021 | **MEASURED** (1874 events discovered, oldest event + 2023 event individually verified end-to-end) | n/a | n/a — out of scope for this task | Already bulk-extracted in an earlier phase |
| HRRR | **MEASURED**: real `wrfsfcf01.grib2` + `.idx` confirmed present at 2023-01-15, 2024-07-15, 2026-07-15; bucket content found back to at least 2015-01-15 | **MEASURED** (small listing/existence checks, not full downloads) | HRRR model version has changed over the years (grid/physics upgrades); not characterized here | **2023-01-01 → present** for the common window | Comfortably covered with wide margin |
| GFS | **MEASURED boundary**: `noaa-gfs-bdp-pds` content EXISTS 2021-06-15, **MISSING 2021-01-15** | **MEASURED** | GFS has had resolution/physics upgrades historically | **2023-01-01 → present** | ~19 months of margin past the real bucket-start boundary |
| GEFS | **MEASURED boundary**: content EXISTS 2021-01-15, **MISSING 2020-01-15** | **MEASURED** | GEFS moved from 21 to 31 members at a past upgrade (2020); our test already confirmed exactly 31 members (`gec00`+`gep01-30`) for 2025-07-01 | **2023-01-01 → present** | ~2 years of margin past the real bucket-start boundary |
| NBM | **MEASURED boundary**: content EXISTS 2021-01-15, **MISSING 2020-01-15** | **MEASURED** | NBM itself has had version bumps (v3.x → v4.x); MEASURED discrepancy already known between NBM's direct TMAX and hourly-TMP-reconstructed max (Part 13) | **2023-01-01 → present** | ~2 years of margin past the real bucket-start boundary |
| ECMWF (Open Data) | **MEASURED, CORRECTED**: a real product-path schema change was found — `0p4-beta` (through ~2024-01) → bare `0p25` (Feb 2024) → `ifs/0p25`+`aifs/` (**2024-02-28 → present**, confirmed through 2026-07-15) — our current `data/ecmwf.py` code only reads the last scheme | **MEASURED** (directly observed path transition) | Two real schema changes found within ~1 month of each other in early 2024; earlier objects use path conventions our code does not construct | **2024-02-28 → present**, NOT 2023-01-01 | Using 2023-01-01 would need separate path logic for two now-superseded schemes — not attempted; requires approval if wanted |
| Surface observations (ISD, KLGA/KJFK/KEWR) | **MEASURED**: `begin: 1973-01-01`; annual files re-confirmed present/stable for 2022-2025 via HEAD requests | **MEASURED** | Continuous single station-id record per `isd-history.csv` (no fragmentation found) | 1973-01-01 → present available; **use 2023-01-01 → present for the common window**, longer history usable in obs-only models | Clean records, but only useful jointly with numerical sources within the common window |
| Surface observations (KNYC/Central Park) | **MEASURED, with a serious caveat**: current clean single-id record spans `2005-01-01 → 2025` (annual files re-confirmed present/stable 2022-2025); the ISD master registry additionally shows fragmented predecessor Central Park records `725033-94728` (1943-1997), `999999-94728` (1965-1997), `725060-94728` (2010-2012) — see §7. **2026's annual file does not yet exist for ANY of the 4 stations** — a publishing-lag artifact, not a Central-Park-specific problem | **MEASURED** | Multiple historical station-id records with an unexplained **1997-2005 gap** in ISD's Central Park record | **2005-01-01 → present** for single-ID continuity; **2023-01-01 → 2025-12-31** for the joint common window (2026 pending annual-file publication) | Anything before 2005 needs cross-ID joining work not attempted here |
| AFD (NWS OKX) | Iowa Environmental Mesonet's IEM archive is documented to hold NWS text products for many years; untested beyond 2025-06-30/07-01 in this round | **UNRESOLVED** (unchanged this round) | AFD text format/section conventions have likely been stable for years but unverified | **2023-01-01 → present** | Matches the common window; a longer AFD-only history is plausible but unverified |

### Primary common training window (PROPOSED)

**2023-01-01 → present** (rolling), for any model that wants ALL sources
simultaneously. Rationale: it's the one boundary we have partial real
evidence for (ECMWF's 2023-01-18 earliest observed object), it comfortably
post-dates GEFS's 31-member upgrade and NBM's early/immature versions, and
it entirely avoids Central Park's pre-2005 station-id fragmentation.

### Longer histories for fewer-source models (PROPOSED)

- **Observations + AFD only**: KLGA/KJFK/KEWR back to 1973-01-01; KNYC back
  to 2005-01-01 (single clean id) — a statistical/ML model using only
  observations and AFD could train on ~20 years, not just ~2.
- **HRRR + observations only**: bucket listing confirmed content back to at
  least 2015-01-15 (this audit's earliest probe date) — a longer HRRR-only
  history is plausible; probing further back (e.g. the commonly documented
  ~2014-07 origin) was not attempted since it wasn't needed for the
  2023-present common window.

### RESOLVED (2026-09-26 audit) — was previously flagged as requiring verification

The archive-depth spot-check this section originally called for has now
been done (small bucket-listing/HEAD requests, no bulk downloads) — see
[`bulk_storage_estimate.md`](bulk_storage_estimate.md) §1 for exact dates
tested and the ECMWF product-path schema-change finding. Remaining open
item: AFD's archive depth was not re-tested this round and stays
UNRESOLVED (unchanged from the original report).

---

## 4. Central Park numerical-model grid strategy (Part 5)

**MEASURED** grid points (from the actual 2025-07-01 test data, requested
`lat=40.78, lon=-73.97` — close to but not exactly KNYC's registry
coordinates `40.779,-73.969`; PROPOSED: use the registry coordinates exactly
for consistency going forward):

| Model | Grid lat/lon (nearest point) | Distance from Central Park | Native resolution |
|---|---|---|---|
| HRRR | 40.7884, -73.9654 | **1.02 km** | ~3 km (Lambert Conformal) — MEASURED from `data/hrrr.py` |
| GFS | 40.75, -74.00 | **4.18 km** | 0.25° (~20-27 km at this latitude) — MEASURED via eccodes |
| GEFS | 40.75, -74.00 | **4.18 km** | 0.25° compact "small" product (`pgrb2sp25`) — MEASURED (see §9) |
| NBM | 40.7758, -73.9709 | **0.47 km** | ~2.5 km (NDFD-based Lambert Conformal) |
| ECMWF (det + ens) | 40.75, -74.00 | **4.18 km** | 0.25° (public documentation; not independently resolution-checked here) |

**PROPOSED coordinates for bulk extraction:** `lat=40.7794, lon=-73.9691`
(KNYC's registry coordinates) for every source, so the "Central Park point"
is defined identically everywhere rather than by each test script's
slightly different requested coordinate.

---

## 5. Surrounding spatial-context recommendation (Part 6)

**MEASURED, and the single most important cost finding in this whole
document:** GRIB2 byte-range extraction (via `.idx` sidecar files) fetches
**one full compressed grid message per variable per forecast-hour/step**,
regardless of how many grid points are subsequently read from it. Measured
per-message sizes (§storage estimate doc) already reflect a *whole-CONUS or
whole-globe* message. This means:

- Extracting **additional grid points from an already-fetched message costs
  ~zero additional remote bytes** (the message is already downloaded and
  decoded via `eccodes` to find the nearest point; finding N nearby points
  instead of 1 is a cheap in-memory operation).
- Extracting additional grid points **does** cost additional processed-row
  storage and additional per-row database growth, roughly linearly in patch
  size.

Given this, patches are "almost free" remotely but not local-storage-free,
especially for GEFS's 31-member multiplier.

**Per-source recommendation:**

| Source | Recommendation | Patch definition | Reasoning |
|---|---|---|---|
| HRRR | **Central Park + small patch** | 3×3 nearest grid cells (~3 km spacing → ~6×6 km box) | Remote cost ~zero; HRRR's fine 3km grid can resolve real coastal/urban gradients (sea breeze, urban heat) cheaply |
| GFS | **Central Park + small patch** | 3×3 nearest grid cells (~0.25° spacing → ~50×50 km box) | Same near-zero remote cost; coarser grid, so the "patch" mainly captures synoptic-scale gradients, still useful |
| NBM | **Central Park + small patch** | 3×3 nearest grid cells (~2.5 km spacing) | Same reasoning as HRRR; NBM already blends models so patch differences are more muted but still cheap to keep |
| ECMWF (deterministic) | **Central Park point only, patch deferred** | n/a | ECMWF's own historical availability is the least certain source (§8); avoid multiplying an already-fragile source's row count until access is confirmed durable |
| GEFS | **Central Park point only for all 31 members; patch reserved for the control member (`gec00`) only, if any** | n/a (or 3×3 for `gec00` only, as an optional add-on) | A 3×3 patch × 31 members × several variables multiplies row count ~9× on top of an already-large ensemble; not worth it until member-level point data proves useful |
| ECMWF (ensemble) | **Central Park point only** | n/a | Same reasoning as GEFS, compounded by the source's own durability uncertainty |

The master dataset schema (§10) keeps an explicit `is_primary_central_park_grid`
boolean and `distance_from_central_park_km` field so target-point vs.
context-point rows are never ambiguous, per Part 5/6's requirement.

---

## 6. Variable selection (Parts 7-9)

**MEASURED constraint:** every one of the 5 numerical-model feasibility
tests only ever fetched a **spot-check set of 7-9 surface variables** (2m
temperature, 2m dewpoint, relative humidity, 10m U/V wind, surface
pressure, total cloud cover, accumulated precip, downward shortwave
radiation — plus source-specific extras: NBM's direct max/min-temperature
"period" file, ECMWF's `mx2t3` direct 3-hour-max-temperature field). **None
of the 5 sources were ever tested for CAPE/CIN or PBL-height availability.**
Any CORE/OPTIONAL/EXCLUDE classification for those two categories below is
therefore explicitly UNRESOLVED pending a small spot-check, not a
measured fact.

| Category | Field | Classification | Why |
|---|---|---|---|
| Temperature | 2m temperature | **CORE** | Direct driver of Tmax; MEASURED available on every source |
| Temperature | Direct Tmax/Tmax-like field | **CORE where available (NBM, ECMWF), OPTIONAL/UNRESOLVED elsewhere (HRRR, GFS, GEFS)** | NBM's direct TMAX and ECMWF's `mx2t3` (max 2m temp over prior 3h) are MEASURED present in our raw test files; HRRR/GFS/GEFS's native max-temp fields were never spot-checked |
| Moisture | 2m dew point | **CORE** | Governs latent heat partitioning, humidity-driven heat-index effects, sea-breeze triggers |
| Moisture | Relative humidity | **OPTIONAL** | Largely derivable from temp+dewpoint; keep as a direct field for convenience/QA, not a new physical driver |
| Wind | 10m U/V components | **CORE** | Directional wind carries mixing/advection/sea-breeze information; keeping U/V (not just speed) preserves direction |
| Wind | Wind speed if directly available | **OPTIONAL** | Derivable from U/V; keep only if a source gives it "for free" (no separate message cost) |
| Pressure | Surface pressure | **CORE** | Needed for frontal-passage/synoptic context, cheap (small message size, MEASURED ~0.3-0.5MB) |
| Pressure | Mean sea-level pressure | **OPTIONAL** | Useful for synoptic pattern context; redundant with surface pressure at this near-sea-level site |
| Cloud | Total cloud cover | **CORE** | Directly gates daytime shortwave heating — a strong, cheap Tmax driver |
| Cloud | Specific cloud layers | **EXCLUDE** | No clear incremental value over total cloud cover for a Tmax target; adds real cost (extra messages) |
| Precipitation | Accumulated precipitation | **CORE** | Cheapest message of the set (MEASURED ~0.3MB); rain strongly suppresses daytime heating |
| Precipitation | Precipitation rate | **EXCLUDE** | Accumulated precip already captures the Tmax-relevant signal; rate adds cost with little incremental value |
| Solar/Radiation | Downward shortwave radiation | **CORE** | Direct physical driver of daytime heating; MEASURED present on every source, though one of the larger messages (~2.6MB for HRRR) |
| Solar/Radiation | Other radiation fields | **EXCLUDE** | No clear added value for a Tmax-only target |
| Convection/Stability | CAPE | **OPTIONAL, UNRESOLVED AVAILABILITY** | Convective cloud formation can suppress afternoon heating on hot humid days; plausible relevance, but untested on any source including whether GEFS's compact `pgrb2sp25` product even carries it |
| Convection/Stability | CIN | **EXCLUDE** | Weaker, more indirect relevance than CAPE for a max-temperature (not severe-weather) target |
| Boundary layer | PBL height | **OPTIONAL, UNRESOLVED AVAILABILITY** | Mixing depth affects how surface heat gets distributed vertically; plausible but untested, and rarely available in compact ensemble products |
| Other | — | **EXCLUDE** | No other field met a clear, source-agnostic meteorological justification for a max-temperature target |

### Variable × source matrix

| Variable | HRRR | GFS | GEFS | NBM | ECMWF |
|---|---|---|---|---|---|
| 2m temperature | CORE | CORE | CORE (all members) | CORE | CORE |
| Direct Tmax-like field | OPTIONAL (untested) | OPTIONAL (untested) | OPTIONAL (untested) | **CORE** (measured, direct TMAX) | **CORE** (measured, `mx2t3`) |
| 2m dew point | CORE | CORE | CORE (all members) | CORE | CORE |
| Relative humidity | OPTIONAL | OPTIONAL | OPTIONAL | OPTIONAL | OPTIONAL |
| 10m U/V wind | CORE | CORE | CORE (all members) | CORE | CORE |
| Wind speed (direct) | EXCLUDE (derive from U/V) | EXCLUDE | EXCLUDE | OPTIONAL (NBM has a direct `WIND` field, measured) | EXCLUDE |
| Surface pressure | CORE | CORE | CORE (all members) | OPTIONAL | CORE |
| MSLP | EXCLUDE | OPTIONAL | EXCLUDE | EXCLUDE | OPTIONAL |
| Total cloud cover | CORE | CORE | CORE (all members) | CORE | OPTIONAL (untested on ECMWF) |
| Accumulated precip | CORE | CORE | CORE (all members) | CORE | CORE |
| Downward shortwave radiation | CORE | CORE | OPTIONAL (cost × 31 members) | OPTIONAL (untested) | OPTIONAL (untested) |
| CAPE | OPTIONAL/UNRESOLVED | OPTIONAL/UNRESOLVED | OPTIONAL/UNRESOLVED | EXCLUDE (post-processed product, unlikely to carry raw CAPE) | OPTIONAL/UNRESOLVED |
| PBL height | OPTIONAL/UNRESOLVED | OPTIONAL/UNRESOLVED | OPTIONAL/UNRESOLVED | EXCLUDE | OPTIONAL/UNRESOLVED |

**Major cross-source differences:** GEFS's shortwave radiation is downgraded
to OPTIONAL specifically because of the ×31-member multiplier — a variable
that's cheap for one deterministic run becomes one of the largest cost
drivers once multiplied by every ensemble member. NBM downgrades
pressure/MSLP to OPTIONAL because, as a blended/statistically post-processed
product, its own pressure field adds little beyond what the raw dynamical
models already provide (§7 below).

---

## 7. Cross-source duplication (Part 9)

Per instruction, duplication across HRRR/GFS/GEFS/NBM/ECMWF is **kept, not
removed** — model disagreement (already surfaced in `weather_state.py`'s
`cross_model_diagnostics`, MEASURED to show real spreads, e.g. ~8°F between
GFS and NBM at a single July-1 timestamp) is itself a predictor. The one
exception under review is:

**NBM** is a statistically post-processed/blended product built partly
*from* the raw dynamical models. Retaining its full variable set at the same
fidelity as the raw models (HRRR/GFS/ECMWF) adds real storage cost for
fields where NBM is unlikely to contribute independent information beyond
what it already blends in. Recommendation: keep NBM's **temperature,
dewpoint, direct TMAX, and cloud cover** at full priority (these are its
core value-add — a blended, presumably better-calibrated forecast), but
downgrade NBM's **pressure and wind-speed fields to OPTIONAL** (see matrix
above) since they add comparatively little beyond the raw models' own
pressure/wind fields for a Central Park Tmax target.

---

## 8. Per-source bulk specifications (Parts 10-14)

### 8.1 HRRR

- **Historical period:** 2023-01-01 → present (PROPOSED; see §3 caveats).
- **Runs/day:** all 24 hourly runs — **MEASURED requirement**: our own
  `weather_state.py` completeness-fix work directly demonstrated that
  preserving only synoptic-hour runs would hide real, materially different
  intraday forecast updates (the 02Z run genuinely completed with a 91.44°F
  daily max, different from both the stale 87.5°F and the transient partial
  77.2°F artifact). One run/day is explicitly insufficient.
- **Forecast-hour range:** only forecast hours whose `valid_time` falls
  within the local Central Park target day — reuse
  `expected_target_day_valid_times("hrrr", run_time, ...)` from
  `data/weather_state.py` directly (do not re-derive this logic).
  Synoptic-hour runs (00/06/12/18Z) reach the full remaining target day
  (max lead 48h); intermediate-hour runs (max lead 18h) cover a
  correspondingly smaller tail of the target day — this is expected and
  already handled by the completeness architecture, not a defect to fix
  here.
- **Target-day filtering:** at extraction time, only fetch/keep forecast
  hours landing inside `[local_day_start_utc, local_day_end_utc)` for the
  target date the run can reach — do not fetch or store hours forecasting
  days beyond the immediate target day.
- **Variables:** CORE set from §6 (temperature, dewpoint, U/V wind,
  pressure, cloud cover, precip, shortwave radiation) = 8 messages/hour;
  CAPE/PBL deferred pending a spot-check.
- **Central Park grid:** nearest point (1.02 km, MEASURED) + 3×3 patch
  (§5).
- **Availability metadata:** `available_time` from S3 `Last-Modified`
  (`S3_LAST_MODIFIED_PROXY`, MEASURED to be progressive across
  forecast-hours within a single run — reuse as-is).
- **Remote extraction strategy:** `.idx`-based byte-range GRIB2 fetch, same
  pattern as `data/hrrr.py`'s `fetch_idx`/`find_message`/`fetch_byte_range`
  — no code changes needed, just parameterize over the full date range.
- **Processed storage format:** partitioned Parquet (§10).
- See [`bulk_storage_estimate.md`](bulk_storage_estimate.md) for expected
  rows/day, request counts, and byte estimates.

### 8.2 GFS

- **Historical period:** 2023-01-01 → present.
- **Runs/day:** all 4 synoptic runs (00/06/12/18Z) — GFS does not publish
  additional intermediate-hour runs the way HRRR does, so 4/day is already
  the source's real cadence, not an arbitrary reduction.
- **Forecast-hour range:** GFS is hourly through +120h — far beyond what a
  next-day Central Park target needs. **Useful horizon: only forecast
  hours whose valid_time falls within the target day** (typically ≤48h
  lead for same-day predictions made the day before). Do not fetch or
  store GFS's extended-range (+120h to +384h) data — it cannot affect a
  single target day's Tmax reconstruction and would be pure waste.
- **Variables/grid/availability/storage:** same CORE set and mechanics as
  HRRR (§6, §5); shortwave radiation kept CORE here since GFS is
  deterministic (no ensemble multiplier).

### 8.3 GEFS

- **Historical period:** 2023-01-01 → present.
- **Runs/day:** all 4 (00/06/12/18Z) — matches GEFS's real cadence.
- **Members:** **all 31** (`gec00` + `gep01`-`gep30`) for every CORE
  variable — per explicit instruction, member-level data must never be
  reduced to mean/median/std/quantiles at extraction time; those are
  derived later by `weather_state.py`'s `get_ensemble_state`.
- **Forecast-hour range:** 3-hourly, restricted to target-day valid times
  only (same logic as HRRR/GFS).
- **Variables/member — REVISED after cost audit, PENDING APPROVAL:** GEFS's
  compact `pgrb2sp25` product contains all 9 spot-checked surface fields,
  confirmed by real cached message files. Three scenarios were quantified
  (see `bulk_storage_estimate.md` §7): **GEFS FULL** (TMP/DPT/UGRD/VGRD/
  PRES/APCP for all 31 members, ~3.2 TB over the original window), **GEFS
  TEMPERATURE-FOCUSED** (TMP only, all 31 members, ~0.40 TB, -87.6%), and
  **GEFS INTERMEDIATE** (TMP+DPT for all 31 members; UGRD/VGRD/PRES/APCP for
  the control member `gec00` only, ~0.87 TB, -72.8%). **INTERMEDIATE is
  recommended** — it keeps full ensemble uncertainty for the two fields most
  directly tied to Tmax (temperature and dewpoint/moist-heating effects)
  while dropping the 31-member multiplier on wind/pressure/precip, which are
  already available at full fidelity from HRRR/GFS/ECMWF. **This changes the
  original all-6-variables-all-members spec and requires explicit
  approval before extraction.** CAPE/PBL availability in `pgrb2sp25` remains
  UNRESOLVED, not spot-checked this round.
- **Central Park grid:** nearest point only for all 31 members (§5); no
  patch for GEFS in this plan.
- **Availability metadata:** **MEASURED, already confirmed in this
  project's own testing**: `available_time` is genuinely member/file
  -specific (3 distinct Last-Modified timestamps observed across 31
  sampled members for the same run+forecast-hour). Must be preserved
  per-member, never collapsed to a single run-level timestamp.

### 8.4 NBM

- **Historical period:** 2023-01-01 → present.
- **Runs/day — RESOLVED by direct investigation, PENDING APPROVAL:** a
  real 7-consecutive-hourly-run comparison (2025-07-01, 06Z-12Z, small
  byte-range fetches only) found temperature for a fixed future hour
  changes only ~0.0-0.05°F between most consecutive runs (one ±0.7°F
  outlier), while cloud cover moved 15 percentage points over the same 6
  hours (including a 7-point swing within just 3 hours). **Recommendation:
  8 runs/day (every 3 hours: 00/03/06/09/12/15/18/21Z)** — fine enough to
  not straddle the observed cloud-cover swings, a 3× reduction from hourly
  (66.7% NBM-specific savings, ~1.23 TB vs. ~3.7 TB over the original
  window). See `bulk_storage_estimate.md` §3 for the full measurement and
  cadence-cost table (24/12/8/4 runs/day all quantified). **Based on one
  day's sample — requires explicit approval, and re-checking against 2-3
  more active-weather days is recommended before bulk extraction.**
- **Forecast-hour range:** hourly to +36h, 3-hourly to +192h (MEASURED
  schedule, confirmed by a mixed 1h/3h step within a single run in the test
  data) — restrict to target-day valid times only.
- **Variables:** temperature, dewpoint, cloud cover CORE; **both the hourly
  TMP trajectory AND the direct TMAX "period" field, per explicit
  instruction** — the discrepancy between them is a known open question
  and must not be silently resolved by picking one. MEASURED storage cost
  of adding the direct TMAX field is small (~1.5MB per run, comparable to
  a single extra hourly-TMP message).
- **Uncertainty fields:** NBM publishes an ensemble-derived standard
  deviation field for temperature (already extracted correctly in this
  project's feasibility test, after fixing the `fetch_idx` forecast-desc
  parsing bug) — **CORE**, since it is NBM's unique value-add (a
  spread/uncertainty estimate on top of a blended forecast) and is cheap.
- **Central Park grid:** nearest point (0.47 km, MEASURED — the closest of
  any source) + 3×3 patch.

### 8.5 ECMWF (deterministic + ensemble)

Per explicit instruction: **do not let ECMWF's uncertainty block the rest
of the dataset.** Classify separately:

**ECMWF deterministic — CORE, with a durability caveat, CORRECTED start date.**
- Historical period: **2024-02-28 → present**, NOT 2023-01-01 and not the
  previously assumed 2023-01-18. A dedicated path-structure investigation
  (small listing requests with a delimiter to reveal subfolders) found the
  Open Data product moved through three schemes: `0p4-beta` (through
  ~2024-01) → bare `0p25` (Feb 1-27, 2024) → `ifs/0p25`+`aifs/` (2024-02-28
  onward, confirmed stable through 2026-07-15). `data/ecmwf.py`'s
  `grib_key()` only constructs the last scheme's paths — real objects exist
  before 2024-02-28 but our code cannot read them without new path logic
  (not written here). This shortens ECMWF's usable window to ~2.6 years as
  of today, not the ~3.75 years the other sources get.
- Runs/day: **at least 4 (00/06/12/18Z), not 2** — a broader listing found
  all four hourly subfolders present even back in mid-2023, meaning the
  previous test's `RUN_HOURS = {0, 12}` finding was specific to the one
  date tested, not a real limitation. `data/ecmwf.py`'s `RUN_HOURS`
  constant should be corrected in a future code-touching phase (not done
  in this design-only task).
- Forecast hours: 3-hourly to 144h, 6-hourly beyond — restricted to
  target-day valid times.
- Variables: 2m temperature, `mx2t3` (direct 3-hour max temperature —
  MEASURED present, CORE), 2m dewpoint, 10u/10v, surface pressure,
  precipitation. Cloud cover and shortwave were never spot-checked on
  ECMWF — OPTIONAL/UNRESOLVED.
- Central Park grid: nearest point only, no patch (§5).
- Access/archive reliability: **MEASURED discrepancy, unresolved** —
  documented retention says ~2-3 days; this project's own bucket listing
  found real objects going back well over two years (with the caveat above
  that our code can only read the current path scheme, 2024-02-28+). This
  project does **not** treat S3 `Last-Modified` as true dissemination time
  (the ~514-minute/~8.6-hour average lag measured in the feasibility test
  is far larger than every
  other source's lag and is tagged `availability_confidence="LOW"` in
  `weather_state.py` already — carry that tag into the master schema
  unchanged).

**ECMWF ensemble — OPTIONAL/DEFERRED for bulk extraction.**
- This project's own ensemble feasibility sample was **deliberately small**
  (3 of ~8 target-day steps, 1 run) and `weather_state.py`'s own
  completeness logic correctly marks it `PARTIAL`/`INSUFFICIENT` — it has
  never been shown usable end-to-end.
- Recommendation: **defer** bulk ECMWF-ensemble extraction until (a) the
  deterministic feed's durability is confirmed over a longer window and
  (b) a dedicated feasibility pass confirms full member (51) and full
  target-day-step coverage is actually retrievable historically at
  reasonable cost. Do not bulk-extract an ensemble this project has never
  verified end-to-end.

---

## 9. Surface observation bulk specification (Part 15-17)

- **Stations:** KNYC (primary), KLGA, KJFK, KEWR — station identity always
  preserved, never averaged (per explicit instruction and consistent with
  the whole project's design).
- **Fields to retain (from the actual ISD canonical schema, MEASURED — see
  `data/observations.py`'s `parse_isd_row`/`_scaled` and the 26-column
  canonical CSV schema already produced):** `observation_time`,
  `report_type_raw`, `quality_control_version`, both raw and derived
  temperature/dewpoint (C and F) with QC flags, relative humidity, wind
  direction/speed/gust, station pressure, sea-level pressure, precipitation,
  cloud cover raw code, visibility, weather codes raw, quality flags raw,
  `is_real_observation` (excludes SOD/SOM summary placeholder rows).
- **Preserve irregular observations and SPECI**, i.e. do not filter to only
  routine hourly METAR reports — `report_type_raw` already distinguishes
  these and must be kept, not discarded.
- **No resampling, no interpolation, no station-collapsing** at extraction
  time — this matches the project's entire point-in-time design and is not
  a new decision here.
- **Availability-time issue (Part 17), MEASURED/confirmed unresolved:** the
  ISD canonical schema **has no `available_time` column at all** (unlike
  every numerical source). `observation_time` is the only timestamp. Master
  schema must preserve `observation_time` and an explicit
  `availability_time_type = "OBSERVATION_TIME_PROXY"` field, and must never
  pretend `observation_time` is a true ingestion/publication timestamp.
  This stays UNRESOLVED, not silently fixed.

---

## 10. Central Park (KNYC) historical station-id continuity (Part 16)

**MEASURED, directly from NOAA's `isd-history.csv` master registry**
(recorded in `data/observations.py`'s comments and confirmed in
`scripts/test_observations_nyc_feasibility.py`'s station-continuity
finding):

Central Park has **four** historical station-id records in ISD, with an
unexplained gap:

| Station ID | Coverage | Notes |
|---|---|---|
| 725033-94728 | 1943-1997 | Predecessor record |
| 999999-94728 | 1965-1997 | Overlapping alternate/duplicate record |
| *(gap)* | **1997-2005** | **No Central Park record found in this registry for this span** |
| 725060-94728 | 2010-2012 | Short-lived intermediate record, itself inside a gap relative to the current record |
| **725053-94728** | **2005-2025 (current)** | The ID used throughout this entire project's testing |

By contrast, LaGuardia/JFK/Newark each show one clean, continuous
1973-2025 record — Central Park's fragmentation is a real anomaly specific
to this station, not an artifact of how we're querying ISD.

**Recommendation:**
- The master dataset's canonical `station_label = "KNYC"` field maps
  **only** to `725053-94728` (2005-present) by default — this is the safe,
  unambiguous mapping.
- **Do not silently merge** `725033-94728`/`999999-94728`/`725060-94728`
  into the `KNYC` label. If a future need arises for pre-2005 Central Park
  history, that requires a **dedicated, separate reconciliation task**
  (verifying whether those older records share the same physical location,
  instrumentation, and elevation) — explicitly out of scope here.
- Given this, and independent of the numerical-source date constraints in
  §3, **KNYC observational history for this project is bounded at
  2005-01-01** unless that reconciliation work is done.

---

## 11. NWS AFD bulk specification (Part 18)

- Office: OKX. Product: AFDOKX.
- **Preserve full original raw text, unmodified** — no summarization,
  truncation, embedding, numeric conversion, or LLM involvement, matching
  this project's entire AFD-handling philosophy to date.
- Preserve **every issuance**, including closely spaced updates (this
  project's July-1 test alone had issuances as close as ~30-90 minutes
  apart during active weather — see `afd_documents_metadata.csv`).
- Preserve parsed sections (`afd_sections.parquet`'s existing schema:
  `product_id, issuance_time, section_name, section_text`) alongside the
  full raw text — both, not one or the other. Section names are
  variable-suffixed (e.g. `"NEAR TERM /THROUGH TONIGHT/"`) and must be
  matched by prefix downstream, never assumed to be fixed strings.
- Availability assumption: `issuance_time` only (`ISSUANCE_TIME_PROXY`) —
  no independent availability metadata exists for AFD, matching the
  already-established `available_time` column being 100% null.
- Storage format: JSONL or Parquet for structured metadata + sections;
  raw text stored as a plain string field (small relative to numerical
  data — see storage estimate doc).

---

## 12. Point-in-time metadata requirements (Parts 19-20)

Every numerical-forecast row in the master dataset must carry, at minimum:

```
source, run_time, valid_time, available_time, availability_time_type,
forecast_hour, member, grid_id, grid_latitude, grid_longitude,
distance_from_central_park_km, is_primary_central_park_grid,
variable, value, units, source_object, retrieval_metadata (optional)
```

`run_time`, `valid_time`, and `available_time` are **never** collapsed into
one timestamp — this is already how `weather_state.py` is built and is
carried forward unchanged into the master dataset design.

`availability_time_type` takes one of exactly three values across this
whole project, and no others should be invented:
`S3_LAST_MODIFIED_PROXY` (all six numerical sources),
`OBSERVATION_TIME_PROXY` (surface observations),
`ISSUANCE_TIME_PROXY` (AFD). Where availability is genuinely unknown, that
must be preserved as unknown, not defaulted to `run_time` for convenience —
this is the project's most fundamental point-in-time-integrity rule
(Part 33) and is repeated here because the master schema is where it could
most easily be violated by a careless bulk-extraction implementation.

---

## 13. Raw vs. processed storage policy (Part 21)

| Source | Policy | Reason |
|---|---|---|
| HRRR/GFS/GEFS/NBM/ECMWF | **RANGE-READ / STREAM AND DISCARD** — extract the needed grid point(s) and variables via `.idx` byte-range fetch, decode with `eccodes`, discard the raw GRIB2 message | Full GRIB2 files are large (whole-CONUS/whole-globe grids) and provide no further value once the needed points are extracted; keeping them would multiply storage by orders of magnitude for no benefit |
| Observations (ISD) | **KEEP RAW** (the yearly per-station CSV.GZ files) | Already small (measured: the entire July-1 test's raw observation data is ~208KB); cheap to keep in full for reproducibility |
| AFD | **KEEP RAW** (full text) | Small relative to numerical data; full text IS the canonical product, not an intermediate artifact |

For the GRIB2 sources, **provenance is preserved instead of raw files**:
`source_object` (the exact GRIB2 key/URL and byte range read) plus
`retrieval_metadata` (idx line, message length) are enough to
reconstruct exactly where each value came from without keeping the
multi-megabyte source file. This satisfies "preserve enough provenance to
reproduce where each value came from" without paying for permanent raw
storage.

---

## 14. Canonical storage design (Part 22)

Directory structure (PROPOSED, following the repository's existing
`data/processed/weather/<source>/` convention, extended for bulk history):

```
data/processed/weather/
  hrrr/history/year=2023/month=01/run_date=2023-01-01/part-*.parquet
  gfs/history/year=2023/month=01/run_date=2023-01-01/part-*.parquet
  gefs/history/year=2023/month=01/run_date=2023-01-01/part-*.parquet
  nbm/history/year=2023/month=01/run_date=2023-01-01/part-*.parquet
  ecmwf/history/year=2023/month=01/run_date=2023-01-01/part-*.parquet
  observations/history/year=2023/station_id=725053-94728/part-*.parquet
  afd/history/year=2023/month=01/afd_documents.parquet
  afd/history/year=2023/month=01/afd_sections.parquet
```

Partitioning by `source / year / month / run_date` (numerical sources) and
`source / year / station_id` (observations) avoids millions of tiny files
while keeping the common query — *"give me everything known by 12:00 ET on
July 1, 2025 for Central Park"* — efficient: it resolves to a handful of
`run_date` partitions per source (the target day plus 1-2 days of lead-in),
each already small (single-digit MB per source per day at the row counts
in §storage estimate doc), rather than scanning the entire historical
dataset.

---

## 15. Data types / compression (Part 23)

- **Temperature/other float fields: `float32`**, not `float64` — GRIB2
  itself typically stores at similar or lower precision; no scientifically
  useful precision is lost, and it halves numeric-column storage.
- **Categorical/dictionary encoding** for `source`, `model`, `product`,
  `variable`, `unit`, `station_id`, `ensemble_member` — these are
  low-cardinality strings repeated across every row. Parquet already does
  this somewhat automatically (**MEASURED**: a same-day HRRR canonical file
  shrank from 60,305 bytes CSV to 19,941 bytes Parquet, a ~3.0x reduction,
  even without explicit categorical casting); explicit `pandas.Categorical`
  dtypes before writing should improve this further.
- **`ensemble_member`** as a small integer code (0=control, 1-30=perturbed)
  instead of the string `"gep01"` etc. — cheap, unambiguous, and avoids
  repeating a 5-character string 31× per forecast-hour × millions of rows.
- **Timestamps** as proper timezone-aware `timestamp[us]` Parquet columns,
  not strings (the current test CSVs store them as strings — acceptable for
  a one-day test, not for bulk history).
- **Grid IDs**: a small integer/categorical code per (source, patch-cell)
  rather than repeating full lat/lon floats on every row — still keep the
  actual lat/lon/distance columns for provenance, but consider a compact
  `grid_id` for join efficiency.

---

## 16. Resume/failure architecture (Part 24)

A manifest (PROPOSED, one row per extraction unit) with at minimum:

```
source, date, run_time, forecast_hour, member (nullable),
source_object, attempt_count, status (PENDING/IN_PROGRESS/DONE/FAILED),
bytes_read, rows_extracted, error, retry_status, validation_status,
last_attempt_at
```

This directly mirrors the resumable-checkpoint pattern already built and
proven in this project's Kalshi bulk downloaders (`save_checkpoint_json`
with atomic-write + Windows-`PermissionError` retry). Reuse that pattern
rather than inventing a new one: each extraction unit
(source, date, run, forecast_hour, member) is idempotent — re-running never
re-fetches a unit already marked `DONE` with a passing `validation_status`.

---

## 17. Validation architecture (Part 25)

Automated checks per source, run after each extraction batch, all
**reporting**, never silently repairing:

- Expected runs/day present (per §8's per-source run cadence).
- Expected member count present (GEFS: exactly 31; flag any run with
  fewer).
- Expected forecast hours present (cross-check against
  `expected_target_day_valid_times()` — reuse directly).
- Expected variables present per row-group.
- No duplicate `(source, run_time, valid_time, member, variable, grid_id)`
  keys.
- No missing runs/members silently skipped (explicit gap report, not a
  fill).
- Impossible temperature values (e.g. outside roughly -40°F to 130°F for
  this location) flagged, not discarded.
- Timestamp ordering: `run_time <= available_time` where both exist (never
  the reverse) — this is a direct restatement of Part 33's data-leakage
  rule as an automated check.
- Unit consistency (Kelvin in, `_f` derived consistently — already the
  convention throughout this project).
- Central Park grid-point consistency (the "primary" grid point/distance
  must not silently drift between extraction runs for the same source).
- Station-identity consistency (no accidental cross-station-id mixing,
  especially given §10's Central Park fragmentation risk).

---

## 18. Download priority (Part 26)

**PROPOSED**, based on this project's actual feasibility results (not the
generic order suggested in the prompt):

1. **Surface observations** — cleanest, most durable archive (KLGA/KJFK/KEWR
   MEASURED continuous since 1973; KNYC since 2005), smallest storage,
   fewest unresolved issues besides the availability-time gap (which is
   simply preserved as unknown, not blocking).
2. **AFD** — equally durable-looking archive, tiny storage, zero numerical
   complexity.
3. **HRRR** — MEASURED, fully working end-to-end pipeline including the
   completeness architecture; the highest-resolution, most information-dense
   numerical source for a single city.
4. **GFS** — same maturity level as HRRR, smaller messages, simpler
   (4 runs/day, no ensemble).
5. **NBM** — working pipeline, but carries the known TMAX-vs-hourly-TMP
   discrepancy (Part 13) and a real hourly-vs-synoptic run-cadence decision
   still pending approval (§8.4) — sequence after the more settled sources.
6. **GEFS** — working pipeline, but the largest source by storage (31
   members); worth doing after the cheaper sources are proven at scale so
   any manifest/resume issues are caught on smaller data first.
7. **ECMWF deterministic** — last among the "go" sources because of its own
   MEASURED access-durability uncertainty (§8.5); extract only after the
   rest of the plan is validated, so a failure here doesn't block anything
   else.
8. **ECMWF ensemble — deferred**, not sequenced at all in this plan (§8.5).

**Reassessed after the audit (Task 13): no change to this order.** Nothing
found in the archive-depth or cost audit gives a reason to reorder — ECMWF
deterministic's shortened, corrected window (2024-02-28+) if anything
reinforces keeping it last (least historical depth of any included
source), and NBM/GEFS's revised cadence/variable recommendations don't
change their relative position in the sequence.

---

## 19. LEAN / BALANCED / RICH scenarios (Part 27) — REVISED, see storage doc for full numbers

See [`bulk_storage_estimate.md`](bulk_storage_estimate.md) §9 for the full
numeric comparison (both the original ~912-day window and the real
~1,364-day window as of today). Summary:

| | LEAN | **BALANCED (optimized, recommended)** | RICH |
|---|---|---|---|
| Sources | Obs + AFD + HRRR + GFS + NBM (4/day) | + GEFS (**INTERMEDIATE**, pending approval) + ECMWF deterministic (corrected window) | + ECMWF ensemble (still deferred) + wider patches |
| Spatial | Central Park point only, every source | Central Park point + 3×3 patch for HRRR/GFS/NBM (§5); point-only for GEFS/ECMWF | 5×5 patch for HRRR/GFS/NBM; 3×3 for GEFS control member only |
| Ensemble | none | GEFS **TMP+DPT for all 31 members**, wind/pressure/precip for control member only | GEFS FULL (all 6 vars × 31 members) + ECMWF ensemble (deferred) |
| NBM cadence | 4/day | **8/day (recommended, pending approval)** | 8/day |
| Variables | Temperature, dewpoint, U/V wind (CORE minimum) | Full CORE set from §6 | CORE + OPTIONAL (RH, MSLP, CAPE/PBL pending spot-check) |
| Period | 2023-present (ECMWF: 2024-02-28+) | 2023-present (ECMWF: 2024-02-28+) | same |
| Remote reads (~912-day window) | ~1.6 TB | **~8.1 TB** (was ~13.0 TB) | ~15+ TB |

---

## 20. Recommended plan (Part 28) — REVISED

**BALANCED (optimized)**, as defined above and detailed per-source in §8,
for the **2023-01-01 → present** common window for HRRR/GFS/GEFS/NBM/
observations/AFD, and **2024-02-28 → present** for ECMWF deterministic
(corrected — see §8.5), with three items still requiring explicit
approval, none applied automatically:

1. **NBM cadence: 8 runs/day (every 3 hours)**, not the previously
   unresolved hourly-vs-4/day choice — based on a direct, quantified
   investigation (§8.4, `bulk_storage_estimate.md` §3), reducing NBM's
   remote-read cost by 66.7% versus hourly while still resolving the
   cloud-cover swings actually observed.
2. **GEFS INTERMEDIATE**: TMP+DPT for all 31 members, UGRD/VGRD/PRES/APCP
   for the control member only — reduces GEFS's remote-read cost by 72.8%
   versus the original all-6-variables-all-members spec while never
   reducing temperature or dewpoint ensemble information (§8.3,
   `bulk_storage_estimate.md` §7).
3. **ECMWF deterministic start date: 2024-02-28**, not 2023-01-01 — a
   correction forced by a real, newly-discovered product-path schema
   change, not an optional trade-off (§8.5).

With these three items approved, the optimized BALANCED plan totals
**≈8.1 TB remote / ≈1.6 GB local** at the original ~912-day window (a
**~37% reduction** from the previous ~13.0 TB / ~2.6 GB estimate), or
**≈12.1 TB remote / ≈2.4 GB local** at the real ~1,364-day window if
extraction begins today (2023-01-01 → 2026-09-26). ECMWF ensemble remains
deferred; CAPE/PBL availability across all 5 sources remains an open
spot-check, not blocking the rest.
