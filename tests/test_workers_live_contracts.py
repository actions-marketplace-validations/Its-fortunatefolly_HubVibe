"""Pin the provider request/response shapes that were verified against the
LIVE services on 2026-09-18 (project resolver-time, real credentials).

Each test here guards a defect that a mocked test could not see and a real
call did: the model ids that 404 in us-central1 but resolve on `global`, the
Claude sampling parameter that 400s on the current generation, the Maps route
Waypoints, and a BigQuery job that outlives its timeout being read as a
successful empty answer. No network is touched here -- the live proof was the
real run; these keep it from quietly reverting.
"""

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"


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


def _provider(name):
    return importlib.import_module(W.__name__ + ".providers." + name)


def _fake_google(monkeypatch, google_auth=None):
    # Pass the provider module's OWN google_auth when it matters: in a full
    # run, earlier tests can leave a provider bound to another copy of it.
    google_auth = google_auth or _provider("google_auth")
    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", object())
    monkeypatch.setattr(google_auth, "_project", "test-project")

    async def headers():
        return {"Authorization": "Bearer test"}

    monkeypatch.setattr(google_auth, "headers", headers)


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


def _capture_posts(monkeypatch, module, reply):
    """Replace httpx.AsyncClient in one provider module; record each POST."""
    sent = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append({"url": url, "json": json})
            return _Response(reply)

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    return sent


# --- Vertex endpoint: `global`, where current models and aliases live -------

def test_gemini_models_are_called_on_the_global_endpoint():
    gemini = _provider("gemini")
    url = gemini.model_url("p", "gemini-flash-latest", "generateContent")
    assert url == ("https://aiplatform.googleapis.com/v1/projects/p/locations/global"
                   "/publishers/google/models/gemini-flash-latest:generateContent")


def test_a_regional_override_still_builds_a_regional_host():
    gemini = _provider("gemini")
    url = gemini.model_url("p", "m", "generateContent", region="us-central1")
    assert url.startswith("https://us-central1-aiplatform.googleapis.com/")
    assert "/locations/us-central1/" in url


def test_no_default_points_at_the_retiring_2_5_text_line():
    """gemini-2.5-flash/-pro retire 2026-10-20."""
    for name in ("gemini", "completion", "search_grounding", "code_exec"):
        source = (PKG / "providers" / f"{name}.py").read_text()
        assert '"gemini-2.5-flash"' not in source, name
        assert "gemini-2.5-pro" not in source, name


def test_veo_is_not_dragged_to_global_with_gemini():
    """Veo is served regionally; it has its own region setting."""
    assert _provider("veo").DEFAULT_REGION == "us-central1"


# --- Claude on Vertex: no sampling parameter where it 400s ------------------

