"""/.well-known/ard.json -- Agentic Resource Discovery manifest.

The ARD specification (agenticresourcediscovery.org/spec, v0.91) is the
federated, domain-anchored index Google, Microsoft and Hugging Face registries
crawl: a domain publishes `{"entries": [...]}` at /.well-known/ard.json, each
entry naming one resource with an `identifier` (urn:air:<publisher>:...), a
`displayName`, an IANA media `type`, exactly one of `url` / `data`, and --
the signal the registries build their semantic index from --
`representativeQueries` (2-5 natural-language asks) and `capabilities`.

Built from the same catalogs the routes charge from, so an entry can never
describe a price, an input or an output the route does not have. Only
workers this deployment can deliver are listed, by the same fail-closed rule
the manifest and the 402 follow. Nothing here names a payment rail: which
rails settle is deployment state and lives in /.well-known/agent.json.
"""

from typing import Callable, Iterable, Optional

SPEC_VERSION = "0.91"
CONTEXT = "https://agenticresourcediscovery.org/context/v1"

# Media types. MCP server cards and A2A cards are the spec's own; ARD does not
# mandate a type for an OpenAPI document or a single HTTP route, so those use
# the OpenAPI Initiative's media type and a vendor type for our route
# descriptor (the `data` of a route entry is the descriptor itself).
TYPE_MCP_SERVER_CARD = "application/mcp-server-card+json"
TYPE_A2A_AGENT_CARD = "application/a2a-agent-card+json"
TYPE_OPENAPI = "application/openapi+json"
TYPE_AGENT_MANIFEST = "application/json"
TYPE_HUBVIBE_ROUTE = "application/vnd.hubvibe.route+json"

# The natural-language asks each audit serves. The workers' live in
# app/workers/contract.py next to their output schemas.
AUDIT_QUERIES = {
    "/audit/wcag": [
        "check a website for WCAG 2.1 accessibility violations",
        "run an axe-core accessibility audit on a URL",
        "is this page accessible, and which rules does it fail",
    ],
    "/audit/seo": [
        "on-page SEO audit of a web page",
        "check title, meta description, canonical and structured data on a URL",
        "what SEO problems does this page have",
    ],
    "/audit/security": [
        "check a site's security headers: HSTS, CSP, X-Content-Type-Options",
        "security header audit of a URL",
        "is this website missing security headers",
    ],
    "/audit/performance": [
        "how heavy is this page: DOM size, bytes transferred, request count",
        "page weight and request count audit of a URL",
    ],
    "/audit/bundle": [
        "run accessibility, SEO, security and performance checks on a URL in one call",
        "full site compliance audit of a page",
        "gate a deployment on accessibility and SEO regressions",
    ],
}


def _identifier(publisher: str, namespace: str, name: str) -> str:
    # urn:air:<publisher>:<namespace>:<name>; the pattern allows [a-zA-Z0-9._-]
    # in every segment after the publisher, which every catalog name satisfies.
    return f"urn:air:{publisher}:{namespace}:{name}"


def _capability_token(name: str) -> str:
    """`market.quote` -> `MarketQuote`: the short skill token the spec asks
    for in `capabilities`, alongside the raw names an agent may already know."""
    return "".join(part.capitalize() for part in name.replace(".", "_").split("_"))


