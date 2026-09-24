"""A2A (Agent2Agent protocol) -- the Agent Card and the JSON-RPC endpoint.

One endpoint, /a2a, speaks A2A 1.0 (PascalCase methods, ProtoJSON enums)
and 0.3 (slash methods, lowercase states, `kind` on every object). The
spec picks the version from the `A2A-Version` header, and an empty header
means 0.3 (A2A 1.0.1, section 3.6).

Every skill is an MCP tool this node already sells: a SendMessage names
one (`{"skill": ..., "arguments": {...}}` in a data part or in the message
metadata) and is handed to the same tools/call machinery /mcp runs, so the
gate, the price, billing, the receipt and the ledger row are that path's,
not a second copy. Payment rides the a2a-x402 extension (v0.2, standalone
flow: the v2 PaymentRequired in the task's status metadata, the signed
PaymentPayload back in the message metadata) or the ordinary HTTP payment
headers on the POST.

This module is pure shaping -- no I/O -- so main.py owns the wiring.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

X402_EXTENSION_URI = "https://github.com/google-agentic-commerce/a2a-x402/blob/main/spec/v0.2"
SUPPORTED_VERSIONS = ("1.0", "0.3")

# Method name -> operation, in both versions' spellings.
_OPERATIONS = {
    "SendMessage": "send", "message/send": "send",
    "GetTask": "get", "tasks/get": "get",
    "CancelTask": "cancel", "tasks/cancel": "cancel",
}
# Real A2A methods this node does not offer (no streaming, no push, no
# listing, no extended card): the spec's own errors, not "method not found".
_UNSUPPORTED = {
    "SendStreamingMessage": -32004, "message/stream": -32004,
    "SubscribeToTask": -32004, "tasks/resubscribe": -32004,
    "ListTasks": -32004, "tasks/list": -32004,
    "CreateTaskPushNotificationConfig": -32003, "GetTaskPushNotificationConfig": -32003,
    "ListTaskPushNotificationConfigs": -32003, "DeleteTaskPushNotificationConfig": -32003,
    "tasks/pushNotificationConfig/set": -32003, "tasks/pushNotificationConfig/get": -32003,
    "tasks/pushNotificationConfig/list": -32003, "tasks/pushNotificationConfig/delete": -32003,
    "GetExtendedAgentCard": -32007, "agent/getAuthenticatedExtendedCard": -32007,
}


def negotiate_version(requested: Optional[str]) -> Optional[str]:
    """The Major.Minor this request speaks, or None when it is not served."""
    value = (requested or "").strip()
    if not value:
        return "0.3"
    major_minor = ".".join(value.split(".")[:2])
    return major_minor if major_minor in SUPPORTED_VERSIONS else None


def operation(method) -> tuple:
    """(operation, error code): ('send', None), (None, -32004), (None, -32601)."""
    if method in _OPERATIONS:
        return _OPERATIONS[method], None
    return None, _UNSUPPORTED.get(method, -32601)


def error(request_id, code: int, message: str, reason: Optional[str] = None) -> dict:
    body = {"code": code, "message": message}
    if reason:
        body["data"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                         "reason": reason, "domain": "a2a-protocol.org"}]
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def build_card(*, base_url: str, name: str, description: str, version: str,
               tools: list, tags_for, example_for) -> dict:
    """The Agent Card: one skill per sellable MCP tool, both protocol versions
    at /a2a, and the x402 payment extension."""
    skills = []
    for tool in tools:
        example = json.dumps({"skill": tool["name"], "arguments": example_for(tool["name"])})
        skills.append({
            "id": tool["name"],
            "name": tool.get("title") or tool["name"],
            "description": tool["description"],
            "tags": list(tags_for(tool["name"])),
            "examples": [example],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        })
    endpoint = f"{base_url}/a2a"
    return {
        "name": name,
        "description": description,
        "supportedInterfaces": [
            {"url": endpoint, "protocolBinding": "JSONRPC", "protocolVersion": v}
            for v in SUPPORTED_VERSIONS
        ],
        "provider": {"organization": "HubVibe", "url": base_url},
        "version": version,
        "documentationUrl": f"{base_url}/llms.txt",
        "iconUrl": f"{base_url}/favicon.svg",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [{
                "uri": X402_EXTENSION_URI,
                "description": (
                    "Every skill is paid per call with x402 (USDC on Base or Solana). "
                    "An unpaid SendMessage returns the task in input-required with the "
                    "price in x402.payment.required; send the signed PaymentPayload in "
                    "the next message's x402.payment.payload with the same taskId. "
                    "X-PAYMENT, PAYMENT-SIGNATURE and X-API-Key headers work too."
                ),
                "required": False,
            }],
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
    }


def parse_message(message: dict) -> dict:
    """What a SendMessage asks for: skill, arguments, payment, ids, text.

    Reads both versions' parts: 1.0's `{"data": ...}` / `{"text": ...}` and
    0.3's `{"kind": "data", "data": ...}` / `{"kind": "text", ...}`."""
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    skill = metadata.get("skill")
    arguments = metadata.get("arguments") if isinstance(metadata.get("arguments"), dict) else None
    texts = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict) and "skill" in data:
            skill = data["skill"]
            if isinstance(data.get("arguments"), dict):
                arguments = data["arguments"]
            else:
                arguments = {k: v for k, v in data.items() if k != "skill"}
        elif isinstance(part.get("text"), str):
            texts.append(part["text"])
    payload = metadata.get("x402.payment.payload")
    return {
        "skill": skill if isinstance(skill, str) else None,
        "arguments": arguments,
        "payment": payload if isinstance(payload, dict) else None,
        "task_id": message.get("taskId") if isinstance(message.get("taskId"), str) else None,
        "context_id": message.get("contextId") if isinstance(message.get("contextId"), str) else None,
        "text": " ".join(texts),
    }


