"""Shared helpers for retrieving the NWS Daily Climate Report for Central
Park, NY (PIL CLINYC, product code CDUS41, issuing office KOKX / NWS New
York) via the Iowa Environmental Mesonet's (IEM) AFOS text archive -- the
SAME retrieval mechanism already validated and used in production for the
AFD product (see data/afd.py's module docstring for the full investigation
of this archive's access pattern; this module reuses it unchanged).

PRODUCT IDENTITY (CONFIRMED via live retrieval, 2026-10-04): the report
text itself explicitly states "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR
<date>..." -- this is unambiguously Central Park, NY, the project's target
location, not a different station or a CWA-wide product.

REPORT CADENCE (CONFIRMED via live probes across DST transitions and a leap
day -- 2025-01-01, 2025-03-09, 2025-11-02, 2024-02-29 -- all showed exactly
the same pattern, no exceptions found):

Exactly TWO CLINYC reports are issued per UTC calendar date:

  1. An early-morning report (~06:15-07:00 UTC, i.e. ~2 AM local), whose
     header reads "...CLIMATE SUMMARY FOR <PREVIOUS calendar day>..." with
     NO "VALID TODAY AS OF" qualifier, and whose temperature table is
     labeled "YESTERDAY". This is the COMPLETE, FINAL summary for the
     PRECEDING calendar day (that day's observations are all in by the
     time this report is issued).

  2. An afternoon report (~20:30-21:30 UTC, i.e. ~4:30 PM local), whose
     header reads "...CLIMATE SUMMARY FOR <THAT SAME calendar day>..." WITH
     an explicit "VALID TODAY AS OF <HHMM> <AM/PM> LOCAL TIME." qualifier,
     and whose temperature table is labeled "TODAY". This is an
     INCOMPLETE, PRELIMINARY intraday snapshot -- the calendar day is not
     yet over, so its MAXIMUM figure is NOT guaranteed to be the eventual
     final daily max (the afternoon/evening could still produce a higher
     reading after the report's cutoff time).

SELECTION RULE (the critical correctness property this module enforces):
for a target calendar day D, the FINAL realized daily Tmax comes ONLY from
the report whose header date is D AND which lacks the "VALID TODAY AS OF"
qualifier (i.e. the early-morning report for D, which is issued and dated
the FOLLOWING calendar day, D+1). The afternoon "TODAY"-labeled report for
D must NEVER be used as D's final label, even though it is chronologically
the LATER-issued report for calendar date D's own UTC listing -- its content
describes an intraday, not-yet-final subset of day D. This directly
implements the user's explicit instruction: "never accidentally use an
afternoon preliminary report as the final daily label if a later finalized
report exists" (here, "later" means issued the next morning, not later
within the same UTC listing date).

No CORRECTED/AMENDED CLINYC variants were observed in this project's
sampling; if one is encountered it will show as a 3rd product_id on some
date and is NOT silently handled by this module -- callers must treat an
unexpected product count per day as a condition requiring investigation,
not an automatic pass-through.

ARCHIVE ACCESS (identical mechanism to data/afd.py):
  - List products for PIL + calendar date (HTML, scraped for product_id links):
    https://mesonet.agron.iastate.edu/wx/afos/list.phtml?by=pil&pil=CLINYC&year=Y&month=M&day=D
  - Raw text for one product (JSON-wrapped plain text):
    https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}

product_id format (OBSERVED, identical to AFD's): {YYYYMMDDHHMM}-{CCCC}-{WMOID}-{PIL},
e.g. "202506150618-KOKX-CDUS41-CLINYC". The leading 12-digit timestamp is
UTC and is used here as ISSUANCE_TIME, exactly as for AFD.
"""
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = "https://mesonet.agron.iastate.edu"
OFFICE = "OKX"
ISSUING_OFFICE = "KOKX"  # full 4-letter office identifier, as it appears in product_id/WMO heading
PIL = "CLINYC"


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def list_product_ids(year: int, month: int, day: int, stats: RequestStats | None = None) -> list[dict]:
    """List every CLINYC product_id issued for a UTC calendar date. Identical
    mechanism to data/afd.py's list_product_ids (same IEM endpoint)."""
    t0 = time.time()
    r = requests.get(
        f"{BASE_URL}/wx/afos/list.phtml",
        params={"by": "pil", "pil": PIL, "year": year, "month": month, "day": day},
        timeout=30,
    )
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    r.raise_for_status()
    product_ids = sorted(set(re.findall(r"p\.php\?pid=([^\"&]+)", r.text)))
    return [{"product_id": pid, "issuance_time": parse_issuance_from_product_id(pid)} for pid in product_ids]


