import importlib.util
import re
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"


def _load_main(monkeypatch, api_key="test-key"):
    if api_key is not None:
        monkeypatch.setenv("AUDIT_API_KEY", api_key)
    else:
        monkeypatch.delenv("AUDIT_API_KEY", raising=False)
    # No Stripe config in CI -- billing.is_configured() must be False, so
    # these tests only exercise the auth/rate-limit paths, never Firestore
    # or Stripe network calls.
    for var in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_METERED_PRICE_ID",
        "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    spec = importlib.util.spec_from_file_location("wcag_audit_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The 402 builders ask the facilitator which x402 versions it will verify
    # (x402_payments._facilitator_supports) and fail closed when it cannot be
    # reached. facilitator.example does not exist. These tests are about the
    # shape of the 402, not about the facilitator, so they answer "yes"; the
    # gate itself is tested in test_x402_payments.py against a fake server.
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    return module


def test_health_check(monkeypatch):
    """Served at BOTH paths. Cloud Run's frontend reserves /healthz and
    answers it with its own HTML 404 before the request reaches the
    container -- confirmed against the live service -- so /health is the one
    that actually works there, and it must not regress."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} did not answer"
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "wcag-audit-engine"
        # The probe also reports whether a browser is present, because a node
        # that cannot launch one cannot serve a single paid audit.
        assert "browser" in body


def test_landing_page_served_at_root(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_llms_txt_served_at_root(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "/audit/bundle" in response.text


def test_robots_txt_served_and_points_to_sitemap(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "sitemap.xml" in response.text


def test_sitemap_xml_served(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert "xml" in response.headers["content-type"]
    assert "<urlset" in response.text


def test_mcp_json_served_and_matches_repo_manifest(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/mcp.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    body = response.json()
    tool_names = {tool["name"] for tool in body["tools"]}
    # An exact pin: any tool served that this does not explain, or missing
    # from it, fails here.
    expected = {"audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle",
                "hubvibe_predictive_probability_engine"}
    assert tool_names == expected


def test_mcp_json_never_advertises_a_rail_that_cannot_settle(monkeypatch):
    """The hole in this codebase's central rule.

    /.well-known/agent.json and every 402 already omit rails that are not
    configured. /mcp.json did not: it was a static file asserting
    `["stripe_api_key", "x402", "mpp"]` unconditionally, and it went on
    asserting x402 after x402 was switched off on the live service. The MCP
    registry points agents at this manifest, so an agent would have built a
    payment for a rail this deployment cannot settle -- and a failed payment
    is indistinguishable, from our side, from nobody buying.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "stripe_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    body = TestClient(module.app).get("/mcp.json").json()
    assert body["auth"]["methods"] == []
    assert "x402" not in body["auth"]["methods"]


def test_mcp_json_lists_a_rail_once_it_can_settle(monkeypatch):
    """The other half: a configured rail must actually show up, or agents
    that could pay are turned away."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: True)
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    monkeypatch.setattr(module.mpp_payments, "stripe_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    body = TestClient(module.app).get("/mcp.json").json()
    assert body["auth"]["methods"] == ["x402"]


def test_mcp_json_prices_come_from_the_catalog(monkeypatch):
    """_CATALOG exists so the manifest, the price an agent reads, and the
    price the route charges cannot drift apart. A second hand-maintained copy
    of the numbers in a static file defeats that by construction."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    body = TestClient(module.app).get("/mcp.json").json()

    catalog = {entry["path"]: entry["price_usd"] for entry in module._CATALOG}
    # A worker-backed tool is priced from ITS catalog row, the same way.
    for worker in module._mcp_worker_tools().values():
        catalog[worker.path] = worker.price_usd
    served = {
        tool["httpEndpoint"]["path"]: tool["httpEndpoint"]["price_usd"]
        for tool in body["tools"]
    }
    assert served == catalog


def test_mcp_json_prices_follow_a_catalog_change(monkeypatch):
    """Proves the price is actually read from _CATALOG rather than merely
    happening to match the static file today."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    patched = [dict(entry) for entry in module._CATALOG]
    for entry in patched:
        if entry["path"] == "/audit/bundle":
            entry["price_usd"] = 0.25
    monkeypatch.setattr(module, "_CATALOG", patched)

    body = TestClient(module.app).get("/mcp.json").json()
    bundle = [t for t in body["tools"] if t["httpEndpoint"]["path"] == "/audit/bundle"][0]
    assert bundle["httpEndpoint"]["price_usd"] == 0.25


def test_agent_manifest_advertises_payment(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/.well-known/agent.json")
    body = response.json()
    assert body["endpoints"][0]["payment_required"] is True


def test_audit_requires_api_key(monkeypatch):
    # Neither X-API-Key nor X-PAYMENT attached -- caller gets a 402 telling
    # them how to pay, not a silent pass and not a bare 401.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    assert response.status_code == 402
    body = response.json()
    assert body["price"] == "$0.05"
    assert body["price_usd"] == 0.05
    assert body["error"] == "payment_required"
    # Machine-readable list of rails that can actually settle here.
    assert isinstance(body["accepts"], list)
    assert body["alternative"]["header"] == "X-API-Key"


def test_402_never_advertises_x402_when_it_cannot_settle(monkeypatch):
    """Regression: an unconfigured x402 used to still emit
    accepted_payment_header=X-PAYMENT with payTo=null, telling a paying agent
    to send a payment to nowhere. Advertise a rail only if it works."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    assert module.x402_payments.is_configured() is False, "test presumes x402 is off in CI"

    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    body = response.json()

    assert response.status_code == 402
    assert "payTo" not in body
    assert "accepted_payment_header" not in body
    assert not any(entry.get("protocol") == "x402" for entry in body["accepts"])


def test_402_advertises_x402_when_it_is_configured(monkeypatch):
    from fastapi.testclient import TestClient

    addr = "0x32b08c5e927c69877d0fcab35618c265674922bc"
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: True)
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    monkeypatch.setattr(module.x402_payments, "_PAY_TO_ADDRESS", addr)

    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    body = response.json()

    # `accepts` is the x402 spec's array, not ours: a client validates every
    # entry in it against PaymentRequirementsV1 and raises on the first that
    # does not fit, before signing anything. So it holds x402 entries only,
    # in the spec's field names.
    assert len(body["accepts"]) == 1
    entry = body["accepts"][0]
    assert entry["payTo"] == addr
    assert entry["scheme"] == "exact"
    assert entry["network"] == "base", "v1 clients register schemes by legacy name"
    assert entry["maxAmountRequired"] == "50000", "$0.05 in USDC atomic units"
    assert entry["asset"].startswith("0x")
    assert entry["maxTimeoutSeconds"] > 0
    assert body["x402Version"] == 1, "without this a client will not read the body"


def test_402_bundle_price_is_carried_everywhere(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    body = client.post("/audit/bundle", json={"url": "https://example.com"}).json()

    assert body["price"] == "$0.15"
    assert body["price_usd"] == 0.15


def test_audit_rejects_wrong_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/audit", json={"url": "https://example.com"}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 402


def test_audit_rejects_unbilled_key_when_billing_unconfigured(monkeypatch):
    # No AUDIT_API_KEY set and Stripe isn't configured -- every key must be
    # rejected rather than falling through to an unauthenticated Firestore
    # lookup or a fabricated pass.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(
        "/audit", json={"url": "https://example.com"}, headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 402


def test_audit_rejects_garbage_x_payment_header(monkeypatch):
    # A malformed/unsigned X-PAYMENT must fail closed exactly like a bad
    # API key -- never fall through to a free audit.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "not-a-real-payment"},
    )
    assert response.status_code == 402


def test_audit_rejects_x_payment_when_x402_unconfigured(monkeypatch):
    # No X402_FACILITATOR_URL / X402_PAY_TO_ADDRESS set in CI -- even a
    # well-formed-looking X-PAYMENT header must be rejected, never trusted
    # on its own.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    assert module.x402_payments.is_configured() is False
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "eyJmYWtlIjogInBheWxvYWQifQ=="},
    )
    assert response.status_code == 402


def test_authenticate_accepts_internal_key_without_x_payment(monkeypatch):
    # The existing Stripe/internal-key path must keep working untouched by
    # the x402/MPP additions -- it doesn't need an X-PAYMENT or Authorization
    # header at all.
    module = _load_main(monkeypatch, api_key="test-key")
    auth = module._authenticate("test-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "internal"


def test_authenticate_x402_success_grants_access(monkeypatch):
    # With a facilitator that confirms verification, a valid X-PAYMENT alone
    # (no API key) is sufficient to gain access. Note the payment is only
    # VERIFIED at this point, not settled -- settlement waits until an audit
    # has actually produced a result (see _bill).
    module = _load_main(monkeypatch, api_key=None)
    pending = object()
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None, **kw: pending)
    auth = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "x402"
    assert auth.pending_payment is pending, "payment must be held unsettled until delivery"


def test_authenticate_x402_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None, **kw: None)
    result = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_mpp_success_grants_access(monkeypatch):
    # A valid Authorization: Payment ... credential alone (no API key, no
    # X-PAYMENT) is sufficient when MPP verification succeeds.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: True)
    auth = module._authenticate(None, None, "Payment some-base64url-credential")
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "mpp"


def test_authenticate_mpp_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: False)
    result = module._authenticate(None, None, "Payment some-base64url-credential")
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_ignores_non_payment_authorization_scheme(monkeypatch):
    # An Authorization header using a different scheme (e.g. Bearer) must
    # never be handed to MPP verification -- only the literal "Payment "
    # prefix triggers it.
    module = _load_main(monkeypatch, api_key=None)
    calls = []
    monkeypatch.setattr(
        module.mpp_payments,
        "verify_and_settle_sync",
        lambda header, realm=None: calls.append(header) or False,
    )
    result = module._authenticate(None, None, "Bearer some-jwt")
    assert isinstance(result, module.JSONResponse)
    assert calls == []


def test_audit_accepts_mpp_authorization_header(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: True)
    monkeypatch.setattr(
        module,
        "_run_axe",
        lambda html, url: {"violations": []},
    )
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"Authorization": "Payment some-base64url-credential"},
    )
    assert response.status_code == 200
    assert response.json()["pass"] is True


def test_402_response_advertises_mpp_challenges_when_configured(monkeypatch):
    # When MPP is configured, the 402 response must carry a
    # WWW-Authenticate: Payment header per spec, not just the x402 JSON body.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(
        module.mpp_payments,
        "www_authenticate_headers",
        lambda realm=None, price_usd=None: ["Payment id=\"x\", method=\"stripe\""],
    )
    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    assert response.status_code == 402
    assert response.headers["cache-control"] == "no-store"
    www_auth_values = response.headers.get_list("www-authenticate")
    assert any('method="stripe"' in v for v in www_auth_values)


def test_billing_endpoints_501_when_unconfigured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/billing/checkout", json={"email": "a@example.com"})
    assert response.status_code == 501


def test_rate_limit_rejects_after_threshold(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=2, window_seconds=60.0)
    assert limiter.check("some-key") is True
    assert limiter.check("some-key") is True
    assert limiter.check("some-key") is False


def test_rate_limit_is_per_key_not_global(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=1, window_seconds=60.0)
    assert limiter.check("key-a") is True
    assert limiter.check("key-a") is False
    # A different paying caller must not be throttled by someone else's usage.
    assert limiter.check("key-b") is True


def test_rate_limit_window_expires(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=1, window_seconds=0.05)
    assert limiter.check("k") is True
    assert limiter.check("k") is False
    time.sleep(0.06)
    assert limiter.check("k") is True


def test_rate_limiter_evicts_idle_keys_and_stays_bounded(monkeypatch):
    """A dict-of-deques keyed by caller IP that never evicts is an OOM at
    volume -- one leaked entry per unique caller, forever."""
    module = _load_main(monkeypatch)
    # A controlled clock, not a real 50ms window: on a slow CI runner the 500
    # checks below took longer than the window and the sweep evicted some of
    # them before the first assertion (386 == 500). The limiter's behaviour
    # is a function of time; the test supplies the time.
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(module.time, "time", lambda: clock["now"])
    limiter = module._SlidingWindowLimiter(
        limit=5, window_seconds=0.05, sweep_interval=0.0
    )
    for i in range(500):
        limiter.check(f"ip-{i}")
    assert len(limiter._log) == 500
    clock["now"] += 0.06
    # One more call triggers a sweep, which must drop the 500 expired windows.
    limiter.check("fresh-key")
    assert len(limiter._log) == 1


def test_rate_limiter_hard_caps_table_under_key_flood(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(
        limit=5, window_seconds=600.0, max_keys=100, sweep_interval=1e9
    )
    for i in range(400):
        limiter.check(f"ip-{i}")
    # Windows are all still live, so eviction-by-expiry can't help; the hard
    # cap is the only thing standing between this and unbounded growth.
    assert len(limiter._log) <= 400
    assert len(limiter._log) < 400 or limiter._max_keys >= 400


def test_rate_limit_is_enforced_before_any_payment_is_settled(monkeypatch):
    """Regression: the limiter used to run AFTER _authenticate, which is
    where x402/MPP payments actually settle. An over-limit caller therefore
    paid, then received a 429 -- money taken, no audit, no refund. Rejection
    must happen before the payment instrument is touched."""
    module = _load_main(monkeypatch)

    settled = []

    def _exploding_verify(payment_header, price=None, resource_url=None):
        settled.append(payment_header)
        return True

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _exploding_verify)
    # Exhaust the limiter for this key before the request comes in.
    monkeypatch.setattr(module, "_audit_limiter", module._SlidingWindowLimiter(limit=0, window_seconds=60.0))

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "some-signed-payment"},
    )

    assert response.status_code == 429
    assert settled == [], "payment was settled despite the caller being rate limited"
    body = response.json()
    assert body["billed"] is False
    assert response.headers["Retry-After"] == "60"


# --- New multi-route audit suite -------------------------------------------

NEW_PAID_ROUTES = [
    ("/audit/wcag", {"url": "https://example.com"}, 0.05),
    ("/audit/seo", {"url": "https://example.com"}, 0.05),
    ("/audit/security", {"url": "https://example.com"}, 0.05),
    ("/audit/performance", {"url": "https://example.com"}, 0.05),
    ("/audit/bundle", {"url": "https://example.com"}, 0.15),
]


@pytest.mark.parametrize("path,body,price", NEW_PAID_ROUTES)
def test_new_routes_are_fail_closed_with_correct_price(monkeypatch, path, body, price):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(path, json=body)
    assert response.status_code == 402
    assert response.json()["price"] == f"${price:.2f}"


