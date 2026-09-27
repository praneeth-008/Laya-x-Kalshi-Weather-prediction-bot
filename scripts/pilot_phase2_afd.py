"""PILOT Phase 2 -- NWS AFDOKX for 2025-01-01 to PILOT_END_DATE. Resumable:
a JSON checkpoint tracks per-product_id fetch status so a rerun skips
already-DONE documents and only retries FAILED ones.
"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from data.afd import list_product_ids, fetch_raw_text, parse_sections, RequestStats

PILOT_START_DATE = date(2025, 1, 1)
PILOT_END_DATE = date(2025, 8, 24)
PIL = "AFDOKX"
OFFICE = "OKX"

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data/raw/pilot/afd/products"
OUT_DIR = ROOT / "data/processed/pilot/afd"
RAW_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = OUT_DIR / "pilot_afd_checkpoint.json"


def load_checkpoint():
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {"days_listed": [], "products": {}}


def save_checkpoint(cp):
    tmp = CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, indent=2, default=str))
    tmp.replace(CHECKPOINT_PATH)


def main():
    stats = RequestStats()
    cp = load_checkpoint()

    n_days = (PILOT_END_DATE - PILOT_START_DATE).days + 1
    all_dates = [PILOT_START_DATE + timedelta(days=i) for i in range(n_days + 1)]  # +1 day margin for UTC spillover

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
            save_checkpoint(cp)
            print(f"  listed {len(cp['days_listed'])}/{len(all_dates)} days, {len(all_product_ids)} distinct products so far")

    save_checkpoint(cp)
    print(f"Listing complete: {len(all_product_ids)} distinct product_ids across {len(all_dates)} UTC dates")

    pending = [pid for pid, info in cp["products"].items() if info["status"] != "DONE"]
    print(f"Fetching {len(pending)} pending documents...")

    docs = []
    for i, pid in enumerate(pending):
        raw_path = RAW_DIR / f"{pid}.txt"
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
            save_checkpoint(cp)
            print(f"  fetched {i+1}/{len(pending)}")

    save_checkpoint(cp)

    # Build canonical documents + sections tables from ALL DONE products (raw files on disk).
    doc_rows = []
    section_rows = []
    for pid, info in cp["products"].items():
        if info["status"] != "DONE":
            continue
        raw_path = RAW_DIR / f"{pid}.txt"
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

    docs_df = pd.DataFrame(doc_rows).sort_values("issuance_time")
    sections_df = pd.DataFrame(section_rows)
    docs_df.to_parquet(OUT_DIR / "pilot_afd_documents.parquet", index=False)
    sections_df.to_parquet(OUT_DIR / "pilot_afd_sections.parquet", index=False)

    # Validation.
    docs_df["issuance_date"] = docs_df["issuance_time"].dt.date
    counts_by_day = docs_df.groupby("issuance_date").size()
    all_pilot_dates = [PILOT_START_DATE + timedelta(days=i) for i in range(n_days)]
    missing_days = [str(d) for d in all_pilot_dates if d not in counts_by_day.index]
    dup_products = docs_df["product_id"].duplicated().sum()
    section_counts = sections_df.groupby("section_name").size().sort_values(ascending=False).head(20).to_dict()

    failed = [pid for pid, info in cp["products"].items() if info["status"] == "FAILED"]

    report = {
        "pilot_start_date": str(PILOT_START_DATE), "pilot_end_date": str(PILOT_END_DATE),
        "total_documents": len(docs_df),
        "documents_per_day_mean": float(counts_by_day.mean()),
        "documents_per_day_min": int(counts_by_day.min()),
        "documents_per_day_max": int(counts_by_day.max()),
        "missing_days_count": len(missing_days),
        "missing_days": missing_days,
        "duplicate_products": int(dup_products),
        "section_availability_top20": section_counts,
        "text_length_mean": float(docs_df["text_length"].mean()),
        "text_length_min": int(docs_df["text_length"].min()),
        "text_length_max": int(docs_df["text_length"].max()),
        "failed_products": failed,
        "total_requests": stats.n_requests,
        "total_bytes_downloaded": stats.bytes_downloaded,
        "status": "PASS" if len(missing_days) == 0 and len(failed) == 0 else "WARNING",
    }
    with open(OUT_DIR / "pilot_afd_download_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nTotal documents: {len(docs_df)}, missing days: {len(missing_days)}, failed: {len(failed)}")
    print(f"Status: {report['status']}")
    print("Outputs written to", OUT_DIR)


if __name__ == "__main__":
    main()
