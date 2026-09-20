"""x402 payment path tests.

The bug these exist to prevent: x402ResourceServer.initialize() is a plain
synchronous method, but this module used to `await` it. `await None` raises
TypeError, and verify_only_sync catches every exception and fails closed --
so a fully configured x402 deployment rejected 100% of payments with no
diagnostic anywhere. Fail-closed is the right default, but it means a wiring
mistake is indistinguishable from a genuinely invalid payment unless
something actually exercises the accept path.

Every test here mocks the facilitator: verification and settlement are the
facilitator's job, and the point is to prove this module drives it correctly,
not to re-test the x402 library or reach the network.
"""

import importlib.util
import json
import sys
from pathlib import Path
import logging
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
X402_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py"


# 0x + exactly 40 hex. The default used to be "0xabc", so five tests below
# asserted verify/settle behaviour under a pay-to address that could never
# receive a payment. is_configured() now shape-checks it, so a placeholder
# here silently disables the very rail these tests exercise.
VALID_PAY_TO = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _load_x402(monkeypatch, *, facilitator="https://facilitator.example",
               pay_to=VALID_PAY_TO, auth_headers=None):
    if facilitator is None:
        monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)
    else:
        monkeypatch.setenv("X402_FACILITATOR_URL", facilitator)
    if pay_to is None:
        monkeypatch.delenv("X402_PAY_TO_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("X402_PAY_TO_ADDRESS", pay_to)
    if auth_headers is None:
        monkeypatch.delenv("X402_FACILITATOR_AUTH_HEADERS", raising=False)
    else:
        monkeypatch.setenv("X402_FACILITATOR_AUTH_HEADERS", auth_headers)

    name = "hubvibe_x402_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, X402_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_fake_server(monkeypatch, module, *, valid=True, settled=True,
                         settle_response=None):
    """Stand in for x402ResourceServer with the REAL library's shape.

    initialize() is deliberately a plain MagicMock (sync, returns a
    non-awaitable) because that is exactly what the real method is -- an
    AsyncMock here would hide the very bug this file exists to catch.

    `settle_response` replaces the MagicMock settle result with a real
    x402 SettleResponse, for tests that encode it into the receipt header.
    """
    server = MagicMock()
    server.initialize = MagicMock(return_value=None)
    server.register = MagicMock()
    # Concrete amount so the Stripe-recording path can do arithmetic on it;
    # atomic USDC units, $0.03. A bare MagicMock would make int() raise and
    # silently skip recording in every test that goes through this helper.
    requirement = MagicMock()
    requirement.amount = "30000"
    server.build_payment_requirements = MagicMock(return_value=[requirement])

    verify_result = MagicMock()
    verify_result.is_valid = valid
    settle_result = MagicMock()
    settle_result.success = settled
    settle_result.transaction = "0xsettledtx"
    if settle_response is not None:
        settle_result = settle_response

    async def _verify(*a, **k):
        return verify_result

    async def _settle(*a, **k):
        return settle_result

    server.verify_payment = _verify
    server.settle_payment = _settle

    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=server))
    monkeypatch.setattr(module, "HTTPFacilitatorClient", MagicMock())
    monkeypatch.setattr(module, "FacilitatorConfig", MagicMock())
    monkeypatch.setattr(module, "ExactEvmServerScheme", MagicMock())
    monkeypatch.setattr(module, "ResourceConfig", MagicMock())
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: MagicMock())
    return server


def test_valid_payment_is_accepted(monkeypatch):
    """The regression. Before the fix verify returned None -- always."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is True


def test_initialize_is_called_synchronously_not_awaited(monkeypatch):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    module.verify_only_sync("signed-payment", price="$0.03")

    server.initialize.assert_called_once()


def test_facilitator_rejection_fails_closed(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, valid=False)

    assert module.verify_only_sync("signed-payment", price="$0.03") is None


def test_failed_settlement_fails_closed(monkeypatch):
    """Verified but not settled means we were not actually paid."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, valid=True, settled=False)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is False


def test_unconfigured_deployment_never_accepts(monkeypatch):
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.is_configured() is False
    assert module.verify_only_sync("signed-payment", price="$0.03") is None


def test_requirements_are_cached_per_price_not_shared(monkeypatch):
    """A $0.03 payment must never satisfy a $0.10 bundle challenge."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    module.verify_only_sync("p", price="$0.03")
    module.verify_only_sync("p", price="$0.10")
    module.verify_only_sync("p", price="$0.03")

    prices = {c.kwargs["price"] for c in module.ResourceConfig.call_args_list}
    assert prices == {"$0.03", "$0.10"}
    # Two distinct prices -> two builds; the repeat $0.03 is served from cache.
    assert server.build_payment_requirements.call_count == 2


def test_server_is_not_cached_when_initialize_fails(monkeypatch):
    """A briefly unreachable facilitator must not poison the server for the
    life of the process -- the next request should retry."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("facilitator unreachable"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    assert module.verify_only_sync("p", price="$0.03") is None
    assert module._server is None, "a server that failed to initialize was cached"


def test_no_x402_is_offered_when_unconfigured(monkeypatch):
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.accepts_entry(price="$0.03") is None


def test_accepts_entry_advertises_the_real_address_when_configured(monkeypatch):
    module = _load_x402(monkeypatch, pay_to=VALID_PAY_TO)
    _install_fake_server(monkeypatch, module)

    entry = module.accepts_entry(price="$0.03")
    assert entry["scheme"] == "exact"
    assert entry["payTo"] == VALID_PAY_TO, "the spec spells it payTo; pay_to is unreadable"


@pytest.mark.parametrize(
    "bad_address",
    [
        "0x32b08c5e927c69877d0fcab35618c265674922b",   # 39 hex -- one short
        "0x32b08c5e927c69877d0fcab35618c265674922bcd",  # 41 hex -- one long
        "0xabc",                                        # a placeholder
        "0x32b08c5e927c69877d0fcab35618c26567492zz",   # right length, not hex
        "32b08c5e927c69877d0fcab35618c265674922bc",    # 40 hex, missing 0x
        "changeme",
        # Shape-valid but unownable: passes every format check, and USDC
        # reverts transfers to address(0). Shipped once, live, for real.
        "0x0000000000000000000000000000000000000000",
    ],
)
def test_a_malformed_pay_to_address_never_advertises_x402(monkeypatch, bad_address):
    """A recipient that cannot receive must not be offered as a live rail.

    This is the incident, not a hypothetical: a deployment ran with a 16-hex
    pay-to address while advertising x402 as live, so every agent that found
    the service through the Bazaar built a payment to an address that could
    not receive it. Nothing errored. From this side it was indistinguishable
    from nobody wanting to buy.

    `bool(_PAY_TO_ADDRESS)` was the entire check, so every string below used
    to switch the rail ON. The deploy preflight catches some of these, but
    only when the value is a plain env var -- a Secret Manager value is
    explicitly not shape-checked there, so this is the only check that holds
    wherever the value came from.
    """
    module = _load_x402(monkeypatch, pay_to=bad_address)

    assert module.is_configured() is False
    assert module.accepts_entry(price="$0.03") is None


def test_a_non_evm_network_is_not_held_to_the_evm_address_shape(monkeypatch):
    """0x + 40 hex is an EVM address format. Enforcing it on a Solana or
    other non-eip155 deployment would fail closed on a correctly configured
    service -- the same false-negative this guard exists to prevent, pointed
    the other way."""
    monkeypatch.setenv("X402_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    module = _load_x402(monkeypatch, pay_to="9mcxc1SomeSolanaStyleAddressVG12")

    assert module.is_configured() is True


@pytest.mark.parametrize("bad_header", ["", "   ", "not-base64-at-all"])
def test_malformed_payment_header_fails_closed(monkeypatch, bad_header):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    def _explode(_header):
        raise ValueError("malformed payment header")

    monkeypatch.setattr(module, "decode_payment_signature_header", _explode)
    assert module.verify_only_sync(bad_header, price="$0.03") is None


# --- Authenticated facilitators ------------------------------------------
#
# The free public facilitator at x402.org is testnet-only. Any facilitator
# that settles real money on mainnet authenticates the resource server, so
# without a way to send credentials, x402 could only ever have been switched
# on against a facilitator that wanted none -- i.e. not a paying one.


def test_no_auth_provider_when_no_credentials_are_configured(monkeypatch):
    module = _load_x402(monkeypatch)
    assert module._auth_provider() is None


def test_auth_headers_are_sent_on_every_facilitator_endpoint(monkeypatch):
    """verify, settle, supported and bazaar are separate calls. Credentials
    missing from any one of them means that call fails while the others
    succeed -- a partial outage that is far harder to diagnose than a clean
    rejection."""
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok123"})
    )
    headers = module._auth_provider().get_auth_headers()

    for endpoint in ("verify", "settle", "supported", "bazaar"):
        assert getattr(headers, endpoint) == {"Authorization": "Bearer tok123"}, (
            f"{endpoint} would be called without credentials"
        )


