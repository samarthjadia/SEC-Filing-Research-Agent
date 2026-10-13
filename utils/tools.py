"""Tools the agent can call to answer questions about any SEC-registered company.

Public functions (no leading underscore) are the tools: agents/mcp_server.py
exposes them over MCP, and their docstrings + type hints become the tool
descriptions the model reads. Private helpers (leading _) are internal.

Data comes live from SEC EDGAR via utils/edgar.py (cached on disk), not
from local files. Only 10-K and 10-Q filings are used for text; earnings
press releases (8-K exhibits) are intentionally left out for now.

Typical agent flow:
  1. get_company_filings      — which 10-K / 10-Q filings exist, with fiscal periods
  2. search_company_report    — meaning search inside one filing's text
  3. list_company_concepts    — which tagged line items exist
  4. get_company_financials   — exact numbers for one line item

The facts JSON is not a spreadsheet. Each line item (revenue, profit, …)
has a machine name, an English label, and a list of numbers over time.
"""

from __future__ import annotations

from datetime import date
import re

from utils.edgar import (
    EdgarError,
    filing_url,
    get_company_facts,
    get_filing_text_path,
    get_submissions,
    normalize_ticker,
    ticker_to_cik,
)
from utils.vectors import VectorSearchError, search_filing_text


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Only these forms are offered to the model. Amendments (10-K/A) are skipped:
# they are often partial and would confuse "latest quarter" logic.
SUPPORTED_FORMS = {"10-K", "10-Q"}

# What each document type is good for. Shown to the model next to every filing.
DOC_TYPE_USES = {
    "10-Q": "Quarterly SEC filing: full financial statements, segment detail, MD&A (why results changed), liquidity, risk updates.",
    "10-K": "Annual SEC filing: full-year and Q4 results, business overview, complete risk factors, segment detail, MD&A.",
}

# Pattern that checks whether a string looks like an SEC accession number
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")  # e.g. 0001045810-26-000075

# List of month names used to turn the SEC's numeric fiscal-year-end into a sentence the model can read.
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

DEFAULT_FACT_LIMIT = 8     # how many past periods to return for one line item
DEFAULT_FILING_LIMIT = 12  # ≈ 3 years of 10-Q + 10-K


# ===========================================================================
# Private helpers — used by the tools below, not by the LLM directly
# ===========================================================================

# Takes a ticker string and returns two values in a tuple: 
# 1. the cleaned ticker 
# 2. SEC’s numeric company id for it.
# *** All tools call this to resolve the ticker to a symbol and cik first
def normalize_accession(value: str) -> str:
    """Write an SEC accession as 10 digits, a hyphen, 2 digits, a hyphen, 6 digits.

    The filings list returns "0001045810-26-000075". Models often drop the
    hyphens and send "000104581026000075", which is the same 18 digits. Insert
    the hyphens before the lookup so that first call succeeds.
    """
    digits = re.sub(r"\D", "", value.strip())
    if len(digits) == 18:
        return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
    return value.strip()


def _resolve(ticker: str) -> tuple[str, int]:
    """'amd' → ('AMD', 2488). Raises EdgarError for unknown tickers."""
    symbol = normalize_ticker(ticker)
    return symbol, ticker_to_cik(symbol)



