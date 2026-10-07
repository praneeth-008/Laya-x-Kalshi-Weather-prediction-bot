"""Regression tests for the 2026-10-07 fix distinguishing a confirmed-404
"archive year not published yet" outcome (structural unavailability) from
a genuine extraction failure in scripts/backfill_observations.py. See
docs/decisions.md and the stop condition: "validation cannot distinguish
structural unavailability from extraction failure" must never be true."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.backfill_observations import load_or_fetch_year, empty_observation_frame
from data.observations import RequestStats

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


def _fake_response(status_code):
    r = requests.Response()
    r.status_code = status_code
    return r


station = {"station_id": "725053-94728", "usaf": "725053", "wban": "94728"}

# ---- a confirmed 404 is treated as structural absence, not raised ----
with patch("scripts.backfill_observations.RAW_DIR") as mock_dir:
    mock_path = mock_dir.__truediv__.return_value
    mock_path.exists.return_value = False
    with patch("scripts.backfill_observations.fetch_station_year") as mock_fetch:
        mock_fetch.side_effect = requests.exceptions.HTTPError(response=_fake_response(404))
        result = load_or_fetch_year(station, 2026, RequestStats())
        check("a 404 on the annual file returns None (structural absence), not raised", result is None)

# ---- any OTHER HTTP error still propagates and fails loudly ----
with patch("scripts.backfill_observations.RAW_DIR") as mock_dir:
    mock_path = mock_dir.__truediv__.return_value
    mock_path.exists.return_value = False
    with patch("scripts.backfill_observations.fetch_station_year") as mock_fetch:
        mock_fetch.side_effect = requests.exceptions.HTTPError(response=_fake_response(500))
        try:
            load_or_fetch_year(station, 2026, RequestStats())
            check("a 500 error still propagates (never silently swallowed)", False, "did not raise")
        except requests.exceptions.HTTPError:
            check("a 500 error still propagates (never silently swallowed)", True)

# ---- empty_observation_frame() matches the real parser's schema exactly ----
from data.observations import parse_isd_row
real_cols = set(parse_isd_row({}).keys())
empty_cols = set(empty_observation_frame().columns)
check("empty_observation_frame() schema matches parse_isd_row()'s real output exactly", real_cols == empty_cols,
      f"missing: {real_cols - empty_cols}, extra: {empty_cols - real_cols}")
check("empty_observation_frame() has zero rows", len(empty_observation_frame()) == 0)

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
if results["failed"]:
    print("FAILURES:")
    for f in results["failed"]:
        print(" -", f)
    sys.exit(1)
