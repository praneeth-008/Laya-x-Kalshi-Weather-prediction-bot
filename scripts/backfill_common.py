"""Shared infrastructure for the HISTORICAL BACKFILL (2026-09-29), generalizing
the 40-sampled-day pilot scripts (scripts/pilot_phase3*.py..pilot_phase7*.py)
to continuous SIX-MONTH CALENDAR BLOCKS, per the approved baseline commit
8af8d30 (40-day pilot pipeline-survival validation).

CRITICAL: this module does not change any extraction METHODOLOGY. Every
backfill_{source}.py script reuses the exact same data/{source}.py primitives
(grib_key/fetch_idx/find_message/fetch_byte_range/decode_message_multi_point)
and the exact same required_forecast_hours_for_run()/run_times_for_selected_days()
shape as its pilot_phase*.py counterpart -- the ONLY generalization is that
SELECTED_DATES becomes every calendar day in a block instead of 40 sampled
days, and OUT_DIR becomes block-scoped so backfill output never touches or
depends on the frozen pilot output under data/processed/pilot/.

BLOCK SCHEDULE: ten SIX-MONTH CALENDAR blocks (Jan-Jun / Jul-Dec), newest
complete block first, working backward to ~5 years of history.

CORRECTED 2026-09-29 during Block 1 pre-launch: the calendar alone is NOT
sufficient to determine "the newest complete block" -- the OBSERVATIONS
source (NOAA ISD Global Hourly, noaa-global-hourly-pds S3 bucket), which
supplies the realized-Tmax LABEL for every state, lags well behind "today."
Direct probe (HEAD + content check, 2026-09-29): the annual per-station CSV
for 2026 returns HTTP 404 for all 4 stations (KNYC/KLGA/KJFK/KEWR) -- it does
not exist yet. The 2025 annual file DOES exist (HTTP 200) but its actual
content currently ends at 2025-08-25..2025-08-27 (verified per-station,
consistent across all 4 stations) -- NOT the full calendar year. This matches
almost exactly the original 40-day pilot's own PILOT_START_DATE..PILOT_END_DATE
window (2025-01-01..2025-08-24), confirming this was already the real,
governing constraint when the pilot itself was built.

The numerical forecast sources (HRRR/GFS/NBM/GEFS/ECMWF) ARE current through
at least 2026-01-01 (confirmed via scripts/backfill_grid_check.py's live
probes returning real, freshly-dated messages) -- only the ISD observations
archive lags. Since observations supply the label, a block cannot be
FROZEN without them, so the true "newest complete block... without
depending on an incomplete current period" is bounded by the OBSERVATIONS
archive, not the calendar or the numerical archives:

  2026H1 (2026-01-01..06-30): observations archive returns 404 -- SKIP.
  2025H2 (2025-07-01..12-31): observations only cover through ~08-27 --
    INCOMPLETE, would be missing Sep-Dec entirely -- SKIP.
  2025H1 (2025-01-01..06-30): fully covered by the confirmed-available
    observations window, with a ~2-month safety margin before the real
    cutoff -- this is the true newest COMPLETE block. START HERE.

Working backward in strict six-month steps for 10 blocks from 2025H1 reaches
five years of history: 2020-07-01 through 2025-06-30. (NBM's own confirmed
archive floor is "at least back to 2021" per data/nbm.py's module docstring
-- blend.20200101/ returns zero keys. The oldest planned block, 2020H2,
therefore needs its own explicit re-verification when reached; this is
flagged, not assumed, and is NOT a reason to change anything about the
blocks between now and then.)

These are ENGINEERING/EXTRACTION units, not ML train/test splits.
"""
import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKFILL_ROOT = ROOT / "data/processed/backfill"

CENTRAL_PARK = {"lat": 40.7794, "lon": -73.9691}
PATCH_OFFSETS = [(-0.03, -0.03), (-0.03, 0), (-0.03, 0.03), (0, -0.03), (0, 0), (0, 0.03), (0.03, -0.03), (0.03, 0), (0.03, 0.03)]

