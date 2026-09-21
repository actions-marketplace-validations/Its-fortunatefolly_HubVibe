"""Payment safety for the worker network.

The rules under test are the ones that cost real money if they are wrong:

  * a worker settles through the CORE's gate, not a second implementation
  * a job that failed is never billed
  * a retry with the same Idempotency-Key never charges twice
  * a bad body is refused BEFORE the payment instrument is touched
  * nothing a worker does can change what an audit charges

The audit routes' own behaviour is covered by the existing suite and is not
re-tested here; what IS tested is that adding workers left it alone.
"""

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
    """A fresh app with x402 configured and the ledger pointed at tmp."""
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()

    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)

    # Re-bind the router to THIS app instance.
    #
    # The worker package is a singleton, but the wider suite loads main.py
    # many times (load_main_fresh), and every load re-runs the mount block at
    # the end of main.py -- so whichever module loaded LAST owns the router's
    # gate functions. Run alone, that is this module and everything passes;
    # run after test_wcag_audit_engine.py, the router still points at a
    # different app, and a test that stubs _bill here watches a stub that is
    # never called. Binding explicitly makes these tests order-independent.
    #
    # Production loads main.py exactly once, so this is a test-isolation
    # concern rather than a behaviour this fixes.
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill,
        deliver=module._deliver,
        failed_response=module._failed_audit_response,
        with_page=module.browser_pool.with_page,
        goto_guarded=getattr(module.audits, "goto_guarded", None),
    )
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _stub_skill(app_module, monkeypatch, name, fn):
    """Replace one worker's implementation, so payment behaviour can be tested
    without depending on a live provider."""
    registry = dict(W.router.REGISTRY)
    registry[name] = fn
    monkeypatch.setattr(W.router, "REGISTRY", registry)


# --- the core guarantee -----------------------------------------------------

_PAYMENT_MODULES = {"x402_payments", "mpp_payments", "billing", "x402", "stripe", "cdp"}


def _imported_names(path: Path) -> set:
    """Every module name this file imports, from the AST.

    Deliberately NOT a text search: these modules discuss the payment layer at
    length in their docstrings, and a grep would fail on the prose while
    missing an import written as `importlib.import_module("x402_payments")`.
    """
    import ast

    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.Call):
            # importlib.import_module("...") / __import__("...")
            target = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if target in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value.split(".")[0])
    return names


def test_no_worker_module_imports_the_payment_layer():
    """If this ever fails, there are two payment implementations in the service
    and one of them is not the one that has been taking real money."""
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        overlap = _imported_names(path) & _PAYMENT_MODULES
        if overlap:
            offenders[path.name] = sorted(overlap)
    assert not offenders, f"worker modules reaching into payments: {offenders}"


