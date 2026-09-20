"""Tests for the agent-facing x402 auto-paying client.

What these exist to prevent: this is the only client in the repo that can
move money on its own, inside a loop, with no human watching. The failure
that matters is not "an audit was missed" -- it is "the client paid when it
should not have, or kept paying". So the spending limits, and the refund on
a payment that never settled, are tested harder than the happy path.

The x402 library itself is never exercised end-to-end here (that needs a
funded wallet and a live facilitator); what is tested is that this module
drives it correctly and that every path around it fails closed.
"""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "wcag-audit-engine" / "integrations" / "hubvibe_tollbooth.py"


def _load():
    spec = importlib.util.spec_from_file_location("hubvibe_tollbooth", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hubvibe_tollbooth"] = module
    spec.loader.exec_module(module)
    return module


tollbooth = _load()


# A valid-format EVM key that has never held funds. Only used to prove the
# x402 client wires up; nothing here signs a payment that reaches a network.
THROWAWAY_KEY = "0x" + "11" * 32


def _client(**kwargs):
    kwargs.setdefault("base_url", "https://hubvibe.test")
    return tollbooth.HubVibeTollbooth(**kwargs)


def _transport(handler):
    """Patch httpx.Client so the module under test talks to a fake server."""
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.Client the module creates through a handler."""

    def install(handler):
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = _transport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(tollbooth.httpx, "Client", factory)

    return install


# --- configuration -------------------------------------------------------


def test_from_env_refuses_when_no_auth_is_configured(monkeypatch):
    """A client that can neither authenticate nor pay 402s on its first call.
    Better to fail at construction, where a human is still watching."""
    monkeypatch.delenv("HUBVIBE_API_KEY", raising=False)
    monkeypatch.delenv("HUBVIBE_WALLET_KEY", raising=False)
    with pytest.raises(tollbooth.PaymentNotConfigured):
        tollbooth.HubVibeTollbooth.from_env()


def test_from_env_reads_limits(monkeypatch):
    monkeypatch.setenv("HUBVIBE_API_KEY", "k")
    monkeypatch.delenv("HUBVIBE_WALLET_KEY", raising=False)
    monkeypatch.setenv("HUBVIBE_MAX_PRICE_USD", "0.11")
    monkeypatch.setenv("HUBVIBE_BUDGET_USD", "1.25")
    booth = tollbooth.HubVibeTollbooth.from_env()
    assert booth.max_price_usd == 0.11
    assert booth.budget_usd == 1.25


def test_limits_cannot_be_set_to_zero_or_negative():
    """There is no supported way to configure an unbounded spender."""
    with pytest.raises(ValueError):
        _client(api_key="k", budget_usd=0)
    with pytest.raises(ValueError):
        _client(api_key="k", max_price_usd=-1)


def test_x402_client_is_only_built_when_a_wallet_is_given():
    assert _client(api_key="k")._http_client is None
    assert _client(wallet_key=THROWAWAY_KEY)._http_client is not None


# --- spending limits -----------------------------------------------------


def test_per_call_cap_rejects_an_overpriced_challenge():
    booth = _client(api_key="k", max_price_usd=0.05, budget_usd=100.0)
    with pytest.raises(tollbooth.BudgetExceeded):
        booth._reserve(0.06)
    assert booth.spent_usd == 0.0


def test_budget_stops_a_runaway_loop_of_individually_cheap_calls():
    """Each call is under the per-call cap; together they are not."""
    booth = _client(api_key="k", max_price_usd=0.10, budget_usd=0.25)
    booth._reserve(0.10)
    booth._reserve(0.10)
    with pytest.raises(tollbooth.BudgetExceeded):
        booth._reserve(0.10)
    assert booth.spent_usd == pytest.approx(0.20)


def test_usd_converts_to_usdc_atomic_units():
    assert tollbooth._usd_to_atomic(0.03) == 30_000
    assert tollbooth._usd_to_atomic(0.10) == 100_000


# --- price parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"price": "$0.03"}, 0.03),
        ({"price": 0.10}, 0.10),
        ({"accepts": [{"price": "$0.10"}]}, 0.10),
    ],
)
def test_challenge_price_is_read_from_the_402(body, expected):
    assert tollbooth.HubVibeTollbooth._challenge_price_usd(body, 999.0) == expected


@pytest.mark.parametrize("body", [{}, {"price": "free"}, {"price": None}, "not json", []])
def test_unparseable_price_falls_back_to_the_published_rate_not_zero(body):
    """A price of 0.0 would slip past both spending limits unnoticed."""
    assert tollbooth.HubVibeTollbooth._challenge_price_usd(body, 0.03) == 0.03


# --- request paths -------------------------------------------------------


def test_api_key_path_sends_the_key_and_returns_the_body(mock_http):
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-API-Key")
        return httpx.Response(200, json={"pass": True, "wcag": {"pass": True}})

    mock_http(handler)
    result = _client(api_key="secret").audit("https://example.com")
    assert seen["key"] == "secret"
    assert result["pass"] is True


def test_402_without_a_wallet_raises_rather_than_returning_a_falsy_result(mock_http):
    """An agent must never read a failed audit as a clean site."""
    mock_http(lambda request: httpx.Response(402, json={"price": "$0.10"}))
    with pytest.raises(tollbooth.PaymentNotConfigured):
        _client(api_key="k").audit("https://example.com")


def test_402_is_paid_and_the_request_is_retried(mock_http, monkeypatch):
    calls = []

    def handler(request):
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(402, json={"price": "$0.10"})
        return httpx.Response(200, json={"pass": True})

    mock_http(handler)
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)
    monkeypatch.setattr(
        booth._http_client,
        "handle_402_response",
        lambda headers, body: ({"PAYMENT-SIGNATURE": "signed"}, object()),
    )

    result = booth.audit("https://example.com")
    assert result["pass"] is True
    assert len(calls) == 2
    assert calls[1]["payment-signature"] == "signed"
    assert booth.spent_usd == pytest.approx(0.10)


def test_budget_is_refunded_when_the_payment_is_rejected(mock_http, monkeypatch):
    """A rejected payment never settled, so it must not consume budget --
    otherwise a run of failures silently exhausts the client's ability to buy
    anything at all."""
    mock_http(lambda request: httpx.Response(402, json={"price": "$0.10"}))
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)
    monkeypatch.setattr(
        booth._http_client,
        "handle_402_response",
        lambda headers, body: ({"PAYMENT-SIGNATURE": "signed"}, object()),
    )
    with pytest.raises(tollbooth.HubVibeError):
        booth.audit("https://example.com")
    assert booth.spent_usd == 0.0


def test_budget_is_refunded_when_signing_fails(mock_http, monkeypatch):
    mock_http(lambda request: httpx.Response(402, json={"price": "$0.10"}))
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)

    def boom(headers, body):
        raise RuntimeError("no funds")

    monkeypatch.setattr(booth._http_client, "handle_402_response", boom)
    with pytest.raises(tollbooth.HubVibeError):
        booth.audit("https://example.com")
    assert booth.spent_usd == 0.0


def test_an_overpriced_challenge_is_never_signed(mock_http, monkeypatch):
    """The cap has to be enforced before a signature exists, not after."""
    mock_http(lambda request: httpx.Response(402, json={"price": "$5.00"}))
    booth = _client(wallet_key=THROWAWAY_KEY, max_price_usd=0.25, budget_usd=100.0)
    signed = []
    monkeypatch.setattr(
        booth._http_client,
        "handle_402_response",
        lambda headers, body: signed.append(1) or ({}, object()),
    )
    with pytest.raises(tollbooth.BudgetExceeded):
        booth.audit("https://example.com")
    assert signed == []


def test_server_errors_raise(mock_http):
    mock_http(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(tollbooth.HubVibeError):
        _client(api_key="k").audit("https://example.com")


def test_unknown_endpoint_is_rejected_before_any_request_is_made():
    with pytest.raises(ValueError):
        _client(api_key="k").audit("https://example.com", endpoint="pentest")


def test_html_audit_rejects_dimensions_that_need_a_live_page():
    """security and performance need a real HTTP response and a real page
    load; accepting raw HTML for them would bill for a check that cannot run."""
    booth = _client(api_key="k")
    for endpoint in ("security", "performance"):
        with pytest.raises(ValueError):
            booth.audit_html("<html></html>", endpoint=endpoint)


def test_published_prices_match_the_service_catalog():
    """These prices are what the budget pre-check is denominated in. If the
    service's rates change and these do not, the ceiling stops meaning what
    it says."""
    assert tollbooth.PRICES_USD == {
        "wcag": 0.05,
        "seo": 0.05,
        "security": 0.05,
        "performance": 0.05,
        "bundle": 0.15,
    }


# --- x402 client arity: 2.18 takes (headers, body), 2.21 requires a third ---
#
# Found by running scripts/first-paid-call.sh against a locally booted node.
# It died with:
#
#   HubVibeError: Could not construct an x402 payment:
#     x402HTTPClientSync.handle_402_response() missing 1 required
#     positional argument: 'request_url'
#
# requirements.txt pinned x402==2.18.0 at the time (2.22.0 now, which takes
# the third argument), so the service and the test suite were safe. The two
# places that are not: first-paid-call.sh shells out to bare
# `python3`, which resolves to whatever x402 the machine has; and this module
# ships to agent authors who install x402 themselves. The TypeError lands
# before any signature exists, so there is nothing on-chain to look at and no
# facilitator involved -- indistinguishable, from the server side, from nobody
# buying. That is the #61 failure shape exactly.


def test_a_2_21_style_client_is_given_the_request_url(mock_http, monkeypatch):
    """x402 >= 2.21 requires request_url. Calling it with two arguments raises
    before a payment can be constructed, which is how a funded agent bounces
    in silence."""
    seen = {}

    def handler(request):
        if not seen.get("challenged"):
            seen["challenged"] = True
            return httpx.Response(402, json={"price": "$0.10"})
        return httpx.Response(200, json={"pass": True})

    mock_http(handler)
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)

    def v221(headers, body, request_url):
        seen["request_url"] = request_url
        return ({"PAYMENT-SIGNATURE": "signed"}, object())

    monkeypatch.setattr(booth._http_client, "handle_402_response", v221)

    result = booth.audit("https://example.com")
    assert result["pass"] is True
    # The URL handed over must be the one that was actually challenged -- a
    # v2 client binds the signature to it, so a wrong URL signs for the wrong
    # resource and the facilitator rejects it.
    assert seen["request_url"].endswith("/audit/bundle")
    assert seen["request_url"].startswith(booth.base_url)


def test_a_2_18_style_client_is_still_called_with_two_arguments(mock_http, monkeypatch):
    """The pinned version must keep working: passing an extra argument to it
    raises exactly as loudly as omitting one from 2.21."""
    seen = {}

    def handler(request):
        if not seen.get("challenged"):
            seen["challenged"] = True
            return httpx.Response(402, json={"price": "$0.10"})
        return httpx.Response(200, json={"pass": True})

    mock_http(handler)
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)

    def v218(headers, body):
        seen["argc"] = 2
        return ({"PAYMENT-SIGNATURE": "signed"}, object())

    monkeypatch.setattr(booth._http_client, "handle_402_response", v218)

    assert booth.audit("https://example.com")["pass"] is True
    assert seen["argc"] == 2


def test_an_uninspectable_handler_falls_back_to_the_pinned_arity(mock_http, monkeypatch):
    """Some callables have no introspectable signature. Guessing the newer
    arity there would break the version this repo actually pins."""
    seen = {}

    def handler(request):
        if not seen.get("challenged"):
            seen["challenged"] = True
            return httpx.Response(402, json={"price": "$0.10"})
        return httpx.Response(200, json={"pass": True})

    mock_http(handler)
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)

    class Uninspectable:
        def __call__(self, *args):
            seen["argc"] = len(args)
            return ({"PAYMENT-SIGNATURE": "signed"}, object())

        def __getattr__(self, name):  # pragma: no cover - defensive
            raise AttributeError(name)

    obj = Uninspectable()
    monkeypatch.setattr(
        tollbooth.inspect, "signature",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("no signature")),
    )
    monkeypatch.setattr(booth._http_client, "handle_402_response", obj)

    assert booth.audit("https://example.com")["pass"] is True
    assert seen["argc"] == 2


# --- the settlement receipt -------------------------------------------------


def _b64(obj: dict) -> str:
    import base64
    import json

    return base64.b64encode(json.dumps(obj).encode()).decode()


def _paid_booth(mock_http, monkeypatch, paid_response: httpx.Response):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(402, json={"price": "$0.03"})
        return paid_response

    mock_http(handler)
    booth = _client(wallet_key=THROWAWAY_KEY, budget_usd=1.0)
    monkeypatch.setattr(
        booth._http_client,
        "handle_402_response",
        lambda headers, body: ({"PAYMENT-SIGNATURE": "signed"}, object()),
    )
    return booth


def test_the_settlement_receipt_is_kept_from_the_payment_response_header(mock_http, monkeypatch):
    """The receipt is the payer's only on-chain reference. The client keeps
    it where a script can print it -- first-paid-call.sh prints the Basescan
    link for exactly this transaction hash."""
    receipt = {"success": True, "transaction": "0x" + "cd" * 32, "network": "eip155:8453"}
    booth = _paid_booth(
        mock_http, monkeypatch,
        httpx.Response(200, json={"pass": True}, headers={"PAYMENT-RESPONSE": _b64(receipt)}),
    )
    assert booth.last_settlement is None, "nothing paid yet"

    assert booth.audit("https://example.com", endpoint="wcag")["pass"] is True
    assert booth.last_settlement == receipt


def test_the_v1_receipt_header_name_is_read_too(mock_http, monkeypatch):
    receipt = {"success": True, "transaction": "0x" + "ef" * 32, "network": "base"}
    booth = _paid_booth(
        mock_http, monkeypatch,
        httpx.Response(200, json={"pass": True}, headers={"X-PAYMENT-RESPONSE": _b64(receipt)}),
    )
    booth.audit("https://example.com", endpoint="wcag")
    assert booth.last_settlement["transaction"] == "0x" + "ef" * 32


def test_a_paid_200_without_a_receipt_still_delivers(mock_http, monkeypatch):
    booth = _paid_booth(mock_http, monkeypatch, httpx.Response(200, json={"pass": True}))
    assert booth.audit("https://example.com", endpoint="wcag")["pass"] is True
    assert booth.last_settlement is None


def test_a_malformed_receipt_never_turns_a_paid_audit_into_an_error(mock_http, monkeypatch):
    booth = _paid_booth(
        mock_http, monkeypatch,
        httpx.Response(200, json={"pass": True}, headers={"PAYMENT-RESPONSE": "%%%not-base64"}),
    )
    assert booth.audit("https://example.com", endpoint="wcag")["pass"] is True
    assert booth.last_settlement is None
