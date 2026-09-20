"""Tests for the 14 wave-1 bees: search, code, llm.generate, image, speech,
data.forecast/anomalies, verify.claims, research.web/company, monitor,
security.mcp_inspect.

The generic guarantees (402 pricing, Bazaar data, unavailable -> 503,
idempotency, "nothing billed on failure") are already exercised for every
CATALOG entry by test_workers_payment_safety.py. What is specific to these
workers and worth its own test:

  * their own input validation runs and refuses BEFORE payment is read
  * the SQL the two BigQuery-AI workers build from structured input is what
    was intended, since neither this suite nor CI has a live BigQuery to
    run it against
  * the composite helper's partial-source handling, which nothing else
    exercises
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
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()

    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_w1", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        with_page=module.browser_pool.with_page,
        goto_guarded=getattr(module.audits, "goto_guarded", None))
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _stub_skill(app_module, monkeypatch, name, fn):
    registry = dict(W.router.REGISTRY)
    registry[name] = fn
    monkeypatch.setattr(W.router, "REGISTRY", registry)


def _fake_google_credentials(monkeypatch):
    """This environment has no real GCP ADC (nor does CI). To test a
    Google-backed worker's WIRING -- as opposed to the fail-closed path,
    which test_workers_payment_safety.py's generic
    `test_an_unavailable_worker_...` tests already cover for any worker --
    fake `configured()` the same way test_workers_ledger.py does, by setting
    google_auth's own cached-resolution state directly rather than reaching
    a real metadata server.

    Imported via `W.__name__` rather than `W.providers.google_auth`: once the
    `app_module` fixture re-points W at main.py's OWN import of the workers
    package (a separate module identity from this file's standalone
    `_load_workers()`), `providers` is not guaranteed to already be bound as
    an attribute on that instance even though the file content is identical.
    """
    import importlib

    google_auth = importlib.import_module(W.__name__ + ".providers.google_auth")
    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", object())
    monkeypatch.setattr(google_auth, "_project", "test-project")


# --- every new worker exists and is wired -----------------------------------

WAVE1_NAMES = {
    "search.web", "llm.generate", "code.execute", "image.generate",
    "speech.synthesize", "speech.transcribe", "data.forecast", "data.anomalies",
    "verify.claims", "research.web", "research.company", "monitor.snapshot",
    "monitor.check", "security.mcp_inspect",
}


def test_every_wave1_worker_is_in_the_catalog_and_registry():
    names = {w.name for w in W.catalog.CATALOG}
    assert WAVE1_NAMES <= names
    for name in WAVE1_NAMES:
        worker = W.catalog.BY_NAME[name]
        assert worker.skill in W.router.REGISTRY, f"{name}: skill not registered"


# --- validation runs before any provider is touched -------------------------

def test_search_web_rejects_empty_and_oversized_query():
    async def run(payload):
        return await W.skills.search.web_search(None, payload)

    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(run({"query": ""}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(run({"query": "x" * 401}))


def test_code_execute_rejects_empty_and_oversized_code():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.code.execute(None, {"code": ""}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.code.execute(None, {"code": "x" * 8001}))


def test_llm_generate_rejects_bad_inputs():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.generate(None, {}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.generate(None, {"prompt": "hi", "max_tokens": 5000}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.generate(None, {"prompt": "hi", "temperature": 3}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.generate(None, {"prompt": "hi", "provider": "nonexistent-vendor"}))


def test_generate_image_rejects_empty_prompt():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.generate_image(None, {"prompt": ""}))


def test_synthesize_speech_rejects_empty_and_oversized_text():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.synthesize_speech(None, {"text": ""}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.synthesize_speech(None, {"text": "x" * 3001}))


def test_transcribe_speech_rejects_missing_and_invalid_base64():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.transcribe_speech(None, {}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.transcribe_speech(None, {"audio_base64": "not base64!!"}))


def test_verify_claims_rejects_missing_claims_or_sources():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.verify.verify_claims(None, {"sources": ["https://example.com"]}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.verify.verify_claims(None, {"claims": ["x"]}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.verify.verify_claims(
            None, {"claims": ["x"] * 11, "sources": ["https://example.com"]}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.verify.verify_claims(
            None, {"claims": ["x"], "sources": ["https://example.com"] * 5}))


def test_research_web_rejects_missing_question_and_bad_max_sources():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.composites.research_web(None, {}))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.composites.research_web(
            None, {"question": "what?", "max_sources": 9}))


def test_research_company_rejects_missing_company():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.composites.research_company(None, {}))


def test_security_mcp_inspect_rejects_a_blocked_target():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.security.mcp_inspect(
            None, {"url": "http://169.254.169.254/latest/meta-data/"}))


def test_monitor_check_without_a_prior_snapshot_is_invalid_request():
    import asyncio

    W.ledger.reset_for_tests()
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.monitor.check(None, {"url": "https://example.com/never-snapshotted"}))


# --- data.forecast / data.anomalies build the SQL they claim to ------------

class _FakeCtx:
    """Enough of JobContext for a skill's ctx.run to work without the real
    envelope: runs `call` against one stub provider and returns its value."""

    def __init__(self):
        self.captured_sql = None

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        class _StubBigQuery:
            id = "bigquery"

            def available(self):
                return True

        outer = self

        class _Value(dict):
            pass

        async def query(sql, max_gib=None):
            outer.captured_sql = sql
            return W.runtime.ProviderResult(
                value={"columns": ["a"], "rows": [], "row_count": 0,
                       "total_rows": 0, "truncated": False, "gib_processed": 0.0,
                       "cache_hit": False},
                cost_micros=0, cost_measured=True)

        provider = _StubBigQuery()
        provider.query = query
        result = await call(provider)
        return result.value


def test_forecast_builds_the_documented_ai_forecast_call():
    import asyncio

    ctx = _FakeCtx()
    asyncio.run(W.skills.data.forecast(ctx, {
        "table": "proj.ds.tbl", "timestamp_col": "ts", "data_col": "val",
        "horizon": 5}))
    sql = ctx.captured_sql
    assert "AI.FORECAST(" in sql
    assert "`proj.ds.tbl`" in sql
    assert "data_col => 'val'" in sql
    assert "timestamp_col => 'ts'" in sql
    assert "horizon => 5" in sql


def test_forecast_rejects_a_column_name_that_is_not_a_plain_identifier():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.data.forecast(_FakeCtx(), {
            "table": "proj.ds.tbl", "timestamp_col": "ts; DROP TABLE x", "data_col": "val"}))


def test_forecast_id_cols_render_as_a_safe_sql_array_literal():
    import asyncio

    ctx = _FakeCtx()
    asyncio.run(W.skills.data.forecast(ctx, {
        "table": "proj.ds.tbl", "timestamp_col": "ts", "data_col": "val",
        "id_cols": ["region", "sku"]}))
    assert "id_cols => ['region', 'sku']" in ctx.captured_sql


def test_detect_anomalies_builds_the_two_table_call():
    import asyncio

    ctx = _FakeCtx()
    asyncio.run(W.skills.data.detect_anomalies(ctx, {
        "history_table": "proj.ds.hist", "target_table": "proj.ds.target",
        "timestamp_col": "ts", "data_col": "val", "anomaly_prob_threshold": 0.8}))
    sql = ctx.captured_sql
    assert "AI.DETECT_ANOMALIES(TABLE `proj.ds.hist`, TABLE `proj.ds.target`" in sql
    assert "anomaly_prob_threshold => 0.8" in sql


def test_detect_anomalies_rejects_threshold_out_of_range():
    import asyncio
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.data.detect_anomalies(_FakeCtx(), {
            "history_table": "proj.ds.h", "target_table": "proj.ds.t",
            "timestamp_col": "ts", "data_col": "val", "anomaly_prob_threshold": 1.5}))


# --- composite source-gathering: partial failure is disclosed, not fatal ---

def test_gather_sources_reports_unreadable_sources_as_partial(monkeypatch):
    import asyncio

    async def fake_search(ctx, payload):
        return {"answer": "n/a", "sources": [
            {"url": "https://good.example/a", "title": "Good"},
            {"url": "https://bad.example/b", "title": "Bad"}]}

    async def fake_extract(ctx, payload):
        if "bad.example" in payload["url"]:
            raise W.runtime.PermanentProviderError("blocked by robots.txt")
        return {"final_url": payload["url"], "title": "Good", "text": "content",
               "text_chars": 7, "truncated": False, "links": [],
               "rendered": False}

    monkeypatch.setattr(W.skills.search, "web_search", fake_search)
    monkeypatch.setattr(W.skills.extract, "extract_page", fake_extract)

    read, partial = asyncio.run(
        W.skills.composites._gather_sources(_FakeCtx(), "query", max_sources=2))
    assert len(read) == 1 and read[0]["url"] == "https://good.example/a"
    assert len(partial) == 1 and "bad.example" in partial[0]["url"]


def test_gather_sources_dedupes_by_host():
    import asyncio

    async def fake_search(ctx, payload):
        return {"answer": "n/a", "sources": [
            {"url": "https://example.com/a", "title": "A"},
            {"url": "https://example.com/b", "title": "B"},
            {"url": "https://other.example/c", "title": "C"}]}

    async def fake_extract(ctx, payload):
        return {"final_url": payload["url"], "title": "T", "text": "x",
               "text_chars": 1, "truncated": False, "links": [], "rendered": False}

    import pytest as _pytest
    from unittest import mock
    with mock.patch.object(W.skills.search, "web_search", fake_search), \
         mock.patch.object(W.skills.extract, "extract_page", fake_extract):
        read, partial = asyncio.run(
            W.skills.composites._gather_sources(_FakeCtx(), "query", max_sources=3))
    urls = [r["url"] for r in read]
    assert urls == ["https://example.com/a", "https://other.example/c"], (
        "same-host second source must be skipped, not counted toward max_sources")


# --- end-to-end wiring for a representative sample of the new routes -------

def test_search_web_unpaid_call_prices_and_lists_in_bazaar(client, monkeypatch):
    _fake_google_credentials(monkeypatch)
    response = client.post("/work/search/web", json={"query": "x"})
    assert response.status_code == 402
    body = response.json()
    assert body["price_usd"] == 0.10
    assert "bazaar" in (body.get("extensions") or {})


def test_data_forecast_priced_at_ten_dollars(client, monkeypatch):
    _fake_google_credentials(monkeypatch)
    body = client.post("/work/data/forecast", json={}).json()
    assert body["price_usd"] == 10.00


def test_image_generate_failure_is_unbilled(app_module, client, monkeypatch):
    _fake_google_credentials(monkeypatch)

    async def always_fails(ctx, payload):
        raise W.runtime.PermanentProviderError("the model refused the prompt")

    _stub_skill(app_module, monkeypatch, "image.generate", always_fails)
    billed = []
    monkeypatch.setattr(app_module, "_bill",
                        lambda auth, price_usd: billed.append(price_usd))
    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=app_module._bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)

    response = client.post("/work/image/generate", headers={"X-API-Key": "test-key"},
                           json={"prompt": "a beehive"})
    assert response.status_code == 502
    assert response.json()["billed"] is False
    assert billed == []


def test_verify_claims_missing_field_is_400_before_payment(client, monkeypatch):
    _fake_google_credentials(monkeypatch)
    response = client.post("/work/verify/claims", headers={"X-API-Key": "test-key"}, json={})
    assert response.status_code == 400
    assert response.json()["billed"] is False