def test_the_router_settles_nothing_itself():
    """The router may CALL the injected gate, but must define no settlement of
    its own -- no facilitator call, no payment construction."""
    import ast

    tree = ast.parse((PKG / "router.py").read_text())
    defined = {node.name for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in defined:
        assert "settle" not in name.lower(), f"router defines {name}"
        assert "payment_required" not in name.lower(), f"router defines {name}"
    assert not (_imported_names(PKG / "router.py") & _PAYMENT_MODULES)


# --- what the buyer sees before paying --------------------------------------

def test_unpaid_worker_call_returns_a_payable_402_with_its_own_price(client):
    response = client.post("/work/market/quote", json={"product_id": "BTC-USD"})
    assert response.status_code == 402
    body = response.json()
    assert body["price_usd"] == 0.02
    entry = body["accepts"][0]
    assert entry["payTo"] == TEST_PAY_TO
    assert entry["maxAmountRequired"] == "20000"  # $0.02 in USDC atomic units
    assert entry["maxTimeoutSeconds"] == 300


def test_every_sellable_worker_route_carries_bazaar_discovery_data(client):
    """Same rule the audits are held to: a paid route with no Bazaar record is
    payable but invisible to capability search, which from the inside looks
    exactly like nobody wanting to buy it.

    Only LIVE workers are checked, because an unavailable one is deliberately
    not for sale here and answers 503 rather than a 402."""
    live = W.catalog.live()
    assert live, "no workers are available in this environment -- nothing tested"
    missing = []
    for worker in live:
        body = client.post(worker.path, json={}).json()
        if "bazaar" not in (body.get("extensions") or {}):
            missing.append(worker.path)
    assert not missing, f"no Bazaar discovery data on: {missing}"


def test_the_402_describes_the_worker_the_caller_asked_for(client):
    """A worker's challenge must describe that worker, not fall back to the
    audits' description -- it is the only prose an agent's operator sees
    before authorising a signature."""
    body = client.post("/work/market/quote", json={"product_id": "BTC-USD"}).json()
    description = body["accepts"][0]["description"]
    assert "site audit" not in description.lower()
    assert "price" in description.lower() or "coinbase" in description.lower()


def test_worker_prices_reach_the_challenge_from_the_worker_catalog(client):
    for worker in W.catalog.live():
        body = client.post(worker.path, json={}).json()
        assert body["price_usd"] == worker.price_usd, worker.name


# --- a bad request costs nothing --------------------------------------------

def test_missing_required_field_is_refused_before_payment_is_read(client):
    """Checked after the facilitator, a 400 burns the payer's nonce and their
    corrected retry is then refused as a replay."""
    response = client.post("/work/chain/address",
                           headers={"X-API-Key": "test-key"}, json={})
    assert response.status_code == 400
    body = response.json()
    assert body["billed"] is False
    assert body["reason"] == "invalid_request"
    assert "input_schema" in body


def test_a_json_body_that_is_not_an_object_is_refused(client):
    response = client.post(
        "/work/llm/analyze",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
        content=b"[1,2,3]")
    assert response.status_code == 400
    assert response.json()["billed"] is False


def test_invalid_input_inside_the_skill_is_a_400_not_a_502(client):
    response = client.post("/work/chain/address",
                           headers={"X-API-Key": "test-key"},
                           json={"address": "not-an-address"})
    assert response.status_code == 400
    assert response.json()["billed"] is False


# --- a failed job is never billed -------------------------------------------

def test_a_provider_failure_returns_502_and_bills_nothing(app_module, client, monkeypatch):
    async def always_fails(ctx, payload):
        raise W.runtime.TransientProviderError("provider is down", reason="provider_down")

    _stub_skill(app_module, monkeypatch, "market.quote", always_fails)
    billed = []
    monkeypatch.setattr(app_module, "_bill",
                        lambda auth, price_usd: billed.append(price_usd))
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                           json={"product_id": "BTC-USD"})
    assert response.status_code == 502
    assert response.json()["billed"] is False
    assert billed == [], "a job that produced no result must never be billed"
    # The test client forgives a stale Content-Length; uvicorn does not, and
    # a copied one made every failed worker call die mid-response in prod.
    assert int(response.headers["content-length"]) == len(response.content)


def test_unconfigured_provider_is_503_not_502(app_module, client, monkeypatch):
    """"We cannot do this here" is a different answer from "we tried and it
    broke", and an autonomous caller routes differently on each."""
    async def unconfigured(ctx, payload):
        raise W.runtime.ProviderUnavailable("no credential on this deployment")

    _stub_skill(app_module, monkeypatch, "market.quote", unconfigured)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)
    response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                           json={"product_id": "BTC-USD"})
    assert response.status_code == 503
    assert response.json()["billed"] is False


# --- idempotency: a retry must not pay twice --------------------------------

def test_same_idempotency_key_returns_the_stored_result_and_is_not_billed(
        app_module, client, monkeypatch):
    runs = []

    async def counted(ctx, payload):
        runs.append(1)
        return {"ran": len(runs)}

    _stub_skill(app_module, monkeypatch, "market.quote", counted)
    billed = []
    monkeypatch.setattr(app_module, "_bill",
                        lambda auth, price_usd: billed.append(price_usd) or None)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    headers = {"X-API-Key": "test-key", "Idempotency-Key": "job-123"}
    first = client.post("/work/market/quote", headers=headers,
                        json={"product_id": "BTC-USD"})
    second = client.post("/work/market/quote", headers=headers,
                         json={"product_id": "BTC-USD"})

    assert first.status_code == 200 and second.status_code == 200
    assert len(runs) == 1, "the job must run once, not once per retry"
    assert second.json()["idempotent_replay"] is True
    assert second.json()["billed"] is False
    assert len(billed) == 1, "the duplicate must not reach the billing call"


