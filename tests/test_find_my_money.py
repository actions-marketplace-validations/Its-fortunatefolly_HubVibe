"""scripts/find-my-money.sh -- where did the money go?

On 2026-09-08 the owner funded the payer and the box said nothing arrived.
Nothing was lost. Every tool here reads exactly one token on exactly one
chain (native USDC on Base), so USDC sent on Ethereum, bridged USDbC on
Base, or plain ETH all read as $0.00 -- on screen, identical to "never
received". This script reads the address across every chain it could be on.

The failure that matters most is a chain that cannot be READ being reported
as a chain that is EMPTY. That is the difference between "check again" and
"your money is gone", told to someone who just sent real money.
"""

import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "find-my-money.sh"
ADDRESS = "0x104feA79F30b4fB4Da86B6D65951217F914bdd35"

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _chain(holdings):
    """A fake chain. holdings maps a lowercased token contract to a raw
    integer amount, plus optional 'native'."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if payload["method"] == "eth_getBalance":
                value = holdings.get("native", 0)
            else:
                to = payload["params"][0]["to"].lower()
                value = holdings.get(to, 0)
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": "0x" + hex(value)[2:].rjust(64, "0"),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def _run(rpc_overrides, address=ADDRESS):
    """Point the script's chain table at local fakes by rewriting the URLs.

    The RPC list is a literal in the script on purpose -- a checker whose
    endpoints come from the environment is a checker that can be pointed at a
    lying source. So the test rewrites the file it runs, rather than the
    script growing a hook that exists only for tests.
    """
    source = SCRIPT.read_text()
    for real, fake in rpc_overrides.items():
        source = source.replace(real, fake)
    tmp = REPO_ROOT / "scripts" / ".find-my-money.undertest.sh"
    tmp.write_text(source)
    try:
        return subprocess.run(
            ["bash", str(tmp), address], capture_output=True, text=True,
            timeout=120, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                              "HOME": "/nonexistent-home"},
        )
    finally:
        tmp.unlink(missing_ok=True)


REAL = {
    "base": "https://mainnet.base.org",
    "eth": "https://ethereum-rpc.publicnode.com",
    "arb": "https://arbitrum-one-rpc.publicnode.com",
    "op": "https://optimism-rpc.publicnode.com",
    "pol": "https://polygon-bor-rpc.publicnode.com",
}
DEAD = "http://127.0.0.1:9"


def test_usdc_on_base_is_found_and_called_the_right_chain():
    server, url = _chain({USDC_BASE: 250_000})  # $0.25
    try:
        result = _run({REAL["base"]: url, REAL["eth"]: DEAD, REAL["arb"]: DEAD,
                       REAL["op"]: DEAD, REAL["pol"]: DEAD})
    finally:
        server.shutdown()
    assert "FOUND" in result.stdout, result.stdout
    assert "Base" in result.stdout
    assert "0.250000 USDC" in result.stdout
    assert "That is the right chain" in result.stdout


def test_usdc_on_the_wrong_chain_is_found_and_says_it_is_not_lost():
    """The whole point. Money on Ethereum is not missing money."""
    server, url = _chain({USDC_ETH: 1_000_000})  # $1 on the wrong chain
    try:
        result = _run({REAL["eth"]: url, REAL["base"]: DEAD, REAL["arb"]: DEAD,
                       REAL["op"]: DEAD, REAL["pol"]: DEAD})
    finally:
        server.shutdown()
    assert "FOUND" in result.stdout, result.stdout
    assert "Ethereum" in result.stdout
    assert "NOT where this node settles" in result.stdout
    assert "Nothing is lost" in result.stdout
    assert "same private key" in result.stdout


def test_bridged_usdbc_on_base_is_found_even_though_the_payer_cannot_use_it():
    """USDbC sits at a different contract than native USDC. The paid call
    cannot spend it, and every other tool here reports $0.00 -- which reads
    as 'never arrived' while the money is right there."""
    usdbc = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
    server, url = _chain({usdbc: 500_000})
    try:
        result = _run({REAL["base"]: url, REAL["eth"]: DEAD, REAL["arb"]: DEAD,
                       REAL["op"]: DEAD, REAL["pol"]: DEAD})
    finally:
        server.shutdown()
    assert "USDbC (bridged)" in result.stdout, result.stdout
    assert "0.500000" in result.stdout


def test_an_unreadable_chain_is_never_reported_as_empty():
    """The failure that matters most. 'Could not be read' and 'holds nothing'
    are opposite messages to someone who just sent money, and an RPC outage
    must never produce the second one."""
    result = _run({v: DEAD for v in REAL.values()})
    assert "could not be read" in result.stdout, result.stdout
    assert "NOT a zero balance" in result.stdout
    assert "not a conclusion" in result.stdout
    # It must not claim the address is empty everywhere.
    assert "Every chain above reads empty" not in result.stdout
    assert result.returncode == 2, "an inconclusive read must not exit clean"


def test_genuinely_empty_everywhere_says_so_and_points_at_the_transaction():
    """When every chain really does answer zero, say it plainly -- and name
    the one case where funds are NOT recoverable, so nobody keeps hunting."""
    servers = []
    overrides = {}
    try:
        for real in REAL.values():
            server, url = _chain({})
            servers.append(server)
            overrides[real] = url
        result = _run(overrides)
    finally:
        for server in servers:
            server.shutdown()
    assert "Every chain above reads empty" in result.stdout, result.stdout
    assert "character by character" in result.stdout
    assert result.returncode == 1


def test_a_malformed_address_is_refused_before_any_lookup():
    result = _run({v: DEAD for v in REAL.values()}, address="not-an-address")
    assert result.returncode != 0
    assert "starts with 0x" in result.stdout
    assert "Reading every chain" not in result.stdout


def test_a_short_hex_address_is_refused():
    """42 characters or it is not an address. A truncated paste that gets
    looked up anyway returns 'empty' for an address that does not exist."""
    result = _run({v: DEAD for v in REAL.values()}, address="0x104feA79F30b4")
    assert result.returncode != 0
    assert "42-character" in result.stdout