@pytest.mark.parametrize("path,body", [(p, b) for p, b, _ in NEW_PAID_ROUTES])
def test_new_routes_accept_internal_key_and_execute(monkeypatch, path, body):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(
        module.audits, "fetch_once", return_value=object()
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value=_perf,
    ):
        client = TestClient(module.app)
        response = client.post(path, json=body, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["pass"] is True


def test_audit_wcag_seo_require_html_or_url(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    client = TestClient(module.app)
    for path in ("/audit/wcag", "/audit/seo"):
        response = client.post(path, json={}, headers={"X-API-Key": "test-key"})
        assert response.status_code == 400


def test_audit_security_url_field_required_by_schema(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    client = TestClient(module.app)
    response = client.post("/audit/security", json={}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 422  # Pydantic validation: url is required


def test_audit_route_execution_failure_returns_502_and_is_not_billed(monkeypatch):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(module.billing, "record_usage", lambda *a, **k: calls.append(1))
    with patch.object(module.audits, "run_seo_audit", side_effect=RuntimeError("boom")):
        client = TestClient(module.app)
        response = client.post(
            "/audit/seo", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 502
    assert response.json()["pass"] is None
    assert calls == []


def test_bundle_failure_is_atomic_and_unbilled(monkeypatch):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(module.billing, "record_usage", lambda *a, **k: calls.append(1))
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(module.audits, "run_security_audit", side_effect=RuntimeError("network error")):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 502
    assert calls == []


def test_bundle_success_meters_the_price_it_charges(monkeypatch):
    # The bundle charges $0.15, so it must meter 15 cents -- not the 3 "units"
    # it used to report, which at $0.01/unit invoiced $0.03 for the whole call.
    # See billing.record_usage.
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    recorded = []
    monkeypatch.setattr(
        module.billing,
        "record_usage",
        lambda customer_id, price_cents: recorded.append(price_cents),
    )
    # Force the internal key path to look Stripe-billable so _bill() actually
    # calls record_usage -- the internal key itself is normally unbilled.
    real_authenticate = module._authenticate

    def _fake_authenticate(*args, **kwargs):
        auth = real_authenticate(*args, **kwargs)
        if isinstance(auth, module.AuthContext):
            auth.stripe_billable = True
            auth.customer_id = "cus_test"
        return auth

    monkeypatch.setattr(module, "_authenticate", _fake_authenticate)

    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(
        module.audits, "fetch_once", return_value=object()
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value=_perf,
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 200
    assert recorded == [15]


def _capture_meter_events(monkeypatch, module):
    """Collect what would have been sent to Stripe's meter."""
    events = []

    class _MeterEvent:
        @staticmethod
        def create(**kwargs):
            events.append(kwargs)

    monkeypatch.setattr(module.billing.stripe.billing, "MeterEvent", _MeterEvent)
    return events


def test_record_usage_meters_the_price_not_the_call(monkeypatch):
    """The metered Price is $0.01 per unit, so a $0.05 call owes 5 units.

    It used to report exactly one unit per call, which invoiced a third of the
    money on every single call -- and nothing said so, because the Price was
    never attached to a subscription. This account's meter aggregates by
    `count`, where the event value is ignored, so 5 units means 5 events.
    """
    module = _load_main(monkeypatch)
    events = _capture_meter_events(monkeypatch, module)

    module.billing.record_usage("cus_test", price_cents=3)
    assert len(events) == 3

    events.clear()
    module.billing.record_usage("cus_test", price_cents=10)
    assert len(events) == 10

    assert {event["payload"]["stripe_customer_id"] for event in events} == {"cus_test"}
    # Distinct identifiers, or Stripe dedupes the ten events of a bundle down
    # to one and the 90% undercharge comes straight back.
    assert len({event["identifier"] for event in events}) == 10


def test_a_sum_meter_bills_the_same_money_in_one_call(monkeypatch):
    """The same 10 cents, reported the way a `sum` meter reads it. A meter's
    formula cannot be edited after creation, so this is the shape a NEW meter
    would take -- and it must bill identically, or moving to it silently
    changes every invoice."""
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "_METER_AGGREGATION", "sum")
    events = _capture_meter_events(monkeypatch, module)

    module.billing.record_usage("cus_test", price_cents=10)

    assert [event["payload"]["value"] for event in events] == ["10"]


def test_record_usage_refuses_an_unknown_meter_aggregation(monkeypatch):
    """A typo here is a uniform mis-bill, not an error anyone would notice."""
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "_METER_AGGREGATION", "average")
    events = _capture_meter_events(monkeypatch, module)

    with pytest.raises(ValueError):
        module.billing.record_usage("cus_test", price_cents=3)
    assert events == []


def test_record_usage_follows_the_price_when_the_meter_unit_changes(monkeypatch):
    """The unit is a fact about the Stripe Price, not a constant. If the Price
    goes to $0.05/unit, a $0.15 bundle is 3 units."""
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "_METER_UNIT_CENTS", 5)
    monkeypatch.setattr(module.billing, "_METER_AGGREGATION", "sum")
    events = _capture_meter_events(monkeypatch, module)

    module.billing.record_usage("cus_test", price_cents=10)

    assert events[0]["payload"]["value"] == "2"


def test_record_usage_refuses_a_price_that_does_not_reconcile(monkeypatch):
    """Rounding here would mis-bill by a few percent on every single call and
    never show up anywhere. Refusing is loud and costs one audit's revenue."""
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "_METER_UNIT_CENTS", 5)
    events = _capture_meter_events(monkeypatch, module)

    with pytest.raises(ValueError):
        module.billing.record_usage("cus_test", price_cents=3)
    assert events == []


def test_a_failed_meter_call_warns_but_never_withholds_the_audit(monkeypatch):
    """The customer already has the result in hand. A billing fault is a
    warning on the response, not a 500."""
    module = _load_main(monkeypatch)
    auth = module.AuthContext(stripe_billable=True, customer_id="cus_test")

    def _explode(*args, **kwargs):
        raise ValueError("meter rejected it")

    monkeypatch.setattr(module.billing, "record_usage", _explode)
    assert "meter rejected it" in module._bill(auth, price_usd=0.05)


def test_each_route_meters_its_own_price(monkeypatch):
    """A single audit is 3 cents and the bundle is 10. _bill takes the price
    rather than a unit count precisely so these cannot drift apart."""
    module = _load_main(monkeypatch)
    auth = module.AuthContext(stripe_billable=True, customer_id="cus_test")
    metered = []
    monkeypatch.setattr(
        module.billing,
        "record_usage",
        lambda customer_id, price_cents: metered.append(price_cents),
    )

    assert module._bill(auth, price_usd=0.05) is None
    assert module._bill(auth, price_usd=0.15) is None
    assert metered == [5, 15]


def _openapi_with_tempo(monkeypatch):
    """The served OpenAPI doc, with the tempo rail live.

    Patched on the shared mpp_payments module (like the manifest tests above)
    rather than via env vars: its config constants were baked at ITS import,
    which predates this test."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: True)
    monkeypatch.setattr(
        module.mpp_payments,
        "_TEMPO_RECIPIENT_ADDRESS",
        "0x32b08c5e927c69877d0fcab35618c265674922bc",
    )
    return module, TestClient(module.app).get("/openapi.json").json()


def test_openapi_marks_every_paid_route_with_x_payment_info(monkeypatch):
    """MPP's reference tooling discovers paid endpoints from openapi.json:
    an operation is payable iff it carries x-payment-info. Without the
    extension `mppx validate` reported `endpoints: []` and skipped its whole
    challenge suite -- this node's own directory said "no tolls here"."""
    module, doc = _openapi_with_tempo(monkeypatch)

    paid_paths = [e["path"] for e in module._CATALOG] + list(module._CATALOG_ALIASES)
    for path in paid_paths:
        operation = doc["paths"][path]["post"]
        info = operation.get("x-payment-info")
        assert info and info["offers"], f"{path} is not discoverable as payable"
        assert all(o["amount"].isdigit() for o in info["offers"])
        # The discovery spec: an operation with x-payment-info MUST declare
        # a 402 response. mppx validate fails the whole document otherwise.
        assert "402" in operation["responses"], f"{path} declares no 402"
        # The validator derives its 402 probe body from the example; against
        # the html-or-url anyOf its schema-generated guess fails validation
        # and the route reads as broken (422 forever) when it is merely
        # under-documented.
        example = operation["requestBody"]["content"]["application/json"]["example"]
        assert "url" in example, f"{path} has no probe-able body example"

    # The bundle's offer must carry the bundle's price, not the flat rate.
    bundle = doc["paths"]["/audit/bundle"]["post"]["x-payment-info"]["offers"]
    assert bundle[0]["amount"] == "150000"  # $0.15 in USDC base units

    assert "docs" in doc["x-service-info"]


def test_openapi_carries_no_x_payment_info_when_no_mpp_rail_exists(monkeypatch):
    """Fail closed on the discovery surface too: a node that cannot settle
    an MPP payment must not tell MPP tooling that its routes are payable."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)  # no Stripe, no tempo
    doc = TestClient(module.app).get("/openapi.json").json()
    for path_item in doc["paths"].values():
        for operation in path_item.values():
            assert "x-payment-info" not in operation


def test_openapi_annotation_follows_a_rail_change(monkeypatch):
    """The base document is cached by FastAPI; the annotation must not be.
    A frozen copy would keep advertising a rail past the config turning it
    off -- the same staleness bug as every other cached surface here."""
    from fastapi.testclient import TestClient

    module, doc = _openapi_with_tempo(monkeypatch)
    assert "x-payment-info" in doc["paths"]["/audit/wcag"]["post"]

    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    doc = TestClient(module.app).get("/openapi.json").json()
    assert "x-payment-info" not in doc["paths"]["/audit/wcag"]["post"]


def test_agent_manifest_lists_all_five_audit_routes(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/.well-known/agent.json")
    paths = {e["path"] for e in response.json()["endpoints"]}
    for expected in ("/audit/wcag", "/audit/seo", "/audit/security", "/audit/performance", "/audit/bundle"):
        assert expected in paths
    bundle = next(e for e in response.json()["endpoints"] if e["path"] == "/audit/bundle")
    assert bundle["price_usd"] == 0.15


# --- key store outage ---------------------------------------------------


def test_authenticate_returns_402_not_500_when_the_key_store_is_unreachable(monkeypatch):
    """The exact production failure this exists to prevent.

    No Firestore database had ever been created for the project, so
    lookup_key raised NotFound on every keyed call. Unhandled, that surfaced
    as HTTP 500: a machine caller could not pay, could not usefully retry,
    and could not tell a broken backend from a rejected key. The whole paid
    path was dead and nothing caught it, because verify-live.sh only ever
    exercised the UNauthenticated 402 -- it now has a paid-path check too,
    guarded by tests/test_verify_live.py.

    Falling through to the payment challenge is still fail-closed -- nobody
    gets in without paying -- it just answers with something actionable.
    """
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)

    def _boom(key):
        raise RuntimeError("404 The database (default) does not exist for project")

    monkeypatch.setattr(module.billing, "lookup_key", _boom)

    with pytest.raises(RuntimeError):
        # Guard the guard: if lookup_key itself stops raising, this test would
        # pass for the wrong reason.
        module.billing.lookup_key("k")

    monkeypatch.setattr(module.billing, "lookup_key", lambda key: None)
    result = module._authenticate("some-key", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_lookup_key_swallows_a_dead_key_store_and_returns_none(monkeypatch):
    """billing.lookup_key is the boundary: an unreachable store must read as
    'no usable key', never as an exception escaping into the request."""
    billing = _load_main(monkeypatch, api_key=None).billing

    def _dead():
        raise RuntimeError("404 The database (default) does not exist for project")

    monkeypatch.setattr(billing, "_firestore", _dead)
    billing._key_store_warned = False
    assert billing.lookup_key("anything") is None


def test_a_dead_key_store_is_logged_at_error_not_swallowed_silently(monkeypatch, caplog):
    """A subscriber being silently downgraded to pay-per-call is exactly the
    kind of outage that otherwise looks identical to 'nobody is buying'."""
    billing = _load_main(monkeypatch, api_key=None).billing

    def _dead():
        raise RuntimeError("The database (default) does not exist")

    monkeypatch.setattr(billing, "_firestore", _dead)
    billing._key_store_warned = False
    with caplog.at_level("ERROR"):
        billing.lookup_key("anything")
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert "Firestore" in caplog.text


# --- SaaS monthly quota ------------------------------------------------


def test_authenticate_falls_back_to_402_when_quota_exceeded(monkeypatch):
    # A valid Stripe key that's over its monthly quota must not be treated
    # as sufficient on its own -- this is what makes "overage falls back to
    # x402/MPP rates" actually true rather than just landing-page copy.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id, plan=None: False)

    result = module._authenticate("real-stripe-key", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_succeeds_when_quota_available(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id, plan=None: True)

    auth = module._authenticate("real-stripe-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is True
    assert auth.customer_id == "cus_123"


def test_auth_path_hands_the_plan_to_the_quota_check(monkeypatch):
    """The per-plan cap only works if the plan reaches the quota check.

    The cap is stored on the key document precisely so this lookup carries
    it. Drop the kwarg here and every Agency subscriber silently reverts to
    the 1,500 fallback -- the exact cut-off the per-plan caps exist to fix,
    and invisible because the call still succeeds.
    """
    module = _load_main(monkeypatch, api_key=None)
    seen = {}
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(
        module.billing,
        "lookup_key",
        lambda key: {"customer_id": "cus_agency", "plan": "agency"},
    )

    def _quota(customer_id, plan=None):
        seen["customer_id"] = customer_id
        seen["plan"] = plan
        return True

    monkeypatch.setattr(module.billing, "check_and_increment_quota", _quota)

    auth = module._authenticate("agency-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert seen == {"customer_id": "cus_agency", "plan": "agency"}


def test_internal_key_bypasses_quota_check(monkeypatch):
    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(
        module.billing, "check_and_increment_quota", lambda customer_id, plan=None: calls.append(1) or False
    )
    auth = module._authenticate("test-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.payment_method == "internal"
    assert calls == []


# --- Agent-facing discovery manifest ---------------------------------------


def test_manifest_prices_match_what_routes_actually_charge(monkeypatch):
    """The manifest is what an agent budgets against. If it advertises a
    price the route doesn't actually challenge for, the agent builds a
    payment for the wrong amount and the call fails at settlement."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    advertised = {
        e["path"]: e["price_usd"]
        for e in manifest["endpoints"]
        if e.get("payment_required")
    }
    assert advertised, "manifest advertised no paid endpoints"

    for path, price in advertised.items():
        body = {"url": "https://example.com"}
        challenged = client.post(path, json=body).json()
        assert challenged["price_usd"] == price, f"{path} advertises {price}, charges {challenged['price_usd']}"


def test_manifest_only_lists_payment_methods_that_can_settle(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    # Nothing is configured in CI, so an honest manifest lists nothing.
    assert manifest["payment"]["methods"] == []

    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: True)
    refreshed = client.get("/.well-known/agent.json").json()
    assert "mpp-tempo" in refreshed["payment"]["methods"]


_SIBLING_MODULES = ("billing", "x402_payments", "mpp_payments", "audits")


def _drop_sibling_cache():
    import sys

    for name in _SIBLING_MODULES:
        sys.modules.pop(f"wcag_audit_engine_{name}", None)


@pytest.fixture
def load_main_fresh():
    """Load main.py with its sibling modules re-read from the environment.

    main.py caches siblings in sys.modules under `wcag_audit_engine_<name>`
    so audits.py and main.py share one browser_pool. That cache also means
    billing.py's module-level os.environ.get calls run exactly once per
    process -- so a test that sets Stripe env vars and reloads main would
    otherwise still get the first billing module the process ever loaded,
    and would pass while testing nothing.

    Clears the cache on the way in and on the way out, so neither this test
    nor the next one inherits the other's Stripe configuration.
    """
    import importlib.util

    def _load(unique):
        _drop_sibling_cache()
        spec = importlib.util.spec_from_file_location(unique, MAIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Same reason as in _load_main: the 402 shape tests are not about the
        # facilitator, which does not exist here; fail-closed would hide x402.
        module.x402_payments._facilitator_supports = lambda version, network: True
        return module

    yield _load
    _drop_sibling_cache()


def test_planless_checkout_fails_cleanly_rather_than_500ing(monkeypatch, load_main_fresh):
    """With the retired defaults gone, a plan-less checkout used to build a
    Stripe call with price=None and blow up as an opaque 500. It must say
    what to pass instead."""
    for var in ("STRIPE_METERED_PRICE_ID", "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")

    module = load_main_fresh("wcag_audit_main_noplan")

    with pytest.raises(ValueError) as excinfo:
        module.billing.create_checkout_session("b@example.com", "https://x/s", "https://x/c")
    assert "pro" in str(excinfo.value), "the error should name a plan the caller can actually pick"

    # And the route turns that into a 400, not a 500.
    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    response = client.post("/billing/checkout", json={"email": "b@example.com"})
    assert response.status_code == 400, f"got {response.status_code}: {response.text}"


def test_each_plans_quota_covers_what_that_plan_promises(monkeypatch, load_main_fresh):
    """The included-scans cap has to fit the plan's own advertised coverage.

    One global 1,500 cap applied to every subscriber: Agency is sold as "50
    sites, audited daily", which is 1,550 bundle calls in a 31-day month --
    so the $249 customer got cut off before month end, and cut off around
    day 7 if they audited per-dimension instead of bundling. Pro and Agency
    also shared a ceiling, so paying 3x bought no extra capacity.
    """
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    module = load_main_fresh("wcag_audit_main_quota")
    billing = module.billing

    sites = {"pro": 5, "agency": 50}
    for plan, site_count in sites.items():
        quota = billing.monthly_quota_for(plan)
        # Worst case a customer can legitimately drive: every site, every
        # dimension, every day of the longest month.
        worst_case = site_count * 4 * 31
        assert quota >= worst_case, (
            f"{plan} sells {site_count} sites audited daily "
            f"({worst_case} calls in a 31-day month) but caps at {quota}"
        )

    assert billing.monthly_quota_for("agency") > billing.monthly_quota_for("pro"), (
        "Agency costs 3x Pro and must not buy the same ceiling"
    )
    # An unrecognised or legacy key keeps the old behaviour rather than
    # silently getting unlimited access.
    assert billing.monthly_quota_for(None) == billing.SAAS_MONTHLY_QUOTA
    assert billing.monthly_quota_for("nonsense") == billing.SAAS_MONTHLY_QUOTA


def test_purchased_plan_is_recorded_so_the_quota_can_see_it(monkeypatch, load_main_fresh):
    """The cap is per-plan, which only works if activation stores the plan."""
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    module = load_main_fresh("wcag_audit_main_activate")
    billing = module.billing

    written = {}

    class _Doc:
        def __init__(self, key):
            self.key = key

        def get(self):
            return type("S", (), {"exists": False})()

        def set(self, data):
            written[self.key] = data

    class _Collection:
        def __init__(self, name):
            self.name = name

        def document(self, doc_id):
            return _Doc(f"{self.name}/{doc_id}")

    monkeypatch.setattr(
        billing, "_firestore", lambda: type("DB", (), {"collection": staticmethod(_Collection)})()
    )

    billing.activate_customer({"customer": "cus_123", "metadata": {"plan": "agency"}})

    key_doc = next(v for k, v in written.items() if k.startswith("api_keys/"))
    assert key_doc["plan"] == "agency", "the key must carry the plan the quota is sized from"
    assert key_doc["customer_id"] == "cus_123"

    # A checkout with no plan metadata (legacy) must still activate.
    written.clear()
    billing.activate_customer({"customer": "cus_456"})
    legacy = next(v for k, v in written.items() if k.startswith("api_keys/"))
    assert legacy["plan"] is None


def test_mcp_handshake_names_only_rails_that_can_settle(monkeypatch):
    """Every MCP client reads `instructions` on connect.

    It used to say "need an X-API-Key header, or an x402/MPP payment" no
    matter what was configured. Unlike the OpenAPI schema -- a module constant
    baked in at import -- this is a request handler, so it can name exactly
    what this deployment settles rather than hedging.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: False)
    # stripe_available_for, not stripe_configured: the manifest lists the SPT
    # rail only where the price clears Stripe's minimum card charge, and every
    # route in the catalog is priced below it.
    monkeypatch.setattr(module.mpp_payments, "stripe_available_for", lambda cents: True)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    response = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}},
    )
    instructions = response.json()["result"]["instructions"]
    assert "mpp-stripe" in instructions
    assert "x402" not in instructions


def test_mcp_handshake_says_so_when_no_rail_is_configured(monkeypatch):
    """Listing nothing would read as "free"; it has to say what it means."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "stripe_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    response = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}},
    )
    assert "no rail is configured" in response.json()["result"]["instructions"]


def test_endpoint_auth_prose_does_not_contradict_its_own_method_list(monkeypatch):
    """agent.json carried both, in the same object, disagreeing.

    Each endpoint entry has a live-derived `payment_methods` list AND a static
    `auth` prose blurb. The blurb asserted x402 unconditionally, so with x402
    off an endpoint said `payment_methods: ["mpp-stripe"]` while the prose
    beside it told the reader to send an X-PAYMENT header. The prose cannot be
    per-request (it is a module constant), so it must point at the live field
    rather than name rails as available.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: False)
    # stripe_available_for, not stripe_configured: the manifest lists the SPT
    # rail only where the price clears Stripe's minimum card charge, and every
    # route in the catalog is priced below it.
    monkeypatch.setattr(module.mpp_payments, "stripe_available_for", lambda cents: True)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    body = TestClient(module.app).get("/.well-known/agent.json").json()
    endpoint = body["endpoints"][0]
    assert endpoint["payment_methods"] == ["mpp-stripe"]
    assert "payment.methods" in endpoint["auth"]
    assert "agent.json" in endpoint["auth"]
    # The phrasing that read as a promise rather than a menu.
    assert "One of: X-API-Key header" not in endpoint["auth"]


def test_openapi_description_does_not_assert_a_rail(monkeypatch):
    """The schema's own blurb named x402 and MPP as the challenge format."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    description = TestClient(module.app).get("/openapi.json").json()["info"]["description"]
    assert "x402 JSON body and/or MPP" not in description
    assert "agent.json" in description


def test_no_shipped_surface_asserts_a_payment_rail_as_available():
    """A rail named as a fact on a static surface goes stale silently.

    /mcp.json asserted `["stripe_api_key", "x402", "mpp"]` in a file and kept
    asserting x402 after x402 was switched off. The landing page said the same
    thing in its meta description, its JSON-LD (which search and AI crawlers
    read), and a spec table row reading "Payment rails: x402 - MPP".

    None of those can know what a deployment has configured, so none of them
    may claim it. The rule this codebase already follows everywhere else is to
    point at the live source instead: /.well-known/agent.json and the 402 body
    both list only rails that can genuinely settle.

    llms.txt deliberately still names all three, because it explains what the
    protocols ARE and then says in as many words that which are live is
    deployment-specific and must be read from agent.json. Describing a menu is
    fine; asserting availability is not.
    """
    surfaces = [
        REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "index.html",
        REPO_ROOT / "server.json",
        REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json",
    ]
    offenders = []
    for path in surfaces:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for term in ("x402", "mpp-stripe", "mpp-tempo"):
            if term in text.lower():
                offenders.append(f"{path.relative_to(REPO_ROOT)} names {term!r}")

    assert not offenders, (
        "these surfaces cannot know which rails a deployment can settle, so "
        "they must not name one -- link /.well-known/agent.json instead:\n  "
        + "\n  ".join(offenders)
    )


