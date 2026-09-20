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
