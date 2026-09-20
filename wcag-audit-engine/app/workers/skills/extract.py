"""Web extraction worker: a URL in, clean structured content out."""

from urllib.parse import urlparse

from .. import runtime
from ..providers import web


def validate_url(raw, field: str = "url") -> str:
    """Shared by every worker that takes a URL, so they refuse the same
    things in the same words."""
    if not raw or not isinstance(raw, str):
        raise runtime.InvalidRequest(f"`{field}` is required and must be a string.")
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise runtime.InvalidRequest(f"`{field}` must be an http(s) URL.")
    if not parsed.netloc:
        raise runtime.InvalidRequest(f"`{field}` has no host.")
    # The same guard the audit routes apply, run here so the refusal is FREE:
    # raised before the payment gate, the caller is never charged and their
    # x402 nonce is never burned on a URL we were always going to refuse.
    problem = web.target_problem(url)
    if problem:
        raise runtime.InvalidRequest(f"`{field}` {problem}.")
    return url


async def extract_page(ctx, payload: dict) -> dict:
    """Fetch one page and return its readable content.

    Tries the real browser first so JS-rendered pages extract correctly, and
    falls back to a plain fetch -- which is both cheaper and the path that
    still works when Chromium is under memory pressure on a small box.
    """
    url = validate_url(payload.get("url"))

    async def call(provider):
        return await provider.extract(url)

    value = await ctx.run("extract", web.PROVIDERS, call, per_attempt_seconds=60)
    return {
        "url": value["url"],
        "final_url": value.get("final_url"),
        "title": value.get("title"),
        "description": value.get("description"),
        "text": value["text"],
        "text_chars": value["text_chars"],
        "truncated": value.get("truncated", False),
        "links": value.get("links") or [],
        "javascript_rendered": value.get("rendered", False),
    }


SKILLS = {"extract.page": extract_page}
