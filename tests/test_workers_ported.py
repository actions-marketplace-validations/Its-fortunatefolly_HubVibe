"""Tests for the six keyless bees ported from the (now superseded) /svc
catalog: fetch.raw, chain.rpc, market.rates, market.ticker,
prediction.market, prediction.events.

All six are keyless, so unlike the wave-1 Google-backed workers these can be
exercised end to end (402 pricing, Bazaar data) without faking any
credential. What's specific to them and worth its own test beyond the
generic guarantees test_workers_payment_safety.py already covers for every
CATALOG entry:

  * chain.rpc's allowlist and eth_getLogs range cap
  * a JSON-RPC error object is DELIVERED, not raised, for chain.rpc -- the
    one behaviour this worker exists to have that chain.network/address/
    transaction deliberately do not
  * the other five workers' own input validation
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()

    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_ported", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        with_page=module.browser_pool.with_page,
        goto_guarded=getattr(module.audits, "goto_guarded", None))
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


PORTED_ROUTES = {
    "/work/fetch/raw": 0.10,
    "/work/chain/rpc": 0.05,
    "/work/market/rates": 0.02,
    "/work/market/ticker": 0.02,
    "/work/prediction/market": 0.05,
    "/work/prediction/events": 0.05,
}


def test_every_ported_route_prices_and_lists_in_bazaar(client):
    for path, price in PORTED_ROUTES.items():
        body = client.post(path, json={}).json()
        assert body["price_usd"] == price, path
        assert "bazaar" in (body.get("extensions") or {}), path


# --- chain.rpc: allowlist, range cap, error-as-result -----------------------

def test_chain_rpc_rejects_a_method_not_on_the_allowlist():
    """The allowlist itself lives on the provider (shared with chain.network/
    address/transaction's own `rpc()`), so this checks it there directly --
    the skill's own validation only rules out an empty/malformed `method`."""
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.providers.base_rpc.PROVIDERS[0].rpc_raw("eth_sendRawTransaction", []))


def test_chain_rpc_requires_a_method():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.chain.rpc_passthrough(None, {}))


def test_chain_rpc_rejects_an_oversized_getlogs_range():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.providers.base_rpc.PROVIDERS[0].rpc_raw(
            "eth_getLogs", [{"fromBlock": "0x0", "toBlock": hex(10_001)}]))


def test_a_json_rpc_error_is_delivered_as_the_result_not_raised(monkeypatch):
    """The one behaviour that distinguishes chain.rpc from chain.network/
    address/transaction: a revert reason is the chain's authoritative
    answer, not a HubVibe failure."""

    class _FakeCtx:
        def remaining(self):
            return 999

        async def run(self, step, providers, call, **kwargs):
            class _Provider:
                id = "base-rpc:fake"

                def available(self):
                    return True

                async def rpc_raw(self, method, params):
                    return W.runtime.ProviderResult(
                        value={"method": method, "endpoint": "fake",
                              "error": {"code": 3, "message": "execution reverted"}},
                        cost_micros=0, cost_measured=True)

            result = await call(_Provider())
            return result.value

    value = asyncio.run(W.skills.chain.rpc_passthrough(
        _FakeCtx(), {"method": "eth_call", "params": [{}]}))
    assert value["error"]["message"] == "execution reverted"
    assert "result" not in value


# --- the other five workers' own input validation ---------------------------

def test_fetch_raw_rejects_a_blocked_target():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.fetch.fetch_raw(
            None, {"url": "http://169.254.169.254/latest/meta-data/"}))


def test_market_rates_rejects_an_invalid_currency_code():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.market.rates(None, {"currency": "not a currency"}))


def test_market_ticker_rejects_a_malformed_product_id():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.market.ticker(None, {"product_id": "bitcoin"}))


def test_prediction_market_rejects_a_malformed_slug():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.market.prediction_market_by_slug(
            None, {"slug": "Not A Valid Slug!"}))


def test_prediction_events_rejects_an_out_of_range_limit():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.market.prediction_events(None, {"limit": 500}))


# --- payment safety, spot-checked for the newest worker ---------------------

def test_fetch_raw_failure_is_unbilled(app_module, client, monkeypatch):
    async def always_fails(ctx, payload):
        raise W.runtime.TransientProviderError("target unreachable")

    registry = dict(W.router.REGISTRY)
    registry["fetch.raw"] = always_fails
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    billed = []
    monkeypatch.setattr(app_module, "_bill",
                        lambda auth, price_usd: billed.append(price_usd))
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    response = client.post("/work/fetch/raw", headers={"X-API-Key": "test-key"},
                           json={"url": "https://example.com"})
    assert response.status_code == 502
    assert response.json()["billed"] is False
    assert billed == []
