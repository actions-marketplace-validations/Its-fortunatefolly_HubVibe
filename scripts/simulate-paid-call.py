#!/usr/bin/env python3
"""Run the first paid call end to end, for free, against a facilitator that
verifies signatures for real.

WHY THIS EXISTS

Every x402 test in tests/ mocks the x402 library's server object, so none of
them has ever pushed a REAL signed EIP-3009 authorization through the REAL
x402 HTTP facilitator client, into a facilitator, and back out as a settled
audit. The two live attempts on 2026-09-02 were the first time that path ran
at all, and both died inside this node before the facilitator was called --
bugs (#82, #83) that no mocked test could see, because the mocks stood in for
exactly the code that failed.

This script is the missing layer. It boots the real service on localhost with
the live x402 configuration, points it at a stub facilitator, and drives it
with `scripts/first-paid-call.sh` -- the same script the owner runs in Cloud
Shell, the same client library, the same wallet handling. The stub is not a
rubber stamp: /verify and /settle recover the EIP-712 signer from the
signature the client sent (the same check the x402 facilitator does, minus
the on-chain balance), reject a wrong recipient or amount or window, refuse a
nonce that was already settled, and catalog the Bazaar record the way the
spec says a facilitator should (validated with the x402 library's own
facilitator-side validator). What a real facilitator would reject, this
rejects.

Before the paid call it runs one API-key audit, so the worker thread that
later serves the payment already hosts Playwright's event loop -- the exact
state that killed the live verify in #83. A regression there shows up here
as a bare 402, not in the owner's wallet.

What it proves, when it exits 0:

  * the deployed 402 is payable by the real client library (v2 header path)
  * the signature the client produces verifies against the challenge
  * the node calls /verify BEFORE the audit and /settle AFTER it, once each,
    with the same payload -- so a failed audit is never charged and a
    delivered one always is
  * the Bazaar record rides the payment payload and survives validation
  * the 200 carries a PAYMENT-RESPONSE receipt with the settlement tx hash
  * the MCP paywall is payable by the x402 MCP client shape: the v2 challenge
    is read out of the tool result's structuredContent, the payment goes in
    params._meta["x402/payment"], the receipt comes back in _meta, and the
    facilitator catalogs the tool as an "mcp" Bazaar resource
  * the delivered MCP result carries structuredContent that validates against
    the outputSchema the tool advertises -- the official MCP SDK client raises
    on a non-error result that lacks it, after the payment has settled
  * nothing in the node raised "cannot be called from a running event loop"
  * a replayed signature is refused without reaching the facilitator
  * sixteen concurrent payers against a KEEP-ALIVE facilitator all settle
    (the connection-reuse-across-event-loops bug rejected 56 of 96)
  * with the facilitator unreachable the node answers fast, offers no x402,
    and does not re-probe the outage on every request
  * a second node with the target gate on refuses metadata, loopback,
    private and non-http URLs with a 400 before any payment is read, and
    refuses an oversized html body

What it cannot prove: that USDC moves. Only money proves that. It removes
every reason short of money for the live call to fail.

Usage:
    bash -c 'python3 scripts/simulate-paid-call.py'

Needs the service's dependencies and a Chromium Playwright can launch
(`python -m playwright install chromium`). Exit 0 = every check passed.

SIM_NODE_PYTHON=/path/to/other/python boots the NODE on a different
interpreter than the one paying, so a client on one x402 release can be
proved against a node on another -- agents in the wild run whatever x402
they installed, not the one this repo pins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "wcag-audit-engine"

# What facilitator.xpay.sh/supported returned to the owner on 2026-09-02, read
# off their screen and recorded in docs/HANDOFF.md. Mirrored exactly so the
# node's version gate (#82) sees the live facilitator's vocabulary.
SUPPORTED = {
    "kinds": [
        {"x402Version": 2, "scheme": "exact", "network": "eip155:8453"},
        {"x402Version": 2, "scheme": "exact", "network": "eip155:84532"},
        {"x402Version": 1, "scheme": "exact", "network": "base"},
        {"x402Version": 1, "scheme": "exact", "network": "base-sepolia"},
    ],
    "extensions": [],
    "signers": {"eip155:*": ["0x2772F7F74ac0aCA38C6238aA5EcE72B27bEB8C17"]},
}

# CAIP-2 <-> the legacy v1 name. A v1 body says "base"; every requirements
# object this node builds says "eip155:8453". Both mean chain 8453.
_LEGACY_NAMES = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}

PAGE_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>simulated target</title></head>"
    "<body><main><h1>Paid-call target</h1><p>Static, local, audited.</p></main>"
    "</body></html>"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# The stub facilitator
# ---------------------------------------------------------------------------


class FacilitatorState:
    """Everything the stub records, so the harness can assert on it."""

    def __init__(self, index: bool):
        self.lock = threading.Lock()
        self.log: list[dict] = []
        self.used_nonces: set[str] = set()
        self.catalog: list[dict] = []
        self.index = index

    def record(self, entry: dict) -> None:
        with self.lock:
            self.log.append(entry)


def _normalise(body: dict) -> dict:
    """Pull the fields a facilitator checks out of a v1 or v2 request body."""
    version = int(body.get("x402Version") or 0)
    payload = body.get("paymentPayload") or {}
    req = body.get("paymentRequirements") or {}
    inner = payload.get("payload") or {}
    if version == 2:
        accepted = payload.get("accepted") or {}
        network = accepted.get("network")
        req_amount = req.get("amount")
    else:
        accepted = {"scheme": payload.get("scheme"), "network": payload.get("network")}
        network = payload.get("network")
        req_amount = req.get("maxAmountRequired")
    return {
        "version": version,
        "scheme": accepted.get("scheme"),
        "network": network,
        "req_network": req.get("network"),
        "pay_to": req.get("payTo"),
        "amount": req_amount,
        "asset": req.get("asset"),
        "extra": req.get("extra") or {},
        "authorization": inner.get("authorization") or {},
        "signature": inner.get("signature"),
        "extensions": payload.get("extensions") or {},
        "resource": payload.get("resource") or {},
        "payload": payload,
    }


def _check_payment(fields: dict) -> str | None:
    """The facilitator's checks, minus chain state. None means valid."""
    from x402.mechanisms.evm.eip712 import hash_eip3009_authorization
    from x402.mechanisms.evm.types import ExactEIP3009Authorization
    from x402.mechanisms.evm.utils import get_evm_chain_id, hex_to_bytes
    from x402.mechanisms.evm.verify import verify_eoa_signature

    if fields["scheme"] != "exact":
        return "unsupported_scheme"
    net = _LEGACY_NAMES.get(fields["network"], fields["network"])
    req_net = _LEGACY_NAMES.get(fields["req_network"], fields["req_network"])
    if net != req_net:
        return "network_mismatch"
    auth = fields["authorization"]
    if not auth or not fields["signature"]:
        return "invalid_signature"
    if (auth.get("to") or "").lower() != (fields["pay_to"] or "").lower():
        return "recipient_mismatch"
    if int(auth.get("value", -1)) != int(fields["amount"]):
        return "invalid_exact_evm_payload_authorization_value_mismatch"
    now = int(time.time())
    if int(auth["validBefore"]) < now + 6:
        return "invalid_exact_evm_payload_authorization_valid_before"
    if int(auth["validAfter"]) > now:
        return "invalid_exact_evm_payload_authorization_valid_after"
    extra = fields["extra"]
    if "name" not in extra or "version" not in extra:
        return "missing_eip712_domain"
    digest = hash_eip3009_authorization(
        ExactEIP3009Authorization(
            from_address=auth["from"],
            to=auth["to"],
            value=str(auth["value"]),
            valid_after=str(auth["validAfter"]),
            valid_before=str(auth["validBefore"]),
            nonce=auth["nonce"],
        ),
        get_evm_chain_id(net),
        fields["asset"],
        extra["name"],
        extra["version"],
    )
    if not verify_eoa_signature(digest, hex_to_bytes(fields["signature"]), auth["from"]):
        return "invalid_signature"
    return None


