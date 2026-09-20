"""Raw HTTP fetch: status, headers, body -- no extraction, no rendering.

Where extract.page answers "what does this page say", fetch.raw answers
"what did the server actually send back". Every status code, including a
4xx or 5xx from the target, is a completed result, not a failure.
"""

from ..providers import web
from .extract import validate_url


async def fetch_raw(ctx, payload: dict) -> dict:
    url = validate_url(payload.get("url"))

    async def call(provider):
        return await provider.fetch(url)

    value = await ctx.run("fetch", web.FETCH_PROVIDERS, call, per_attempt_seconds=45)
    return value


SKILLS = {"fetch.raw": fetch_raw}
