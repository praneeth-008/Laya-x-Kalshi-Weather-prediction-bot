"""Shared helpers for retrieving NWS Area Forecast Discussion (AFD) text
for New York City via the Iowa Environmental Mesonet's (IEM) AFOS text
archive.

WFO FOR NYC (DOCUMENTED, verified live via https://www.weather.gov/okx/):
National Weather Service New York, NY -- WFO identifier OKX, physically
located in Upton, NY. Its County Warning Area (CWA) covers northeast New
Jersey, southern Connecticut, Long Island, and southeast New York,
including the NYC metro. KNYC (Central Park), KLGA (LaGuardia), KJFK
(JFK), and KEWR (Newark) are ALL within this single CWA -- one office, one
AFD product covers all four; this test does NOT infer which of these is
the eventual Kalshi settlement station.

PRODUCT: AFD (Area Forecast Discussion), PIL "AFDOKX" (issuing center code
"KOKX" + product code "AFD"), WMO collective ID "FXUS61".

ARCHIVE SOURCE -- this is explicitly a THIRD-PARTY archive, not run by
NOAA/NWS itself: Iowa Environmental Mesonet (IEM), mesonet.agron.iastate.edu.
It is used here because NOAA's own official real-time API
(api.weather.gov) was tested live in this session and found to NOT
support historical date-range queries -- a query for AFDOKX between
2025-06-30 and 2025-07-02 returned zero results even though the endpoint
itself works for current/recent products, mirroring the same short-
retention pattern already seen for aviationweather.gov's METAR API
elsewhere in this project. IEM is the standard, widely-used archive for
this exact purpose and was verified live to have deep, working historical
coverage, satisfying the task's explicit "IEM/other archives only if
necessary and clearly identified as third-party" allowance.

ACCESS MECHANISM (OBSERVED, hybrid of two separate IEM endpoints -- their
JSON list API does NOT accept year/month/day date-range params in this
session, so the HTML listing page is used for date-scoped discovery, and
the clean JSON text API is used for the actual raw text):
  - List products for one PIL + calendar date (HTML, scraped for
    product_id links):
    https://mesonet.agron.iastate.edu/wx/afos/list.phtml?by=pil&pil={PIL}&year=Y&month=M&day=D
  - Raw text for one product (JSON, exact unmodified text):
    https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}

product_id format (OBSERVED): {YYYYMMDDHHMM}-{CCCC}-{WMOID}-{PIL}, e.g.
"202507010009-KOKX-FXUS61-AFDOKX". The leading 12-digit timestamp is UTC
and matches the product's own WMO header timestamp -- used here as
ISSUANCE_TIME (DOCUMENTED: this is the product's self-declared issuance
time, per the WMO abbreviated header line inside the raw text itself,
cross-checked against the product_id's own embedded timestamp).

AVAILABILITY_TIME: IEM's JSON list endpoint exposes an "entered" field
(when IEM's own system logged the product) for CURRENT/recent listings --
a plausible OBSERVED proxy for real-world availability, analogous to the
S3 Last-Modified proxy used for the GRIB sources elsewhere in this
project. This project did NOT verify that field is exposed identically
for historical (2025-07-01) listings via the HTML archive page (it isn't
directly visible there) -- so available_time is reported as UNKNOWN for
historical AFDs unless proven otherwise, never fabricated.

Historical depth (OBSERVED via direct probes against July 1 of several
years): AFDOKX products found for 1999, 2000, 2005, ...2025; NONE found
for 1997 or 1998 -- consistent with OKX becoming an operational WFO
identifier around the NWS modernization era. Archive depth is therefore
roughly 26+ years for this specific office/product, not indefinite, and
not verified further back than this test's probes.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://mesonet.agron.iastate.edu"
NYC_TZ = ZoneInfo("America/New_York")

OFFICE = "OKX"
PIL = "AFDOKX"


@dataclass
class RequestStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    seconds_elapsed: float = 0.0

    def record(self, n_bytes: int, seconds: float) -> None:
        self.n_requests += 1
        self.bytes_downloaded += n_bytes
        self.seconds_elapsed += seconds


def list_product_ids(pil: str, year: int, month: int, day: int, stats: RequestStats | None = None) -> list[dict]:
    """List every product_id issued for a PIL on a given calendar date
    (UTC calendar date, matching the product_id's own embedded date)."""
    t0 = time.time()
    r = requests.get(
        f"{BASE_URL}/wx/afos/list.phtml",
        params={"by": "pil", "pil": pil, "year": year, "month": month, "day": day},
        timeout=30,
    )
    elapsed = time.time() - t0
    if stats:
        stats.record(len(r.content), elapsed)
    r.raise_for_status()
    product_ids = sorted(set(re.findall(r"p\.php\?pid=([^\"&]+)", r.text)))
    results = []
    for pid in product_ids:
        issuance_dt = parse_issuance_from_product_id(pid)
        results.append({"product_id": pid, "issuance_time": issuance_dt})
    return results


def parse_issuance_from_product_id(product_id: str) -> datetime:
    """product_id format: {YYYYMMDDHHMM}-{CCCC}-{WMOID}-{PIL}"""
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


# Section headers observed in real AFDOKX text follow the pattern
# ".SECTION NAME..." on their own line (a leading period, uppercase words,
# then a run of periods). This regex is used to SPLIT the raw text into
# sections for the "section-parsed" table -- it does not interpret content.
SECTION_HEADER_RE = re.compile(r"^\.([A-Z][A-Z0-9 /]+?)\.\.\.\s*$", re.MULTILINE)


def parse_sections(raw_text: str) -> list[dict]:
    """Split raw AFD text into (section_name, section_text) pairs using
    the product's own ".SECTION NAME..." headers. Text before the first
    header (the WMO/AWIPS header block and product title) is kept as a
    'HEADER' pseudo-section so nothing is discarded."""
    matches = list(SECTION_HEADER_RE.finditer(raw_text))
    sections = []
    if not matches:
        return [{"section_name": "HEADER", "section_text": raw_text}]
    if matches[0].start() > 0:
        sections.append({"section_name": "HEADER", "section_text": raw_text[: matches[0].start()]})
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        sections.append({"section_name": name, "section_text": raw_text[start:end].strip()})
    return sections


def local_day_utc_bounds(local_date, tz: ZoneInfo = NYC_TZ) -> tuple[datetime, datetime]:
    start_local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
