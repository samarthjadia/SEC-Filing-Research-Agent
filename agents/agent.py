"""PydanticAI agent over the SEC EDGAR MCP server (GPT nano).

The framework runs the tool loop and validates a typed final answer.
You no longer maintain OpenAI tool JSON or parse the last message by hand.

Tools are not imported directly: the agent launches agents/mcp_server.py as a
subprocess and talks to it over MCP (stdio). PydanticAI asks the server
"what tools do you have?", forwards those schemas to the model, and relays
each tool call back to the server.

Setup:
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # OpenAI key, SEC contact, and the AWS vector settings

Run:
    python agents/agent.py
    python agents/agent.py "What did AMD say about revenue last quarter?"
    python agents/agent.py --steps "..."    # walk the agent loop node by node (learning view)
    python agents/agent.py --events "..."   # print the production event stream as JSON lines
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, AgentRunResult, AgentRunResultEvent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPartDelta,
)
from pydantic_ai.usage import UsageLimits

###### Define the project root and if its not found then add it to the path ######
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.tools import normalize_accession


def _tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Show and store the accession with hyphens, even if the model omitted them."""
    accession = args.get("accession")
    if name == "search_company_report" and isinstance(accession, str):
        return {**args, "accession": normalize_accession(accession)}
    return args

###### Load the environment variables (AWS credentials, AGENT_MODEL, SEC_USER_AGENT field required for SEC Requests) ######
load_dotenv(_PROJECT_ROOT / ".env")

####################################################################
####################################################################
###### Define the model and the maximum number of tool rounds ######
####################################################################
####################################################################
MODEL = os.getenv("AGENT_MODEL", "gpt-5-nano")  # chat model; .env overrides this default

MAX_TOOL_ROUNDS = 8

# Hard cap on tokens for one question (input + output, summed over every LLM call).
MAX_TOKENS_PER_QUESTION = 80_000 # A typical question uses 15-40k; this stops a runaway loop from burning budget.

# This server script is passed to MCPToolset to that tells PydanticAI to start this file and talk to it over stdin and stdout
# The agent writes the request to the childs stdin and reads the response from the childs stdout
MCP_SERVER_SCRIPT = _PROJECT_ROOT / "agents" / "mcp_server.py" # Path to the MCP server script  

# The first search of a big 10-K downloads it and embeds every chunk before it can answer.
# Later searches of that same filing are a lookup and finish well inside this window.
MCP_READ_TIMEOUT_SECONDS = 180

# Basically tells the agent where to use the tools and what to do with the data
RESEARCH_GUIDE = (_PROJECT_ROOT / "knowledge" / "research_guide.md").read_text(encoding="utf-8") # Path to the research guide file

# Passed to the LLM on every single call (ex. initial question and every tool call result back to the LLM)
# Potential for improvement here because this takes a lot of tokens 
AGENT_INSTRUCTIONS = f"""You are a research assistant for SEC filings (10-K, 10-Q) and XBRL financial data.
You can research any US company that files with the SEC; the user gives a ticker
or company name (convert well-known names to tickers yourself, e.g. Apple → AAPL).
Earnings press releases and call transcripts are not available.
How to use tools:
1. get_company_filings — list a ticker's 10-K / 10-Q filings with accession, fiscal_period, and use_for.
   Pick filings by fiscal_period and use_for, not by calendar dates.
2. search_company_report — meaning search inside one filing (what management wrote).
   Pass a plain-language query, not a regular expression. Copy accession exactly
   from get_company_filings, including both hyphens: 0001045810-26-000075.
   Never delete the hyphens or join the digits together. The first search of a
   filing stores embeddings and can take a minute. Copy the returned passages
   into sources.
3. list_company_concepts — find official line-item ids (e.g. NetIncomeLoss).
   Pass a query like "Revenue" or taxonomy "us-gaap". Do not guess English names
   in get_company_financials.
4. get_company_financials — exact tagged numbers for one concept id.
   val is in unit (USD = dollars, not millions). The same quarter can appear
   twice: a 3-month span vs year-so-far — compare start dates.

Put citations only in sources: one object per filing (document, accession, url, excerpt copied from the tool).
Do not list filings or a Sources: line inside answer.
confidence is 0.0 (guess / no evidence) to 1.0 (tools clearly support the answer).
If a tool returns nothing or an error, say so in the answer and lower confidence.

{RESEARCH_GUIDE}
"""

