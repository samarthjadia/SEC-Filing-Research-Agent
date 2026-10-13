"""HTTP API for the web UI: streams agent runs as Server-Sent Events, plus cheap no-LLM endpoints.

Run (from the project root):
    uvicorn agents.server:app --reload --port 8000

Endpoints:
    POST /ask                         {"question": "..."} → SSE stream of agent events (uses OpenAI tokens)
    GET  /ask?q=...                   same, for quick curl tests
    GET  /company/{ticker}            name, fiscal-year note, recent filings   (EDGAR only, no tokens)
    GET  /financials/{ticker}/{metric} quarterly series for a chart           (EDGAR only, no tokens)
    GET  /health                      liveness check + today's token spend

Try it:
    curl -N -X POST localhost:8000/ask -H 'content-type: application/json' -d '{"question":"AMD net income last quarter?"}'
    curl localhost:8000/company/NVDA
    curl localhost:8000/financials/NVDA/revenue

Guardrails (so a public demo can't drain your OpenAI budget):
  * Question length is capped (MAX_QUESTION_CHARS).
  * Each visitor IP gets ASK_LIMIT_PER_HOUR questions per rolling hour.
  * All visitors together share DAILY_TOKEN_BUDGET tokens per day.
  * Per-question token caps live in agents/agent.py (USAGE_LIMITS).
  These counters are in memory: they reset when the server restarts and are
  per-process. Fine for one small server; use Redis for anything bigger.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.agent import MODEL, agent, stream_run
from utils.tools import get_company_filings, get_company_financials

MAX_QUESTION_CHARS = 500
ASK_LIMIT_PER_HOUR = int(os.getenv("ASK_LIMIT_PER_HOUR", "10"))
DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "2000000"))  # ≈ $1/day on gpt-5-nano


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

# server's startup and shutdown hook. 
# Uvicorn calls it once when the API process starts, 
# and the yield inside it is the pause where the server sits and handles requests until you stop it.
# 
# FastAPI always calls lifespan(app) by default even though the function doesn't do anything with it.
# lifespan is a special function that FastAPI calls when the server starts and stops.
# asynccontextmanager is needed for FastAPI to be able to use the the function and start and stop the server.
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the EDGAR MCP server subprocess once at boot and reuse it for every request.

    `async with agent:` enters the agent's toolsets, which spawns
    agents/mcp_server.py and completes the MCP handshake. Without this, each
    /ask request would start (and tear down) its own subprocess.
    """
    async with agent: # calls the agent's toolsets, which spawns agents/mcp_server.py and completes the MCP handshake. 
                      # Without this, each /ask request would start (and tear down) its own subprocess.
        yield


app = FastAPI(title="company-research-agent", lifespan=lifespan)  # the app that uvicorn serves


# ---------------------------------------------------------------------------
# Guardrails: per-IP rate limit + global daily token budget
# ---------------------------------------------------------------------------

_ask_times: dict[str, deque[float]] = defaultdict(deque)  # ip → timestamps of recent questions
_budget = {"day": date.today(), "tokens": 0} # a dictionary to store the day and the number of tokens spent today
_guard_lock = threading.Lock() # a lock to synchronize access to the _ask_times and _budget dictionaries