def test_auth_provider_is_handed_to_the_facilitator_client(monkeypatch):
    """Building the provider is useless if it never reaches the client."""
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok123"})
    )
    _install_fake_server(monkeypatch, module)

    module.verify_only_sync("signed-payment", price="$0.03")

    assert module.FacilitatorConfig.call_args is not None, "FacilitatorConfig was never built"
    provider = module.FacilitatorConfig.call_args.kwargs.get("auth_provider")
    assert provider is not None, "the facilitator client was configured without credentials"
    assert provider.get_auth_headers().verify == {"Authorization": "Bearer tok123"}


@pytest.mark.parametrize(
    "bad", ['{"Authorization": 5}', '["not", "an", "object"]', "not json at all", '"a string"']
)
def test_malformed_auth_headers_raise_rather_than_silently_dropping(monkeypatch, bad):
    """Silently ignoring bad credentials leaves x402 advertised while the
    facilitator rejects every payment -- indistinguishable from nobody
    buying, and invisible for as long as nobody looks."""
    module = _load_x402(monkeypatch, auth_headers=bad)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        module._auth_provider()


def test_static_headers_provider_is_used_when_configured(monkeypatch):
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok"})
    )
    provider = module._auth_provider()
    assert type(provider).__name__ == "_StaticAuthProvider"
    assert provider.get_auth_headers().verify == {"Authorization": "Bearer tok"}


# --- Recording settlements in Stripe -------------------------------------
#
# The pattern from Stripe's machine-payments sample: after the facilitator
# settles USDC on-chain, mirror it into Stripe as a PaymentIntent in
# transaction_verification mode. This is what makes x402 revenue appear in
# the Stripe balance instead of accumulating invisibly on an address -- and
# "earning while reading zero" is the one confusion this project cannot
# afford, because zero is also what no demand looks like.


def _settlement(tx="0xtxhash", success=True, amount="30000"):
    """A settle result + requirements pair shaped like the real library's:
    amount is atomic USDC units (6 decimals), $0.03 == 30_000."""
    result = MagicMock()
    result.transaction = tx
    result.success = success
    requirements = MagicMock()
    requirements.amount = amount
    return result, requirements


def _capture_payment_intents(monkeypatch, *, boom=False):
    """Intercept stripe.PaymentIntent.create.

    Patches the shared `stripe` library object, not the module under test, so
    it binds for whichever x402 module the caller loaded.
    """
    import stripe

    calls = []

    def _create(**kwargs):
        if boom:
            raise RuntimeError("stripe exploded")
        calls.append(kwargs)
        pi = MagicMock()
        pi.id = "pi_test"
        return pi

    monkeypatch.setattr(stripe.PaymentIntent, "create", staticmethod(_create))
    return calls


def test_a_settled_payment_is_recorded_as_a_stripe_payment_intent(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)

    result, requirements = _settlement(amount="30000")  # $0.03
    module.record_settlement_in_stripe(result, requirements)

    assert len(calls) == 1
    call = calls[0]
    assert call["amount"] == 3, "30_000 atomic USDC units is 3 cents"
    assert call["currency"] == "usd"
    opts = call["payment_method_options"]["crypto"]
    assert opts["mode"] == "transaction_verification"
    assert opts["transaction_verification_options"]["network"] == "base"
    assert opts["transaction_verification_options"]["transaction_hash"] == "0xtxhash"


def test_recording_is_idempotent_by_transaction_hash(monkeypatch):
    """A retry or double call must not double-count revenue. The idempotency
    key IS the transaction hash, so Stripe collapses duplicates server-side."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)

    result, requirements = _settlement(tx="0xsame")
    module.record_settlement_in_stripe(result, requirements)

    assert calls[0]["idempotency_key"] == "0xsame"


def test_recording_failure_never_fails_the_settlement(monkeypatch):
    """By the time recording runs the money has already moved on-chain. A
    bookkeeping failure that turned into a payment failure would refuse
    service to a caller who has already paid -- the worst possible outcome."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    _capture_payment_intents(monkeypatch, boom=True)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert module.settle_sync(pending) is True


def test_settlement_still_succeeds_without_a_stripe_key(monkeypatch):
    """Stripe recording is optional bookkeeping, not a payment dependency.
    A deployment paying to a self-custody wallet has no Stripe to record
    into, and its payments must still settle."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert module.settle_sync(pending) is True
    assert calls == [], "no key, no PaymentIntent -- and no crash"


def test_a_failed_settlement_is_never_recorded(monkeypatch):
    """Recording an unsettled payment would invent revenue in Stripe that
    never arrived on-chain -- bookkeeping fraud by bug."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)

    result, requirements = _settlement(success=False)
    module.record_settlement_in_stripe(result, requirements)
    result2, requirements2 = _settlement(tx=None)
    module.record_settlement_in_stripe(result2, requirements2)

    assert calls == []


def test_an_unmapped_network_skips_recording_rather_than_guessing(monkeypatch):
    """transaction_verification verifies against a named network. Guessing
    the name records the payment against the wrong chain, which is worse
    than not recording: it looks reconciled and is not."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    monkeypatch.setenv("X402_NETWORK", "eip155:1")  # Ethereum mainnet, unmapped
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)

    result, requirements = _settlement()
    module.record_settlement_in_stripe(result, requirements)

    assert calls == []


def test_sub_cent_settlements_are_not_recorded(monkeypatch):
    """Stripe rejects zero-cent PaymentIntents; a sub-cent settlement would
    turn every recording attempt into a logged error."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)

    result, requirements = _settlement(amount="4000")  # $0.004
    module.record_settlement_in_stripe(result, requirements)

    assert calls == []


def test_settle_sync_records_after_a_successful_settle(monkeypatch):
    """Both settle paths must record -- settle_sync is the one the paid
    routes actually use (verify first, deliver, then settle)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is True
    assert len(calls) == 1


# --- Bazaar discovery records must survive the facilitator's own validator ---
#
# The Bazaar half of a 402 is only worth emitting if a facilitator will
# actually catalog it, and a facilitator that validates before cataloging runs
# exactly the check below. These assert against the x402 library's own
# `validate_discovery_extension` rather than against a hand-written expected
# dict, because the thing that matters is not "does this look right to us" but
# "does the indexer accept it". It did not: `declare_discovery_extension`
# leaves `method` to be enriched by machinery this service does not use, so
# every record went out failing its own co-emitted schema.

def _validate_bazaar(extension: dict):
    from x402.extensions.bazaar import validate_discovery_extension

    assert "bazaar" in extension, "x402 is configured, so a record must be emitted"
    return validate_discovery_extension(extension["bazaar"])


def test_a_body_route_discovery_record_passes_the_facilitator_validator(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    extension = module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        output_example={"pass": True},
    )
    result = _validate_bazaar(extension)
    assert result.valid, result.errors


def test_a_body_route_discovery_record_names_the_http_method(monkeypatch):
    """The paid routes are POST-only. A record that omits the method is not
    just schema-invalid -- an agent reading it has no way to know how to
    call the resource it just found."""
    module = _load_x402(monkeypatch)
    extension = module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    assert extension["bazaar"]["info"]["input"]["method"] == "POST"


def test_an_mcp_tool_discovery_record_passes_the_facilitator_validator(monkeypatch):
    module = _load_x402(monkeypatch)
    extension = module.bazaar_extension_for_mcp_tool(
        tool_name="audit_wcag",
        description="WCAG 2.1 A/AA accessibility audit via axe-core. $0.03 per call.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        example={"url": "https://example.com"},
    )
    result = _validate_bazaar(extension)
    assert result.valid, result.errors


def test_no_discovery_record_is_emitted_when_x402_cannot_settle(monkeypatch):
    """Same fail-closed rule as every other x402 surface: an unpayable
    resource must not be advertised in an index agents shop by capability."""
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"}, input_schema={"type": "object"}
    ) == {}
    assert module.bazaar_extension_for_mcp_tool(
        tool_name="audit_wcag", description="d", input_schema={"type": "object"}
    ) == {}


# --- a refused payment must say WHY, in the log Cloud Run keeps ------------
#
# The first real paid call against the deployed node came back as a bare 402
# re-challenge. The facilitator's invalid_reason -- or the exception that
# stopped verify from ever reaching it -- existed for a few milliseconds
# inside this process and was discarded by `except Exception: return None`.
# The Cloud Run log had nothing; the owner had the word "rejected". These pin
# the reason into the log at WARNING, which the default handler emits to
# stderr and Cloud Run captures. The fail-closed return values are asserted
# unchanged in every case: the log is what changed, not the contract.


def _rejecting_verify(server, *, reason, message="the facilitator said no"):
    result = MagicMock()
    result.is_valid = False
    result.invalid_reason = reason
    result.invalid_message = message
    result.payer = "0xpayer"

    async def _verify(*a, **k):
        return result

    server.verify_payment = _verify


def _exploding_verify(server, exc):
    async def _verify(*a, **k):
        raise exc

    server.verify_payment = _verify


def test_a_facilitator_rejection_is_logged_with_its_reason(monkeypatch, caplog):
    module = _load_x402(monkeypatch, facilitator="https://fac.example")
    server = _install_fake_server(monkeypatch, module)
    _rejecting_verify(server, reason="insufficient_funds")

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None

    text = caplog.text
    assert "REJECTED" in text
    assert "insufficient_funds" in text
    assert "the facilitator said no" in text
    assert "https://fac.example" in text
    assert "$0.03" in text


def test_an_unreachable_facilitator_is_logged_as_such_not_as_a_rejection(monkeypatch, caplog):
    """A refusal and an outage need different fixes. Collapsing both into a
    402 is how a week gets spent on the wrong one."""
    module = _load_x402(monkeypatch, facilitator="https://fac.example")
    server = _install_fake_server(monkeypatch, module)
    _exploding_verify(server, ConnectionError("Name or service not known"))

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None

    text = caplog.text
    assert "FAILED before the facilitator could answer" in text
    assert "ConnectionError" in text
    assert "Name or service not known" in text
    assert "REJECTED" not in text


def test_a_refused_settlement_is_logged_after_delivery(monkeypatch, caplog):
    """This is the case where an audit went out unpaid. It must be the
    loudest of all, and it must carry the facilitator's reason."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module, settled=False)

    settle_result = MagicMock()
    settle_result.success = False
    settle_result.error_reason = "authorization_expired"
    settle_result.error_message = "past validBefore"

    async def _settle(*a, **k):
        return settle_result

    server.settle_payment = _settle

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    with caplog.at_level(logging.WARNING):
        assert module.settle_sync(pending) is False

    text = caplog.text
    assert "settle REFUSED" in text
    assert "authorization_expired" in text
    assert "past validBefore" in text