# get_submissions(cik) returns a big dictionary. Inside it, recent filings look like this, with one list per field:
# submissions["filings"]["recent"] = {
#     "form":             ["10-Q", "10-K", "8-K"],
#     "accessionNumber":  ["0000002488-26-000123", "0000002488-26-000045", "..."],
#     "filingDate":       ["2026-08-05", "2026-02-04", "..."],
#     "reportDate":       ["2026-06-30", "2025-12-31", "..."],
#     "primaryDocument":  ["amd-20260630.htm", "amd-20251231.htm", "..."],
# }
#
# This function returns a list of dictionaries, one per filing, with those five fields joined back together.
# ex:
# [
#     {
#         "form": "10-Q",
#         "accessionNumber": "0000002488-26-000123",
#         "filingDate": "2026-08-05",
#         "reportDate": "2026-06-30",
#         "primaryDocument": "amd-20260630.htm",
#     },
#     {
#         "form": "10-K",
#         "accessionNumber": "0000002488-26-000045",
#         ...
#     },
# ]
def _recent_filings(submissions: dict) -> list[dict]:
    """Turn the submissions API's columnar table into one dict per filing.

    EDGAR sends {"form": [...], "accessionNumber": [...], ...} — parallel
    lists where index i across every list describes filing i. zip() stitches
    them back into rows. "recent" holds at least the last year or last 1,000
    filings, which is plenty for 10-K/10-Q questions.
    """
    recent = (submissions.get("filings") or {}).get("recent") or {}
    columns = ("form", "accessionNumber", "filingDate", "reportDate", "primaryDocument")
    return [dict(zip(columns, values)) for values in zip(*(recent.get(c, []) for c in columns))]

# It takes facts, the XBRL numbers from get_company_facts. 
# It returns a dictionary. 
# The key is an accession number. 
# The value is a pair: the fiscal year as an integer, and the period code as a string ("Q1", "Q2", "Q3", or "FY" for the annual filing).
# ex:
# {
#     "0000002488-26-000123": (2026, "Q2"),
#     "0000002488-26-000045": (2025, "FY"),
#     ...
# }
def _fiscal_periods_by_accession(facts: dict) -> dict[str, tuple[int, str]]:
    """Map each filing's accession number to the fiscal period the company itself tagged.

    Simply: every number in a 10-Q is stamped with "this filing is FY2026 Q2".
    We read that stamp, so we never have to guess fiscal calendars from dates.

    Technically: in companyfacts, each fact row carries accn (which filing it
    came from) plus fy/fp, which are that *filing's* DocumentFiscalYearFocus /
    DocumentFiscalPeriodFocus — the same for every row from one filing. So the
    first row we see per accn gives us the label.
    """
    periods: dict[str, tuple[int, str]] = {}
    for _tax, _name, payload in _iter_concepts(facts):
        for rows in (payload.get("units") or {}).values():
            for row in rows:
                accn = row.get("accn")
                if accn and accn not in periods and row.get("fy") and row.get("fp"):
                    periods[accn] = (int(row["fy"]), str(row["fp"]))
    return periods


def _format_fiscal_period(fy: int, fp: str) -> str:
    """(2026, 'Q2') → 'FY2026 Q2'; (2025, 'FY') → 'FY2025 Q4' (a 10-K covers Q4 and the full year)."""
    return f"FY{fy} Q4" if fp == "FY" else f"FY{fy} {fp}"


def _fiscal_period_for_date(period_end: date, fiscal_year_end_month: int) -> str:
    """Fallback when a filing has no XBRL tags: estimate "FY2027 Q1" from the period-end date.

    Fiscal years are named for the calendar year they end in. With a January
    year end (NVDA), April 2026 falls in the year ending Jan 2027 -> FY2027,
    and it is the 1st quarter after the Jan year end -> Q1.
    """
    fiscal_year = period_end.year + (1 if period_end.month > fiscal_year_end_month else 0)
    months_into_year = (period_end.month - fiscal_year_end_month - 1) % 12  # 0..11
    return f"FY{fiscal_year} Q{months_into_year // 3 + 1}"


def _fiscal_year_note(fiscal_year_end: str | None) -> str | None:
    """'0125' → a sentence the model can use to map calendar dates to fiscal periods."""
    if not fiscal_year_end or len(fiscal_year_end) != 4:
        return None
    month, day = int(fiscal_year_end[:2]), int(fiscal_year_end[2:])
    note = f"Fiscal year ends around {_MONTHS[month - 1]} {day}."
    if month == 12:
        return note + " Fiscal quarters line up with calendar quarters."
    return note + (
        " Fiscal quarters do NOT match calendar quarters; use each filing's fiscal_period"
        " (taken from the company's own tags) instead of converting dates yourself."
    )


