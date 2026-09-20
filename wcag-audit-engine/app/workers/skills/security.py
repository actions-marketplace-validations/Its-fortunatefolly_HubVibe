"""Inspect a customer-specified MCP endpoint: auth posture, protocol
version, and which of its tools are not marked read-only."""

from .. import runtime
from ..providers import mcp_probe
from .extract import validate_url


async def mcp_inspect(ctx, payload: dict) -> dict:
    url = validate_url(payload.get("url"))

    async def call(provider):
        return await provider.inspect(url)

    value = await ctx.run("inspect", mcp_probe.PROVIDERS, call, per_attempt_seconds=30)
    if not value["reachable"]:
        raise runtime.TransientProviderError(
            f"{url} did not answer the MCP handshake.", reason="target_unreachable")
    return value


SKILLS = {"security.mcp_inspect": mcp_inspect}