def test_no_shipped_surface_still_quotes_the_retired_plan():
    """Retired pricing must not survive anywhere a buyer or an agent reads.

    The $49/1,500 plan was removed from the manifest but lived on in two
    READMEs for weeks, because the fix grepped one file. Anything quoting a
    price that no Stripe Price ID backs is a broken sale, so check the whole
    shipped surface rather than the file that happened to prompt the fix.
    """
    import re

    retired = re.compile(
        r"\$49\b|1,?500 scans|1,?500 included|included_calls_per_month"
        r"|\$29\.99|\$79\b|\$249\b|\"human_plans\"|plan-btn"
        # Word order is not the point, the OFFER is. The sweep looked for
        # "per site watched" while the landing page's meta description --
        # the one string every crawler and link preview shows -- said
        # "watched per site on a plan" and sailed through for a day.
        r"|per site watched|watched per site"
    )

    surfaces = []
    for pattern in ("*.md", "*.html", "*.txt", "*.json", "*.yml", "*.py"):
        for path in REPO_ROOT.rglob(pattern):
            parts = path.parts
            if any(p in parts for p in (".git", "node_modules", "venv", "venv_clean")):
                continue
            if path.name == "HANDOFF.md":  # history is allowed to remember prices
                continue
            if path.name == Path(__file__).name:  # this test names them on purpose
                continue
            surfaces.append(path)

    assert surfaces, "found no files to check -- the glob is wrong"

    offenders = []
    for path in surfaces:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if retired.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:90]}")

    assert not offenders, "retired pricing still shipped:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "bad_key",
    [
        "0x32b08c5e927c69877d0fcab35618c265674922b",  # a payout address
        "price_1U34LiDA21T9EAQB3LK5dS0I",  # a Price ID
        "pk_live_abc123",  # the PUBLISHABLE key, not the secret
        "whsec_abc123",  # the webhook secret
        "changeme",
    ],
)
def test_a_key_that_is_not_a_stripe_key_sells_nothing(bad_key, monkeypatch, load_main_fresh):
    """A non-empty STRIPE_SECRET_KEY is not the same as a usable one.

    Any non-empty string used to satisfy is_configured(), so the wrong value
    in the right variable would have had the manifest advertise all three
    plans plus the stripe_api_key rail while every Stripe call failed
    authentication. A rail that cannot settle must never be advertised, so a
    key that cannot possibly work has to count as no key.
    """
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_AGENCY", "price_agency")
    monkeypatch.setenv("STRIPE_PRICE_ONEOFF_REPORT", "price_report")
    monkeypatch.setenv("STRIPE_SECRET_KEY", bad_key)

    module = load_main_fresh("wcag_audit_main_badkey")
    assert module.billing.stripe_key_looks_valid() is False
    assert module.billing.is_configured() is False
    assert module.billing.human_plans_live() == []

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()
    assert "human_plans" not in manifest["pricing"], (
        "advertised plans nobody can buy with a broken Stripe key"
    )
    assert "stripe_api_key" not in manifest["payment"]["methods"], (
        "advertised a Stripe rail that cannot authenticate"
    )


@pytest.mark.parametrize(
    "good_key",
    [
        "sk_live_abc123",
        "sk_test_abc123",
        "rk_live_abc123",
        "rk_test_abc123",
        # A secret piped in with `echo` keeps a trailing newline. The key is
        # correct; the padding is not. Stripe rejects it and, unstripped, the
        # prefix check would too -- quietly pulling every plan off a service
        # whose credentials are actually fine.
        "  sk_live_abc123\n",
        "sk_live_abc123\n",
    ],
)
def test_a_real_stripe_key_sells_normally(good_key, monkeypatch, load_main_fresh):
    """The shape check must not reject keys Stripe actually issues, however
    they arrived."""
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_SECRET_KEY", good_key)

    module = load_main_fresh("wcag_audit_main_goodkey")
    assert module.billing.stripe_key_looks_valid() is True, f"rejected {good_key!r}"
    assert module.billing.is_configured() is True
    # The key is fine; the plan is retired (2026-09-06), so it still sells nothing.
    assert module.billing.plan_available("pro") is False
    assert module.billing.human_plans_live() == []
    # The padding must be gone from what we hand to Stripe, not merely
    # tolerated by the check.
    assert module.billing.stripe.api_key == good_key.strip()


# 0x + exactly 40 hex. Synthetic, but it must be SHAPE-VALID: every test
# below asserts what a live x402 deployment does, and a fixture the deploy
# gate would reject describes a deployment that cannot exist.
#
# This used to be "0x...4922b" -- 39 hex -- which is the literal value
# tests/test_preflight.py names SHORT_ADDR and exists to prove the deploy
# preflight REJECTS. So the whole "x402 is live" suite ran under an address
# that could never ship, which is the same failure that reached production:
# a pay-to address of the wrong length, advertised as a live rail, settling
# nothing. test_the_x402_fixture_would_survive_the_deploy_gate keeps the
# fixture and the gate in agreement.
_X402_TEST_PAY_TO = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _x402_env(monkeypatch):
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", _X402_TEST_PAY_TO)
    monkeypatch.delenv("AUDIT_API_KEY", raising=False)


def test_the_x402_fixture_would_survive_the_deploy_gate():
    """The fixture must pass the SAME check repair-and-deploy.sh enforces.

    Reads the regex out of the deploy script rather than restating it, so
    the two cannot drift apart -- a restated copy is how the fixture and the
    gate disagreed silently in the first place.
    """
    script = (REPO_ROOT / "scripts" / "repair-and-deploy.sh").read_text()
    match = re.search(r"grep -qiE '(\^0x\[0-9a-f\]\{40\}\$)'", script)
    assert match, (
        "repair-and-deploy.sh no longer preflights the pay-to address with "
        "the expected regex; this guard is now checking nothing"
    )
    assert re.match(match.group(1), _X402_TEST_PAY_TO, re.IGNORECASE), (
        f"{_X402_TEST_PAY_TO} has {len(_X402_TEST_PAY_TO) - 2} hex chars; the "
        "deploy gate requires exactly 40, so every test asserting 'x402 is "
        "live' is describing a deployment that would never have shipped"
    )


def test_requirements_pull_the_extras_bazaar_discovery_needs():
    """Static check, because the runtime one cannot be trusted here.

    x402.extensions.bazaar imports jsonschema and idna, which arrive via the
    `extensions` extra -- not via `evm`. A developer machine almost always
    has both transitively, so the feature appears to work locally while
    being dead in the deployed container, and it fails closed and silent so
    nothing announces it. Asserting on the requirements text is the only
    check that a well-stocked environment cannot mask.
    """
    for path in (REPO_ROOT / "requirements.txt",
                 REPO_ROOT / "wcag-audit-engine" / "requirements.txt"):
        text = path.read_text()
        x402_lines = [ln for ln in text.splitlines() if ln.strip().startswith("x402")]
        assert x402_lines, f"{path.name} does not pin x402 at all"
        assert any("extensions" in ln for ln in x402_lines), (
            f"{path.name} pins {x402_lines} -- without the `extensions` extra, "
            "Bazaar discovery silently returns {} in the deployed container"
        )
        # The starlette that `mcp` drags in is incompatible with the pinned
        # FastAPI; it broke 49 tests once already. Never via an x402 extra.
        assert not any("[all]" in ln or ",mcp" in ln or "[mcp" in ln for ln in x402_lines), (
            f"{path.name} pulls the x402 mcp/all extra, which installs the `mcp` "
            "package and a starlette that conflicts with the pinned FastAPI"
        )


def test_402_carries_bazaar_discovery_when_x402_is_live(monkeypatch, load_main_fresh):
    """The Bazaar is how agents find a paid endpoint by capability.

    Facilitators catalog x402 resources by reading this extension off their
    402s. Without it the endpoint is reachable only by someone who already
    knows the URL, which defeats the point of being a machine-payable
    service -- being payable is worthless if nothing can find you.
    """
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar")

    from fastapi.testclient import TestClient

    body = TestClient(module.app).post(
        "/audit/bundle", json={"url": "https://example.com"}
    ).json()

    info = body["extensions"]["bazaar"]["info"]
    assert info["input"]["type"] == "http"
    assert info["input"]["bodyType"] == "json"
    # The advertised input must match what the route actually accepts, or the
    # index sends agents into a 400.
    assert "url" in info["input"]["body"]
    assert body["extensions"]["bazaar"]["schema"], "discovery data must carry its schema"


def test_every_paid_route_carries_bazaar_discovery(monkeypatch, load_main_fresh):
    """Not just the bundle -- EVERY audit route the app actually serves.

    The check above proves /audit/bundle is indexable. That is one route out
    of six, and the discovery data is looked up per-path: a paid route
    missing from _CATALOG returns {} from _bazaar_extension_for_path and
    emits a 402 with no `extensions` at all. It still takes money, so
    nothing looks broken -- it is simply never indexed by a facilitator, and
    nothing else notices.

    Invisible-but-payable is the worst failure mode this service has: from
    the inside it is indistinguishable from nobody wanting to buy.

    The route list comes from app.routes, NOT from _CATALOG. Deriving it
    from the catalog makes the test circular -- dropping a route from the
    catalog would silently drop it from the check too, so the one bug this
    exists to catch would sail through green. (Verified by mutation: the
    catalog-driven version passed with /audit/seo deleted.)
    """
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar_all")

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    served = sorted({
        route.path
        for route in module.app.routes
        if "POST" in getattr(route, "methods", set()) and route.path.startswith("/audit")
    })
    assert served, "no audit routes found -- this guard is checking nothing"

    # Back-compat aliases are deliberately NOT indexed: an alias and its
    # canonical route are the same capability, and listing both would offer
    # an agent the identical audit twice under two URLs. The exclusion is
    # keyed off the manifest's own alias note rather than a hardcoded list,
    # so a genuinely new route can never be waved through by adding its name
    # here -- it has no alias note, so it must carry discovery data.
    manifest = client.get("/.well-known/agent.json").json()
    aliases = {
        e["path"]
        for e in manifest["endpoints"]
        if str(e.get("note", "")).lower().startswith("alias of")
    }

    missing = []
    for path in served:
        if path in aliases:
            continue
        body = client.post(path, json={"url": "https://example.com"}).json()
        if "bazaar" not in body.get("extensions", {}):
            missing.append(path)

    assert not missing, (
        "these paid routes emit a 402 with no Bazaar discovery data, so a "
        "facilitator can never index them and no agent can find them by "
        "capability (each needs a _CATALOG entry):\n  " + "\n  ".join(missing)
    )


def test_no_bazaar_discovery_when_x402_cannot_settle(monkeypatch):
    """Same rule as every other x402 surface: the index is reached through a
    facilitator, so with none configured there is nothing to be indexed by,
    and listing an unpayable resource advertises a sale we cannot complete."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    assert module.x402_payments.is_configured() is False

    body = TestClient(module.app).post(
        "/audit/bundle", json={"url": "https://example.com"}
    ).json()
    assert "extensions" not in body


def test_mcp_paywall_declares_itself_as_an_mcp_resource(monkeypatch, load_main_fresh):
    """An agent that finds the tool in the Bazaar calls it over MCP, so the
    discovery record has to name the tool and its transport -- not describe
    the HTTP route the shared 402 builder would have described."""
    import json

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar_mcp")

    from fastapi.testclient import TestClient

    response = TestClient(module.app).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "audit_bundle", "arguments": {"url": "https://example.com"}},
        },
    )
    challenge = json.loads(response.json()["result"]["content"][0]["text"])

    info = challenge["extensions"]["bazaar"]["info"]["input"]
    assert info["type"] == "mcp", "an MCP tool must not be indexed as an HTTP route"
    assert info["toolName"] == "audit_bundle"
    assert info["transport"] == "streamable-http"
    assert info["inputSchema"] == next(
        t["inputSchema"] for t in module._mcp_tools() if t["name"] == "audit_bundle"
    ), "the indexed schema must be the one the tool actually advertises"


def test_bazaar_failure_never_blocks_a_payment_challenge(monkeypatch, load_main_fresh):
    """Discovery is an enhancement. If building it throws, the caller must
    still get a 402 it can pay -- losing the index is survivable, losing the
    sale is not."""
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar_boom")

    def _boom(*args, **kwargs):
        raise RuntimeError("bazaar library exploded")

    monkeypatch.setattr(
        module.x402_payments, "declare_discovery_extension", _boom, raising=False
    )
    import x402.extensions.bazaar as bz

    monkeypatch.setattr(bz, "declare_discovery_extension", _boom)

    from fastapi.testclient import TestClient

    response = TestClient(module.app).post("/audit/bundle", json={"url": "https://example.com"})
    assert response.status_code == 402
    body = response.json()
    assert body["price_usd"] == 0.15
    assert body["accepts"], "the challenge lost its payable rail"
    assert body["accepts"][0]["scheme"] == "exact"
    assert "extensions" not in body


def test_manifest_points_at_reachable_discovery_documents(monkeypatch):
    """Every discovery URL the manifest advertises must actually resolve --
    a 404 here is an agent's dead end."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    for name, url in manifest["discovery"].items():
        path = url.replace(manifest["base_url"], "")
        if name == "mcp_endpoint":
            # JSON-RPC: POST-only by protocol, so GET is correctly a 405.
            r = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert r.status_code == 200 and "tools" in r.json()["result"], (
                f"{name} -> {path} does not speak MCP"
            )
            continue
        assert client.get(path).status_code == 200, f"{name} -> {path} is not reachable"


def test_cors_exposes_www_authenticate_so_browser_agents_can_pay(monkeypatch):
    """Without WWW-Authenticate in expose_headers a browser-resident agent
    cannot read the MPP challenge off a 402, so it has no way to pay."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"Origin": "https://some-agent.example"},
    )
    assert response.status_code == 402
    exposed = response.headers.get("access-control-expose-headers", "")
    assert "WWW-Authenticate" in exposed


def test_favicon_and_og_image_are_served(monkeypatch):
    """og:image referenced in the page head must actually resolve, or link
    previews render blank and the shared link loses its click-through."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    favicon = client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]

    og = client.get("/og-image.png")
    assert og.status_code == 200
    assert "image/png" in og.headers["content-type"]


def test_page_head_social_tags_point_at_served_assets(monkeypatch):
    import re

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    html = client.get("/").text

    referenced = set(re.findall(r'(?:href|content)="https://[^"]+?(/[^"/]+\.(?:svg|png))"', html))
    referenced |= set(re.findall(r'href="(/[^"]+\.svg)"', html))
    assert referenced, "page head references no image assets at all"
    for path in referenced:
        assert client.get(path).status_code == 200, f"{path} is referenced but 404s"


# --- Machine payers are charged only for audits that actually ran ----------


_SETTLED_TX = "0x" + "cd" * 32


def _x402_caller(monkeypatch, module, *, settle_ok=True):
    """Make an X-PAYMENT header authenticate, tracking verify/settle calls.

    The pending payment is a real PendingPayment, and a successful settle
    leaves a real SettleResponse on it -- exactly what x402_payments does --
    so the route's receipt header is built by the real code, not faked.
    """
    calls = {"verified": 0, "settled": 0}
    sentinel = module.x402_payments.PendingPayment(None, None, "$0.05")

    def _verify(header, price=None, **kw):
        calls["verified"] += 1
        return sentinel

    def _settle(pending):
        calls["settled"] += 1
        assert pending is sentinel
        if settle_ok:
            from x402.schemas import SettleResponse

            pending.settle_result = SettleResponse(
                success=True, transaction=_SETTLED_TX, network="eip155:8453",
                payer="0x" + "11" * 20,
            )
        return settle_ok

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)
    return calls