def _iter_concepts(facts: dict, taxonomy: str | None = None):
    """Walk every line item in the facts JSON.

    The JSON is nested like:
        facts["us-gaap"]["NetIncomeLoss"] = {label, units: {USD: [rows...]}}

    - taxonomy  = which official name-list the item came from
                  ("us-gaap" = standard accounting terms;
                   "dei" = company/document info)
    - concept   = machine name of the line item, e.g. NetIncomeLoss
    - payload   = English label, description, and the actual numbers

    If taxonomy is set (e.g. "us-gaap"), skip the other name-lists.
    """
    for tax_name, concepts in facts.items():
        if taxonomy and tax_name != taxonomy:
            continue
        if not isinstance(concepts, dict):
            continue
        for name, payload in concepts.items():
            if isinstance(payload, dict):
                yield tax_name, name, payload


def _compact_facts(observations: list, limit: int) -> list[dict]:
    """Take a long history of one number and keep only recent, unique periods.

    The government file repeats the same quarter many times: the company files
    a quarterly report, then later an annual report that updates that quarter.
    We keep the copy that was submitted most recently.

    Two rows can share the same end date but mean different things:
      - one quarter: start is about 3 months before end  (Apr–Jun)
      - year-so-far: start is much earlier               (Jan–Jun)
    `start` is part of the identity so those stay as two separate rows.

    Field cheat sheet on each row:
      val    = the number
      start  = when the period began (often missing for snapshot items like cash)
      end    = when the period ended / the as-of date
      fy     = the company's fiscal year for this row
      fp     = which slice of the year: Q1, Q2, Q3, or FY (full year)
      form   = which report: 10-Q (quarterly) or 10-K (annual)
      filed  = date that report was submitted
      accn   = accession number of that report (same id get_company_filings uses)
      frame  = optional calendar label like CY2026Q2
    """
    unique: dict[tuple, dict] = {}
    for row in observations:
        if not isinstance(row, dict) or "val" not in row:
            continue
        # One "period" = date range + quarterly/annual tag + report type.
        key = (row.get("start"), row.get("end"), row.get("fp"), row.get("form"))
        previous = unique.get(key)
        # Dates are YYYY-MM-DD strings, so later-in-alphabet = later in time.
        if previous is None or str(row.get("filed", "")) > str(previous.get("filed", "")):
            unique[key] = {
                k: row[k]
                for k in ("start", "end", "val", "fy", "fp", "form", "filed", "accn", "frame")
                if k in row
            }
    # Newest period first; if two share an end date, newer filing first.
    ranked = sorted(
        unique.values(),
        key=lambda r: (str(r.get("end", "")), str(r.get("filed", ""))),
        reverse=True,
    )
    return ranked[:limit]


def _find_concept(facts: dict, concept_id: str) -> tuple[str, str, dict] | None:
    """Find one line item by its machine name (ignores capitalization).

    If the same name exists in more than one name-list, prefer "us-gaap"
    (the standard accounting list).
    """
    wanted = concept_id.replace(" ", "")
    hits = [
        (tax, name, payload)
        for tax, name, payload in _iter_concepts(facts)
        if name.lower() == wanted.lower()
    ]
    if not hits:
        return None
    for hit in hits:
        if hit[0] == "us-gaap":
            return hit
    return hits[0]


def _fiscal_sort_key(info: dict) -> tuple:
    """Newest fiscal period first; ties broken by filing date."""
    match = re.match(r"FY(\d{4}) Q(\d)", info.get("fiscal_period") or "")
    period = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    return (period, info.get("filed", ""))


# ===========================================================================
# LLM tool-call methods — exposed by agents/mcp_server.py
# ===========================================================================


