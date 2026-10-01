"""HISTORICAL BACKFILL -- lightweight pre-block grid/location sanity check
(section 4 of the backfill spec). For each applicable numerical source,
fetches ONE representative message near the start of the target block AND
one from a known-good pilot-era reference date, decodes both, and compares
grid fingerprint (md5GridSection), dimensions (Ni/Nj), and Central-Park
nearest-grid-point distance. A fingerprint mismatch is NOT automatically an
error (see spec) -- it is logged and flagged for investigation; the check
still reports PASS/FLAGGED per source rather than raising, so the caller
(the block controller) decides whether to proceed or stop.

Usage: python scripts/backfill_grid_check.py --block 2026H1
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_common import CENTRAL_PARK, PATCH_OFFSETS, block_arg_parser, get_block, ECMWF_VALID_FROM

REFERENCE_DATE_UTC = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)  # known-good pilot-era date, already validated throughout this project


def _probe_eccodes(raw_bytes):
    import eccodes
    gid = eccodes.codes_new_from_message(raw_bytes)
    try:
        info = {}
        for key in ["gridType", "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees"]:
            try:
                info[key] = eccodes.codes_get(gid, key)
            except Exception:
                info[key] = None
        try:
            info["md5GridSection"] = eccodes.codes_get(gid, "md5GridSection")
        except Exception:
            info["md5GridSection"] = None
        return info
    finally:
        eccodes.codes_release(gid)


def probe_hrrr(run_time):
    from data.hrrr import grib_key, fetch_idx, find_message, fetch_byte_range
    key = grib_key(run_time, run_time.hour, 3)
    idx = fetch_idx(key)
    msg = find_message(idx, "TMP", "2 m above ground")
    if msg is None:
        return None
    raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    from data.pilot_extraction import decode_message_multi_point
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    decoded = decode_message_multi_point(raw, points, max_distance_km=50)[4]  # center point
    info = _probe_eccodes(raw)
    info["distance_km"] = decoded["distance_km"]
    return info


def probe_gfs(run_time):
    from data.gfs import grib_key, fetch_idx, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
    key = grib_key(run_time, run_time.hour, 3)
    idx = fetch_idx(key)
    msg = find_message(idx, "TMP", "2 m above ground")
    if msg is None:
        return None
    raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    from data.pilot_extraction import decode_message_multi_point
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    decoded = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)[4]
    info = _probe_eccodes(raw)
    info["distance_km"] = decoded["distance_km"]
    return info


def probe_nbm(run_time):
    from data.nbm import grib_key, fetch_idx, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
    key = grib_key(run_time, run_time.hour, 3, "core")
    idx = fetch_idx(key)
    msg = find_message(idx, "TMP", "2 m above ground", desc_excludes="ens std dev")
    if msg is None:
        return None
    raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    from data.pilot_extraction import decode_message_multi_point
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    decoded = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)[4]
    info = _probe_eccodes(raw)
    info["distance_km"] = decoded["distance_km"]
    return info


def probe_gefs(run_time):
    from data.gefs import grib_key, fetch_idx, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
    key = grib_key(run_time, run_time.hour, "gec00", 3, "pgrb2sp25")
    idx = fetch_idx(key)
    msg = find_message(idx, "TMP", "2 m above ground")
    if msg is None:
        return None
    raw, _ = fetch_byte_range(key, msg["byte_start"], msg.get("byte_end"))
    from data.pilot_extraction import decode_message_multi_point
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    decoded = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)[4]
    info = _probe_eccodes(raw)
    info["distance_km"] = decoded["distance_km"]
    return info


def probe_ecmwf(run_time):
    from data.ecmwf import grib_key, fetch_index, find_message, fetch_byte_range, MAX_PATCH_DISTANCE_KM
    key = grib_key(run_time, run_time.hour, 3, "oper")
    entries = fetch_index(key)
    msg = find_message(entries, "2t")
    if msg is None:
        return None
    raw, _ = fetch_byte_range(key, msg["_offset"], msg["_length"])
    from data.pilot_extraction import decode_message_multi_point
    points = [(CENTRAL_PARK["lat"] + dlat, CENTRAL_PARK["lon"] + dlon) for dlat, dlon in PATCH_OFFSETS]
    decoded = decode_message_multi_point(raw, points, max_distance_km=MAX_PATCH_DISTANCE_KM)[4]
    info = _probe_eccodes(raw)
    info["distance_km"] = decoded["distance_km"]
    return info


PROBES = {"hrrr": probe_hrrr, "gfs": probe_gfs, "nbm": probe_nbm, "gefs": probe_gefs, "ecmwf": probe_ecmwf}


def main():
    args = block_arg_parser("grid check").parse_args()
    block = get_block(args.block)
    block_probe_time = datetime.combine(block.start, datetime.min.time(), tzinfo=timezone.utc).replace(hour=6)

    results = {}
    for source, fn in PROBES.items():
        if source == "ecmwf" and block.start < ECMWF_VALID_FROM and block.end < ECMWF_VALID_FROM:
            results[source] = {"status": "SKIPPED_STRUCTURALLY_UNAVAILABLE", "reason": f"block before {ECMWF_VALID_FROM}"}
            continue
        probe_time = block_probe_time if source != "ecmwf" or block_probe_time >= datetime.combine(ECMWF_VALID_FROM, datetime.min.time(), tzinfo=timezone.utc) else datetime.combine(ECMWF_VALID_FROM, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12)
        if source == "ecmwf":
            probe_time = probe_time.replace(hour=12) if probe_time.hour not in (0, 12) else probe_time

        try:
            ref = fn(REFERENCE_DATE_UTC.replace(hour=(6 if source != "ecmwf" else 12)))
        except Exception as e:
            ref = None
            ref_error = str(e)
        else:
            ref_error = None

        try:
            new = fn(probe_time)
        except Exception as e:
            new = None
            new_error = str(e)
        else:
            new_error = None

        if ref is None or new is None:
            results[source] = {
                "status": "PROBE_FAILED",
                "reference_probe_time": str(REFERENCE_DATE_UTC),
                "block_probe_time": str(probe_time),
                "reference_error": ref_error, "block_error": new_error,
                "reference_result": ref, "block_result": new,
            }
            continue

        fingerprint_match = (ref.get("md5GridSection") == new.get("md5GridSection")) and ref.get("md5GridSection") is not None
        dims_match = (ref.get("Ni") == new.get("Ni")) and (ref.get("Nj") == new.get("Nj"))
        distance_ok = new["distance_km"] is not None and new["distance_km"] < 25.0  # generous ceiling, well above any source's own MAX_PATCH_DISTANCE_KM

        status = "PASS" if (fingerprint_match and dims_match and distance_ok) else "FLAGGED_FOR_INVESTIGATION"
        results[source] = {
            "status": status,
            "reference_probe_time": str(REFERENCE_DATE_UTC),
            "block_probe_time": str(probe_time),
            "reference": ref, "block": new,
            "fingerprint_match": fingerprint_match, "dims_match": dims_match,
            "block_distance_km": new["distance_km"], "distance_ok": distance_ok,
        }

    overall = "PASS" if all(r["status"] in ("PASS", "SKIPPED_STRUCTURALLY_UNAVAILABLE") for r in results.values()) else "FLAGGED_FOR_INVESTIGATION"
    report = {"block_id": block.block_id, "start": str(block.start), "end": str(block.end), "overall_status": overall, "sources": results}

    out_dir = Path(__file__).resolve().parents[1] / "data/processed/backfill" / block.block_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "grid_check_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(json.dumps(report, indent=2, default=str))
    print(f"\nOVERALL: {overall}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
