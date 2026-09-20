"""Guards for the GitHub Action's x402 payment path (scripts/x402_pay.py).

This is the code that spends money inside somebody else's CI pipeline, on a
schedule they did not choose, so the guards here are about restraint rather
than features: the per-call cap must reach the signer, a bad key must not be
echoed into a CI log, and a failure must report an unpaid call rather than a
broken action.

It exists because the action's only payment path was an API key, and a key
costs a browser checkout. A CI step whose first run fails with "HTTP 402 -- go
buy a plan" is deleted on the next push, which closed the adoption funnel of
the highest-volume distribution channel this service has.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "x402_pay.py"
ACTION = REPO_ROOT / "action.yml"

# A syntactically valid secp256k1 key. Never funded, never used: these tests
# stub the network, so no signature ever leaves the process.
_FAKE_KEY = "0x" + "11" * 32


def _run(tmp_path, **env_overrides):
    """Run the payer with x402 stubbed out, in a scratch cwd.

    The stubbing itself is `_with_sitecustomize`: CPython imports a
    `sitecustomize` off PYTHONPATH before anything else, which is what gets
    the fake x402 in place before the payer imports the real one.
    """
    env = dict(os.environ)
    env.update(
        {
            "BASE_URL": "https://node.example",
            "AUDIT_ENDPOINT": "bundle",
            "TARGET_URL": "https://example.com",
            "TIMEOUT_SECONDS": "90",
            "HUBVIBE_WALLET_KEY": _FAKE_KEY,
            "MAX_PRICE_USD": "0.15",
            "PYTHONPATH": str(tmp_path),
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )


# A stub for the SIGNING half of x402 only. The HTTP half is a real local
# server (see _node below), because the bug this file missed lived exactly
# there: the stub used to define a `post()` the real client does not have, so
# every test passed against a client that could not exist.
#
# What is stubbed here mirrors the real x402HTTPClientSync surface the payer
# uses -- handle_402_response and process_payment_result -- and records the
# spend policy so the cap can still be asserted on the signer, before a
# signature exists.
_STUB = '''
import json, sys, types

recorded = {}

def _save():
    with open("recorded.json", "w") as fh:
        json.dump(recorded, fh)

def max_amount(atomic):
    recorded["cap_atomic"] = atomic
    return ("max_amount", atomic)

class x402ClientSync:
    pass

def register_exact_evm_client(client, signer, policies=None):
    recorded["policies"] = policies
    _save()

class x402HTTPClientSync:
    def __init__(self, client):
        pass

    def handle_402_response(self, headers, body, request_url):
        recorded["url"] = request_url
        recorded["challenge"] = (body or b"").decode("utf-8", "replace")[:300]
        _save()
        %(on_402)s
        return {"X-PAYMENT": "stub-payment"}, {"stub": "payload"}

    def process_payment_result(self, payment_payload, get_header, status):
        recorded["settled_status"] = status
        recorded["receipt"] = get_header("payment-response")
        _save()
        return None

class EthAccountSigner:
    def __init__(self, account):
        pass

x402 = types.ModuleType("x402")
x402.max_amount = max_amount
x402.x402ClientSync = x402ClientSync
sys.modules["x402"] = x402

http_mod = types.ModuleType("x402.http")
http_mod.x402HTTPClientSync = x402HTTPClientSync
sys.modules["x402.http"] = http_mod

evm = types.ModuleType("x402.mechanisms.evm")
evm.EthAccountSigner = EthAccountSigner
sys.modules["x402.mechanisms"] = types.ModuleType("x402.mechanisms")
sys.modules["x402.mechanisms.evm"] = evm

exact = types.ModuleType("x402.mechanisms.evm.exact")
exact.register_exact_evm_client = register_exact_evm_client
sys.modules["x402.mechanisms.evm.exact"] = exact
'''


def _stub(on_402="pass"):
    return (_STUB % {"on_402": on_402}) + "\n"


def _with_sitecustomize(tmp_path, stub):
    (tmp_path / "sitecustomize.py").write_text(stub)
    return tmp_path


@contextlib.contextmanager
def _node(paid_status=200, paid_body='{"pass": true}'):
    """A real HTTP node that answers 402 until a payment header arrives.

    The payer must actually make both requests and carry the headers the client
    handed it between them -- the protocol dance the old stub skipped entirely.
    """
    state = {"unpaid": 0, "paid": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            if self.headers.get("X-PAYMENT"):
                state["paid"] += 1
                payload = paid_body.encode()
                self.send_response(paid_status)
                self.send_header("content-type", "application/json")
                self.send_header("payment-response", "stub-receipt")
            else:
                state["unpaid"] += 1
                payload = json.dumps(
                    {"error": "payment_required", "x402Version": 1, "accepts": []}
                ).encode()
                self.send_response(402)
                self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], state
    finally:
        server.shutdown()
        server.server_close()


def test_a_successful_payment_reports_the_status_and_writes_the_body(tmp_path):
    """The full dance against a real server: call unpaid, get 402, hand it to
    the client, resend carrying the headers it returned, get the audit."""
    _with_sitecustomize(tmp_path, _stub())
    with _node() as (base, state):
        result = _run(tmp_path, BASE_URL=base)
    assert result.stdout.strip().endswith("200"), result.stderr
    assert json.loads((tmp_path / "response.json").read_text())["pass"] is True
    assert state["unpaid"] == 1, "the payer never made the unpaid call"
    assert state["paid"] == 1, "the payer never retried with the payment header"
    recorded = json.loads((tmp_path / "recorded.json").read_text())
    assert recorded["settled_status"] == 200, "the settle receipt was never read"
    assert recorded["receipt"] == "stub-receipt"


def test_the_per_call_cap_reaches_the_signer_as_a_spend_policy(tmp_path):
    """The cap has to bind BEFORE a signature exists. Checking the price after
    the fact is not a cap, it is a receipt."""
    _with_sitecustomize(tmp_path, _stub())
    with _node() as (base, _state):
        result = _run(tmp_path, BASE_URL=base, MAX_PRICE_USD="0.15")
    assert result.returncode == 0, result.stderr
    recorded = json.loads((tmp_path / "recorded.json").read_text())
    # USDC is 6 decimals: $0.15 -> 150000 atomic units.
    assert recorded["cap_atomic"] == 150000
    assert recorded["policies"], "no spend policy was registered on the signer"


def test_a_different_cap_is_converted_not_hardcoded(tmp_path):
    _with_sitecustomize(tmp_path, _stub())
    with _node() as (base, _state):
        _run(tmp_path, BASE_URL=base, MAX_PRICE_USD="0.03")
    recorded = json.loads((tmp_path / "recorded.json").read_text())
    assert recorded["cap_atomic"] == 30000


def test_a_zero_or_negative_cap_refuses_to_pay(tmp_path):
    """An unbounded cap in someone else's CI is the one setting that can empty
    a wallet, so it fails closed rather than defaulting."""
    _with_sitecustomize(tmp_path, _stub())
    result = _run(tmp_path, MAX_PRICE_USD="0")
    assert result.stdout.strip() == "402"
    assert "greater than zero" in result.stderr


def test_a_bad_wallet_key_never_appears_in_the_log(tmp_path):
    """CI logs are retained and often public. A diagnostic must not make a
    private key -- or its length -- recoverable."""
    secret = "0xdeadbeef"
    _with_sitecustomize(tmp_path, _stub())
    result = _run(tmp_path, HUBVIBE_WALLET_KEY=secret)
    assert result.stdout.strip() == "402"
    assert "not a valid EVM private key" in result.stderr
    assert secret not in result.stderr
    assert secret not in result.stdout
    assert "deadbeef" not in (tmp_path / "response.json").read_text()


def test_a_payment_failure_reports_an_unpaid_call_not_a_crash(tmp_path):
    """A traceback would read as a broken action; it is an unpaid call, and the
    action already has a branch that says so usefully."""
    _with_sitecustomize(tmp_path, _stub(on_402="raise RuntimeError('facilitator said no')"))
    with _node() as (base, _state):
        result = _run(tmp_path, BASE_URL=base)
    assert result.stdout.strip() == "402"
    assert "x402 payment failed" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_action_passes_the_wallet_and_cap_through_to_the_script():
    """An input the run block never receives is an input that silently does
    nothing -- and this one is the difference between paying and not."""
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(ACTION.read_text())

    assert "wallet-key" in spec["inputs"]
    assert "max-price-usd" in spec["inputs"]
    assert spec["inputs"]["max-price-usd"]["default"] == "0.15"

    step = spec["runs"]["steps"][-1]
    assert step["env"]["HUBVIBE_WALLET_KEY"] == "${{ inputs.wallet-key }}"
    assert step["env"]["MAX_PRICE_USD"] == "${{ inputs.max-price-usd }}"
    assert "x402_pay.py" in step["run"]


def test_the_published_action_repo_ships_the_payer():
    """action.yml calls it by $GITHUB_ACTION_PATH. A published copy without it
    fails with 'No such file or directory' on the one path that spends money."""
    generator = (REPO_ROOT / "scripts" / "publish-action-repo.sh").read_text()
    assert "x402_pay.py" in generator


def test_the_api_key_path_still_wins_when_both_are_set():
    """A key is prepaid; a wallet spends per call. Preferring the wallet would
    charge someone who has already paid."""
    yaml = pytest.importorskip("yaml")
    run = yaml.safe_load(ACTION.read_text())["runs"]["steps"][-1]["run"]
    assert '[ -z "$HUBVIBE_API_KEY" ] && [ -n "${HUBVIBE_WALLET_KEY:-}" ]' in run


def test_the_payer_only_calls_methods_the_real_x402_client_has():
    """Every other test in this file replaces x402 with a stub that defines
    whatever the payer happens to call, so a call to a method the real client
    does NOT have passes here and fails only in the customer's pipeline.

    That is exactly what shipped: x402_pay.py called `http.post(url, json=...)`.
    x402HTTPClientSync has no `post` -- it encodes and decodes the protocol
    around a request the caller makes (handle_402_response, create_payment_payload,
    encode_payment_signature_header, process_payment_result). Every Action run
    paying by wallet raised AttributeError before a byte reached the node, and
    the wallet is the only way a CI customer can pay on an x402-only deployment.

    So this one guard imports the REAL pinned client and asserts the payer's
    calls exist on it. Imported hard, never skipped: x402 is pinned in
    requirements.txt, and a skip here would restore the blind spot.
    """
    import ast

    from x402.http import x402HTTPClientSync

    tree = ast.parse(SCRIPT.read_text())

    # The name x402HTTPClientSync(...) is bound to, so the guard follows a
    # rename instead of silently checking nothing.
    client_names = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "x402HTTPClientSync"
    }
    assert client_names, "no x402HTTPClientSync(...) assignment found; this guard sees nothing"

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in client_names
    }
    assert called, "the payer calls nothing on the x402 client; this guard sees nothing"

    missing = sorted(m for m in called if not hasattr(x402HTTPClientSync, m))
    assert not missing, (
        "x402_pay.py calls %s on x402HTTPClientSync; the real client has no such "
        "method, so every wallet-paid run dies with AttributeError. Available: %s"
        % (missing, sorted(m for m in dir(x402HTTPClientSync) if not m.startswith("_")))
    )
