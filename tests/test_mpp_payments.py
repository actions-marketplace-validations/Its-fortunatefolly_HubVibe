import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MPP_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "mpp_payments.py"


def _load_mpp(monkeypatch, **env):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("MPP_STRIPE_NETWORK_PROFILE_ID", raising=False)
    monkeypatch.delenv("MPP_TEMPO_RPC_URL", raising=False)
    monkeypatch.delenv("MPP_TEMPO_TOKEN_ADDRESS", raising=False)
    monkeypatch.delenv("MPP_TEMPO_RECIPIENT_ADDRESS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("mpp_payments", MPP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.stripe.api_key = env.get("STRIPE_SECRET_KEY")
    return module


def test_not_configured_offers_no_challenges(monkeypatch):
    module = _load_mpp(monkeypatch)
    assert module.is_configured() is False
    assert module.www_authenticate_headers(realm="api.example.com") == []


def _both_rails(monkeypatch, **extra):
    return _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_STRIPE_NETWORK_PROFILE_ID="profile_test123",
        MPP_TEMPO_RPC_URL="https://tempo-rpc.example.com",
        MPP_TEMPO_TOKEN_ADDRESS="0x20c0000000000000000000000000000000000000",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
        **extra,
    )


def test_configured_offers_both_method_challenges(monkeypatch):
    module = _both_rails(monkeypatch)
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.50)
    assert len(headers) == 2
    assert any('method="stripe"' in h for h in headers)
    assert any('method="tempo"' in h for h in headers)
    assert all('realm="api.example.com"' in h for h in headers)


def test_the_spt_rail_is_not_offered_below_stripes_minimum(monkeypatch):
    """Stripe rejects a card SPT charge under 0.50 USD outright.

    Every route here is priced at $0.05-$0.15, so offering this rail on them
    would hand an agent a challenge, take its single-use token, and fail at
    the API every time -- a rail advertised and unable to settle, which is the
    exact shape of the bug that made the x402 rail unpayable for months. Tempo
    has no such floor and is unaffected.
    """
    module = _both_rails(monkeypatch)
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.03)

    # No per-call stripe charge: Stripe would reject it on amount alone.
    assert not any('intent="charge"' in h and 'method="stripe"' in h for h in headers)
    assert any('method="tempo"' in h for h in headers)

    # But a TOP-UP is offered, because a block is the only thing this rail can
    # sell below the floor. Leaving it out would drop the fiat option entirely
    # rather than offering the one shape of it that can settle.
    assert any('method="stripe"' in h and 'intent="topup"' in h for h in headers)

    entries = module.accepts_entries(price_usd=0.10)
    assert [entry["method"] for entry in entries] == ["tempo"]


def test_the_spt_floor_is_stripes_number_not_ours(monkeypatch):
    """A deployment whose Stripe account carries a different minimum says so
    with a variable, not a patch."""
    module = _both_rails(monkeypatch, MPP_STRIPE_MIN_CENTS="3")
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.03)
    assert any('method="stripe"' in h for h in headers)


def test_a_malformed_tempo_recipient_turns_the_rail_off(monkeypatch):
    """39 hex characters is what `mppx validate` actually caught on
    2026-08-29, on all six paid routes, while every check here was green:
    `tempo_configured()` only asked whether the variable was non-empty. The
    x402 rail shipped a 16-hex address and later the zero address through
    exactly this gap. Nothing may be advertised -- not the challenge, not the
    accepts entry, not the discovery offer."""
    module = _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x32b08c5e927c69877d0fcab35618c265674922b",
    )
    assert module.tempo_configured() is False
    assert module.www_authenticate_headers(realm="api.example.com") == []
    assert module.accepts_entries(price_usd=0.03) == []
    assert module.discovery_offers(0.03) == []


def test_a_zero_tempo_recipient_turns_the_rail_off(monkeypatch):
    """0x + 40 zeros passes every format gate and can never receive a
    transfer -- the canonical way to satisfy a shape check with a value that
    answers "no" to the only question that matters."""
    module = _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x" + "0" * 40,
    )
    assert module.tempo_configured() is False
    assert module.www_authenticate_headers(realm="api.example.com") == []