def get_company_filings(ticker: str, limit: int = DEFAULT_FILING_LIMIT) -> dict:
    """List a company's recent 10-K and 10-Q filings from SEC EDGAR, newest fiscal period first.

    Works for any US-listed ticker. Each filing has an `accession` id — pass it
    to search_company_report. `fiscal_period` comes from the company's own
    tags (e.g. "FY2027 Q1"); a 10-K is labelled Q4 and also covers the full year.
    fiscal_year_note explains how this company's fiscal year maps to calendar dates.

        get_company_filings("NVDA")
        # {"ticker": "NVDA", "company": "NVIDIA CORP", "fiscal_year_note": "...", "filings": [
        #   {"accession": "0001045810-26-000123", "form": "10-Q", "fiscal_period": "FY2027 Q2",
        #    "period_end": "2026-07-26", "filed": "2026-08-27", "url": "https://...", "use_for": "..."}, ...]}
    """
    if not ticker:
        return {"error": "ticker is required"}
    try:
        symbol, cik = _resolve(ticker)
        submissions = get_submissions(cik)
        facts = (get_company_facts(cik).get("facts") or {})
    except EdgarError as e:
        return {"ticker": ticker, "error": str(e)}

    fiscal_year_end = submissions.get("fiscalYearEnd")
    fy_end_month = int(fiscal_year_end[:2]) if fiscal_year_end else 12
    tagged_periods = _fiscal_periods_by_accession(facts)

    filings = []
    for row in _recent_filings(submissions):
        if row["form"] not in SUPPORTED_FORMS:
            continue
        accession = row["accessionNumber"]
        if accession in tagged_periods:
            fiscal_period = _format_fiscal_period(*tagged_periods[accession])
        elif row.get("reportDate"):
            fiscal_period = _fiscal_period_for_date(date.fromisoformat(row["reportDate"]), fy_end_month)
        else:
            fiscal_period = None
        filings.append({
            "accession": accession,
            "form": row["form"],
            "fiscal_period": fiscal_period,
            "period_end": row.get("reportDate"),
            "filed": row.get("filingDate"),
            "url": filing_url(cik, accession, row["primaryDocument"]),
            "use_for": DOC_TYPE_USES[row["form"]],
        })

    filings.sort(key=_fiscal_sort_key, reverse=True)
    return {
        "ticker": symbol,
        "company": submissions.get("name"),
        "fiscal_year_note": _fiscal_year_note(fiscal_year_end),
        "filings": filings[: max(1, limit)],
    }


def list_company_concepts(ticker: str, query: str = "", taxonomy: str | None = None) -> list[dict]:
    """List the line-item names this company has numbers for (from SEC XBRL company facts).

    Use this to discover ids like NetIncomeLoss, then pass one id to
    get_company_financials. `query` keeps rows whose id or English label
    contains that text. `taxonomy` keeps one name-list only (try "us-gaap"
    for standard accounting items).

        list_company_concepts("AMD")
        list_company_concepts("AMD", "Revenue")
        list_company_concepts("NVDA", taxonomy="us-gaap")
    """
    if not ticker:
        return []
    try:
        _symbol, cik = _resolve(ticker)
        data = get_company_facts(cik)
    except EdgarError as e:
        return [{"error": str(e)}]

    needle = query.lower().strip()
    catalog = []
    for tax_name, name, payload in _iter_concepts(data.get("facts") or {}, taxonomy=taxonomy):
        label = payload.get("label") or ""
        # Skip unless the search text appears in the machine name or English label.
        if needle and needle not in name.lower() and needle not in label.lower():
            continue
        catalog.append({
            "taxonomy": tax_name,  # which official list this name belongs to
            "concept": name,       # machine id to pass to get_company_financials
            "label": label,        # English name, e.g. "Net Income (Loss)..."
            "units": list((payload.get("units") or {}).keys()),  # USD, shares, ...
        })
    # Group by name-list, then A–Z by machine id.
    catalog.sort(key=lambda row: (row["taxonomy"], row["concept"]))
    return catalog


