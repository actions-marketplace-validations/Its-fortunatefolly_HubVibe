"""stats.probability -- the predictive probability engine -- and its MCP tool.

Three things are pinned here. The arithmetic: regression, t and normal
quantities against published reference values (Anscombe's first quartet,
closed-form t distributions for df = 1 and 2, SciPy 1.17.1 values recorded
in this file) and the order-independence that makes the output a function
of the points alone. The gate: a body the engine cannot compute from is
refused before payment, and a table request is refused before payment on a
deployment without BigQuery. The surfaces: the HTTP route, the MCP tool
`hubvibe_predictive_probability_engine`, the static mcp.json and glama.json
all describe the one catalog row, and a paid MCP call returns the route's
own envelope with a receipt.
"""

import importlib.util
import json
import math
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
TOOL = "hubvibe_predictive_probability_engine"
WORKER = "stats.probability"
PATH = "/work/stats/probability"

# Anscombe's quartet, set I (Anscombe 1973). Published fit: y = 3.00 + 0.500 x,
# R^2 = 0.67, s.e. of slope 0.118, t = 4.24.
ANSCOMBE_X = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
ANSCOMBE_Y = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]

# 2 * scipy.stats.t.sf(t, df), SciPy 1.17.1.
SCIPY_TWO_SIDED_P = {
    (3, 2.5): 0.08770664700806556,
    (5, 8.0): 0.0004929066605724442,
    (10, 1.5): 0.16450732644544014,
    (30, 4.0): 0.0003818456360837564,
    (100, 2.5): 0.014045789124077172,
    (1000, 8.0): 3.4266614823914978e-15,
    (100000, 1.5): 0.13361755952283066,
}
# scipy.stats.t.ppf(0.975, df), SciPy 1.17.1.
SCIPY_T_975 = {3: 3.1824463052837078, 5: 2.5705818356363146, 10: 2.228138851986274,
               30: 2.0422724563012378, 100: 1.9839715185235518, 1000: 1.9623390808264083}


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


def _stats():
    """The stats module of the SAME package instance `W` points at. Another
    test file's fresh load replaces the sys.modules entry, so a by-name
    import would hand back a second copy whose exception classes and
    functions are not the ones W.router registered."""
    return W.skills.stats


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_stats", MAIN_PATH)
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
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _points(xs, ys):
    return [[x, y] for x, y in zip(xs, ys)]


def _compute(payload):
    stats = _stats()
    request = stats.parse(payload)
    xs, ys = request["points"]
    return stats.compute(xs, ys, request)


def _rpc_call(client, arguments, headers=None, meta=None):
    params = {"name": TOOL, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return client.post("/mcp", headers=headers or {}, json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params})


# --- the arithmetic ------------------------------------------------------------

def test_the_t_distribution_matches_closed_forms_and_scipy():
    stats = _stats()
    for t in (0.5, 1.0, 2.0, 10.0, 100.0):
        # df = 1 is the Cauchy distribution: p = 1 - (2/pi) arctan(t).
        assert stats.t_two_sided_p(t, 1) == pytest.approx(1 - 2 / math.pi * math.atan(t), abs=1e-15)
        # df = 2 has the closed form p = 1 - t / sqrt(2 + t^2).
        assert stats.t_two_sided_p(t, 2) == pytest.approx(1 - t / math.sqrt(2 + t * t), abs=1e-15)
    for (df, t), reference in SCIPY_TWO_SIDED_P.items():
        tolerance = 1e-9 if df >= 100000 else 1e-12
        assert stats.t_two_sided_p(t, df) == pytest.approx(reference, rel=tolerance), (df, t)
    for df, reference in SCIPY_T_975.items():
        assert stats.t_critical(0.05, df) == pytest.approx(reference, rel=1e-10), df
    assert stats.t_critical(0.05, 2) == pytest.approx(4.302652729911275, rel=1e-9)
    # The extremes are finite and ordered.
    assert stats.t_two_sided_p(0.0, 7) == 1.0
    assert stats.t_two_sided_p(math.inf, 7) == 0.0
    assert stats.t_critical(0.5, 3) < stats.t_critical(0.05, 3) < stats.t_critical(0.001, 3)


