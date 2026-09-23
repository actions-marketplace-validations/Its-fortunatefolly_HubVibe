"""Official MCP (Model Context Protocol) server for HubVibe's site
compliance auditing suite -- wraps the 5 REST audit endpoints as MCP
tools using the official `mcp` Python SDK, so any MCP client (Claude
Desktop, Claude Code, etc.) can call them directly by name.

This is a standalone script, deliberately NOT part of the deployed
FastAPI service or its requirements.txt: the `mcp` package pulls in a
newer `starlette` than the one wcag-audit-engine pins for FastAPI, so
bundling it into the service's dependency tree would risk breaking
production on the next deploy. Run this locally (or in its own
container); it talks to the real deployed API over plain HTTP, the same
way integrations/langchain_tool.py does -- it is a thin client, not a
reimplementation of the audit logic.

Usage:
    pip install -r integrations/mcp_requirements.txt
    HUBVIBE_API_KEY=<your key> python integrations/mcp_server.py

Point an MCP client at this script via stdio, e.g. a Claude Desktop
mcpServers config entry:
    {"hubvibe": {"command": "python", "args": ["integrations/mcp_server.py"],
                 "env": {"HUBVIBE_API_KEY": "<your key>"}}}

Auth: HUBVIBE_API_KEY only, same limitation as langchain_tool.py --
x402/MPP payment construction is out of scope for a thin tool wrapper.
Use the `x402`/`mppx` client libraries directly against the routes in
/.well-known/agent.json if a caller should pay per-call instead.
"""

import os

import httpx
from mcp.server.mcpserver import MCPServer

try:
    # mcp 2.x: only a ToolError's message reaches the client. Any other
    # exception is masked to "Error executing tool <name>", which hid the
    # payment hint below from every Claude Desktop user without a key.
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - older mcp
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        ToolError = RuntimeError

HUBVIBE_BASE_URL = os.environ.get(
    "HUBVIBE_BASE_URL", "https://hubvibe-io.com"
)

# Kept equal to SERVICE_VERSION / server.json / mcp.json by a test.
VERSION = "1.4.2"

server = MCPServer(
    name="hubvibe-site-audit",
    version=VERSION,
    description=(
        "Real, rule-based site compliance audits -- accessibility (axe-core), "
        "SEO, security headers, and performance. Deterministic checks only, "
        "never an LLM guessing at quality."
    ),
)


def _call(path: str, url: str) -> dict:
    api_key = os.environ.get("HUBVIBE_API_KEY")
    if not api_key:
        raise ToolError("Set HUBVIBE_API_KEY before calling this tool")
    response = httpx.post(
        f"{HUBVIBE_BASE_URL}{path}",
        json={"url": url},
        headers={"X-API-Key": api_key},
        timeout=60.0,
    )
    if response.status_code == 402:
        raise ToolError(
            f"Payment required: {response.json()}. Set a valid HUBVIBE_API_KEY, "
            "or use the x402/mppx client libraries to pay per-call instead."
        )
    response.raise_for_status()
    return response.json()


@server.tool()
def audit_wcag(url: str) -> dict:
    """WCAG 2.1 A/AA accessibility audit via axe-core. $0.05/call."""
    return _call("/audit/wcag", url)


@server.tool()
def audit_seo(url: str) -> dict:
    """SEO audit: title, meta description, H1 structure, canonical link,
    OpenGraph tags, structured data, lang attribute. $0.05/call."""
    return _call("/audit/seo", url)


@server.tool()
def audit_security(url: str) -> dict:
    """Security headers audit: HTTPS, HSTS, CSP, X-Content-Type-Options,
    clickjacking protection, Referrer-Policy, CORS. Not a TLS/cipher scan
    or a penetration test. $0.05/call."""
    return _call("/audit/security", url)


@server.tool()
def audit_performance(url: str) -> dict:
    """Performance audit: DOM node count, transferred bytes, and request
    count from one real page load. Not a full Lighthouse-style audit.
    $0.05/call."""
    return _call("/audit/performance", url)


@server.tool()
def audit_bundle(url: str) -> dict:
    """Runs audit_wcag + audit_seo + audit_security + audit_performance
    atomically against one URL, billed once. If any dimension fails to
    run, the whole call fails and nothing is billed. $0.15/call."""
    return _call("/audit/bundle", url)


if __name__ == "__main__":
    server.run()
