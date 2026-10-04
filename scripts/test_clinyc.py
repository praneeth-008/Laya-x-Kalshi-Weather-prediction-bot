"""Unit tests for the CLINYC canonical-label pipeline (data/clinyc.py):
target-date association, preliminary/final selection, timezone/date
boundaries, missing reports, duplicate/corrected reports, malformed
reports, impossible temperatures, no-future-label-leakage, and provenance
preservation. Follows the same ad-hoc check() pattern as
scripts/test_integration.py.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.clinyc import (
    parse_report, select_canonical_label, build_canonical_labels,
    ClinycParseError, ClinycSelectionError, LABEL_UNAVAILABLE_STATUS, CANONICAL_LABEL_SOURCE,
)

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


def make_report(header_date_str, is_prelim=False, max_val="80", max_flag="", min_val="60", min_flag=""):
    prelim_line = "VALID TODAY AS OF 430 PM LOCAL TIME." if is_prelim else ""
    label = "TODAY" if is_prelim else "YESTERDAY"
    return f"""391
CDUS41 KOKX 010656
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR {header_date_str}...
{prelim_line}

TEMPERATURE (F)
 {label}
  MAXIMUM         {max_val}{max_flag}    216 PM  63  1965  41  12  43
  MINIMUM         {min_val}{min_flag}    947 PM  -7  1917  30  14  39

$$
"""


# ---- 1. target-date association (header date, independent of issuance date) ----
pid_final_jun15 = "202506160656-KOKX-CDUS41-CLINYC"  # issued next morning for JUNE 15
r = parse_report(make_report("JUNE 15 2025"), pid_final_jun15)
check("target_date parsed from header, not product_id date", r["target_date"] == date(2025, 6, 15), r["target_date"])
check("issuance_time parsed from product_id", r["issuance_time"] == datetime(2025, 6, 16, 6, 56, tzinfo=timezone.utc))
check("final report is_preliminary == False", r["is_preliminary"] is False)

# ---- 2. preliminary/final selection ----
pid_prelim_jun15 = "202506152130-KOKX-CDUS41-CLINYC"
prelim = parse_report(make_report("JUNE 15 2025", is_prelim=True, max_val="78"), pid_prelim_jun15)
final = parse_report(make_report("JUNE 15 2025", max_val="80"), pid_final_jun15)
sel = select_canonical_label(date(2025, 6, 15), [prelim, final])
check("final preferred over preliminary", sel["canonical_tmax_label_f"] == 80.0, sel)
check("preliminary-only never used as final label", True)  # covered by case below
sel_prelim_only = select_canonical_label(date(2025, 6, 15), [prelim])
check("preliminary-only day is LABEL_UNAVAILABLE (never substituted)", sel_prelim_only["canonical_label_status"] == LABEL_UNAVAILABLE_STATUS)

# ---- 3. timezone/date boundaries (year + month boundary: Dec 31 -> Jan 1 final) ----
pid_newyear = "202501010656-KOKX-CDUS41-CLINYC"
r_ny = parse_report(make_report("DECEMBER 31 2024"), pid_newyear)
check("year-boundary header date parses correctly", r_ny["target_date"] == date(2024, 12, 31))
check("year-boundary issuance_time is Jan 1 (next day)", r_ny["issuance_time"].date() == date(2025, 1, 1))

# ---- 4. missing reports ----
sel_missing = select_canonical_label(date(2025, 6, 18), [])
check("zero candidates -> LABEL_UNAVAILABLE_CLINYC", sel_missing["canonical_label_status"] == LABEL_UNAVAILABLE_STATUS)
check("missing day has no canonical value", sel_missing["canonical_tmax_label_f"] is None)
check("missing day never falls back to any source", sel_missing["canonical_label_source"] == "LABEL_UNAVAILABLE")

# ---- 5. duplicate reports (identical-value reissue) ----
pid_final_a = "202506160656-KOKX-CDUS41-CLINYC"
pid_final_b = "202506160900-KOKX-CDUS41-CLINYC"  # later reissue, same value
final_a = parse_report(make_report("JUNE 15 2025", max_val="80"), pid_final_a)
final_b = parse_report(make_report("JUNE 15 2025", max_val="80"), pid_final_b)
sel_dup = select_canonical_label(date(2025, 6, 15), [final_a, final_b])
check("duplicate identical finals resolved via latest issuance", sel_dup["product_id"] == pid_final_b)
check("duplicate identical finals -> MULTI_FINAL_IDENTICAL note", "MULTI_FINAL_IDENTICAL" in sel_dup["selection_notes"])
check("duplicate identical finals -> status still OK", sel_dup["canonical_label_status"] == "OK")

# ---- corrected report (differing value, e.g. MM filled in later) ----
pid_corr = "202506161200-KOKX-CDUS41-CLINYC"
final_corrected = parse_report(make_report("JUNE 15 2025", max_val="82"), pid_corr)
sel_corr = select_canonical_label(date(2025, 6, 15), [final_a, final_corrected])
check("correction (differing value) -> latest-issued wins", sel_corr["canonical_tmax_label_f"] == 82.0)
check("correction -> MULTI_FINAL_DIFFERING note", "MULTI_FINAL_DIFFERING" in sel_corr["selection_notes"])

# ---- 6. malformed reports ----
try:
    parse_report("no header or temperature table here", "badpid")
    check("malformed report (no header) raises ClinycParseError", False, "did not raise")
except ClinycParseError:
    check("malformed report (no header) raises ClinycParseError", True)

bad_text = """...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JUNE 15 2025...
TEMPERATURE (F)
 YESTERDAY
  (no maximum row here)
