"""Places, routes and weather, via Google's own managed Maps Grounding Lite
MCP server -- these are thin pass-throughs to what that server already
validates, not a reimplementation of Maps."""

from .. import runtime
from ..providers import maps_grounding

MAX_QUERY_CHARS = 300


async def places(ctx, payload: dict) -> dict:
    query = (payload.get("query") or "").strip()
    if not query:
        raise runtime.InvalidRequest("`query` is required.")
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is over the {MAX_QUERY_CHARS}-character limit.")
    region_code = payload.get("region_code")
    if region_code is not None and not isinstance(region_code, str):
        raise runtime.InvalidRequest("`region_code`, when given, must be a string.")

    async def call(provider):
        return await provider.search_places(query, region_code=region_code)

    result = await ctx.run("search_places", maps_grounding.PROVIDERS, call, per_attempt_seconds=25)
    return {"query": query, "result": result}


_TRAVEL_MODES = {"DRIVE", "WALK", "BICYCLE", "TRANSIT"}


async def route(ctx, payload: dict) -> dict:
    origin = (payload.get("origin") or "").strip()
    destination = (payload.get("destination") or "").strip()
    if not origin or not destination:
        raise runtime.InvalidRequest("`origin` and `destination` are required.")
    travel_mode = (payload.get("travel_mode") or "DRIVE").strip().upper()
    if travel_mode not in _TRAVEL_MODES:
        raise runtime.InvalidRequest(f"`travel_mode` must be one of {sorted(_TRAVEL_MODES)}.")

    async def call(provider):
        return await provider.compute_routes(origin, destination, travel_mode=travel_mode)

    result = await ctx.run("compute_routes", maps_grounding.PROVIDERS, call, per_attempt_seconds=25)
    return {"origin": origin, "destination": destination, "travel_mode": travel_mode,
           "result": result}


async def weather(ctx, payload: dict) -> dict:
    location = (payload.get("location") or "").strip()
    if not location:
        raise runtime.InvalidRequest("`location` is required.")
    if len(location) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`location` is over the {MAX_QUERY_CHARS}-character limit.")

    async def call(provider):
        return await provider.lookup_weather(location)

    result = await ctx.run("lookup_weather", maps_grounding.PROVIDERS, call, per_attempt_seconds=25)
    return {"location": location, "result": result}


SKILLS = {"maps.places": places, "maps.route": route, "maps.weather": weather}
