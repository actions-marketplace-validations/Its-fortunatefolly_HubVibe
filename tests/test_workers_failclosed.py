"""Tests for the four wave-2b bees that are fail-closed past credentials:
llm.generate's Claude-on-Vertex provider, maps.places/route/weather, and
video.generate.

None of these can run for real here (no live Google project, no Maps key),
so what's tested is exactly the thing that matters before either arrives:
that each one reports itself unavailable with a specific, actionable
reason rather than a generic one, that the catalog/router agree on that
(503, no price, not advertised), and the request-shape validation each
does before ever reaching its provider.
"""

import asyncio
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

    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_failclosed", MAIN_PATH)
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


def _fake_google_credentials(monkeypatch):
    import importlib

    google_auth = importlib.import_module(W.__name__ + ".providers.google_auth")
    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", object())
    monkeypatch.setattr(google_auth, "_project", "test-project")


# --- maps.*: unavailable with a specific reason, not the generic one -------

def test_maps_places_is_unavailable_without_a_key(client, monkeypatch):
    """This must stay unavailable EVEN WITH Google credentials -- Maps
    Grounding Lite is keyed separately from the Vertex/BigQuery scope."""
    _fake_google_credentials(monkeypatch)
    response = client.post("/work/maps/places", json={"query": "coffee"})
    assert response.status_code == 503
    body = response.json()
    assert body["billed"] is False
    assert "price_usd" not in body


def test_maps_grounding_provider_names_the_missing_key(monkeypatch):
    import importlib

    maps_grounding = importlib.import_module(W.__name__ + ".providers.maps_grounding")
    monkeypatch.delenv("MAPS_GROUNDING_LITE_API_KEY", raising=False)
    provider = maps_grounding.PROVIDERS[0]
    assert provider.available() is False
    assert "MAPS_GROUNDING_LITE_API_KEY" in provider.unavailable_reason()


def test_maps_grounding_provider_is_available_once_keyed(monkeypatch):
    import importlib

    maps_grounding = importlib.import_module(W.__name__ + ".providers.maps_grounding")
    monkeypatch.setenv("MAPS_GROUNDING_LITE_API_KEY", "test-key-value")
    assert maps_grounding.PROVIDERS[0].available() is True


# --- maps.* input validation --------------------------------------------

def test_maps_places_rejects_an_empty_query():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.maps.places(None, {"query": ""}))


def test_maps_route_requires_both_ends():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.maps.route(None, {"origin": "SF"}))


def test_maps_route_rejects_an_unknown_travel_mode():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.maps.route(
            None, {"origin": "SF", "destination": "Oakland", "travel_mode": "TELEPORT"}))


def test_maps_weather_requires_a_location():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.maps.weather(None, {}))


# --- video.generate: advertised exactly when credentials resolve ----------
#
# The old WORKER_VEO_ENABLED gate existed only because the model id was
# unverified. A real call to veo-3.1-fast-generate-001 on resolver-time
# (2026-09-18) returned a 4s mp4 in ~30s, so the gate is gone. What stays
# pinned is the part that protects a paying caller: never advertised without
# credentials, and the request shape refused before any call is made.

def test_video_generate_is_unavailable_without_credentials(monkeypatch):
    import importlib

    google_auth = importlib.import_module(W.__name__ + ".providers.google_auth")
    veo = importlib.import_module(W.__name__ + ".providers.veo")
    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", None)
    provider = veo.PROVIDERS[0]
    assert provider.available() is False
    assert provider.unavailable_reason()


def test_video_generate_is_available_once_credentials_resolve(monkeypatch):
    import importlib

    _fake_google_credentials(monkeypatch)
    veo = importlib.import_module(W.__name__ + ".providers.veo")
    assert veo.PROVIDERS[0].available() is True
    assert veo._MODEL == "veo-3.1-fast-generate-001"


def test_video_generate_rejects_a_bad_aspect_ratio_before_any_call(monkeypatch):
    import importlib

    _fake_google_credentials(monkeypatch)
    veo = importlib.import_module(W.__name__ + ".providers.veo")
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(veo.PROVIDERS[0].generate("a bee", aspect_ratio="1:1"))


def test_video_generate_rejects_a_bad_duration(monkeypatch):
    import importlib

    _fake_google_credentials(monkeypatch)
    veo = importlib.import_module(W.__name__ + ".providers.veo")
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(veo.PROVIDERS[0].generate("a bee", duration_seconds=5))


def test_generate_video_skill_rejects_an_empty_prompt():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.media.generate_video(None, {"prompt": ""}))


# --- llm.generate: Claude on Vertex model selection -------------------------

def test_llm_generate_accepts_a_pinned_claude_model_in_the_matching_provider():
    import importlib

    completion = importlib.import_module(W.__name__ + ".providers.completion")
    claude = next(p for p in completion.PROVIDERS if p.provider_name == "anthropic")
    gemini_provider = next(p for p in completion.PROVIDERS if p.provider_name == "gemini")
    assert claude.matches("anthropic", "claude-opus-5") is True
    assert claude.matches("anthropic", "gemini-2.5-flash") is False
    assert claude.matches("gemini", None) is False
    assert gemini_provider.matches("anthropic", None) is False
    assert gemini_provider.matches(None, None) is True


def test_llm_generate_rejects_a_model_no_configured_provider_offers():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.generate(
            None, {"prompt": "hi", "model": "gpt-4-this-does-not-exist"}))


def test_claude_on_vertex_reports_403_as_unavailable_not_a_provider_error(monkeypatch):
    """A 403 here means Model Garden access is not enabled yet -- a
    one-time setup step, not a health blip worth retrying or falling back
    from. Confirmed against a fake transport so no network is touched."""
    import importlib

    completion = importlib.import_module(W.__name__ + ".providers.completion")
    google_auth = importlib.import_module(W.__name__ + ".providers.google_auth")

    class _FakeCreds:
        valid = True
        token = "fake-token"

    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", _FakeCreds())
    monkeypatch.setattr(google_auth, "_project", "test-project")

    class _FakeResponse:
        status_code = 403
        text = "Model Garden access required"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse()

    import httpx as httpx_module
    monkeypatch.setattr(httpx_module, "AsyncClient", lambda **kwargs: _FakeClient())

    claude = next(p for p in completion.PROVIDERS if p.provider_name == "anthropic")
    with pytest.raises(W.runtime.ProviderUnavailable) as exc_info:
        asyncio.run(claude.generate("hi", None, 100, 0.5))
    assert "Model Garden" in str(exc_info.value)