def test_a_valid_payment_logs_no_rejection(monkeypatch, caplog):
    """The guard must not cry wolf: a clean payment produces no REJECTED or
    FAILED line, or the log becomes noise on exactly the day it matters."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is not None

    assert "REJECTED" not in caplog.text
    assert "FAILED" not in caplog.text


def test_the_stripe_mirror_is_off_unless_asked_for(monkeypatch, caplog):
    """On this deployment the pay-to is a self-custody wallet, so mirroring a
    settlement into Stripe cannot succeed -- and the old default-on behaviour
    would have logged, on every real payment, a traceback saying Stripe 'will
    not show it until this transaction hash is recorded'. A log line that is
    false on the one day someone reads it is worse than no line.

    With the key set and the flag absent: no PaymentIntent, no exception,
    one INFO line that says where the money actually is."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.delenv("X402_STRIPE_MIRROR", raising=False)
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch)
    _install_fake_server(monkeypatch, module)

    with caplog.at_level(logging.INFO):
        pending = module.verify_only_sync("signed-payment", price="$0.03")
        assert module.settle_sync(pending) is True

    assert calls == []
    assert "will not appear in Stripe" in caplog.text
    assert "Traceback" not in caplog.text
    assert "recording it in Stripe failed" not in caplog.text


# --- never advertise an x402 version the facilitator will not verify --------
#
# Found by simulation. Against a facilitator whose /supported lists only the
# legacy v1 name ("base"), the node still sent the v2 PAYMENT-REQUIRED header
# naming eip155:8453. A v2-capable client took the offer and signed for
# eip155:8453; the node then raised SchemeNotFoundError before the facilitator
# was called and failed closed into a bare 402 -- every time, whatever the
# wallet held. The two rejected live attempts had exactly that shape.


def _facilitator_that_supports(server, *versions):
    """Stand in for the cached /supported: a kind exists only for `versions`."""
    server.get_supported_kind = (
        lambda version, network, scheme: object() if version in versions else None
    )


def test_the_v2_header_is_withheld_when_the_facilitator_is_v1_only(monkeypatch, caplog):
    module = _load_x402(monkeypatch, facilitator="https://v1only.example")
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 1)

    with caplog.at_level(logging.WARNING):
        assert module.payment_required_header(price="$0.03") == {}
    # v1 is still offered, so the rail stays payable for v1 clients.
    assert module.accepts_entry(price="$0.03") is not None
    assert "v2 on eip155:8453 will NOT be advertised" in caplog.text
    assert "https://v1only.example" in caplog.text


def test_the_v1_body_is_withheld_when_the_facilitator_is_v2_only(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 2)

    with caplog.at_level(logging.WARNING):
        assert module.accepts_entry(price="$0.03") is None
    assert module.payment_required_header(price="$0.03") != {}
    assert "v1 on base will NOT be advertised" in caplog.text


def test_both_versions_are_offered_when_the_facilitator_supports_both(monkeypatch):
    """The gate must refuse what cannot be verified, not become a third gate
    on the normal case."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 1, 2)

    assert module.accepts_entry(price="$0.03") is not None
    assert "PAYMENT-REQUIRED" in module.payment_required_header(price="$0.03")


def test_an_unreachable_facilitator_withholds_both_versions_and_says_so(monkeypatch, caplog):
    """Fail-closed: a challenge nobody can pay reads as nobody buying."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    def boom(version, network, scheme):
        raise ConnectionError("facilitator down")

    server.get_supported_kind = boom

    with caplog.at_level(logging.WARNING):
        assert module.payment_required_header(price="$0.03") == {}
        assert module.accepts_entry(price="$0.03") is None
    assert "ConnectionError: facilitator down" in caplog.text


def test_a_legacy_only_facilitator_is_offered_nothing_and_the_log_says_why(monkeypatch, caplog):
    """The library builds every verification's requirements under the CAIP-2
    name and refuses the legacy one outright (parse_price("$0.03", "base")
    raises "Unsupported network format"). So a facilitator listing only
    "base" can be offered nothing -- not even v1 -- or the node takes a
    signature it can never build the requirements to verify. Simulated:
    v1 offered, v1 paid, SchemeNotFoundError for eip155:8453 with the
    facilitator never called. That is the shape of the live rejections."""
    module = _load_x402(monkeypatch, facilitator="https://legacy.example")
    server = _install_fake_server(monkeypatch, module)
    server.get_supported_kind = (
        lambda version, network, scheme: object() if network == "base" else None
    )

    with caplog.at_level(logging.WARNING):
        assert module.accepts_entry(price="$0.03") is None
        assert module.payment_required_header(price="$0.03") == {}
    assert "lists 'base' but not 'eip155:8453'" in caplog.text
    assert "CAIP-2" in caplog.text


# --- verify/settle must work on a thread that hosts a running event loop ----
#
# Playwright's sync API (app/browser_pool.py) keeps a running loop in each
# worker thread for the life of the pooled browser, and anyio reuses those
# threads. The first real paid call against the deployed node died on this:
# the live log read "RuntimeError: asyncio.run() cannot be called from a
# running event loop", verify raised before the facilitator was contacted,
# and the caller saw a bare 402. Reproduced deterministically against a local
# node with MAX_CONCURRENT_AUDITS=1: one audit, then one paid call. These
# reproduce the same condition in-process: the sync entry points are invoked
# from inside a running loop, exactly as on a poisoned worker thread.

import asyncio as _asyncio


def _call_in_running_loop(fn, *args, **kwargs):
    async def _inner():
        return fn(*args, **kwargs)

    return _asyncio.run(_inner())