def build_manifest(
    *,
    base_url: str,
    version: str,
    display_title: str,
    audits: Iterable[dict],
    audit_output_schemas: dict,
    audit_input_schema_for: Callable[[dict], dict],
    mcp_tool_names: Iterable[str],
    workers_catalog,
    worker_contract,
    updated_at: Optional[str] = None,
) -> dict:
    """The manifest, from the live catalogs.

    `audits` are the rows of main._CATALOG; `workers_catalog` is
    app.workers.catalog (or None when the network is not configured);
    `worker_contract` is app.workers.contract.
    """
    base = base_url.rstrip("/")
    publisher = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    stamp = {"updatedAt": updated_at} if updated_at else {}
    common = {"version": version, **stamp}

    entries = [
        {
            "identifier": _identifier(publisher, "node", "hubvibe"),
            "displayName": display_title,
            "type": TYPE_AGENT_MANIFEST,
            "url": f"{base}/.well-known/agent.json",
            "description": (
                "The node's own manifest: every audit and worker with price, "
                "input and output schema, the payment rails live on this "
                "deployment, rate limits, and how receipts work. Read this "
                "first; everything below is one route of it."),
            "capabilities": ["SiteAudit", "WorkerNetwork", "MachinePayable", "Receipts"],
            "representativeQueries": [
                "machine-payable API an agent can pay per call without an account",
                "pay-per-call site audits and dev utilities over HTTP 402",
                "what can I buy from this node and what does each call cost",
            ],
            "tags": ["hubvibe", "http-402", "pay-per-call", "agents"],
            "metadata": {"openapi": f"{base}/openapi.json", "llms_txt": f"{base}/llms.txt"},
            **common,
        },
        {
            "identifier": _identifier(publisher, "mcp", "site-audits"),
            "displayName": "HubVibe site audits (MCP server)",
            "type": TYPE_MCP_SERVER_CARD,
            "url": f"{base}/mcp.json",
            "description": (
                "MCP server (Streamable HTTP) exposing the five deterministic "
                "site audits (WCAG 2.1 via axe-core, SEO, security headers, "
                "performance, and the bundle) and every worker as tools. "
                "initialize and tools/list are free; tools/call is billed per call."),
            "capabilities": list(mcp_tool_names),
            "representativeQueries": [
                "MCP server with a website accessibility audit tool",
                "MCP tool to check a URL for SEO and security header problems",
                "run WCAG, SEO, security and performance audits from an MCP client",
            ],
            "tags": ["mcp", "wcag", "seo", "security-headers", "performance"],
            "metadata": {"endpoint": f"{base}/mcp", "protocol": "streamable-http"},
            **common,
        },
        {
            "identifier": _identifier(publisher, "agent", "a2a"),
            "displayName": "HubVibe (A2A agent)",
            "type": TYPE_A2A_AGENT_CARD,
            "url": f"{base}/.well-known/agent-card.json",
            "description": (
                "A2A Agent Card: every MCP tool is a skill, served over JSON-RPC "
                "at /a2a in A2A 1.0 and 0.3, paid per call with the a2a-x402 "
                "extension or the usual HTTP payment headers."),
            "capabilities": list(mcp_tool_names),
            "representativeQueries": [
                "A2A agent that sells site audits and dev utilities per call",
                "agent-to-agent API paid with x402 on Base or Solana",
                "A2A skill for WCAG, SEO, web research or LLM calls",
            ],
            "tags": ["a2a", "x402", "pay-per-call", "agents"],
            "metadata": {"endpoint": f"{base}/a2a", "protocol": "jsonrpc"},
            **common,
        },
        {
            "identifier": _identifier(publisher, "api", "openapi"),
            "displayName": f"{display_title} (OpenAPI)",
            "type": TYPE_OPENAPI,
            "url": f"{base}/openapi.json",
            "description": (
                "OpenAPI 3.1 for every route on the node. Each paid route "
                "declares x-payment-info, a request example and a 200 response "
                "schema with example; every /work route's result keys are "
                "typed there."),
            "capabilities": ["OpenAPI", "SiteAudit", "WorkerNetwork"],
            "representativeQueries": [
                "OpenAPI spec for a pay-per-call agent API",
                "REST endpoints with x-payment-info for autonomous agents",
            ],
            "tags": ["openapi", "rest", "http-402"],
            **common,
        },
    ]

    for row in audits:
        path = row["path"]
        name = path.rsplit("/", 1)[-1]
        entries.append({
            "identifier": _identifier(publisher, "audit", name),
            "displayName": f"Site audit: {name}",
            "type": TYPE_HUBVIBE_ROUTE,
            "data": {
                "path": path,
                "method": "POST",
                "url": f"{base}{path}",
                "price_usd": row["price_usd"],
                "payment": "HTTP 402 per call; rails as listed in /.well-known/agent.json",
                "input_schema": audit_input_schema_for(row),
                "output_schema": audit_output_schemas[path],
                "returns": row["returns"],
                "deterministic": True,
            },
            "description": row["description"],
            "capabilities": [f"audit_{name}", _capability_token(f"audit.{name}")],
            "representativeQueries": AUDIT_QUERIES[path],
            "tags": ["audit", name, "deterministic", "pay-per-call"],
            "metadata": {"price_usd": row["price_usd"], "path": path, "method": "POST",
                         "deterministic": True},
            **common,
        })

    live = workers_catalog.live() if workers_catalog is not None else []
    for worker in live:
        entries.append({
            "identifier": _identifier(publisher, "work", worker.name),
            "displayName": f"{worker.title} ({worker.name})",
            "type": TYPE_HUBVIBE_ROUTE,
            "data": {
                "path": worker.path,
                "method": "POST",
                "url": f"{base}{worker.path}",
                "price_usd": worker.price_usd,
                "tier": worker.tier,
                "payment": "HTTP 402 per call; rails as listed in /.well-known/agent.json",
                "input_schema": worker.input_schema,
                "input_example": workers_catalog.example_for(worker),
                "output_schema": worker.output_schema,
                "output_example": worker_contract.output_example(worker),
                "returns": worker.returns,
                "max_seconds": worker.max_seconds,
                "composes": worker.composes,
                "receipt": f"{base}/work/receipts/{{receipt_id}}",
                **({"buyer_note": workers_catalog.buyer_note(worker)}
                   if workers_catalog.buyer_note(worker) else {}),
            },
            "description": worker.description,
            "capabilities": [worker.name, _capability_token(worker.name),
                             *(_capability_token(c) for c in worker.composes)],
            "representativeQueries": worker_contract.REPRESENTATIVE_QUERIES[worker.name],
            "tags": [*worker.tags, worker.tier, "pay-per-call"],
            "metadata": {"price_usd": worker.price_usd, "tier": worker.tier,
                         "path": worker.path, "method": "POST",
                         "compound": bool(worker.composes),
                         "max_seconds": worker.max_seconds},
            **common,
        })

    return {
        "@context": CONTEXT,
        "specVersion": SPEC_VERSION,
        "publisher": publisher,
        "displayName": display_title,
        "entries": entries,
    }