def test_failed_audit_does_not_settle_an_x402_payment(monkeypatch):
    """Regression: settlement used to happen during authentication, so a
    caller whose audit then failed to run had paid for nothing. Audits fail
    routinely at volume -- the site being audited goes down or times out."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)

    def _boom(*a, **k):
        raise RuntimeError("target site unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 502
    assert response.json()["pass"] is None
    assert calls["verified"] == 1, "payment should still be verified to gate access"
    assert calls["settled"] == 0, "a failed audit must never be charged for"


def test_successful_audit_settles_the_x402_payment(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 200
    assert response.json()["pass"] is True
    assert calls["settled"] == 1, "a delivered audit must actually be charged for"
    assert "billing_warning" not in response.json()


def test_a_refused_settlement_withholds_the_audit_and_charges_nothing(monkeypatch, load_main_fresh):
    """A settle the facilitator refuses used to be answered with the audit
    anyway plus a "not charged" warning -- the work given away on every
    refusal (2026-09-11: a facilitator whose settlement signer was out of
    gas delivered every call free). The reference x402 server discards the
    handler's response on a failed settle and re-issues the 402; so do we.
    """
    from fastapi.testclient import TestClient

    # The rail must be configured for the re-challenge to carry accepts[], and
    # x402_payments reads its configuration once per process, so the module
    # is loaded fresh under this environment rather than reused.
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_refused_settle_rest")
    calls = _x402_caller(monkeypatch, module, settle_ok=False)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = TestClient(module.app).post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert calls["settled"] == 1, "settle must still be attempted once"
    assert response.status_code == 402, "a refused settle delivered the audit anyway"
    body = response.json()
    assert body["error"] == "settlement_refused"
    assert body["billed"] is False
    assert "Nothing was charged" in body["error_detail"]
    assert "fresh authorization" in body["error_detail"]
    assert "violations" not in body and "pass" not in body, "the audit leaked on a refusal"
    assert body["accepts"], "the 402 must still be payable, so the caller can retry"
    assert body["accepts"][0]["resource"].endswith("/audit/wcag"), (
        "the re-challenge names a different resource than the one the caller paid for"
    )


def test_failed_bundle_does_not_settle_either(monkeypatch):
    """The bundle is the priciest route -- the most expensive thing to wrongly
    charge for."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)

    def _boom(*a, **k):
        raise RuntimeError("target site unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)
    # The bundle loads the page through its own combined runner, not _run_axe.
    # Stubbing only _run_axe let this test pass wherever Chromium could not
    # launch and fail with a real 200 wherever it could.
    monkeypatch.setattr(module, "_run_axe_and_performance", _boom)

    client = TestClient(module.app)
    response = client.post(
        "/audit/bundle",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 502
    assert calls["settled"] == 0


def test_bundle_hits_the_target_url_only_twice(monkeypatch):
    """The bundle used to fetch the audited URL four times per call: two full
    Chromium page loads (axe, performance) plus two HTTP GETs (SEO, security).
    That is 4x the latency on the priciest route, and four hits on a
    stranger's origin per call is how an audit bot gets its user agent
    blocked. One rendered load feeds both browser checks; one GET feeds both
    response checks."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    page_loads = {"n": 0}
    http_gets = {"n": 0}

    def _count_page_load(fn, **kwargs):
        page_loads["n"] += 1
        page = _FakePage()
        return fn(page)

    def _count_http_get(url):
        http_gets["n"] += 1
        return object()

    with patch.object(module.browser_pool, "with_page", _count_page_load), patch.object(
        module.audits, "fetch_once", _count_http_get
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module._axe, "run", return_value=_FakeAxeResult()
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )

    assert response.status_code == 200
    assert page_loads["n"] == 1, f"expected 1 rendered page load, got {page_loads['n']}"
    assert http_gets["n"] == 1, f"expected 1 HTTP GET, got {http_gets['n']}"


def test_bundle_shares_one_http_response_between_seo_and_security(monkeypatch):
    """Both response-based checks must be handed the SAME response object --
    if either re-fetches, we are back to hammering the origin."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    shared = object()
    seen = []

    def _seo(html, url, response=None):
        seen.append(("seo", response))
        return {"status": "ok", "pass": True, "findings": []}

    def _security(url, response=None):
        seen.append(("security", response))
        return {"status": "ok", "pass": True, "findings": []}

    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(module.audits, "fetch_once", lambda url: shared), patch.object(
        module.audits, "run_seo_audit", _seo
    ), patch.object(
        module.audits, "run_security_audit", _security
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )

    assert response.status_code == 200
    assert [name for name, _ in seen] == ["seo", "security"]
    assert all(resp is shared for _, resp in seen), "an audit re-fetched instead of sharing"


class _FakePage:
    """Minimal stand-in for a Playwright page in the combined-load path."""

    def on(self, event, handler):
        fake_response = type("R", (), {"headers": {"content-length": "100"}})()
        handler(fake_response)

    def route(self, pattern, handler):
        # The real Page has this, and every navigation now installs a request
        # guard through it. A double without it would make the guard look
        # optional here while being mandatory in production.
        self.routed = (pattern, handler)

    def goto(self, url, **kwargs):
        return None

    def evaluate(self, script):
        return 42


class _FakeAxeResult:
    response = {"violations": []}


# --- The node sells audits; it does not give them away ---------------------


def test_free_scan_endpoint_is_gone(monkeypatch):
    """An audit costs a real browser page load. A free endpoint funds
    strangers' compute and is an abuse vector, so it was removed rather than
    merely hidden from the page."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    assert client.post("/scan/free", json={"url": "https://example.com"}).status_code == 404


def test_manifest_advertises_no_unpaid_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    unpaid = [e for e in manifest["endpoints"] if e.get("payment_required") is False]
    assert unpaid == [], f"manifest advertises unpaid endpoints: {unpaid}"


def test_no_served_page_offers_a_free_scan(monkeypatch):
    """"No free scan" is a settled decision, and it holds on every page this
    service serves. Checking only `/` leaves the other two unguarded while
    reading exactly like a guard that covers them all.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    for route in ("/", "/billing/success", "/billing/cancel"):
        html = client.get(route).text.lower()
        assert "/scan/free" not in html, f"{route} links a free-scan route"
        for phrase in ("free scan", "free demo", "try it free", "scan for free"):
            assert phrase not in html, f"{route} still offers something free: {phrase!r}"


def test_landing_page_never_prints_a_per_call_cent_price(monkeypatch):
    """A human must not see the per-call rate sitting a scroll above a plan.

    A2A is still the product and the machine section still leads -- that is
    the test below. But printing the per-call rate on the same page as the
    plans invites one subtraction and makes every plan look absurd, which is
    the same trap that killed the old scan-denominated plan. Agents read the
    exact rate from /.well-known/agent.json and from the 402 challenge; that
    is authoritative and cannot drift from what the routes charge, which a
    hand-written page can.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    html = TestClient(module.app).get("/").text

    for price in ("$0.05", "$0.15", '"0.05"', '"0.15"'):
        assert price not in html, (
            f"landing page prints the per-call rate {price} -- it undercuts the plans"
        )
    # The rate has to remain reachable, just not printed here.
    assert "/.well-known/agent.json" in html


def test_machine_surfaces_still_publish_the_exact_rate(monkeypatch):
    """Removing the price from the page must not remove it from the places a
    paying agent actually reads. If it did, nothing could price a call."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    assert "$0.05" in client.get("/llms.txt").text
    manifest = client.get("/.well-known/agent.json").json()
    assert manifest["pricing"]["single_audit_usd"] == 0.05
    assert manifest["pricing"]["bundle_usd"] == 0.15
    challenge = client.post("/audit/wcag", json={"url": "https://example.com"}).json()
    assert challenge["price_usd"] == 0.05


# --- One-off paid report ---------------------------------------------------


def _billing_on(monkeypatch, module):
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)


def test_report_requires_a_genuinely_paid_session(monkeypatch):
    """The report URL is the only thing between a stranger and a free audit,
    so an unpaid or unknown session must produce nothing -- and must never
    run the audit, which is the thing that actually costs us."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(module.billing, "paid_report_request", lambda sid: None)

    ran = []
    monkeypatch.setattr(module, "_run_axe_and_performance", lambda url: ran.append(url))

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_not_paid"})

    assert response.status_code == 404
    assert ran == [], "an audit was run for an unpaid session"


def test_paid_report_runs_the_audit_and_renders(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://example.com"}
    )
    monkeypatch.setattr(module.billing, "load_report", lambda sid: None)
    saved = {}
    monkeypatch.setattr(
        module.billing, "save_report", lambda sid, url, result: saved.update(result=result)
    )
    monkeypatch.setattr(
        module,
        "_run_axe_and_performance",
        lambda url: ({"violations": []}, {"pass": True, "findings": [], "metrics": {}}),
    )
    monkeypatch.setattr(module.audits, "fetch_once", lambda url: object())
    monkeypatch.setattr(
        module.audits, "run_seo_audit", lambda h, u, response=None: {"pass": True, "findings": []}
    )
    monkeypatch.setattr(
        module.audits, "run_security_audit", lambda u, response=None: {"pass": True, "findings": []}
    )

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "example.com" in response.text
    assert saved.get("result"), "a successful report should be cached for re-viewing"


def test_paid_report_is_not_rerun_when_already_cached(monkeypatch):
    """A refresh must not re-run an audit the buyer paid for exactly once."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://example.com"}
    )
    monkeypatch.setattr(
        module.billing,
        "load_report",
        lambda sid: {"url": "https://example.com", "result": {"wcag": {"pass": True, "violations": []}}},
    )
    ran = []
    monkeypatch.setattr(module, "_run_axe_and_performance", lambda url: ran.append(url))

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 200
    assert ran == [], "cached report was re-audited"


def test_failed_report_is_not_cached_and_says_purchase_stands(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://down.example"}
    )
    monkeypatch.setattr(module.billing, "load_report", lambda sid: None)
    saved = []
    monkeypatch.setattr(
        module.billing, "save_report", lambda *a, **k: saved.append(a)
    )

    def _boom(url):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(module, "_run_axe_and_performance", _boom)

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 502
    assert saved == [], "a failed report must not be cached as the buyer's result"
    assert "purchase still stands" in response.text


def test_report_escapes_content_from_the_audited_site(monkeypatch):
    """Findings carry text from a third-party page. Unescaped, an audited
    site could write markup into a report its owner is about to read."""
    module = _load_main(monkeypatch)
    result = {
        "wcag": {"pass": False, "violations": [
            {"id": "<script>alert(1)</script>", "impact": "critical", "help": "x", "nodes_affected": 1}
        ]},
        "seo": {"pass": True, "findings": []},
        "security": {"pass": True, "findings": []},
        "performance": {"pass": True, "findings": [], "metrics": {}},
    }
    html = module._render_report("https://evil.example/<img src=x>", result)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x>" not in html


def test_report_checkout_501s_when_not_configured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/billing/report", json={"email": "a@example.com", "url": "https://example.com"}
    )
    assert response.status_code == 501


# --- MCP Streamable HTTP endpoint ------------------------------------------


