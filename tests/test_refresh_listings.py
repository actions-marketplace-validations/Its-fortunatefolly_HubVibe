"""scripts/refresh-listings.sh and scripts/register_mcp_tool.py -- keeping
this node listed, and freshly listed, in every facilitator's Bazaar.

A listing carries the price and description of the LAST payment through that
facilitator, so settling everything through one lets the other advertise a
stale rate. On 2026-09-13 both indexes quoted $0.03 while the node charged
$0.05 until each was re-paid. These guard the script that prevents it, and
above all guard the money: the cycle spends real USDC, so it must know what
it will cost before it starts and must never leave the box on a facilitator
it did not start on.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "refresh-listings.sh"
MCP_REGISTER = REPO_ROOT / "scripts" / "register_mcp_tool.py"


class _StubRPC(threading.Thread):
    """A Base RPC that answers one eth_call with a chosen USDC balance.

    The script reads the payer through $BASE_RPC (the same variable
    payment-status.sh uses), so pointing it here is how a test spends no
    money and still exercises the affordability gate. `balance=None` answers
    with an error, which is the unreadable-chain case.
    """

    def __init__(self, balance):
        super().__init__(daemon=True)
        self.balance = balance
        handler = self._handler()
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def _handler(self):
        balance = self.balance

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if balance is None:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"upstream is down")
                    return
                atomic = int(round(float(balance) * 1_000_000))
                body = json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "result": "0x%064x" % atomic}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()


def _env(tmp_path, facilitator="https://facilitator.payai.network", balance="9.99", **extra):
    """A fake box: an .env naming a facilitator, a payer key, and a stub RPC
    standing in for Base so nothing here can spend or read real money."""
    compose = tmp_path / "vps"
    compose.mkdir(parents=True, exist_ok=True)
    (compose / ".env").write_text(
        "DOMAIN=hubvibe-io.com\nX402_FACILITATOR_URL=%s\n" % facilitator
    )
    wallet = tmp_path / "wallet-key"
    # A valid secp256k1 key so eth_account can derive an address from it.
    wallet.write_text("0x" + "11" * 32)
    rpc = _StubRPC(None if balance == "RPCFAIL" else balance)
    rpc.start()
    env = dict(os.environ)
    env.pop("CLOUD_SHELL", None)
    env.pop("DEVSHELL_PROJECT_ID", None)
    env.update(
        COMPOSE_DIR=str(compose),
        HUBVIBE_WALLET_FILE=str(wallet),
        BASE_RPC=rpc.url,
        **extra,
    )
    return env, compose, rpc


def _run(env, timeout=120):
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=timeout
    )


@pytest.mark.parametrize("marker", ["CLOUD_SHELL", "DEVSHELL_PROJECT_ID"])
def test_it_refuses_cloud_shell_by_name_and_spends_nothing(tmp_path, marker):
    """Every paying script here refuses Cloud Shell: a temporary terminal has
    its own home, so it finds no wallet and reports a wallet problem when the
    only problem is the machine."""
    env, compose, rpc = _env(tmp_path)
    env[marker] = "true" if marker == "CLOUD_SHELL" else "some-project"
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 1
    assert "Cloud Shell" in result.stdout
    assert "on the box" in result.stdout
    # And it stopped before touching anything.
    assert "X402_FACILITATOR_URL=https://facilitator.payai.network" in (compose / ".env").read_text()


def test_dry_run_prices_the_cycle_and_pays_nothing(tmp_path):
    """The number matters: a cycle that runs out of money halfway leaves one
    index fresh, one stale, and the box possibly on the wrong facilitator."""
    env, compose, rpc = _env(tmp_path)
    env["DRY_RUN"] = "1"
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 0, result.stdout + result.stderr
    # 2 facilitators x (4 x $0.05 + 1 x $0.15) + 5 MCP tools on the one that
    # indexes them = $0.70 + $0.35.
    assert "$1.05" in result.stdout, result.stdout
    assert "DRY_RUN" in result.stdout
    assert "X402_FACILITATOR_URL=https://facilitator.payai.network" in (compose / ".env").read_text()


def test_only_one_facilitator_costs_less(tmp_path):
    env, _, rpc = _env(tmp_path)
    env.update(DRY_RUN="1", ONLY="coinbase")
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 0, result.stdout
    # Coinbase does not index MCP, so it is the five routes and nothing else.
    assert "$0.35" in result.stdout, result.stdout
    assert "MCP tools" not in result.stdout


def test_it_refuses_rather_than_half_spend_when_the_wallet_is_short(tmp_path):
    """Stopping before the first payment is the whole point. A cycle that dies
    halfway is worse than one that never started."""
    env, _, rpc = _env(tmp_path, balance="0.10")
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 1
    assert "short" in result.stdout
    assert "Send USDC on Base" in result.stdout
    assert "no ETH needed" in result.stdout


def test_an_unreadable_chain_is_not_reported_as_an_empty_wallet(tmp_path):
    """"could not read the balance" and "the wallet is empty" call for
    completely different actions; this repo has conflated them before."""
    env, _, rpc = _env(tmp_path, balance="RPCFAIL")
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 1
    assert "could not read the balance" in result.stdout
    assert "short" not in result.stdout


def _pythons(tmp_path):
    """Two python3 wrappers: one whose eth_account import fails (a shadow
    module on PYTHONPATH, so it fails wherever the real one is installed),
    and one that works. The first goes on PATH, the second into a fake
    ~/.hubvibe-venv -- the environment first-paid-call.sh builds on the box."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "eth_account.py").write_text("raise ImportError(\"No module named 'eth_account'\")\n")
    broken = tmp_path / "broken-bin"
    broken.mkdir()
    (broken / "python3").write_text("#!/bin/sh\nPYTHONPATH=%s exec %s \"$@\"\n" % (shadow, sys.executable))
    (broken / "python3").chmod(0o755)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("#!/bin/sh\nexec %s \"$@\"\n" % sys.executable)
    (venv / "bin" / "python3").chmod(0o755)
    return broken, venv