# ECMWF's grib_key() (ifs/0p25 archive path regime) is only valid from
# 2024-02-29 onward -- confirmed in scripts/pilot_phase7_ecmwf.py's docstring
# via a 21-date probe. Older regimes exist but are NOT implemented. Any block
# (or portion of a block) before this date must treat ECMWF as STRUCTURALLY
# UNAVAILABLE, not a failure. This is the one explicitly pre-documented
# historical regime boundary; any OTHER regime change encountered in an older
# block is an UNKNOWN and must STOP the autonomous loop (see docs/backfill_plan.md).
ECMWF_VALID_FROM = date(2024, 2, 29)

# NBM's confirmed observed archive depth is "at least back to 2021" (blend.20210101/
# exists; blend.20200101/ returns zero keys -- see data/nbm.py's module docstring).
# Our oldest planned block (2020 H2) starts BEFORE this confirmed floor, so it
# needs its own explicit re-verification when reached (flagged, not assumed).
NBM_OBSERVED_ARCHIVE_FLOOR = date(2021, 1, 1)

# Real, directly-probed NOAA ISD Global Hourly (observations) archive currency
# as of 2026-09-29 -- the 2025 annual per-station file exists but its content
# currently ends here (confirmed per-station: KNYC/KLGA/KJFK 2025-08-27,
# KEWR 2025-08-25); the 2026 annual file does not exist yet (HTTP 404). This
# is why BLOCKS starts at 2025H1 rather than the calendar-only "newest
# complete" block. Re-probe before assuming this has advanced.
OBSERVATIONS_ARCHIVE_CONTENT_ENDS = date(2025, 8, 25)  # conservative (KEWR's earlier cutoff)


@dataclass(frozen=True)
class BlockSpec:
    block_id: str
    start: date
    end: date  # inclusive


def _six_month_blocks(newest_start: date, n_blocks: int) -> list[BlockSpec]:
    blocks = []
    cur_start = newest_start
    for _ in range(n_blocks):
        if cur_start.month == 1:
            cur_end = date(cur_start.year, 6, 30)
            block_id = f"{cur_start.year}H1"
        else:
            cur_end = date(cur_start.year, 12, 31)
            block_id = f"{cur_start.year}H2"
        blocks.append(BlockSpec(block_id, cur_start, cur_end))
        # step back six months
        if cur_start.month == 1:
            cur_start = date(cur_start.year - 1, 7, 1)
        else:
            cur_start = date(cur_start.year, 1, 1)
    return blocks


# Full block REGISTRY (definitions only, not processing order): the original
# 10 blocks (2020H2..2025H1) plus 2025H2 and 2026H1, added 2026-10-02 at the
# user's explicit request to temporarily prioritize recent history despite
# their known partial/zero observations coverage (see PROCESSING_QUEUE and
# the module docstring addendum below). 12 blocks total, 2020-07-01..2026-06-30.
BLOCKS: list[BlockSpec] = _six_month_blocks(date(2026, 1, 1), 12)
BLOCKS_BY_ID = {b.block_id: b for b in BLOCKS}