# Sent only when the model finished with sources=[]. The tool results are still
# in the conversation, so this asks for a new final object rather than a new search.
SOURCE_RETRY_PROMPT = """Your previous final answer left sources empty.
Rewrite that answer in the required format, using the tool results already in this conversation.
- answer is prose only. Do not include a Sources: section or a list of filings there.
- sources is a list with one object per filing you used. Copy document, accession, url, and excerpt from the tool results. The excerpt must be text the tool returned, not a paraphrase.
- If no tool returned evidence, leave sources empty and say that in the answer.
Do not call the tools again unless an excerpt you need is missing from the conversation.
"""
############################################################################################################################
############################################################################################################################
############################################################################################################################
############################################################################################################################

# Data class that inherits from BaseModel and turns it into a validator and json schema for the model to use
# The Field is metadata for the model to use to understand the data (Description explain the field and its purpose, Default=None if its not a required field)
class Source(BaseModel):
    """One filing the tools actually returned, plus the bit of text that supports the answer."""
    document: str = Field(
        description='Human label for the filing, e.g. "AMD 10-Q FY2026 Q2" or "AMD XBRL company facts".',
    )
    accession: str | None = Field(
        default=None,
        description="Accession id from get_company_filings or facts[].accn, e.g. 0000002488-26-000123.",
    )
    url: str | None = Field(
        default=None,
        description="Filing url from get_company_filings, if known.",
    )
    excerpt: str = Field(
        description="Short quote, number, or search-hit text copied from that tool result.",
    )

# Data class that inherits from BaseModel and turns it into a validator and json schema for the model to use
# This defines the final answer that the model will produce
class AgentResponse(BaseModel):
    """Structured final answer. PydanticAI asks the model to produce the final answer in this format."""
    answer: str = Field(
        description="Prose answer only. Do not list filenames or a Sources: section here.",
    )
    sources: list[Source] = Field(
        default_factory=list, # list vs []: list is better than [] because [] will share the same memory address and will be mutated in place across the different calls
        description="Evidence objects: filename + excerpt from tools. Empty only if nothing was retrieved.",
    )
    confidence: float = Field(
        ge=0.0, # ge=greater than or equal to
        le=1.0, # le=less than or equal to
        description="The confidence in the answer. 0.0 = no evidence, 1.0 = tools clearly support the answer.",
    )
    # Right now it drops the duplicate sources even if they are different excerpts from the same filing
    # This is a potential for improvement here
    @field_validator("sources") # When AgentResponse is build, this is run on sources field to deduplicate the sources and reassign the value to the sources field
    @classmethod # This is a class method because it is not tied to a specific instance of the class (gets 'cls' instead of 'self')
    def _dedupe_sources(cls, value: list[Source]) -> list[Source]:
        """Keep one source per filing (accession if present, else the document label)."""
        seen: list[Source] = []
        seen_keys: set[str] = set()
        for item in value:
            key = (item.accession or item.document).strip()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            seen.append(item.model_copy(update={"document": item.document.strip(), "excerpt": item.excerpt.strip()}))
        return seen


# MCPToolset + a Path → PythonStdioTransport: PydanticAI runs
# `<this venv's python> agents/mcp_server.py` as a child process and speaks MCP
# over its stdin/stdout. The subprocess starts when a run begins (or once for
# the whole app inside `async with agent:` — see agents/server.py).
# ------------------------------------------------------------------------------------------------
# This toolset is passed to the Agent object to define the tools that the agent can use
edgar_tools = MCPToolset( # edgar_tools is a MCPToolset object that is used to define the tools that the agent can use
    MCP_SERVER_SCRIPT, # Path to the MCP server script (defined above)
    id="edgar", # This is the id of the toolset (used to identify the toolset in the model)
    read_timeout=MCP_READ_TIMEOUT_SECONDS, # 180 seconds (defined above)
)