def test_verify_only_sync_works_inside_a_running_loop(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = _call_in_running_loop(module.verify_only_sync, "signed", price="$0.03")
    assert pending is not None


def test_settle_sync_works_inside_a_running_loop(monkeypatch):
    """The settle path is the one where this bug costs money directly: the
    audit was already delivered, and a settle that dies on the loop check
    delivers it unpaid."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    assert _call_in_running_loop(module.settle_sync, pending) is True


def test_no_bare_asyncio_run_remains_on_the_payment_path():
    """A new call site written with plain asyncio.run() reintroduces the bug
    on exactly the threads that have ever served an audit. Counted off the
    AST, not grepped, so docstrings and comments cannot confuse it."""
    import ast

    tree = ast.parse(X402_PATH.read_text())
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run_coro_sync")
    ok_lines = set(range(helper.lineno, helper.end_lineno + 1))
    stray = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "asyncio"
        and node.lineno not in ok_lines
    ]
    assert stray == [], f"bare asyncio.run() at lines {stray} -- use _run_coro_sync"


# --- the settlement receipt -------------------------------------------------
#
# x402 spec step 10: after settling, the resource server hands the
# facilitator's settle response back to the payer in PAYMENT-RESPONSE. Until
# this, a paying agent got an audit and no transaction hash -- proof of
# nothing to reconcile against its wallet. Found by simulate-paid-call.py,
# the first thing to read the headers of a paid 200; every test before it
# stopped at the status code.

_RECEIPT_TX = "0x" + "ab" * 32


def _real_settle_response():
    from x402.schemas import SettleResponse

    return SettleResponse(
        success=True, transaction=_RECEIPT_TX, network="eip155:8453", payer="0x" + "11" * 20
    )


def test_settle_sync_keeps_the_settlement_for_the_receipt(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending.settle_result is None, "nothing to receipt before settlement"
    assert module.settle_sync(pending) is True
    assert pending.settle_result.transaction == _RECEIPT_TX


def test_receipt_headers_decode_with_the_x402_client(monkeypatch):
    """Both header names, same value, and the x402 library's own decoder --
    the one a paying client uses -- reads the transaction back out."""
    from x402.http.utils import decode_payment_response_header

    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    module.settle_sync(pending)

    headers = module.receipt_headers(pending)

    assert set(headers) == {"PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"}
    assert headers["X-PAYMENT-RESPONSE"] == headers["PAYMENT-RESPONSE"]
    decoded = decode_payment_response_header(headers["PAYMENT-RESPONSE"])
    assert decoded.success is True
    assert decoded.transaction == _RECEIPT_TX


def test_no_receipt_for_a_refused_settlement(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settled=False)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.settle_sync(pending) is False
    assert pending.settle_result is None
    assert module.receipt_headers(pending) == {}


def test_no_receipt_before_settlement_and_none_without_a_payment(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.receipt_headers(pending) == {}
    assert module.receipt_headers(None) == {}


def test_an_unencodable_receipt_is_logged_and_never_raises(monkeypatch, caplog):
    """The money has moved by the time the receipt is built. A receipt that
    cannot be encoded is a bookkeeping gap to log, never a reason to turn a
    paid, delivered audit into a 500."""
    import types

    module = _load_x402(monkeypatch)
    pending = module.PendingPayment(None, None, "$0.03")
    pending.settle_result = types.SimpleNamespace(success=True)  # no model_dump_json

    with caplog.at_level(logging.WARNING):
        assert module.receipt_headers(pending) == {}
    assert "receipt could not be encoded" in caplog.text


# --- one event loop for every facilitator call -------------------------------
#
# The facilitator client keeps one httpx.AsyncClient with pooled keep-alive
# connections, and a pooled connection is bound to the loop that opened it.
# asyncio.run() per call meant a new loop per call, and against a keep-alive
# facilitator at 16 concurrent payers 56 of 96 payments died in this node with
# "Event loop is closed" / "bound to a different event loop" -- facilitator
# never asked. Every facilitator coroutine now runs on one long-lived loop.


def test_every_facilitator_call_runs_on_the_same_loop_from_any_thread(monkeypatch):
    import asyncio
    import threading

    module = _load_x402(monkeypatch)

    async def which_loop():
        return id(asyncio.get_running_loop())

    seen = []
    lock = threading.Lock()

    def worker():
        for _ in range(3):
            loop_id = module._run_coro_sync(which_loop())
            with lock:
                seen.append(loop_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 24
    assert len(set(seen)) == 1, "facilitator coroutines ran on more than one event loop"


def test_the_shared_loop_is_not_the_callers_loop(monkeypatch):
    """The #83 case: a caller thread that already hosts a running loop. The
    facilitator coroutine must run elsewhere, never on that loop."""
    import asyncio

    module = _load_x402(monkeypatch)

    async def which_loop():
        return id(asyncio.get_running_loop())

    async def caller():
        mine = id(asyncio.get_running_loop())
        theirs = module._run_coro_sync(which_loop())
        return mine, theirs

    mine, theirs = asyncio.run(caller())
    assert mine != theirs


def test_a_facilitator_call_that_never_answers_is_bounded(monkeypatch):
    import asyncio
    import time

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_FACILITATOR_CALL_TIMEOUT", 0.2)

    async def hangs():
        await asyncio.sleep(30)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        module._run_coro_sync(hangs())
    assert time.monotonic() - started < 5


# --- the facilitator outage must not become a node outage ---------------------


def test_an_unreachable_facilitator_is_not_re_probed_on_every_request(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    with pytest.raises(RuntimeError):
        module._get_server()
    with pytest.raises(RuntimeError) as second:
        module._get_server()
    assert boom.initialize.call_count == 1, "the outage was re-probed on the next request"
    assert "not retried" in str(second.value)
    assert "connection refused" in str(second.value), "the original failure is carried in the message"

    # Once the window has passed, it tries again.
    monkeypatch.setattr(module, "_SERVER_RETRY_SECONDS", 0.0)
    with pytest.raises(RuntimeError):
        module._get_server()
    assert boom.initialize.call_count == 2


def test_a_failed_facilitator_still_fails_closed_fast(monkeypatch):
    """During the back-off window the 402 simply carries no x402 -- no
    exception escapes, and nothing waits on the network."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    assert module.payment_required_header("$0.03") == {}
    assert module.accepts_entry("$0.03") is None
    assert module.verify_only_sync("signed-payment", price="$0.03") is None
    assert boom.initialize.call_count == 1


def test_supported_is_fetched_with_a_short_timeout(monkeypatch):
    """/supported is read under the module lock, so its timeout is how long
    every 402 waits when the facilitator is down. Eight seconds, not the
    library's thirty."""
    module = _load_x402(monkeypatch)
    client = module._FacilitatorClient(module.FacilitatorConfig(url="https://facilitator.example"))
    http = client._get_sync_client()
    try:
        assert http.timeout.connect == 8.0
        assert http.timeout.read == 8.0
    finally:
        http.close()


# --- replay: one signed authorization buys one audit -------------------------


def _payload_with_nonce(nonce: str):
    payload = MagicMock()
    payload.payload = {"authorization": {"nonce": nonce, "from": "0x" + "11" * 20}, "signature": "0xsig"}
    return payload


def _counting_fake_server(monkeypatch, module, **kwargs):
    server = _install_fake_server(monkeypatch, module, **kwargs)
    calls = {"verify": 0}
    original = server.verify_payment

    async def _verify(*a, **k):
        calls["verify"] += 1
        return await original(*a, **k)

    server.verify_payment = _verify
    return calls


def test_a_replayed_authorization_is_refused_before_the_facilitator(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xAA"))

    assert module.verify_only_sync("signed", price="$0.03") is not None
    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 1, "the replay reached the facilitator"
    assert "replayed authorization" in caplog.text


def test_a_nonce_is_released_when_the_facilitator_rejects_it(monkeypatch):
    """A retry after a transient rejection or outage is legitimate."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module, valid=False)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xBB"))

    assert module.verify_only_sync("signed", price="$0.03") is None
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 2, "a nonce whose verify failed must be admitted again"


def test_a_nonce_is_released_when_verify_raises(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xCC"))

    def facilitator_down(coro, *a, **k):
        coro.close()  # never awaited on purpose; close it so Python does not warn
        raise RuntimeError("facilitator down")

    monkeypatch.setattr(module, "_run_coro_sync", facilitator_down)

    assert module.verify_only_sync("signed", price="$0.03") is None
    monkeypatch.undo()
    module = _load_x402(monkeypatch)
    _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xCC"))
    assert module.verify_only_sync("signed", price="$0.03") is not None
    _ = calls


def test_a_nonce_stays_spent_after_a_failed_settle(monkeypatch):
    """Settle failed after delivery: one unpaid audit. Re-sending the same
    signature must not buy a second one."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module, settled=False)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xDD"))

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is False
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 1


def test_distinct_nonces_are_independent(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payloads = iter([_payload_with_nonce("0x01"), _payload_with_nonce("0x02")])
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: next(payloads))

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("b", price="$0.03") is not None
    assert calls["verify"] == 2


def test_the_replay_guard_is_case_insensitive_on_the_nonce(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payloads = iter([_payload_with_nonce("0xABCD"), _payload_with_nonce("0xabcd")])
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: next(payloads))

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("b", price="$0.03") is None
    assert calls["verify"] == 1