def test_a_failed_job_releases_its_idempotency_key_for_a_genuine_retry(
        app_module, client, monkeypatch):
    """The failed job was not billed, so the caller is entitled to retry the
    same key. Holding it would turn one outage into a poisoned request id."""
    calls = {"n": 0}

    async def fails_then_works(ctx, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise W.runtime.TransientProviderError("down", reason="provider_down")
        return {"ok": True}

    _stub_skill(app_module, monkeypatch, "market.quote", fails_then_works)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    headers = {"X-API-Key": "test-key", "Idempotency-Key": "retry-me"}
    first = client.post("/work/market/quote", headers=headers, json={"product_id": "BTC-USD"})
    second = client.post("/work/market/quote", headers=headers, json={"product_id": "BTC-USD"})
    assert first.status_code == 502
    assert second.status_code == 200, "the same key must work again after a failure"
    assert second.json()["result"] == {"ok": True}


def test_an_in_flight_duplicate_gets_409_rather_than_running_twice(app_module):
    W.ledger.reset_for_tests()
    state, _ = W.ledger.claim_idempotency("k-inflight", "call-1", "market.quote")
    assert state == "claimed"
    again, _ = W.ledger.claim_idempotency("k-inflight", "call-2", "market.quote")
    assert again == "in_progress"


# --- the audits are untouched -----------------------------------------------

def test_audit_routes_still_price_and_answer_exactly_as_before(client):
    for path, price in (("/audit/wcag", 0.05), ("/audit/seo", 0.05),
                        ("/audit/security", 0.05), ("/audit/performance", 0.05),
                        ("/audit/bundle", 0.15), ("/audit", 0.05)):
        body = client.post(path, json={"url": "https://example.com"}).json()
        assert body["price_usd"] == price, path


def test_a_worker_cannot_change_what_an_audit_charges(app_module, monkeypatch):
    """The worker catalog is consulted only AFTER _CATALOG, so a worker row
    colliding with an audit path could never shadow it."""
    monkeypatch.setattr(W.catalog, "BY_PATH", dict(
        W.catalog.BY_PATH, **{"/audit/wcag": W.catalog.CATALOG[0]}))
    assert app_module._price_of("/audit/wcag") == 0.05


def test_the_manifest_keeps_workers_out_of_the_audit_endpoint_list(client):
    manifest = client.get("/.well-known/agent.json").json()
    endpoint_paths = {entry["path"] for entry in manifest["endpoints"]}
    assert {"/audit/wcag", "/audit/seo", "/audit/security",
            "/audit/performance", "/audit/bundle", "/audit"} <= endpoint_paths
    assert not {path for path in endpoint_paths if path.startswith("/work/")}
    assert manifest["workers"]["available"] is True
    assert manifest["workers"]["count"] == len(W.catalog.live())


def test_health_reports_workers_without_letting_them_change_its_status(client):
    body = client.get("/health")
    assert body.status_code == 200
    payload = body.json()
    assert payload["status"] == "ok"
    assert payload["workers"]["configured"] is True


def test_worker_discovery_index_is_free(client):
    response = client.get("/work")
    assert response.status_code == 200
    assert response.json()["count"] == len(W.catalog.live())


# --- never advertise what this deployment cannot deliver --------------------

def test_an_unavailable_worker_is_not_advertised_anywhere(client, monkeypatch):
    """A worker whose provider has no credential here must vanish from the
    index, the manifest and the price gate -- not appear with a price we
    cannot honour."""
    target = W.catalog.BY_NAME["market.quote"]
    monkeypatch.setattr(type(target), "available",
                        lambda self: self.name != "market.quote")

    index = client.get("/work").json()
    assert "market.quote" not in [w["name"] for w in index["workers"]]
    assert "market.quote" in [u["name"] for u in index["unavailable"]]

    manifest = client.get("/.well-known/agent.json").json()
    assert "market.quote" not in [c["name"] for c in manifest["workers"]["capabilities"]]


def test_an_unavailable_worker_answers_503_and_never_quotes_a_price(client, monkeypatch):
    """Taking money for a capability we cannot run is the worst outcome
    available here, so the refusal comes BEFORE any payment instrument."""
    monkeypatch.setattr(type(W.catalog.BY_NAME["market.quote"]), "available",
                        lambda self: self.name != "market.quote")

    response = client.post("/work/market/quote", json={"product_id": "BTC-USD"})
    assert response.status_code == 503, "must refuse, not 402"
    body = response.json()
    assert body["billed"] is False
    assert body["reason"] == "capability_unavailable"
    assert "price_usd" not in body, "an unavailable worker must not quote a price"


def test_every_worker_has_a_request_example_carrying_its_required_fields():
    missing = {
        worker.name: sorted(set(worker.input_schema.get("required") or [])
                            - set(W.catalog.example_for(worker)))
        for worker in W.catalog.CATALOG
    }
    missing = {name: fields for name, fields in missing.items() if fields}
    assert not missing, f"add example values for: {missing}"


def test_openapi_marks_every_live_worker_route_payable(client):
    """Crawlers find paid endpoints by x-payment-info. Without it every /work
    route read as free in openapi.json while its own 402 priced it."""
    doc = client.get("/openapi.json").json()
    live = W.catalog.live()
    assert live, "no live workers -- this guard is checking nothing"
    for worker in live:
        operation = doc["paths"][worker.path]["post"]
        assert operation.get("x-payment-info", {}).get("offers"), worker.path
        assert "402" in operation["responses"], worker.path
        body = operation["requestBody"]["content"]["application/json"]
        assert body["schema"] == worker.input_schema, worker.path
        assert set(worker.input_schema.get("required") or []) <= set(body["example"]), worker.path


def test_audit_routes_openapi_bodies_are_untouched_by_the_worker_entries(client):
    doc = client.get("/openapi.json").json()
    body = doc["paths"]["/audit/wcag"]["post"]["requestBody"]["content"]["application/json"]
    assert body["example"] == {"url": "https://example.com"}
    assert "$ref" in body["schema"] or body["schema"].get("title"), "audit schema must stay FastAPI's own"


def test_a_worker_above_the_client_cap_tells_the_buyer_how_to_lift_it(client):
    """Stock x402 clients refuse any payment over $1.00 locally. The 402 for a
    dearer worker must say so in the body (the header the library signs is
    untouched), and a cheap worker must not carry the note."""
    dear = next(w for w in W.catalog.CATALOG if w.price_usd > 1.00 and w.available())
    cheap = next(w for w in W.catalog.CATALOG if w.price_usd <= 1.00 and w.available())

    response = client.post(dear.path, json=W.catalog.example_for(dear))
    assert response.status_code == 402
    body = response.json()
    assert "buyer_note" in body and f"${dear.price_usd:.2f}" in body["buyer_note"]
    assert "max_amount_per_payment" in body["buyer_note"]
    assert body.get("accepts"), "the payable accepts[] list must survive the rewrite"
    assert int(response.headers["content-length"]) == len(response.content)
    assert "payment-required" in {k.lower() for k in response.headers}

    response = client.post(cheap.path, json=W.catalog.example_for(cheap))
    assert response.status_code == 402
    assert "buyer_note" not in response.json()

    index = client.get("/work").json()
    by_name = {w["name"]: w for w in index["workers"]}
    assert "buyer_note" in by_name[dear.name]
    assert "buyer_note" not in by_name[cheap.name]
    assert "spend_cap" in index

    paths = client.get("/openapi.json").json()["paths"]
    assert paths[dear.path]["post"]["x-buyer-note"] == by_name[dear.name]["buyer_note"]
    assert "x-buyer-note" not in paths[cheap.path]["post"]


# --- receipts: response carries the id, the endpoint reproduces the row ------

def test_a_delivered_job_carries_a_retrievable_receipt(app_module, client, monkeypatch):
    async def works(ctx, payload):
        return {"answer": 42, "echo": payload}

    _stub_skill(app_module, monkeypatch, "market.quote", works)
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: None)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response, node_version="test-1")

    body = {"product_id": "BTC-USD"}
    response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200
    delivered = response.json()
    receipt_id = delivered["receipt_id"]
    assert receipt_id.startswith("rcpt_") and delivered["receipt_url"] == f"/work/receipts/{receipt_id}"

    receipt = client.get(delivered["receipt_url"]).json()
    assert receipt["receipt_id"] == receipt_id
    assert receipt["request_id"] == receipt_id[len("rcpt_"):]
    # Delivered on an API key: no x402 settlement exists, and the receipt says so.
    assert receipt["outcome"] == "delivered_not_settled"
    assert receipt["paid"] is False and receipt["delivered"] is True
    assert receipt["payment"]["tx_hash"] is None and receipt["payment"]["settled"] is False
    # The hashes are recomputable from what the buyer sent and received.
    assert receipt["request"]["request_hash"] == W.ledger.canonical_hash(body)
    assert receipt["delivery"]["result_hash"] == W.ledger.canonical_hash(delivered["result"])
    assert receipt["request"]["worker"] == "market.quote" and receipt["request"]["price_usd"] == 0.02
    assert receipt["execution"]["status"] == "ok" and receipt["node_version"] == "test-1"
    # Retrievable by request_id as well.
    assert client.get(f"/work/receipts/{receipt['request_id']}").json()["receipt_id"] == receipt_id