# This is the usage limits for the agent (defined above)
USAGE_LIMITS = UsageLimits(
    request_limit=MAX_TOOL_ROUNDS, # Most times one question may call the model. Each request is one round trip to LLM (ask & receive = 1 round)
    total_tokens_limit=MAX_TOKENS_PER_QUESTION # Maximum number of tokens for the whole question (defined above)
    )

# This Agent object owns the model, instructions, tools, output type, retries, and also runs the agent loop.
# ------------------------------------------------------------------------------------------------
# Three ways to run the same agent loop (model -> tools -> repeat -> final answer):
# .run_sync()          sync: blocks until done and returns only the final result (used by run_agent / default CLI)
# .iter()              async: hands you each step of the loop (prompt, model call, tool calls, end) to walk yourself (--steps)
# .run_stream_events() async: runs the whole loop and yields live events as they happen (--events, web UI)
# sync = waits on each call; async = can pause while waiting (needs asyncio.run or an existing event loop)
agent = Agent(
    f"{MODEL}",
    name="company-research-agent",
    description="A research assistant for SEC filings and XBRL financial data.",
    metadata={
        "model": MODEL,
        "max_tool_rounds": MAX_TOOL_ROUNDS,
        "data_source": "SEC EDGAR via MCP",
        "agent_instructions": AGENT_INSTRUCTIONS,
        "version": "2.0.0",
        "created_at": datetime.now().isoformat(),
    },
    output_type=AgentResponse,
    retries=2,
    instructions=AGENT_INSTRUCTIONS,
    toolsets=[edgar_tools],
    tool_timeout=MCP_READ_TIMEOUT_SECONDS,
)

def _rewrite_if_sources_empty(result: AgentRunResult[AgentResponse]) -> AgentResponse:
    """One more model call when the answer cited filings in prose but left sources empty.

    An empty list is valid to Pydantic, so the normal retries do not catch it.
    all_messages(...) keeps the tool results. output_tool_return_content closes
    the previous final-answer tool call so the model can write a new one.
    The same usage object keeps this call inside the original token cap.
    If the rewrite is also empty, that answer is returned as-is.
    """
    if result.output.sources:
        return result.output
    rewritten = agent.run_sync(
        SOURCE_RETRY_PROMPT,
        message_history=result.all_messages(
            output_tool_return_content="Answer received, but sources was empty. Rewrite it with citations in sources.",
        ),
        usage_limits=USAGE_LIMITS,
        usage=result.usage,
    )
    return rewritten.output


# One entry point for the agent that uses the run_sync which blocks until done and returns only the final result
# as opposed to the .iter or .run_stream_events which are async and yield live events as they happen
def run_agent(question: str) -> AgentResponse:
    """Run one question through the PydanticAI loop; return a typed AgentResponse."""
    result = agent.run_sync(
        question,
        usage_limits=USAGE_LIMITS,
    )
    return _rewrite_if_sources_empty(result)


# ---------------------------------------------------------------------------
# Streaming, part 1 (learning view): walk the agent loop one node at a time
# ---------------------------------------------------------------------------

PREVIEW_CHARS = 300  # tool results can be huge (whole 10-Q search hits); only show this much

