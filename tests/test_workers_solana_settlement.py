"""A Solana (SVM) x402 settlement is recorded as paid, by the facilitator's word.

An EVM x402 payload names its payer in the EIP-3009 authorization block; a
Solana payload carries a signed transaction and no such block. The ledger
read the payer only from the authorization, and derived `settled` from the
payer being present -- so the first live Solana-settled call (2026-09-22,
tx prRhBh3n...) was written as payer null / settled 0, and its public
receipt told the buyer `delivered_not_settled`, `paid: false`, while
carrying the transaction hash that proved the opposite.

The payer of an SVM payment is named by the facilitator's settle response.
The router re-reads it after settlement and marks the call settled on the
facilitator's own verdict.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAY_TO = "J1K4mdbvXEbJLRKpgxFcA7s66LWMCYHu71ye6Hz2icSh"
PAYER = "28HFaUbw5ZRmcYiQw89kGDbnDhBFSdL1Lx1dHESguAiw"
TX = "prRhBh3naPBw2gVChvNsagvRpvAwyRoQAzfBVruxAkqKyNsHszAfEvuGQLfsBgeVMmvkh6YeQLzwXRhqi8CxX4y"
# A request without any credential is priced by the node before it reaches
# the worker router (a free 402). A payment header, whatever it holds, is
# what routes the request to the gate -- stubbed here.
PAID = {"PAYMENT-SIGNATURE": "stub", "X-PAYMENT": "stub"}


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
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd")
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_svm", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda v, n: True)
    return module


def _svm_pending():
    """The shape the payment layer hands the router for a Solana payment:
    a payload with a signed transaction and NO authorization block, the
    accepted Solana requirement, and -- after _bill -- the facilitator's
    settle response naming the payer."""
    payload = SimpleNamespace(payload={"transaction": "AQAAAA...base64..."})
    requirement = {"scheme": "exact", "network": SOLANA, "asset": USDC_MINT,
                   "amount": "20000", "payTo": PAY_TO, "maxTimeoutSeconds": 300}
    return SimpleNamespace(payload=payload, requirements=[requirement], price="$0.02",
                           settle_result=None, settle_state=None, settle_error=None)


def _configure(app_module, monkeypatch, pending, settle_ok=True):
    async def works(ctx, payload):
        return {"product_id": "BTC-USD", "price": "1", "source": "coinbase-advanced-trade-public"}

    # A keyless worker, so the route is available on the test box and the
    # request reaches the payment gate (the Google-backed ones answer 503).
    registry = dict(W.router.REGISTRY)
    registry["market.quote"] = works
    monkeypatch.setattr(W.router, "REGISTRY", registry)

    auth = SimpleNamespace(customer_id=None, payment_method="x402", pending_payment=pending,
                           issued_key=None, credit_owed=0, prepaid_key=None, prepaid_cents=0,
                           mpp_credential=None, challenge_host=None, challenge_path=None)

    def bill(auth_, price_usd):
        # What settle_sync leaves on the pending payment once the facilitator
        # has answered: its SettleResponse, which names the payer.
        if settle_ok:
            pending.settle_result = SimpleNamespace(success=True, payer=PAYER, transaction=TX,
                                                    network=SOLANA)
            pending.settle_state = "settled"
        else:
            pending.settle_state = "refused"
        return None

    W.router.configure(
        authorize_and_rate_limit=lambda *a, **k: (auth, None),
        bill=bill,
        deliver=lambda content, auth_: content,
        failed_response=app_module._failed_audit_response,
        node_version="test-svm")


def test_a_solana_settled_call_is_recorded_paid_with_the_facilitators_payer(app_module, monkeypatch):
    pending = _svm_pending()
    _configure(app_module, monkeypatch, pending)
    client = TestClient(app_module.app)

    response = client.post("/work/market/quote", json={"product_id": "BTC-USD"}, headers=PAID)
    assert response.status_code == 200, response.text
    receipt = client.get(response.json()["receipt_url"]).json()

    assert receipt["outcome"] == "paid_delivered"
    assert receipt["paid"] is True and receipt["delivered"] is True
    payment = receipt["payment"]
    assert payment["settled"] is True
    assert payment["payer"] == PAYER
    assert payment["tx_hash"] == TX
    assert payment["network"] == SOLANA
    assert payment["asset"] == USDC_MINT
    assert payment["pay_to"] == PAY_TO
    assert payment["amount_atomic"] == 20000 and payment["amount_usd"] == 0.02
    assert payment["rail"] == "x402"


def test_the_ledger_row_says_settled_with_the_solana_payer(app_module, monkeypatch):
    pending = _svm_pending()
    _configure(app_module, monkeypatch, pending)
    client = TestClient(app_module.app)

    receipt_id = client.post("/work/market/quote", json={"product_id": "BTC-USD"}, headers=PAID).json()["receipt_id"]
    call_id = W.ledger.call_id_for(receipt_id)
    row = W.ledger.receipt_for(call_id)
    assert row["payment"]["settled"] is True and row["payment"]["payer"] == PAYER


def test_an_evm_payer_is_still_read_from_the_authorization_block():
    """The EVM path is unchanged: the authorization's `from` wins, and the
    settle response is only consulted when there is none."""
    evm = SimpleNamespace(payload=SimpleNamespace(payload={"authorization": {"from": "0xabc"}}),
                          settle_result=SimpleNamespace(payer="0xdef"))
    assert W.router._payer_of(SimpleNamespace(pending_payment=evm)) == "0xabc"
    svm = SimpleNamespace(payload=SimpleNamespace(payload={"transaction": "..."}),
                          settle_result=SimpleNamespace(payer=PAYER))
    assert W.router._payer_of(SimpleNamespace(pending_payment=svm)) == PAYER
    unsettled = SimpleNamespace(payload=SimpleNamespace(payload={"transaction": "..."}),
                                settle_result=None)
    assert W.router._payer_of(SimpleNamespace(pending_payment=unsettled)) is None


def test_a_refused_solana_settlement_is_not_recorded_as_paid(app_module, monkeypatch):
    """The facilitator's verdict decides: refused stays unsettled, no payer."""
    pending = _svm_pending()
    _configure(app_module, monkeypatch, pending, settle_ok=False)
    client = TestClient(app_module.app)

    receipt_id = client.post("/work/market/quote", json={"product_id": "BTC-USD"}, headers=PAID).json()["receipt_id"]
    receipt = client.get(f"/work/receipts/{receipt_id}").json()
    assert receipt["payment"]["settled"] is False and receipt["payment"]["payer"] is None
    assert receipt["outcome"] == "delivered_not_settled"