"""
try:
    parse_report(bad_text, "badpid2")
    check("malformed report (no MAXIMUM row) raises ClinycParseError", False, "did not raise")
except ClinycParseError:
    check("malformed report (no MAXIMUM row) raises ClinycParseError", True)

# ---- 7. impossible temperatures ----
try:
    parse_report(make_report("JUNE 15 2025", max_val="999"), "badpid3")
    check("implausible MAXIMUM temperature raises ClinycParseError", False, "did not raise")
except ClinycParseError:
    check("implausible MAXIMUM temperature raises ClinycParseError", True)

# ---- MM (missing) handled, not fabricated as 0 ----
r_mm = parse_report(make_report("JUNE 15 2025", max_val="MM"), "202506160656-KOKX-CDUS41-CLINYC")
check("MM maximum parses to None, not 0", r_mm["max_temp_f"] is None)

# ---- R record-flag parsed separately from numeric value ----
r_flag = parse_report(make_report("JUNE 15 2025", max_val="96", max_flag="R"), "202506160656-KOKX-CDUS41-CLINYC")
check("R record flag captured separately", r_flag["max_temp_f"] == 96.0 and r_flag["max_temp_flag"] == "R", r_flag)

# ---- 8. no future-label leakage ----
# A "final" whose issuance_time is on/before its own target_date must never be used.
pid_same_day = "202506150900-KOKX-CDUS41-CLINYC"  # issued same day as target -- should never happen for a real final
bad_final = parse_report(make_report("JUNE 15 2025", max_val="80"), pid_same_day)
try:
    select_canonical_label(date(2025, 6, 15), [bad_final])
    check("same-day-issued 'final' triggers no-lookahead safeguard", False, "did not raise")
except ClinycSelectionError:
    check("same-day-issued 'final' triggers no-lookahead safeguard", True)

# ---- irreducible tie (same issuance_time, differing values) ----
tie_a = dict(final_a)
tie_b = dict(final_corrected)
tie_b["issuance_time"] = tie_a["issuance_time"]  # force a tie
try:
    select_canonical_label(date(2025, 6, 15), [tie_a, tie_b])
    check("irreducible tie (same issuance, differing values) raises", False, "did not raise")
except ClinycSelectionError:
    check("irreducible tie (same issuance, differing values) raises", True)

# ---- latest final MM while earlier final has a value -> regime anomaly ----
mm_latest = parse_report(make_report("JUNE 15 2025", max_val="MM"), pid_final_b)
try:
    select_canonical_label(date(2025, 6, 15), [final_a, mm_latest])
    check("latest-final-MM-while-earlier-has-value raises", False, "did not raise")
except ClinycSelectionError:
    check("latest-final-MM-while-earlier-has-value raises", True)

# ---- 9. provenance preservation ----
check("provenance: product_id preserved", sel["product_id"] == pid_final_jun15)
check("provenance: issuance_time preserved", sel["issuance_time"] is not None)
check("provenance: parser_version preserved", "parser_version" in sel and sel["parser_version"])
check("provenance: issuing_office preserved", sel["issuing_office"] == "KOKX")
check("provenance: canonical_label_source is the documented constant", sel["canonical_label_source"] == CANONICAL_LABEL_SOURCE)

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
if results["failed"]:
    print("FAILURES:")
    for f in results["failed"]:
        print(" -", f)
    sys.exit(1)