async def run_agent_verbose(question: str) -> AgentResponse:
    """Run the agent with agent.iter() and print every step of the loop as it happens."""
    # agent.iter() starts a run but does not drive it; we pull each step ("node") ourselves.
    async with agent.iter(
        question,  # the user prompt that seeds the first UserPromptNode
        usage_limits=USAGE_LIMITS,  # same safety cap as run_agent
    ) as run:  # `run` is an AgentRun: an async iterator of graph nodes plus shared state (run.ctx)
        step = 0  # count LLM calls so the output reads as "step 1, step 2, ..."
        async for node in run:  # each iteration executes one node and hands us the next one
            if Agent.is_user_prompt_node(node):  # node 1: builds the system + user messages (No API call; just build the message object to send in model request; only runs once per run)
                print("[prompt] building messages for the model") # No llm call here so step doesn't increment
            elif Agent.is_model_request_node(node):  # a request to the model is about to happen (ex. after user prompt is built, it is passed in this to the api call to the llm)
                step += 1  # every ModelRequestNode is one "think" step
                print(f"\n--- step {step}: calling {MODEL} ---")
            elif Agent.is_call_tools_node(node):  # the LLM answered and is asking for a tool call
                # node.stream(run.ctx) yields fine-grained events while the tools execute.
                async with node.stream(run.ctx) as events:
                    async for event in events:  # one event per tool call start / finish
                        if isinstance(event, FunctionToolCallEvent):  # model asked for a tool
                            name = event.part.tool_name  # which Python function it picked
                            args = _tool_args(name, event.part.args_as_dict())  # hyphens restored on accession
                            print(f"  -> {name}({args})")
                        elif isinstance(event, FunctionToolResultEvent):  # tool finished
                            if isinstance(event.part, RetryPromptPart):  # args failed validation
                                # PydanticAI sends the error back to the model so it can fix the call.
                                print(f"  !! retry: {str(event.part.content)[:PREVIEW_CHARS]}")
                            else:  # normal ToolReturnPart: the value your function returned
                                print(f"  <- {str(event.part.content)[:PREVIEW_CHARS]}")
            elif Agent.is_end_node(node):  # the model produced a valid AgentResponse; loop is over
                print(f"\n[done] after {step} model call(s)")
        result = run.result  # available once the End node has been produced
        if result is None:
            raise RuntimeError("agent run ended without an End node")
        if not result.output.sources:
            print("\n[retry] sources was empty; asking for the same answer with citations in sources")
        return _rewrite_if_sources_empty(result)


# ---------------------------------------------------------------------------
# Streaming, part 2 (production view): one flat event stream in *our* schema
# ---------------------------------------------------------------------------

