"""Google Maps Platform, via the Google-managed Maps Grounding Lite MCP
server -- not rebuilt, connected. Google runs `mapstools.googleapis.com`;
this is an MCP client for the three tools it exposes (search_places,
compute_routes, lookup_weather), the same "one call, one JSON-RPC method"
shape mcp_probe.py already speaks to inspect a THIRD PARTY's server.

TWO WAYS TO AUTHENTICATE, NEITHER ASSUMED. Google documents both an API key
(`X-Goog-Api-Key`, MAPS_GROUNDING_LITE_API_KEY) and OAuth with the scope
`https://www.googleapis.com/auth/maps-platform.mapstools`
(developers.google.com/maps/ai/grounding-lite). The OAuth path reuses this
package's one Google credential re-scoped (google_auth.scoped_headers), so no
second secret is needed. Verified on the box's service account 2026-09-19:
`cloud-platform` alone lists tools but every tools/call is 403 "insufficient
authentication scopes"; with the mapstools scope all three tools answer.
Ambient Cloud Shell credentials cannot be re-scoped at all. So the OAuth path is used only when the operator sets
WORKER_MAPS_ADC=1 after a real call succeeded on that deployment. Until one
of the two is set, these workers stay unavailable and are never advertised.
"""

import json
import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth
from .base_rpc import USER_AGENT

_ENDPOINT = os.environ.get("WORKER_MAPS_GROUNDING_URL", "https://mapstools.googleapis.com/mcp")
_TIMEOUT = float(os.environ.get("WORKER_MAPS_TIMEOUT_SECONDS", "30"))
_MAPS_SCOPE = "https://www.googleapis.com/auth/maps-platform.mapstools"


def _api_key() -> str:
    return os.environ.get("MAPS_GROUNDING_LITE_API_KEY", "").strip()


def _adc_enabled() -> bool:
    return os.environ.get("WORKER_MAPS_ADC") == "1"


class _MapsGroundingLite:
    id = "maps-grounding-lite"

    def available(self) -> bool:
        if _api_key():
            return True
        return _adc_enabled() and google_auth.configured()

    def unavailable_reason(self) -> str:
        if _adc_enabled():
            return google_auth.unavailable_reason()
        return ("neither MAPS_GROUNDING_LITE_API_KEY nor WORKER_MAPS_ADC=1 is set "
                "(the Maps Grounding Lite API is enabled on the project)")

    async def _auth_headers(self) -> dict:
        if _api_key():
            return {"X-Goog-Api-Key": _api_key()}
        scoped = await google_auth.scoped_headers(_MAPS_SCOPE)
        return {"Authorization": scoped["Authorization"],
                "X-Goog-User-Project": scoped["X-Goog-User-Project"]}

    async def _call_tool(self, tool: str, arguments: dict) -> dict:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(await self._auth_headers())
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(_ENDPOINT, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Maps Grounding Lite timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Maps Grounding Lite unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise runtime.ProviderUnavailable(
                "Maps Grounding Lite refused the credential (API key restriction, or "
                "an OAuth token without the maps-platform.mapstools scope).")
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Maps Grounding Lite returned {response.status_code}",
                reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Maps Grounding Lite rejected the call ({response.status_code}): "
                f"{response.text[:200]}")

        data = self._parse(response)
        if data is None:
            raise runtime.InvalidProviderResponse("Maps Grounding Lite answered without JSON.")
        if "error" in data and data["error"] is not None:
            message = (data["error"] or {}).get("message", "unknown error")
            raise runtime.PermanentProviderError(f"Maps Grounding Lite: {message}")

        result = data.get("result") or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in (result.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"]
            raise runtime.PermanentProviderError(
                "Maps Grounding Lite: " + (" ".join(texts) or "tool call failed"))
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        # Fall back to the text content blocks every MCP tool result carries.
        texts = [c.get("text", "") for c in (result.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return {"text": "\n".join(texts)}
        raise runtime.InvalidProviderResponse("Maps Grounding Lite returned an empty result.")

    def _parse(self, response: httpx.Response) -> Optional[dict]:
        try:
            if "text/event-stream" in (response.headers.get("content-type") or ""):
                last = None
                for line in response.text.splitlines():
                    if line.startswith("data:"):
                        last = line[len("data:"):].strip()
                return json.loads(last) if last else None
            return response.json()
        except Exception:
            return None

    async def search_places(self, text_query: str, region_code: Optional[str] = None
                            ) -> runtime.ProviderResult:
        args = {"text_query": text_query}
        if region_code:
            args["region_code"] = region_code
        result = await self._call_tool("search_places", args)
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"query={text_query[:40]}")

    async def compute_routes(self, origin: str, destination: str,
                             travel_mode: str = "DRIVE") -> runtime.ProviderResult:
        # origin/destination are Waypoints -- an object carrying one of
        # address / lat_lng / place_id -- exactly as lookup_weather's location
        # below. A bare string 400s every call (Google's MCP reference).
        result = await self._call_tool("compute_routes", {
            "origin": {"address": origin}, "destination": {"address": destination},
            "travel_mode": travel_mode})
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"{origin[:20]}->{destination[:20]}")

    async def lookup_weather(self, location: str) -> runtime.ProviderResult:
        result = await self._call_tool("lookup_weather", {"location": {"address": location}})
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"location={location[:40]}")


PROVIDERS = [_MapsGroundingLite()]