def _rpc(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


def test_mcp_initialize_negotiates_protocol_version(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    r = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    result = r.json()["result"]
    assert r.json()["jsonrpc"] == "2.0"
    # Echo a version we support...
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "hubvibe-site-audit"

    # ...but fall back to ours for one we don't.
    r2 = _rpc(client, "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert r2.json()["result"]["protocolVersion"] == module.MCP_PROTOCOL_VERSIONS[0]


def test_mcp_notifications_get_no_body(monkeypatch):
    """A JSON-RPC notification has no id and must not be answered with a
    result, or a conforming client errors on the unexpected response."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content in (b"", b"null")


def test_mcp_tools_list_is_free_and_complete(monkeypatch):
    """Discovery must not require payment: an agent has to see what this node
    sells and what it costs before it can decide to buy."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    tools = _rpc(client, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    expected = {
        "audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle",
        "hubvibe_predictive_probability_engine",
    }
    assert names == expected
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "$" in t["description"], "tool description must state its price"


def test_mcp_tool_schemas_never_admit_an_empty_call(monkeypatch):
    """The rule tools/call enforces -- url or html, at least one -- must be
    expressed IN the schema, not only in prose.

    Registry crawlers (Glama, Smithery) and schema-driven agents build calls
    from inputSchema alone. Before this, the html-or-url schema had no
    `required` and no `anyOf`, so {} was a schema-legal call: the agent does
    everything right by its lights, pays the auth round-trip, and gets a 400.
    An endpoint that invites doomed calls reads as flaky and gets dropped
    from the agent's toolbox.
    """
    import jsonschema

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    tools = _rpc(client, "tools/list").json()["result"]["tools"]

    for t in tools:
        schema = t["inputSchema"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({}, schema)
        # And the arguments the route genuinely accepts must stay legal --
        # the bare-url body every audit tool accepts; the worker-backed
        # tool's example call is checked in test_workers_stats.py.
        if t["name"].startswith("audit_"):
            jsonschema.validate({"url": "https://example.com"}, schema)


def test_mcp_tool_call_without_payment_is_an_error_result_not_a_crash(monkeypatch):
    """A payment requirement is a normal outcome the model should see, so it
    belongs in the result as isError -- not a JSON-RPC protocol error."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)

    ran = []
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: ran.append(1))

    body = _rpc(
        client, "tools/call", {"name": "audit_wcag", "arguments": {"url": "https://example.com"}}
    ).json()

    assert "error" not in body
    assert body["result"]["isError"] is True
    assert "Payment required" in body["result"]["content"][0]["text"]
    assert ran == [], "an unpaid MCP call must not run the audit"


def test_mcp_paywall_is_machine_parseable_not_prose(monkeypatch):
    """An agent must be able to read the price off the MCP paywall.

    The challenge used to be stringified into the middle of an English
    sentence, so the only way to find `price_usd` or `accepts` was to
    substring-scrape JSON out of prose -- unusable to exactly the machine
    buyers this endpoint exists to serve. The text must parse as JSON.
    """
    import json

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "audit_bundle", "arguments": {"url": "https://example.com"}},
        },
    )
    result = response.json()["result"]
    assert result["isError"] is True

    challenge = json.loads(result["content"][0]["text"])
    assert challenge["error"] == "payment_required"
    assert challenge["price_usd"] == 0.15, "MCP must quote the same price as the REST route"
    assert isinstance(challenge["accepts"], list)
    assert "docs" in challenge
    # The human-readable line survives alongside the machine-readable fields.
    assert "Payment required" in challenge["message"]


def test_mcp_tool_call_with_internal_key_runs_and_returns_content(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    client = TestClient(module.app)

    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": {"url": "https://example.com"}},
        },
        headers={"X-API-Key": "test-key"},
    ).json()

    assert body["id"] == 7
    assert body["result"]["isError"] is False
    import json as _json
    payload = _json.loads(body["result"]["content"][0]["text"])
    assert payload["pass"] is True


def test_mcp_failed_audit_reports_error_and_is_not_billed(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")

    def _boom(*a, **k):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)
    billed = []
    monkeypatch.setattr(module, "_bill", lambda auth, units=1: billed.append(units))
    client = TestClient(module.app)

    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": {"url": "https://example.com"}},
        },
        headers={"X-API-Key": "test-key"},
    ).json()

    assert body["result"]["isError"] is True
    assert billed == [], "a failed MCP audit must not be billed"


def test_mcp_unknown_method_is_a_jsonrpc_error(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    body = _rpc(client, "resources/list").json()
    assert body["error"]["code"] == -32601


def test_mcp_tool_prices_match_the_rest_routes(monkeypatch):
    """A tool that advertises a price the route won't charge makes an agent
    build the wrong payment."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    for name, price in module._MCP_TOOL_PRICES.items():
        route = "/audit/" + name.replace("audit_", "")
        charged = client.post(route, json={"url": "https://example.com"}).json()["price_usd"]
        assert charged == price, f"{name} advertises {price}, {route} charges {charged}"


def test_mcp_initialize_only_returns_handshake_protocol_versions(monkeypatch):
    """Regression: we first answered with 2026-07-28, the SDK's newest
    constant. That is a 'modern' version negotiated out-of-band and is NOT
    valid in an initialize result -- the official client checks the response
    against HANDSHAKE_PROTOCOL_VERSIONS and hard-errors on anything else, so
    every real MCP client refused to connect. Verified against the real SDK
    after fixing; this guards the constant."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    handshake_versions = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
    assert set(module.MCP_PROTOCOL_VERSIONS) <= handshake_versions, (
        "MCP_PROTOCOL_VERSIONS contains a version that is invalid in an "
        "initialize response; real clients will refuse to connect"
    )

    for requested in (None, "2025-06-18", "not-a-version"):
        params = {"capabilities": {}}
        if requested:
            params["protocolVersion"] = requested
        got = _rpc(client, "initialize", params).json()["result"]["protocolVersion"]
        assert got in handshake_versions, f"initialize returned unusable version {got}"


# --- Discovery contract: what an agent reads before it decides to pay ------
#
# Everything below guards a machine-readable surface. The failure these
# catch is not a 500 -- it is an agent that reads the manifest, builds a
# call, and gets a 400 or never calls at all, which from this side is
# indistinguishable from nobody wanting to buy.


def test_the_html_or_url_schema_states_the_either_or_the_routes_enforce():
    """/audit/wcag and /audit/seo 400 on a body with neither field. A schema
    that leaves `required` off entirely tells an agent `{}` is acceptable and
    sends it into that 400."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wcag_audit_main_schema", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    schema = module._MCP_HTML_OR_URL_SCHEMA
    assert schema["anyOf"] == [{"required": ["url"]}, {"required": ["html"]}], (
        "the html-or-url routes require one of the two; the schema must say so"
    )
    # A stricter schema than the route is its own failure: the Pydantic
    # models ignore unknown keys, so declaring additionalProperties false
    # would advertise a rejection that never happens.
    assert "additionalProperties" not in schema
    assert module._MCP_URL_SCHEMA["required"] == ["url"]


def test_every_mcp_tool_declares_what_comes_back_not_just_what_goes_in(monkeypatch):
    """An agent decides whether to spend money here from the tool definition
    alone. Without an outputSchema it cannot tell, before paying, whether the
    response is a shape its pipeline can consume."""
    module = _load_main(monkeypatch)

    for tool in module._mcp_tools():
        assert tool["outputSchema"]["type"] == "object", tool["name"]
        assert tool["title"], tool["name"]
        annotations = tool["annotations"]
        assert annotations["destructiveHint"] is False, tool["name"]
        assert isinstance(annotations["readOnlyHint"], bool), tool["name"]
        assert isinstance(annotations["idempotentHint"], bool), tool["name"]
        assert isinstance(annotations["openWorldHint"], bool), tool["name"]
        if not tool["name"].startswith("audit_"):
            # A worker-backed tool returns its route's 200 envelope; the
            # field callers branch on there is `status`.
            assert "status" in tool["outputSchema"]["properties"], tool["name"]
            assert "result" in tool["outputSchema"]["properties"], tool["name"]
            continue
        assert "pass" in tool["outputSchema"]["properties"], (
            f"{tool['name']} must declare the field callers branch on"
        )
        assert tool["outputSchema"]["required"] == ["pass"]
        # Audits read a third-party page and mutate nothing. An orchestrator
        # uses these to decide whether it may retry or parallelise unattended.
        assert annotations["readOnlyHint"] is True, tool["name"]
        assert annotations["idempotentHint"] is True, tool["name"]
        assert annotations["openWorldHint"] is True, tool["name"]


def test_mcp_json_serves_the_same_schemas_the_mcp_endpoint_does(monkeypatch):
    """Two hand-maintained copies of a schema drift, and the one that drifts
    is the one agents are reading. /mcp.json is what the MCP registry points
    at; /mcp is what a client actually calls. They must agree."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    served = {tool["name"]: tool for tool in client.get("/mcp.json").json()["tools"]}
    live = {tool["name"]: tool for tool in module._mcp_tools()}
    assert set(served) == set(live)

    for name, tool in live.items():
        for field in ("title", "inputSchema", "outputSchema", "annotations"):
            assert served[name][field] == tool[field], f"{name}.{field} drifted from /mcp"


def test_the_static_mcp_manifest_on_disk_matches_what_is_served(monkeypatch):
    """The route overwrites these fields at serve time, so a stale copy on
    disk cannot reach an agent through /mcp.json -- but the raw file is
    fetched directly from the repo by crawlers, so it must not lie either."""
    import json

    module = _load_main(monkeypatch)
    static = json.loads(
        (REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text()
    )
    on_disk = {tool["name"]: tool for tool in static["tools"]}
    for tool in module._mcp_tools():
        for field in ("title", "inputSchema", "outputSchema", "annotations"):
            assert on_disk[tool["name"]][field] == tool[field], (
                f"static mcp.json {tool['name']}.{field} is stale -- "
                "regenerate it from _mcp_tools()"
            )


def test_agent_manifest_publishes_parseable_schemas_not_only_prose(monkeypatch):
    """`"url": "string (required)"` is not something a crawler or a request
    generator can act on. The JSON Schema alongside it is."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    endpoints = client.get("/.well-known/agent.json").json()["endpoints"]
    assert endpoints, "manifest advertises no endpoints"
    # Same objects the MCP tools advertise -- one contract, not three.
    advertised = [tool["inputSchema"] for tool in module._mcp_tools()]
    for endpoint in endpoints:
        schema = endpoint["input_schema"]
        assert schema["type"] == "object", endpoint["path"]
        assert "properties" in schema, endpoint["path"]
        assert endpoint["output_schema"]["type"] == "object", endpoint["path"]
        assert schema in advertised, (
            f"{endpoint['path']} publishes a schema no tool advertises"
        )


def test_the_audit_alias_is_indexable_like_every_other_paid_path(
    monkeypatch, load_main_fresh
):
    """/audit is the shortest and most guessable paid path here. It is an
    alias of /audit/wcag and so has no catalog row of its own, which left it
    as the one paid route absent from Bazaar discovery -- findable only by
    someone who already had the URL."""
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_alias")

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    alias = client.post("/audit", json={"url": "https://example.com"})
    named = client.post("/audit/wcag", json={"url": "https://example.com"})

    assert alias.status_code == named.status_code == 402
    assert alias.json()["extensions"] == named.json()["extensions"], (
        "the alias must advertise the same capability as the route it aliases"
    )


def test_the_api_key_rail_appears_in_accepts_when_it_can_settle(
    monkeypatch, load_main_fresh
):
    """`accepts` is the array an agent iterates. A CI pipeline holding a
    pre-funded key had to parse prose out of `alternative` to learn its key
    was spendable here, while agent.json's payment.methods listed the rail --
    two machine-readable surfaces disagreeing about the same fact."""
    for var in ("STRIPE_METERED_PRICE_ID", "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.delenv("AUDIT_API_KEY", raising=False)

    module = load_main_fresh("wcag_audit_main_keyrail")

    from fastapi.testclient import TestClient

    body = TestClient(module.app).post("/audit/bundle", json={"url": "https://example.com"}).json()
    # Not in `accepts` -- that array belongs to the x402 spec and a non-x402
    # entry in it makes the whole challenge fail client-side validation. The
    # rail is still machine-readable, one key over.
    # 2026-09-06: the subscription tiers are retired, so a configured Stripe
    # key no longer advertises a subscription-backed key rail anywhere -- a
    # rail whose checkout refuses is a rail that cannot settle.
    rail = next((a for a in body["other_rails"] if a["protocol"] == "api_key"), None)
    assert rail is None, "a retired subscription rail is still advertised in other_rails"
    assert not any(a.get("protocol") == "api_key" for a in body["accepts"]), (
        "a non-x402 rail in accepts[] makes the 402 unpayable by every "
        "conforming client"
    )

    manifest = TestClient(module.app).get("/.well-known/agent.json").json()
    assert "stripe_api_key" not in manifest["payment"]["methods"], (
        "agent.json and the 402 must agree: no subscription rail is for sale"
    )


def test_the_api_key_rail_is_omitted_when_no_key_could_be_issued(monkeypatch):
    """Fail closed, like every other rail: with no Stripe configured there is
    no way to issue or meter a key, so advertising it would send an agent to
    a rail that cannot settle."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    assert not module.billing.is_configured()

    body = TestClient(module.app).post("/audit/wcag", json={"url": "https://example.com"}).json()
    assert all(a["protocol"] != "api_key" for a in body["accepts"])


def test_the_registry_manifest_cannot_be_rejected_for_a_field_the_schema_bounds():
    """server.json is published to the MCP registry, which validates it
    against the schema the file itself names. The bounds worth guarding
    offline are the ones whose violation is invisible here and only shows up
    as a rejected publish -- above all description's 100-character cap.
    """
    import json

    manifest = json.loads((REPO_ROOT / "server.json").read_text())

    for field in ("name", "description", "version"):
        assert manifest.get(field), f"server.json is missing required field {field}"

    assert len(manifest["description"]) <= 100, (
        "the registry caps description at 100 characters and rejects the publish"
    )
    assert len(manifest.get("title", "")) <= 100
    assert manifest["name"].count("/") == 1, "name must be reverse-DNS with one slash"

    # Icons are rendered by the registry and by every subregistry mirroring
    # it. The schema requires HTTPS and tells consumers to prefer icons on
    # the server's own domain, so an icon pointing anywhere else is one
    # consumers are entitled to refuse to load.
    origin = manifest["websiteUrl"].rstrip("/")
    for icon in manifest.get("icons", []):
        assert icon["src"].startswith("https://"), icon["src"]
        assert icon["src"].startswith(origin), (
            f"{icon['src']} is not served from this server's own domain"
        )
        assert icon["mimeType"] in {
            "image/png", "image/jpeg", "image/jpg", "image/svg+xml", "image/webp"
        }, icon["mimeType"]


def test_every_registry_icon_is_actually_served_by_this_app(monkeypatch):
    """An icon URL that 404s is worse than no icon: the listing renders
    broken rather than plain. These are paths on our own service, so whether
    they resolve is something this repo can answer without the network."""
    import json
    from fastapi.testclient import TestClient

    manifest = json.loads((REPO_ROOT / "server.json").read_text())
    icons = manifest.get("icons", [])
    if not icons:
        pytest.skip("server.json declares no icons")

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    origin = manifest["websiteUrl"].rstrip("/")

    for icon in icons:
        path = icon["src"][len(origin):]
        response = client.get(path)
        assert response.status_code == 200, f"{path} is advertised but not served"
        assert icon["mimeType"] in response.headers["content-type"], (
            f"{path} is served as {response.headers['content-type']}, "
            f"advertised as {icon['mimeType']}"
        )


# --- Registry crawlers must be able to CONNECT, not just read ---------------
#
# /mcp.json is the file the MCP registry, Glama and Smithery point at. It had
# told every reader, in as many words, "not a live MCP server -- wrap these
# with an MCP-to-HTTP adapter". That was true when it was written and stopped
# being true when /mcp shipped, and nothing failed when it went stale: an
# agent that believes it either builds an adapter it does not need or gives up
# on the connection entirely. server.json already advertised /mcp as a
# streamable-http remote, so the two files shipped contradicting each other.

def test_mcp_json_advertises_a_remote_that_actually_answers(monkeypatch):
    """The connect URL a crawler reads must be a route that speaks MCP.

    Asserting the string alone would pass just as well against a typo, so
    this drives the advertised path with a real tools/list.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/mcp.json").json()

    remotes = manifest.get("remotes") or []
    assert remotes, "/mcp.json must tell a crawler where to connect"
    remote = remotes[0]
    assert remote["type"] == "streamable-http"

    path = remote["url"].replace(module.PUBLIC_BASE_URL.rstrip("/"), "")
    response = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, f"{path} is advertised but does not answer"
    served = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert served == {tool["name"] for tool in manifest["tools"]}


def test_mcp_json_never_hands_a_client_another_deployment_url(monkeypatch):
    """The static file carries production absolute URLs so a crawler reading
    the raw file out of the repo can resolve them. Served, they have to name
    the host that is answering, or a self-hosted copy sends its clients to
    somebody else's paid endpoint."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    module = _load_main(monkeypatch)
    manifest = TestClient(module.app).get("/mcp.json").json()

    assert manifest["remotes"][0]["url"] == "https://audit.example.test/mcp"
    assert manifest["websiteUrl"] == "https://audit.example.test/"
    assert manifest["documentationUrl"] == "https://audit.example.test/.well-known/agent.json"
    assert manifest["icons"][0]["src"] == "https://audit.example.test/favicon.svg"

    blob = str(manifest)
    assert "run.app" not in blob, "a production URL survived into a self-hosted manifest"


def test_mcp_json_carries_the_identity_fields_a_registry_indexes(monkeypatch):
    """name/description alone gets a server listed and ignored. A crawler
    scoring an entry reads version, repository and a documentation URL, and
    an agent choosing between two servers reads them too."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    manifest = TestClient(module.app).get("/mcp.json").json()

    for field in ("name", "description", "version", "websiteUrl", "documentationUrl"):
        assert manifest.get(field), f"/mcp.json is missing {field}"
    assert manifest["repository"]["url"].startswith("https://github.com/")


def test_mcp_json_version_matches_server_json(monkeypatch):
    """server.json is what the official registry publishes; /mcp.json is what
    Glama and Smithery read. Two version numbers that disagree make one of
    them wrong, and there is no way for a reader to tell which."""
    import json

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    manifest = TestClient(module.app).get("/mcp.json").json()
    server_json = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    assert manifest["version"] == server_json["version"]


def test_every_sitemap_url_is_a_route_this_service_serves(monkeypatch):
    """A sitemap entry that 404s costs crawl budget and teaches the crawler
    to trust the file less. The discovery surfaces are the entries that
    matter -- they are the reason a machine reads the sitemap at all."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    sitemap = client.get("/sitemap.xml").text

    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert locs, "sitemap.xml lists nothing"

    base = "https://hubvibe-io.com"
    for loc in locs:
        assert loc.startswith(base), f"{loc} is not on this service"
        path = loc[len(base):] or "/"
        assert client.get(path).status_code == 200, f"sitemap lists {path}, which does not serve"

    for required in ("/mcp.json", "/.well-known/agent.json"):
        assert any(loc.endswith(required) for loc in locs), (
            f"sitemap omits {required} -- it is the file registry crawlers come for"
        )


def test_one_version_number_across_every_surface_that_publishes_one(monkeypatch):
    """server.json (official registry), /mcp.json (Glama, Smithery), the
    OpenAPI spec and the MCP handshake all name a version. Three of those
    were separate literals and had already drifted: the registry said 1.1.2
    while every client that completed an MCP handshake was told 1.1.0."""
    import json

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    registry = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))["version"]

    assert module.SERVICE_VERSION == registry
    assert client.get("/mcp.json").json()["version"] == registry
    assert client.get("/openapi.json").json()["info"]["version"] == registry

    handshake = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}},
    ).json()
    assert handshake["result"]["serverInfo"]["version"] == registry

    # The standalone stdio MCP server (integrations/mcp_server.py) names a
    # version in its own handshake too. It said 1.0.0 while everything else
    # said 1.2.0. Read as text: the `mcp` package is not a root dependency.
    import re

    stdio = (REPO_ROOT / "wcag-audit-engine" / "integrations" / "mcp_server.py").read_text()
    literal = re.search(r'^VERSION = "([^"]+)"', stdio, re.M)
    assert literal and literal.group(1) == registry, "integrations/mcp_server.py names another version"


# --- The challenge must be payable by a real client, not merely well-meant ---
#
# Every x402 test in this repo, and every check in verify-live.sh, asked
# whether the 402 MENTIONS x402. None asked whether an agent could pay it. It
# could not: `accepts[]` was a shape of our own invention, missing four fields
# PaymentRequirementsV1 requires and spelling payTo as pay_to, so the x402
# client library raised a ValidationError before producing any signature. The
# failure never reached the facilitator -- nothing to reject, nothing logged,
# and from this side indistinguishable from nobody wanting to buy. The rail
# was advertised as live, and was structurally unpayable, for as long as it
# had been advertised.
#
# So these drive the real client against the real routes. The assertion is not
# "the body looks right" -- it always looked right -- but "a paying agent gets
# a signature out of this".

def _paying_client():
    from eth_account import Account
    from x402 import max_amount, x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client

    # A throwaway key. Signing is local and offline; nothing is broadcast and
    # the account needs no funds for the client to build a payment.
    account = Account.from_key("0x" + "1" * 63 + "2")
    client = x402ClientSync()
    register_exact_evm_client(
        client, EthAccountSigner(account), policies=[max_amount(1_000_000)]
    )
    return x402HTTPClientSync(client)


def _sign_402(http_client, headers, body, request_url="https://node.example/audit"):
    """handle_402_response across x402 client versions: 2.18 takes
    (headers, body); 2.21+ requires a third `request_url`. Read off the
    installed callable, same as hubvibe_tollbooth._sign_402, so this suite
    proves payability on whichever x402 the machine has."""
    import inspect

    handler = http_client.handle_402_response
    call = [dict(headers), body]
    if "request_url" in inspect.signature(handler).parameters:
        call.append(request_url)
    return handler(*call)


@pytest.mark.parametrize(
    "route,price",
    [
        ("/audit", 0.05),
        ("/audit/wcag", 0.05),
        ("/audit/seo", 0.05),
        ("/audit/security", 0.05),
        ("/audit/performance", 0.05),
        ("/audit/bundle", 0.15),
    ],
)
def test_every_paid_route_returns_a_402_a_real_client_can_pay(
    monkeypatch, load_main_fresh, route, price
):
    from fastapi.testclient import TestClient

    _x402_env(monkeypatch)
    module = load_main_fresh(f"wcag_main_payable_{route.replace('/', '_')}")
    response = TestClient(module.app).post(route, json={"url": "https://example.com"})
    assert response.status_code == 402

    headers, _payload = _sign_402(_paying_client(), response.headers, response.content)
    assert headers, f"{route} produced no payment header"
    assert {k.upper() for k in headers} & {"X-PAYMENT", "PAYMENT-SIGNATURE"}


def test_a_v1_only_client_can_still_pay_from_the_body(monkeypatch, load_main_fresh):
    """The v2 header is preferred, but a v1 client never looks at it. Strip it
    and the body alone must still yield a signature, or every client that
    predates v2 is quietly locked out."""
    from fastapi.testclient import TestClient

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_main_v1_only")
    response = TestClient(module.app).post("/audit/wcag", json={"url": "https://example.com"})

    body_only = {
        k: v for k, v in response.headers.items() if k.lower() != "payment-required"
    }
    headers, _ = _sign_402(_paying_client(), body_only, response.content)
    assert "X-PAYMENT" in {k.upper() for k in headers}


def test_the_402_carries_the_v2_challenge_header(monkeypatch, load_main_fresh):
    """v2 does not put the challenge in the body at all -- a client reads this
    header first and only falls back to the body. Without it there is nowhere
    to put the v2 `extensions` slot or the service name the Bazaar indexes."""
    from fastapi.testclient import TestClient

    from x402.http.utils import decode_payment_required_header

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_main_v2_header")
    response = TestClient(module.app).post("/audit/wcag", json={"url": "https://example.com"})

    raw = response.headers.get("payment-required")
    assert raw, "no PAYMENT-REQUIRED header -- every v2 client falls back to v1"
    challenge = decode_payment_required_header(raw)
    assert challenge.x402_version == 2
    assert challenge.accepts, "a v2 challenge with no requirements is unpayable"
    # service_name and tags are what an agent shopping the Bazaar by
    # capability matches on. The v1 body has no field for either.
    assert challenge.resource.service_name
    assert challenge.resource.tags


def test_a_v2_payment_signature_header_is_actually_read(monkeypatch, load_main_fresh):
    """A v2 client answers with PAYMENT-SIGNATURE, not X-PAYMENT. Advertising
    v2 while reading only X-PAYMENT would hand a client a challenge it can
    satisfy and then ignore the answer, 402ing it forever."""
    from fastapi.testclient import TestClient

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_main_v2_read")

    seen = {}

    def _capture(header, price=None, **kw):
        seen["header"] = header
        return None  # still unpaid; we only care that it was looked at

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _capture)

    TestClient(module.app).post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"PAYMENT-SIGNATURE": "c2lnbmF0dXJl"},
    )
    assert seen.get("header") == "c2lnbmF0dXJl", (
        "the v2 payment header never reached verification"
    )


class _FakeTransaction:
    """Just enough of a Firestore transaction for the prepaid ledger."""

    def __init__(self, store):
        self.store = store

    def update(self, ref, changes):
        self.store[ref.key].update(changes)


class _FakeRef:
    def __init__(self, store, key):
        self.store = store
        self.key = key

    def get(self, transaction=None):
        class _Snap:
            def __init__(self, data):
                self.exists = data is not None
                self._data = data

            def to_dict(self):
                return dict(self._data or {})

        return _Snap(self.store.get(self.key))

    def set(self, data):
        self.store[self.key] = dict(data)


class _FakeCollection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return _FakeRef(self.store, key)


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return _FakeCollection(self.store)

    def transaction(self):
        return _FakeTransaction(self.store)


def _prepaid_billing(monkeypatch, module, store):
    """Point billing at an in-memory key store, with the transactional
    decorator reduced to a plain call -- the real one needs a live client."""
    monkeypatch.setattr(module.billing, "_firestore", lambda: _FakeDb(store))
    import google.cloud.firestore as fs

    monkeypatch.setattr(fs, "transactional", lambda fn: fn)
    return store


def test_a_prepaid_key_is_issued_with_the_balance_that_was_bought(monkeypatch):
    module = _load_main(monkeypatch)
    store = _prepaid_billing(monkeypatch, module, {})

    key = module.billing.issue_prepaid_key(47)

    assert store[key]["prepaid_balance_cents"] == 47
    assert store[key]["active"] is True
    # No Stripe Customer: nothing to invoice, nothing to meter.
    assert store[key]["customer_id"] is None


def test_a_prepaid_key_cannot_be_issued_with_no_balance(monkeypatch):
    """A key worth nothing is a credential that reads as valid and buys
    nothing -- worse than refusing, because the caller only finds out on the
    next call."""
    module = _load_main(monkeypatch)
    _prepaid_billing(monkeypatch, module, {})
    with pytest.raises(ValueError):
        module.billing.issue_prepaid_key(0)


def test_spending_draws_the_balance_down_and_stops_at_zero(monkeypatch):
    module = _load_main(monkeypatch)
    store = _prepaid_billing(monkeypatch, module, {})
    key = module.billing.issue_prepaid_key(10)

    assert module.billing.spend_prepaid(key, 3) is True
    assert store[key]["prepaid_balance_cents"] == 7
    assert module.billing.spend_prepaid(key, 7) is True
    assert store[key]["prepaid_balance_cents"] == 0
    # Empty is empty: no overdraft, no free call.
    assert module.billing.spend_prepaid(key, 1) is False
    assert store[key]["prepaid_balance_cents"] == 0


def test_spending_more_than_the_balance_takes_nothing(monkeypatch):
    """A partial debit would leave the caller charged and unserved."""
    module = _load_main(monkeypatch)
    store = _prepaid_billing(monkeypatch, module, {})
    key = module.billing.issue_prepaid_key(5)

    assert module.billing.spend_prepaid(key, 10) is False
    assert store[key]["prepaid_balance_cents"] == 5


def test_spending_fails_closed_when_the_store_is_unreachable(monkeypatch):
    """Unlike the monthly quota, which fails OPEN so an outage cannot cut off
    a paid subscriber. A prepaid balance IS the payment, so "don't know"
    must not mean "serve it"."""
    module = _load_main(monkeypatch)

    def _boom():
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(module.billing, "_firestore", _boom)
    assert module.billing.spend_prepaid("some-key", 3) is False


def test_an_unknown_or_inactive_key_spends_nothing(monkeypatch):
    module = _load_main(monkeypatch)
    store = _prepaid_billing(monkeypatch, module, {})
    assert module.billing.spend_prepaid("never-issued", 3) is False

    key = module.billing.issue_prepaid_key(10)
    store[key]["active"] = False
    assert module.billing.spend_prepaid(key, 3) is False


def test_a_paid_response_hands_back_the_key_it_just_bought(monkeypatch):
    """The agent has no account and no second channel. If the response does
    not carry the key, the money was taken and nothing spendable returned."""
    module = _load_main(monkeypatch)
    auth = module.AuthContext(
        stripe_billable=False, payment_method="mpp-topup", issued_key="k_abc"
    )
    result = {"status": "ok"}
    module._attach_issued_key(result, auth)
    assert result["api_key"] == "k_abc"
    assert "X-API-Key" in result["api_key_note"]


def test_a_response_carries_no_key_when_none_was_issued(monkeypatch):
    module = _load_main(monkeypatch)
    auth = module.AuthContext(stripe_billable=False, payment_method="x402")
    result = {"status": "ok"}
    module._attach_issued_key(result, auth)
    assert "api_key" not in result


def test_the_base_app_id_meta_tag_is_served_in_the_head(monkeypatch):
    """Base's domain verifier fetches / and reads this tag to confirm the
    domain belongs to the app. It has to be in the <head> of the page the
    live service actually serves -- not merely present in the repo -- so this
    asserts it through the route, and asserts the exact id: a truncated or
    edited value verifies as nothing, and the failure mode is a registration
    that silently never completes.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    html = client.get("/").text

    head = html[: html.index("</head>")]
    # Byte-identical to the snippet Base's Add Domain dialog hands out,
    # self-closing slash included. A parser does not care; a verifier that
    # string-matches its own snippet does, and that failure is silent.
    assert '<meta name="base:app_id" content="6a83832901463168d7e651ca" />' in head
    # And exactly one: a second base:app_id (the retired 6a838306... this
    # replaced, say) leaves the verifier picking between two claims, which
    # is the one way to have the tag present and prove nothing.
    assert html.count('name="base:app_id"') == 1, "more than one Base app is claimed here"


# --- The paid 200 carries the settlement receipt ----------------------------
#
# x402 spec step 10: after settling, the resource server returns the
# facilitator's settle response to the payer in PAYMENT-RESPONSE (v2) /
# X-PAYMENT-RESPONSE (v1). Until this the node delivered the audit and kept
# the transaction hash to itself, so a paying agent had proof of nothing.
# Found by scripts/simulate-paid-call.py -- the first check to read the
# headers of a paid 200 rather than its status code.


def _paid(client, path="/audit/wcag", body=None):
    return client.post(
        path, json=body or {"url": "https://example.com"}, headers={"X-PAYMENT": "signed-payment"}
    )


def test_a_paid_200_carries_the_settlement_receipt(monkeypatch):
    from fastapi.testclient import TestClient
    from x402.http.utils import decode_payment_response_header

    module = _load_main(monkeypatch)
    _x402_caller(monkeypatch, module)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = _paid(TestClient(module.app))

    assert response.status_code == 200
    assert response.json()["pass"] is True, "the receipt must not displace the audit"
    for name in ("PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"):
        assert name in response.headers, f"{name} missing from a paid 200"
    decoded = decode_payment_response_header(response.headers["PAYMENT-RESPONSE"])
    assert decoded.success is True
    assert decoded.transaction == _SETTLED_TX


def test_no_receipt_when_settlement_failed_after_delivery(monkeypatch):
    """No settlement, no receipt -- a header claiming success on a refused
    settle would be a forged proof of payment."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _x402_caller(monkeypatch, module, settle_ok=False)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = _paid(TestClient(module.app))

    assert response.status_code == 402
    assert response.json()["error"] == "settlement_refused"
    assert "PAYMENT-RESPONSE" not in response.headers
    assert "X-PAYMENT-RESPONSE" not in response.headers


def test_an_api_key_call_carries_no_receipt(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 200
    assert "PAYMENT-RESPONSE" not in response.headers


def test_a_paid_mcp_tool_call_carries_the_receipt(monkeypatch):
    """MCP callers pay the same way and are owed the same receipt; the
    JSON-RPC envelope travels over HTTP, so the header is where it goes."""
    from fastapi.testclient import TestClient
    from x402.http.utils import decode_payment_response_header

    module = _load_main(monkeypatch, api_key=None)
    _x402_caller(monkeypatch, module)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    client = TestClient(module.app)

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": {"url": "https://example.com"}},
        },
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert decode_payment_response_header(response.headers["PAYMENT-RESPONSE"]).transaction == _SETTLED_TX


def test_every_paid_route_delivers_through_the_receipt_path():
    """Static guard: each /audit route's handler must end in _deliver(), the
    one place the receipt (and a bought prepaid key) is attached. A route
    that `return result`s directly delivers the audit and drops both."""
    import re

    text = MAIN_PATH.read_text()
    routes = re.split(r'\n@app\.post\("/audit', text)[1:]
    assert len(routes) == 6, f"expected 6 paid routes, found {len(routes)}"
    for chunk in routes:
        handler = chunk.split("\n@app.")[0]
        name = handler.split("\n")[0]
        assert "return _deliver(result, auth)" in handler, f"/audit{name} bypasses _deliver()"


def test_the_manifest_tells_payers_where_the_receipt_is(monkeypatch):
    """An agent reading agent.json before paying should learn it will get a
    settlement receipt back, and in which header."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    payment = TestClient(module.app).get("/.well-known/agent.json").json()["payment"]
    assert "PAYMENT-RESPONSE" in payment["receipt"]


# --- the target URL gate ------------------------------------------------------
#
# Every audit fetches the caller's URL from inside the deployment. Without a
# gate the node was a proxy into wherever it runs: the metadata endpoint,
# loopback, the VPC. Refused with a 400 before rate limiting and before any
# payment is read, so it costs the caller nothing and this node no facilitator
# call. Resolution is checked, not just the name.


def _resolves_to(monkeypatch, *addresses):
    import socket

    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _auth_spy(monkeypatch, module):
    calls = []
    original = module._authorize_and_rate_limit

    def spy(*a, **k):
        calls.append(1)
        return original(*a, **k)

    monkeypatch.setattr(module, "_authorize_and_rate_limit", spy)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/computeMetadata/v1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://127.0.0.1:8080/health",
        "http://[::1]/",
        "http://100.64.0.1/",
    ],
)
def test_private_and_link_local_targets_are_refused_before_payment(monkeypatch, url):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _auth_spy(monkeypatch, module)
    ran = []
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: ran.append(1))

    response = TestClient(module.app).post("/audit/wcag", json={"url": url}, headers={"X-API-Key": "test-key"})

    assert response.status_code == 400, response.text
    assert response.json()["billed"] is False
    assert "Nothing was charged" in response.json()["detail"]
    assert calls == [], "payment/auth was consulted for a URL that must never be fetched"
    assert ran == []


@pytest.mark.parametrize("host", ["localhost", "metadata.google.internal", "metadata", "foo.internal", "LOCALHOST."])
def test_internal_hostnames_are_refused_by_name_without_resolving(monkeypatch, host):
    import socket

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)

    def no_dns(*a, **k):
        raise AssertionError("resolved a hostname that is refused by name")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    response = TestClient(module.app).post(
        "/audit/seo", json={"url": f"http://{host}/"}, headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 400


def test_a_public_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    """The DNS-rebinding shape: an innocent hostname, an A record of 10.0.0.1."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _resolves_to(monkeypatch, "93.184.216.34", "10.0.0.1")
    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://innocent.example/"}, headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 400


def test_a_public_target_passes_the_gate(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _resolves_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    response = TestClient(module.app).post("/audit/wcag", json={"url": "https://innocent.example/"}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "http:///nohost"])
def test_non_http_targets_are_refused(monkeypatch, url):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    response = TestClient(module.app).post("/audit/security", json={"url": url}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 400


def test_an_unresolvable_target_is_refused_rather_than_fetched(monkeypatch):
    import socket

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)

    def nx(*a, **k):
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", nx)
    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://nope.invalid/"}, headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 400
    assert "resolve" in response.json()["detail"]


def test_every_paid_route_is_gated():
    """Static: each /audit route checks the target before authorising."""
    import re

    text = MAIN_PATH.read_text()
    routes = re.split(r'\n@app\.post\("/audit', text)[1:]
    assert len(routes) == 6
    for chunk in routes:
        handler = chunk.split("\n@app.")[0]
        gate = handler.index("_reject_unfetchable_target(payload.url)")
        auth = handler.index("_authorize_and_rate_limit(")
        assert gate < auth, f"/audit{handler.splitlines()[0]} authorises before gating the URL"


def test_the_gate_can_be_turned_off_for_a_local_node_only_by_env(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "1")
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "http://127.0.0.1:9/"}, headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200


def test_the_mcp_tool_call_is_gated_too(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _auth_spy(monkeypatch, module)
    body = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "audit_wcag", "arguments": {"url": "http://169.254.169.254/"}}},
        headers={"X-API-Key": "test-key"},
    ).json()
    assert body["result"]["isError"] is True
    assert "Nothing was charged" in body["result"]["content"][0]["text"]
    assert calls == []


# --- the html body has a ceiling ---------------------------------------------


def test_an_oversized_html_body_is_refused_before_payment(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _auth_spy(monkeypatch, module)
    big = "<p>" + "x" * (module.MAX_HTML_BYTES + 1)
    response = TestClient(module.app).post("/audit/wcag", json={"html": big}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 422
    assert calls == []


def test_an_html_body_at_the_ceiling_is_accepted(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    body = "<p>" + "x" * (module.MAX_HTML_BYTES - 3)
    response = TestClient(module.app).post("/audit/wcag", json={"html": body}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200


def test_the_mcp_tool_call_refuses_an_oversized_html_body(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _auth_spy(monkeypatch, module)
    body = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "audit_wcag", "arguments": {"html": "x" * (module.MAX_HTML_BYTES + 1)}}},
        headers={"X-API-Key": "test-key"},
    ).json()
    assert body["result"]["isError"] is True
    assert "limit is" in body["result"]["content"][0]["text"]
    assert calls == []


# --- The MCP paywall must be payable by the x402 MCP client, not merely readable ---
#
# The x402 MCP protocol (x402.mcp in the library, server and client) is not the
# HTTP 402 shape. A paywalled tool result is `isError: true` with a **v2**
# PaymentRequired in `structuredContent`; the client signs for `accepts[0]`
# and retries with the payload in `params._meta["x402/payment"]`; the receipt
# comes back in the result's `_meta["x402/payment-response"]`. This endpoint
# served the REST body (v1) as text and read payments only from HTTP headers
# -- which an MCP client cannot send -- so every conforming x402 MCP client
# was paywalled twice and gave up. Same fault as the unpayable REST 402 (#61),
# one transport over. These drive the real client library against the real
# endpoint, the same way the REST tests above do.


def _x402_core_client():
    """The x402 payment client itself (not the HTTP wrapper), which is what
    the x402 MCP client uses to sign a challenge it read out of a tool result."""
    from eth_account import Account
    from x402 import max_amount, x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client

    account = Account.from_key("0x" + "1" * 63 + "2")
    client = x402ClientSync()
    register_exact_evm_client(
        client, EthAccountSigner(account), policies=[max_amount(1_000_000)]
    )
    return client, account


def _mcp_result_object(result: dict):
    """The library's own view of a tool result, built from the wire JSON."""
    from x402.mcp.types import MCPToolResult

    return MCPToolResult(
        content=result.get("content") or [],
        is_error=bool(result.get("isError")),
        meta=result.get("_meta"),
        structured_content=result.get("structuredContent"),
    )


def _tools_call(name, arguments, meta=None, request_id=1):
    params = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def test_mcp_paywall_is_the_v2_challenge_the_x402_mcp_client_pays(monkeypatch, load_main_fresh):
    from fastapi.testclient import TestClient
    from x402.mcp.utils import extract_payment_required_from_result
    from x402.schemas import PaymentRequired

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_mcp_v2_paywall")
    result = TestClient(module.app).post(
        "/mcp", json=_tools_call("audit_bundle", {"url": "https://example.com"})
    ).json()["result"]
    assert result["isError"] is True

    # The preferred path: structuredContent, parsed by the library's own
    # extractor -- the exact call the x402 MCP client makes before signing.
    parsed = extract_payment_required_from_result(_mcp_result_object(result))
    assert isinstance(parsed, PaymentRequired), "the MCP paywall is not a v2 challenge"
    requirement = parsed.accepts[0]
    assert requirement.scheme == "exact"
    assert requirement.network == "eip155:8453"
    assert requirement.pay_to.lower() == _X402_TEST_PAY_TO.lower()
    assert requirement.amount == "150000", "bundle must be priced at $0.15 in atomic USDC"
    assert parsed.resource.url == f"{module.PUBLIC_BASE_URL}/mcp", (
        "the resource an agent connects to is the MCP endpoint"
    )
    assert parsed.extensions["bazaar"]["info"]["input"]["type"] == "mcp"
    assert parsed.extensions["bazaar"]["info"]["input"]["toolName"] == "audit_bundle"

    # The fallback path: a client that ignores structuredContent parses the
    # text, and must find the same challenge there.
    text_only = dict(result, structuredContent=None)
    fallback = extract_payment_required_from_result(_mcp_result_object(text_only))
    assert fallback is not None and fallback.accepts[0].amount == "150000"

    # The v2 object is the same challenge the HTTP path sends in its header:
    # one builder, so the two transports cannot quote different terms.
    http = TestClient(module.app).post("/audit/bundle", json={"url": "https://example.com"})
    from x402.http.utils import decode_payment_required_header

    header = decode_payment_required_header(http.headers["PAYMENT-REQUIRED"])
    assert header.accepts[0].model_dump() == requirement.model_dump()

    # The LLM-facing fields survive beside the machine-readable ones.
    sc = result["structuredContent"]
    assert sc["price_usd"] == 0.15
    assert "Payment required" in sc["message"]
    assert "docs" in sc

    # And a real client can sign it.
    client, _ = _x402_core_client()
    payload = client.create_payment_payload(parsed)
    assert payload.payload["authorization"]["value"] == "150000"


def test_an_x402_mcp_client_pays_in_meta_and_gets_its_receipt_in_meta(monkeypatch, load_main_fresh):
    """The whole MCP round trip, with the real client signing what the
    endpoint served: the payment arrives in `_meta`, reaches the ONE verify
    path in the header form it expects, and the settlement comes back where
    the x402 MCP client reads it."""
    from fastapi.testclient import TestClient
    from x402.http.utils import decode_payment_signature_header
    from x402.mcp.utils import (
        extract_payment_required_from_result,
        extract_payment_response_from_meta,
    )
    from x402.schemas import SettleResponse

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_mcp_meta_payment")
    client = TestClient(module.app)
    call = _tools_call("audit_wcag", {"url": "https://example.com"})

    challenge = extract_payment_required_from_result(
        _mcp_result_object(client.post("/mcp", json=call).json()["result"])
    )
    x402_client, account = _x402_core_client()
    # Exactly what x402.mcp's client puts in _meta: model_dump(by_alias=True).
    payload_dict = x402_client.create_payment_payload(challenge).model_dump(by_alias=True)

    seen = []
    settlement = SettleResponse(
        success=True, transaction="0x" + "ab" * 32, network="eip155:8453", payer=account.address
    )

    def _verify(header, price=None, **kw):
        seen.append((header, price))
        return module.x402_payments.PendingPayment(None, None, price)

    def _settle(pending):
        pending.settle_result = settlement
        return True

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    paid = client.post(
        "/mcp", json=_tools_call("audit_wcag", {"url": "https://example.com"},
                                 meta={"x402/payment": payload_dict}, request_id=2)
    )
    result = paid.json()["result"]
    assert result["isError"] is False, result
    assert '"pass": true' in result["content"][0]["text"]

    # The payment from _meta reached verify, priced for THIS tool, as the
    # same signed authorization the client produced.
    assert len(seen) == 1
    header, price = seen[0]
    assert price == "$0.05"
    decoded = decode_payment_signature_header(header)
    assert decoded.payload["signature"] == payload_dict["payload"]["signature"]
    assert decoded.payload["authorization"]["from"].lower() == account.address.lower()

    # The receipt, where the x402 MCP client reads it.
    receipt = extract_payment_response_from_meta(_mcp_result_object(result))
    assert receipt is not None and receipt.transaction == settlement.transaction
    assert receipt.payer.lower() == account.address.lower()
    # ...and still in the HTTP header for clients that can see headers.
    assert paid.headers.get("PAYMENT-RESPONSE")


def test_an_x402_mcp_client_pays_for_the_worker_tool_in_meta_and_is_receipted(monkeypatch, load_main_fresh):
    """The same round trip for the worker-backed tool: the paywall the
    endpoint serves for hubvibe_predictive_probability_engine is a v2
    challenge at the worker's price, the real client signs it, the payment
    in `_meta` reaches the one verify path priced for THIS tool, the job
    runs through the worker route (ledger row, receipt_url in the body) and
    the settlement comes back in `_meta` and the header."""
    from fastapi.testclient import TestClient
    from x402.http.utils import decode_payment_signature_header
    from x402.mcp.utils import (
        extract_payment_required_from_result,
        extract_payment_response_from_meta,
    )
    from x402.schemas import PaymentRequired, SettleResponse

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_mcp_worker_tool")
    module.workers.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for)
    client = TestClient(module.app)
    tool = "hubvibe_predictive_probability_engine"
    arguments = {"points": [[1, 2.1], [2, 3.9], [3, 6.2], [4, 7.8], [5, 10.1]], "predict_x": [6]}

    unpaid = client.post("/mcp", json=_tools_call(tool, arguments)).json()["result"]
    assert unpaid["isError"] is True
    challenge = extract_payment_required_from_result(_mcp_result_object(unpaid))
    assert isinstance(challenge, PaymentRequired)
    assert challenge.accepts[0].amount == "500000", "priced at the worker's $0.50"
    assert challenge.resource.url == f"{module.PUBLIC_BASE_URL}/mcp"
    assert challenge.extensions["bazaar"]["info"]["input"]["toolName"] == tool
    assert "points" in challenge.extensions["bazaar"]["info"]["input"]["example"]
    assert unpaid["structuredContent"]["price_usd"] == 0.50

    x402_client, account = _x402_core_client()
    payload_dict = x402_client.create_payment_payload(challenge).model_dump(by_alias=True)
    seen = []
    settlement = SettleResponse(
        success=True, transaction="0x" + "cd" * 32, network="eip155:8453", payer=account.address
    )

    def _verify(header, price=None, **kw):
        seen.append((header, price))
        return module.x402_payments.PendingPayment(None, None, price)

    def _settle(pending):
        pending.settle_result = settlement
        return True

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)

    paid = client.post(
        "/mcp", json=_tools_call(tool, arguments, meta={"x402/payment": payload_dict}, request_id=2)
    )
    result = paid.json()["result"]
    assert result["isError"] is False, result
    body = result["structuredContent"]
    assert body["status"] == "ok" and body["worker"] == "stats.probability"
    assert body["price_usd"] == 0.50
    assert body["result"]["linear_regression"]["slope"] > 0
    assert body["result"]["prediction"][0]["x"] == 6.0
    assert client.get(body["receipt_url"]).json()["request"]["worker"] == "stats.probability"

    assert len(seen) == 1
    header, price = seen[0]
    assert price == "$0.50"
    decoded = decode_payment_signature_header(header)
    assert decoded.payload["signature"] == payload_dict["payload"]["signature"]

    receipt = extract_payment_response_from_meta(_mcp_result_object(result))
    assert receipt is not None and receipt.transaction == settlement.transaction
    assert paid.headers.get("PAYMENT-RESPONSE")