def test_a_well_formed_tempo_recipient_keeps_the_rail_on(monkeypatch):
    """The guard must not be so eager it kills a working rail."""
    module = _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
    )
    assert module.tempo_configured() is True
    assert len(module.www_authenticate_headers(realm="api.example.com")) == 1


def test_discovery_offers_mirror_the_challenge_gating(monkeypatch):
    """x-payment-info offers come from here, and they must obey the same
    fail-closed rules as the WWW-Authenticate challenges: tempo at any amount,
    stripe only at or above its SPT floor."""
    module = _both_rails(monkeypatch)

    offers = module.discovery_offers(0.03, description="d")
    assert [o["method"] for o in offers] == ["tempo"]
    assert offers[0]["amount"] == "30000"  # base units, not cents
    assert offers[0]["currency"] == "0x20c0000000000000000000000000000000000000"
    assert offers[0]["intent"] == "charge"

    offers = module.discovery_offers(0.50)
    assert [o["method"] for o in offers] == ["stripe", "tempo"]
    assert offers[0]["amount"] == "50"  # cents for stripe

    # The reference schema requires amount to be an integer string.
    for offer in offers:
        assert offer["amount"].isdigit()


def test_discovery_offers_are_empty_when_no_rail_is_configured(monkeypatch):
    """No rails -> no offers -> no x-payment-info anywhere. Advertising a
    paid endpoint with no way to pay is the exact thing this codebase
    exists to never do."""
    module = _load_mpp(monkeypatch)
    assert module.discovery_offers(0.03) == []


def test_a_stale_challenge_under_the_minimum_is_not_charged(monkeypatch):
    """Challenges live for their whole TTL, so one minted before the floor
    existed can still arrive. Burning a caller's single-use SPT on a charge
    Stripe will reject is worse than refusing it."""
    module = _both_rails(monkeypatch)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "charge", {"amount": "3", "currency": "usd"}
    )

    # Recorded rather than raised: _verify_stripe catches every exception and
    # fails closed, so an AssertionError in here would be swallowed and the
    # test would pass whether or not Stripe was called.
    calls = []
    monkeypatch.setattr(
        module.stripe.PaymentIntent,
        "create",
        lambda **kwargs: calls.append(kwargs) or {"status": "succeeded"},
    )
    assert module._verify_stripe(challenge, {"spt": "spt_123"}) is False
    assert calls == [], "a charge under Stripe's own minimum was sent anyway"


