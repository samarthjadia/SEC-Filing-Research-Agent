"""Small client for the SEC EDGAR APIs, with an on-disk cache.

Simply: this file is the only place that talks to the SEC. Everything else
asks it for "AMD's filings" or "AMD's numbers" and gets plain Python dicts
back. Anything it downloads is saved under .cache/edgar/ so the next question
about the same company is fast and does not hit the SEC again.

Technically:
  * Endpoints (no API key needed; docs:
    https://www.sec.gov/search-filings/edgar-application-programming-interfaces):
        www.sec.gov/files/company_tickers.json          ticker → CIK lookup
        data.sec.gov/submissions/CIK##########.json     filing history + metadata
        data.sec.gov/api/xbrl/companyfacts/CIK#####.json  every tagged number
        www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc} the filing document itself
  * Fair-access rules: every request must send a User-Agent with contact info
    (SEC_USER_AGENT in .env) and stay under 10 requests/second. Requests without
    a User-Agent get HTTP 403; going over the rate gets your IP blocked for a while.
  * Thread safety: the MCP server runs sync tools in worker threads, so the rate
    limiter uses a lock and cache files are written atomically (temp file + rename).

Cache layout mirrors the URL, so you can open the files and see what was fetched:
    .cache/edgar/data.sec.gov/submissions/CIK0000002488.json
    .cache/edgar/www.sec.gov/Archives/edgar/data/2488/000000248826000123/amd-20260627.htm
    .cache/edgar/text/2488/0000002488-26-000123.txt     (converted plain text)
"""

from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from utils.html_text import decode_html, html_to_text

### Defining the project root path so that the .env file can be loaded ###
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(_PROJECT_ROOT / ".env")

CACHE_DIR = Path(os.getenv("EDGAR_CACHE_DIR", _PROJECT_ROOT / ".cache" / "edgar")).resolve()
TEXT_CACHE_DIR = CACHE_DIR / "text"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"

# How long a cached copy counts as fresh, in seconds. None = forever.
#   - The ticker list barely changes.
#   - Submissions/facts change whenever the company files, so refresh hourly-ish.
#   - An archived filing never changes after it is published, so cache forever.
TICKERS_TTL = 24 * 3600 # 24 hours
SUBMISSIONS_TTL = 3600 # 1 hour
FACTS_TTL = 6 * 3600 # 6 hours
ARCHIVE_TTL = None # Forever

MIN_SECONDS_BETWEEN_REQUESTS = 0.12  # ≈ 8 req/s, safely under the SEC's 10 req/s
MAX_ATTEMPTS = 4                     # first try + 3 retries on 429/5xx (429 = Too Many Requests, 5xx = Server Error)
HTTP_TIMEOUT_SECONDS = 30            # 30 seconds


class EdgarError(RuntimeError):
    """Anything that stops us from getting data from EDGAR (bad ticker, network, 4xx)."""


# ---------------------------------------------------------------------------
# HTTP: one shared client, rate-limited, with retries
# ---------------------------------------------------------------------------

# They exist because the MCP server can run two tools at the same time, on two threads, and both threads call into this file.
_rate_lock = threading.Lock()

# This is a global variable that is used to store the timestamp of the last request
_last_request_at = 0.0

# The HTTP connection object (httpx.Client) that actually performs the downloads. 
# It starts as None because it is not built at import time. It is built on the first request, inside _http()
_client: httpx.Client | None = None

# The lock stops two threads from both seeing _client as None and opening two connections.
# "does the connection already exist?" and, the first time, creating it. 
# After that, every call takes the lock for a moment, sees the existing client, and returns it.
_client_lock = threading.Lock()

# Returns the user agent string for the SEC API
def _user_agent() -> str:
    """The SEC requires "Name email" style contact info on every request."""
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        raise EdgarError(
            'Set SEC_USER_AGENT in .env, e.g. SEC_USER_AGENT="Your Name you@email.com". '
            "The SEC rejects requests without it."
        )
    return ua


def _http() -> httpx.Client:
    """Lazily build one httpx.Client so TCP/TLS connections are reused across calls."""
    global _client # 'global' keyword is used to modify the global variable _client (not create a new local variable and modifying that)
    with _client_lock: # with statement is used to acquire the lock and release it automatically when these lines are done executing
        if _client is None:
            _client = httpx.Client( # builds the HTTP connection object
                headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=True, # tells the HTTP client to follow a “this moved” response and return the file at the new address
            )
        return _client


