"""The discovery contract: what machines read about the 37 workers and the
audits must be the literal shape the routes return, on every surface.

Before this, openapi.json documented every /work 200 as `{}`, agent.json's
workers carried only prose (`returns`), and the Bazaar record on all 37
402s advertised the placeholder output `{"status": "ok"}` -- the one field
the Bazaar ranks on. And each surface still called the node a five-tool
audit suite.

These tests pin: one explicit output schema per worker whose generated
example validates; the same schema on openapi.json, agent.json, /work and
the Bazaar record; the ARD manifest at /.well-known/ard.json conforming to
the spec's own JSON Schema (vendored in tests/fixtures); and one title on
every surface.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
# https://raw.githubusercontent.com/ards-project/ard-spec/main/spec/schemas/ard-entry.schema.json
# fetched 2026-09-22 (spec v0.91). Vendored so CI does not depend on GitHub.
ARD_SCHEMA_PATH = REPO_ROOT / "tests" / "fixtures" / "ard-entry.schema.json"

TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
TITLE = "HubVibe: 38 Machine-Payable Dev Utilities and WCAG Audits"


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
    """A fresh app with x402 configured (so 402s carry Bazaar records) and a
    self-hosted PUBLIC_BASE_URL (so absolute URLs are checked, not assumed)."""
    global W
    W = _load_workers()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_discovery", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill,
        deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
    )
    return module


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app)


def _without_nones(value):
    if isinstance(value, dict):
        return {k: _without_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nones(v) for v in value]
    return value


def _validator(schema: dict):
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


# --- one explicit output schema per worker ------------------------------------

def test_every_worker_publishes_an_explicit_output_schema_not_a_placeholder():
    """`{"type": "object", "description": "see returns"}` told an agent
    nothing. Every worker's result schema names its keys."""
    thin = [w.name for w in W.catalog.CATALOG
            if w.output_schema.get("type") != "object"
            or not (w.output_schema.get("properties") or {})]
    assert not thin, f"workers still advertising a placeholder output: {thin}"
    assert len(W.catalog.CATALOG) == 38
    assert set(W.catalog.contract.OUTPUT_SCHEMAS) == {w.name for w in W.catalog.CATALOG}


@pytest.mark.parametrize("worker", W.catalog.CATALOG, ids=lambda w: w.name)
def test_the_generated_example_validates_against_its_own_schema(worker):
    """The example on openapi.json and the Bazaar is generated FROM the
    schema; if the two ever disagree this is where it shows."""
    result_schema = worker.output_schema
    _validator(result_schema).validate(W.catalog.output_example(worker))
    response_schema = W.catalog.response_schema(worker)
    example = W.catalog.response_example(worker)
    _validator(response_schema).validate(example)
    assert example["worker"] == worker.name
    assert example["price_usd"] == worker.price_usd
    assert example["receipt_url"].startswith("/work/receipts/")
    # Every documented key of the result appears in the example, so a
    # reader of the example sees the whole shape.
    assert set(result_schema["properties"]) <= set(example["result"])


def test_every_worker_has_two_to_five_representative_queries():
    """The ARD spec: representativeQueries SHOULD contain 2-5 examples."""
    counts = {w.name: len(W.catalog.contract.REPRESENTATIVE_QUERIES.get(w.name, []))
              for w in W.catalog.CATALOG}
    off = {name: n for name, n in counts.items() if not 2 <= n <= 5}
    assert not off, f"representative query count outside 2-5: {off}"


# --- the same schema on every surface ------------------------------------------

def test_openapi_documents_every_work_200_with_the_routes_schema_and_example(client):
    doc = client.get("/openapi.json").json()
    live = W.catalog.live()
    assert live
    for worker in live:
        content = doc["paths"][worker.path]["post"]["responses"]["200"]["content"]["application/json"]
        assert content["schema"] == W.catalog.response_schema(worker), worker.path
        assert content["schema"]["properties"]["result"] == worker.output_schema, worker.path
        # FastAPI serialises the document with exclude_none, so a null-valued
        # example field (tools_error: null) is dropped from openapi.json; the
        # Bazaar record carries the example in full.
        assert content["example"]["result"] == _without_nones(W.catalog.output_example(worker)), worker.path


