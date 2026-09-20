"""Base L2 chain reads over public JSON-RPC.

Connected, not rebuilt: the chain is the chain. This is an RPC client with a
User-Agent and a fallback endpoint.

THE USER-AGENT IS LOAD-BEARING. Measured 2026-09-14: the public Base RPCs
began answering 403 to requests without one. The repo's paying scripts hit
this first; the same rule applies to every outbound chain read here.

This is READ-ONLY and deliberately so. It is a data worker; it never signs,
never sends value, and shares nothing with the payment path. HubVibe's money
moves only through the existing x402/Coinbase code, which this cannot reach.
"""

import os
import re
from typing import Optional

import httpx

from .. import runtime

USER_AGENT = os.environ.get("WORKER_HTTP_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")

_ENDPOINTS = [e.strip() for e in os.environ.get(
    "WORKER_BASE_RPC_URLS",
    "https://mainnet.base.org,https://base-rpc.publicnode.com").split(",") if e.strip()]

_TIMEOUT = float(os.environ.get("WORKER_RPC_TIMEOUT_SECONDS", "20"))

# Read-only methods only. An allowlist rather than a denylist: a new RPC
# method should have to be considered before a customer can reach it.
ALLOWED_METHODS = {
    "eth_blockNumber", "eth_getBalance", "eth_getTransactionByHash",
    "eth_getTransactionReceipt", "eth_getCode", "eth_getStorageAt",
    "eth_getTransactionCount", "eth_call", "eth_getBlockByNumber",
    "eth_getBlockByHash", "eth_gasPrice", "eth_chainId", "eth_getLogs",
    "eth_estimateGas", "eth_feeHistory", "eth_maxPriorityFeePerGas",
    "eth_getBlockTransactionCountByNumber", "net_version", "web3_clientVersion",
}

# eth_getLogs against a public node with no range limit can be refused by the
# node anyway, but capping it here turns that into a clean 400 before payment
# rather than a provider timeout after it.
LOG_RANGE_CAP_BLOCKS = 10_000
_RATE_LIMIT_MESSAGE = re.compile(r"rate.?limit|limit exceeded|too many request", re.I)


class _BaseRpc:
    def __init__(self, url: str):
        self.url = url
        host = url.split("//")[-1].split("/")[0]
        self.id = f"base-rpc:{host}"

    def available(self) -> bool:
        return True  # keyless public endpoint

    def unavailable_reason(self) -> str:
        return ""

    async def rpc_raw(self, method: str, params: Optional[list] = None) -> runtime.ProviderResult:
        """Like `rpc`, but a JSON-RPC error object is delivered as the
        RESULT rather than raised -- a revert reason or "block not found" is
        the chain's authoritative answer to exactly this call, and a caller
        asking for a raw passthrough is asking to see that, not have it
        turned into an HTTP failure. A rate-limit-shaped error is the one
        exception: that is the upstream refusing US, not the chain
        answering, so it still raises to trigger fallback."""
        if method not in ALLOWED_METHODS:
            raise runtime.InvalidRequest(
                f"`{method}` is not one of this worker's read-only methods: "
                f"{', '.join(sorted(ALLOWED_METHODS))}")
        if method == "eth_getLogs" and params and isinstance(params[0], dict):
            try:
                from_block, to_block = params[0].get("fromBlock"), params[0].get("toBlock")
                # A blockHash filter addresses exactly one block, so no range
                # applies and nothing needs capping.
                if not params[0].get("blockHash"):
                    hex_bounds = (
                        isinstance(from_block, str) and from_block.startswith("0x")
                        and isinstance(to_block, str) and to_block.startswith("0x"))
                    if not hex_bounds:
                        # A tag ("latest", "earliest") or an omitted bound makes
                        # the span uncomputable, so the old check SKIPPED it --
                        # meaning {"fromBlock":"0x0","toBlock":"latest"} asked the
                        # node for all of chain history and timed out after the
                        # caller had paid. Refuse it for free instead.
                        raise runtime.InvalidRequest(
                            "eth_getLogs needs explicit hex fromBlock and toBlock "
                            f"(a tag like 'latest' cannot be bounded against the "
                            f"{LOG_RANGE_CAP_BLOCKS}-block limit), or a blockHash.")
                    # Inclusive range: from..to spans (to - from + 1) blocks.
                    if int(to_block, 16) - int(from_block, 16) + 1 > LOG_RANGE_CAP_BLOCKS:
                        raise runtime.InvalidRequest(
                            f"eth_getLogs block range exceeds the "
                            f"{LOG_RANGE_CAP_BLOCKS}-block limit.")
            except ValueError:
                raise runtime.InvalidRequest(
                    "eth_getLogs fromBlock/toBlock are not parseable hex quantities.")

        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    self.url, json=payload,
                    headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{self.id} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{self.id} unreachable: {exc}") from exc

        if response.status_code in (403, 429):
            raise runtime.TransientProviderError(
                f"{self.id} refused the read ({response.status_code}); "
                "public RPCs rate-limit by address.", reason="provider_rate_limited")
        if response.status_code >= 400:
            raise runtime.TransientProviderError(f"{self.id} returned {response.status_code}")

        data = response.json()
        if not isinstance(data, dict) or ("result" not in data and "error" not in data):
            raise runtime.InvalidProviderResponse(f"{self.id} returned a non-JSON-RPC response.")

        value = {"method": method, "endpoint": self.url}
        if "error" in data and data["error"] is not None:
            error = data["error"]
            message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
            if _RATE_LIMIT_MESSAGE.search(message):
                raise runtime.TransientProviderError(
                    f"{self.id} rate-limited: {message[:120]}", reason="provider_rate_limited")
            value["error"] = error
        else:
            value["result"] = data.get("result")
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True, usage=method)

    async def rpc(self, method: str, params: Optional[list] = None) -> runtime.ProviderResult:
        if method not in ALLOWED_METHODS:
            raise runtime.InvalidRequest(
                f"`{method}` is not one of this worker's read-only methods: "
                f"{', '.join(sorted(ALLOWED_METHODS))}")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    self.url, json=payload,
                    headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{self.id} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{self.id} unreachable: {exc}") from exc

        if response.status_code in (403, 429):
            raise runtime.TransientProviderError(
                f"{self.id} refused the read ({response.status_code}); "
                "public RPCs rate-limit by address.", reason="provider_rate_limited")
        if response.status_code >= 400:
            raise runtime.TransientProviderError(f"{self.id} returned {response.status_code}")

        data = response.json()
        if "error" in data:
            message = (data.get("error") or {}).get("message", "unknown")
            # An RPC-level error is the node telling us the CALL is wrong;
            # another endpoint will say the same thing.
            raise runtime.PermanentProviderError(f"RPC error: {message}")
        if "result" not in data:
            raise runtime.InvalidProviderResponse("RPC answered without a result.")

        # Free public endpoint: cost is genuinely zero, and measured as such.
        return runtime.ProviderResult(
            value={"method": method, "result": data["result"], "endpoint": self.url},
            cost_micros=0, cost_measured=True, usage=method)


PROVIDERS = [_BaseRpc(u) for u in _ENDPOINTS]