def _catalog(state: FacilitatorState, fields: dict) -> str | None:
    """Do what the Bazaar spec says: validate the extension, then index it.

    Uses the x402 library's own facilitator-side validator -- the one that
    rejected every record this node emitted before #52 -- so a record that
    passes here passes a real Bazaar.
    """
    ext = fields["extensions"].get("bazaar") if fields["extensions"] else None
    if not ext:
        return "no bazaar extension on the payment payload"
    from x402.extensions.bazaar.facilitator import validate_and_extract

    result = validate_and_extract(ext)
    if not result.valid:
        return "bazaar record rejected: " + "; ".join(result.errors)
    url = fields["resource"].get("url") or ""
    with state.lock:
        state.catalog.append(
            {
                "resourceUrl": url,
                "type": ((ext.get("info") or {}).get("input") or {}).get("type"),
                "x402Version": fields["version"],
                "accepts": [fields["payload"].get("accepted") or {}],
                "lastUpdated": int(time.time()),
                "metadata": {
                    "serviceName": fields["resource"].get("serviceName"),
                    "tags": fields["resource"].get("tags"),
                },
            }
        )
    return None


def _make_handler(state: FacilitatorState, pay_to: str):
    class Handler(BaseHTTPRequestHandler):
        # Keep-alive, like every production facilitator. With HTTP/1.0 (the
        # default) each connection closes after one response, and that hid the
        # cross-event-loop connection reuse bug: 56 of 96 concurrent payments
        # rejected by the node against a keep-alive stub, zero against this
        # one before the change. A stub that is easier on the code than the
        # real thing is not a simulation.
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):  # quiet
            pass

        def _send(self, status: int, body, content_type="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            path = self.path.split("?")[0]
            state.record({"method": "GET", "path": path})
            if path == "/supported":
                return self._send(200, SUPPORTED)
            if path == "/health":
                return self._send(200, {"status": "ok", "service": "stub-facilitator"})
            if path == "/discovery/resources":
                if not state.index:
                    # xpay.sh, verbatim: HTTP 200 and this body.
                    return self._send(200, {"message": "Not Found"})
                with state.lock:
                    items = list(state.catalog)
                return self._send(
                    200,
                    {
                        "x402Version": 2,
                        "items": items,
                        "pagination": {"limit": 100, "offset": 0, "total": len(items)},
                    },
                )
            if path == "/page":
                return self._send(200, PAGE_HTML.encode(), "text/html; charset=utf-8")
            return self._send(404, {"message": "Not Found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._read_json()
            if path == "/rpc":
                # first-paid-call.sh's balance check: one USDC, always.
                return self._send(
                    200,
                    {"jsonrpc": "2.0", "id": body.get("id"), "result": "0x" + format(1_000_000, "064x")},
                )
            if path not in ("/verify", "/settle"):
                state.record({"method": "POST", "path": path})
                return self._send(404, {"message": "Not Found"})

            fields = _normalise(body)
            reason = _check_payment(fields)
            payer = fields["authorization"].get("from")
            entry = {
                "method": "POST",
                "path": path,
                "x402Version": fields["version"],
                "network": fields["network"],
                "pay_to": fields["pay_to"],
                "amount": fields["amount"],
                "nonce": fields["authorization"].get("nonce"),
                "signature": fields["signature"],
                "payer": payer,
                "reason": reason,
                "resource_url": fields["resource"].get("url"),
            }

            if path == "/verify":
                if reason is None:
                    entry["bazaar"] = _catalog(state, fields)
                state.record(entry)
                if reason:
                    return self._send(200, {"isValid": False, "invalidReason": reason, "payer": payer})
                return self._send(200, {"isValid": True, "payer": payer})

            # /settle
            nonce = fields["authorization"].get("nonce")
            if reason is None:
                with state.lock:
                    if nonce in state.used_nonces:
                        reason = "nonce_already_used"
                    else:
                        state.used_nonces.add(nonce)
            if reason:
                entry["reason"] = reason
                state.record(entry)
                return self._send(
                    200,
                    {
                        "success": False,
                        "errorReason": reason,
                        "payer": payer,
                        "transaction": "",
                        "network": fields["req_network"],
                    },
                )
            tx = "0x" + hashlib.sha256((fields["signature"] or "").encode()).hexdigest()
            entry["transaction"] = tx
            state.record(entry)
            return self._send(
                200,
                {
                    "success": True,
                    "payer": payer,
                    "transaction": tx,
                    "network": fields["req_network"],
                    "amount": str(fields["amount"]),
                },
            )

    return Handler


def start_facilitator(port: int, pay_to: str, index: bool) -> FacilitatorState:
    state = FacilitatorState(index=index)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state, pay_to))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


def start_node(port: int, env: dict, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "wb")
    node_python = os.environ.get("SIM_NODE_PYTHON") or sys.executable
    proc = subprocess.Popen(
        [node_python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=SERVICE_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.3)
    log.flush()
    sys.stdout.write(log_path.read_text(errors="replace")[-3000:])
    raise SystemExit("STOP  the node did not come up on port %d" % port)


def _post(url: str, body: dict, headers: dict | None = None, timeout=90):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {"raw": raw[:500].decode(errors="replace")}
        return exc.code, dict(exc.headers), parsed


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, what: str) -> None:
        self.passed += 1
        print(f"  \033[32mPASS\033[0m  {what}")

    def fail(self, what: str) -> None:
        self.failed += 1
        print(f"  \033[31mFAIL\033[0m  {what}")

    def expect(self, cond: bool, what: str) -> bool:
        (self.ok if cond else self.fail)(what)
        return cond


def step(msg: str) -> None:
    print(f"\n\033[1m==> {msg}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="make the stub answer /discovery/resources the way xpay.sh does "
        "({'message':'Not Found'}), to exercise that branch of first-paid-call.sh",
    )
    parser.add_argument("--keep", action="store_true", help="leave the node log in place")
    args = parser.parse_args()

    from eth_account import Account

    checks = Checks()
    work = Path(os.environ.get("SIM_WORKDIR") or (REPO / ".sim-paid-call"))
    work.mkdir(exist_ok=True)
    node_log = work / "node.log"

    payer = Account.create()
    recipient = Account.create().address  # a wallet nobody holds; never the payer
    wallet_file = work / "wallet-key"
    wallet_file.write_text(payer.key.hex() if payer.key.hex().startswith("0x") else "0x" + payer.key.hex())
    wallet_file.chmod(0o600)

    fac_port, node_port = _free_port(), _free_port()
    facilitator = f"http://127.0.0.1:{fac_port}"
    base = f"http://127.0.0.1:{node_port}"

    step("Starting the stub facilitator (real signature checks, Bazaar index=%s)" % (not args.no_index))
    state = start_facilitator(fac_port, recipient, index=not args.no_index)
    print(f"  facilitator {facilitator}  recipient {recipient}  payer {payer.address}")

    step("Booting the node with the live x402 configuration")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("STRIPE_", "MPP_", "X402_", "GOOGLE_", "FIRESTORE"))
    }
    env.update(
        {
            "X402_FACILITATOR_URL": facilitator,
            "X402_PAY_TO_ADDRESS": recipient,
            "PUBLIC_BASE_URL": base,
            "AUDIT_API_KEY": "simulate-paid-call",
            # The audited page lives on 127.0.0.1 here. The deployed node
            # refuses such targets (see _target_url_problem); the gate itself
            # is exercised below on a second node WITHOUT this override.
            "ALLOW_PRIVATE_TARGETS": "1",
            # One worker thread: the thread the API-key audit poisons IS the
            # thread the paid call lands on. This is the #83 reproduction.
            "MAX_CONCURRENT_AUDITS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    node = start_node(node_port, env, node_log)
    print(f"  node {base}  log {node_log}")

    try:
        step("Poisoning the worker thread with one API-key audit (the #83 precondition)")
        status, _, body = _post(
            f"{base}/audit/wcag",
            {"html": PAGE_HTML},
            headers={"X-API-Key": "simulate-paid-call"},
        )
        if not checks.expect(status == 200, f"an API-key audit runs on this machine (HTTP {status})"):
            print("        " + json.dumps(body)[:400])
            print("        The paid call needs a working browser. Install one with:")
            print("            python -m playwright install chromium")
            return 1

        step("Reading the 402 the paying client will see")
        status, headers, body = _post(f"{base}/audit/wcag", {"url": f"{facilitator}/page"})
        checks.expect(status == 402, f"unpaid call is challenged (HTTP {status})")
        lower = {k.lower(): v for k, v in headers.items()}
        checks.expect("payment-required" in lower, "402 carries the v2 PAYMENT-REQUIRED header")
        accepts = body.get("accepts") or []
        checks.expect(
            any(a.get("scheme") == "exact" and a.get("payTo") == recipient for a in accepts),
            "402 body carries a payable v1 accepts[] entry naming the recipient",
        )

        step("Running scripts/first-paid-call.sh against it -- the owner's exact script")
        run_env = dict(os.environ)
        run_env.update(
            {
                "BASE": base,
                "FACILITATOR": facilitator,
                "TARGET_URL": f"{facilitator}/page",
                "HUBVIBE_WALLET_FILE": str(wallet_file),
                "BASE_RPC": f"{facilitator}/rpc",
                # The script shells out to bare python3; make that THIS interpreter.
                "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
            }
        )
        run_env.pop("HUBVIBE_WALLET_KEY", None)
        result = subprocess.run(
            ["bash", str(REPO / "scripts" / "first-paid-call.sh")],
            cwd=REPO,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        for line in (result.stdout + result.stderr).rstrip().splitlines():
            print("      | " + line)
        checks.expect(result.returncode == 0, f"first-paid-call.sh exited {result.returncode}")

        step("What the facilitator saw")
        with state.lock:
            log = list(state.log)
            catalog = list(state.catalog)
        paths = [e["path"] for e in log]
        verifies = [e for e in log if e["path"] == "/verify"]
        settles = [e for e in log if e["path"] == "/settle"]
        print("  sequence: " + " -> ".join(paths))

        checks.expect("/supported" in paths, "node read /supported before advertising anything")
        checks.expect(len(verifies) == 1, f"exactly one /verify for one paid call (got {len(verifies)})")
        checks.expect(len(settles) == 1, f"exactly one /settle for one delivered audit (got {len(settles)})")
        if verifies and settles:
            v, s = verifies[0], settles[0]
            checks.expect(
                paths.index("/verify") < paths.index("/settle"),
                "verify happened before settle (audit ran in between)",
            )
            checks.expect(v["reason"] is None, f"the signature VERIFIED (reason={v['reason']})")
            checks.expect(s["reason"] is None, f"the payment SETTLED (reason={s['reason']})")
            checks.expect(
                v["signature"] == s["signature"] and v["nonce"] == s["nonce"],
                "settle carried the same signed authorization that was verified",
            )
            checks.expect(
                v["pay_to"].lower() == recipient.lower(),
                f"payTo is the configured recipient ({v['pay_to']})",
            )
            checks.expect(str(v["amount"]) == "50000", f"amount is $0.05 in atomic USDC ({v['amount']})")
            checks.expect(v["payer"].lower() == payer.address.lower(), "payer is the funded wallet, not the recipient")
            checks.expect(v["x402Version"] == 2, f"the client paid with x402 v{v['x402Version']} (the header path)")
            checks.expect(
                (v.get("resource_url") or "") == f"{base}/audit/wcag",
                f"resource.url names the paid route ({v.get('resource_url')})",
            )
            checks.expect(v.get("bazaar") is None, f"Bazaar record accepted by the x402 validator ({v.get('bazaar')})")
            if not args.no_index:
                checks.expect(
                    len(catalog) == 1 and catalog[0]["resourceUrl"] == f"{base}/audit/wcag",
                    "the node is now in the facilitator's Bazaar index",
                )

        step("Receipt: does the 200 hand the payer the settlement?")
        receipt_tx, pay_headers = _paid_call_receipt(base, payer, f"{facilitator}/page")
        with state.lock:
            second_settle = [e for e in state.log if e["path"] == "/settle"]
        tx2 = second_settle[-1].get("transaction") if len(second_settle) == 2 else None
        checks.expect(
            receipt_tx is not None and tx2 is not None and receipt_tx == tx2,
            f"PAYMENT-RESPONSE header carries the settlement transaction ({receipt_tx})",
        )

        step("MCP: the x402 MCP client shape -- v2 challenge in structuredContent, payment in _meta")
        mcp_tx, mcp_result = _mcp_paid_call(base, payer, f"{facilitator}/page")
        with state.lock:
            mcp_settles = [e for e in state.log if e["path"] == "/settle"]
            mcp_verifies = [e for e in state.log if e["path"] == "/verify"]
            catalog_now = list(state.catalog)
        last_settle = mcp_settles[-1] if mcp_settles else {}
        checks.expect(
            mcp_result is not None and mcp_result.get("isError") is False,
            "the paid MCP tool call returned a result (isError=%s)" % (mcp_result or {}).get("isError"),
        )
        checks.expect(
            mcp_tx is not None and mcp_tx == last_settle.get("transaction"),
            f"_meta['x402/payment-response'] carries the settlement transaction ({mcp_tx})",
        )
        checks.expect(
            len(mcp_verifies) == 3 and len(mcp_settles) == 3,
            f"the MCP payment was verified and settled once each (verify={len(mcp_verifies)}, settle={len(mcp_settles)})",
        )
        checks.expect(
            (last_settle.get("resource_url") or "") == f"{base}/mcp",
            f"the MCP payment names the MCP endpoint as its resource ({last_settle.get('resource_url')})",
        )
        if not args.no_index:
            checks.expect(
                any(c["type"] == "mcp" and c["resourceUrl"] == f"{base}/mcp" for c in catalog_now),
                "the MCP tool is in the facilitator's Bazaar index as an mcp resource",
            )
        schema_problem = _output_schema_problem(base, "audit_wcag", mcp_result or {})
        checks.expect(
            schema_problem is None,
            "the delivered MCP result validates against the tool's advertised outputSchema"
            + (f" -- {schema_problem}" if schema_problem else ""),
        )

        step("v1: a pre-v2 client that never sees PAYMENT-REQUIRED pays from the body with X-PAYMENT")
        with state.lock:
            verifies_before_v1 = sum(1 for e in state.log if e["path"] == "/verify")
        v1_status, v1_tx, v1_header = _paid_call_v1(base, payer, f"{facilitator}/page")
        with state.lock:
            v1_verifies = [e for e in state.log if e["path"] == "/verify"][verifies_before_v1:]
            v1_settles = [e for e in state.log if e["path"] == "/settle"]
        last_v1 = v1_verifies[-1] if v1_verifies else {}
        checks.expect(v1_header == "X-PAYMENT", f"the client paid through the v1 header ({v1_header})")
        checks.expect(
            last_v1.get("x402Version") == 1,
            f"the node sent the facilitator an x402 v{last_v1.get('x402Version')} verify",
        )
        checks.expect(
            last_v1.get("reason") is None and v1_status == 200,
            f"a v1 payer gets the audit (HTTP {v1_status}, facilitator reason={last_v1.get('reason')})",
        )
        checks.expect(
            v1_tx is not None and v1_settles and v1_tx == v1_settles[-1].get("transaction"),
            f"X-PAYMENT-RESPONSE carries the v1 settlement transaction ({v1_tx})",
        )

        step("Replay: the same signed payment sent again")
        with state.lock:
            verifies_before = sum(1 for e in state.log if e["path"] == "/verify")
        status, _, body = _post(
            f"{base}/audit/wcag", {"url": f"{facilitator}/page"}, headers=pay_headers or {}
        )
        with state.lock:
            verifies_after = sum(1 for e in state.log if e["path"] == "/verify")
        checks.expect(status == 402, f"a replayed authorization is refused (HTTP {status})")
        checks.expect(
            verifies_after == verifies_before,
            "the replay never reached the facilitator (verify count unchanged)",
        )

        step("Sixteen payers at once, against a keep-alive facilitator (module level)")
        ok_count, fail_count, reasons = _hammer_module(facilitator, recipient, threads=16, rounds=4)
        checks.expect(
            fail_count == 0,
            f"{ok_count} of {ok_count + fail_count} concurrent payments verified and settled"
            + (f" -- failures: {sorted(reasons)[:3]}" if reasons else ""),
        )

        step("Facilitator outage: the node must stay up and stop advertising x402")
        dead = f"http://127.0.0.1:{_free_port()}"
        t_first, t_second, header_during_outage = _outage_probe(dead, recipient)
        checks.expect(
            header_during_outage == {},
            "no x402 offered while the facilitator is unreachable (fail-closed)",
        )
        checks.expect(t_first < 10, f"first 402 during the outage took {t_first:.1f}s (<10s)")
        checks.expect(t_second < 0.5, f"second 402 took {t_second:.2f}s -- the outage is remembered, not re-probed")

        step("A second node with the target gate ON and a dead facilitator")
        node2_port = _free_port()
        env2 = dict(env)
        env2.pop("ALLOW_PRIVATE_TARGETS", None)
        env2.update({"X402_FACILITATOR_URL": dead, "PUBLIC_BASE_URL": f"http://127.0.0.1:{node2_port}"})
        node2_log = work / "node2.log"
        node2 = start_node(node2_port, env2, node2_log)
        try:
            base2 = f"http://127.0.0.1:{node2_port}"
            probe_payer = {"X-PAYMENT": "not-a-real-payment"}
            for bad in (f"{facilitator}/page", "http://169.254.169.254/computeMetadata/v1/",
                        "http://metadata.google.internal/", "http://localhost:8080/health",
                        "http://10.0.0.1/", "ftp://example.com/"):
                # A dummy payment header: the target gate must answer BEFORE the
                # payment is read, so a payer's bad request costs nothing. (A
                # request with no credential at all is priced first, below.)
                status, _, body = _post(f"{base2}/audit/wcag", {"url": bad}, headers=probe_payer)
                checks.expect(
                    status == 400 and body.get("billed") is False,
                    f"refuses to fetch {bad} before reading the payment (HTTP {status})",
                )
            status, _, body = _post(
                f"{base2}/audit/wcag", {"url": "http://93.184.216.34/"}, headers={"X-API-Key": "simulate-paid-call"}
            )
            # A public IP literal passes the gate; with an internal key the
            # audit is attempted and fails (no egress here) -- 502, not 400.
            checks.expect(status in (200, 502), f"a public address passes the gate (HTTP {status})")
            t0 = time.time()
            status, headers2, body = _post(f"{base2}/audit/wcag", {"url": "http://93.184.216.34/"})
            elapsed = time.time() - t0
            lower2 = {k.lower() for k in headers2}
            checks.expect(
                status == 402 and "payment-required" not in lower2
                and not any(a.get("scheme") == "exact" for a in body.get("accepts") or []),
                f"unpaid call during a facilitator outage: 402 with no x402 rail (HTTP {status})",
            )
            checks.expect(elapsed < 10, f"that 402 took {elapsed:.1f}s (<10s), the node is not hanging on the outage")
            status, _, body = _post(
                f"{base2}/audit/wcag", {"html": "<p>" + "x" * (2 * 1024 * 1024 + 1)}, headers=probe_payer
            )
            checks.expect(status == 422, f"an oversized html body is refused before any payment (HTTP {status})")
        finally:
            node2.terminate()
            try:
                node2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                node2.kill()
            if not args.keep:
                node2_log.unlink(missing_ok=True)

        step("Node log")
        node_text = node_log.read_text(errors="replace")
        checks.expect(
            "cannot be called from a running event loop" not in node_text,
            "no 'asyncio.run() cannot be called from a running event loop' (#83)",
        )
        checks.expect("Traceback" not in node_text, "no traceback in the node log")
        for line in node_text.splitlines():
            if "x402" in line and ("WARNING" in line or "ERROR" in line):
                print("      | " + line[:200])
    finally:
        node.terminate()
        try:
            node.wait(timeout=10)
        except subprocess.TimeoutExpired:
            node.kill()
        if not args.keep and checks.failed == 0:
            for p in (wallet_file, node_log):
                p.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass

    print(f"\n{checks.passed} passed, {checks.failed} failed")
    if checks.failed:
        print(f"node log kept at {node_log}")
    return 1 if checks.failed else 0


def _x402_core_client(payer):
    from x402 import max_amount, x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client

    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(payer), policies=[max_amount(150_000)])
    return client


def _x402_http_client(payer):
    from x402.http import x402HTTPClientSync

    return x402HTTPClientSync(_x402_core_client(payer))


def _mcp_paid_call(base: str, payer, target_url: str):
    """Pay for one tool call the way the x402 MCP client does.

    The library's MCP client needs an MCP SDK session; this node speaks plain
    JSON-RPC over POST, so the client's steps are replayed here with the
    library's own helpers: read the challenge out of the tool result with
    `extract_payment_required_from_result`, sign it with the payment client,
    send the payload in `params._meta["x402/payment"]`, read the receipt
    back with `extract_payment_response_from_meta`. Returns (tx hash or
    None, the paid result dict or None)."""
    from x402.mcp.types import MCPToolResult
    from x402.mcp.utils import (
        extract_payment_required_from_result,
        extract_payment_response_from_meta,
    )

    def _as_result(result: dict) -> MCPToolResult:
        return MCPToolResult(
            content=result.get("content") or [],
            is_error=bool(result.get("isError")),
            meta=result.get("_meta"),
            structured_content=result.get("structuredContent"),
        )

    call = {
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "audit_wcag", "arguments": {"url": target_url}},
    }
    status, _, body = _post(f"{base}/mcp", call)
    result = (body or {}).get("result") or {}
    challenge = extract_payment_required_from_result(_as_result(result))
    if challenge is None:
        print(f"      unpaid MCP call: HTTP {status}, no payable challenge in the result")
        return None, None
    payload = _x402_core_client(payer).create_payment_payload(challenge)
    call["params"]["_meta"] = {"x402/payment": payload.model_dump(by_alias=True)}
    status, _, body = _post(f"{base}/mcp", call)
    result = (body or {}).get("result") or {}
    print(f"      paid MCP call: HTTP {status}, isError={result.get('isError')}")
    receipt = extract_payment_response_from_meta(_as_result(result))
    return (receipt.transaction if receipt is not None else None), result


def _sign(http_client, headers: dict, body: bytes, request_url: str) -> dict:
    """Payment headers for a 402, across x402 client versions (2.18 / 2.21)."""
    import inspect

    handler = http_client.handle_402_response
    call = [dict(headers), body]
    if "request_url" in inspect.signature(handler).parameters:
        call.append(request_url)
    pay_headers, _ = handler(*call)
    return pay_headers


def _paid_call_receipt(base: str, payer, target_url: str):
    """One more paid call, made directly, to read the response HEADERS the
    tollbooth client does not expose. Returns (tx hash from PAYMENT-RESPONSE
    or None, the payment headers that were sent) so the caller can replay."""
    import httpx
    from x402.http.utils import decode_payment_response_header

    http_client = _x402_http_client(payer)
    with httpx.Client(timeout=90) as http:
        first = http.post(f"{base}/audit/wcag", json={"url": target_url})
        if first.status_code != 402:
            print(f"      expected a 402, got {first.status_code}")
            return None, None
        pay_headers = _sign(http_client, first.headers, first.content, f"{base}/audit/wcag")
        paid = http.post(f"{base}/audit/wcag", json={"url": target_url}, headers=pay_headers)
        print(f"      paid call: HTTP {paid.status_code}")
        header = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE")
        if not header:
            return None, pay_headers
        return decode_payment_response_header(header).transaction, pay_headers


def _paid_call_v1(base: str, payer, target_url: str):
    """The pre-v2 client path: strip PAYMENT-REQUIRED so the official client
    reads the v1 body, sign, send X-PAYMENT. Returns (status, tx hash from
    the receipt or None, the header the client used).

    Until 2026-09-06 this leg did not exist and the node verified every v1
    payment against v2 requirements; the facilitator refused each one and
    every pre-v2 agent was turned away by a rail the 402 advertised."""
    import httpx
    from x402.http.utils import decode_payment_response_header

    http_client = _x402_http_client(payer)
    with httpx.Client(timeout=90) as http:
        first = http.post(f"{base}/audit/wcag", json={"url": target_url})
        if first.status_code != 402:
            print(f"      expected a 402, got {first.status_code}")
            return first.status_code, None, None
        body_only = {k: v for k, v in first.headers.items() if k.lower() != "payment-required"}
        pay_headers = _sign(http_client, body_only, first.content, f"{base}/audit/wcag")
        header_used = next((k.upper() for k in pay_headers if k.upper() in ("X-PAYMENT", "PAYMENT-SIGNATURE")), None)
        paid = http.post(f"{base}/audit/wcag", json={"url": target_url}, headers=pay_headers)
        print(f"      v1 paid call: HTTP {paid.status_code} (sent {header_used})")
        if paid.status_code != 200:
            print(f"      body: {paid.text[:300]}")
        receipt = paid.headers.get("X-PAYMENT-RESPONSE") or paid.headers.get("PAYMENT-RESPONSE")
        tx = decode_payment_response_header(receipt).transaction if receipt else None
        return paid.status_code, tx, header_used


def _output_schema_problem(base: str, tool_name: str, result: dict):
    """What the official MCP SDK client would raise on this result, or None.

    Mirrors mcp.client.session.ClientSession.call_tool: on a non-error
    result for a tool that declares an outputSchema, structuredContent must
    be present and must validate against that schema, else RuntimeError --
    in the agent's process, after the payment settled."""
    import jsonschema

    _, _, body = _post(f"{base}/mcp", {"jsonrpc": "2.0", "id": 12, "method": "tools/list"})
    tool = next((t for t in body["result"]["tools"] if t["name"] == tool_name), None)
    if tool is None:
        return f"{tool_name} is not in tools/list"
    schema = tool.get("outputSchema")
    if schema is None:
        return None
    if result.get("isError"):
        return "the result is an error"
    if "structuredContent" not in result:
        return "no structuredContent on a tool that declares an outputSchema"
    try:
        jsonschema.validate(result["structuredContent"], schema)
    except jsonschema.ValidationError as exc:
        return f"structuredContent does not validate: {exc.message}"
    return None


def _load_payments_module(facilitator: str, recipient: str):
    """The node's x402 module, imported fresh against `facilitator`."""
    import importlib.util

    os.environ["X402_FACILITATOR_URL"] = facilitator
    os.environ["X402_PAY_TO_ADDRESS"] = recipient
    name = f"x402_payments_sim_{abs(hash(facilitator))}"
    spec = importlib.util.spec_from_file_location(name, SERVICE_DIR / "app" / "x402_payments.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _hammer_module(facilitator: str, recipient: str, *, threads: int, rounds: int):
    """Drive verify+settle from many threads at once through the node's own
    payment module -- the exact code path a busy instance runs. Each thread
    signs its own payments; the facilitator is the keep-alive stub.

    This is where the cross-loop connection-reuse bug lived. Sequential runs
    cannot see it; this does, every time."""
    import logging

    from eth_account import Account

    module = _load_payments_module(facilitator, recipient)
    reasons: set = set()

    class _Capture(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "FAILED" in msg or "REJECTED" in msg or "REFUSED" in msg:
                reasons.add(msg.split(": ", 2)[-1][:90])

    logging.getLogger(module.__name__).addHandler(_Capture())
    counts = {"ok": 0, "fail": 0}
    lock = threading.Lock()

    def worker():
        payer = Account.create()
        http_client = _x402_http_client(payer)
        for _ in range(rounds):
            header = module.payment_required_header("$0.05", resource_url="http://node/audit/wcag")
            pay = _sign(http_client, header, b"", "http://node/audit/wcag")
            pending = module.verify_only_sync(pay["PAYMENT-SIGNATURE"], "$0.05")
            good = pending is not None and module.settle_sync(pending)
            with lock:
                counts["ok" if good else "fail"] += 1

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return counts["ok"], counts["fail"], reasons


def _outage_probe(dead_facilitator: str, recipient: str):
    """Time two 402 builds against a facilitator that is not there."""
    module = _load_payments_module(dead_facilitator, recipient)
    t0 = time.time()
    header = module.payment_required_header("$0.05", resource_url="http://node/audit/wcag")
    t_first = time.time() - t0
    t0 = time.time()
    module.payment_required_header("$0.05", resource_url="http://node/audit/wcag")
    t_second = time.time() - t0
    return t_first, t_second, header


if __name__ == "__main__":
    sys.exit(main())