def test_a_payload_without_a_nonce_is_not_blocked(monkeypatch):
    """Other schemes carry no EIP-3009 nonce; the guard must not refuse them."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payload = MagicMock()
    payload.payload = {"something": "else"}
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("a", price="$0.03") is not None
    assert calls["verify"] == 2


# --- every settlement leaves one countable line in the log -------------------


def test_each_settlement_is_logged_with_its_transaction(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    with caplog.at_level(logging.INFO):
        assert module.settle_sync(pending) is True
    lines = [r for r in caplog.records if "x402 SETTLED" in r.getMessage()]
    assert len(lines) == 1
    assert _RECEIPT_TX in lines[0].getMessage()
    assert "$0.03" in lines[0].getMessage()


def test_a_refused_settlement_is_not_logged_as_settled(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settled=False)
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    with caplog.at_level(logging.INFO):
        module.settle_sync(pending)
    assert "x402 SETTLED" not in caplog.text


# ---------------------------------------------------------------------------
# The MCP transport: payment in, receipt out, one challenge for both transports.
# ---------------------------------------------------------------------------


def _signed_mcp_payload():
    """A real x402 v2 PaymentPayload, signed by a throwaway key against a
    challenge shaped exactly like this node's -- what the x402 MCP client
    puts in `_meta["x402/payment"]`."""
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo

    account = Account.from_key("0x" + "2" * 63 + "1")
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(account))
    challenge = PaymentRequired(
        x402Version=2,
        error="payment_required",
        resource=ResourceInfo(url="https://node.example/mcp", description="d", mimeType="application/json"),
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="eip155:8453",
                asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                amount="30000",
                payTo=VALID_PAY_TO,
                maxTimeoutSeconds=300,
                extra={"name": "USD Coin", "version": "2"},
            )
        ],
    )
    return client.create_payment_payload(challenge), account


def test_a_meta_payment_dict_reencodes_into_the_header_the_verify_path_reads(monkeypatch):
    from x402.http.utils import decode_payment_signature_header

    module = _load_x402(monkeypatch)
    payload, account = _signed_mcp_payload()
    wire = payload.model_dump(by_alias=True)  # what x402.mcp's client sends

    header = module.payment_header_from_meta(wire)
    assert isinstance(header, str) and header
    decoded = decode_payment_signature_header(header)
    assert decoded.payload["signature"] == wire["payload"]["signature"]
    assert decoded.payload["authorization"]["from"].lower() == account.address.lower()
    # The nonce the replay guard keys on survives the round trip.
    assert module._payment_nonce(decoded) == wire["payload"]["authorization"]["nonce"].lower()


def test_a_meta_payment_json_string_is_accepted_too(monkeypatch):
    """The official server accepts the payload as a JSON string as well."""
    from x402.http.utils import decode_payment_signature_header

    module = _load_x402(monkeypatch)
    payload, _ = _signed_mcp_payload()
    document = payload.model_dump_json(by_alias=True)
    header = module.payment_header_from_meta(document)
    assert decode_payment_signature_header(header).payload["signature"] == payload.payload["signature"]


def test_a_base64_header_in_meta_passes_through_untouched(monkeypatch):
    module = _load_x402(monkeypatch)
    from x402.http.utils import encode_payment_signature_header

    payload, _ = _signed_mcp_payload()
    already = encode_payment_signature_header(payload)
    assert module.payment_header_from_meta(already) == already


@pytest.mark.parametrize("junk", [None, "", "   ", 42, 4.2, True, "{not json", "[unterminated"])
def test_meta_payment_garbage_is_none_never_an_exception(monkeypatch, junk):
    module = _load_x402(monkeypatch)
    assert module.payment_header_from_meta(junk) is None


def test_receipt_meta_carries_the_settlement_where_the_mcp_client_reads_it(monkeypatch):
    from x402.mcp.types import MCPToolResult
    from x402.mcp.utils import extract_payment_response_from_meta

    module = _load_x402(monkeypatch)
    pending = module.PendingPayment(None, None, "$0.03")
    pending.settle_result = _real_settle_response()
    meta = module.receipt_meta(pending)
    assert set(meta) == {"x402/payment-response"}
    assert meta["x402/payment-response"]["transaction"] == pending.settle_result.transaction
    # Read back with the library's own extractor -- the consumer's parser.
    receipt = extract_payment_response_from_meta(
        MCPToolResult(content=[], is_error=False, meta=meta)
    )
    assert receipt is not None and receipt.transaction == pending.settle_result.transaction


def test_no_receipt_meta_for_a_refused_or_absent_settlement(monkeypatch):
    from x402.schemas import SettleResponse

    module = _load_x402(monkeypatch)
    assert module.receipt_meta(None) == {}
    pending = module.PendingPayment(None, None, "$0.03")
    assert module.receipt_meta(pending) == {}, "no settlement, no receipt"
    pending.settle_result = SettleResponse(
        success=False, error_reason="insufficient_funds", transaction="", network="eip155:8453"
    )
    assert module.receipt_meta(pending) == {}, "a receipt on a refused settle is a forged proof of payment"


def test_the_mcp_challenge_is_the_header_challenge(monkeypatch):
    """One builder for both transports: the dict the MCP paywall puts in
    structuredContent must be byte-for-byte what the PAYMENT-REQUIRED header
    decodes to, so the two cannot quote different prices or recipients."""
    from x402.http.utils import decode_payment_required_header

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: True)
    kwargs = dict(price="$0.10", resource_url="https://node.example/mcp",
                  description="bundle", extensions={"bazaar": {"info": {}, "schema": {}}})

    as_dict = module.payment_required_v2_dict(**kwargs)
    header = module.payment_required_header(**kwargs)["PAYMENT-REQUIRED"]
    decoded = decode_payment_required_header(header).model_dump(by_alias=True, exclude_none=True)
    assert as_dict == decoded
    assert as_dict["accepts"][0]["amount"] == "100000"
    assert as_dict["accepts"][0]["payTo"] == VALID_PAY_TO
    assert as_dict["resource"]["url"] == "https://node.example/mcp"
    assert "mimeType" in as_dict["resource"] and None not in as_dict["resource"].values()


def test_the_mcp_challenge_is_empty_whenever_the_header_would_be(monkeypatch):
    """Fail-closed together: not configured, or a facilitator that will not
    verify v2, and there is no structured challenge -- never one naming a
    recipient the node cannot settle to."""
    module = _load_x402(monkeypatch, facilitator=None)
    assert module.payment_required_v2_dict("$0.03") == {}
    assert module.payment_required_header("$0.03") == {}

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: False)
    assert module.payment_required_v2_dict("$0.03") == {}
    assert module.payment_required_header("$0.03") == {}


# --- 2026-09-06 audit: v1 payers, local match, reasons, honest settlement ---
#
# The audit's live harness paid the node with the official client on the v1
# path (PAYMENT-REQUIRED stripped, X-PAYMENT sent) and watched the node hand
# the facilitator a v1 payload beside v2-shaped requirements. The facilitator
# refused; the payer got a bare 402. Every pre-v2 client was being turned
# away by a rail the 402 advertised. These tests pin the repair and the three
# fixes found beside it.

from types import SimpleNamespace


class _V1Payload:
    """What decode_payment_signature_header returns for an X-PAYMENT header."""

    x402_version = 1

    def __init__(self, to, value, nonce="0xv1nonce", network="base"):
        self.scheme = "exact"
        self.network = network
        self.payload = {
            "authorization": {"to": to, "value": value, "nonce": nonce, "from": "0x" + "11" * 20},
            "signature": "0xsig",
        }

    def get_scheme(self):
        return self.scheme

    def get_network(self):
        return self.network


def _price_locally(monkeypatch, module):
    """parse_price without the (mocked) scheme: $0.03 -> 30000 atomic USDC."""
    def _priced(price):
        return SimpleNamespace(
            amount=str(int(round(float(price.lstrip("$")) * 1_000_000))),
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            extra={"name": "USD Coin", "version": "2"},
        )

    monkeypatch.setattr(module, "_priced_asset", _priced)
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: True)


def _capturing_verify(server):
    seen = {}

    async def _verify(payload, requirements, *a, **k):
        seen["payload"], seen["requirements"] = payload, requirements
        result = MagicMock()
        result.is_valid = True
        return result

    server.verify_payment = _verify
    return seen


def test_a_v1_payment_is_verified_against_v1_requirements(monkeypatch):
    """THE repair. A v1 payload must reach the facilitator beside a
    PaymentRequirementsV1 built from the same accepts[] entry the 402
    advertised -- legacy network name, maxAmountRequired, the resource --
    not the v2 object every payment used to be checked against."""
    from x402.schemas.v1 import PaymentRequirementsV1

    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _price_locally(monkeypatch, module)
    seen = _capturing_verify(server)
    monkeypatch.setattr(
        module, "decode_payment_signature_header",
        lambda h: _V1Payload(to=VALID_PAY_TO, value="30000"),
    )

    pending = module.verify_only_sync(
        "signed-v1", price="$0.03", resource_url="https://hubvibe-io.com/audit/wcag"
    )

    assert pending is not None, module.last_rejection()
    requirements = seen["requirements"]
    assert isinstance(requirements, PaymentRequirementsV1), type(requirements)
    assert requirements.network == "base"
    assert requirements.max_amount_required == "30000"
    assert requirements.pay_to == VALID_PAY_TO
    assert requirements.resource == "https://hubvibe-io.com/audit/wcag"
    # And settle carries the same v1 object, not a v2 one.
    assert pending.requirements[0] is requirements


def test_a_v2_payment_still_uses_the_v2_requirements(monkeypatch):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    seen = _capturing_verify(server)
    payload = MagicMock()
    payload.x402_version = 2
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)

    assert module.verify_only_sync("signed-v2", price="$0.03") is not None
    assert seen["requirements"] is server.build_payment_requirements.return_value[0]


def test_a_v1_payment_for_a_cheaper_route_is_refused_before_the_facilitator(monkeypatch):
    """A $0.03 authorization sent to the $0.10 route. Standard facilitators
    refuse it; this node no longer waits to find out, and says why."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _price_locally(monkeypatch, module)
    seen = _capturing_verify(server)
    monkeypatch.setattr(
        module, "decode_payment_signature_header",
        lambda h: _V1Payload(to=VALID_PAY_TO, value="30000"),
    )

    assert module.verify_only_sync("signed-v1", price="$0.10", resource_url="https://n/x") is None
    assert "requirements" not in seen, "the mismatch reached the facilitator"
    reason, detail = module.last_rejection()
    assert reason == "payment_mismatch"
    assert "amount" in detail