# Guesses which visitor sent the question, so the rate limit can count questions per person. 
# It is written for the Next.js proxy in web/src/app/api/[...path]/route.ts, the only thing that should be calling this API.
def _client_ip(request: Request) -> str:
    """Best guess at the visitor's IP.

    The Next.js proxy forwards the browser's IP in X-Forwarded-For; without it
    every request would look like it came from the proxy. Only trust this
    header when the backend is reachable *only* through your proxy — otherwise
    anyone can fake it.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() # returns the first IP address in the X-Forwarded-For header
    return request.client.host if request.client else "unknown" 

# The rule is "10 questions in the last hour," not "10 questions ever." 
# A question asked at 1:00 should not still count at 2:05. now - times[0] > 3600 means that oldest timestamp is more than 
# 3,600 seconds ago, so popleft() removes it. 
# The while loop keeps going because several old ones may have expired. 
# It stops at the first timestamp that is still inside the hour. Everything after that is newer, so those still count.
# 
# hen it counts what is left. If that is already 10, this new question is rejected. If not, append(now) records it.
def _check_rate_limit(ip: str) -> None:
    """Sliding window: drop timestamps older than 1 hour, then count what's left."""
    now = time.time() # get the current time
    with _guard_lock: # acquire the lock to synchronize access to the _ask_times dictionary
        times = _ask_times[ip] # get the list of questions for the IP address
        while times and now - times[0] > 3600: # if the time difference between the first question and the current time is greater than 1 hour, remove the first question
            times.popleft() # remove the first question
        if len(times) >= ASK_LIMIT_PER_HOUR: # if the number of questions in the last hour is greater than the limit, raise a 429 error
            retry_in = int(3600 - (now - times[0])) + 1 # calculate the time to wait before the next request
            raise HTTPException( # raise a 429 error
                status_code=429,
                detail=f"Rate limit: {ASK_LIMIT_PER_HOUR} questions per hour. Try again in {retry_in // 60 + 1} min.",
            )
        times.append(now) # add the current time to the list of questions


def _tokens_spent_today() -> int:
    with _guard_lock: # acquire the lock to synchronize access to the _budget dictionary
        if _budget["day"] != date.today():  # new day → reset the counter
            _budget.update(day=date.today(), tokens=0) # reset the counter to 0
        return _budget["tokens"] # return the number of tokens spent today


def _record_tokens(usage: dict) -> None:
    with _guard_lock: # acquire the lock to synchronize access to the _budget dictionary
        _budget["tokens"] += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)) # add the number of input and output tokens to the counter


# ---------------------------------------------------------------------------
# /ask — the agent, streamed as Server-Sent Events
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)

# turns one agent run into a live stream the browser can read. 
# Each call starts one new question through the agent. It does not start a new asyncio event loop.
# It returns an async generator that yields one SSE frame at a time.
# _start_stream calls it once per /ask. That starts one agent run: model, tools, model again, until final_result.
def _sse_frame(event: dict) -> str:
    """One SSE frame: id, event, and data lines, then a blank line."""
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