def test_agent_json_carries_each_workers_output_schema_and_the_envelope(client):
    workers = client.get("/.well-known/agent.json").json()["workers"]
    assert workers["available"] is True
    assert workers["response_envelope"] == W.catalog.contract.RESPONSE_ENVELOPE
    assert "/work/receipts/" in workers["receipts"]
    by_name = {c["name"]: c for c in workers["capabilities"]}
    for worker in W.catalog.live():
        assert by_name[worker.name]["output_schema"] == worker.output_schema, worker.name


def test_work_index_carries_each_workers_output_schema(client):
    index = client.get("/work").json()
    assert index["response_envelope"] == W.catalog.contract.RESPONSE_ENVELOPE
    for row in index["workers"]:
        assert row["output_schema"] == W.catalog.BY_NAME[row["name"]].output_schema, row["name"]


def test_the_bazaar_record_on_a_worker_402_shows_the_real_response(client):
    """Not `{"status": "ok"}`: the envelope with this worker's result, and
    the schema the library's own discovery validator accepts."""
    from x402.extensions.bazaar import validate_discovery_extension

    for worker in W.catalog.live():
        body = client.post(worker.path, json={}).json()
        assert body.get("price_usd") == worker.price_usd, worker.path
        output = body["extensions"]["bazaar"]["info"]["output"]
        assert output["example"] == W.catalog.response_example(worker), worker.path
        assert output["example"]["result"] != {}, worker.path
        # The library embeds the output schema inside the record's own
        # schema, as the type of `info.output.example`.
        embedded = body["extensions"]["bazaar"]["schema"]["properties"]["output"]["properties"]["example"]
        expected = W.catalog.response_schema(worker)
        assert embedded["properties"] == expected["properties"], worker.path
        assert embedded["required"] == expected["required"], worker.path
        assert validate_discovery_extension(body["extensions"]).valid is True, worker.path


# --- /.well-known/ard.json ---------------------------------------------------

def _ard_manifest_validator():
    schema = json.loads(ARD_SCHEMA_PATH.read_text(encoding="utf-8"))
    manifest_schema = {**schema, "$ref": "#/$defs/ArdManifest"}
    return _validator(manifest_schema)