def test_a_v1_payment_to_another_recipient_is_refused_before_the_facilitator(monkeypatch):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _price_locally(monkeypatch, module)
    seen = _capturing_verify(server)
    monkeypatch.setattr(
        module, "decode_payment_signature_header",
        lambda h: _V1Payload(to="0x" + "ab" * 20, value="30000"),
    )

    assert module.verify_only_sync("signed-v1", price="$0.03", resource_url="https://n/x") is None
    assert "requirements" not in seen
    assert module.last_rejection()[0] == "payment_mismatch"


def test_a_v2_payment_that_matches_no_requirement_is_refused_before_the_facilitator(monkeypatch):
    """v2 delegates the comparison to the library's find_matching_requirements."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    seen = _capturing_verify(server)
    server.find_matching_requirements = MagicMock(return_value=None)
    payload = MagicMock()
    payload.x402_version = 2
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)

    assert module.verify_only_sync("signed-v2", price="$0.03") is None
    assert "requirements" not in seen
    assert module.last_rejection()[0] == "payment_mismatch"


def test_a_refused_v1_payment_releases_its_nonce_for_a_corrected_retry(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    _price_locally(monkeypatch, module)
    monkeypatch.setattr(
        module, "decode_payment_signature_header",
        lambda h: _V1Payload(to=VALID_PAY_TO, value="30000", nonce="0xsame"),
    )
    assert module.verify_only_sync("v1", price="$0.10", resource_url="https://n/x") is None
    # Same nonce, now against the route it was signed for: admitted.
    assert module.verify_only_sync("v1", price="$0.03", resource_url="https://n/x") is not None


def test_the_refusal_reason_is_left_for_the_402(monkeypatch):
    """A payer with an empty wallet and a payer facing a facilitator outage
    used to get byte-identical 402s. The route reads the reason from here."""
    module = _load_x402(monkeypatch, facilitator="https://fac.example")
    server = _install_fake_server(monkeypatch, module)

    _rejecting_verify(server, reason="insufficient_funds", message="wallet holds 0 USDC")
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert module.last_rejection() == ("insufficient_funds", "wallet holds 0 USDC")
    assert module.rejection_is_transient("insufficient_funds") is False

    _exploding_verify(server, ConnectionError("Name or service not known"))
    assert module.verify_only_sync("signed", price="$0.03") is None
    reason, detail = module.last_rejection()
    assert reason == "facilitator_unavailable"
    assert "retry" in detail
    assert module.rejection_is_transient(reason) is True


def test_a_replay_is_reported_as_a_replay(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xCC"))
    assert module.verify_only_sync("signed", price="$0.03") is not None
    assert module.last_rejection() is None, "a success must clear the previous reason"
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert module.last_rejection()[0] == "payment_replayed"


def test_an_undecodable_header_is_reported_as_such(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    def _boom(header):
        raise ValueError("not base64")

    monkeypatch.setattr(module, "decode_payment_signature_header", _boom)
    assert module.verify_only_sync("garbage", price="$0.03") is None
    assert module.last_rejection()[0] == "invalid_payment_payload"


def _settle_response(**overrides):
    from x402.schemas import SettleResponse

    fields = dict(success=False, network="eip155:8453", payer="0x" + "11" * 20, transaction="")
    fields.update(overrides)
    return SettleResponse(**fields)


def test_a_pending_settlement_is_pending_not_refused_and_hands_over_the_hash(monkeypatch):
    """settlement_pending with a transaction is money in flight. The payer
    used to be told 'not charged' and given no receipt while USDC landed."""
    module = _load_x402(monkeypatch)
    tx = "0x" + "ab" * 32
    _install_fake_server(
        monkeypatch, module, settled=False,
        settle_response=_settle_response(errorReason="settlement_pending", transaction=tx),
    )
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "pending"
    assert pending.settle_result is not None
    headers = module.receipt_headers(pending)
    assert headers, "a pending settlement with a hash must still hand the payer the hash"
    from x402.http.utils import decode_payment_response_header

    assert decode_payment_response_header(headers["PAYMENT-RESPONSE"]).transaction == tx
    assert module.receipt_meta(pending)[module.MCP_PAYMENT_RESPONSE_META_KEY]["transaction"] == tx


def test_a_refusal_that_names_a_reverted_transaction_is_still_a_refusal(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(
        monkeypatch, module, settled=False,
        settle_response=_settle_response(errorReason="transaction_reverted", transaction="0x" + "cd" * 32),
    )
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "refused"
    assert module.receipt_headers(pending) == {}
    assert module.receipt_meta(pending) == {}


def test_a_settle_timeout_is_unknown_not_refused(monkeypatch, caplog):
    """The facilitator may complete a settle this node stopped waiting for."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    def _timeout(coro, timeout=None):
        coro.close()
        raise TimeoutError("facilitator call exceeded 45s")

    monkeypatch.setattr(module, "_run_coro_sync", _timeout)
    with caplog.at_level(logging.WARNING):
        assert module.settle_sync(pending) is False
    assert pending.settle_state == "unknown"
    assert "UNKNOWN" in caplog.text
    assert "reconcile" in caplog.text


def test_a_settled_payment_is_marked_settled(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_settle_response(success=True, transaction="0x" + "ef" * 32))
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert module.settle_sync(pending) is True
    assert pending.settle_state == "settled"


def test_the_openapi_offer_prices_the_x402_rail_or_says_nothing(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    _price_locally(monkeypatch, module)
    monkeypatch.setattr(module, "payment_required_v2", lambda **kw: object())

    offer = module.discovery_offer("$0.10")
    assert offer["method"] == "x402"
    assert offer["amount"] == "100000"
    assert offer["payTo"] == VALID_PAY_TO
    assert offer["x402Versions"] == [1, 2]

    # Neither version verifiable -> no offer at all, same as the 402.
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: False)
    monkeypatch.setattr(module, "payment_required_v2", lambda **kw: None)
    assert module.discovery_offer("$0.10") == {}

    unconfigured = _load_x402(monkeypatch, facilitator=None)
    assert unconfigured.discovery_offer("$0.03") == {}


# --- httpx timeouts are not builtin TimeoutError ----------------------------
#
# The `except TimeoutError` branch was written to catch a settle whose outcome
# this node cannot know, and it caught only the 45s guard in _run_coro_sync.
# httpx's own timeouts inherit from Exception, not TimeoutError, and the
# client's default read timeout (30s) fires BEFORE that guard -- so every
# mid-flight settle timeout landed in the generic handler, was recorded
# "refused", and told the payer "this call was not charged" about a transfer
# that may have completed. That wording invites a second payment for one
# audit. Found while diagnosing the owner's 2026-09-08 failed settle.


def _raise_on_settle(monkeypatch, module, exc):
    """Make the next facilitator call raise. Patched AFTER verify, so the
    pending payment under test is a real one."""
    def _always(coro, timeout=None):
        coro.close()
        raise exc

    monkeypatch.setattr(module, "_run_coro_sync", _always)


@pytest.mark.parametrize("exc_name", ["ReadTimeout", "WriteTimeout", "RemoteProtocolError"])
def test_a_settle_that_may_have_reached_the_facilitator_is_unknown(monkeypatch, caplog, exc_name):
    """These fire after the request went out. The transfer may have landed."""
    import httpx

    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    exc_cls = getattr(httpx, exc_name)
    assert not issubclass(exc_cls, TimeoutError), (
        f"{exc_name} is now a builtin TimeoutError; this test's premise is stale"
    )
    _raise_on_settle(monkeypatch, module, exc_cls("boom"))

    with caplog.at_level(logging.WARNING):
        assert module.settle_sync(pending) is False
    assert pending.settle_state == "unknown", (
        "a settle that may have completed is being reported as not charged"
    )
    assert "UNKNOWN" in caplog.text
    assert "reconcile" in caplog.text
    # The owner greps one token for every settle failure.
    assert "x402 settle" in caplog.text


@pytest.mark.parametrize("exc_name", ["ConnectTimeout", "ConnectError", "PoolTimeout"])
def test_a_settle_that_never_left_this_node_is_refused_not_unknown(monkeypatch, exc_name):
    """The request was never sent, so the money certainly did not move.

    Calling these "unknown" would be the mirror-image lie: it sends the owner
    hunting the chain for a transfer that cannot exist, and blocks a re-run
    that is perfectly safe. ConnectTimeout is an httpx TimeoutException by
    inheritance, so the leaf class has to win the classification.
    """
    import httpx

    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    _raise_on_settle(monkeypatch, module, getattr(httpx, exc_name)("boom"))

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "refused", (
        "a request that never left is being reported as possibly-charged"
    )


