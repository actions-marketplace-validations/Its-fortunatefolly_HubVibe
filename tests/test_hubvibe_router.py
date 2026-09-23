"""The buyer-side router: clears a 402 with the local wallet, caches
analytical results for 24 hours, and refuses to sign past its caps.

Driven against a fake node in-process (a real HTTP server on 127.0.0.1)
that answers 402 until it sees a PAYMENT-SIGNATURE header, so no wallet,
no chain and no money are involved. Signing itself is stubbed at the one
seam where the x402 library is called.
"""

import base64
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = REPO_ROOT / "wcag-audit-engine" / "integrations" / "hubvibe_router.py"


def _load_router():
    spec = importlib.util.spec_from_file_location("hubvibe_router", ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hubvibe_router"] = module
    spec.loader.exec_module(module)
    return module


R = _load_router()


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeNode:
    """A HubVibe-shaped node: 402 (with the v2 header and a JSON body) until
    paid, then a 200 envelope with a PAYMENT-RESPONSE receipt. Counts every
    execution so cache hits are provable."""

    def __init__(self, prices: dict):
        self.prices = prices
        self.executions = []
        self.refuse_payment = False
        node = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, extra=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/work":
                    return self._send(200, {"count": len(node.prices), "workers": []})
                return self._send(404, {"detail": "Not Found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                price = node.prices.get(self.path)
                if price is None:
                    return self._send(404, {"detail": "Not Found"})
                paid = self.headers.get("PAYMENT-SIGNATURE")
                if price > 0 and (not paid or node.refuse_payment):
                    challenge = {"x402Version": 2, "accepts": [
                        {"scheme": "exact", "network": "eip155:8453", "amount": str(int(price * 1_000_000)),
                         "payTo": "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", "asset": "0x8335"},
                        {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                         "amount": str(int(price * 1_000_000)), "payTo": "J1K4", "asset": "EPjF"}]}
                    return self._send(402, {"error": "payment_required", "price_usd": price,
                                            "detail": "facilitator refused" if paid else "pay first"},
                                      {"PAYMENT-REQUIRED": _b64(challenge)})
                node.executions.append((self.path, body))
                envelope = {"status": "ok", "worker": self.path.replace("/work/", "").replace("/", "."),
                            "price_usd": price, "result": {"echo": body, "n": len(node.executions)},
                            "provenance": {"steps": [], "providers_used": [], "attempts": 1, "elapsed_ms": 1},
                            "receipt_id": "rcpt_%d" % len(node.executions),
                            "receipt_url": "/work/receipts/rcpt_%d" % len(node.executions)}
                extra = {"PAYMENT-RESPONSE": _b64({"success": True, "payer": "0xPAYER", "transaction": "0xTX",
                                                   "network": "eip155:8453"})} if price > 0 else None
                return self._send(200, envelope, extra)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def node():
    n = FakeNode({"/work/market/quote": 0.02, "/work/data/query": 0.50, "/work/research/brief": 5.00,
                  "/audit/wcag": 0.05, "/work/free/probe": 0.0})
    yield n
    n.close()


@pytest.fixture
def router(node, tmp_path, monkeypatch):
    r = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.00, budget_usd=2.00,
                 daily_cap_usd=50.00, cache_ttl=3600, home=str(tmp_path / "home"))
    signed = []

    def fake_sign(response, url):  # the one seam where the x402 library signs
        signed.append((url, R.Router._price_of(response)))
        return {"PAYMENT-SIGNATURE": "stub-signature"}

    monkeypatch.setattr(r, "_sign", fake_sign)
    r.signed = signed
    return r


# --- clearing a 402 -------------------------------------------------------------

def test_a_402_is_cleared_with_the_local_wallet_and_the_result_delivered(router, node):
    out = router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "BTC-USD"}
    assert router.signed == [(node.url + "/work/market/quote", 0.02)]
    assert node.executions == [("/work/market/quote", {"product_id": "BTC-USD"})]
    assert router.spent_usd == 0.02
    row = router.ledger()[-1]
    assert row["path"] == "/work/market/quote" and row["settled"] is True
    assert row["tx"] == "0xTX" and row["payer"] == "0xPAYER" and row["rail"] == "base"


def test_a_free_route_is_delivered_without_signing_anything(router, node):
    out = router.call("/work/free/probe", {"a": 1})
    assert out["status"] == "ok" and router.signed == [] and router.spent_usd == 0.0


def test_quote_is_free_and_names_the_rails(router, node):
    q = router.quote("/work/research/brief", {"url": "https://example.com"})
    assert q["status"] == 402 and q["price_usd"] == 5.00
    assert q["rails"] == ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"]
    assert router.signed == [] and node.executions == []


def test_a_refused_settlement_is_reported_not_swallowed(router, node):
    node.refuse_payment = True
    with pytest.raises(R.PaymentRefused) as info:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert "facilitator refused" in str(info.value)
    assert router.spent_usd == 0.0
    assert router.ledger()[-1]["settled"] is False and router.ledger()[-1]["outcome"] == "refused"


# --- caps: refused BEFORE a signature exists -------------------------------------------

def test_a_price_above_the_per_call_cap_is_never_signed(router, node):
    with pytest.raises(R.CapExceeded) as info:
        router.call("/work/research/brief", {"url": "https://example.com"})
    assert info.value.extra["cap"] == "per_call" and router.signed == []
    assert node.executions == []


def test_the_process_budget_stops_a_loop(router, node):
    for i in range(4):  # budget 2.00 at 0.50 each: four fit
        router.call("/work/data/query", {"sql": f"select {i}"})
    with pytest.raises(R.CapExceeded) as info:
        router.call("/work/data/query", {"sql": "select 99"})
    assert info.value.extra["cap"] == "process_budget"
    assert len(router.signed) == 4 and router.spent_usd == 2.00


def test_the_daily_cap_reads_the_shared_ledger_across_processes(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    first = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.0, budget_usd=100.0,
                     daily_cap_usd=0.60, home=home)
    monkeypatch.setattr(first, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    first.call("/work/data/query", {"sql": "select 1"})  # 0.50 today
    second = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.0, budget_usd=100.0,
                      daily_cap_usd=0.60, home=home)  # a new process on the same machine
    monkeypatch.setattr(second, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.CapExceeded) as info:
        second.call("/work/data/query", {"sql": "select 2"})  # would be 1.00 today
    assert info.value.extra["cap"] == "daily"


def test_caps_must_be_positive():
    with pytest.raises(R.CapExceeded):
        R.Router("http://x", evm_key="0x" + "1" * 64, max_price_usd=0)


def test_no_wallet_fails_closed_with_a_configuration_error(node, tmp_path):
    r = R.Router(node.url, home=str(tmp_path / "home"))
    with pytest.raises(R.NotConfigured):
        r.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert node.executions == []


# --- the 24-hour analytical cache -------------------------------------------------------

def test_a_repeated_analytical_query_is_served_from_cache_and_paid_once(router, node):
    body = {"sql": "select name from t limit 1"}
    first = router.call("/work/data/query", body)
    second = router.call("/work/data/query", body)
    assert first["router"]["cache"] == "miss" and second["router"]["cache"] == "hit"
    assert second["result"] == first["result"]
    assert len(node.executions) == 1 and len(router.signed) == 1 and router.spent_usd == 0.50


def test_the_cache_key_is_the_md5_of_route_and_body(router):
    import hashlib
    body = {"sql": "select 1", "max_scan_gib": 1}
    expected = hashlib.md5(json.dumps({"path": "/work/data/query", "body": body}, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert R.Router.cache_key("/work/data/query", body) == expected
    assert R.Router.cache_key("/work/data/query", {"max_scan_gib": 1, "sql": "select 1"}) == expected
    assert R.Router.cache_key("/work/data/query", {"sql": "select 2"}) != expected


def test_an_expired_cache_entry_is_bought_again(router, node):
    body = {"sql": "select 1"}
    router.call("/work/data/query", body)
    router.cache_ttl = 0
    time.sleep(0.01)
    again = router.call("/work/data/query", body)
    assert again["router"]["cache"] == "miss" and len(node.executions) == 2


def test_non_analytical_routes_are_never_cached(router, node):
    body = {"product_id": "BTC-USD"}
    out = router.call("/work/market/quote", body)
    router.call("/work/market/quote", body)
    assert len(node.executions) == 2 and "router" not in out


def test_cache_can_be_bypassed_per_call_and_cleared(router, node):
    body = {"sql": "select 1"}
    router.call("/work/data/query", body)
    router.call("/work/data/query", body, use_cache=False)
    assert len(node.executions) == 2
    assert router.cache_clear() == 1
    router.call("/work/data/query", body)
    assert len(node.executions) == 3


# --- the local proxy ----------------------------------------------------------------------

def test_the_proxy_clears_402s_for_an_agent_that_only_speaks_http(router, node):
    port = _free_port()
    threading.Thread(target=R.serve, args=(router, "127.0.0.1", port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/router/status", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    status = httpx.get(base + "/router/status").json()
    assert status["node"] == node.url and status["rail"] == "base"
    out = httpx.post(base + "/work/market/quote", json={"product_id": "BTC-USD"}, timeout=10).json()
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "BTC-USD"}
    over = httpx.post(base + "/work/research/brief", json={"url": "https://example.com"}, timeout=10)
    assert over.status_code == 402 and over.json()["reason"] == "spend_cap_exceeded"
    assert httpx.get(base + "/work", timeout=10).json()["count"] == 5
    assert httpx.get(base + "/router/ledger", timeout=10).json()["rows"][-1]["path"] == "/work/market/quote"


# --- errors never leak a local path -------------------------------------------------------

def test_errors_are_json_and_carry_no_local_paths(router, node):
    node.refuse_payment = True
    with pytest.raises(R.RouterError) as info:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    body = info.value.as_json()
    assert body["status"] == "error" and body["reason"] == "payment_refused"
    assert "/home/" not in json.dumps(body) and "home" not in body


# --- the copyable shape: HubVibeRouter(endpoint=, wallet_type=).execute_task(tool=, payload=) ---

def test_tool_names_resolve_to_routes():
    f = R.Router.route_for
    assert f("stats.probability") == "/work/stats/probability"
    assert f("market.quote") == "/work/market/quote"
    assert f("research.page_facts") == "/work/research/page_facts"
    assert f("audit.wcag") == "/audit/wcag" and f("bundle") == "/audit/bundle"
    assert f("/work/llm/generate") == "/work/llm/generate" and f("/audit/seo") == "/audit/seo"


def test_the_copyable_client_executes_a_task_by_tool_name(node, tmp_path, monkeypatch):
    monkeypatch.setenv("HUBVIBE_WALLET_KEY", "0x" + "2" * 64)
    monkeypatch.setenv("HUBVIBE_HOME", str(tmp_path / "home"))
    client = R.HubVibeRouter(endpoint=node.url, wallet_type="base")
    monkeypatch.setattr(client, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    response = client.execute_task(tool="market.quote", payload={"product_id": "BTC-USD"})
    assert response["status"] == "ok" and response["worker"] == "market.quote"
    assert response["result"]["echo"] == {"product_id": "BTC-USD"} and response["price_usd"] == 0.02
    assert client.rail == "base" and client.spent_usd == 0.02


def test_the_copyable_client_picks_the_solana_rail_by_wallet_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBVIBE_SOLANA_KEY", "not-a-real-key")
    monkeypatch.setenv("HUBVIBE_HOME", str(tmp_path / "home"))
    client = R.HubVibeRouter(endpoint="http://127.0.0.1:9", wallet_type="solana")
    assert client.rail == "solana"
    with pytest.raises(R.NotConfigured):
        R.HubVibeRouter(endpoint="http://127.0.0.1:9", wallet_type="ethereum")