def test_ard_manifest_is_served_and_conforms_to_the_spec_schema(client):
    response = client.get("/.well-known/ard.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    manifest = response.json()
    _ard_manifest_validator().validate(manifest)
    assert manifest["entries"]


def test_ard_lists_every_audit_every_live_worker_and_the_cards(client):
    manifest = client.get("/.well-known/ard.json").json()
    ids = [e["identifier"] for e in manifest["entries"]]
    assert len(ids) == len(set(ids)), "duplicate identifiers"
    pattern = re.compile(r"^urn:air:audit\.example\.test:[a-z]+:[a-zA-Z0-9._-]+$")
    assert all(pattern.match(i) for i in ids), ids
    by_id = {e["identifier"]: e for e in manifest["entries"]}
    for worker in W.catalog.live():
        entry = by_id[f"urn:air:audit.example.test:work:{worker.name}"]
        assert entry["data"]["price_usd"] == worker.price_usd
        assert entry["data"]["output_schema"] == worker.output_schema
        assert entry["data"]["input_example"] == W.catalog.example_for(worker)
        assert worker.name in entry["capabilities"]
        assert entry["representativeQueries"] == W.catalog.contract.REPRESENTATIVE_QUERIES[worker.name]
        assert "url" not in entry  # inline data, so exactly one of url/data
    for path in ("/audit/wcag", "/audit/seo", "/audit/security", "/audit/performance", "/audit/bundle"):
        entry = by_id[f"urn:air:audit.example.test:audit:{path.rsplit('/', 1)[-1]}"]
        assert entry["data"]["path"] == path
        assert entry["data"]["output_schema"]["type"] == "object"
    assert by_id["urn:air:audit.example.test:mcp:site-audits"]["url"] == "https://audit.example.test/mcp.json"
    assert by_id["urn:air:audit.example.test:mcp:site-audits"]["type"] == "application/mcp-server-card+json"
    assert by_id["urn:air:audit.example.test:api:openapi"]["url"] == "https://audit.example.test/openapi.json"
    assert by_id["urn:air:audit.example.test:node:hubvibe"]["url"] == "https://audit.example.test/.well-known/agent.json"


def test_every_ard_entry_carries_two_to_five_queries_and_a_capability(client):
    for entry in client.get("/.well-known/ard.json").json()["entries"]:
        assert 2 <= len(entry["representativeQueries"]) <= 5, entry["identifier"]
        assert entry["capabilities"], entry["identifier"]
        assert entry["description"], entry["identifier"]


def test_ard_asserts_no_rail_and_points_at_no_production_host(client):
    """Which rails settle is deployment state and lives in agent.json, so no
    entry carries a payment-methods list; and a self-hosted copy must not
    point at hubvibe-io.com or the retired host."""
    manifest = client.get("/.well-known/ard.json").json()
    for entry in manifest["entries"]:
        assert "payment_methods" not in json.dumps(entry), entry["identifier"]
        payment = (entry.get("data") or {}).get("payment", "")
        if payment:
            assert "agent.json" in payment, entry["identifier"]
    lowered = json.dumps(manifest).lower()
    for term in ("run.app", "hubvibe-io.com"):
        assert term not in lowered, term


def test_ard_is_reachable_from_every_other_discovery_surface(client):
    assert client.get("/.well-known/agent.json").json()["discovery"]["ard"].endswith("/.well-known/ard.json")
    assert client.get("/openapi.json").json()["x-service-info"]["docs"]["ard"] == "/.well-known/ard.json"
    assert "Agentmap: https://hubvibe-io.com/.well-known/ard.json" in client.get("/robots.txt").text
    assert "https://hubvibe-io.com/.well-known/ard.json" in client.get("/sitemap.xml").text
    assert 'rel="ard"' in client.get("/").text
    assert "/.well-known/ard.json" in client.get("/llms.txt").text


# --- one title everywhere -----------------------------------------------------

def test_one_title_on_openapi_agent_json_mcp_json_and_the_registry_entry(client):
    assert client.get("/openapi.json").json()["info"]["title"] == TITLE
    assert client.get("/.well-known/agent.json").json()["name"] == TITLE
    assert client.get("/mcp.json").json()["description"].startswith(TITLE)
    assert json.loads((STATIC / "mcp.json").read_text())["description"].startswith(TITLE)
    assert json.loads((REPO_ROOT / "server.json").read_text())["title"] == TITLE


def test_no_surface_still_calls_the_node_an_audit_suite(client):
    stale = "Site Compliance Auditing Suite"
    assert stale not in client.get("/openapi.json").text
    assert stale not in client.get("/.well-known/agent.json").text
    assert stale not in client.get("/mcp.json").text
    assert stale not in client.get("/.well-known/ard.json").text


# --- the Bazaar identity on every 402: one name, this route's own tags ---------

def _v2_resource(response):
    from x402.http.utils import decode_payment_required_header
    raw = response.headers.get("payment-required")
    assert raw, "no PAYMENT-REQUIRED header"
    return decode_payment_required_header(raw).resource


def test_every_402_names_the_service_hubvibe_not_site_audits(client):
    """The index stamped "HubVibe Site Audits" on an LLM completion. One
    name for all 43 routes; the description says what the route sells."""
    for path in ("/audit/wcag", "/work/market/quote", "/work/prediction/events"):
        body = {"url": "https://example.com"} if path.startswith("/audit") else {}
        resource = _v2_resource(client.post(path, json=body))
        assert resource.service_name == "HubVibe", (path, resource.service_name)
        assert len(resource.service_name) <= 32


def test_a_worker_402_carries_its_own_catalog_tags_not_the_audit_tags(client):
    """Capability search matches on `resource.tags`. A Polymarket read tagged
    `accessibility` is found by nobody looking for prediction markets."""
    for worker in W.catalog.live():  # a worker without its provider answers 503, not 402
        resource = _v2_resource(client.post(worker.path, json={}))
        expected = [t for t in worker.tags][:5]
        assert resource.tags == expected, (worker.name, resource.tags)
        assert "accessibility" not in resource.tags or "accessibility" in worker.tags


def test_an_audit_402_keeps_the_audit_tags(client):
    resource = _v2_resource(client.post("/audit/seo", json={"url": "https://example.com"}))
    assert resource.tags == ["accessibility", "wcag", "seo", "security", "performance"]


def test_every_routes_tags_pass_the_facilitators_validation(client):
    """<=5 tags, each printable ASCII of <=32 chars: the Bazaar's own rules.
    An invalid list costs the whole discovery record. Two workers carry more
    than five tags in the catalog; the challenge sends the first five."""
    paths = [w.path for w in W.catalog.live()] + [
        "/audit/wcag", "/audit/seo", "/audit/security", "/audit/performance", "/audit/bundle"]
    # The catalog rows the test deployment cannot serve are checked statically.
    for w in W.catalog.CATALOG:
        assert all(0 < len(t) <= 32 and t.isascii() and t.isprintable() for t in w.tags), w.name
    for path in paths:
        body = {"url": "https://example.com"} if path.startswith("/audit") else {}
        tags = _v2_resource(client.post(path, json=body)).tags
        assert 1 <= len(tags) <= 5, (path, tags)
        assert all(0 < len(t) <= 32 and t.isascii() and t.isprintable() for t in tags), (path, tags)
        assert len(set(tags)) == len(tags), (path, tags)


def test_bazaar_tags_helper_trims_and_defaults():
    x = app_x402()
    assert x.bazaar_tags(["a", "b", "c", "d", "e", "f"]) == ["a", "b", "c", "d", "e"]
    assert x.bazaar_tags(["ok", "x" * 33, "ok", "", "caf\u00e9", "fine"]) == ["ok", "fine"]
    assert x.bazaar_tags(None) == ["accessibility", "wcag", "seo", "security", "performance"]
    assert x.bazaar_tags([]) == ["accessibility", "wcag", "seo", "security", "performance"]


def app_x402():
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_x402_tags", REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mcp_paywall_for_the_probability_tool_carries_the_stats_tags(client, app_module):
    """The MCP paywall is the other place a v2 challenge is built. The
    worker-backed tool names its worker's capability, not accessibility."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "hubvibe_predictive_probability_engine",
                          "arguments": {"points": [[1, 2], [2, 4], [3, 6]]}}}
    response = client.post("/mcp", json=payload)
    text = response.text
    assert "statistics" in text and "regression" in text, text[:400]
    assert '"serviceName": "HubVibe"' in text or '"serviceName":"HubVibe"' in text, text[:400]


def test_an_audit_402s_bazaar_record_shows_the_real_audit_response(client, app_module):
    """The audits' index record advertised the placeholder {"pass": true}
    long after the workers got real schemas. Now it carries the same output
    schema the MCP tool and ARD publish, with an example that validates."""
    from x402.http.utils import decode_payment_required_header
    for path, schema in app_module._MCP_OUTPUT_SCHEMAS.items():
        raw = client.post(path, json={"url": "https://example.com"}).headers.get("payment-required")
        challenge = decode_payment_required_header(raw)
        info = challenge.extensions["bazaar"]["info"]["output"]
        example = info["example"]
        assert example != {"pass": True}, path
        _validator(schema).validate(example)
        recorded = challenge.extensions["bazaar"]["schema"]["properties"]["output"]["properties"]
        assert recorded["example"].get("required") or recorded["example"].get("properties"), path
