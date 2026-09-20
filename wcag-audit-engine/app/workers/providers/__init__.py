"""Provider adapters: the thinnest possible layer between a HubVibe worker
and somebody else's already-built service.

An adapter's whole job is protocol translation and cost measurement. It never
reimplements the capability: BigQuery does the querying, Vertex does the
inference, Base's RPC does the chain reads, Playwright does the rendering.

Every adapter exposes:
    .id            stable provider id, used in the ledger and the breaker
    .available()   fail-closed: False when this deployment lacks credentials,
                   which keeps an unconfigured capability from ever being
                   advertised or charged for
and one or more async methods returning runtime.ProviderResult.
"""

from . import (  # noqa: F401
    base_rpc, bigquery, code_exec, coinbase_market, completion, gemini, imagen,
    maps_grounding, mcp_probe, polymarket, search_grounding, stt, tts, veo, web,
)

ALL = (gemini, bigquery, base_rpc, coinbase_market, polymarket, web,
      search_grounding, code_exec, imagen, tts, stt, mcp_probe, completion,
      maps_grounding, veo)


def health() -> dict:
    """Per-provider configured/not-configured, for /health and the manifest.

    Deliberately does NOT call the providers: a health endpoint that makes six
    outbound requests becomes the thing that takes the node down.
    """
    out = {}
    for module in ALL:
        for provider in module.PROVIDERS:
            out[provider.id] = {
                "available": provider.available(),
                "detail": provider.unavailable_reason() if not provider.available() else "configured",
            }
    return out
