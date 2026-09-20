"""Base chain workers: read-only on-chain lookups, shaped for an agent.

These never sign or send value. HubVibe's money moves only through the
existing x402/Coinbase code, which this cannot reach.
"""

import re

from .. import runtime
from ..providers import base_rpc

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TXHASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_WEI_PER_ETH = 10 ** 18


def _hex_to_int(value) -> int:
    if value in (None, "0x", ""):
        return 0
    try:
        return int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise runtime.InvalidProviderResponse(f"Chain returned a non-numeric value: {value!r}")


async def _rpc(ctx, step: str, method: str, params: list):
    async def call(provider):
        return await provider.rpc(method, params)

    value = await ctx.run(step, base_rpc.PROVIDERS, call, per_attempt_seconds=20)
    return value["result"]


async def address_report(ctx, payload: dict) -> dict:
    """Everything an agent usually wants about one Base address in one call.

    Three RPC reads (balance, transaction count, code) folded into a single
    answer, including whether the address is a contract -- the question a
    caller otherwise has to know to derive from `eth_getCode` themselves.
    """
    address = (payload.get("address") or "").strip()
    if not _ADDRESS.match(address):
        raise runtime.InvalidRequest("`address` must be a 0x-prefixed 40-hex-character address.")

    balance_hex = await _rpc(ctx, "balance", "eth_getBalance", [address, "latest"])
    nonce_hex = await _rpc(ctx, "nonce", "eth_getTransactionCount", [address, "latest"])
    code = await _rpc(ctx, "code", "eth_getCode", [address, "latest"])

    wei = _hex_to_int(balance_hex)
    is_contract = bool(code) and code not in ("0x", "0x0")
    return {
        "address": address,
        "network": "base-mainnet",
        "balance_wei": str(wei),
        "balance_eth": round(wei / _WEI_PER_ETH, 18),
        "transaction_count": _hex_to_int(nonce_hex),
        "is_contract": is_contract,
        "code_size_bytes": max(0, (len(code) - 2) // 2) if is_contract else 0,
    }


async def transaction(ctx, payload: dict) -> dict:
    """One transaction, with its receipt folded in so success/failure and gas
    actually used come back in the same answer."""
    tx_hash = (payload.get("hash") or payload.get("tx_hash") or "").strip()
    if not _TXHASH.match(tx_hash):
        raise runtime.InvalidRequest("`hash` must be a 0x-prefixed 64-hex-character hash.")

    tx = await _rpc(ctx, "tx", "eth_getTransactionByHash", [tx_hash])
    if tx is None:
        raise runtime.InvalidRequest(
            "No such transaction on Base mainnet (it may be pending, or on another chain).")
    receipt = await _rpc(ctx, "receipt", "eth_getTransactionReceipt", [tx_hash])

    value_wei = _hex_to_int(tx.get("value"))
    result = {
        "hash": tx_hash,
        "network": "base-mainnet",
        "from": tx.get("from"),
        "to": tx.get("to"),
        "value_wei": str(value_wei),
        "value_eth": round(value_wei / _WEI_PER_ETH, 18),
        "block_number": _hex_to_int(tx.get("blockNumber")) if tx.get("blockNumber") else None,
        "mined": tx.get("blockNumber") is not None,
    }
    if receipt:
        result.update({
            "status": "success" if _hex_to_int(receipt.get("status")) == 1 else "failed",
            "gas_used": _hex_to_int(receipt.get("gasUsed")),
            "log_count": len(receipt.get("logs") or []),
        })
    else:
        result["status"] = "pending"
    return result


async def network_state(ctx, payload: dict) -> dict:
    """Current head and gas price -- the cheap liveness read agents poll."""
    block_hex = await _rpc(ctx, "block", "eth_blockNumber", [])
    gas_hex = await _rpc(ctx, "gas", "eth_gasPrice", [])
    gas_wei = _hex_to_int(gas_hex)
    return {
        "network": "base-mainnet",
        "block_number": _hex_to_int(block_hex),
        "gas_price_wei": str(gas_wei),
        "gas_price_gwei": round(gas_wei / 1e9, 6),
    }


async def rpc_passthrough(ctx, payload: dict) -> dict:
    """A generic allowlisted JSON-RPC read: your method, your params. A
    JSON-RPC error object (a revert reason, "block not found") comes back
    as the RESULT, not a failure -- that is the chain's own answer to
    exactly this call."""
    method = (payload.get("method") or "").strip()
    if not method:
        raise runtime.InvalidRequest("`method` is required.")
    params = payload.get("params")
    if params is None:
        params = []
    if not isinstance(params, list):
        raise runtime.InvalidRequest("`params`, when given, must be a list.")

    async def call(provider):
        return await provider.rpc_raw(method, params)

    return await ctx.run("rpc", base_rpc.PROVIDERS, call, per_attempt_seconds=20)


SKILLS = {
    "chain.address": address_report,
    "chain.transaction": transaction,
    "chain.network": network_state,
    "chain.rpc": rpc_passthrough,
}