async def _sse(question: str, request: Request) -> AsyncIterator[str]:
    """Turn each event dict from stream_run() into one SSE frame.

    The browser splits the stream on blank lines to get one event at a time.
    A watcher cancels the agent when the client disconnects. Starlette does not
    do that itself on current ASGI, so Stop would otherwise run the question to the end.
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in stream_run(question):
                await queue.put(event)
        finally:
            # put_nowait does not await, so a cancellation still delivers the sentinel.
            queue.put_nowait(None)

    producer = asyncio.create_task(produce())

    async def watch() -> None:
        while not producer.done():
            if await request.is_disconnected():
                producer.cancel()
                return
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(watch())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            if event["type"] == "final":
                _record_tokens(event.get("usage") or {})
            yield _sse_frame(event)
    finally:
        watcher.cancel()
        producer.cancel()
        await asyncio.gather(producer, watcher, return_exceptions=True) 

# _client_ip(request) runs first and returns a string like "203.0.113.5". 
# That string is passed into _check_rate_limit. 
# The check throws away this IP's questions older than an hour, then counts what is left. 
# If there are already 10, it raises HTTPException with status 429 and this function stops.
# 
# StreamingResponse takes the async generator _sse(question) and writes each chunk as _sse yields it. 
# Calling _sse here does not run the agent yet. 
# It only builds the generator. 
# FastAPI pulls from it while sending the response, and that pull is what starts stream_run and the agent.
def _start_stream(question: str, request: Request) -> StreamingResponse:
    """Apply guardrails, then return the streaming response."""
    if _tokens_spent_today() >= DAILY_TOKEN_BUDGET:
        raise HTTPException(status_code=503, detail="The demo's daily budget is used up. Please try again tomorrow.")
    _check_rate_limit(_client_ip(request)) 
    return StreamingResponse( 
        _sse(question.strip(), request),  # FastAPI pulls from this async generator and writes each chunk as it arrives
        media_type="text/event-stream",  # tells the browser this is a Server-Sent Event stream, so it should read the frames as they arrive instead of waiting for the connection to close.
        # X-Accel-Buffering: By default nginx collects response bytes until it has a chunk worth sending. 
        #                    For a normal page that is fine. For this stream it would hold back "calling get_company_filings" 
        #                    until more text arrived, so the trace would pop in late instead of line by line. 
        #                    "no" tells nginx to pass each piece through as soon as the API writes it. 
        headers={
            "Cache-Control": "no-cache",  # proxies must not buffer or cache a live stream
            "X-Accel-Buffering": "no",  # disables nginx response buffering if you deploy behind it
        },
    )


@app.post("/ask")
async def ask(body: AskRequest, request: Request) -> StreamingResponse:
    """Used by the web UI (fetch() + POST lets us send JSON and headers)."""
    return _start_stream(body.question, request) 

# for a quick test from the terminal or a browser: curl -N "localhost:8000/ask?q=AMD%20net%20income"
# %20 is the URL encoding for a space.
@app.get("/ask")
async def ask_get(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUESTION_CHARS, description="Question for the agent"),
) -> StreamingResponse:
    """GET version for curl / browser EventSource testing."""
    return _start_stream(q, request) # starts the stream and returns the StreamingResponse object


# ---------------------------------------------------------------------------
# No-LLM endpoints for the company panel (cost: cached EDGAR calls only)
# ---------------------------------------------------------------------------
# Declared with plain `def` (not `async def`): FastAPI runs these in a thread
# pool, so the blocking EDGAR HTTP calls don't freeze the event loop.

# A "metric" in the UI maps to one or more XBRL concepts, because companies
# tag the same idea differently (e.g. revenue has three common names).
METRICS: dict[str, dict] = {
    "revenue": {
        "label": "Revenue",
        "concepts": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    },
    "net-income": {"label": "Net income", "concepts": ["NetIncomeLoss"]},
    "eps": {"label": "Diluted EPS", "concepts": ["EarningsPerShareDiluted"]},
    "operating-income": {"label": "Operating income", "concepts": ["OperatingIncomeLoss"]},
}


def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _fourth_quarters(facts: list[dict]) -> list[dict]:
    """Add a Q4 where the company only reported the full year.

    Revenue and income add up, so the fourth quarter is the full-year number
    minus the nine-month year-to-date number with the same start date. If a
    real 3-month row already ends on that year-end, keep it and do not invent
    a second one. EPS must not come through here: per-share math is not additive.
    """
    quarterly: list[dict] = []
    nine_month: list[dict] = []
    full_year: list[dict] = []
    for fact in facts:
        start, end = fact.get("start"), fact.get("end")
        if not start or not end or "val" not in fact:
            continue
        span = _days_between(start, end)
        if 70 <= span <= 110:
            quarterly.append(fact)
        elif 240 <= span <= 320:  # about nine months, including a 53-week year
            nine_month.append(fact)
        elif 350 <= span <= 380:  # about twelve months (52 or 53 weeks)
            full_year.append(fact)

    def latest_filing(rows: list[dict]) -> dict[tuple[str, str], dict]:
        """One row per start/end. A later filing restates the same period, so it wins."""
        chosen: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["start"], row["end"])
            previous = chosen.get(key)
            if previous is None or str(row.get("filed", "")) >= str(previous.get("filed", "")):
                chosen[key] = row
        return chosen

    already_tagged = {row["end"] for row in quarterly}
    nines = latest_filing(nine_month)
    derived: list[dict] = []
    for year in latest_filing(full_year).values():
        if year["end"] in already_tagged:
            continue  # the company already published a 3-month Q4
        candidates = [
            row for row in nines.values()
            if row["start"] == year["start"] and row["end"] < year["end"]
        ]
        if not candidates:
            continue
        nine = max(candidates, key=lambda row: row["end"])  # year-to-date through Q3
        # The gap from that date to year-end should itself be one quarter.
        if not 70 <= _days_between(nine["end"], year["end"]) <= 120:
            continue
        derived.append({"end": year["end"], "val": year["val"] - nine["val"], "filed": year.get("filed", "")})
    return quarterly + derived


@app.get("/company/{ticker}")
def company(ticker: str) -> dict:
    """Company header + filings list for the side panel."""
    result = get_company_filings(ticker, limit=8)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/financials/{ticker}/{metric}")
def financials(ticker: str, metric: str, quarters: int = Query(8, ge=1, le=20)) -> dict:
    """Last N single-quarter values of a metric, oldest → newest (ready to chart).

    Filings mix 3-month, 6-month, 9-month and 12-month rows for the same
    concept. A chart needs comparable bars, so we keep only rows whose
    start→end span is about one quarter (70–110 days). When a 10-K has no
    3-month Q4, revenue and income derive it as full year minus nine months.
    EPS is not derived, because the share count changes each quarter.
    """
    # "revenue" from the URL → the display name and the XBRL tags to try.
    spec = METRICS.get(metric)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric!r}. Try: {', '.join(METRICS)}")

    # Companies use different official names for the same idea (three for revenue).
    # Try each tag and keep the one whose newest number is the most recent.
    best: dict | None = None
    for concept in spec["concepts"]:
        data = get_company_financials(ticker, concept, limit=80)  # from EDGAR company facts, not the filing HTML
        if "error" in data:
            continue  # this company never used that tag
        # A fact's fy/fp describe the filing, not the quarter (a 2026 10-Q also
        # repeats the 2025 comparison), so rows are chosen by their start/end
        # dates. Dollar metrics also gain a derived Q4. EPS stays 3-month tags only.
        if metric == "eps":
            # Per-share figures do not add, so Q4 is included only when it was tagged.
            rows = [
                row for row in data["facts"]
                if row.get("start") and row.get("end") and 70 <= _days_between(row["start"], row["end"]) <= 110
            ]
        else:
            rows = _fourth_quarters(data["facts"])
        points = sorted(
            ({"end": row["end"], "val": row["val"], "filed": str(row.get("filed", ""))} for row in rows),
            key=lambda row: (row["end"], row["filed"]),
            reverse=True,  # newest quarter first; a restatement (later filing) comes before the original
        )
        if points and (best is None or points[0]["end"] > best["points"][0]["end"]):
            best = {"concept": concept, "unit": data["unit"], "points": points}

    if best is None:
        raise HTTPException(status_code=404, detail=f"No quarterly {spec['label'].lower()} data for {ticker.upper()}")

    # The same quarter can appear twice (first in a 10-Q, again restated in a 10-K).
    # Keep the first, which is the newer filing, and drop the rest.
    seen, series = set(), []
    for p in best["points"]:
        if p["end"] not in seen:
            seen.add(p["end"])
            series.append(p)
    return {
        "ticker": ticker.upper(),
        "metric": metric,
        "label": spec["label"],  # "Revenue" — the chart title
        "concept": best["concept"],  # which XBRL tag actually had the data
        "unit": best["unit"],  # USD means dollars, not millions
        "points": [{"end": p["end"], "val": p["val"]} for p in reversed(series[:quarters])],  # oldest → newest
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": MODEL, "tokens_today": _tokens_spent_today(), "daily_budget": DAILY_TOKEN_BUDGET}