def test_anscombe_set_one_reproduces_the_published_fit():
    result = _compute({"points": _points(ANSCOMBE_X, ANSCOMBE_Y), "predict_x": [10],
                       "probability_queries": [{"below": 8}, {"above": 8}, {"between": [6, 9]}]})
    fit = result["linear_regression"]
    assert fit["slope"] == pytest.approx(0.500090909, abs=1e-9)
    assert fit["intercept"] == pytest.approx(3.000090909, abs=1e-9)
    assert fit["r_squared"] == pytest.approx(0.666542459, abs=1e-9)
    assert fit["slope_std_error"] == pytest.approx(0.117905500, abs=1e-9)
    assert fit["slope_t"] == pytest.approx(4.2415, abs=1e-4)
    assert fit["degrees_of_freedom"] == 9
    assert fit["f_statistic"] == pytest.approx(fit["slope_t"] ** 2)
    assert fit["slope_ci"][0] < fit["slope"] < fit["slope_ci"][1]
    assert fit["slope_ci"] == pytest.approx(
        [fit["slope"] - fit["t_critical"] * fit["slope_std_error"],
         fit["slope"] + fit["t_critical"] * fit["slope_std_error"]])

    tests = result["p_values"]
    assert tests["slope"]["p_value"] == pytest.approx(0.00216963, abs=1e-8)
    assert tests["slope"]["significant_at_alpha"] is True
    assert tests["intercept"]["p_value"] == pytest.approx(0.02573, abs=1e-5)
    assert tests["normality_of_residuals"]["test"] == "jarque_bera"
    assert 0 < tests["normality_of_residuals"]["p_value"] <= 1

    normal = result["normal_distribution"]
    assert normal["of"] == "y"
    assert normal["mean"] == pytest.approx(7.500909, abs=1e-6)
    assert normal["std_dev"] == pytest.approx(2.031568, abs=1e-6)
    assert normal["median"] == 7.58
    assert [q["p"] for q in normal["quantiles"]] == [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    assert normal["quantiles"][3]["value"] == pytest.approx(normal["mean"])
    below, above, between = (p["probability"] for p in normal["probabilities"])
    assert below + above == pytest.approx(1.0)
    assert 0 < between < 1

    (prediction,) = result["prediction"]
    assert prediction["x"] == 10.0
    assert prediction["y_hat"] == pytest.approx(8.001, abs=1e-9)
    lo, hi = prediction["mean_ci"]
    plo, phi = prediction["prediction_interval"]
    assert plo < lo < prediction["y_hat"] < hi < phi
    assert result["n"] == 11 and result["alpha"] == 0.05 and result["confidence_level"] == 0.95
    assert result["metrics"] == ["linear_regression", "normal_distribution", "p_values", "prediction"]
    assert result["notes"] == []
    assert "fsum" in result["method"]


def test_the_output_is_a_function_of_the_points_not_their_order():
    """fsum is exactly rounded, so a shuffled copy must give byte-identical
    JSON -- the property that makes the engine deterministic over rows
    BigQuery pages back in whatever order it likes."""
    import random

    rng = random.Random(7)
    points = [[i / 3, 2.5 * i + rng.gauss(0, 4)] for i in range(2000)]
    shuffled = list(points)
    rng.shuffle(shuffled)
    payload = {"predict_x": [10, 2000], "probability_queries": [{"between": [100, 900]}]}
    one = json.dumps(_compute({"points": points, **payload}), sort_keys=True)
    two = json.dumps(_compute({"points": shuffled, **payload}), sort_keys=True)
    assert one == two
    assert "NaN" not in one and "Infinity" not in one


def test_degenerate_inputs_are_answered_with_nulls_and_notes_never_nan():
    perfect = _compute({"points": [[1, 2], [2, 4], [3, 6], [4, 8]]})
    assert perfect["linear_regression"]["slope"] == 2.0
    assert perfect["linear_regression"]["sse"] == 0.0
    assert perfect["linear_regression"]["slope_t"] is None
    assert perfect["p_values"]["slope"]["p_value"] == 0.0
    assert perfect["p_values"]["normality_of_residuals"] is None
    assert any("exactly on a line" in note for note in perfect["notes"])

    constant = _compute({"points": [[1, 0.1], [2, 0.1], [3, 0.1], [4, 0.1]]})
    assert constant["linear_regression"]["slope"] == 0.0
    assert constant["linear_regression"]["r"] is None
    assert constant["linear_regression"]["r_squared"] is None
    assert constant["normal_distribution"]["std_dev"] == 0.0
    assert constant["normal_distribution"]["quantiles"] is None
    assert constant["normal_distribution"]["normality"] is None
    assert any("r and R^2 are undefined" in note for note in constant["notes"])
    for result in (perfect, constant):
        json.dumps(result, allow_nan=False)  # raises on NaN or infinity

    only_distribution = _compute({"points": [[5, 1], [5, 2]], "metrics": ["normal_distribution"],
                                  "distribution_of": "x"})
    assert only_distribution["linear_regression"] is None
    assert only_distribution["normal_distribution"]["of"] == "x"


@pytest.mark.parametrize("payload, message", [
    ({}, "exactly one source"),
    ({"points": [[1, 2]], "table": "a.b.c"}, "exactly one source"),
    ({"points": []}, "non-empty"),
    ({"points": [[1, 2], [2, 3]]}, "at least 3 points"),
    ({"points": [[1, 2], [1, 3], [1, 4]]}, "Every x is the same"),
    ({"points": [[1, 2], [2, "3"], [3, 4]]}, "must be a number"),
    ({"points": [[1, 2], [2, True], [3, 4]]}, "must be a number"),
    ({"points": [[1, 2, 3], [2, 3], [3, 4]]}, "points[0] must be"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "metrics": ["mean"]}, "`metrics` must be"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "metrics": ["prediction"]}, "needs `predict_x`"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "alpha": 1}, "`alpha` must be"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "distribution_of": "z"}, "`distribution_of`"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "probability_queries": [{"between": [3, 1]}]}, "[low, high]"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "probability_queries": [{"near": 1}]}, "probability_queries[0]"),
    ({"points": [[1, 2], [2, 3], [3, 4]], "max_rows": 5}, "applies only to `table`"),
    ({"table": "not a table", "x_column": "a", "y_column": "b"}, "fully qualified"),
    ({"table": "p.d.t", "x_column": "a; DROP", "y_column": "b"}, "plain column name"),
    ({"table": "p.d.t", "x_column": "a", "y_column": "b", "max_rows": 2}, "`max_rows`"),
])
def test_requests_the_engine_cannot_compute_from_are_refused_as_invalid(payload, message):
    stats = _stats()
    with pytest.raises(W.runtime.InvalidRequest) as refused:
        stats.parse(payload)
    assert message in str(refused.value)