def test_challenge_binding_rejects_tampered_id(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    assert module._verify_challenge_binding(challenge, "api.example.com") is True

    tampered = dict(challenge)
    tampered["id"] = "x" + tampered["id"]
    assert module._verify_challenge_binding(tampered, "api.example.com") is False


def test_challenge_binding_rejects_wrong_realm(monkeypatch):
    # A challenge issued for one hostname must not validate against another
    # -- this is what stops a challenge from being replayed cross-deployment.
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    assert module._verify_challenge_binding(challenge, "evil.example.com") is False


def test_challenge_binding_rejects_expired(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    expired = dict(challenge)
    expired["expires"] = "2020-01-01T00:00:00Z"
    expired["id"] = module._challenge_id(
        expired["realm"], expired["method"], expired["intent"], expired["request"], expired["expires"], None, None
    )
    assert module._verify_challenge_binding(expired, "api.example.com") is False


def test_verify_and_settle_rejects_malformed_credential(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    assert module.verify_and_settle_sync("not-valid-base64url-json", realm="api.example.com") is False


def test_verify_and_settle_rejects_unknown_method(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "paypal", "charge", {"amount": "3"})
    credential = module._b64url_encode(json.dumps({"challenge": challenge, "payload": {}}).encode())
    assert module.verify_and_settle_sync(credential, realm="api.example.com") is False


def test_verify_tempo_rejects_non_hash_payload_types(monkeypatch):
    # "transaction" (pull mode) and "proof" (zero-amount) aren't implemented
    # -- must fail closed, not be silently treated as valid.
    module = _load_mpp(
        monkeypatch,
        MPP_TEMPO_RPC_URL="https://tempo-rpc.example.com",
        MPP_TEMPO_TOKEN_ADDRESS="0x20c0000000000000000000000000000000000000",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
        STRIPE_SECRET_KEY="sk_test_fake",
    )
    assert module._verify_tempo({}, {"type": "transaction", "signature": "0xabc"}) is False
    assert module._verify_tempo({}, {"type": "proof", "signature": "0xabc"}) is False
    assert module._verify_tempo({}, {}) is False


def test_verify_stripe_rejects_non_spt_token(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    assert module._verify_stripe({}, {"spt": "not-an-spt"}) is False
    assert module._verify_stripe({}, {}) is False


def test_a_topup_is_not_offered_above_the_floor(monkeypatch):
    """Above the floor the per-call charge is the better offer, and a top-up
    beside it is just a second way to pay that nobody asked for."""
    module = _both_rails(monkeypatch)
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.50)
    assert any('method="stripe"' in h and 'intent="charge"' in h for h in headers)
    assert not any('intent="topup"' in h for h in headers)


def test_a_topup_credential_is_not_consumed_as_a_per_call_payment(monkeypatch):
    """The two intents mean different things. Letting a $0.50 top-up settle
    through the per-call path would take the money, serve one $0.05 audit, and
    silently keep the rest."""
    module = _both_rails(monkeypatch)
    # Stripe is stubbed to ACCEPT, so the only thing that can refuse this is
    # the intent check itself. Without the stub the fake key fails at the API
    # and the test passes whether or not the guard exists -- exactly the
    # form-not-function trap this file has been bitten by before.
    monkeypatch.setattr(module, "_verify_stripe", lambda challenge, payload: True)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "topup", {"amount": "50", "currency": "usd"}
    )
    credential = module._b64url_encode(
        __import__("json").dumps({"challenge": challenge, "payload": {"spt": "spt_1"}}).encode()
    )
    assert module.verify_and_settle_sync(credential, realm="api.example.com") is False


def test_a_topup_below_stripes_floor_is_refused(monkeypatch):
    """A challenge quoting less than Stripe will accept cannot settle, and
    crediting on it would hand out balance for a charge that never cleared."""
    module = _both_rails(monkeypatch)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "topup", {"amount": "3", "currency": "usd"}
    )
    credential = module._b64url_encode(
        __import__("json").dumps({"challenge": challenge, "payload": {"spt": "spt_1"}}).encode()
    )
    assert module.settle_topup_sync(credential, realm="api.example.com") is None


def test_a_settled_topup_returns_the_challenge_amount_not_the_callers(monkeypatch):
    """The amount comes from the HMAC-bound challenge, so an agent cannot claim
    more credit than it paid for."""
    module = _both_rails(monkeypatch)
    monkeypatch.setattr(module, "_verify_stripe", lambda challenge, payload: True)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "topup", {"amount": "50", "currency": "usd"}
    )
    credential = module._b64url_encode(
        __import__("json").dumps(
            {"challenge": challenge, "payload": {"spt": "spt_1", "amount": "99999"}}
        ).encode()
    )
    assert module.settle_topup_sync(credential, realm="api.example.com") == 50


def test_a_topup_for_the_wrong_realm_is_refused(monkeypatch):
    """Same replay protection as every other challenge here."""
    module = _both_rails(monkeypatch)
    monkeypatch.setattr(module, "_verify_stripe", lambda challenge, payload: True)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "topup", {"amount": "50", "currency": "usd"}
    )
    credential = module._b64url_encode(
        __import__("json").dumps({"challenge": challenge, "payload": {"spt": "spt_1"}}).encode()
    )
    assert module.settle_topup_sync(credential, realm="evil.example.com") is None