@pytest.mark.parametrize("model,expect_temperature", [
    ("claude-sonnet-5", False),
    ("claude-opus-5", False),
    ("claude-haiku-4-5", True),
])
def test_claude_temperature_is_sent_only_where_the_model_accepts_it(
        monkeypatch, model, expect_temperature):
    _fake_google(monkeypatch)
    completion = _provider("completion")
    sent = _capture_posts(monkeypatch, completion, {
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"})
    asyncio.run(completion.PROVIDERS[1].generate("hi", None, 16, 0.2, model))
    assert ("temperature" in sent[0]["json"]) is expect_temperature


# --- Maps: compute_routes takes Waypoint objects ---------------------------

def test_compute_routes_sends_waypoint_objects(monkeypatch):
    maps = _provider("maps_grounding")
    calls = []

    async def fake_call_tool(self, name, args):
        calls.append((name, args))
        return {"routes": []}

    monkeypatch.setattr(type(maps.PROVIDERS[0]), "_call_tool", fake_call_tool)
    asyncio.run(maps.PROVIDERS[0].compute_routes("Austin, TX", "Dallas, TX"))
    name, args = calls[0]
    assert name == "compute_routes"
    assert args["origin"] == {"address": "Austin, TX"}
    assert args["destination"] == {"address": "Dallas, TX"}
    # Keys are snake_case, per Google's published tool schema.
    assert "travel_mode" in args


# --- BigQuery: an unfinished job is a failure, never an empty success ------

def test_an_unfinished_bigquery_job_raises_instead_of_returning_no_rows(monkeypatch):
    bigquery = _provider("bigquery")
    _fake_google(monkeypatch, bigquery.google_auth)
    provider = bigquery.PROVIDER

    async def estimate(sql):
        return 1024

    async def post(body):
        return {"jobComplete": False, "jobReference": {"jobId": "j"}}

    monkeypatch.setattr(provider, "estimate", estimate)
    monkeypatch.setattr(provider, "_post", post)
    # The provider module's own runtime: in a full run it can be a different
    # copy from W.runtime, and the class must match the one actually raised.
    with pytest.raises(bigquery.runtime.TransientProviderError):
        asyncio.run(provider.query("SELECT 1"))


def test_a_finished_bigquery_job_still_returns_its_rows(monkeypatch):
    bigquery = _provider("bigquery")
    _fake_google(monkeypatch, bigquery.google_auth)
    provider = bigquery.PROVIDER

    async def estimate(sql):
        return 1024

    async def post(body):
        return {"jobComplete": True, "schema": {"fields": [{"name": "n"}]},
                "rows": [{"f": [{"v": "7"}]}], "totalRows": "1",
                "totalBytesProcessed": "1024"}

    monkeypatch.setattr(provider, "estimate", estimate)
    monkeypatch.setattr(provider, "_post", post)
    value = asyncio.run(provider.query("SELECT 7 AS n")).value
    assert value["rows"] == [{"n": "7"}]


# --- TTS: current voice names are accepted, malformed ones are not ---------

@pytest.mark.parametrize("voice", [
    "en-US-Chirp3-HD-Aoede", "cmn-CN-Standard-A", "fil-PH-Wavenet-A",
    "en-US-Neural2-F", "en-US-Studio-O"])
def test_tts_accepts_voices_google_actually_serves(voice):
    assert _provider("tts")._VOICE_RE.match(voice)


@pytest.mark.parametrize("voice", ["en-US", "EN-us-x", "en-US-x;rm", "en-US-a-b-c-d-e"])
def test_tts_rejects_malformed_voice_names(voice):
    assert not _provider("tts")._VOICE_RE.match(voice)


# --- MCP probe: the Streamable HTTP handshake, in full ----------------------

def test_mcp_probe_completes_the_streamable_http_handshake(monkeypatch):
    probe_mod = _provider("mcp_probe")
    monkeypatch.setattr(probe_mod.web, "target_problem", lambda url: None)
    sent = []

    class _Reply(_Response):
        def __init__(self, body, headers):
            super().__init__(body)
            self.headers = {"content-type": "application/json", **headers}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append({"method": json["method"], "id": json.get("id"), "headers": headers})
            if json["method"] == "initialize":
                return _Reply({"result": {"protocolVersion": "2025-03-26",
                                          "serverInfo": {"name": "s", "version": "1"}}},
                              {"mcp-session-id": "sess-123"})
            if json["method"] == "tools/list":
                return _Reply({"result": {"tools": [{"name": "t"}]}}, {})
            return _Reply({}, {})

    monkeypatch.setattr(probe_mod.httpx, "AsyncClient", _Client)
    result = asyncio.run(probe_mod.PROVIDERS[0].inspect("https://mcp.example.com/mcp"))

    assert [s["method"] for s in sent] == ["initialize", "notifications/initialized", "tools/list"]
    assert sent[1]["id"] is None  # a notification carries no id
    for later in sent[1:]:
        assert later["headers"]["Mcp-Session-Id"] == "sess-123"
        assert later["headers"]["MCP-Protocol-Version"] == "2025-03-26"
    assert "Mcp-Session-Id" not in sent[0]["headers"]
    assert result.value["tool_count"] == 1


# --- Maps: API key, or re-scoped ADC only once proven on the deployment ----

def test_maps_stays_off_on_adc_until_the_operator_opts_in(monkeypatch):
    maps = _provider("maps_grounding")
    _fake_google(monkeypatch)
    monkeypatch.delenv("MAPS_GROUNDING_LITE_API_KEY", raising=False)
    monkeypatch.delenv("WORKER_MAPS_ADC", raising=False)
    assert maps.PROVIDERS[0].available() is False
    monkeypatch.setenv("WORKER_MAPS_ADC", "1")
    assert maps.PROVIDERS[0].available() is True


def test_maps_sends_a_maps_scoped_token_on_adc_and_the_key_when_set(monkeypatch):
    maps = _provider("maps_grounding")
    google_auth = _provider("google_auth")
    _fake_google(monkeypatch)
    scopes = []

    async def scoped_headers(scope):
        scopes.append(scope)
        return {"Authorization": "Bearer scoped", "X-Goog-User-Project": "test-project"}

    monkeypatch.setattr(google_auth, "scoped_headers", scoped_headers)
    monkeypatch.delenv("MAPS_GROUNDING_LITE_API_KEY", raising=False)
    monkeypatch.setenv("WORKER_MAPS_ADC", "1")
    headers = asyncio.run(maps.PROVIDERS[0]._auth_headers())
    assert headers == {"Authorization": "Bearer scoped", "X-Goog-User-Project": "test-project"}
    assert scopes == ["https://www.googleapis.com/auth/maps-platform.mapstools"]

    monkeypatch.setenv("MAPS_GROUNDING_LITE_API_KEY", "k")
    assert asyncio.run(maps.PROVIDERS[0]._auth_headers()) == {"X-Goog-Api-Key": "k"}


# --- Economics: published rates, reasoning billed as output ----------------

def test_gemini_cost_uses_the_published_rate_for_the_model(monkeypatch):
    gemini = _provider("gemini")
    monkeypatch.delenv("GEMINI_PRICE_PER_MTOK_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_PER_MTOK_OUT", raising=False)
    # 1M in + 1M out on the alias: the dearest current Flash rate, $1.50 + $9.00.
    assert gemini._cost_micros(1_000_000, 1_000_000, "gemini-flash-latest") == (10_500_000, True)
    assert gemini._cost_micros(1_000_000, 0, "gemini-3.5-flash-lite") == (300_000, True)
    assert gemini._cost_micros(1_000, 1_000, "some-unpriced-model") == (None, False)
    monkeypatch.setenv("GEMINI_PRICE_PER_MTOK_IN", "1")
    monkeypatch.setenv("GEMINI_PRICE_PER_MTOK_OUT", "2")
    assert gemini._cost_micros(1_000_000, 1_000_000, "gemini-flash-latest") == (3_000_000, True)


def test_reasoning_tokens_are_billed_as_output():
    gemini = _provider("gemini")
    assert gemini.output_tokens_of({"candidatesTokenCount": 10, "thoughtsTokenCount": 90}) == 100
    assert gemini.output_tokens_of({"candidatesTokenCount": 10}) == 10


def test_bigquery_is_costed_at_list_price_by_default(monkeypatch):
    bigquery = _provider("bigquery")
    monkeypatch.delenv("BQ_PRICE_PER_TIB", raising=False)
    assert bigquery._cost_micros(1024 ** 4) == (6_250_000, True)


def test_veo_refreshes_its_token_on_every_poll(monkeypatch):
    """A generation can outlive the access token fetched at submit time; a
    poll sent with the stale one is a 401 mid-job (seen live 2026-09-19)."""
    veo = _provider("veo")
    _fake_google(monkeypatch, veo.google_auth)
    monkeypatch.setattr(veo, "_POLL_INTERVAL", 0)
    calls = {"headers": 0, "polls": 0}

    async def headers():
        calls["headers"] += 1
        return {"Authorization": f"Bearer t{calls['headers']}"}

    async def post(self, url, hdrs, body):
        if url.endswith(":predictLongRunning"):
            return {"name": "op-1"}
        calls["polls"] += 1
        assert hdrs["Authorization"] == f"Bearer t{calls['headers']}", "poll sent a stale token"
        done = calls["polls"] >= 3
        return {"done": done, "response": {"videos": [{"bytesBase64Encoded": "AAAA",
                                                        "mimeType": "video/mp4"}]}} if done else {"done": False}

    monkeypatch.setattr(veo.google_auth, "headers", headers)
    monkeypatch.setattr(type(veo.PROVIDERS[0]), "_post", post)
    asyncio.run(veo.PROVIDERS[0].generate("a bee", deadline_seconds=30))
    assert calls["polls"] == 3
    assert calls["headers"] == 1 + calls["polls"]