def test_a_facilitator_that_answered_non_200_is_still_refused(monkeypatch):
    """The library raises ValueError on a non-200 from /settle, before any
    SettleResponse is parsed. The facilitator answered; it did not settle."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    _raise_on_settle(monkeypatch, module, ValueError("Facilitator settle failed (503): busy"))

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "refused"


def test_the_45s_guard_is_still_unknown(monkeypatch):
    """The original case must not regress while widening the net."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    _raise_on_settle(monkeypatch, module, TimeoutError("facilitator call exceeded 45s"))

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "unknown"


# --- the reason belongs on the response, not only in our log ----------------


def test_a_refusal_records_the_facilitators_reason_for_the_payer(monkeypatch):
    """The node is the only party that knows why a settle failed: the payer
    sees a 200 with an audit in it, and the operator must be logged into the
    box to read the log. On 2026-09-08 that cost the owner a night of
    grepping for an answer the node had in hand and discarded."""
    module = _load_x402(monkeypatch)
    _install_fake_server(
        monkeypatch, module, settled=False,
        settle_response=_settle_response(errorReason="insufficient_funds"),
    )
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "refused"
    assert "insufficient_funds" in (pending.settle_error or ""), (
        "the facilitator's reason is being discarded again"
    )


def test_an_exception_records_its_type_for_the_payer(monkeypatch):
    """"ValueError: Facilitator settle failed (503)" and "ReadTimeout" call
    for different actions. One generic sentence for both is the bug."""
    import httpx

    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    _raise_on_settle(monkeypatch, module, httpx.ReadTimeout("timed out"))

    assert module.settle_sync(pending) is False
    assert pending.settle_state == "unknown"
    assert "ReadTimeout" in (pending.settle_error or "")


def test_the_reason_is_one_bounded_line(monkeypatch):
    """A facilitator's error text is someone else's string -- it can be a
    page of HTML. It goes on a JSON body and into a log line, so flatten it
    and cap it rather than letting a remote service size our response."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    _raise_on_settle(monkeypatch, module, ValueError("x\ny\n" + "z" * 5000))

    assert module.settle_sync(pending) is False
    reason = pending.settle_error or ""
    assert "\n" not in reason and len(reason) <= 180, f"unbounded reason: {len(reason)}"
    assert "ValueError" in reason


def test_the_node_only_calls_methods_the_real_x402_server_has_and_awaits_them_right():
    """Every test in this file replaces the resource server with a stub that
    defines whatever this module happens to call. A call to a method the real
    library does NOT have therefore passes here and fails only when real money
    is on the line -- which is exactly what shipped on the payer side (#110:
    `http.post(...)` on a class that has never had `post`, so every wallet-paid
    Action run died with AttributeError before a byte left the process).

    That guard covered scripts/x402_pay.py. This is the same guard for the
    money-critical half: the node's own verify and settle.

    It also checks something the payer's guard does not, because it is the
    other way to call a real method wrongly: `await`-ing a sync method raises
    TypeError, and calling an async one without await returns a coroutine that
    reads as truthy and silently never runs. In settle_sync either lands in
    `except Exception`, is recorded "refused", and tells the payer their call
    was not charged -- a settle failure indistinguishable from the facilitator
    saying no. Imported hard, never skipped: x402 is pinned in requirements.txt
    and a skip here restores the blind spot.
    """
    import ast
    import inspect

    from x402 import x402ResourceServer

    module_path = REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py"
    tree = ast.parse(module_path.read_text())

    # Names bound to the resource server, so a rename does not blind this.
    server_names = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "_get_server"
    }
    assert server_names, "no _get_server() assignment found; this guard sees nothing"

    def server_calls(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in server_names
        )

    called = {n.func.attr for n in ast.walk(tree) if server_calls(n)}
    assert called, "this module calls nothing on the resource server; guard sees nothing"

    missing = sorted(m for m in called if not hasattr(x402ResourceServer, m))
    assert not missing, (
        "x402_payments.py calls %s on x402ResourceServer, which has no such "
        "method -- every payment would die before reaching the facilitator. "
        "Available: %s"
        % (missing, sorted(m for m in dir(x402ResourceServer) if not m.startswith("_")))
    )

    awaited = {
        n.value.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Await) and server_calls(n.value)
    }
    wrong = []
    for name in sorted(called):
        is_async = inspect.iscoroutinefunction(getattr(x402ResourceServer, name))
        if is_async and name not in awaited:
            wrong.append(f"{name} is async and is called without await somewhere")
        if not is_async and name in awaited:
            wrong.append(f"{name} is sync and is awaited")
    assert not wrong, (
        "await mismatch against the real library: %s. Awaiting a sync method "
        "raises TypeError; not awaiting an async one silently never runs it. "
        "Inside settle_sync either is recorded 'refused' and tells the payer "
        "they were not charged." % wrong
    )

    # Existence and await-ness are two of the three ways to call a real method
    # wrongly. The third is arity: the stubs here accept whatever they are
    # handed, so a library that grows a required argument, or loses one, binds
    # fine in tests and raises TypeError in front of a paying agent.
    unbindable = []
    for node in ast.walk(tree):
        if not server_calls(node):
            continue
        try:
            sig = inspect.signature(getattr(x402ResourceServer, node.func.attr))
        except (TypeError, ValueError):
            continue
        positional = [object()] * (len(node.args) + 1)  # +1 for self
        keywords = {kw.arg: object() for kw in node.keywords if kw.arg}
        if any(kw.arg is None for kw in node.keywords):
            continue  # **kwargs splat: arity is not statically knowable
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        try:
            sig.bind(*positional, **keywords)
        except TypeError as exc:
            unbindable.append(f"{node.func.attr} at line {node.lineno}: {exc}")
    assert not unbindable, (
        "these calls do not fit the real library's signature: %s -- they raise "
        "TypeError against the pinned x402, and the stubs in this file accept "
        "anything, so nothing else here would notice." % unbindable
    )


# --- Coinbase CDP facilitator --------------------------------------------
#
# CDP is the facilitator that matters for discovery: it settles on mainnet and
# it is what lists a resource in the x402 Bazaar, the index the official x402
# SDKs read by default. It signs a fresh JWT per call, bound to that call's
# method, host and FULL path -- so unlike a bearer token these headers cannot
# be computed once and reused, and a wrong path means every request is
# rejected with a signature that looks valid. Restored 2026-09-12 after the
# owner opened a new CDP account (the earlier one was blocked on a DBA review).


def _cdp_secret():
    """A real Ed25519 keypair in CDP's base64(private||public) format, so the
    SDK actually signs rather than being mocked into agreeing with us."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw + pub).decode()


