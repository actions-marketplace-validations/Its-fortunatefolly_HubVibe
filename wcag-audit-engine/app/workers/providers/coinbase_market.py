"""Coinbase Advanced Trade PUBLIC market data.

Connected, not rebuilt. Verified keyless 2026-09-15:
  GET /api/v3/brokerage/market/products/BTC-USD -> price returned, no auth.

Deliberately the PUBLIC market surface only -- no key, no account, no trading
scope. This worker reads prices; it can never place an order, and it shares
no credential with HubVibe's payment path.
"""

import os

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_BASE = os.environ.get("WORKER_COINBASE_BASE", "https://api.coinbase.com")
_TIMEOUT = float(os.environ.get("WORKER_MARKET_TIMEOUT_SECONDS", "20"))


class _CoinbaseMarket:
    id = "coinbase-market"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _get(self, path: str, params: dict = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(f"{_BASE}{path}", params=params or {},
                                            headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Coinbase timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Coinbase unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"Coinbase returned {response.status_code}")
        if response.status_code == 404:
            raise runtime.InvalidRequest("Unknown product id (try e.g. BTC-USD, ETH-USD).")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Coinbase rejected the read ({response.status_code}): {response.text[:160]}")
        return response.json()

    async def product(self, product_id: str) -> runtime.ProviderResult:
        data = await self._get(f"/api/v3/brokerage/market/products/{product_id}")
        if not data.get("product_id"):
            raise runtime.InvalidProviderResponse("Coinbase returned no product.")
        value = {
            "product_id": data.get("product_id"),
            "price": data.get("price"),
            "price_change_24h_pct": data.get("price_percentage_change_24h"),
            "volume_24h": data.get("volume_24h"),
            "volume_change_24h_pct": data.get("volume_percentage_change_24h"),
            "quote_currency": data.get("quote_currency_id"),
            "base_currency": data.get("base_currency_id"),
            "status": data.get("status"),
        }
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"product={product_id}")

    async def candles(self, product_id: str, granularity: str,
                      start: str, end: str) -> runtime.ProviderResult:
        data = await self._get(
            f"/api/v3/brokerage/market/products/{product_id}/candles",
            {"granularity": granularity, "start": start, "end": end})
        candles = data.get("candles") or []
        return runtime.ProviderResult(
            value={"product_id": product_id, "granularity": granularity,
                   "candles": candles, "count": len(candles)},
            cost_micros=0, cost_measured=True, usage=f"candles={len(candles)}")

    async def rates(self, currency: str) -> runtime.ProviderResult:
        data = await self._get("/v2/exchange-rates", {"currency": currency})
        payload = data.get("data") or {}
        if not payload.get("rates"):
            raise runtime.InvalidProviderResponse("Coinbase returned no exchange rates.")
        return runtime.ProviderResult(
            value={"currency": payload.get("currency") or currency, "rates": payload["rates"]},
            cost_micros=0, cost_measured=True, usage=f"currency={currency}")


class _CoinbaseExchange:
    """Coinbase's Exchange market-data host -- a different backend from the
    Advanced Trade API above, which is what makes it a real independent
    source rather than the same outage twice."""

    id = "coinbase-exchange"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def ticker(self, product_id: str) -> runtime.ProviderResult:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(
                    f"https://api.exchange.coinbase.com/products/{product_id}/ticker",
                    headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Coinbase Exchange timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Coinbase Exchange unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"Coinbase Exchange returned {response.status_code}")
        if response.status_code == 404:
            raise runtime.InvalidRequest("Unknown product id (try e.g. BTC-USD, ETH-USD).")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Coinbase Exchange rejected the read ({response.status_code})")
        data = response.json()
        if not data.get("price"):
            raise runtime.InvalidProviderResponse("Coinbase Exchange returned no ticker price.")
        return runtime.ProviderResult(
            value={"product_id": product_id, "price": data.get("price"),
                   "bid": data.get("bid"), "ask": data.get("ask"),
                   "volume": data.get("volume"), "time": data.get("time")},
            cost_micros=0, cost_measured=True, usage=f"product={product_id}")


PROVIDERS = [_CoinbaseMarket()]
PROVIDER = PROVIDERS[0]
RATES_PROVIDERS = [_CoinbaseMarket()]
TICKER_PROVIDERS = [_CoinbaseExchange()]
