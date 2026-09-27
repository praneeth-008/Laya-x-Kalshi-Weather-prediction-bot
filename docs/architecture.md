# System Architecture

Status: describes the architecture AS DESIGNED IN CODE. Not every stage has been executed end-to-end yet -- see "Implementation status" below for what has actually run.

## End-to-end pipeline

```
weather archives (HRRR, GFS, NBM, GEFS, ECMWF, ISD observations, NWS AFD)
    |
    v
point-in-time extraction (byte-range GRIB reads, checkpointed, resumable)
    |
    v
source-specific validation (structural + scientific/semantic -- see validation.md)
    |
    v
historical weather state X_t (data/weather_state.py)
    |
    v
probabilistic Tmax model ("Laya")
    |
    v
P(Tmax = x | X_t)
    |
    v
Kalshi bucket mapping
    |
    v
executable Kalshi prices
    |
    v
trading policy / risk / execution
```

## Three layers

**A. Weather prediction layer** -- everything from the archives down through `P(Tmax = x | X_t)`. This is the layer this project has worked on so far: `data/hrrr.py`, `data/gfs.py`, `data/observations.py`, `data/afd.py`, `data/pilot_extraction.py`, `data/weather_state.py`, and the eventual Laya model.

**B. Kalshi contract/pricing layer** -- mapping a temperature probability distribution onto Kalshi's specific bucket structure and settlement mechanism for the `KXHIGHNY` series (`data/kalshi.py` handles historical market/event discovery only, not pricing logic yet).

**C. Trading/execution layer** -- policy, risk, and order execution against Kalshi. Not started.

## Critical rule: the weather model is trained independently of Kalshi prices

The weather model (layer A) must be trained and validated using only weather data and observed outcomes -- never using Kalshi prices as a feature or a training signal. Kalshi prices are introduced later, in layer B, purely for economic/trading evaluation (comparing the model's independently-derived probabilities against what the market already prices in). This ordering is intentional: the research question this project is investigating is whether the model finds information *not already reflected* in Kalshi prices (see `README.md`), which is only answerable if the model never saw those prices during development.

## Implementation status by layer

| Component | Status |
|---|---|
| HRRR extraction | 40-day representative pilot complete, FINAL PASS (see `data_sources.md`) |
| GFS extraction | 40-day representative pilot complete, FINAL PASS |
| NBM / GEFS / ECMWF extraction | Not started (feasibility-tested only) |
| ISD observations | Collected for the pilot window (2025-01-01 to 2025-08-24), 4 NYC-area stations |
| NWS AFD | Collected for the pilot window, OKX/AFDOKX |
| `data/weather_state.py` (X_t construction, no-lookahead enforcement, completeness logic) | Implemented and exercised against a one-day feasibility dataset; **not yet run against the actual 40-day HRRR/GFS pilot output** -- flagging this rather than assuming it has been |
| Laya probabilistic model | Not started |
| Kalshi bucket mapping / pricing | Not started (only historical market/event discovery exists in `data/kalshi.py`) |
| Trading/execution | Not started |

## Central Park settlement note

The forecast target (Central Park / KNYC observed daily max temperature) is not automatically the same thing as the Kalshi settlement mechanism for `KXHIGHNY`. See `city_configuration.md` for why these are kept separately auditable.