def _load_cdp(monkeypatch, url="https://api.cdp.coinbase.com/platform/v2/x402", **kw):
    monkeypatch.setenv("CDP_API_KEY_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("CDP_API_KEY_SECRET", _cdp_secret())
    return _load_x402(monkeypatch, facilitator=url, **kw)


def _jwt_claims(header_value):
    import base64

    payload = header_value.split(" ", 1)[1].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_cdp_signs_each_endpoint_with_its_own_method_and_full_path(monkeypatch):
    """The signed `uris` claim must name the real method and the real path,
    prefix included. CDP's facilitator lives under /platform/v2/x402, and a
    JWT signed for the bare path authenticates nothing -- which would present
    as x402 configured and every payment rejected."""
    module = _load_cdp(monkeypatch)
    headers = module._auth_provider().get_auth_headers()

    expected = {
        "verify": "POST api.cdp.coinbase.com/platform/v2/x402/verify",
        "settle": "POST api.cdp.coinbase.com/platform/v2/x402/settle",
        "supported": "GET api.cdp.coinbase.com/platform/v2/x402/supported",
        "bazaar": "GET api.cdp.coinbase.com/platform/v2/x402/discovery/resources",
    }
    for endpoint, uri in expected.items():
        claims = _jwt_claims(getattr(headers, endpoint)["Authorization"])
        assert claims["uris"] == [uri], f"{endpoint} signed for the wrong request"


def test_cdp_tokens_are_distinct_per_endpoint(monkeypatch):
    """Reusing one token across endpoints is the obvious shortcut and it does
    not work -- each is bound to its own method and path."""
    module = _load_cdp(monkeypatch)
    headers = module._auth_provider().get_auth_headers()
    tokens = {
        getattr(headers, e)["Authorization"]
        for e in ("verify", "settle", "supported", "bazaar")
    }
    assert len(tokens) == 4


def test_cdp_takes_precedence_over_static_headers(monkeypatch):
    """Nobody sets a CDP key pair by accident; it is the more specific config."""
    module = _load_cdp(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer stale"})
    )
    provider = module._auth_provider()
    assert type(provider).__name__ == "_CdpAuthProvider"
    assert "stale" not in provider.get_auth_headers().verify["Authorization"]


def test_cdp_rejects_a_facilitator_url_it_cannot_sign_for(monkeypatch):
    """Without a host there is nothing to bind the JWT to, so fail loudly at
    construction rather than emitting tokens no facilitator will accept."""
    with pytest.raises(ValueError):
        _load_cdp(monkeypatch, url="not-a-url")._auth_provider()


def test_static_headers_still_used_when_no_cdp_credentials(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok"})
    )
    assert type(module._auth_provider()).__name__ == "_StaticAuthProvider"


def test_cdp_credentials_are_not_sent_to_a_non_coinbase_facilitator(monkeypatch):
    """A CDP token is a JWT bound to Coinbase's own host, not a shared secret
    anyone else could validate. Signing a third-party facilitator's requests
    with one is meaningless at best and a 401 at worst -- and a 401 here is
    the worst failure this file knows: x402 still advertised on every 402,
    every payment rejected, indistinguishable from nobody buying."""
    module = _load_cdp(monkeypatch, url="https://facilitator.payai.network")
    assert module._auth_provider() is None, (
        "CDP credentials were handed to a facilitator that cannot validate them"
    )


def test_the_documented_one_variable_swap_actually_works(monkeypatch):
    """The whole point: with the CDP key pair still mounted, changing only
    X402_FACILITATOR_URL must leave a working, advertised x402 rail."""
    module = _load_cdp(monkeypatch, url="https://facilitator.payai.network")
    _install_fake_server(monkeypatch, module)
    assert module.is_configured(), "the rail stopped being advertised"
    assert module.accepts_entry(price="$0.03") is not None


def test_static_headers_still_reach_a_non_coinbase_facilitator(monkeypatch):
    """Ignoring CDP must fall through to the generic credential path, not
    swallow it -- a facilitator that wants a bearer token still gets one."""
    module = _load_cdp(
        monkeypatch,
        url="https://facilitator.example.com",
        auth_headers=json.dumps({"Authorization": "Bearer tok"}),
    )
    provider = module._auth_provider()
    assert provider is not None
    assert provider.get_auth_headers().verify == {"Authorization": "Bearer tok"}


def test_cdp_is_still_used_for_coinbase_hosts(monkeypatch):
    """The fall-through must not disarm CDP where it is the right credential."""
    module = _load_cdp(monkeypatch)
    provider = module._auth_provider()
    assert provider is not None
    assert type(provider).__name__ == "_CdpAuthProvider"


@pytest.mark.parametrize(
    "host",
    [
        "https://api.cdp.coinbase.com/platform/v2/x402",
        "https://coinbase.com/x402",
        "https://user:pw@api.cdp.coinbase.com:443/platform/v2/x402",
    ],
)
def test_coinbase_hosts_are_recognised_through_port_and_userinfo(monkeypatch, host):
    module = _load_cdp(monkeypatch, url=host)
    assert module._host_is_coinbase(host) is True


@pytest.mark.parametrize(
    "host",
    [
        # The lookalike that matters: suffix matching on "coinbase.com" without
        # the leading dot would accept this and hand over the key pair.
        "https://api.cdp.coinbase.com.evil.example/x402",
        "https://notcoinbase.com/x402",
        "https://facilitator.payai.network",
    ],
)
def test_lookalike_hosts_never_receive_cdp_credentials(monkeypatch, host):
    module = _load_cdp(monkeypatch, url=host)
    assert module._host_is_coinbase(host) is False
    assert module._auth_provider() is None


# --- Solana: the second rail ---------------------------------------------
#
# Both facilitators this node has used list Solana first among their networks
# and their indexes are full of Solana-paid resources. A Base-only challenge
# sends every Solana-wallet agent away unpaid. The rail is gated three ways:
# a valid Solana address the owner holds, the facilitator listing `exact` on
# Solana mainnet, and a fee payer declared for it. Nothing else advertises it.

SOLANA_NET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FEE_PAYER = "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww"


def _solana_address():
    from solders.keypair import Keypair

    return str(Keypair().pubkey())


def _load_solana(monkeypatch, address, **kw):
    if address is None:
        monkeypatch.delenv("X402_SOLANA_PAY_TO_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("X402_SOLANA_PAY_TO_ADDRESS", address)
    return _load_x402(monkeypatch, **kw)


class _Kind:
    def __init__(self, extra=None):
        self.extra = extra or {}


def _facilitator_with_solana(server, fee_payer=FEE_PAYER):
    def kind(version, network, scheme):
        if network == SOLANA_NET:
            return _Kind({"feePayer": fee_payer} if fee_payer else {}) if version == 2 else None
        return object()

    server.get_supported_kind = kind


def _v2_networks(module):
    challenge = module.payment_required_v2(price="$0.03", resource_url="https://hubvibe-io.com/audit/wcag")
    assert challenge is not None, "the v2 challenge could not be built at all"
    return {a.network: a for a in challenge.accepts}


def test_no_solana_address_means_no_solana_rail(monkeypatch):
    module = _load_solana(monkeypatch, None)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server)
    assert SOLANA_NET not in _v2_networks(module)


def test_the_solana_rail_is_offered_with_the_facilitators_fee_payer(monkeypatch):
    address = _solana_address()
    module = _load_solana(monkeypatch, address)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server)

    rails = _v2_networks(module)
    assert "eip155:8453" in rails, "Base must stay first and live"
    sol = rails[SOLANA_NET]
    assert sol.pay_to == address
    assert sol.asset == USDC_SOLANA, "the asset must be the USDC mint the facilitator settles"
    assert sol.amount == "30000", "$0.03 in USDC's six decimals"
    assert sol.extra["feePayer"] == FEE_PAYER, "a client cannot build the transfer without it"
    assert list(rails)[0] == "eip155:8453"


def test_the_solana_rail_is_withheld_when_the_facilitator_does_not_list_solana(monkeypatch):
    module = _load_solana(monkeypatch, _solana_address())
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 1, 2)  # every network answered, but no feePayer
    assert SOLANA_NET not in _v2_networks(module)


def test_the_solana_rail_is_withheld_without_a_fee_payer(monkeypatch, caplog):
    module = _load_solana(monkeypatch, _solana_address())
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server, fee_payer=None)
    with caplog.at_level(logging.WARNING):
        assert SOLANA_NET not in _v2_networks(module)
    assert "no feePayer" in caplog.text


@pytest.mark.parametrize("bad", ["not-base58", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
                                 "11111111111111111111111111111111", "   "])
def test_a_malformed_or_unownable_solana_address_turns_the_rail_off_loudly(monkeypatch, bad, caplog):
    """An EVM address, a typo, or the all-zero system key: none can receive
    USDC on Solana. Advertising any of them is advertising a recipient that
    cannot receive -- the one fault this file exists to prevent."""
    module = _load_solana(monkeypatch, bad)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server)
    with caplog.at_level(logging.WARNING):
        assert module.solana_configured() is False
        assert SOLANA_NET not in _v2_networks(module)
    assert "Solana rail OFF" in caplog.text
    assert module.is_configured(), "Base must be untouched by a bad Solana address"


def test_a_solana_payer_is_verified_and_settled_against_the_solana_requirement(monkeypatch):
    """With two rails advertised, the payload names which one it paid. Verify
    and settle must use THAT requirement: a Solana payload checked against
    the Base requirement is refused by every facilitator."""
    module = _load_solana(monkeypatch, _solana_address())
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server)

    base_req = MagicMock(network="eip155:8453", amount="30000")
    sol_req = MagicMock(network=SOLANA_NET, amount="30000")
    server.build_payment_requirements = MagicMock(side_effect=[[base_req], [sol_req]])

    payload = MagicMock(x402_version=2)
    payload.accepted.network = SOLANA_NET
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)
    seen = {}

    async def _verify(p, req):
        seen["verify"] = req
        return MagicMock(is_valid=True)

    async def _settle(p, req):
        seen["settle"] = req
        return MagicMock(success=True, transaction="sig", network=SOLANA_NET, payer="p")

    server.verify_payment = _verify
    server.settle_payment = _settle

    pending = module.verify_only_sync("signed-solana", price="$0.03")
    assert pending is not None, module.last_rejection()
    assert seen["verify"] is sol_req
    assert pending.requirements == [sol_req]
    assert module.settle_sync(pending) is True
    assert seen["settle"] is sol_req


def test_a_base_payer_still_gets_the_base_requirement_when_both_rails_exist(monkeypatch):
    module = _load_solana(monkeypatch, _solana_address())
    server = _install_fake_server(monkeypatch, module)
    _facilitator_with_solana(server)
    base_req = MagicMock(network="eip155:8453", amount="30000")
    sol_req = MagicMock(network=SOLANA_NET, amount="30000")
    server.build_payment_requirements = MagicMock(side_effect=[[base_req], [sol_req]])
    payload = MagicMock(x402_version=2)
    payload.accepted.network = "eip155:8453"
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)
    seen = {}

    async def _verify(p, req):
        seen["verify"] = req
        return MagicMock(is_valid=True)

    server.verify_payment = _verify
    pending = module.verify_only_sync("signed-base", price="$0.03")
    assert pending is not None, module.last_rejection()
    assert seen["verify"] is base_req
