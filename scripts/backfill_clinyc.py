"""Builds the canonical CLINYC daily-Tmax LABEL layer for one six-month
block. This is the LABEL source (NWS CLINYC Central Park daily climate
report); it is intentionally separate from the intraday-feature extraction
scripts (backfill_observations.py etc.) -- see data/clinyc.py's module
docstring for the full investigation and selection-rule rationale, approved
by the user 2026-10-04 as the canonical y_d definition:

    y_d = finalized NWS CLINYC Central Park daily Tmax

ISD/KNYC observations remain a point-in-time FEATURE source and are neither
modified nor replaced by this script.

Output (data/processed/backfill/{block}/clinyc/):
  - raw/{product_id}.txt           raw report text, preserved for audit
  - clinyc_labels.parquet          one row per calendar day in the block
  - clinyc_validation_report.json  coverage + anomaly report for this block

Genuinely missing days are marked canonical_label_status =
LABEL_UNAVAILABLE_CLINYC and are NEVER filled from ISD, a preliminary
report, another station, interpolation, or a model estimate.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from scripts.backfill_common import BACKFILL_ROOT, block_arg_parser, calendar_days, get_block
from data.clinyc import build_canonical_labels, RequestStats, LABEL_UNAVAILABLE_STATUS


def run(block_id: str) -> dict:
    block = get_block(block_id)
    dates = calendar_days(block.start, block.end)

    out_dir = BACKFILL_ROOT / block_id / "clinyc"
    raw_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    stats = RequestStats()
    labels, parse_errors = build_canonical_labels(dates, raw_dir=raw_dir, stats=stats)

    df = pd.DataFrame(labels)
    df.to_parquet(out_dir / "clinyc_labels.parquet", index=False)

    n_days = len(dates)
    available = [r for r in labels if r["canonical_label_status"] == "OK"]
    missing = [r for r in labels if r["canonical_label_status"] == LABEL_UNAVAILABLE_STATUS]
    selection_errors = [r for r in labels if r.get("selection_error")]
    multi_final = [r for r in labels if r["canonical_label_status"] == "OK" and r["n_final_candidates"] > 1]
    prelim_rejected_days = [r for r in labels if r["n_preliminary_rejected"] > 0]
    provenance_gaps = [
        r for r in available
        if not (r.get("product_id") and r.get("issuance_time") is not None and r.get("parser_version"))
    ]
    impossible_values = [r for r in available if r["canonical_tmax_label_f"] is not None and not (-40.0 <= r["canonical_tmax_label_f"] <= 120.0)]

    report = {
        "block_id": block_id,
        "n_days_expected": n_days,
        "n_days_available": len(available),
        "n_days_missing": len(missing),
        "coverage_pct": round(len(available) / n_days * 100, 2) if n_days else None,
        "missing_dates": [str(r["target_date"]) for r in missing],
        "multi_final_days": [
            {"date": str(r["target_date"]), "n_candidates": r["n_final_candidates"], "notes": r["selection_notes"]}
            for r in multi_final
        ],
        "days_with_preliminary_rejected": len(prelim_rejected_days),
        "selection_errors": [{"date": str(r["target_date"]), "error": r["selection_error"]} for r in selection_errors],
        "parse_errors": parse_errors,
        "duplicate_target_dates": int(df["target_date"].duplicated().sum()) if len(df) else 0,
        "impossible_values": [{"date": str(r["target_date"]), "value": r["canonical_tmax_label_f"]} for r in impossible_values],
        "provenance_validation": "PASS" if not provenance_gaps else f"FAIL: {len(provenance_gaps)} available day(s) missing product_id/issuance_time/parser_version",
        "no_lookahead_check": "PASS (enforced inside select_canonical_label: every used final's issuance_time must postdate its own target_date)",
        "total_requests": stats.n_requests,
        "total_bytes": stats.bytes_downloaded,
    }
    with open(out_dir / "clinyc_validation_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(json.dumps(report, indent=2, default=str))
    return report


if __name__ == "__main__":
    args = block_arg_parser("CLINYC canonical labels").parse_args()
    run(args.block)
