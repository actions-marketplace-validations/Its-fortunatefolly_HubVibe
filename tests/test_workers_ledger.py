"""The ledger: what we charged, what it cost, and what we refuse to claim.

The discipline enforced here is the commercially important one. A margin
figure that quietly assumes an unmeasured provider call was free is worse than
no figure at all: it would report a capability as profitable at exactly the
moment we have no idea whether it is.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "workers"


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
ledger = W.ledger


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "w.db"))
    ledger.reset_for_tests()
    yield ledger
    ledger.reset_for_tests()


def _settled_call(call_id, worker="llm.analyze", price=0.25, cost=1350,
                  measured=True, payer="0xpayer", status="ok"):
    ledger.open_call(call_id, worker, f"/work/{worker}", price, payer=payer, rail="x402")
    ledger.record_provider_call(call_id, "vertex:gemini-2.5-flash", 1, 0.0, True,
                                latency_ms=800, cost_micros=cost,
                                cost_measured=measured, usage="in=100 out=200")
    ledger.close_call(call_id, status, latency_ms=820,
                      provider_used="vertex:gemini-2.5-flash", attempts=1,
                      settled=(status == "ok"), payer=payer)


def test_margin_is_computed_only_from_measured_costs(db):
    _settled_call("c1", cost=1350, measured=True)
    _settled_call("c2", cost=None, measured=False)

    row = next(r for r in ledger.summary() if r["worker"] == "llm.analyze")
    assert row["calls"] == 2
    assert row["settled"] == 2
    assert row["revenue_usd"] == 0.50
    assert row["unmeasured_calls"] == 1
    # Only the measured half feeds the margin.
    assert row["measured_revenue_usd"] == 0.25
    assert row["measured_provider_cost_usd"] == 0.00135
    assert row["measured_gross_profit_usd"] == pytest.approx(0.24865)


def test_margin_is_none_when_nothing_was_measured(db):
    """The honest answer to "what is the margin" with no known cost is
    "unknown", not 100%."""
    _settled_call("c1", cost=None, measured=False)
    row = ledger.summary()[0]
    assert row["measured_gross_margin_pct"] is None
    assert row["measured_gross_profit_usd"] is None
    assert row["unmeasured_calls"] == 1


def test_one_unmeasured_attempt_makes_the_whole_call_unmeasured(db):
    """A call's total is a lower bound if any attempt could not report its
    cost. Treating it as complete would understate what we spent."""
    ledger.open_call("c1", "research.brief", "/work/research/brief", 5.0, payer="0xp")
    ledger.record_provider_call("c1", "http-fetch", 1, 0.0, True, cost_micros=0,
                                cost_measured=True)
    ledger.record_provider_call("c1", "vertex:gemini-2.5-flash", 1, 0.0, True,
                                cost_micros=None, cost_measured=False)
    ledger.close_call("c1", "ok", settled=True, payer="0xp")

    row = ledger.summary()[0]
    assert row["unmeasured_calls"] == 1
    assert row["measured_gross_margin_pct"] is None


def test_failed_attempts_are_recorded_so_reliability_is_not_flattered(db):
    """Recording only winning attempts would make every provider look perfect
    and make any success-rate claim meaningless."""
    ledger.open_call("c1", "market.quote", "/work/market/quote", 0.02)
    ledger.record_provider_call("c1", "coinbase-market", 1, 0.0, False,
                                failure_reason="timeout", cost_micros=0,
                                cost_measured=True)
    ledger.record_provider_call("c1", "coinbase-market", 2, 0.0, True,
                                cost_micros=0, cost_measured=True)
    ledger.close_call("c1", "ok", attempts=2, settled=True)

    health = {row["provider"]: row for row in ledger.provider_health()}
    assert health["coinbase-market"]["attempts"] == 2
    assert health["coinbase-market"]["ok"] == 1
    assert health["coinbase-market"]["success_rate_pct"] == 50.0


def test_failed_calls_are_not_counted_as_revenue(db):
    _settled_call("c1", status="failed", payer="0xp")
    row = ledger.summary()[0]
    assert row["failed"] == 1
    assert row["settled"] == 0
    assert row["revenue_usd"] == 0.0


def test_repeat_payers_surface_recurring_demand(db):
    """The wallet shows money arriving; only this shows who came back."""
    _settled_call("c1", payer="0xrepeat")
    _settled_call("c2", worker="market.quote", price=0.02, payer="0xrepeat")
    _settled_call("c3", payer="0xonce")

    repeats = ledger.repeat_payers()
    assert [r["payer"] for r in repeats] == ["0xrepeat"]
    assert repeats[0]["paid_calls"] == 2
    assert repeats[0]["distinct_workers"] == 2


def test_success_rate_is_measured_not_asserted(db):
    for index in range(9):
        _settled_call(f"ok{index}")
    _settled_call("bad", status="failed")
    row = next(r for r in ledger.summary() if r["worker"] == "llm.analyze")
    assert row["calls"] == 10
    assert row["success_rate_pct"] == 90.0


def test_a_ledger_failure_never_raises_into_the_request_path(db, monkeypatch):
    """This sits in the path of a call the caller already paid for: a write
    failure must cost us the row, never the customer's result."""
    def explode():
        raise RuntimeError("disk full")

    monkeypatch.setattr(ledger, "_connect", explode)
    ledger.open_call("x", "w", "/work/w", 1.0)
    ledger.record_provider_call("x", "p", 1, 0.0, True)
    ledger.close_call("x", "ok")
    assert ledger.summary() == []


