"""The SQLite key store must hold money as safely as Firestore did.

The store's one dangerous job is the prepaid ledger: a balance is the
payment itself, so a race that lets two calls both spend the same cents is
theft from the operator, and an overdraft is theft from the caller. These
tests drive billing.py's REAL functions against the real SQLite file --
no fakes -- because the shim exists precisely so billing keeps one code
path, and a test that faked the shim would prove nothing about it.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BILLING_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "billing.py"


def _load_billing(monkeypatch, tmp_path, backend="sqlite"):
    monkeypatch.setenv("KEY_STORE", backend)
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    # A Stripe key so is_configured-style gates don't matter here; these
    # tests never reach Stripe.
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_keystore")
    name = "billing_sqlite_under_test"
    sys.modules.pop(name, None)
    sys.modules.pop("wcag_audit_engine_keystore_sqlite", None)
    spec = importlib.util.spec_from_file_location(name, BILLING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_issue_lookup_and_spend_round_trip(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(50)

    record = billing.lookup_key(key)
    assert record is not None
    assert record["prepaid_balance_cents"] == 50
    assert record["active"] is True
    assert record["customer_id"] is None

    assert billing.spend_prepaid(key, 3) is True
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 47


def test_the_balance_stops_at_zero_and_never_overdrafts(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(10)
    assert billing.spend_prepaid(key, 10) is True
    assert billing.spend_prepaid(key, 1) is False
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 0

    short = billing.issue_prepaid_key(5)
    assert billing.spend_prepaid(short, 10) is False, "a partial debit charges without serving"
    assert billing.lookup_key(short)["prepaid_balance_cents"] == 5


def test_a_debit_touches_only_the_balance(monkeypatch, tmp_path):
    """transaction.update writes ONE field. A store that replaced the whole
    document on update would drop `active` with the first debit, and every
    later spend of a genuinely funded key would be refused -- a paying
    caller turned away by bookkeeping."""
    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(9)
    assert billing.spend_prepaid(key, 3) is True
    record = billing.lookup_key(key)
    assert record["active"] is True, "the debit destroyed the rest of the record"
    assert record["customer_id"] is None
    assert billing.spend_prepaid(key, 3) is True, "the second spend of a funded key was refused"
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 3


def test_an_unknown_key_spends_nothing(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    assert billing.spend_prepaid("never-issued", 3) is False
    assert billing.lookup_key("never-issued") is None


def test_concurrent_debits_cannot_double_spend(monkeypatch, tmp_path):
    """The reason the store is transactional at all. Sixteen threads race to
    spend a balance that only covers half of them; exactly half may win.
    With DEFERRED transactions (or none) several threads read the same
    balance and all succeed -- proved by mutation: dropping BEGIN IMMEDIATE
    turns this red."""
    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(24)  # 8 x 3 cents

    results = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker():
        start.wait()
        won = billing.spend_prepaid(key, 3)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 8, f"{sum(results)} of 16 debits succeeded on a balance covering 8"
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 0


def test_quota_counts_and_caps_per_calendar_month(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    monkeypatch.setattr(billing, "PLAN_MONTHLY_QUOTA", {"pro": 3})
    for _ in range(3):
        assert billing.check_and_increment_quota("cus_1", plan="pro") is True
    assert billing.check_and_increment_quota("cus_1", plan="pro") is False
    # A different customer has their own counter.
    assert billing.check_and_increment_quota("cus_2", plan="pro") is True


def test_activation_is_idempotent_across_webhook_retries(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    session = {"customer": "cus_9", "metadata": {"plan": "agency"}}
    first = billing.activate_customer(session)
    second = billing.activate_customer(session)
    assert first == second, "a retried webhook minted a second key"
    assert billing.lookup_key(first)["plan"] == "agency"


def test_reports_survive_the_round_trip(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path)
    billing.save_report("sess_1", "https://example.com", {"pass": True})
    report = billing.load_report("sess_1")
    assert report["url"] == "https://example.com"
    assert report["result"] == {"pass": True}
    assert billing.load_report("missing") is None


def test_the_store_survives_a_process_restart(monkeypatch, tmp_path):
    """The file is the record. A key issued before a restart must still be
    spendable after one -- that is the whole point of not keeping this in
    memory."""
    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(9)
    billing2 = _load_billing(monkeypatch, tmp_path)
    assert billing2.spend_prepaid(key, 9) is True
    assert billing2.lookup_key(key)["prepaid_balance_cents"] == 0


def test_an_unknown_backend_is_refused_not_guessed(monkeypatch, tmp_path):
    billing = _load_billing(monkeypatch, tmp_path, backend="mongodb")
    with pytest.raises(ValueError):
        billing._firestore()


def test_firestore_stays_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("KEY_STORE", raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_keystore")
    name = "billing_default_backend"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, BILLING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    assert module._KEY_STORE_BACKEND == "firestore"


def test_snapshot_get_raises_on_a_missing_field_like_firestore(monkeypatch, tmp_path):
    """billing.check_and_increment_quota relies on Firestore's contract that
    DocumentSnapshot.get raises KeyError for an absent field; a shim that
    returned None would turn `count >= quota` into a TypeError swallowed by
    the fail-open handler -- quota silently never enforced."""
    billing = _load_billing(monkeypatch, tmp_path)
    store = billing._firestore()
    store.collection("quota_usage").document("x").set({"period": "2026-09"})
    snapshot = store.collection("quota_usage").document("x").get()
    with pytest.raises(KeyError):
        snapshot.get("count")
    assert snapshot.get("period") == "2026-09"


def test_the_whole_http_path_spends_a_prepaid_key_out_of_sqlite(monkeypatch, tmp_path):
    """Wire-level proof for the off-Google deployment: a key issued into the
    SQLite store buys real calls through the real route, and an empty key is
    challenged with a 402 -- exactly the behaviour the Firestore deployment
    has, on a box with no Google credentials at all."""
    import importlib.util

    from fastapi.testclient import TestClient

    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    # Enough Stripe configuration that billing.is_configured() is True and
    # the key path runs; nothing here ever reaches Stripe's API.
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_wire")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_wire")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_wire")
    monkeypatch.delenv("AUDIT_API_KEY", raising=False)

    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)
    spec = importlib.util.spec_from_file_location(
        "wcag_main_sqlite_wire", REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)

    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    client = TestClient(module.app)

    key = module.billing.issue_prepaid_key(10)  # exactly two $0.05 calls
    for _ in range(2):
        response = client.post(
            "/audit/wcag", json={"url": "https://example.com"}, headers={"X-API-Key": key}
        )
        assert response.status_code == 200, response.text
    # The third call finds an empty balance and is challenged, not served.
    response = client.post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-API-Key": key}
    )
    assert response.status_code == 402
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 0


def test_a_prepaid_key_spends_without_a_sellable_subscription(monkeypatch, tmp_path):
    """The MPP top-up mints prepaid keys using STRIPE_SECRET_KEY and a network
    profile. It needs no webhook secret and no subscription Price, which is
    exactly what billing.is_configured() demands.

    Gating the SPEND on is_configured() therefore sold a $0.50 key on a
    top-up-only box and refused every call made with it: the customer paid and
    got nothing. Spending is a question for the key store, not for whether
    Stripe could sell a subscription.
    """
    from fastapi.testclient import TestClient

    billing = _load_billing(monkeypatch, tmp_path)
    key = billing.issue_prepaid_key(50)
    assert billing.is_configured() is False, "fixture must model a top-up-only box"
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 50

    import test_wcag_audit_engine as engine_tests

    module = engine_tests._load_main(monkeypatch)
    monkeypatch.setattr(module, "billing", billing)
    client = TestClient(module.app)

    # /audit/seo with raw html is the one paid route that needs no browser,
    # so this measures the charge rather than the sandbox's Chromium.
    response = client.post(
        "/audit/seo",
        json={"html": "<html lang='en'><head><title>t</title></head><body><h1>x</h1></body></html>"},
        headers={"X-API-Key": key},
    )
    assert response.status_code != 402, (
        "a funded prepaid key was refused on a box that cannot sell subscriptions"
    )
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 45, "the call was not charged"


def test_a_topup_credits_the_key_you_already_have(monkeypatch, tmp_path):
    """Setting up payment should happen once. A top-up that mints a NEW key
    turns every refill into a re-setup: the CI pipeline has to rotate the
    secret it stored, and whatever was left on the old key is stranded because
    nothing else can ever spend it. Presenting a key tops that key up."""
    from fastapi.testclient import TestClient

    billing = _load_billing(monkeypatch, tmp_path)
    # 1 cent left: too little for a $0.05 call, so the key path falls through
    # to the top-up. That is the refill moment, and the moment the residual
    # used to be stranded on a key nothing could spend again.
    key = billing.issue_prepaid_key(1)

    import test_wcag_audit_engine as engine_tests

    module = engine_tests._load_main(monkeypatch)
    monkeypatch.setattr(module, "billing", billing)
    # A $0.50 top-up settles; the call it pays for is $0.03.
    monkeypatch.setattr(module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: 50)

    client = TestClient(module.app)
    response = client.post(
        "/audit/seo",
        json={"html": "<html lang='en'><head><title>t</title></head><body><h1>x</h1></body></html>"},
        headers={"X-API-Key": key, "Authorization": "Payment stub-credential"},
    )
    assert response.status_code == 200, response.text

    # Same key, and it carries the cent it already had plus the 45 the
    # top-up bought after the call it paid for.
    assert response.json().get("api_key", key) == key, "the top-up rotated the caller's key"
    # The 1 cent it still held, plus the 45 the top-up bought after the call.
    assert billing.lookup_key(key)["prepaid_balance_cents"] == 46


def test_a_topup_without_a_key_still_mints_one(monkeypatch, tmp_path):
    """The no-key case is the whole point of the rail: a machine arrives with
    no account, pays, and leaves holding something it can spend."""
    from fastapi.testclient import TestClient

    billing = _load_billing(monkeypatch, tmp_path)
    import test_wcag_audit_engine as engine_tests

    module = engine_tests._load_main(monkeypatch)
    monkeypatch.setattr(module, "billing", billing)
    monkeypatch.setattr(module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: 50)

    client = TestClient(module.app)
    response = client.post(
        "/audit/seo",
        json={"html": "<html lang='en'><head><title>t</title></head><body><h1>x</h1></body></html>"},
        headers={"Authorization": "Payment stub-credential"},
    )
    assert response.status_code == 200, response.text
    minted = response.json().get("api_key")
    assert minted, "a payer with no key must leave holding one"
    assert billing.lookup_key(minted)["prepaid_balance_cents"] == 45
