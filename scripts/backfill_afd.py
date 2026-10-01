"""HISTORICAL BACKFILL -- NWS AFDOKX text products, one six-month calendar
block. Generalizes scripts/pilot_phase2_afd.py (approved baseline 8af8d30)
from the fixed 2025-01-01..2025-08-24 pilot window to an arbitrary block's
[start, end] range. Resumable: a JSON checkpoint tracks per-product_id fetch
status so a rerun skips already-DONE documents and only retries FAILED ones.

Usage: python scripts/backfill_afd.py --block 2026H1
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.afd import list_product_ids, fetch_raw_text, parse_sections, RequestStats
from scripts.backfill_common import block_arg_parser, get_block, block_out_dir

SOURCE = "afd"
PIL = "AFDOKX"
OFFICE = "OKX"
ROOT = Path(__file__).resolve().parents[1]


def load_checkpoint(checkpoint_path):
    if checkpoint_path.exists():
        return json.loads(checkpoint_path.read_text())
    return {"days_listed": [], "products": {}}


def save_checkpoint(cp, checkpoint_path):
    tmp = checkpoint_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, indent=2, default=str))
    tmp.replace(checkpoint_path)


def main():
    args = block_arg_parser("AFD").parse_args()
    block = get_block(args.block)
    out_dir = block_out_dir(SOURCE, block.block_id)
    raw_dir = ROOT / "data/raw/backfill/afd/products"
    raw_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / f"backfill_{SOURCE}_checkpoint.json"

    stats = RequestStats()
    cp = load_checkpoint(checkpoint_path)

    n_days = (block.end - block.start).days + 1
    all_dates = [block.start + timedelta(days=i) for i in range(n_days + 1)]  # +1 day margin for UTC spillover

    all_product_ids = set(cp["products"].keys())
    for d in all_dates:
        day_key = str(d)
        if day_key in cp["days_listed"]:
            continue
        try:
            products = list_product_ids(PIL, d.year, d.month, d.day, stats=stats)
        except Exception as e:
            print(f"  LIST FAILED for {day_key}: {e}")
            continue
        for p in products:
            all_product_ids.add(p["product_id"])
            if p["product_id"] not in cp["products"]:
                cp["products"][p["product_id"]] = {"issuance_time": str(p["issuance_time"]), "status": "PENDING"}
        cp["days_listed"].append(day_key)
        if len(cp["days_listed"]) % 30 == 0:
            save_checkpoint(cp, checkpoint_path)
            print(f"[{block.block_id}]  listed {len(cp['days_listed'])}/{len(all_dates)} days, {len(all_product_ids)} distinct products so far")

    save_checkpoint(cp, checkpoint_path)
    print(f"[{block.block_id}] Listing complete: {len(all_product_ids)} distinct product_ids across {len(all_dates)} UTC dates")

    pending = [pid for pid, info in cp["products"].items() if info["status"] != "DONE"]
    print(f"[{block.block_id}] Fetching {len(pending)} pending documents...")

    for i, pid in enumerate(pending):
        raw_path = raw_dir / f"{pid}.txt"
        try:
            if raw_path.exists():
                raw_text = raw_path.read_text(encoding="utf-8")
            else:
                raw_text = fetch_raw_text(pid, stats=stats)
                raw_path.write_text(raw_text, encoding="utf-8")
            cp["products"][pid]["status"] = "DONE"
            cp["products"][pid]["text_length"] = len(raw_text)
        except Exception as e:
            cp["products"][pid]["status"] = "FAILED"
            cp["products"][pid]["error"] = str(e)
            print(f"  FAILED {pid}: {e}")
        if (i + 1) % 100 == 0:
            save_checkpoint(cp, checkpoint_path)
            print(f"[{block.block_id}]  fetched {i+1}/{len(pending)}")

    save_checkpoint(cp, checkpoint_path)

    doc_rows = []
    section_rows = []
    for pid, info in cp["products"].items():
        if info["status"] != "DONE":
            continue
        raw_path = raw_dir / f"{pid}.txt"
        raw_text = raw_path.read_text(encoding="utf-8")
        issuance_time = pd.Timestamp(info["issuance_time"])
        doc_rows.append(
            {
                "source": "afd", "office": OFFICE, "product_id": pid, "product_type": "AFD",
                "issuance_time": issuance_time, "available_time": None,
                "availability_time_type": "ISSUANCE_TIME_PROXY",
                "text_length": len(raw_text), "raw_text": raw_text,
            }
        )
        for sec in parse_sections(raw_text):
            section_rows.append({"product_id": pid, "issuance_time": issuance_time, **sec})

    docs_df = pd.DataFrame(doc_rows).sort_values("issuance_time") if doc_rows else pd.DataFrame(doc_rows)
    sections_df = pd.DataFrame(section_rows)
    docs_df.to_parquet(out_dir / "backfill_afd_documents.parquet", index=False)
    sections_df.to_parquet(out_dir / "backfill_afd_sections.parquet", index=False)

    if not docs_df.empty:
        docs_df["issuance_date"] = docs_df["issuance_time"].dt.date
        counts_by_day = docs_df.groupby("issuance_date").size()
    else:
        counts_by_day = pd.Series(dtype=int)
    all_block_dates = [block.start + timedelta(days=i) for i in range(n_days)]
    missing_days = [str(d) for d in all_block_dates if d not in counts_by_day.index]
    dup_products = docs_df["product_id"].duplicated().sum() if not docs_df.empty else 0
    section_counts = sections_df.groupby("section_name").size().sort_values(ascending=False).head(20).to_dict() if not sections_df.empty else {}

    failed = [pid for pid, info in cp["products"].items() if info["status"] == "FAILED"]

    report = {
        "block_id": block.block_id, "block_start": str(block.start), "block_end": str(block.end),
        "total_documents": len(docs_df),
        "documents_per_day_mean": float(counts_by_day.mean()) if len(counts_by_day) else None,
        "documents_per_day_min": int(counts_by_day.min()) if len(counts_by_day) else None,
        "documents_per_day_max": int(counts_by_day.max()) if len(counts_by_day) else None,
        "missing_days_count": len(missing_days),
        "missing_days": missing_days,
        "duplicate_products": int(dup_products),
        "section_availability_top20": section_counts,
        "text_length_mean": float(docs_df["text_length"].mean()) if not docs_df.empty else None,
        "text_length_min": int(docs_df["text_length"].min()) if not docs_df.empty else None,
        "text_length_max": int(docs_df["text_length"].max()) if not docs_df.empty else None,
        "failed_products": failed,
        "total_requests": stats.n_requests,
        "total_bytes_downloaded": stats.bytes_downloaded,
        "status": "PASS" if len(missing_days) == 0 and len(failed) == 0 else "WARNING",
    }
    with open(out_dir / "backfill_afd_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n[{block.block_id}] Total documents: {len(docs_df)}, missing days: {len(missing_days)}, failed: {len(failed)}")
    print(f"[{block.block_id}] Status: {report['status']}")
    print("Outputs written to", out_dir)


if __name__ == "__main__":
    main()