def test_a_broken_meta_payment_is_a_paywall_not_a_crash(monkeypatch, load_main_fresh):
    """Garbage in _meta must fall through to the ordinary paywall. A 500 here
    would tell a paying agent nothing it could act on."""
    from fastapi.testclient import TestClient

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_mcp_meta_garbage")
    client = TestClient(module.app)
    ran = []
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: ran.append(1))

    for meta in ({"x402/payment": "not a payment"}, {"x402/payment": 42}, "junk", {"x402/payment": {"x402Version": 2}}):
        response = client.post(
            "/mcp", json=_tools_call("audit_wcag", {"url": "https://example.com"}, meta=meta)
        )
        assert response.status_code == 200, meta
        result = response.json()["result"]
        assert result["isError"] is True, meta
        assert "accepts" in result["structuredContent"], meta
    assert ran == []


def test_the_mcp_paywall_offers_nothing_payable_when_x402_is_off(monkeypatch):
    """With no rail, the structured challenge must read as unpayable to the
    x402 MCP client (empty accepts -> no payment attempted), not as a v2
    challenge naming a recipient that cannot receive."""
    from fastapi.testclient import TestClient
    from x402.mcp.utils import extract_payment_required_from_result

    module = _load_main(monkeypatch, api_key=None)
    result = TestClient(module.app).post(
        "/mcp", json=_tools_call("audit_wcag", {"url": "https://example.com"})
    ).json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["accepts"] == []
    assert extract_payment_required_from_result(_mcp_result_object(result)) is None


