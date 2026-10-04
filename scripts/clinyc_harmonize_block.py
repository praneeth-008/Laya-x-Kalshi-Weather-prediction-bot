"""LABEL-ONLY harmonization: attaches the canonical CLINYC daily-Tmax label
layer to an ALREADY-FROZEN block's integrated output, without re-downloading
or re-extracting any numerical source (HRRR/GFS/NBM/GEFS/ECMWF/ISD
observations/AFD) and without altering any existing column's values.

Adds, to both integrated/state_index.parquet and integrated/structured_features.parquet:
  canonical_tmax_label_f, canonical_label_source, canonical_label_status

Additionally to structured_features.parquet only (fuller audit detail):
  isd_sampled_tmax_f (alias of the existing ISD-derived label_tmax_f, made
    explicit as a FEATURE/audit value, never the supervised target),
  clinyc_minus_isd_f, clinyc_product_id, clinyc_issuance_time_utc,
  clinyc_issuing_office, clinyc_report_status, clinyc_max_temp_flag,
  clinyc_parser_version

Idempotent: reruns overwrite these same columns rather than duplicating them.

The canonical supervised label going forward is:
    y_d = finalized NWS CLINYC Central Park daily Tmax (canonical_tmax_label_f)
ISD-derived label_tmax_f / isd_sampled_tmax_f remain FEATURE/audit data only.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from scripts.backfill_common import BACKFILL_ROOT, block_arg_parser, get_block
import scripts.backfill_clinyc as backfill_clinyc

HARMONIZED_STATE_INDEX_COLS = ["canonical_tmax_label_f", "canonical_label_source", "canonical_label_status"]
HARMONIZED_STRUCTURED_COLS = HARMONIZED_STATE_INDEX_COLS + [
    "isd_sampled_tmax_f", "clinyc_minus_isd_f", "clinyc_product_id", "clinyc_issuance_time_utc",
    "clinyc_issuing_office", "clinyc_report_status", "clinyc_max_temp_flag", "clinyc_parser_version",
]


def _drop_existing(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df.drop(columns=[c for c in cols if c in df.columns])


def harmonize(block_id: str) -> dict:
    block = get_block(block_id)
    integrated_dir = BACKFILL_ROOT / block_id / "integrated"
    state_index_path = integrated_dir / "state_index.parquet"
    structured_path = integrated_dir / "structured_features.parquet"

    if not state_index_path.exists() or not structured_path.exists():
        raise FileNotFoundError(
            f"{block_id}: integrated output not found at {integrated_dir} -- block must be built/frozen "
            "before label-only harmonization can attach to it."
        )

    label_report = backfill_clinyc.run(block_id)

    labels_df = pd.read_parquet(BACKFILL_ROOT / block_id / "clinyc" / "clinyc_labels.parquet")
    labels_df["target_date"] = pd.to_datetime(labels_df["target_date"]).dt.date

    clinyc_cols = labels_df.rename(columns={
        "product_id": "clinyc_product_id",
        "issuance_time": "clinyc_issuance_time_utc",
        "issuing_office": "clinyc_issuing_office",
        "report_status": "clinyc_report_status",
        "max_temp_flag": "clinyc_max_temp_flag",
        "parser_version": "clinyc_parser_version",
    })[["target_date", "canonical_tmax_label_f", "canonical_label_source", "canonical_label_status",
        "clinyc_product_id", "clinyc_issuance_time_utc", "clinyc_issuing_office", "clinyc_report_status",
        "clinyc_max_temp_flag", "clinyc_parser_version"]]

    state_index = pd.read_parquet(state_index_path)
    state_index["target_date"] = pd.to_datetime(state_index["target_date"]).dt.date
    state_index = _drop_existing(state_index, HARMONIZED_STATE_INDEX_COLS)
    state_index = state_index.merge(
        clinyc_cols[["target_date", "canonical_tmax_label_f", "canonical_label_source", "canonical_label_status"]],
        on="target_date", how="left", validate="many_to_one",
    )

    structured = pd.read_parquet(structured_path)
    structured["target_date"] = pd.to_datetime(structured["target_date"]).dt.date
    structured = _drop_existing(structured, HARMONIZED_STRUCTURED_COLS)
    structured = structured.merge(clinyc_cols, on="target_date", how="left", validate="many_to_one")
    structured["isd_sampled_tmax_f"] = structured["label_tmax_f"]
    structured["clinyc_minus_isd_f"] = structured["canonical_tmax_label_f"] - structured["isd_sampled_tmax_f"]

    n_rows_before_si, n_rows_before_sf = len(pd.read_parquet(state_index_path)), len(pd.read_parquet(structured_path))
    assert len(state_index) == n_rows_before_si, "harmonization must not change state_index row count"
    assert len(structured) == n_rows_before_sf, "harmonization must not change structured_features row count"

    state_index.to_parquet(state_index_path, index=False)
    structured.to_parquet(structured_path, index=False)

    n_rows_with_label = int(structured["canonical_tmax_label_f"].notna().sum())
    n_rows_unavailable = int((structured["canonical_label_status"] == "LABEL_UNAVAILABLE_CLINYC").sum())
    both = structured.dropna(subset=["canonical_tmax_label_f", "isd_sampled_tmax_f"])

    summary = {
        "block_id": block_id,
        "harmonization_type": "LABEL_ONLY (no numerical source re-extraction)",
        "canonical_label_definition": "y_d = finalized NWS CLINYC Central Park daily Tmax",
        "clinyc_label_coverage": label_report,
        "integrated_rows_with_canonical_label": n_rows_with_label,
        "integrated_rows_label_unavailable": n_rows_unavailable,
        "integrated_total_rows": len(structured),
        "isd_vs_clinyc_audit": {
            "n_days_with_both": int(both["target_date"].nunique()),
            "mean_clinyc_minus_isd_f": float(both["clinyc_minus_isd_f"].mean()) if len(both) else None,
            "max_abs_clinyc_minus_isd_f": float(both["clinyc_minus_isd_f"].abs().max()) if len(both) else None,
        },
        "no_lookahead_note": "canonical_tmax_label_f is an outcome label (same no-lookahead guarantee as the pre-existing label_tmax_f); it is never used as an input feature for any state on or before its own target_date.",
    }

    out_path = BACKFILL_ROOT / block_id / "clinyc" / "harmonization_summary.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    manifest_path = BACKFILL_ROOT / block_id / "block_manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        manifest["clinyc_label_harmonization"] = {
            "applied": True,
            "note": "Label-only pass; does not alter numerical extraction/freeze status above.",
            "summary_path": f"data/processed/backfill/{block_id}/clinyc/harmonization_summary.json",
            "canonical_label_coverage_pct": label_report["coverage_pct"],
            "missing_canonical_label_dates": label_report["missing_dates"],
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)

    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    args = block_arg_parser("CLINYC label-only harmonization").parse_args()
    harmonize(args.block)