# Utilizes the 'yield' keyword to return a generator object
# Every time the generator is called, it returns a new value
# The generator object is an AsyncIterator that can be used in a for loop
# The for loop will call the generator object and get the next value
# The generator object will return a dictionary with the following keys:
# - seq: the sequence number of the event
# - type: the type of the event
# - ts: the timestamp of the event
# - data: the data of the event
# -------------------------------------------------------------
# When yield is called, it pauses the function and returns the value to the caller and then when its called again, 
# it resumes from the last yield statement
async def stream_run(question: str) -> AsyncIterator[dict[str, Any]]:
    """Yield small JSON-safe dicts describing the run. Clients depend on this shape, not PydanticAI's."""
    seq = 0  # monotonically increasing id so clients can order events and detect gaps because events are not sent in order (ex. model call, tool call, tool result, etc.)
    step = 0  # which LLM call we are on (each time the model is called, this increments)

    def emit(type_: str, **data: Any) -> dict[str, Any]:
        """Wrap a payload in the envelope every event shares: seq + type + timestamp."""
        nonlocal seq  # mutate the counter from the enclosing function
        seq += 1  # next sequence number
        return {"seq": seq, "type": type_, "ts": datetime.now().isoformat(), **data}

    yield emit("run_started", question=question, model=MODEL)  # tell the client work has begun

    # Holds the AgentRunResult from each attempt. The "final" event is sent
    # only after an empty sources list has had one chance to be rewritten.
    captured: list[AgentRunResult[AgentResponse]] = []

    async def drive(
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        usage: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one agent loop and yield its trace events, keeping the result aside."""
        nonlocal step  # the step counter lives on stream_run; each model call bumps it
        async with agent.run_stream_events(
            prompt,
            message_history=message_history,
            usage=usage,
            usage_limits=USAGE_LIMITS,
        ) as events:
            async for event in events:  # every event from every step, in order
                # A model response's parts are numbered from 0, so part 0 starting = a new LLM call.
                if isinstance(event, PartStartEvent) and event.index == 0:
                    step += 1
                    yield emit("step", step=step)

                # Plain text tokens (rare here, since the final answer is structured output).
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    yield emit("text_delta", step=step, text=event.delta.content_delta)

                # The model decided to call one of your tools.
                elif isinstance(event, FunctionToolCallEvent):
                    yield emit(
                        "tool_call",
                        step=step,
                        id=event.part.tool_call_id,  # pairs this call with its result (calls can run in parallel)
                        name=event.part.tool_name,
                        args=_tool_args(event.part.tool_name, event.part.args_as_dict()),
                    )

                # A tool finished (or its arguments were rejected and the model must retry).
                elif isinstance(event, FunctionToolResultEvent):
                    content = event.part.content
                    # Search results are the passages the answer is built from.
                    # The trace preview stays short; passages carries each chunk whole.
                    passages = (
                        [item for item in content if isinstance(item, str)]
                        if event.part.tool_name == "search_company_report" and isinstance(content, list)
                        else None
                    )
                    yield emit(
                        "tool_retry" if isinstance(event.part, RetryPromptPart) else "tool_result",
                        step=step,
                        id=event.part.tool_call_id,  # same id as the matching tool_call event
                        preview=str(content)[:PREVIEW_CHARS],  # never ship the full blob to a UI
                        **({"passages": passages} if passages is not None else {}),
                    )

                # Keep the validated answer. "final" is emitted by the caller once,
                # so a sources rewrite does not flash an empty citation list first.
                elif isinstance(event, AgentRunResultEvent):
                    captured.append(event.result)

    try:
        async for item in drive(question):
            yield item
        result = captured[-1] if captured else None
        if result is not None and not result.output.sources:
            # Second loop: same tool results, new instruction to fill sources.
            async for item in drive(
                SOURCE_RETRY_PROMPT,
                message_history=result.all_messages(
                    output_tool_return_content="Answer received, but sources was empty. Rewrite it with citations in sources.",
                ),
                usage=result.usage,
            ):
                yield item
            result = captured[-1]
        if result is not None:
            usage = result.usage  # requests and tokens for the whole question, including a rewrite
            yield emit(
                "final",
                output=result.output.model_dump(),  # AgentResponse -> plain dict
                usage={
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
            )
    except Exception as exc:  # errors become events so the client can show them instead of hanging
        yield emit("error", error=type(exc).__name__, message=str(exc))


async def _print_events(question: str) -> None:
    """CLI consumer of stream_run: one JSON object per line (same payload the SSE server sends)."""
    async for event in stream_run(question):
        print(json.dumps(event, default=str), flush=True)  # flush so lines appear immediately


if __name__ == "__main__":
    # Chat, embeddings, SEC downloads, and the vector index all read .env.
    # A missing value fails here instead of halfway through the first question.
    _required = (
        "OPENAI_API_KEY",
        "SEC_USER_AGENT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "VECTOR_BUCKET",
        "VECTOR_INDEX",
        "EMBEDDING_MODEL",
    )
    _missing = [name for name in _required if not os.getenv(name, "").strip()]
    if _missing:
        print("Set these in .env: " + ", ".join(_missing), file=sys.stderr)
        raise SystemExit(1)
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    words = [a for a in sys.argv[1:] if not a.startswith("--")]
    question = (
        " ".join(words).strip()
        or "What was AMD's most recent quarterly net income, and what does the latest 10-Q say drove the change in revenue?"
    )
    print(f"Model: {MODEL}")
    print(f"Q: {question}\n")
    if "--steps" in flags:
        print(asyncio.run(run_agent_verbose(question)).model_dump_json(indent=2))
    elif "--events" in flags:
        asyncio.run(_print_events(question))
    else:
        print(run_agent(question).model_dump_json(indent=2))