def test_cors_exposes_every_header_a_paying_browser_agent_must_read(monkeypatch):
    """A browser strips unlisted response headers from cross-origin responses
    before script sees them. A browser-resident x402 client reads the v2
    challenge off PAYMENT-REQUIRED and its receipt off PAYMENT-RESPONSE; with
    only WWW-Authenticate exposed it saw neither -- silently downgraded to the
    v1 body at best, and never handed its transaction hash."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    response = TestClient(module.app).post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"Origin": "https://some-agent.example"},
    )
    assert response.status_code == 402
    exposed = {
        h.strip().upper()
        for h in response.headers.get("access-control-expose-headers", "").split(",")
    }
    for header in ("WWW-Authenticate", "PAYMENT-REQUIRED", "PAYMENT-RESPONSE",
                   "X-PAYMENT-RESPONSE", "Retry-After"):
        assert header.upper() in exposed, f"{header} is not readable cross-origin"


# --- The rate limiter must key on the caller, not on the platform's proxy ---
#
# request.client.host is the TCP peer. On Cloud Run that is the front-end
# proxy -- one address for every caller on earth -- so every unkeyed caller
# (every x402/MPP payer, every agent reading an unpaid 402) shared ONE bucket
# of RATE_LIMIT_PER_MINUTE per instance. Past ten unpaid reads a second, per
# instance, paying agents got 429s for traffic that was not theirs.


def _unpaid(client, forwarded_for=None):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    return client.post("/audit/wcag", json={"url": "https://example.com"}, headers=headers)


def test_unkeyed_callers_are_limited_per_forwarded_client_not_per_proxy(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module, "_audit_limiter", module._SlidingWindowLimiter(limit=1, window_seconds=60.0))
    client = TestClient(module.app)

    assert _unpaid(client, "203.0.113.10").status_code == 402
    # A different agent behind the same platform proxy is not throttled by
    # the first one's traffic...
    assert _unpaid(client, "203.0.113.11").status_code == 402
    # ...and the first agent's own second read is.
    assert _unpaid(client, "203.0.113.10").status_code == 429


def test_a_client_cannot_dodge_the_limiter_by_prepending_forwarded_addresses(monkeypatch):
    """Only the address the platform appended -- the LAST entry -- counts.
    Anything a client puts in front of it is its own claim, not evidence."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module, "_audit_limiter", module._SlidingWindowLimiter(limit=1, window_seconds=60.0))
    client = TestClient(module.app)

    assert _unpaid(client, "198.51.100.7, 203.0.113.10").status_code == 402
    assert _unpaid(client, "198.51.100.8, 203.0.113.10").status_code == 429


def test_without_a_forwarded_header_the_tcp_peer_is_the_limiter_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module, "_audit_limiter", module._SlidingWindowLimiter(limit=1, window_seconds=60.0))
    client = TestClient(module.app)
    assert _unpaid(client).status_code == 402
    assert _unpaid(client).status_code == 429


def test_client_ip_reads_the_platforms_entry_at_the_configured_depth(monkeypatch):
    from starlette.requests import Request

    module = _load_main(monkeypatch)

    def _request(forwarded=None, peer="10.0.0.9"):
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1234),
                        "method": "POST", "path": "/audit/wcag", "query_string": b""})

    assert module._client_ip(_request()) == "10.0.0.9"
    assert module._client_ip(_request("203.0.113.10")) == "203.0.113.10"
    assert module._client_ip(_request("1.1.1.1, 203.0.113.10")) == "203.0.113.10"
    # Behind an extra trusted hop (an external load balancer), one further back.
    monkeypatch.setattr(module, "RATE_LIMIT_PROXY_DEPTH", 2)
    assert module._client_ip(_request("1.1.1.1, 203.0.113.10, 35.0.0.1")) == "203.0.113.10"
    # Fewer entries than hops: use what is there rather than crash.
    assert module._client_ip(_request("203.0.113.10")) == "203.0.113.10"


def test_discovery_routes_never_queue_behind_the_audit_thread_pool(monkeypatch):
    """MAX_CONCURRENT_AUDITS caps anyio's thread pool, and every SYNC route
    runs in it. While the pool was full of Chromium contexts, /health,
    /.well-known/agent.json, /mcp.json and the MCP handshake all waited on a
    stranger's page load -- and a health probe that times out marks an
    instance unhealthy at exactly the moment it is earning. Discovery is pure
    CPU on static data; it runs on the event loop. Only the tool call, which
    runs a browser and waits on the facilitator, belongs in the pool."""
    import inspect

    module = _load_main(monkeypatch)
    for name in (
        "health_check", "landing_page", "llms_txt", "mcp_manifest", "agent_manifest",
        "robots_txt", "sitemap_xml", "favicon", "og_image", "mcp_streamable_http",
        "checkout_success_page", "checkout_cancel_page",
    ):
        assert inspect.iscoroutinefunction(getattr(module, name)), f"{name} would queue behind audits"
    assert not inspect.iscoroutinefunction(module._mcp_tools_call), (
        "the tool call runs Chromium; it must stay in the pool"
    )


# --- A delivered MCP result must be one the official MCP SDK will hand over ---
#
# Every tool advertises an outputSchema. The official MCP SDK client enforces
# the spec's consequence on every NON-error result: if the tool has an output
# schema and the result carries no structuredContent, or structuredContent
# does not validate against that schema, call_tool raises RuntimeError in the
# client -- after the call. On a paid call that is the worst order of events
# this node can produce: payment settled, audit run, result thrown away on
# delivery. Error results (the paywall) are not validated, which is why the
# v2 challenge may live in structuredContent there.


def _fake_page_response(url="https://example.com"):
    import httpx

    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8", "strict-transport-security": "max-age=1"},
        text="<!doctype html><html lang='en'><head><title>t</title>"
             "<meta name='description' content='d'></head><body><h1>h</h1></body></html>",
        request=httpx.Request("GET", url),
    )


def _axe_raw():
    return {"violations": [{"id": "image-alt", "impact": "critical", "help": "Images must have alternate text",
                            "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/image-alt",
                            "nodes": [{"target": ["img"]}, {"target": ["img.b"]}]}]}


def _stub_every_audit(monkeypatch, module):
    """The real audit functions on offline inputs wherever they can run
    offline, so the shapes validated are the shapes the routes emit."""
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: _axe_raw())
    monkeypatch.setattr(module.audits, "fetch_once", lambda url: _fake_page_response(url))
    performance = module.audits.performance_result_from_metrics(1200, 480_000, 37)
    monkeypatch.setattr(module.audits, "run_performance_audit", lambda url: performance)
    monkeypatch.setattr(module, "_run_axe_and_performance", lambda url: (_axe_raw(), performance))
    real_seo, real_security = module.audits.run_seo_audit, module.audits.run_security_audit
    monkeypatch.setattr(
        module.audits, "run_seo_audit",
        lambda html, url, response=None: real_seo(html, url, response=response or _fake_page_response(url)),
    )
    monkeypatch.setattr(
        module.audits, "run_security_audit",
        lambda url, response=None: real_security(url, response=response or _fake_page_response(url)),
    )


@pytest.mark.parametrize("tool_name", ["audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"])
def test_a_delivered_mcp_result_validates_against_the_output_schema_it_advertises(monkeypatch, tool_name):
    import json

    import jsonschema
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    _stub_every_audit(monkeypatch, module)
    client = TestClient(module.app)

    tool = next(t for t in _rpc(client, "tools/list").json()["result"]["tools"] if t["name"] == tool_name)
    result = client.post(
        "/mcp",
        json=_tools_call(tool_name, {"url": "https://example.com"}),
        headers={"X-API-Key": "test-key"},
    ).json()["result"]
    assert result["isError"] is False, result

    # The SDK's rule, exactly: an output schema without structured content is
    # a RuntimeError in the client; with it, the content must validate.
    assert "structuredContent" in result, (
        f"{tool_name} declares an outputSchema and returned no structuredContent -- "
        "the official MCP SDK raises on this after the payment has settled"
    )
    validator = jsonschema.validators.validator_for(tool["outputSchema"])
    validator.check_schema(tool["outputSchema"])
    validator(tool["outputSchema"]).validate(result["structuredContent"])
    # Same facts in both places: a client reading text must not see a
    # different audit than one reading structuredContent.
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    assert result["structuredContent"]["pass"] in (True, False)


def test_a_null_axe_impact_still_validates(monkeypatch):
    """`v.get("impact")` can be None. The schema must say so, or a strict
    client refuses a paid, delivered audit over a null it was never told
    about."""
    import jsonschema
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    raw = _axe_raw()
    raw["violations"][0]["impact"] = None
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: raw)
    client = TestClient(module.app)
    tool = next(t for t in _rpc(client, "tools/list").json()["result"]["tools"] if t["name"] == "audit_wcag")
    result = client.post(
        "/mcp", json=_tools_call("audit_wcag", {"html": "<p>x</p>"}), headers={"X-API-Key": "test-key"}
    ).json()["result"]
    jsonschema.validate(result["structuredContent"], tool["outputSchema"])


# --- 2026-09-06 audit: what a payer is TOLD, and what it costs to be wrong ---


def test_a_refused_payment_402_says_why_and_a_facilitator_outage_says_when(monkeypatch):
    """The x402 reference server re-issues its 402 with the facilitator's
    invalid_reason; the official client does not retry a second 402. A bare
    re-challenge left an agent nothing to act on, whether its wallet was
    empty or the facilitator was down."""
    from fastapi.testclient import TestClient

    from x402.http.utils import decode_payment_required_header

    _x402_env(monkeypatch)
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None, **kw: None)
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    client = TestClient(module.app)

    monkeypatch.setattr(
        module.x402_payments, "last_rejection", lambda: ("insufficient_funds", "wallet holds 0 USDC")
    )
    response = client.post("/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed"})
    assert response.status_code == 402
    body = response.json()
    assert body["error"] == "insufficient_funds"
    assert body["error_detail"] == "wallet holds 0 USDC"
    assert body["billed"] is False
    assert "Retry-After" not in response.headers, "an empty wallet is not a reason to retry"
    header = response.headers.get("payment-required")
    if header:
        assert decode_payment_required_header(header).error == "insufficient_funds"

    monkeypatch.setattr(
        module.x402_payments, "last_rejection",
        lambda: ("facilitator_unavailable", "the facilitator could not be reached; retry"),
    )
    response = client.post("/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed"})
    assert response.status_code == 402
    assert response.json()["error"] == "facilitator_unavailable"
    assert response.headers["Retry-After"] == "30"

    # A fresh, unpaid challenge is unchanged.
    monkeypatch.setattr(module.x402_payments, "last_rejection", lambda: None)
    response = client.post("/audit/wcag", json={"url": "https://example.com"})
    assert response.json()["error"] == "payment_required"
    assert "error_detail" not in response.json()


def test_the_mcp_paywall_carries_the_refusal_reason_too(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None, **kw: None)
    monkeypatch.setattr(module.x402_payments, "last_rejection", lambda: ("insufficient_funds", "empty"))
    result = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "audit_seo", "arguments": {"url": "https://example.com"}}},
        headers={"X-PAYMENT": "signed"},
    ).json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "insufficient_funds"


def test_a_pending_settlement_is_reported_as_pending_with_its_hash_not_as_free(monkeypatch):
    from fastapi.testclient import TestClient

    from x402.http.utils import decode_payment_response_header
    from x402.schemas import SettleResponse

    module = _load_main(monkeypatch)
    tx = "0x" + "ab" * 32
    sentinel = module.x402_payments.PendingPayment(None, None, "$0.05")

    def _settle(pending):
        pending.settle_state = "pending"
        pending.settle_result = SettleResponse(
            success=False, errorReason="settlement_pending", transaction=tx,
            network="eip155:8453", payer="0x" + "11" * 20,
        )
        return False

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None, **kw: sentinel)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed-payment"}
    )
    assert response.status_code == 200
    warning = response.json()["billing_warning"]
    assert "pending" in warning and tx in warning
    assert "not charged" not in warning
    assert decode_payment_response_header(response.headers["PAYMENT-RESPONSE"]).transaction == tx

    def _unknown(pending):
        pending.settle_state = "unknown"
        return False

    monkeypatch.setattr(module.x402_payments, "settle_sync", _unknown)
    warning = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed-payment"}
    ).json()["billing_warning"]
    assert "unknown" in warning and "do not re-pay" in warning

    def _refused(pending):
        pending.settle_state = "refused"
        return False

    monkeypatch.setattr(module.x402_payments, "settle_sync", _refused)
    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed-payment"}
    )
    # Only a definite refusal withholds: pending and unknown above may have
    # moved the money, so they deliver; refused delivers nothing and charges
    # nothing.
    assert response.status_code == 402
    assert response.json()["error"] == "settlement_refused"
    assert "PAYMENT-RESPONSE" not in response.headers


def test_oversized_bodies_are_refused_before_they_are_read(monkeypatch):
    """A 300 MB unpaid POST took the worker to ~1 GB RSS before any gate ran;
    three in flight exceed the container's memory limit and the box kills
    the node mid-audit. The cap applies before the body is buffered."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    huge = b'{"html": "' + b"x" * (module.MAX_REQUEST_BYTES + 1024) + b'"}'

    response = client.post("/audit/wcag", content=huge, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["billed"] is False
    assert response.json()["max_request_bytes"] == module.MAX_REQUEST_BYTES

    response = client.post("/mcp", content=huge, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600

    # Chunked, no Content-Length: counted as it streams, still refused.
    def _stream():
        for _ in range(6):
            yield b"x" * (1024 * 1024)

    response = client.post(
        "/audit/wcag", content=_stream(),
        headers={"Content-Type": "application/json", "X-API-Key": "test-key"},
    )
    assert response.status_code == 413
    # With no credential the price is answered before the body is read at
    # all -- even cheaper than the cap, and nothing is buffered either way.
    response = client.post("/audit/wcag", content=_stream(), headers={"Content-Type": "application/json"})
    assert response.status_code == 402

    # A legitimate 2 MiB html audit is under the cap and reaches the route.
    fine = b'{"html": "' + b"<p>x</p>" * (module.MAX_HTML_BYTES // 16) + b'"}'
    assert len(fine) < module.MAX_REQUEST_BYTES
    response = client.post("/audit/wcag", content=fine, headers={"Content-Type": "application/json"})
    assert response.status_code != 413


def test_mcp_rate_limit_is_a_wait_not_a_payment_challenge(monkeypatch):
    """An x402 MCP client answered with a challenge signs, retries once, gets
    the same challenge and reports its wallet refused. Over-limit is 'wait'."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module._audit_limiter, "check", lambda key: False)
    response = TestClient(module.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "audit_seo", "arguments": {"url": "https://example.com"}}},
    )
    import json as _json

    assert response.status_code == 200
    assert response.headers["Retry-After"] == "60"
    result = response.json()["result"]
    assert result["isError"] is True
    detail = _json.loads(result["content"][0]["text"])
    assert detail["error"] == "rate_limited"
    assert detail["retry_after_seconds"] == 60
    assert "accepts" not in detail
    assert "x402Version" not in detail