def test_the_table_sql_is_deterministic_read_only_and_backtick_quoted():
    stats = _stats()
    request = stats.parse({"table": "bigquery-public-data.samples.natality",
                           "x_column": "mother_age", "y_column": "weight_pounds",
                           "max_rows": 500})
    sql = stats._table_sql(request["table"])
    assert sql.startswith("SELECT x, y, COUNT(*) OVER () AS rows_available FROM (")
    assert "FROM `bigquery-public-data.samples.natality`" in sql
    assert "CAST(`mother_age` AS FLOAT64) AS x" in sql
    assert "ORDER BY FARM_FINGERPRINT(FORMAT('%T,%T', x, y)), x, y LIMIT 500" in sql
    assert W.providers.bigquery.validate_sql(sql) == sql


# --- the catalog row and its surfaces ----------------------------------------------

def test_the_worker_is_a_catalog_row_priced_on_the_standard_tier():
    worker = W.catalog.BY_NAME[WORKER]
    assert worker.path == PATH
    assert worker.price_usd == 0.50 and worker.tier == "standard"
    assert worker.skill in W.router.REGISTRY
    assert W.router.PRECHECKS[WORKER] is _stats().precheck
    assert worker.requires == []  # inline points need no provider; a table read is prechecked
    example = W.catalog.example_for(worker)
    jsonschema.validate(example, worker.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, worker.input_schema)
    jsonschema.validate({"table": "p.d.t", "x_column": "a", "y_column": "b"}, worker.input_schema)
    # The example computes, and its result satisfies the advertised schema.
    result = _compute(example)
    jsonschema.validate({"source": {"type": "points", "table": None, "x_column": None,
                                    "y_column": None, "sql": None, "rows_available": None,
                                    "rows_used": 5, "sampled": False, "gib_processed": None},
                         **result}, worker.output_schema)


def _gate_must_not_run(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)


