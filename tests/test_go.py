"""scripts/go.sh -- the one command the owner runs on the box.

The first paid call needs exactly two things a script cannot do: money moved
out of the owner's own wallet, and their hands on their own accounts.
Everything else was still being handed to them as homework -- make a wallet,
read an address, go fund it, come back to a terminal that has since dropped,
run a second command. This script is the machine doing the machine's half
unattended, so the owner's part is one transfer whenever they get to it.

These tests drive it with a fake chain, because the sandbox cannot reach Base
and a test that needs the real chain is a test that silently skips.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go.sh"

# A key whose address is stable, so a test can assert the address is printed.
KEY = "0x" + "1" * 63 + "2"


def _address():
    from eth_account import Account

    return Account.from_key(KEY).address


def _fake_rpc(balances):
    """A file:// 'RPC' is not possible -- urlopen POSTs. So run a real one.

    balances: list of USDC amounts served in order, last value repeating. The
    loop must see a balance CHANGE without restarting, which a single static
    response cannot prove.
    """
    import http.server
    import json
    import threading

    remaining = list(balances)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            value = remaining[0] if len(remaining) == 1 else remaining.pop(0)
            raw = hex(int(round(value * 1_000_000)))[2:].rjust(64, "0")
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x" + raw}).encode()
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


def _env(tmp_path, rpc, **extra):
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HUBVIBE_WALLET_FILE": str(key_file),
        "BASE_RPC": rpc,
        "POLL_SECONDS": "1",
        "WAIT_SECONDS": "3",
        "BASE": "http://127.0.0.1:9",
    }
    env.update(extra)
    return env


def _stub_first_paid_call(tmp_path):
    """Stand in for the payment itself, so these tests never spend anything
    and never need a node. It records that it ran."""
    stub_dir = tmp_path / "stub" / "scripts"
    stub_dir.mkdir(parents=True)
    (stub_dir / "go.sh").write_bytes(SCRIPT.read_bytes())
    marker = tmp_path / "paid"
    (stub_dir / "first-paid-call.sh").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf 'PAID CALL RAN\\n'
        printf 'ran' > {marker}
        """))
    return stub_dir / "go.sh", marker


def test_it_pays_immediately_when_the_wallet_is_already_funded(tmp_path):
    """No waiting when there is nothing to wait for."""
    server, rpc = _fake_rpc([1.0])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert "PAID CALL RAN" in result.stdout, result.stdout
    assert marker.exists()
    assert "Waiting for the money" not in result.stdout


def test_it_waits_then_pays_when_the_money_lands(tmp_path):
    """The whole point: the owner sends the money whenever, and the box
    notices and makes the call without them present."""
    server, rpc = _fake_rpc([0.0, 0.0, 0.25])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc, WAIT_SECONDS="30"), timeout=90,
        )
    finally:
        server.shutdown()
    assert "Waiting for the money" in result.stdout, result.stdout
    assert "PAID CALL RAN" in result.stdout, result.stdout
    assert marker.exists()
    # The address to fund must be on screen -- an unfunded run that does not
    # say where to send money is the homework this script exists to remove.
    assert _address() in result.stdout


def test_an_unreadable_chain_never_reads_as_funded(tmp_path):
    """An RPC that is down must not look like a zero balance forever, and must
    never look like enough to pay. Guessing either way spends or stalls."""
    script, marker = _stub_first_paid_call(tmp_path)
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        # A port nothing listens on: every RPC read fails.
        env=_env(tmp_path, "http://127.0.0.1:9"), timeout=90,
    )
    assert not marker.exists(), "it paid without ever reading a balance"
    assert "unreadable" in result.stdout
    assert result.returncode != 0
    assert "Nothing was spent" in result.stdout


def test_it_gives_up_without_spending_when_no_money_arrives(tmp_path):
    """A timeout is not a failure to report darkly -- say nothing was spent
    and how to resume."""
    server, rpc = _fake_rpc([0.0])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert not marker.exists()
    assert result.returncode != 0
    assert "Nothing was spent" in result.stdout
    assert _address() in result.stdout, "must name the address to fund on the way out"


