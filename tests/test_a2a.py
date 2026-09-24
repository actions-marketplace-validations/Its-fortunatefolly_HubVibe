"""The A2A surface: the Agent Card and the /a2a JSON-RPC endpoint.

Pinned: the card carries every field A2A 1.0 requires and lists exactly the
MCP tools as skills; the endpoint answers both 1.0 and 0.3 (an empty
A2A-Version header is 0.3); an unpaid SendMessage is a task in
input-required with the v2 x402 challenge in its status metadata; a real
x402 client's PaymentPayload sent back in the message metadata reaches the
one verify path and the task completes with the receipt; GetTask and the
spec's errors behave.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
TOOL = "hubvibe_predictive_probability_engine"
POINTS = {"points": [[1, 2.1], [2, 3.9], [3, 6.2], [4, 7.8], [5, 10.1]], "predict_x": [6]}
X402_EXT = "https://github.com/google-agentic-commerce/a2a-x402/blob/main/spec/v0.2"


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


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    workers = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    workers.ledger.reset_for_tests()
    workers.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_a2a", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workers = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    workers.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for)
    yield module
    workers.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _send(client, skill, arguments, version="1.0", headers=None, metadata=None,
          task_id=None, request_id=1, part_style="1.0"):
    data_part = {"data": {"skill": skill, "arguments": arguments}} if part_style == "1.0" \
        else {"kind": "data", "data": {"skill": skill, "arguments": arguments}}
    message = {"messageId": f"m{request_id}", "role": "ROLE_USER" if version == "1.0" else "user",
               "parts": [data_part]}
    if metadata:
        message["metadata"] = metadata
    if task_id:
        message["taskId"] = task_id
    method = "SendMessage" if version == "1.0" else "message/send"
    hdrs = {"A2A-Version": version} if version == "1.0" else {}
    hdrs.update(headers or {})
    return client.post("/a2a", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                     "params": {"message": message}}, headers=hdrs)


def test_the_agent_card_has_every_required_field_and_every_tool(app_module, client):
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    assert "max-age" in response.headers["cache-control"] and response.headers["etag"]
    card = response.json()
    for field in ("name", "description", "supportedInterfaces", "version", "capabilities",
                  "defaultInputModes", "defaultOutputModes", "skills"):
        assert card[field], field
    interfaces = {(i["protocolBinding"], i["protocolVersion"]) for i in card["supportedInterfaces"]}
    assert interfaces == {("JSONRPC", "1.0"), ("JSONRPC", "0.3")}
    assert all(i["url"] == "https://audit.example.test/a2a" for i in card["supportedInterfaces"])
    assert {s["id"] for s in card["skills"]} == {t["name"] for t in app_module._mcp_tools()}
    for skill in card["skills"]:
        for field in ("id", "name", "description", "tags"):
            assert skill[field], f"{skill['id']}.{field}"
    assert [e["uri"] for e in card["capabilities"]["extensions"]] == [X402_EXT]
    assert card["capabilities"]["streaming"] is False


def test_an_unpaid_send_is_input_required_with_the_x402_challenge(client):
    body = _send(client, TOOL, POINTS).json()
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    metadata = task["status"]["message"]["metadata"]
    assert metadata["x402.payment.status"] == "payment-required"
    required = metadata["x402.payment.required"]
    assert required["x402Version"] == 2
    assert required["accepts"][0]["amount"] == "500000", "priced at the worker's $0.50"
    assert task["status"]["message"]["role"] == "ROLE_AGENT"


def test_version_0_3_is_the_default_and_uses_the_0_3_shapes(client):
    body = _send(client, TOOL, POINTS, version="0.3", part_style="0.3").json()
    task = body["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "input-required"
    assert task["status"]["message"]["role"] == "agent"
    assert task["status"]["message"]["parts"][0]["kind"] == "text"


def test_an_unsupported_version_is_refused_with_the_spec_error(client):
    response = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                                         "params": {}}, headers={"A2A-Version": "2.0"})
    assert response.json()["error"]["code"] == -32009


def test_a_paid_send_by_key_completes_with_the_result_as_an_artifact(client):
    body = _send(client, TOOL, POINTS, headers={"X-API-Key": "test-key"}).json()
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["status"] == "ok" and result["worker"] == "stats.probability"
    fetched = _get(client, task["id"]).json()["result"]
    assert fetched["status"]["state"] == "TASK_STATE_COMPLETED"


def test_an_x402_client_pays_through_the_extension_and_is_receipted(app_module, client, monkeypatch):
    """The a2a-x402 standalone flow end to end with the real x402 client:
    the challenge from the task metadata is signed, the PaymentPayload goes
    back in the message metadata with the same taskId, the one verify path
    sees it priced for this skill, and the task completes with the receipt."""
    from eth_account import Account
    from x402 import max_amount, x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.schemas import PaymentRequired, SettleResponse

    first = _send(client, TOOL, POINTS).json()["result"]["task"]
    challenge = PaymentRequired.model_validate(
        first["status"]["message"]["metadata"]["x402.payment.required"])

    account = Account.from_key("0x" + "1" * 63 + "2")
    signer = x402ClientSync()
    register_exact_evm_client(signer, EthAccountSigner(account), policies=[max_amount(1_000_000)])
    payload = signer.create_payment_payload(challenge).model_dump(by_alias=True)

    seen = []
    settlement = SettleResponse(success=True, transaction="0x" + "cd" * 32,
                                network="eip155:8453", payer=account.address)

    def _verify(header, price=None, **kw):
        seen.append(price)
        return app_module.x402_payments.PendingPayment(None, None, price)

    def _settle(pending):
        pending.settle_result = settlement
        return True

    monkeypatch.setattr(app_module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(app_module.x402_payments, "settle_sync", _settle)

    paid = client.post("/a2a", headers={"A2A-Version": "1.0", "A2A-Extensions": X402_EXT}, json={
        "jsonrpc": "2.0", "id": 2, "method": "SendMessage",
        "params": {"message": {"messageId": "m2", "role": "ROLE_USER", "taskId": first["id"],
                               "parts": [{"text": "Here is the payment."}],
                               "metadata": {"x402.payment.status": "payment-submitted",
                                            "x402.payment.payload": payload}}}})
    assert paid.headers.get("A2A-Extensions") == X402_EXT
    task = paid.json()["result"]["task"]
    assert seen == ["$0.50"]
    assert task["id"] == first["id"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    metadata = task["status"]["message"]["metadata"]
    assert metadata["x402.payment.status"] == "payment-completed"
    assert metadata["x402.payment.receipts"][0]["transaction"] == settlement.transaction
    assert task["artifacts"][0]["parts"][0]["data"]["status"] == "ok"


def _get(client, task_id):
    return client.post("/a2a", headers={"A2A-Version": "1.0"},
                       json={"jsonrpc": "2.0", "id": 9, "method": "GetTask", "params": {"id": task_id}})


def test_get_task_and_cancel_follow_the_spec_errors(client):
    assert _get(client, "no-such-task").json()["error"]["code"] == -32001
    task = _send(client, TOOL, POINTS).json()["result"]["task"]
    assert _get(client, task["id"]).json()["result"]["id"] == task["id"]
    canceled = client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 3, "method": "CancelTask", "params": {"id": task["id"]}}).json()
    assert canceled["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    again = client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 4, "method": "CancelTask", "params": {"id": task["id"]}}).json()
    assert again["error"]["code"] == -32002


def test_operations_this_agent_does_not_offer_get_the_spec_codes(client):
    def call(method):
        return client.post("/a2a", headers={"A2A-Version": "1.0"},
                           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).json()
    assert call("SendStreamingMessage")["error"]["code"] == -32004
    assert call("CreateTaskPushNotificationConfig")["error"]["code"] == -32003
    assert call("GetExtendedAgentCard")["error"]["code"] == -32007
    assert call("NoSuchMethod")["error"]["code"] == -32601


def test_a_message_without_a_skill_is_told_how_to_name_one(client):
    body = client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
        "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
                               "parts": [{"text": "hello"}]}}}).json()
    assert "skill" in body["result"]["message"]["parts"][0]["text"]


def test_an_unknown_skill_is_invalid_params(client):
    assert _send(client, "no_such_skill", {}).json()["error"]["code"] == -32602


def test_a_worker_name_is_accepted_as_the_skill(client):
    task = _send(client, "stats.probability", POINTS).json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