def test_an_uncomputable_body_is_refused_before_the_payment_gate(client, monkeypatch):
    """A credentialed call (a credential-less POST is answered with the 402
    probe before any route runs) with a body the engine cannot compute
    from gets a 400 with the reason, and the gate is never entered: nothing
    is verified, nothing billed, no nonce burned."""
    _gate_must_not_run(monkeypatch)
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"points": [[1, 1], [1, 2], [1, 3]]})
    assert response.status_code == 400
    body = response.json()
    assert body["reason"] == "invalid_request" and body["billed"] is False
    assert "Every x is the same" in body["detail"]
    assert body["input_schema"]["oneOf"]


def test_a_table_request_is_refused_before_payment_when_bigquery_is_absent(client, monkeypatch):
    """This environment has no Google credential. A table read cannot be
    delivered here, so it is refused as unavailable before the gate -- while
    inline points on the same route still quote a price."""
    assert not W.providers.bigquery.PROVIDER.available()
    challenge = client.post(PATH, json={"points": [[1, 2], [2, 4], [3, 7]]})
    assert challenge.status_code == 402
    assert challenge.json()["price_usd"] == 0.50
    _gate_must_not_run(monkeypatch)
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"table": "p.d.t", "x_column": "a", "y_column": "b"})
    assert response.status_code == 503
    assert response.json()["reason"] == "provider_unavailable"
    assert response.json()["billed"] is False


def test_a_paid_http_call_returns_the_envelope_with_a_receipt(client):
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"points": _points(ANSCOMBE_X, ANSCOMBE_Y), "predict_x": [10]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok" and body["worker"] == WORKER and body["price_usd"] == 0.50
    jsonschema.validate(body, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    assert body["result"]["source"] == {"type": "points", "table": None, "x_column": None,
                                        "y_column": None, "sql": None, "rows_available": None,
                                        "rows_used": 11, "sampled": False, "gib_processed": None}
    assert body["result"]["linear_regression"]["slope"] == pytest.approx(0.500090909, abs=1e-9)
    # The arithmetic is a recorded step with a measured cost of zero.
    assert body["provenance"]["providers_used"] == ["local-arithmetic"]
    assert body["provenance"]["attempts"] == 1
    receipt = client.get(body["receipt_url"]).json()
    assert receipt["request"]["worker"] == WORKER and receipt["execution"]["status"] == "ok"
    assert receipt["execution"]["provider_used"] == "local-arithmetic"


def test_a_table_read_computes_from_the_rows_bigquery_returns(app_module, client, monkeypatch):
    """BigQuery is stubbed at the provider; what is checked is the wiring:
    the SQL sent, the rows read as floats, the sampled flag and the source."""
    import importlib

    google_auth = importlib.import_module(W.__name__ + ".providers.google_auth")
    monkeypatch.setattr(google_auth, "_resolved", True)
    monkeypatch.setattr(google_auth, "_creds", object())
    monkeypatch.setattr(google_auth, "_project", "test-project")
    seen = {}

    async def fake_rows(self, sql, max_rows):
        seen["sql"], seen["max_rows"] = sql, max_rows
        rows = [[str(x), str(y), "500"] for x, y in zip(ANSCOMBE_X, ANSCOMBE_Y)]
        return W.runtime.ProviderResult(
            value={"rows": rows, "row_count": len(rows), "total_rows": len(rows),
                   "bytes_processed": 4096, "gib_processed": 0.0, "cache_hit": False},
            cost_micros=0, cost_measured=True, usage="bytes=4096")

    monkeypatch.setattr(type(W.providers.bigquery.PROVIDER), "rows", fake_rows)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={
        "table": "bigquery-public-data.samples.natality", "x_column": "mother_age",
        "y_column": "weight_pounds", "max_rows": 11, "metrics": ["linear_regression"]})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert seen["max_rows"] == 11 and "LIMIT 11" in seen["sql"]
    assert result["source"]["type"] == "bigquery"
    assert result["source"]["sql"] == seen["sql"]
    assert result["source"]["rows_available"] == 500 and result["source"]["rows_used"] == 11
    assert result["source"]["sampled"] is True
    assert result["linear_regression"]["slope"] == pytest.approx(0.500090909, abs=1e-9)
    assert result["p_values"] is None and result["normal_distribution"] is None
    assert any("500 usable rows" in note for note in result["notes"])
    assert response.json()["provenance"]["providers_used"] == ["bigquery", "local-arithmetic"]


# --- the MCP tool ----------------------------------------------------------------