# PROCESSING QUEUE -- the actual order blocks are run in, distinct from BLOCKS
# (which is just the registry of valid definitions). Updated 2026-10-02 per
# explicit user instruction: after 2024H2 freezes, temporarily jump to the
# two newest blocks (2025H2, 2026H1) instead of continuing the backward
# sequence to 2024H1, then resume the original backward order. This is ONLY
# an execution-order change -- no extraction methodology, schema, variable,
# source, or validation-criteria change is implied or authorized by it.
#
# 2025H2 and 2026H1 are known (confirmed via live archive probe, unchanged
# 2026-09-29 -> 2026-10-02) to have partial (~31%, through ~2025-08-25/27)
# and zero observations/label coverage respectively. Per explicit user
# instruction on 2026-10-02: do NOT change observation/label methodology to
# work around this without further instruction -- the user said they will
# provide an update about an additional KNYC observation/label source
# before deciding how 2025H2/2026H1's missing observations are handled.
# Do not skip, reduce scope, or start investigating a replacement source
# independently; wait for that update when these two blocks are reached.
PROCESSING_QUEUE: list[str] = [
    "2025H1",  # FROZEN (commit 22241b8) -- do not re-extract
    "2024H2",  # IN PROGRESS as of 2026-10-02 -- finish via the existing unmodified procedure
    "2025H2",  # next after 2024H2 -- WAIT for the user's KNYC-source update before handling observations
    "2026H1",  # after 2025H2 -- same wait applies
    "2024H1", "2023H2", "2023H1", "2022H2", "2022H1", "2021H2", "2021H1", "2020H2",  # resume original backward sequence
]


def calendar_days(start: date, end: date) -> list[date]:
    n = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(n)]


def block_out_dir(source: str, block_id: str) -> Path:
    d = BACKFILL_ROOT / block_id / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_block(block_id: str) -> BlockSpec:
    if block_id not in BLOCKS_BY_ID:
        raise ValueError(f"Unknown block_id {block_id!r}. Valid: {list(BLOCKS_BY_ID)}")
    return BLOCKS_BY_ID[block_id]


def block_arg_parser(source_label: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"Backfill {source_label} for one six-month block")
    p.add_argument("--block", required=True, choices=list(BLOCKS_BY_ID), help="six-month block id, e.g. 2026H1")
    return p


# Realized bytes-per-work-item, measured from the SUM of each item's own
# per-item `remote_bytes` field recorded in the completed 40-day pilot's
# checkpoint JSON files (data/processed/pilot/{source}/*_checkpoint.json).
#
# CORRECTED 2026-09-29 during backfill pre-launch testing: an earlier version
# of this table used each script's *_download_report.json "total_bytes_this_run"
# field, which is only that SCRIPT INVOCATION's own incremental byte count --
# several of these pilot scripts were interrupted/resumed multiple times
# during the session, so "total_bytes_this_run" silently reflected only the
# LAST invocation, understating the true per-pilot total by 2-3 orders of
# magnitude for sources that were resumed (hrrr, gfs, nbm, gefs). This was
# caught by the mandated small-safe-test (real per-item bytes measured ~11.4
# MB/item for HRRR core vs. the ~945 B/item the old table implied) BEFORE any
# block was launched -- no extraction had occurred yet under the wrong table.
# The corrected per-item rates below now match each pilot script's own
# pre-benchmark docstring estimate (e.g. HRRR's "~259 GB estimated", NBM's
# "~314 GB estimated", GEFS's "~396 GB estimated") almost exactly, confirming
# those original estimates were accurate and this table was the only error.
REALIZED_BYTES_PER_ITEM = {
    "hrrr": 236_861_214_802 / 20_836,        # ~11.37 MB/item (8 core vars, full-CONUS-grid GRIB2 messages)
    "hrrr_apcp_1h": 7_535_653_814 / 19_996,  # ~377 KB/item
    "hrrr_rh": 33_185_453_719 / 20_836,      # ~1.59 MB/item
    "gfs": 39_208_661_547 / 5_836,           # ~6.72 MB/item
    "nbm": 316_040_452_911 / 31_578,         # ~10.01 MB/item
    "gefs": 402_847_517_313 / 62_744,        # ~6.42 MB/item
    "ecmwf": 5_988_525_259 / 904,            # ~6.62 MB/item (unchanged -- this one was never resumed)
}
BYTE_BUDGET_SAFETY_MULTIPLIER = 3.0


def scaled_byte_budget(source: str, n_work_items: int) -> float:
    rate = REALIZED_BYTES_PER_ITEM[source]
    return rate * max(n_work_items, 1) * BYTE_BUDGET_SAFETY_MULTIPLIER