def test_money_is_stored_in_micros_so_small_costs_do_not_round_to_zero(db):
    """A token-priced call costing $0.000075 is ordinary; in cents it would
    record as free and every margin would read as 100%."""
    _settled_call("c1", price=0.25, cost=75, measured=True)
    row = ledger.summary()[0]
    assert row["measured_provider_cost_usd"] == 0.000075
    assert row["measured_gross_margin_pct"] == pytest.approx(99.97, abs=0.01)


def test_idempotency_claim_is_exclusive(db):
    state, _ = ledger.claim_idempotency("k1", "call-1", "llm.analyze")
    assert state == "claimed"
    again, _ = ledger.claim_idempotency("k1", "call-2", "llm.analyze")
    assert again == "in_progress"

    ledger.complete_idempotency("k1", '{"result": 1}')
    done, stored = ledger.claim_idempotency("k1", "call-3", "llm.analyze")
    assert done == "done"
    assert stored == '{"result": 1}'


def test_releasing_a_claim_lets_a_failed_job_be_retried(db):
    ledger.claim_idempotency("k2", "call-1", "llm.analyze")
    ledger.release_idempotency("k2")
    state, _ = ledger.claim_idempotency("k2", "call-2", "llm.analyze")
    assert state == "claimed", "a failed job was not billed, so its key must be reusable"


# --- credential resolution must never hang the node -------------------------

def test_google_credential_lookup_cannot_block_forever(monkeypatch):
    """Regression: google.auth.default() ends by probing the GCE metadata
    service, and that probe can CONNECT and then never answer. Observed here
    as a test run wedged in do_sys_poll on two ESTABLISHED sockets to
    169.254.169.254:80 for 24 minutes. Unbounded, that is /health and /work
    hanging forever on a node.
    """
    import time

    google_auth = W.providers.google_auth
    monkeypatch.setattr(google_auth, "_RESOLVE_TIMEOUT", 0.2)

    def never_returns(*_args, **_kwargs):
        time.sleep(30)

    monkeypatch.setattr(google_auth, "_resolve_blocking", never_returns)
    monkeypatch.setattr(google_auth, "_resolved", False)
    monkeypatch.setattr(google_auth, "_creds", None)
    monkeypatch.setattr(google_auth, "_project", None)

    started = time.monotonic()
    assert google_auth.configured() is False, "must fail closed, not hang"
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"credential lookup blocked for {elapsed:.1f}s"
    assert "did not finish" in google_auth.unavailable_reason()


def test_a_wedged_credential_lookup_is_not_retried_on_every_call(monkeypatch):
    """The timeout is paid once, not once per request."""
    import time

    google_auth = W.providers.google_auth
    monkeypatch.setattr(google_auth, "_RESOLVE_TIMEOUT", 0.2)
    calls = {"n": 0}

    def never_returns(*_args, **_kwargs):
        calls["n"] += 1
        time.sleep(30)

    monkeypatch.setattr(google_auth, "_resolve_blocking", never_returns)
    monkeypatch.setattr(google_auth, "_resolved", False)
    monkeypatch.setattr(google_auth, "_creds", None)
    monkeypatch.setattr(google_auth, "_project", None)

    for _ in range(3):
        google_auth.configured()
    assert calls["n"] == 1, "the wedged lookup must be attempted once and cached"


# --- receipts ---------------------------------------------------------------

