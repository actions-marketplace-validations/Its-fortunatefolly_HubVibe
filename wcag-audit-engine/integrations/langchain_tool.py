"""LangChain / CrewAI tool wrapper for HubVibe's /audit/bundle endpoint.

Usage (LangChain):
    from integrations.langchain_tool import hubvibe_audit_bundle
    agent = initialize_agent(tools=[hubvibe_audit_bundle], ...)

Usage (CrewAI): recent CrewAI versions accept LangChain @tool-decorated
functions directly in an Agent's `tools=[...]` list, so the same import
works there too -- no separate CrewAI-specific wrapper needed.

Auth: set HUBVIBE_API_KEY (a Stripe-issued or internal key) as an
environment variable. This wrapper only supports the X-API-Key path --
x402/MPP machine payments require constructing and signing a real
payment per call, which is out of scope for a thin tool wrapper. An
agent that should pay per-call instead of holding a subscription key
should use the `x402` or `mppx` client libraries directly against
/.well-known/agent.json's advertised endpoints.

This wrapper does not swallow payment/auth errors -- a 402 response
raises with the actual price/challenge details in the message, rather
than returning a result that looks like a completed (but empty) audit.
"""

import os

import httpx
from langchain_core.tools import tool

HUBVIBE_BASE_URL = os.environ.get(
    "HUBVIBE_BASE_URL", "https://hubvibe-io.com"
)


@tool
def hubvibe_audit_bundle(url: str) -> dict:
    """Run HubVibe's full site-compliance bundle (WCAG accessibility, SEO,
    security headers, and performance) against a live URL.

    Costs $0.15 per call, billed to the configured HUBVIBE_API_KEY. Returns
    a dict with `pass` (bool, true only if every dimension passed) and
    per-dimension results under `wcag`, `seo`, `security`, and
    `performance`, each with its own `pass` and `findings`.
    """
    api_key = os.environ.get("HUBVIBE_API_KEY")
    if not api_key:
        raise RuntimeError("Set HUBVIBE_API_KEY before calling hubvibe_audit_bundle")

    response = httpx.post(
        f"{HUBVIBE_BASE_URL}/audit/bundle",
        json={"url": url},
        headers={"X-API-Key": api_key},
        timeout=60.0,
    )
    if response.status_code == 402:
        raise RuntimeError(
            f"Payment required: {response.json()}. Set a valid HUBVIBE_API_KEY, "
            "or use the x402/mppx client libraries to pay per-call instead."
        )
    response.raise_for_status()
    return response.json()
