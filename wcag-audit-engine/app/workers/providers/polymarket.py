"""Polymarket prediction-market data via the public Gamma API.

Connected, not rebuilt. Verified keyless 2026-09-15: a live market with
outcome prices came back with no credential.

Read-only market DATA. This worker does not trade, hold positions, or take
any action in a market; it reports what a market currently implies.
"""

import json
import os
from typing import Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_BASE = os.environ.get("WORKER_POLYMARKET_BASE", "https://gamma-api.polymarket.com")
_TIMEOUT = float(os.environ.get("WORKER_MARKET_TIMEOUT_SECONDS", "20"))


def _as_list(raw):
    """Gamma returns these as JSON-encoded STRINGS, not arrays -- passing the
    raw value through would hand the buyer a string where the schema promises
    a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [raw]
    return []


class _Polymarket:
    id = "polymarket"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _get(self, path: str, params: dict) -> list:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(f"{_BASE}{path}", params=params,
                                            headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Polymarket timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Polymarket unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"Polymarket returned {response.status_code}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Polymarket rejected the read ({response.status_code})")
        data = response.json()
        return data if isinstance(data, list) else [data]

    def _shape(self, market: dict) -> dict:
        outcomes = _as_list(market.get("outcomes"))
        prices = _as_list(market.get("outcomePrices"))
        implied = []
        for index, outcome in enumerate(outcomes):
            probability = None
            if index < len(prices):
                try:
                    probability = round(float(prices[index]) * 100, 2)
                except (TypeError, ValueError):
                    probability = None
            implied.append({"outcome": outcome, "probability_pct": probability})
        return {
            "id": market.get("id"),
            "question": market.get("question"),
            "slug": market.get("slug"),
            "active": market.get("active"),
            "closed": market.get("closed"),
            "end_date": market.get("endDate"),
            "volume": market.get("volume"),
            "liquidity": market.get("liquidity"),
            "implied_probabilities": implied,
        }

    async def search(self, query: Optional[str] = None, limit: int = 10,
                     active_only: bool = True) -> runtime.ProviderResult:
        params = {"limit": max(1, min(int(limit), 50)), "order": "volume",
                  "ascending": "false"}
        if active_only:
            params.update({"active": "true", "closed": "false"})
        markets = await self._get("/markets", params)
        shaped = [self._shape(m) for m in markets if isinstance(m, dict)]
        if query:
            needle = query.lower()
            matched = [m for m in shaped
                       if needle in str(m.get("question", "")).lower()
                       or needle in str(m.get("slug", "")).lower()]
            # Falling back to the unfiltered set would silently answer a
            # different question than the one asked.
            shaped = matched
        return runtime.ProviderResult(
            value={"query": query, "markets": shaped, "count": len(shaped)},
            cost_micros=0, cost_measured=True, usage=f"markets={len(shaped)}")


    async def by_slug(self, slug: str) -> runtime.ProviderResult:
        # `closed` must be sent explicitly: without it the list endpoint can
        # return a stale closed market for a slug that has since been reused,
        # so the caller is billed for the wrong market's prices.
        markets = await self._get("/markets", {"slug": slug, "closed": "false"})
        shaped = [self._shape(m) for m in markets if isinstance(m, dict)]
        if not shaped:
            raise runtime.InvalidRequest(f"No market found for slug '{slug}'.")
        return runtime.ProviderResult(
            value={"slug": slug, "market": shaped[0]},
            cost_micros=0, cost_measured=True, usage=f"slug={slug}")

    async def events(self, limit: int = 10, active_only: bool = True) -> runtime.ProviderResult:
        params = {"limit": max(1, min(int(limit), 50)), "order": "volume", "ascending": "false"}
        if active_only:
            params.update({"active": "true", "closed": "false"})
        data = await self._get("/events", params)
        events = [
            {"id": e.get("id"), "title": e.get("title"), "slug": e.get("slug"),
             "volume": e.get("volume"), "end_date": e.get("endDate"),
             "market_count": len(e.get("markets") or [])}
            for e in data if isinstance(e, dict)
        ]
        return runtime.ProviderResult(
            value={"events": events, "count": len(events)},
            cost_micros=0, cost_measured=True, usage=f"events={len(events)}")


PROVIDERS = [_Polymarket()]
PROVIDER = PROVIDERS[0]