def test_a_balance_under_the_price_does_not_trigger_a_call(tmp_path):
    """$0.02 cannot pay for a $0.03 call. Trying anyway burns the run and
    hands back a facilitator refusal instead of an answer."""
    server, rpc = _fake_rpc([0.02])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert not marker.exists(), "it tried to pay $0.03 out of $0.02"
    assert result.returncode != 0


@pytest.mark.parametrize("marker_env", [
    {"CLOUD_SHELL": "true"},
    {"DEVSHELL_PROJECT_ID": "resolver-time"},
])
def test_google_cloud_shell_is_refused_before_a_wallet_can_be_made(tmp_path, marker_env):
    """Worse here than anywhere: a wallet made in a temporary terminal
    disappears with the session, and this script would print its address and
    invite the owner to send real money to it."""
    env = _env(tmp_path, "http://127.0.0.1:9")
    env.pop("HUBVIBE_WALLET_FILE")  # no wallet: it would otherwise make one
    env.update(marker_env)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env,
        cwd=REPO_ROOT, timeout=60,
    )
    assert result.returncode != 0
    assert "Cloud Shell" in result.stdout
    assert "Browser terminal" in result.stdout or "ssh root@" in result.stdout
    assert not (tmp_path / ".hubvibe-wallet-key").exists(), "it made a wallet anyway"


def test_it_never_generates_a_second_wallet_over_an_existing_one(tmp_path):
    """The existing key may hold funds. Overwriting it destroys them."""
    server, rpc = _fake_rpc([1.0])
    try:
        script, _ = _stub_first_paid_call(tmp_path)
        env = _env(tmp_path, rpc)
        subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, timeout=90)
    finally:
        server.shutdown()
    assert (tmp_path / "key").read_text() == KEY, "the wallet key was modified"


def test_check_mode_answers_is_it_mine_and_where_is_the_money(tmp_path, monkeypatch):
    """`--check` exists for the two questions a person has about an address
    their own server generated, both unanswerable from a phone on
    2026-09-08: is it mine, and where did my money go.

    Neither confusion was a mistake. A wallet holding only USDC and no ETH
    has NO normal transactions, so an explorer's default tab is empty and
    the funds sit one tab over under ERC-20 transfers -- it looks exactly
    like an empty wallet. And an address with no recovery phrase, whose key
    is a file on a server, is not "yours" in any way a wallet app can show.
    """
    key = tmp_path / "key"
    key.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"], capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=180,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key),
             # An unreachable RPC: the balance may be unreadable, but the
             # address and the explanation must still come out.
             "BASE_RPC": "http://127.0.0.1:9",
             # Without --check this script waits hours for money. Bound it,
             # so removing the flag fails the test in a second instead of
             # hanging the suite.
             "WAIT_SECONDS": "0", "POLL_SECONDS": "1"},
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "0x" in out, "no address printed"
    assert str(key) in out, "it must name the file that controls the address"
    assert "no recovery phrase" in out
    assert "ERC-20" in out, "the empty-Transactions-tab trap is not explained"
    # And it must not have paid for anything.
    assert "Paying for one real call" not in out
    # A zero balance here reads only native USDC on Base. Leaving that
    # number alone would say "your money is gone" about funds sitting on
    # another chain, which is the most alarming thing this project can tell
    # someone who just sent real money. Hand it to the tool that looks.
    assert "find-my-money.sh" in out, (
        "a zero Base balance is presented as final, with nowhere to look next"
    )


def test_check_mode_never_spends(tmp_path):
    """A read-only question must stay read-only. `--check` returning before
    the funded branch is the whole point; if it ever falls through it would
    fire a real payment at someone who only wanted to look."""
    text = SCRIPT.read_text()
    check = text[text.index('if [ "${1:-}" = "--check" ]'):]
    body = check[:check.index("\nfi\n")]
    assert "exit 0" in body, "--check can fall through into the paying path"
    assert "first-paid-call.sh" not in body
