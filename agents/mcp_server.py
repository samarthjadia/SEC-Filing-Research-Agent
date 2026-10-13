"""MCP server that exposes the SEC EDGAR research tools in utils/tools.py.

Simply: MCP (Model Context Protocol) is a standard plug for AI tools. Instead
of the agent importing our Python functions directly, this file "publishes"
them as a little server. Any MCP client — our PydanticAI agent, Cursor,
Claude Desktop, a LangChain agent via langchain-mcp-adapters — can then
discover the tools and call them the same way.

Technically:
  * Built with the official MCP Python SDK (mcp>=2, where FastMCP was renamed
    to MCPServer). add_tool() reads each function's type hints and docstring
    to generate the JSON Schema + description the model sees.
  * Default transport is stdio: the client starts this file as a subprocess
    and exchanges JSON-RPC messages over stdin/stdout. That is why nothing in
    the tools may print() to stdout — it would corrupt the protocol stream.
  * Sync tools run in a worker thread pool, so parallel tool calls from the
    model don't block each other; utils/edgar.py is thread-safe for this.

Run it yourself (normally the agent launches it for you):
    python agents/mcp_server.py                       # stdio (for local clients)
    python agents/mcp_server.py --http                # streamable HTTP on :8001/mcp

Use it from Cursor / Claude Desktop with a config entry like:
    {"command": "/path/to/.venv/bin/python", "args": ["/path/to/agents/mcp_server.py"]}
"""

from __future__ import annotations

import sys
from pathlib import Path

### Defining the project root path so that the tools can be imported ###
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path: # if the project root is not in the sys.path, add it
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp.server.mcpserver import MCPServer # Official MCP Server SDK
from mcp.types import ToolAnnotations  # MCP Tool Annotations that adds small labels to the tools when the server advertises them

from utils.tools import (  # Importing the tools from the tools.py file
    get_company_filings,
    get_company_financials,
    list_company_concepts,
    search_company_report,
)

server = MCPServer( # builds an empty MCP server object named 'edgar-research'
    name="edgar-research",
    instructions=( # short note the server can hand to the client (ex. Cursor, Claude Desktop, etc.) during mcp handshake
                   # My agent doesn't use it because the MCPToolset object leaves 'include_instructions=False' by default
        "Tools for researching US public companies from SEC EDGAR filings (10-K, 10-Q) "
        "and XBRL financial data. Start with get_company_filings to get accession ids."
    ),
)

# readOnlyHint=True: the tool only reads data.
# openWorldHint=True: the tool can reach the internet (SEC, and for search, AWS).
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# The first search of a filing writes vectors into S3, so this tool is not read-only.
_SEARCH = ToolAnnotations(readOnlyHint=False, openWorldHint=True)

for tool in (get_company_filings, list_company_concepts, get_company_financials):
    server.add_tool(tool, annotations=_READ_ONLY)
server.add_tool(search_company_report, annotations=_SEARCH)


if __name__ == "__main__":
    if "--http" in sys.argv:
        server.run("streamable-http", port=8001)
    else:
        server.run()  # the server starts listening on stdin and writes to stdout