def outcome(result: dict, *, paid: bool) -> dict:
    """An MCP tools/call result as a task's state, words, metadata and artifact."""
    structured = result.get("structuredContent")
    meta = result.get("_meta") or {}
    if not result.get("isError"):
        receipt = meta.get("x402/payment-response")
        md = {}
        if receipt:
            md = {"x402.payment.status": "payment-completed", "x402.payment.receipts": [receipt]}
        return {"state": "completed", "text": "Done.", "metadata": md, "data": structured}
    if isinstance(structured, dict) and isinstance(structured.get("accepts"), list) \
            and "x402Version" in structured:
        md = {"x402.payment.status": "payment-failed" if paid else "payment-required",
              "x402.payment.required": structured}
        if paid:
            md["x402.payment.error"] = structured.get("error") or "SETTLEMENT_FAILED"
            md["x402.payment.receipts"] = []
        price = structured.get("price_usd")
        words = f"Payment is required: ${price:.2f}." if isinstance(price, (int, float)) \
            else "Payment is required."
        return {"state": "input-required", "text": words, "metadata": md, "data": None}
    text = ""
    for item in result.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = item["text"]
            break
    try:
        detail = json.loads(text)
    except (TypeError, ValueError):
        detail = None
    words = detail.get("message") if isinstance(detail, dict) and detail.get("message") else text
    md = {"billed": False}
    if paid:
        md.update({"x402.payment.status": "payment-failed",
                   "x402.payment.error": "NOT_SETTLED", "x402.payment.receipts": []})
    return {"state": "failed", "text": words or "The job could not run. Nothing was charged.",
            "metadata": md, "data": detail if isinstance(detail, dict) else None}


def new_task(task_id: Optional[str], context_id: Optional[str], skill: Optional[str],
             arguments: Optional[dict], result: dict) -> dict:
    return {
        "id": task_id or str(uuid.uuid4()),
        "contextId": context_id or str(uuid.uuid4()),
        "skill": skill, "arguments": arguments,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        **result,
    }


def _state(state: str, version: str) -> str:
    return "TASK_STATE_" + state.upper().replace("-", "_") if version == "1.0" else state


def _agent_message(text: str, metadata: dict, version: str, task: Optional[dict] = None) -> dict:
    message = {"messageId": str(uuid.uuid4()),
               "role": "ROLE_AGENT" if version == "1.0" else "agent",
               "parts": [{"text": text} if version == "1.0" else {"kind": "text", "text": text}]}
    if task is not None:
        message["taskId"], message["contextId"] = task["id"], task["contextId"]
    if metadata:
        message["metadata"] = metadata
    if version != "1.0":
        message["kind"] = "message"
    return message


def task_json(task: dict, version: str) -> dict:
    body = {
        "id": task["id"], "contextId": task["contextId"],
        "status": {"state": _state(task["state"], version),
                   "message": _agent_message(task["text"], task.get("metadata") or {}, version, task),
                   "timestamp": task["timestamp"]},
    }
    if task.get("data") is not None:
        part = {"data": task["data"], "mediaType": "application/json"} if version == "1.0" \
            else {"kind": "data", "data": task["data"]}
        body["artifacts"] = [{"artifactId": f"{task['id']}-result",
                              "name": task.get("skill") or "result", "parts": [part]}]
    if version != "1.0":
        body["kind"] = "task"
    return body


def send_result(task: dict, version: str) -> dict:
    """SendMessage's result: 1.0 wraps the task (`{"task": ...}`), 0.3 does not."""
    return {"task": task_json(task, version)} if version == "1.0" else task_json(task, version)


def reply(text: str, version: str) -> dict:
    """SendMessage answered with a message rather than a task."""
    message = _agent_message(text, {}, version)
    return {"message": message} if version == "1.0" else message


class TaskStore:
    """Recent tasks, in memory: GetTask, and a paid follow-up that names only
    its taskId. One uvicorn process serves this node, so one dict is the
    whole store; a restart forgets tasks, and GetTask then says not found."""

    def __init__(self, ttl_seconds: int = 900, max_tasks: int = 2000):
        self._ttl, self._max = ttl_seconds, max_tasks
        self._tasks: dict = {}
        self._lock = threading.Lock()

    def put(self, task: dict) -> None:
        now = time.monotonic()
        with self._lock:
            self._tasks[task["id"]] = (now, task)
            if len(self._tasks) > self._max:
                for key, (stamp, _) in sorted(self._tasks.items(), key=lambda kv: kv[1][0]):
                    if len(self._tasks) <= self._max:
                        break
                    del self._tasks[key]

    def get(self, task_id) -> Optional[dict]:
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return None
            if time.monotonic() - entry[0] > self._ttl:
                del self._tasks[task_id]
                return None
            return entry[1]