def test_a_failed_job_has_a_receipt_that_says_unpaid_and_undelivered(app_module, client, monkeypatch):
    async def always_fails(ctx, payload):
        raise W.runtime.TransientProviderError("provider is down", reason="provider_down")

    _stub_skill(app_module, monkeypatch, "market.quote", always_fails)
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: None)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                           json={"product_id": "BTC-USD"})
    assert response.status_code == 502
    receipt_id = response.json()["receipt_id"]
    receipt = client.get(f"/work/receipts/{receipt_id}").json()
    assert receipt["outcome"] == "unpaid_failed"
    assert receipt["paid"] is False and receipt["delivered"] is False
    assert receipt["delivery"] == {"delivered": False, "result_hash": None}
    assert receipt["payment"]["amount_atomic"] is None and receipt["payment"]["tx_hash"] is None
    assert receipt["execution"]["status"] == "failed"
    assert receipt["execution"]["failure_reason"] == "provider_down"


def test_an_unknown_receipt_is_a_404(client):
    assert client.get("/work/receipts/rcpt_doesnotexist").status_code == 404
    assert client.get("/work/receipts/../etc").status_code in (404, 400)


# --- an MPP `hash`-paid job records the same facts as an x402 settlement ---

def test_an_mpp_hash_paid_job_gets_a_paid_delivered_receipt(app_module, client, monkeypatch):
    """The payer sent USDC on Base itself and presented the tx hash; the
    ledger row and receipt must carry payer, tx, amount, asset, network and
    pay_to exactly as for an x402 settlement, with rail mpp."""
    async def works(ctx, payload):
        return {"answer": 42}

    _stub_skill(app_module, monkeypatch, "market.quote", works)
    facts = {"rail": "mpp", "method": "evm", "payer": "0x37555e884c5eba10f6e816dbecea30965b9b38c0",
             "pay_to": TEST_PAY_TO.lower(), "amount_atomic": 20000,
             "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "network": "eip155:8453",
             "tx_hash": "0x" + "cd" * 32}
    monkeypatch.setattr(app_module.mpp_payments, "verify_and_settle_sync", lambda cred, realm=None: cred == "hash-cred")
    monkeypatch.setattr(app_module.mpp_payments, "settlement_for", lambda cred: dict(facts) if cred == "hash-cred" else None)
    billed = []
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: billed.append(price_usd))
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response,
        mpp_payment_facts=app_module.mpp_payments.settlement_for)

    response = client.post("/work/market/quote", headers={"Authorization": "Payment hash-cred"},
                           json={"product_id": "BTC-USD"})
    assert response.status_code == 200, response.text
    receipt = client.get(response.json()["receipt_url"]).json()
    assert receipt["outcome"] == "paid_delivered" and receipt["paid"] is True and receipt["delivered"] is True
    assert receipt["payment"] == {
        "rail": "mpp", "payer": facts["payer"], "pay_to": facts["pay_to"], "amount_atomic": 20000,
        "amount_usd": 0.02, "asset": facts["asset"], "network": "eip155:8453",
        "tx_hash": facts["tx_hash"], "settled": True}
    assert receipt["delivery"]["result_hash"] == W.ledger.canonical_hash({"answer": 42})
    # The x402 gate's _bill still ran (it is a no-op for a prepaid MPP call).
    assert billed == [0.02]


def test_an_x402_payer_is_unaffected_by_the_mpp_fact_lookup(app_module, client, monkeypatch):
    """With no MPP credential, the lookup is never consulted: an API-key call
    still records no payer and reads delivered_not_settled."""
    async def works(ctx, payload):
        return {"ok": True}

    _stub_skill(app_module, monkeypatch, "market.quote", works)
    consulted = []
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: None)
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response,
        mpp_payment_facts=lambda cred: consulted.append(cred))
    response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"}, json={"product_id": "BTC-USD"})
    assert response.status_code == 200
    receipt = client.get(response.json()["receipt_url"]).json()
    assert receipt["outcome"] == "delivered_not_settled" and receipt["payment"]["rail"] is None
    assert consulted == []
