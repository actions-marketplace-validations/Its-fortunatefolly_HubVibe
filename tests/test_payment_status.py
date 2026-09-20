"""The payment dashboard must tell the truth about where the money goes.

Its most important line is the one that says whether the recipient the node
advertises is actually the owner's wallet. This project ran for weeks
advertising an address nobody held the key to, and every format gate passed
it -- so the check that matters is not "is it well-formed" but "is it one of
the affirmed wallets". These tests drive the real script against a fake node
serving a real-shaped 402, because a dashboard that reads a 402 wrongly is
worse than no dashboard: it reports safety that is not there.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "payment-status.sh"

OWNER_PRIMARY = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
OWNER_ALTERNATE = "0x37555E884c5EbA10f6E816DbecEA30965B9b38C0"
STRANGER = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"  # the historical unidentified address


def _challenge(pay_to):
    return {
        "error": "payment_required",
        "price": "$0.05",
        "price_usd": 0.05,
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": "30000",
                "resource": "https://node.test/audit/wcag",
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "other_rails": [{"protocol": "mpp", "method": "tempo"}],
        "extensions": {"bazaar": {"info": {"input": {"type": "http", "method": "POST"}}, "schema": {}}},
    }


def _serve(pay_to=None, health=True):
    """A fake node. pay_to=None serves a 402 with no x402 rail."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _send(self, status, payload, extra_headers=None):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/health":
                if not health:
                    return self._send(503, {"status": "down"})
                return self._send(200, {"status": "ok", "service": "wcag-audit-engine"})
            if path == "/.well-known/agent.json":
                methods = ["x402", "mpp-tempo"] if pay_to else ["mpp-tempo"]
                return self._send(200, {"payment": {"methods": methods}})
            return self._send(404, {"message": "Not Found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = _challenge(pay_to) if pay_to else dict(_challenge(OWNER_PRIMARY), accepts=[], extensions={})
            headers = {"PAYMENT-REQUIRED": "stub"} if pay_to else {}
            return self._send(402, body, headers)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _run(base, tmp_path, extra_env=None):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "BASE": base,
        # A dead RPC: the wallet section must degrade to a note, never crash.
        "BASE_RPC": "http://127.0.0.1:9",
        "HUBVIBE_WALLET_FILE": str(tmp_path / "no-wallet"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env, cwd=REPO_ROOT
    )


def test_bash_parses_the_script():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_node_paying_the_owner_is_reported_as_safe(tmp_path):
    server, base = _serve(pay_to=OWNER_PRIMARY)
    try:
        result = _run(base, tmp_path)
    finally:
        server.shutdown()
    out = result.stdout
    assert "the node is up" in out
    assert "x402 is LIVE" in out
    assert "$0.05" in out, "the price a caller actually pays must be shown"
    assert "the node pays YOU" in out
    assert "hubvibe.base.eth" in out
    assert "Nothing structural" in out, out[-600:]
    assert "first-paid-call.sh" in out, "the verdict must name the next command"


def test_the_alternate_wallet_is_also_recognised_as_the_owners(tmp_path):
    server, base = _serve(pay_to=OWNER_ALTERNATE)
    try:
        result = _run(base, tmp_path)
    finally:
        server.shutdown()
    assert "the node pays YOU" in result.stdout
    assert "Coinbase/Base app" in result.stdout


def test_a_node_paying_a_stranger_is_reported_as_a_STOP(tmp_path):
    """THE test. A well-formed address that is not the owner's must be called
    out loudly -- this is the exact failure that ran undetected for weeks."""
    server, base = _serve(pay_to=STRANGER)
    try:
        result = _run(base, tmp_path)
    finally:
        server.shutdown()
    out = result.stdout
    assert "NOT an affirmed wallet" in out, out[-800:]
    assert "STOP" in out
    assert STRANGER in out
    assert "pays YOU" not in out
    # The verdict must refuse to bless it, and must name the fix.
    assert "Nothing structural" not in out
    assert "X402_PAY_TO_ADDRESS=" in out


def test_a_node_with_no_x402_rail_says_no_agent_can_pay(tmp_path):
    server, base = _serve(pay_to=None)
    try:
        result = _run(base, tmp_path)
    finally:
        server.shutdown()
    out = result.stdout
    assert "x402 is NOT advertised" in out
    assert "advertises no x402 rail" in out
    assert "x402 is LIVE" not in out


def test_a_dead_node_names_the_install_step(tmp_path):
    result = _run("http://127.0.0.1:9", tmp_path)
    out = result.stdout
    assert "cannot reach" in out
    assert "not serving" in out
    assert "vps-install.sh" in out


def test_an_unreadable_rpc_degrades_to_a_note_never_a_crash(tmp_path):
    """The chain read is the revenue counter; when it cannot be made, the
    dashboard must hand over the Basescan link rather than die or, worse,
    silently report zero."""
    server, base = _serve(pay_to=OWNER_PRIMARY)
    try:
        result = _run(base, tmp_path)
    finally:
        server.shutdown()
    out = result.stdout
    assert "could not read the chain" in out
    assert "basescan.org/address/" in out
    assert "Total received: $0" not in out, "an unreadable balance must never read as zero revenue"
    assert result.returncode == 0


def test_the_script_never_prints_a_private_key(tmp_path):
    """It derives an ADDRESS from the key file. The key itself must never
    leave that file.

    The invariant checked is flow, not spelling: EVERY read of the wallet
    file must feed straight into Account.from_key. A read that goes anywhere
    else -- print, echo, a variable, a log -- is a leak, however it is
    written. A weaker spelling-based check let `print(open(WALLET_FILE).read())`
    through when it was mutation-tested, which is exactly the bug it existed
    to stop.
    """
    text = SCRIPT.read_text()
    assert "Account.from_key" in text, "the script no longer derives the address at all"

    reads = [
        line.strip()
        for line in text.splitlines()
        if ("open(WALLET_FILE)" in line or "WALLET_FILE" in line and "cat" in line)
        and not line.strip().startswith("#")
    ]
    assert reads, "no read of the wallet file found -- has the check been gutted?"
    for line in reads:
        assert "Account.from_key(" in line, f"the wallet file is read outside from_key: {line[:80]}"

    # And the shell half never echoes the key either.
    for leak in ('cat "$WALLET_FILE"', "cat $WALLET_FILE", 'echo "$HUBVIBE_WALLET_KEY"'):
        assert leak not in text, f"the script leaks the key: {leak}"


def test_the_script_is_read_only():
    """A status command must never move money or change a deployment.

    It is allowed -- and useful -- to NAME the next command in its output.
    What it must never do is EXECUTE one. So the property checked is
    positional: every mention of an action command must live inside a
    print/printf/echo, never in command position.
    """
    dangerous = ("first-paid-call.sh", "vps-install.sh", "docker compose", "docker run",
                 "gcloud run deploy", "services update", "git push", "pip install")
    offenders = []
    for number, line in enumerate(SCRIPT.read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for command in dangerous:
            if command not in stripped:
                continue
            # Output, not execution: the mention sits inside a printed string.
            if any(token in stripped for token in ("print(", "printf", "echo ", '"""', "'")):
                continue
            offenders.append(f"line {number}: {stripped[:70]}")
    assert not offenders, "the status script would EXECUTE an action:\n" + "\n".join(offenders)


def test_the_affirmed_wallets_match_the_handoff_record():
    """The owner-wallet list is the thing every judgement here rests on. If
    HANDOFF affirms a wallet this script does not know, the dashboard would
    call the owner's own address a stranger."""
    text = SCRIPT.read_text()
    handoff = (REPO_ROOT / "docs" / "HANDOFF.md").read_text()
    for wallet in (OWNER_PRIMARY, OWNER_ALTERNATE):
        assert wallet.lower() in text.lower(), f"{wallet} missing from the script"
        assert wallet.lower() in handoff.lower(), f"{wallet} is not affirmed in HANDOFF"
    assert STRANGER.lower() not in text.lower().replace("owner_wallets", ""), (
        "a refused address must never appear in the owner list"
    )


def _serve_rpc(balance_units=2_000_000, refuse_python_ua=True):
    """A fake Base RPC that behaves like mainnet.base.org's edge: HTTP 403
    to Python's default User-Agent, a balance to anyone who names itself."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen = []

        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            ua = self.headers.get("User-Agent") or ""
            Handler.seen.append(ua)
            if refuse_python_ua and ua.startswith("Python-urllib"):
                raw = b"forbidden"
                self.send_response(403)
            else:
                raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": hex(balance_units)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", Handler


def test_the_chain_is_read_through_an_edge_that_refuses_pythons_default_user_agent(tmp_path):
    """2026-09-06: from the owner's Cloud Shell the script said 'RPC
    unreachable' while curl read the same endpoint fine. mainnet.base.org
    answers 403 to 'Python-urllib/3.x'. The script must name itself."""
    node, base = _serve(pay_to=OWNER_PRIMARY)
    rpc, rpc_url, handler = _serve_rpc(balance_units=2_000_000)
    try:
        result = _run(base, tmp_path, extra_env={"BASE_RPC": rpc_url})
    finally:
        node.shutdown()
        rpc.shutdown()
    out = result.stdout
    assert "could not read the chain" not in out, out
    assert "2.00" in out, "the $2.00 balance the RPC served must be printed"
    assert handler.seen and not any(ua.startswith("Python-urllib") for ua in handler.seen), handler.seen


def test_a_dead_first_rpc_falls_through_to_the_next(tmp_path):
    node, base = _serve(pay_to=OWNER_PRIMARY)
    rpc, rpc_url, _ = _serve_rpc(balance_units=500_000)
    try:
        result = _run(base, tmp_path, extra_env={"BASE_RPC": f"http://127.0.0.1:9,{rpc_url}"})
    finally:
        node.shutdown()
        rpc.shutdown()
    assert "could not read the chain" not in result.stdout, result.stdout
    assert "0.50" in result.stdout