def get_company_financials(ticker: str, concept_id: str, limit: int = DEFAULT_FACT_LIMIT) -> dict:
    """Return recent values for one line item, by machine id (not English), from SEC XBRL company facts.

    Discover ids with list_company_concepts, then:

        get_company_financials("AMD", "NetIncomeLoss")
        get_company_financials("NVDA", "EarningsPerShareDiluted", limit=4)

    `facts[].val` is in `unit`. If unit is USD, that is dollars, not millions.
    The same quarter can appear twice (3-month vs year-so-far); compare `start`.
    `facts[].accn` is the accession id of the filing the number came from.
    """
    if not ticker or not concept_id:
        return {}
    try:
        symbol, cik = _resolve(ticker)
        data = get_company_facts(cik)
    except EdgarError as e:
        return {"ticker": ticker, "error": str(e)}

    resolved = _find_concept(data.get("facts") or {}, concept_id)
    if resolved is None:
        return {
            "ticker": symbol,
            "error": f"No concept {concept_id!r}. Call list_company_concepts first.",
        }

    taxonomy, concept, payload = resolved
    # Numbers are grouped by unit: dollars (USD), share counts, or $ per share.
    units = payload.get("units") or {}
    unit = next(iter(units), None)  # first unit the file actually has
    observations = units.get(unit, []) if unit else []  # raw history for that unit

    result = {
        "ticker": symbol,
        "entityName": data.get("entityName"),  # legal company name
        "taxonomy": taxonomy,
        "concept": concept,
        "label": payload.get("label"),
        "unit": unit,
        "facts": _compact_facts(observations, limit),  # newest unique periods only
    }
    if payload.get("description"):
        result["description"] = payload["description"][:240]
    return result


def search_company_report(
    ticker: str,
    accession: str,
    query: str,
    max_results: int = 5,
) -> list[str]:
    """Search one 10-K or 10-Q by meaning and return the closest passages.

    `accession` must be copied from get_company_filings with its hyphens kept,
    10 digits, hyphen, 2 digits, hyphen, 6 digits (e.g. "0001045810-26-000075").
    `query` is a plain-language question, not a regular expression.
    The first search of a filing downloads it, embeds it, and stores the
    vectors. Later searches of that same accession only look them up.
    Copy a returned passage into sources[].excerpt.

        search_company_report("AMD", "0000002488-26-000123", "what drove the change in revenue")
    """
    if not ticker or not accession or not query:
        return []
    accession = normalize_accession(accession)
    if not _ACCESSION_RE.match(accession):
        return [f"ERROR: {accession!r} is not an accession number. Use one from get_company_filings."]

    try:
        symbol, cik = _resolve(ticker)
        # Only allow filings that belong to this company and are a supported form.
        # The match also gives us the primary document's filename for the download.
        match = next(
            (row for row in _recent_filings(get_submissions(cik))
             if row["accessionNumber"] == accession and row["form"] in SUPPORTED_FORMS),
            None,
        )
        if match is None:
            return [f"ERROR: {accession} is not a 10-K/10-Q for {ticker}. Call get_company_filings first."]
        # Download (or reuse) the plain text. The vector search reads that text;
        # it does not search the HTML on the SEC website directly.
        report_file = get_filing_text_path(cik, accession, match["primaryDocument"])
        filing_text = report_file.read_text(encoding="utf-8")
        # utils/vectors.py chunks, embeds, stores, and queries S3 Vectors.
        return search_filing_text(symbol, accession, query, filing_text, match["form"], max_results)
    except (EdgarError, VectorSearchError) as e:
        return [f"ERROR: {e}"]
    except Exception as e:
        return [f"ERROR: search failed: {e}"]


# ---------------------------------------------------------------------------
# Manual checks — only runs when you execute this file, not on import
#   python -m utils.tools
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    filings = get_company_filings("AMD", limit=4)
    print({k: v for k, v in filings.items() if k != "filings"})
    for f in filings.get("filings", []):
        print(f)
    print(get_company_financials("AMD", "NetIncomeLoss", limit=3))
    if filings.get("filings"):
        latest = filings["filings"][0]["accession"]
        print(search_company_report("AMD", latest, "what drove the change in revenue", max_results=3))