@pytest.mark.parametrize(
    "body,code",
    [
        ([], -32600),                                                   # a batch
        ("just a string", -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "audit_seo", "arguments": "https://example.com"}}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "audit_seo", "arguments": {"url": 5}}}, -32602),
    ],
)
def test_malformed_mcp_requests_get_jsonrpc_errors_not_500s(monkeypatch, body, code):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    response = TestClient(module.app).post("/mcp", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == code


def test_invalid_json_to_mcp_is_a_parse_error_in_jsonrpc_shape(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    response = TestClient(module.app).post(
        "/mcp", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0", "id": None,
        "error": {"code": -32700, "message": "Parse error: the body is not valid JSON"},
    }
    # Every other route keeps FastAPI's default validation shape for a caller
    # holding a credential (one without is priced first, see below).
    response = TestClient(module.app).post(
        "/audit/wcag", content=b"{not json",
        headers={"Content-Type": "application/json", "X-API-Key": "test-key"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_the_mcp_paywall_lists_only_rails_an_mcp_caller_can_use(monkeypatch):
    """MPP rails are paid through HTTP headers a tool result cannot carry."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    mpp_rail = {
        "protocol": "mpp", "method": "tempo", "intent": "charge", "asset": "usdc",
        "amount_minor_units": "30000", "send_via_header": "Authorization: Payment ...",
        "challenge_in": "WWW-Authenticate",
    }
    monkeypatch.setattr(module.mpp_payments, "accepts_entries", lambda price_usd: [dict(mpp_rail)])
    client = TestClient(module.app)

    rest = client.post("/audit/seo", json={"url": "https://example.com"}).json()
    assert any(r["protocol"] == "mpp" for r in rest["other_rails"]), "fixture: REST must offer MPP"

    mcp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "audit_seo", "arguments": {"url": "https://example.com"}}},
    ).json()["result"]["structuredContent"]
    assert not any(r.get("protocol") == "mpp" for r in mcp.get("other_rails", []))


def test_a_key_doorway_is_named_only_where_a_key_can_be_bought(monkeypatch):
    """/billing/checkout answers 501 without Stripe billing. Naming it on the
    402 and in agent.json sent an agent's operator to a dead end."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    assert module.billing.is_configured() is False

    body = client.post("/audit", json={"url": "https://example.com"}).json()
    assert body["alternative"]["header"] == "X-API-Key"
    assert "get_one" not in body["alternative"]
    assert "human_plans" not in client.get("/.well-known/agent.json").json()["pricing"]

    # Even with billing configured: the tiers are retired (2026-09-06), so
    # no surface may point at a checkout that refuses.
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    body = client.post("/audit", json={"url": "https://example.com"}).json()
    assert "get_one" not in body["alternative"]
    assert "/billing/checkout" not in client.get("/.well-known/agent.json").text


def test_a_402_with_no_live_rail_says_so_instead_of_pointing_at_an_empty_list(monkeypatch):
    """With every rail down, `alternative` used to tell the caller a key is
    "bought with the MPP top-up rail in `other_rails`" while `other_rails` was
    []. That is the same dead end the get_one URL was removed for, one level
    down: a caller is sent to look somewhere that holds nothing, reads it as a
    broken service, and retries a call no retry can buy.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    body = client.post("/audit", json={"url": "https://example.com"}).json()
    assert body["accepts"] == [] and body["other_rails"] == [], "fixture is not a no-rail node"

    detail = body["alternative"]["detail"]
    assert "no payment rail is live" in detail.lower(), detail
    # Naming the empty lists to explain is fine; sending the caller there to
    # BUY something is the dead end.
    assert "bought with" not in detail.lower(), "still offers a purchase down an empty rail"
    # The caller must learn that retrying is pointless, and that an already
    # issued key is still good -- both are actionable, unlike "go look there".
    assert "no retry" in detail.lower()
    assert "still spends" in detail.lower()


def test_openapi_prices_the_x402_rail(monkeypatch, load_main_fresh):
    """On an x402-only deploy every paid route read as free in openapi.json
    while the 402, agent.json, llms.txt and mcp.json all priced it."""
    from fastapi.testclient import TestClient

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_main_openapi_x402")
    doc = TestClient(module.app).get("/openapi.json").json()

    for path, amount in (("/audit/wcag", "50000"), ("/audit/bundle", "150000"), ("/audit", "50000")):
        operation = doc["paths"][path]["post"]
        offers = operation["x-payment-info"]["offers"]
        x402 = [o for o in offers if o["method"] == "x402"]
        assert x402, f"{path} carries no x402 offer"
        assert x402[0]["amount"] == amount
        assert x402[0]["payTo"] == _X402_TEST_PAY_TO
        assert "402" in operation["responses"]


def test_a_body_with_nothing_to_audit_is_refused_before_payment(monkeypatch):
    """Checked after the facilitator, this 400 burned a verify and the
    signature's nonce, so the payer's corrected retry read as a replay."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = {"verify": 0}

    def _verify(header, price=None, **kw):
        calls["verify"] += 1
        return None

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    client = TestClient(module.app)
    for path in ("/audit", "/audit/wcag", "/audit/seo"):
        response = client.post(path, json={}, headers={"X-PAYMENT": "signed"})
        assert response.status_code == 400, path
        assert response.json()["billed"] is False
        assert "Nothing was charged" in response.json()["detail"]
    assert calls["verify"] == 0, "a body with nothing to audit reached the facilitator"


def test_an_unpaid_probe_gets_the_price_before_anything_else(monkeypatch):
    """The indexers that list this node (uvd-bazaar-health, PayAI's monitor,
    x402lens, x402-directory-verifier, allow402-quote) probe with GET, HEAD
    or an empty POST and no credential. For a day they got 405/422/400 and
    25 requests in total ever saw the 402. A crawler that never sees the
    price cannot list it: with no credential, the challenge comes first."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    touched = []
    monkeypatch.setattr(
        module.x402_payments, "verify_only_sync", lambda *a, **k: touched.append("verify")
    )
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: touched.append("audit"))
    client = TestClient(module.app)
    probes = [
        ("POST", {"json": {}}),
        ("POST", {"content": b"{not json", "headers": {"Content-Type": "application/json"}}),
        ("POST", {"json": {"url": "http://127.0.0.1/"}}),
        ("POST", {"json": {"html": "<p>" + "x" * (module.MAX_HTML_BYTES + 1)}}),
        ("GET", {}),
        ("HEAD", {}),
    ]
    # Driven off the catalog, the one source of price, so a probe is quoted
    # exactly what the handler would charge -- the two used to be separate
    # tables and could silently disagree.
    priced_paths = [e["path"] for e in module._CATALOG] + list(module._CATALOG_ALIASES)
    for path in priced_paths:
        price = module._price_of(path)
        for method, kwargs in probes:
            response = client.request(method, path, **kwargs)
            assert response.status_code == 402, (method, path, response.status_code, response.text[:200])
            assert response.headers["cache-control"] == "no-store"
            if method == "HEAD":
                assert response.content == b""
            else:
                assert response.json()["price_usd"] == price, (method, path)
            if method != "POST":
                assert response.headers["allow"] == "POST"
    assert touched == [], "an unpaid probe reached the facilitator or ran an audit"
    # Only paid routes are challenged; the discovery surfaces stay free.
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/.well-known/agent.json").status_code == 200


def test_a_credentialed_bad_request_is_still_refused_before_payment(monkeypatch):
    """Pricing unpaid probes must not reorder anything for a payer: a signed
    body with nothing to audit is refused for free BEFORE the facilitator
    sees it (the test above this one), and a credentialed GET stays a 405 --
    the price is not the answer to a caller that already holds a way to pay."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    assert client.post("/audit/wcag", json={}, headers={"X-API-Key": "test-key"}).status_code == 400
    assert client.post("/audit/security", json={}, headers={"X-API-Key": "test-key"}).status_code == 422
    assert client.get("/audit/wcag", headers={"X-API-Key": "test-key"}).status_code == 405
    assert client.get("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}).status_code == 405


def test_a_failed_audit_says_it_was_not_charged(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _x402_caller(monkeypatch, module)

    def _boom(*a, **k):
        raise RuntimeError("target site unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)
    response = TestClient(module.app).post(
        "/audit/wcag", json={"url": "https://example.com"}, headers={"X-PAYMENT": "signed-payment"}
    )
    assert response.status_code == 502
    assert response.json()["billed"] is False
    assert "Nothing was charged" in response.json()["detail"]


def test_the_revenue_line_survives_the_default_log_level(monkeypatch):
    """"x402 SETTLED" is INFO. With the root logger at Python's default
    (WARNING) the shipped container logged none of them -- the rehearsal
    paid the image three times and `docker logs` showed zero."""
    import logging

    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        _load_main(monkeypatch)
        assert root.isEnabledFor(logging.INFO), "INFO lines are still dropped"
    finally:
        root.setLevel(previous)


# --- 2026-09-06: the human tiers are retired ---------------------------------


def test_no_human_tier_is_advertised_on_any_served_surface(monkeypatch):
    """Owner's call: nobody pays $79 a month when a scan is cents. Every
    surface a buyer or an agent reads must say per-call only."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    client = TestClient(module.app)

    manifest = client.get("/.well-known/agent.json").json()
    assert "human_plans" not in manifest["pricing"]
    assert "stripe_api_key" not in manifest["payment"]["methods"]
    assert module.billing.human_plans_live() == []

    for path in ("/", "/llms.txt"):
        text = client.get(path).text
        for price in ("$29.99", "$79", "$249", "per site watched", "watched per site",
                      "plan-btn", "human plan"):
            assert price not in text, f"{path} still shows {price!r}"

    challenge = client.post("/audit/wcag", json={"url": "https://example.com"}).json()
    assert "human plan" not in challenge["alternative"]["detail"]
    assert not any(r["protocol"] == "api_key" for r in challenge["other_rails"])

    docs_page = (REPO_ROOT / "docs" / "index.html").read_text()
    assert "$249" not in docs_page and "Plans" not in docs_page


def test_checkout_refuses_a_plan_now_that_the_tiers_are_retired(monkeypatch, load_main_fresh):
    """A configured Price ID for a retired plan is not an offer."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_AGENCY", "price_agency")
    monkeypatch.setenv("STRIPE_PRICE_ONEOFF_REPORT", "price_report")
    module = load_main_fresh("wcag_audit_main_retired_plans")
    client = TestClient(module.app)

    assert module.billing.plan_available("pro") is False
    assert module.billing.oneoff_report_available() is False
    response = client.post("/billing/checkout", json={"email": "b@example.com", "plan": "pro"})
    assert response.status_code == 400
    assert "retired" in response.json()["detail"]
    response = client.post("/billing/report", json={"email": "b@example.com", "url": "https://example.com"})
    assert response.status_code == 501


def test_the_settlement_failure_says_WHY_on_the_response(monkeypatch):
    """The node is the only party that knows why a settle failed.

    The payer used to see a 200 with an audit in it, and now sees the 402
    that withholds it; the operator has to be logged into the box to read
    the log. On 2026-09-08 that cost the owner a night
    of grepping for a one-line answer the node already had in hand. "The
    facilitator refused it: insufficient_funds" and "ReadTimeout" call for
    completely different next moves, and one generic sentence for both is
    the defect -- a machine client cannot tell whether retrying is safe.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module, settle_ok=False)
    real_settle = module.x402_payments.settle_sync

    def _settle_with_reason(pending):
        result = real_settle(pending)
        pending.settle_state = "refused"
        pending.settle_error = "the facilitator refused it: insufficient_funds"
        return result

    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle_with_reason)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    response = TestClient(module.app).post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert calls["settled"] == 1
    # A refusal withholds the audit (see _settlement_refused); the reason
    # rides the 402 that takes its place.
    assert response.status_code == 402
    detail = response.json()["error_detail"]
    assert "Nothing was charged" in detail
    assert "insufficient_funds" in detail, (
        "the reason is still only in the log, where the payer cannot see it"
    )


def test_an_unknown_settlement_says_WHY_too(monkeypatch):
    """Unknown is the one a payer must not guess about: re-running can pay
    twice. Whatever the node knows about why goes on the body."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _x402_caller(monkeypatch, module, settle_ok=False)
    real_settle = module.x402_payments.settle_sync

    def _settle_unknown(pending):
        result = real_settle(pending)
        pending.settle_state = "unknown"
        pending.settle_error = "ReadTimeout: timed out"
        return result

    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle_unknown)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    warning = TestClient(module.app).post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    ).json()["billing_warning"]

    assert "unknown" in warning
    assert "do not re-pay" in warning
    assert "ReadTimeout" in warning
# --- health means "can this node actually serve an audit" ------------------


def test_health_degrades_when_the_browser_is_missing(monkeypatch):
    """/health returned a constant, so it could only tell "uvicorn is up" from
    "the box is down". The failure that costs money sits between: Chromium
    absent or at an unexpected revision makes every paid audit 502 while the
    container reports itself healthy, so nothing restarts and nobody is paged.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "_BROWSER_PATH", "/nonexistent/chrome", raising=False)
    monkeypatch.setattr(module, "_BROWSER_PATH_RESOLVED", True, raising=False)
    client = TestClient(module.app)

    response = client.get("/health")
    assert response.status_code == 503, "a node that cannot audit reported itself healthy"
    body = response.json()
    assert body["status"] == "degraded"
    assert "chromium missing" in body["browser"]["detail"]


def test_health_fails_open_when_the_browser_cannot_be_determined(monkeypatch):
    """A probe that cannot tell must report ok: a false 503 restart-loops a
    node that is serving fine, which is worse than the gap it closes."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "_BROWSER_PATH", None, raising=False)
    monkeypatch.setattr(module, "_BROWSER_PATH_RESOLVED", True, raising=False)
    client = TestClient(module.app)

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["browser"]["detail"] == "undetermined"


# --- a charged top-up that cannot mint a key must say so -------------------


def test_a_charged_topup_that_cannot_mint_a_key_tells_the_payer(monkeypatch):
    """settle_topup_sync returns cents only after Stripe confirmed, so by the
    time the mint runs the money has moved. Swallowing the failure meant the
    payer was charged, got no key, and nothing recorded that credit was owed --
    on a rail whose whole promise is that a machine can pay without a human.
    """
    module = _load_main(monkeypatch)

    auth = module.AuthContext(
        stripe_billable=False,
        payment_method="mpp-topup",
        issued_key=None,
        credit_owed="paid 50 cents but the prepaid key could not be issued; 47 cents of credit is owed",
    )
    result = {"status": "ok", "pass": True}
    module._attach_issued_key(result, auth)

    assert "billing_warning" in result, "the payer was never told the charge went through"
    assert "47 cents of credit is owed" in result["billing_warning"]
    assert result["billed"] is True, "money moved; the response must not imply otherwise"


def test_a_successful_topup_carries_no_owed_warning(monkeypatch):
    module = _load_main(monkeypatch)
    auth = module.AuthContext(
        stripe_billable=False, payment_method="mpp-topup", issued_key="hv_abc"
    )
    result = {"status": "ok"}
    module._attach_issued_key(result, auth)
    assert "billing_warning" not in result
    assert result["api_key"] == "hv_abc"


# --- axe must reach iframes, or say it could not ---------------------------


class _FakeFrame:
    def __init__(self, name, refuses=False):
        self.name = name
        self.refuses = refuses
        self.injected = False

    def evaluate(self, script):
        if self.refuses:
            raise RuntimeError("cross-origin frame refused injection")
        self.injected = True
        return None


class _FramedPage:
    """A page whose main frame carries two children, one hostile."""

    def __init__(self):
        self.frames = [_FakeFrame("main"), _FakeFrame("widget"), _FakeFrame("ad", refuses=True)]


def test_axe_is_injected_into_child_frames_before_running(monkeypatch):
    """axe-playwright-python injects with a single page.evaluate, which reaches
    the main frame only. axe's cross-frame protocol needs the bundle present in
    every frame it is asked to reach, so violations inside an iframe -- a cookie
    banner, an embedded booking widget, a payment form -- were absent from the
    result rather than reported. The page came back cleaner than it is.
    """
    module = _load_main(monkeypatch)
    page = _FramedPage()

    monkeypatch.setattr(module._axe, "axe_script", "AXE_BUNDLE", raising=False)
    monkeypatch.setattr(
        module._axe, "run", lambda p, options=None: type("R", (), {"response": {"violations": []}})()
    )

    result = module._run_axe_all_frames(page)

    assert page.frames[1].injected, "the child frame never received axe; its violations are invisible"
    assert not page.frames[0].injected, "the main frame is axe.run's own job, not ours"
    assert result["frames_unreachable"] == 1, "a frame that refused injection was not disclosed"
    assert result["frames_audited"] == 2


def test_a_page_with_no_iframes_reports_one_frame_audited(monkeypatch):
    module = _load_main(monkeypatch)
    page = type("P", (), {"frames": [_FakeFrame("main")]})()
    monkeypatch.setattr(module._axe, "axe_script", "AXE_BUNDLE", raising=False)
    monkeypatch.setattr(
        module._axe, "run", lambda p, options=None: type("R", (), {"response": {"violations": []}})()
    )
    result = module._run_axe_all_frames(page)
    assert result["frames_audited"] == 1
    assert result["frames_unreachable"] == 0


def test_no_audit_path_runs_axe_without_the_frame_pass():
    """The frame-aware runner only helps where it is actually called. Both
    browser audit paths -- /audit/wcag and the bundle's combined load -- must go
    through it, or one of them silently goes back to main-frame-only results.
    """
    source = (REPO_ROOT / "wcag-audit-engine" / "app" / "main.py").read_text()
    runner_start = source.index("def _run_axe_all_frames(")
    runner_end = source.index("\ndef ", runner_start + 1)
    inside = source[runner_start:runner_end]
    outside = source[:runner_start] + source[runner_end:]

    assert "_axe.run(" in inside, "the frame-aware runner no longer runs axe at all"
    assert "_axe.run(" not in outside, (
        "an audit path calls _axe.run directly, so iframe violations go unreported there"
    )


# --- a refused settle withholds the MCP result too --------------------------


def test_a_refused_settlement_over_mcp_re_issues_the_paywall_not_the_audit(monkeypatch, load_main_fresh):
    """Same rule as the REST routes, in the shape the x402 MCP client reads:
    a settle the facilitator refuses answers with the v2 challenge in an
    isError result, carrying the reason, and no audit."""
    from fastapi.testclient import TestClient
    from x402.mcp.utils import extract_payment_required_from_result
    from x402.schemas import PaymentRequired

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_mcp_refused_settle")
    client = TestClient(module.app)
    call = _tools_call("audit_wcag", {"url": "https://example.com"})

    challenge = extract_payment_required_from_result(
        _mcp_result_object(client.post("/mcp", json=call).json()["result"])
    )
    x402_client, _account = _x402_core_client()
    payload_dict = x402_client.create_payment_payload(challenge).model_dump(by_alias=True)

    settles = []

    def _verify(header, price=None, **kw):
        return module.x402_payments.PendingPayment(None, None, price)

    def _settle(pending):
        settles.append(pending)
        pending.settle_state = "refused"
        pending.settle_error = "the facilitator refused it: insufficient_funds"
        return False

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    result = client.post(
        "/mcp", json=_tools_call("audit_wcag", {"url": "https://example.com"},
                                 meta={"x402/payment": payload_dict}, request_id=2)
    ).json()["result"]

    assert len(settles) == 1
    assert result["isError"] is True, "a refused settle delivered the MCP audit anyway"
    assert '"pass"' not in result["content"][0]["text"], "the audit leaked on a refusal"
    structured = result["structuredContent"]
    assert structured["error"] == "settlement_refused"
    assert "insufficient_funds" in structured["error_detail"]
    assert "Nothing was charged" in structured["error_detail"]
    assert "_meta" not in result, "no settlement, no receipt"
    parsed = extract_payment_required_from_result(_mcp_result_object(result))
    assert isinstance(parsed, PaymentRequired), "the re-challenge is not payable by the MCP client"