def test_the_mcp_tool_is_listed_from_the_catalog_row(app_module, client):
    tools = {t["name"]: t for t in client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    tool = tools[TOOL]
    worker = W.catalog.BY_NAME[WORKER]
    assert tool["title"] == worker.title
    assert "$0.50 per call" in tool["description"]
    assert "linear regression" in tool["description"]
    assert "p-values" in tool["description"]
    assert tool["inputSchema"]["oneOf"] == worker.input_schema["oneOf"]
    assert tool["inputSchema"]["properties"] == worker.input_schema["properties"]
    assert tool["outputSchema"] == W.catalog.response_schema(worker)
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    jsonschema.validate(W.catalog.example_for(worker), tool["inputSchema"])
    # The served manifest and the ARD manifest carry it too.
    manifest = client.get("/mcp.json").json()
    served = next(t for t in manifest["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.50}
    assert served["outputSchema"] == tool["outputSchema"]
    card = next(e for e in client.get("/.well-known/ard.json").json()["entries"]
                if e["identifier"].endswith(":mcp:site-audits"))
    assert TOOL in card["capabilities"]


def test_the_static_manifests_on_disk_describe_the_same_tool(app_module):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    worker = W.catalog.BY_NAME[WORKER]
    for path in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
        on_disk = next(t for t in json.loads(path.read_text())["tools"] if t["name"] == TOOL)
        for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
            assert on_disk[field] == live[field], f"{path.name} {field} is stale"
        assert on_disk["httpEndpoint"] == {"method": "POST", "path": PATH,
                                           "price_usd": worker.price_usd}
    assert "predictive probability engine" in json.loads((STATIC / "mcp.json").read_text())["description"]


def test_an_unpaid_mcp_call_gets_the_worker_priced_paywall_not_a_crash(client):
    body = _rpc_call(client, {"points": [[1, 2], [2, 4], [3, 7]]}).json()
    result = body["result"]
    assert result["isError"] is True
    challenge = result["structuredContent"]
    assert challenge["price_usd"] == 0.50
    assert "$0.50" in challenge["message"] and TOOL in challenge["message"]
    assert challenge["accepts"], "the paywall must be payable"
    # The Bazaar record describes THIS tool's call, not an audit's URL body.
    example = challenge["extensions"]["bazaar"]["info"]["input"]
    assert "points" in json.dumps(example)


def test_an_uncomputable_mcp_call_is_an_error_result_that_charged_nothing(client, monkeypatch):
    _gate_must_not_run(monkeypatch)
    body = _rpc_call(client, {"points": [[1, 1], [1, 2], [1, 3]]},
                     headers={"X-API-Key": "test-key"}).json()
    result = body["result"]
    assert result["isError"] is True
    # Same convention as the audit tools: the machine-readable detail is
    # the JSON in the text content.
    detail = json.loads(result["content"][0]["text"])
    assert "Every x is the same" in detail["message"]
    assert detail["billed"] is False
    assert detail["http_status"] == 400
    assert detail["reason"] == "invalid_request"
    assert "error" not in body  # a refusal is a result, not a JSON-RPC error


def test_a_paid_mcp_call_returns_the_routes_envelope_as_structured_content(client):
    response = _rpc_call(client, {"points": _points(ANSCOMBE_X, ANSCOMBE_Y),
                                  "probability_queries": [{"below": 8}]},
                         headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    body = response.json()
    result = body["result"]
    assert result["isError"] is False
    content = result["structuredContent"]
    assert content["status"] == "ok" and content["worker"] == WORKER
    assert content["price_usd"] == 0.50
    jsonschema.validate(content, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    assert content["result"]["p_values"]["slope"]["significant_at_alpha"] is True
    assert json.loads(result["content"][0]["text"]) == content
    # Recorded like an HTTP job: the receipt exists and names the worker.
    receipt = client.get(content["receipt_url"]).json()
    assert receipt["request"]["worker"] == WORKER
    assert receipt["request"]["path"] == PATH
    assert receipt["execution"]["status"] == "ok"


def test_the_worker_tool_never_enters_the_audit_thread_pool(app_module, monkeypatch, client):
    """Its HTTP route runs on the event loop with the workers' own limiter;
    the MCP transport must not move it into the two-slot audit pool."""
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args[0].__name__ if args else None)
        raise AssertionError("worker tool went through run_in_threadpool")

    import starlette.concurrency

    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", spy)
    response = _rpc_call(client, {"points": [[1, 2], [2, 4], [3, 7]]},
                         headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert calls == []