def parse_issuance_from_product_id(product_id: str) -> datetime:
    ts_str = product_id.split("-")[0]
    return datetime.strptime(ts_str, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def fetch_raw_text(product_id: str, stats: RequestStats | None = None) -> str:
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/api/1/nwstext/{product_id}", timeout=30)
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    r.raise_for_status()
    return r.text


# --- Parsing -----------------------------------------------------------

HEADER_DATE_RE = re.compile(r"THE CENTRAL PARK NY CLIMATE SUMMARY FOR\s+([A-Z]+ \d{1,2} \d{4})\.\.\.", re.IGNORECASE)
PRELIM_MARKER_RE = re.compile(r"VALID TODAY AS OF\s+(\d{3,4})\s*(AM|PM)\s+LOCAL TIME", re.IGNORECASE)
# Matches the MAXIMUM row immediately following the YESTERDAY/TODAY label
# under the TEMPERATURE (F) section header. "MM" indicates missing data
# (NWS's own documented missing-value code, never silently treated as 0).
# The numeric value may carry a trailing single-letter flag -- CONFIRMED
# live (2026-10-04, June 2025 heatwave reports): "R" means "RECORD WAS SET
# OR TIED" per the report's own footer legend. The flag is captured
# separately and never silently dropped or treated as part of the number.
MAX_TEMP_RE = re.compile(
    r"TEMPERATURE \(F\)\s*\n\s*(?:YESTERDAY|TODAY)\s*\n\s*MAXIMUM\s+(-?\d+|MM)([A-Z]?)\s+(\d{1,4}\s*(?:AM|PM)|MM)",
    re.IGNORECASE,
)
MIN_TEMP_RE = re.compile(
    r"MINIMUM\s+(-?\d+|MM)([A-Z]?)\s+(\d{1,4}\s*(?:AM|PM)|MM)",
    re.IGNORECASE,
)
PARSER_VERSION = "clinyc_parser_v1_2026-10-04"

MONTH_NAMES = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


class ClinycParseError(ValueError):
    pass


def parse_report(raw_text: str, product_id: str) -> dict:
    """Parses one raw CLINYC report. Returns a dict with target_date,
    is_preliminary, max_temp_f, max_temp_time_lst, min_temp_f,
    min_temp_time_lst, issuance_time, product_id, parser_version. Raises
    ClinycParseError (never silently returns a guessed/partial result) if
    the expected header or temperature structure is not found -- a
    malformed report must be visible as a failure, not a fabricated value."""
    header_match = HEADER_DATE_RE.search(raw_text)
    if not header_match:
        raise ClinycParseError(f"{product_id}: could not find 'CLIMATE SUMMARY FOR <date>' header")
    date_str = header_match.group(1).upper()
    month_name, day_str, year_str = date_str.split()
    if month_name not in MONTH_NAMES:
        raise ClinycParseError(f"{product_id}: unrecognized month name {month_name!r}")
    target_date = datetime(int(year_str), MONTH_NAMES[month_name], int(day_str)).date()

    is_preliminary = bool(PRELIM_MARKER_RE.search(raw_text))

    max_match = MAX_TEMP_RE.search(raw_text)
    if not max_match:
        raise ClinycParseError(f"{product_id}: could not find MAXIMUM temperature row")
    max_val_str, max_flag, max_time_str = max_match.groups()
    if max_val_str.upper() == "MM":
        max_temp_f = None
    else:
        max_temp_f = float(max_val_str)
        if not (-40.0 <= max_temp_f <= 120.0):
            raise ClinycParseError(f"{product_id}: implausible MAXIMUM temperature {max_temp_f}F")

    min_match = MIN_TEMP_RE.search(raw_text, pos=max_match.end())
    min_temp_f = None
    min_time_str = None
    min_flag = ""
    if min_match:
        min_val_str, min_flag, min_time_str = min_match.groups()
        if min_val_str.upper() != "MM":
            min_temp_f = float(min_val_str)

    return {
        "product_id": product_id,
        "issuance_time": parse_issuance_from_product_id(product_id),
        "target_date": target_date,
        "is_preliminary": is_preliminary,
        "max_temp_f": max_temp_f,
        "max_temp_time_lst": max_time_str.strip() if max_val_str.upper() != "MM" else None,
        "max_temp_flag": max_flag or None,
        "min_temp_f": min_temp_f,
        "min_temp_time_lst": min_time_str.strip() if min_time_str else None,
        "min_temp_flag": min_flag or None,
        "parser_version": PARSER_VERSION,
        "raw_text": raw_text,
    }


# --- Canonical label selection -----------------------------------------
#
# This is the production version of the selection rule validated during the
# Phase 1 overlap investigation (2026-10-04) and approved by the user as the
# canonical historical supervised-label rule: y_d = finalized NWS CLINYC
# Central Park daily Tmax, with NO bias-correction toward ISD and NO silent
# fallback (to ISD, a preliminary report, another station, interpolation, or
# a model estimate) when a final report genuinely does not exist.

CANONICAL_LABEL_SOURCE = "NWS_CLINYC_CENTRAL_PARK"
LABEL_UNAVAILABLE_STATUS = "LABEL_UNAVAILABLE_CLINYC"


class ClinycSelectionError(ValueError):
    """Raised when the final-report selection for a target date is genuinely
    ambiguous or would require look-ahead -- never silently resolved."""


def select_canonical_label(target_date: date, candidates: list[dict]) -> dict:
    """Selects the canonical daily Tmax label for one target_date from all
    parsed CLINYC reports whose target_date equals it (candidates may mix
    preliminary and final reports; this function performs the selection).

    Rule (validated Phase 1, 230/236 clean days + 3 correctly-resolved
    multi-final days + 3 genuine gaps in the 2025H1 overlap window):
      1. Only FINAL reports (is_preliminary == False) are eligible. The
         afternoon preliminary "TODAY" report is NEVER used as the final
         label, even though nothing else exists for that date.
      2. If zero finals exist: canonical_label_status = LABEL_UNAVAILABLE_CLINYC,
         canonical_tmax_label_f = None. This is a real, isolated archive gap
         (e.g. 2025-06-02/03/18, 2025-11-13 are known instances) -- never
         filled from ISD, a preliminary report, another station,
         interpolation, or a model estimate.
      3. If multiple finals exist (benign re-issuance or an explicit NWS
         correction, e.g. a "-CCA" suffixed product_id), the LATEST-ISSUED
         final is authoritative -- this is standard NWS convention and
         correctly handles both identical-value re-transmissions and genuine
         corrections (e.g. an original "MM" later filled in).
      4. Fails closed (raises ClinycSelectionError) rather than guessing when:
         - the latest final's value is missing ("MM") while an EARLIER final
           for the same date has a real value (a correction should never
           retract a known value to missing -- this pattern was never
           observed and is treated as a regime anomaly requiring investigation,
           not a silent pick);
         - two or more finals share the exact same (maximal) issuance_time
           but report different values (a genuine, irreducible tie);
         - the selected final's issuance_time falls on or before target_date
           in UTC terms (a no-lookahead safeguard: the validated report
           cadence always issues a day's final on the FOLLOWING UTC calendar
           date; an final issued on-or-before its own target date would mean
           this function is at risk of leaking same-day information into a
           label that must only be used as a post-hoc outcome).
    """
    finals = [c for c in candidates if c["target_date"] == target_date and not c["is_preliminary"]]
    prelims = [c for c in candidates if c["target_date"] == target_date and c["is_preliminary"]]

    base = {
        "target_date": target_date,
        "issuing_office": ISSUING_OFFICE,
        "n_final_candidates": len(finals),
        "n_preliminary_rejected": len(prelims),
        "parser_version": PARSER_VERSION,
    }

    if not finals:
        return {
            **base,
            "clinyc_official_tmax_f": None,
            "canonical_tmax_label_f": None,
            "canonical_label_source": "LABEL_UNAVAILABLE",
            "canonical_label_status": LABEL_UNAVAILABLE_STATUS,
            "issuance_time": None,
            "product_id": None,
            "report_status": None,
            "max_temp_flag": None,
            "selection_notes": f"MISSING_FINAL: 0 final reports, {len(prelims)} preliminary (rejected)",
        }

    max_issuance = max(c["issuance_time"] for c in finals)
    tied = [c for c in finals if c["issuance_time"] == max_issuance]
    tied_values = {c["max_temp_f"] for c in tied}
    if len(tied_values) > 1:
        sorted_tied_values = sorted(tied_values, key=lambda v: (v is None, v))
        raise ClinycSelectionError(
            f"{target_date}: {len(tied)} final reports share the latest issuance_time "
            f"{max_issuance} but report differing values {sorted_tied_values} -- "
            "irreducible tie, refusing to guess"
        )
    latest = tied[0]

    if latest["max_temp_f"] is None:
        earlier_with_value = [c for c in finals if c is not latest and c["max_temp_f"] is not None]
        if earlier_with_value:
            raise ClinycSelectionError(
                f"{target_date}: latest final ({latest['product_id']}) reports MM (missing) but an "
                f"earlier final ({earlier_with_value[0]['product_id']}) had a real value -- regime "
                "anomaly, refusing to silently pick either"
            )

    if latest["issuance_time"].date() <= target_date:
        raise ClinycSelectionError(
            f"{target_date}: selected final ({latest['product_id']}) was issued at "
            f"{latest['issuance_time']} (on or before its own target date) -- no-lookahead safeguard "
            "triggered, refusing to use a same-day-or-earlier 'final' as the label"
        )

    values = {c["max_temp_f"] for c in finals}
    if len(finals) == 1:
        notes = f"OK: 1 final, {len(prelims)} preliminary (rejected)"
        report_status = "FINAL"
    elif len(values) == 1:
        notes = f"MULTI_FINAL_IDENTICAL: {len(finals)} final reports, same value, selected latest-issued ({latest['product_id']})"
        report_status = "FINAL_REISSUED"
    else:
        sorted_values = sorted(values, key=lambda v: (v is None, v))
        notes = f"MULTI_FINAL_DIFFERING: {len(finals)} final reports, values differ {sorted_values}, selected latest-issued ({latest['product_id']})"
        report_status = "FINAL_CORRECTED"

    return {
        **base,
        "clinyc_official_tmax_f": latest["max_temp_f"],
        "canonical_tmax_label_f": latest["max_temp_f"],
        "canonical_label_source": CANONICAL_LABEL_SOURCE,
        "canonical_label_status": "OK",
        "issuance_time": latest["issuance_time"],
        "product_id": latest["product_id"],
        "report_status": report_status,
        "max_temp_flag": latest["max_temp_flag"],
        "selection_notes": notes,
    }


def build_canonical_labels(dates: list[date], raw_dir: Path | None = None, stats: RequestStats | None = None) -> tuple[list[dict], list[str]]:
    """Fetches, parses, and selects the canonical CLINYC label for every date
    in `dates`. Automatically also fetches the following day (margin day)
    for each date's final-report lookup, since a day D's final is issued on
    D+1. If raw_dir is given, raw report text is cached there (one .txt file
    per product_id) and reused on subsequent calls -- never re-fetched once
    cached, matching the audit-preservation requirement (raw reports must
    survive for later re-inspection, not just the parsed/derived values).

    Returns (labels, parse_errors): one label dict per input date (in
    sorted order), and the list of any raw-report parse failures encountered
    while building them (a malformed report is never silently dropped).
    """
    if not dates:
        return [], []
    all_dates = sorted(set(dates) | {d + timedelta(days=1) for d in dates})

    all_products: dict[str, dict] = {}
    for d in all_dates:
        for p in list_product_ids(d.year, d.month, d.day, stats=stats):
            all_products[p["product_id"]] = p

    parsed: dict[str, dict] = {}
    parse_errors: list[str] = []
    for pid in sorted(all_products):
        try:
            if raw_dir is not None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = raw_dir / f"{pid}.txt"
                if raw_path.exists():
                    raw_text = raw_path.read_text(encoding="utf-8")
                else:
                    raw_text = fetch_raw_text(pid, stats=stats)
                    raw_path.write_text(raw_text, encoding="utf-8")
            else:
                raw_text = fetch_raw_text(pid, stats=stats)
            parsed[pid] = parse_report(raw_text, pid)
        except ClinycParseError as e:
            parse_errors.append(str(e))

    by_target_date: dict[date, list[dict]] = {}
    for r in parsed.values():
        by_target_date.setdefault(r["target_date"], []).append(r)

    results = []
    for d in sorted(set(dates)):
        candidates = by_target_date.get(d, [])
        try:
            label = select_canonical_label(d, candidates)
            label["selection_error"] = None
        except ClinycSelectionError as e:
            label = {
                "target_date": d, "issuing_office": ISSUING_OFFICE,
                "clinyc_official_tmax_f": None, "canonical_tmax_label_f": None,
                "canonical_label_source": "LABEL_UNAVAILABLE", "canonical_label_status": LABEL_UNAVAILABLE_STATUS,
                "issuance_time": None, "product_id": None, "report_status": None,
                "max_temp_flag": None, "n_final_candidates": len(candidates), "n_preliminary_rejected": 0,
                "parser_version": PARSER_VERSION, "selection_notes": "SELECTION_ERROR", "selection_error": str(e),
            }
        results.append(label)
    return results, parse_errors
