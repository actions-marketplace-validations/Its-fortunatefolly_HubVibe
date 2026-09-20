"""A failed audit costs the caller nothing, on every rail -- proved at the wire.

"You are charged only for an audit that produced a result" was true for
x402 (settled only after delivery) and false for everyone else: a prepaid
key was debited at authentication and never refunded on a 502, the key an
MPP top-up had just bought was dropped from the 502 body (money taken,
nothing handed back), and an MPP credential stayed marked as spent so the
retry was refused. Each test here breaks the audit engine, drives the REAL
route against the REAL SQLite ledger, and checks what authentication took
has been handed back.

Two neighbours of the same code: a bogus API key must not buy its own
rate-limit bucket, and a non-ASCII key must be a 402, not a 500.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"

TARGET = {"url": "https://example.com"}
PRICE_CENTS = {
    "/audit": 5,
    "/audit/wcag": 5,
    "/audit/seo": 5,
    "/audit/security": 5,
    "/audit/performance": 5,
    "/audit/bundle": 15,
}


def _forget_service_modules():
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)


def _load_main(monkeypatch, tmp_path=None, api_key=None):
    """The real service. With `tmp_path`, on a SQLite key store so prepaid
    keys and top-ups run against the real ledger; never a facilitator, never
    Stripe's network."""
    if tmp_path is not None:
        monkeypatch.setenv("KEY_STORE", "sqlite")
        monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
        # Enough for billing.is_configured(); nothing here reaches Stripe.
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unbill")
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_unbill")
        monkeypatch.setenv("STRIPE_PRICE_PRO", "price_unbill")
    else:
        for var in ("KEY_STORE", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PRO"):
            monkeypatch.delenv(var, raising=False)
    for var in ("X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    if api_key is None:
        monkeypatch.delenv("AUDIT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AUDIT_API_KEY", api_key)

    _forget_service_modules()
    spec = importlib.util.spec_from_file_location("wcag_main_unbill", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _forget_service_modules()
    return module


def _explode(*args, **kwargs):
    raise RuntimeError("target unreachable")


def _break_engine(module, monkeypatch, path):
    """Make the audit behind `path` fail to run, the way an unreachable
    target does."""
    if path in ("/audit", "/audit/wcag"):
        monkeypatch.setattr(module, "_run_axe", _explode)
    elif path == "/audit/seo":
        monkeypatch.setattr(module.audits, "run_seo_audit", _explode)
    elif path == "/audit/security":
        monkeypatch.setattr(module.audits, "run_security_audit", _explode)
    elif path == "/audit/performance":
        monkeypatch.setattr(module.audits, "run_performance_audit", _explode)
    elif path == "/audit/bundle":
        monkeypatch.setattr(module, "_run_axe_and_performance", _explode)
    else:
        raise AssertionError(path)


@pytest.mark.parametrize("path", sorted(PRICE_CENTS))
def test_a_prepaid_key_is_refunded_when_the_audit_fails(monkeypatch, tmp_path, path):
    module = _load_main(monkeypatch, tmp_path)
    _break_engine(module, monkeypatch, path)
    client = TestClient(module.app)
    cents = PRICE_CENTS[path]
    key = module.billing.issue_prepaid_key(cents)  # exactly one call's worth

    response = client.post(path, json=TARGET, headers={"X-API-Key": key})

    assert response.status_code == 502, response.text
    assert response.json()["billed"] is False
    assert "Nothing was charged" in response.json()["detail"]
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == cents, (
        "the debit taken at authentication was not handed back"
    )


def test_the_refunded_key_still_buys_exactly_what_it_holds(monkeypatch, tmp_path):
    """The refund is money, not a flag: after a failure the key buys one
    audit, and the one after that is challenged."""
    module = _load_main(monkeypatch, tmp_path)
    client = TestClient(module.app)
    key = module.billing.issue_prepaid_key(5)

    monkeypatch.setattr(module, "_run_axe", _explode)
    assert client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": key}).status_code == 502

    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    assert client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": key}).status_code == 200
    assert client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": key}).status_code == 402
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 0


def test_a_failed_mcp_tool_call_refunds_the_prepaid_key(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_run_axe", _explode)
    client = TestClient(module.app)
    key = module.billing.issue_prepaid_key(5)

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": TARGET},
        },
        headers={"X-API-Key": key},
    )

    result = response.json()["result"]
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["billed"] is False
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 5


def test_a_topup_key_is_returned_holding_everything_it_bought_when_the_first_audit_fails(
    monkeypatch, tmp_path
):
    """The call that pays the top-up is served out of the credit. If that
    audit fails, the 502 must still carry the key -- the agent has no other
    channel to receive it -- and the key must hold the full amount bought."""
    module = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: 50)
    monkeypatch.setattr(module, "_run_axe", _explode)
    client = TestClient(module.app)

    response = client.post(
        "/audit/wcag", json=TARGET, headers={"Authorization": "Payment topup-credential"}
    )

    assert response.status_code == 502, response.text
    key = response.json().get("api_key")
    assert key, "the key the top-up bought was not returned on the failure"
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 50


def test_an_mpp_credential_is_released_when_the_audit_fails_and_kept_when_it_runs(
    monkeypatch, tmp_path
):
    module = _load_main(monkeypatch, tmp_path)
    released = []
    monkeypatch.setattr(module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda cred, realm=None: True)
    monkeypatch.setattr(module.mpp_payments, "release_credential", released.append)
    client = TestClient(module.app)
    headers = {"Authorization": "Payment per-call-credential"}

    monkeypatch.setattr(module, "_run_axe", _explode)
    assert client.post("/audit/wcag", json=TARGET, headers=headers).status_code == 502
    assert released == ["per-call-credential"]

    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    assert client.post("/audit/wcag", json=TARGET, headers=headers).status_code == 200
    assert released == ["per-call-credential"], "a delivered audit must keep its credential spent"


def test_release_credential_forgets_the_spent_mark_and_never_raises(monkeypatch):
    module = _load_main(monkeypatch)
    mpp = module.mpp_payments
    mpp._used_credentials.update({"spt_spent", "0xtxspent"})
    for payload in ({"spt": "spt_spent"}, {"hash": "0xtxspent"}):
        raw = json.dumps({"challenge": {}, "payload": payload}).encode()
        header = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        mpp.release_credential(header)
    assert "spt_spent" not in mpp._used_credentials
    assert "0xtxspent" not in mpp._used_credentials

    mpp.release_credential("not base64 at all")  # a bad header is a no-op


def test_a_bogus_api_key_does_not_buy_its_own_rate_limit_bucket(monkeypatch):
    """The limiter keys on the presented key. A caller minting a fresh
    bogus key per request used to get a fresh bucket per request -- the
    limiter never saw it. A key that did not authenticate is charged to
    the address it came from."""
    module = _load_main(monkeypatch, api_key="real-key")
    monkeypatch.setattr(
        module, "_audit_limiter", module._SlidingWindowLimiter(limit=1, window_seconds=60.0)
    )
    client = TestClient(module.app)

    first = client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": "bogus-1"})
    assert first.status_code == 402
    second = client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": "bogus-2"})
    assert second.status_code == 429, "a second bogus key walked past the limiter"

    # A key that authenticates never touches the address bucket.
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    real = client.post("/audit/wcag", json=TARGET, headers={"X-API-Key": "real-key"})
    assert real.status_code == 200, real.text


def test_a_non_ascii_api_key_is_challenged_not_crashed(monkeypatch):
    """secrets.compare_digest raises TypeError on a non-ASCII str, and a
    header can carry one. That was a 500 on the paid path for a typo."""
    module = _load_main(monkeypatch, api_key="real-key")
    result = module._authenticate("clé", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402
