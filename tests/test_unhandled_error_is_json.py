"""An exception nothing else caught still answers in JSON.

Starlette's default for an unhandled exception is a 500 with a text/plain
body, "Internal Server Error". A buying agent or a registry crawler reading
that cannot tell a crashed node from a blocked one, and cannot tell whether
it was charged. The catch-all in main.py keeps the 500 (it is a server
fault) but answers `{"status": "error", "reason": "internal_error",
"billed": false}`, and JSON-RPC shape on /mcp.
"""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"


def _load_main(monkeypatch):
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("wcag_audit_main_unhandled", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _boom_route(module, path):
    async def boom():
        raise RuntimeError("simulated provider fault")

    module.app.add_api_route(path, boom, methods=["GET", "POST"])


def test_an_unhandled_exception_answers_in_json_with_billed_false(monkeypatch):
    module = _load_main(monkeypatch)
    _boom_route(module, "/__test_boom")
    client = TestClient(module.app, raise_server_exceptions=False)

    response = client.get("/__test_boom")

    assert response.status_code == 500
    assert "application/json" in response.headers["content-type"]
    body = response.json()
    assert body["status"] == "error"
    assert body["reason"] == "internal_error"
    assert body["billed"] is False
    assert "RuntimeError" in body["detail"]
    assert "Internal Server Error" not in response.text


def test_an_unhandled_exception_on_mcp_is_a_json_rpc_internal_error(monkeypatch):
    module = _load_main(monkeypatch)
    # Force the /mcp handler itself to fault after routing, so the catch-all
    # is the only thing between the exception and the client.
    monkeypatch.setattr(module, "_mcp_tools", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    client = TestClient(module.app, raise_server_exceptions=False)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"})

    assert response.status_code == 500
    assert "application/json" in response.headers["content-type"]
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32603
    assert "Internal Server Error" not in response.text