def test_receipt_columns_are_added_to_a_database_created_before_them(monkeypatch, tmp_path):
    """The box's ledger predates the receipt columns; CREATE TABLE IF NOT
    EXISTS cannot add them, the migration must."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(ledger._SCHEMA.replace("request_hash", "x_request_hash"))  # baseline schema
    old.execute("DROP TABLE worker_calls")
    old.execute("CREATE TABLE worker_calls (call_id TEXT PRIMARY KEY, idempotency_key TEXT, "
                "worker TEXT NOT NULL, path TEXT NOT NULL, started_at REAL NOT NULL, "
                "finished_at REAL, status TEXT NOT NULL, failure_reason TEXT, failure_stage TEXT, "
                "price_micros INTEGER NOT NULL, provider_cost_micros INTEGER, cost_measured INTEGER "
                "NOT NULL DEFAULT 0, provider_used TEXT, providers_tried TEXT, attempts INTEGER NOT "
                "NULL DEFAULT 0, latency_ms INTEGER, payer TEXT, tx_hash TEXT, settled INTEGER NOT "
                "NULL DEFAULT 0, rail TEXT)")
    old.commit(); old.close()

    monkeypatch.setenv("WORKER_LEDGER_PATH", str(path))
    ledger.reset_for_tests()
    ledger.open_call("c1", "market.quote", "/work/market/quote", 0.02, request_hash="sha256:aa")
    columns = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(worker_calls)")}
    assert {"request_hash", "result_hash", "network", "asset", "pay_to", "amount_atomic",
            "node_version"} <= columns
    assert ledger.get_call("c1")["request_hash"] == "sha256:aa"


def test_canonical_hash_is_order_independent_and_stable():
    a = ledger.canonical_hash({"b": 1, "a": [1, 2, {"z": None, "y": "\u00e9"}]})
    b = ledger.canonical_hash({"a": [1, 2, {"y": "\u00e9", "z": None}], "b": 1})
    assert a == b and a.startswith("sha256:") and len(a) == len("sha256:") + 64
    assert ledger.canonical_hash({"b": 2, "a": 1}) != a


# The maps.weather call from the 2026-09-21 seed, as the box ledger recorded
# it and as Base recorded it: payer OX1, 100000 atomic USDC, one transaction.
# The receipt for it must reproduce those exact facts and nothing invented.
SEED_CALL = {
    "call_id": "932ae60bc0414d6a9fa3d341841c8768", "worker": "maps.weather",
    "path": "/work/maps/weather", "price_usd": 0.10,
    "payer": "0x104feA79F30b4fB4Da86B6D65951217F914bdd35",
    "tx_hash": "0x1eb590fc6cfb1403747b4c8c67925401d00efd00a543b0c4a4ad0a99660887ae",
    "pay_to": "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
    "network": "eip155:8453", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "amount_atomic": 100000, "provider_used": "maps-grounding-lite", "latency_ms": 962,
}


def test_receipt_reconciles_a_real_settled_transaction(db):
    c = SEED_CALL
    ledger.open_call(c["call_id"], c["worker"], c["path"], c["price_usd"], payer=c["payer"],
                     rail="x402", request_hash="sha256:" + "1" * 64, node_version="1.4.0")
    result_hash = ledger.canonical_hash({"location": "San Francisco, CA", "result": {"cloudCover": 54}})
    ledger.close_call(c["call_id"], "ok", latency_ms=c["latency_ms"], provider_used=c["provider_used"],
                      attempts=1, settled=True, tx_hash=c["tx_hash"], payer=c["payer"],
                      result_hash=result_hash, network=c["network"], asset=c["asset"],
                      pay_to=c["pay_to"], amount_atomic=c["amount_atomic"])

    r = ledger.receipt_for(c["call_id"])
    assert r["receipt_id"] == "rcpt_" + c["call_id"] and r["request_id"] == c["call_id"]
    assert r["outcome"] == "paid_delivered" and r["paid"] is True and r["delivered"] is True
    assert r["payment"] == {
        "rail": "x402", "payer": c["payer"], "pay_to": c["pay_to"],
        "amount_atomic": 100000, "amount_usd": 0.10, "asset": c["asset"],
        "network": c["network"], "tx_hash": c["tx_hash"], "settled": True}
    assert r["request"]["worker"] == "maps.weather" and r["request"]["price_usd"] == 0.10
    assert r["request"]["request_hash"] == "sha256:" + "1" * 64
    assert r["delivery"] == {"delivered": True, "result_hash": result_hash}
    assert r["execution"]["status"] == "ok" and r["execution"]["provider_used"] == "maps-grounding-lite"
    assert r["node_version"] == "1.4.0"
    assert r["timestamp"].endswith("Z") and r["execution"]["finished_at"] == r["timestamp"]
    assert ledger.call_id_for(r["receipt_id"]) == c["call_id"]
    assert ledger.receipt_for("no-such-call") is None


def test_receipt_outcomes_never_call_an_unsettled_or_failed_job_delivered(db):
    cases = [
        ("ok", True, "paid_delivered", True, True),
        ("failed", True, "paid_failed", True, False),      # would be a bug elsewhere; still honest
        ("failed", False, "unpaid_failed", False, False),
        ("refused", False, "unpaid_refused", False, False),
        ("ok", False, "delivered_not_settled", False, True),
    ]
    for i, (status, settled, outcome, paid, delivered) in enumerate(cases):
        cid = f"case{i}"
        ledger.open_call(cid, "market.quote", "/work/market/quote", 0.02, payer="0xabc" if settled else None)
        ledger.close_call(cid, status, settled=settled, tx_hash="0xtx" if settled else None,
                          result_hash="sha256:r" if status == "ok" else None,
                          failure_reason=None if status == "ok" else "provider_down")
        r = ledger.receipt_for(cid)
        assert (r["outcome"], r["paid"], r["delivered"]) == (outcome, paid, delivered), cid
        assert r["delivery"]["result_hash"] == ("sha256:r" if delivered else None)
        assert r["payment"]["amount_atomic"] == (20000 if paid else None)