def _wait_for_rate_limit() -> None:
    """Block until at least MIN_SECONDS_BETWEEN_REQUESTS has passed since the last request.

    Simply: a turnstile that lets one request through every 0.12 s, no matter
    how many threads are asking. Technically: a global timestamp guarded by a
    lock; each caller reserves the next slot, then sleeps outside the lock.
    """
    global _last_request_at
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _last_request_at + MIN_SECONDS_BETWEEN_REQUESTS)
        _last_request_at = slot
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _download(url: str) -> bytes:
    """GET a URL politely: rate limit, retry on 429/5xx with exponential backoff."""
    for attempt in range(MAX_ATTEMPTS):
        _wait_for_rate_limit() # blocks until at least MIN_SECONDS_BETWEEN_REQUESTS has passed since the last request
        try:
            response = _http().get(url)
        except httpx.HTTPError as e:
            if attempt == MAX_ATTEMPTS - 1:
                raise EdgarError(f"Network error fetching {url}: {e}") from e
        else:
            if response.status_code == 200:
                return response.content
            if response.status_code == 404:
                raise EdgarError(f"Not found on EDGAR: {url}")
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise EdgarError(f"EDGAR returned HTTP {response.status_code} for {url}")
        time.sleep(2**attempt)  # 1s, 2s, 4s
    raise EdgarError(f"Gave up fetching {url}")  # unreachable; keeps type checkers happy


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _cache_path(url: str) -> Path:
    """Map https://host/a/b.json → .cache/edgar/host/a/b.json."""
    parsed = urlparse(url)
    return CACHE_DIR / parsed.netloc / parsed.path.lstrip("/")


def _is_fresh(path: Path, ttl: float | None) -> bool:
    if not path.exists():
        return False
    return ttl is None or (time.time() - path.stat().st_mtime) < ttl


def _write_atomic(path: Path, data: bytes) -> None:
    """Write to a temp file then rename, so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic on the same filesystem


def _fetch_cached(url: str, ttl: float | None) -> Path:
    """Return a local file holding the URL's body, downloading only if the cache is stale.

    If the SEC is unreachable but we have an old copy, use the old copy:
    slightly stale filings are more useful than an error.
    """
    path = _cache_path(url)
    if _is_fresh(path, ttl):
        return path
    try:
        _write_atomic(path, _download(url))
    except EdgarError:
        if path.exists():
            return path
        raise
    return path


@lru_cache(maxsize=16)
def _parse_json_file(path_str: str, mtime: float) -> dict:
    """Parse a cached JSON file once per version.

    companyfacts for a big company is tens of MB; re-parsing it on every tool
    call is slow. The cache key includes the file's mtime, so when the disk
    copy is refreshed we automatically parse the new version.
    """
    with open(path_str, "rb") as f:
        return json.load(f)


def _fetch_json(url: str, ttl: float | None) -> dict:
    path = _fetch_cached(url, ttl)
    return _parse_json_file(str(path), path.stat().st_mtime)


# ---------------------------------------------------------------------------
# Public API used by utils/tools.py
# ---------------------------------------------------------------------------


def normalize_ticker(ticker: str) -> str:
    """'brk.b' → 'BRK-B' (EDGAR writes class shares with a dash)."""
    return ticker.strip().upper().replace(".", "-")


def ticker_to_cik(ticker: str) -> int:
    """Look up a company's CIK (the SEC's permanent company id) from its stock ticker.

    company_tickers.json looks like {"0": {"cik_str": 2488, "ticker": "AMD", "title": "..."}, ...}
    """
    wanted = normalize_ticker(ticker)
    for row in _fetch_json(TICKERS_URL, TICKERS_TTL).values():
        if row.get("ticker", "").upper() == wanted:
            return int(row["cik_str"])
    raise EdgarError(f"Unknown ticker {ticker!r}: not in the SEC's company_tickers.json")


def get_submissions(cik: int) -> dict:
    """Company metadata + recent filing history.

    Notable fields: name, tickers, fiscalYearEnd ("1226" = Dec 26), and
    filings.recent — a *columnar* table: parallel lists such as
    form[i], accessionNumber[i], filingDate[i], reportDate[i], primaryDocument[i].
    """
    return _fetch_json(SUBMISSIONS_URL.format(cik=cik), SUBMISSIONS_TTL)


def get_company_facts(cik: int) -> dict:
    """Every XBRL-tagged number the company has filed (same shape as the old local *_companyFacts.json)."""
    return _fetch_json(COMPANY_FACTS_URL.format(cik=cik), FACTS_TTL)

# Builds the public web address of one filing's main document. It does not download anything.
def filing_url(cik: int, accession: str, document: str) -> str:
    """Public URL of a filing's main document. Accession 0000002488-26-000123 → folder 000000248826000123."""
    return ARCHIVE_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), doc=document)

# Makes sure a filing's plain text is saved on disk, then returns the path to that file. 
# The caller searches the file; this function does not search it.
# Only search_company_report calls it. That tool then embeds this text and searches the stored vectors.
def get_filing_text_path(cik: int, accession: str, document: str) -> Path:
    """Download a filing's HTML once, convert it to plain text once, return the .txt path.

    Simply: the first search inside a 10-Q downloads and cleans it (a few
    seconds); every later search reuses the saved text instantly.
    Technically: both the raw HTML and the derived .txt are cached forever,
    because EDGAR archive documents are immutable once accepted.
    """
    text_path = TEXT_CACHE_DIR / str(cik) / f"{accession}.txt"
    if text_path.exists():
        return text_path
    html_path = _fetch_cached(filing_url(cik, accession, document), ARCHIVE_TTL)
    text = html_to_text(decode_html(html_path.read_bytes()))
    _write_atomic(text_path, text.encode("utf-8"))
    return text_path