def test_a_python_without_the_client_is_named_as_such_not_as_a_missing_wallet(tmp_path):
    """The failure the owner hit on the box on 2026-09-14: bare python3 has no
    eth_account and the script said "no payer wallet". Stop before the wallet
    check, and say what is actually missing."""
    broken, _ = _pythons(tmp_path)
    env, compose, rpc = _env(tmp_path)
    env["PATH"] = "%s:%s" % (broken, env["PATH"])
    env["HUBVIBE_VENV"] = str(tmp_path / "does-not-exist")
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 1
    assert "eth_account" in result.stdout, result.stdout
    assert "no payer wallet" not in result.stdout, result.stdout
    assert "X402_FACILITATOR_URL=https://facilitator.payai.network" in (compose / ".env").read_text()


def test_it_uses_the_environment_first_paid_call_builds(tmp_path):
    """Same broken python3 on PATH, but the venv exists: the script must put
    it first and carry on. This is what makes it work on the box at all."""
    broken, venv = _pythons(tmp_path)
    env, _, rpc = _env(tmp_path)
    env["PATH"] = "%s:%s" % (broken, env["PATH"])
    env["HUBVIBE_VENV"] = str(venv)
    env["DRY_RUN"] = "1"
    try:
        result = _run(env)
    finally:
        rpc.stop()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "$1.05" in result.stdout


def test_the_script_restores_the_facilitator_it_started_on():
    """Static, because reaching this in a live run costs a dollar. Leaving the
    box on a facilitator it did not start on is the one lasting harm this
    script could do, so the restore hangs off an EXIT trap rather than the
    happy path."""
    text = SCRIPT.read_text()
    assert "trap restore EXIT" in text, "the restore is not guaranteed on failure"
    restore = text[text.index("restore() {"):text.index("trap restore EXIT")]
    assert "switch-facilitator.sh" in restore, "it must restore through the verifying switch"
    assert "COULD NOT RESTORE" in restore, "a failed restore must be said out loud, not swallowed"


def test_it_only_registers_mcp_where_that_was_measured_to_work():
    """PayAI's index held 31 `type: mcp` records and Coinbase's 5 (none ours,
    after two paid attempts carrying a valid record). Paying to register a
    tool where nothing ingests it is money for nothing, so the table records
    the measurement and the comment says how to re-take it."""
    text = SCRIPT.read_text()
    table = text[text.index("FACILITATORS=\""):text.index("ROUTES=")]
    assert "payai|https://facilitator.payai.network|yes" in table
    assert "coinbase|https://api.cdp.coinbase.com/platform/v2/x402|no" in table
    assert "Re-measure" in text, "a measured claim must say how to re-measure it"


def test_the_mcp_registrar_carries_the_record_the_official_client_drops():
    """The entire reason this file exists. x402.mcp's client calls
    create_payment_payload(payment_required) with neither resource nor
    extensions, so a paying MCP agent registers nothing -- which is why the
    MCP lane of every Bazaar is nearly empty. Drop the arguments here and the
    script silently becomes pointless while still spending money."""
    text = MCP_REGISTER.read_text()
    assert "create_payment_payload(challenge, challenge.resource, challenge.extensions)" in text
    assert "not challenge.extensions or \"bazaar\" not in challenge.extensions" in text, (
        "it must refuse to pay for a challenge that would register nothing"
    )


def test_the_mcp_registrar_treats_an_uncollected_call_as_a_failure():
    """A settle that did not land registers no record. Reporting success there
    is the false pass this codebase exists to avoid."""
    text = MCP_REGISTER.read_text()
    assert "billing_warning" in text
    body = text[text.index("if warning:"):]
    assert "fail(" in body.split("\n\n")[0] or "fail(" in body[:400]


def test_a_settled_route_is_not_reported_as_failed():
    """The 2026-09-14 cycle paid all ten routes and printed FAILED for each:
    first-paid-call.sh was piped into `grep -q`, which exits at the first
    match, and under pipefail the writer's resulting SIGPIPE fails the
    pipeline. The result must be captured and inspected, never piped."""
    text = SCRIPT.read_text()
    assert 'first-paid-call.sh" 2>&1)"' in text, "capture first-paid-call.sh's output into a variable"
    assert "first-paid-call.sh\" 2>&1 \\\n         | grep -q" not in text
    assert "| grep -q \"settled" not in text, "a pipe into grep -q reports success as failure under pipefail"


def test_both_scripts_are_valid():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    compile(MCP_REGISTER.read_text(), str(MCP_REGISTER), "exec")
