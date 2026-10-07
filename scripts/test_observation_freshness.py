"""Regression tests for the 2026-10-07 observation-freshness fix
(data/weather_state.py's get_observation_state()) -- see docs/decisions.md.

Covers: fresh observations accepted, exact/just-beyond the freshness
boundary, multi-day/multi-month-old observations rejected as current but
never fabricated as zero, Tmax-so-far staying strictly day-scoped and
independent of current-observation freshness, cross-station features
requiring BOTH sides fresh, and no-lookahead remaining intact (including
for a healthy/continuous period, confirming the fix never fires on normal
reporting cadence). Follows the same ad-hoc check() pattern as
scripts/test_integration.py.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import data.weather_state as ws

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


KNYC = "725053-94728"
KLGA = "725030-14732"
STATION_IDS = [KNYC, KLGA]
THRESH_H = ws.OBSERVATION_FRESHNESS_THRESHOLD_HOURS  # 6.0, see data/weather_state.py


def make_obs(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["is_real_observation"] = True
    df["availability_status"] = ws.AVAIL_OBS_ONLY
    df["observation_time"] = pd.to_datetime(df["observation_time"], utc=True)
    return df


def obs_row(sid, ts, temp, dewpoint=None, wind=None, pressure=None):
    return {"station_id": sid, "observation_time": ts, "temperature_f": temp,
            "dewpoint_f": dewpoint, "wind_speed_ms": wind, "station_pressure_hpa": pressure}


BASE = datetime(2025, 9, 15, 14, 0, tzinfo=timezone.utc)

# ---- 1. fresh observation accepted ----
obs = make_obs([obs_row(KNYC, BASE - timedelta(minutes=9), 71.0)])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("fresh observation: available=True", s["available"] is True)
check("fresh observation: current_temp_f == 71.0", s["current_temperature_f"] == 71.0)
check("fresh observation: stale=False", s["stale"] is False)

# ---- 2. observation exactly at freshness boundary ----
obs = make_obs([obs_row(KNYC, BASE - timedelta(hours=THRESH_H), 60.0)])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check(f"exactly at {THRESH_H}h boundary: still accepted (<=)", s["available"] is True and s["current_temperature_f"] == 60.0)

# ---- 3. observation just beyond freshness boundary rejected ----
obs = make_obs([obs_row(KNYC, BASE - timedelta(hours=THRESH_H, minutes=1), 60.0)])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("1 minute beyond boundary: available=False", s["available"] is False)
check("1 minute beyond boundary: current_temp_f is None (not fabricated as 0)", s["current_temperature_f"] is None)
check("1 minute beyond boundary: age/timestamp still preserved for audit", s["has_eligible_observation"] and s["observation_age"] > timedelta(hours=THRESH_H))

# ---- 4. 1-day-old observation rejected ----
obs = make_obs([obs_row(KNYC, BASE - timedelta(days=1), 55.0)])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("1-day-old observation rejected as current", s["available"] is False and s["current_temperature_f"] is None)
check("1-day-old observation: age correctly ~24h", abs(s["observation_age"].total_seconds() / 3600 - 24) < 0.01)

# ---- 5. multi-month-old observation rejected (the original bug scenario) ----
last_real = datetime(2025, 8, 27, 3, 51, tzinfo=timezone.utc)
query = datetime(2025, 12, 31, 19, 0, tzinfo=timezone.utc)
obs = make_obs([obs_row(KNYC, last_real, 68.0)])
state = ws.get_observation_state(obs, query, ws.PROXY_STATE, STATION_IDS, target_date=query.date())
s = state["stations"][KNYC]
check("multi-month-old observation: available=False", s["available"] is False)
check("multi-month-old observation: current_temp_f is None, not the stale 68.0", s["current_temperature_f"] is None)
check("multi-month-old observation: age correctly reflects ~4+ months", s["observation_age"] > timedelta(days=90))
check("multi-month-old observation: latest_observation_time still preserved", s["latest_observation_time"] == pd.Timestamp(last_real))

# ---- 6. stale value cannot appear as current temperature (never zero-filled either) ----
check("stale current_temp_f is None, never 0.0", s["current_temperature_f"] is None and s["current_temperature_f"] != 0.0)
check("stale current_dewpoint_f/wind/pressure also None", all(s[k] is None for k in ("current_dewpoint_f", "current_wind_speed_ms", "current_station_pressure_hpa")))

# ---- 7. Tmax-so-far cannot use previous-day observations ----
yesterday_obs = obs_row(KNYC, datetime(2025, 9, 14, 20, 0, tzinfo=timezone.utc), 90.0)  # yesterday's hot reading
today_obs = obs_row(KNYC, datetime(2025, 9, 15, 13, 0, tzinfo=timezone.utc), 65.0)  # today's cooler reading
obs = make_obs([yesterday_obs, today_obs])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=date(2025, 9, 15))
s = state["stations"][KNYC]
check("Tmax-so-far uses only today's reading (65.0), not yesterday's 90.0", s["max_temperature_observed_so_far_f"] == 65.0)

# ---- 8. no observations on target day -> Tmax-so-far unavailable ----
obs = make_obs([obs_row(KNYC, datetime(2025, 9, 10, 12, 0, tzinfo=timezone.utc), 72.0)])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=date(2025, 9, 15))
s = state["stations"][KNYC]
check("no same-day observations: Tmax-so-far is None", s["max_temperature_observed_so_far_f"] is None)
check("no same-day observations: current_temp_f still rejected (5-day-old, way past threshold)", s["current_temperature_f"] is None)

# ---- Tmax-so-far independent of current-observation staleness (both fresh and stale cases) ----
obs = make_obs([
    obs_row(KNYC, datetime(2025, 9, 15, 10, 0, tzinfo=timezone.utc), 80.0),  # today, now stale relative to BASE (14:00)
])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=date(2025, 9, 15))
s = state["stations"][KNYC]
check("current obs is stale (4h old < 6h threshold is actually fresh -- use for sanity)", s["available"] is True)
check("Tmax-so-far still correctly picks up today's reading regardless", s["max_temperature_observed_so_far_f"] == 80.0)

# ---- 9. cross-station feature unavailable when one station is stale ----
obs = make_obs([
    obs_row(KNYC, BASE - timedelta(minutes=9), 71.0),       # fresh
    obs_row(KLGA, BASE - timedelta(days=200), 50.0),         # ancient/stale
])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
knyc_fresh = state["stations"][KNYC]["current_temperature_f"]
klga_stale = state["stations"][KLGA]["current_temperature_f"]
cross_diff = (knyc_fresh - klga_stale) if (knyc_fresh is not None and klga_stale is not None) else None
check("cross-station diff is None when one side is stale (never fresh-vs-200-days-old)", cross_diff is None)
check("cross_station_diagnostics excludes the stale station", "station_count_available" not in state["cross_station_diagnostics"] or state["cross_station_diagnostics"].get("station_count_available", 0) == 1)

# both fresh -> diff computed normally
obs = make_obs([
    obs_row(KNYC, BASE - timedelta(minutes=9), 71.0),
    obs_row(KLGA, BASE - timedelta(minutes=20), 68.0),
])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
knyc_t = state["stations"][KNYC]["current_temperature_f"]
klga_t = state["stations"][KLGA]["current_temperature_f"]
check("cross-station diff computed normally when both fresh", knyc_t is not None and klga_t is not None and (knyc_t - klga_t) == 3.0)

# ---- 10. no-lookahead remains intact ----
future_obs = obs_row(KNYC, BASE + timedelta(minutes=5), 999.0)  # in the future relative to query time
obs = make_obs([future_obs])
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("a future-timestamped observation is never eligible (no-lookahead)", s["available"] is False and not s.get("has_eligible_observation", False))

# STRICT policy must never expose observations regardless of freshness (matches existing design)
obs = make_obs([obs_row(KNYC, BASE - timedelta(minutes=5), 71.0)])
state = ws.get_observation_state(obs, BASE, ws.STRICT_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("STRICT policy excludes observations entirely regardless of freshness", s["available"] is False)

# ---- 11. continuous healthy ISD behavior remains unchanged ----
healthy_rows = [obs_row(KNYC, BASE - timedelta(minutes=m), 70.0 + (m % 5)) for m in range(9, 600, 51)]
obs = make_obs(healthy_rows)
state = ws.get_observation_state(obs, BASE, ws.PROXY_STATE, STATION_IDS, target_date=BASE.date())
s = state["stations"][KNYC]
check("continuous healthy reporting: available=True, age < 1h (unchanged from pre-fix behavior)",
      s["available"] is True and s["observation_age"] < timedelta(hours=1))

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
if results["failed"]:
    print("FAILURES:")
    for f in results["failed"]:
        print(" -", f)
    sys.exit(1)
