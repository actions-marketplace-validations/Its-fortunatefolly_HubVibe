"""Guards for scripts/first-paid-call.sh's preflight.

This script is the only thing in the repo that spends real money on purpose,
and it spends it once. The whole point of that payment is to break the
discovery deadlock -- the Bazaar spec catalogs a resource only when a payment
payload carrying the discovery extension reaches a facilitator, so an unpaid
resource is an uncatalogued resource by construction, on every facilitator.

Which means the preflight is not a nicety. If the deployed revision emits a
Bazaar record the facilitator will reject on validation, the payment settles
and buys no index entry, and the one shot at bootstrapping discovery is gone.
The gate below is what makes the money conditional on the call being able to
do its job.

The preflight is extracted and driven against synthetic challenges here, so
every rejection path is exercised without a network or a wallet.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "first-paid-call.sh"


def _preflight_source() -> str:
    """The embedded python preflight, on its own."""
    text = SCRIPT.read_text()
    start = text.index("import json, sys", text.index("PREFLIGHT="))
    end = text.index("')", start)
    return text[start:end]


def _run(challenge: str):
    """Feed one challenge body through the preflight; return (verdict, detail)."""
    result = subprocess.run(
        [sys.executable, "-c", _preflight_source()],
        input=challenge,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    verdict, _, detail = out.partition("\t")
    return verdict, detail


GOOD_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _challenge(*, pay_to=GOOD_ADDR, scheme="exact", bazaar_input=...,
               x402_version=1, drop_field=None):
    import json

    if bazaar_input is ...:
        bazaar_input = {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": {"url": "https://example.com"},
        }
    entry = {
        "scheme": scheme,
        "network": "base",
        "maxAmountRequired": "30000",
        "resource": "https://example.test/audit/wcag",
        "payTo": pay_to,
        "maxTimeoutSeconds": 300,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
    if drop_field:
        entry.pop(drop_field)
    body = {"price": "$0.03", "x402Version": x402_version, "accepts": [entry]}
    if bazaar_input is not None:
        body["extensions"] = {"bazaar": {"info": {"input": bazaar_input}}}
    return json.dumps(body)


def test_a_well_formed_challenge_clears_the_gate():
    verdict, detail = _run(_challenge())
    assert verdict == "OK", detail
    assert GOOD_ADDR in detail


def test_no_x402_rail_means_nothing_is_paid():
    """There is no rail to pay on. Attempting anyway would burn a call for a
    402 that repeats itself."""
    verdict, detail = _run(_challenge(scheme="something-else"))
    assert verdict == "FAIL"
    assert "no payable x402 entry" in detail


def test_a_pre_61_challenge_is_refused_by_name():
    """The shape this script was originally written against. accepts[] with no
    x402Version means no v1 client reads it -- and the preflight itself read
    the pre-#61 field names for a while, so it reported "does not advertise
    x402" about a node that did. Name the real cause instead."""
    verdict, detail = _run(_challenge(x402_version=None))
    assert verdict == "FAIL"
    assert "x402Version:1" in detail
    assert "repair-and-deploy" in detail


def test_an_accepts_entry_missing_spec_fields_is_refused():
    """A client validates every entry and raises before signing. Paying into
    that spends nothing and proves nothing."""
    verdict, detail = _run(_challenge(drop_field="asset"))
    assert verdict == "FAIL"
    assert "asset" in detail
    assert "before" in detail


def test_the_zero_address_is_refused_before_any_signature():
    """0x + 40 zeros is shape-valid and unownable -- USDC reverts transfers to
    it. This exact address was deployed for days in 2026-08 while every shape
    check passed. Paying it would destroy the money and prove nothing."""
    verdict, detail = _run(_challenge(pay_to="0x" + "0" * 40))
    assert verdict == "FAIL"
    assert "zero address" in detail


def test_a_challenge_with_no_bazaar_record_is_refused():
    """Settling here would work and index nothing, which spends the one
    bootstrap payment for half its purpose."""
    verdict, detail = _run(_challenge(bazaar_input=None))
    assert verdict == "FAIL"
    assert "index nothing" in detail


def test_a_bazaar_record_missing_its_method_is_refused():
    """The #52 bug, seen from the paying side. A record without `method` fails
    the facilitator's own validator, so it is discarded before cataloguing --
    the payment settles and buys no index entry. If the live 402 still looks
    like this, the deployed revision predates the fix and the fix has to ship
    before the money does."""
    verdict, detail = _run(
        _challenge(
            bazaar_input={
                "type": "http",
                "bodyType": "json",
                "body": {"url": "https://example.com"},
            }
        )
    )
    assert verdict == "FAIL"
    assert "names no HTTP method" in detail
    assert "repair-and-deploy" in detail


def test_an_mcp_record_needs_no_method():
    """Only body records carry a method. Requiring one of an mcp-type record
    would refuse a perfectly catalogable call."""
    verdict, _ = _run(
        _challenge(bazaar_input={"type": "mcp", "toolName": "audit_wcag"})
    )
    assert verdict == "OK"


def test_a_non_json_response_is_refused_rather_than_parsed_as_empty():
    """A node that is down returns an HTML error page. Treating that as an
    empty challenge would fall through to 'no x402 advertised', which reads as
    a config problem and sends the next person to the wrong place."""
    verdict, detail = _run("<html>502 Bad Gateway</html>")
    assert verdict == "FAIL"
    assert "not JSON" in detail


def test_the_script_never_retries_a_payment():
    """A retried payment is a double charge. The script must make exactly one
    signed attempt -- no loop, no retry helper, anywhere in the paying block."""
    import re

    text = SCRIPT.read_text()
    paying = text[text.index("step \"Paying for one real call"):text.index("step \"Re-reading")]
    # Loop keywords as STATEMENTS, not English. `for` was already read that
    # way; `done` was not, so any sentence containing the word tripped it --
    # "the facilitator declined the transfer AFTER the work was done" did,
    # in the message explaining a refused settle. A guard that fires on prose
    # gets edited around or switched off, which is how a real one stops being
    # trusted. `done` closing a loop is a statement, and only that is matched.
    def loop_statements(block):
        return [
            line for line in block.splitlines()
            if re.match(r"\s*(for|while|until|done)\b", line)
        ]

    assert not loop_statements(paying), (
        f"the paying block loops: {loop_statements(paying)} -- a retry around "
        "a signature is a double charge"
    )
    # And it still catches one, so the narrowing above did not blunt it.
    assert loop_statements("  for i in 1 2 3; do\n    pay\n  done\n"), (
        "the loop check no longer detects a loop"
    )


def test_no_wallet_anywhere_stops_before_the_node_is_touched(tmp_path):
    """Fail closed at the top rather than partway through, and say the two
    things that actually unstick someone: the export may simply have been lost
    (Cloud Shell drops env on reconnect, which is exactly how this presented),
    and a wallet can be made right here."""
    env = {"PATH": "/usr/bin:/bin", "HUBVIBE_WALLET_FILE": str(tmp_path / "nope")}
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )
    assert result.returncode == 1
    assert "No paying wallet" in result.stdout
    assert "Cloud Shell drops env on" in result.stdout
    assert "--new-wallet" in result.stdout


def test_an_unset_HOME_does_not_crash_before_the_wallet_message(tmp_path):
    """set -u makes a bare $HOME fatal where HOME is not set. Dying on an
    unbound variable before the wallet message prints is the least useful
    failure this script could have."""
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"}, cwd=REPO_ROOT,
    )
    assert "unbound variable" not in result.stderr
    assert "No paying wallet" in result.stdout


def test_the_wallet_file_is_used_when_the_env_var_is_empty(tmp_path):
    """The whole point of the file: an export that did not survive a reconnect
    must not look like having no wallet at all."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file), "BASE": "http://127.0.0.1:9"},
    )
    assert "wallet key from" in result.stdout
    assert "No paying wallet" not in result.stdout


def test_refusing_to_overwrite_still_shows_the_wallet_you_have(tmp_path):
    """Refusing is right -- overwriting a key that may hold funds destroys
    them. Refusing silently is not. The wallet you already own is the answer
    to the question just asked, and its address is the next thing needed; a
    STOP without it sends someone hunting for a wallet they already have."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--new-wallet"], capture_output=True, text=True,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file)},
    )
    assert result.returncode == 1
    assert "already have a wallet" in result.stdout
    # The address the existing key actually signs for, not a placeholder.
    from eth_account import Account
    assert Account.from_key(key_file.read_text()).address in result.stdout
    assert "USDC on Base" in result.stdout
    assert key_file.read_text() == "0x" + "1" * 63 + "2", "the key was modified"


def test_an_unreadable_key_file_says_so_instead_of_printing_nothing(tmp_path):
    """A truncated or garbage file is not a wallet. Falling through to a blank
    address would be worse than the silent stop it replaced."""
    key_file = tmp_path / "key"
    key_file.write_text("not-a-key")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--new-wallet"], capture_output=True, text=True,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file)},
    )
    assert "does not contain a readable private key" in result.stdout
    assert "HUBVIBE_FORCE_NEW_WALLET=1" in result.stdout


# --- the paying wallet must not be the recipient ---------------------------
#
# The owner's Base wallet is the natural thing to reach for and it is also
# X402_PAY_TO_ADDRESS, so this mistake is one paste away. Nothing else catches
# it: verified by running the script against a node whose payTo was the
# payer's own address -- the x402 client produced a signature without
# complaint. The failure would land at the facilitator, on the one call whose
# entire purpose is to prove the facilitator settles a real payment.
#
# These drive the whole shell script with curl stubbed, because the guard is
# in the shell after the embedded preflight, and the preflight-only harness
# above cannot see it.

# Deterministic throwaway keys. Never funded; they exist so the payer address
# is known to the test rather than generated per run.
KEY_A = "0x" + "11" * 32
ADDR_A = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
KEY_B = "0x" + "22" * 32


def _drive(tmp_path, wallet_key, pay_to, extra_env=None):
    """Run the real script with curl stubbed to serve one challenge."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    challenge = tmp_path / "challenge.json"
    challenge.write_text(_challenge(pay_to=pay_to))

    # The script asks curl for the body then the status code on its own line
    # (-w '\n%{http_code}'); the stub answers the same way.
    (bin_dir / "curl").write_text(
        f'#!/usr/bin/env bash\ncat "{challenge}"\nprintf "\\n402"\n'
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["HUBVIBE_WALLET_KEY"] = wallet_key
    env["BASE"] = "https://example.test"
    env.pop("HUBVIBE_ALLOW_SELF_PAYMENT", None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=180
    )


def test_paying_from_the_recipient_wallet_is_refused(tmp_path):
    result = _drive(tmp_path, wallet_key=KEY_A, pay_to=ADDR_A)
    assert "The paying wallet IS the recipient" in result.stdout
    assert result.returncode == 1
    # It must stop BEFORE spending: no payment attempt, no settlement report.
    assert "Paying for one real call" not in result.stdout


def test_the_recipient_check_is_case_insensitive(tmp_path):
    """EIP-55 checksummed and all-lowercase spellings are the same address.
    Comparing them raw would let the mistake through on a lowercase paste --
    which is exactly the form a wallet app's copy button produces."""
    result = _drive(tmp_path, wallet_key=KEY_A, pay_to=ADDR_A.lower())
    assert "The paying wallet IS the recipient" in result.stdout
    assert result.returncode == 1


def test_a_different_paying_wallet_passes_the_guard(tmp_path):
    """The guard must stop one specific mistake, not become a gate on the
    normal case it exists to protect."""
    result = _drive(tmp_path, wallet_key=KEY_B, pay_to=ADDR_A)
    assert "The paying wallet IS the recipient" not in result.stdout


def test_the_self_payment_refusal_is_overridable(tmp_path):
    """It may well be a valid transfer. Refusing to let the owner try it is
    not this script's call -- refusing to let them do it BY ACCIDENT is."""
    result = _drive(
        tmp_path, wallet_key=KEY_A, pay_to=ADDR_A,
        extra_env={"HUBVIBE_ALLOW_SELF_PAYMENT": "1"},
    )
    assert "self-transfer, allowed by override" in result.stdout
    assert "The paying wallet IS the recipient" not in result.stdout


def test_an_unreadable_balance_hands_over_the_basescan_link(tmp_path):
    """The Base RPC has failed from Cloud Shell on every run so far, and the
    script proceeds without the check -- so a rejection cannot be told apart
    from an empty wallet. When the script cannot answer that question it must
    hand over the page that can, for the exact paying address."""
    from eth_account import Account

    result = _drive(tmp_path, wallet_key=KEY_B, pay_to=ADDR_A,
                    extra_env={"BASE_RPC": "http://127.0.0.1:9"})
    payer = Account.from_key(KEY_B).address
    assert "proceeding without the check" in result.stdout
    assert f"https://basescan.org/address/{payer}" in result.stdout


def test_a_settled_call_prints_the_transaction_link():
    """The receipt is the proof. After a settlement the script must print
    the Basescan link for the transaction the node handed back in
    PAYMENT-RESPONSE, and must say so plainly when the node sent none --
    an empty link would read as a settlement with no transaction."""
    text = SCRIPT.read_text()
    pay_block = text[text.index("Paying for one real call"):]
    assert "booth.last_settlement" in pay_block, "the receipt is never read off the client"
    assert "https://basescan.org/tx/$TX" in pay_block
    assert "no PAYMENT-RESPONSE receipt came back" in pay_block


def test_a_non_json_response_shows_the_status_and_the_body():
    """"not JSON -- is the node up?" was all the owner got on 2026-09-04,
    with the HTTP status and the body discarded. Both go in the message."""
    import os

    result = subprocess.run(
        [sys.executable, "-c", _preflight_source()],
        input="<html><title>503 Service Unavailable</title></html>",
        capture_output=True, text=True,
        env={**os.environ, "STATUS": "503"},
    )
    detail = result.stdout.strip().partition("\t")[2]
    assert "HTTP 503" in detail
    assert "503 Service Unavailable" in detail
    assert "cold-starting" in detail


def test_a_cold_start_5xx_on_the_free_read_is_retried(tmp_path):
    """min-instances is 0: the first request after idle can get Cloud Run's
    own 503 page while the container boots. Reading the challenge costs
    nothing, so the script tries again instead of stopping on it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    challenge = tmp_path / "challenge.json"
    challenge.write_text(_challenge(pay_to=ADDR_A))
    counter = tmp_path / "calls"
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo $n > "{counter}"\n'
        'if [ "$n" -eq 1 ]; then printf "<html>Service Unavailable</html>\\n503"; exit 0; fi\n'
        f'cat "{challenge}"\nprintf "\\n402"\n'
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["HUBVIBE_WALLET_KEY"] = KEY_B
    env["BASE"] = "https://example.test"
    env["HUBVIBE_RETRY_SLEEP"] = "0"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            env=env, timeout=180)

    assert "HTTP 503 from the node (attempt 1 of 3" in result.stdout
    assert "x402 advertised" in result.stdout, result.stdout
    assert "not JSON" not in result.stdout


def test_the_paying_block_still_has_no_loop_after_the_read_retry():
    """The retry lives on the free read only. This pins where the loop ends
    relative to where the money starts."""
    text = SCRIPT.read_text()
    read_block = text[text.index('step "Reading the live 402'):text.index('step "Paying for one real call')]
    assert "for attempt in 1 2 3" in read_block


def test_the_client_is_installed_into_a_venv_never_with_bare_pip():
    """A fresh Ubuntu VPS has python3 but no `pip` command and refuses
    system-wide installs (PEP 668). The first real run died at
    `pip: command not found`. The script must build its own environment
    and reach pip through the interpreter."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh").read_text()
    assert "python3 -m venv" in text, "no private environment is created"
    assert 'export PATH="$VENV/bin:$PATH"' in text, "the venv is not put on PATH for the rest of the script"
    assert "python3 -m pip install" in text
    bare = [line for line in text.splitlines() if re.match(r"^\s*pip\s+install", line)]
    assert not bare, "bare `pip install` again: " + "; ".join(bare)
    assert "python3-venv" in text, "no apt fallback for a box whose python3 lacks the venv module"


def test_a_recovery_phrase_pays_from_the_owners_own_wallet(tmp_path):
    """The Base app exports a recovery phrase, not a raw key. The script
    derives the app's first account from it in memory, names the address,
    and refuses a phrase that derives some other wallet."""
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh"
    phrase = tmp_path / ".hubvibe-wallet-phrase"
    phrase.write_text("test test test test test test test test test test test junk\n")
    venv_bin = str(Path(__file__).resolve().parent.parent / ".venv" / "bin")
    env = {"HOME": str(tmp_path), "PATH": f"{venv_bin}:/usr/bin:/bin", "BASE": "http://127.0.0.1:9"}

    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=180, env=env).stdout
    assert "paying from your own wallet" in out
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out, "the first account of the standard test phrase"
    assert "delete the phrase" in out
    assert "New Base wallet created" not in out, "a phrase must never mint a throwaway wallet"
    assert not (tmp_path / ".hubvibe-wallet-key").exists(), "the derived key must not be written to disk"

    # One phrase, many accounts: the wallet the owner funded may not be the
    # app's first. Naming it finds it rather than failing as "wrong phrase".
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=180,
        env={**env, "HUBVIBE_EXPECT_ADDRESS": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"},
    ).stdout
    assert "account 2" in out, out[:400]
    assert "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC" in out

    # A phrase that genuinely does not hold the wallet says so, and lists what
    # it does hold, instead of sending the owner after a second seed.
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=180,
        env={**env, "HUBVIBE_EXPECT_ADDRESS": "0x37555E884c5EbA10f6E816DbecEA30965B9b38C0"},
    ).stdout
    assert "STOP" in out and "first 10 accounts" in out
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out, "must show what the phrase does derive"
    assert os.environ.get("HUBVIBE_WALLET_KEY") is None


def test_an_empty_wallet_is_not_reported_as_a_broken_rail():
    """The facilitator refusing for want of funds means the signature was
    VALID -- everything up to the money is working. The generic branch told
    the owner their rail was broken and to fix it "before spending any
    effort on demand" (2026-09-07), which is a checker lying about the one
    thing it exists to report."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh").read_text()
    failure = text.split("the payment did not go through", 1)[1].split("esac", 1)[0]

    assert "insufficient_balance" in failure, "the empty-wallet case is not distinguished"
    assert "insufficient_funds" in failure, "only one of the two facilitator spellings is handled"
    assert "The rail is fine" in failure, "an empty wallet must not read as a broken rail"
    assert "$PAYER" in failure, "must name the address to fund"

    # The alarming wording must survive for the case it was written for: a
    # real rejection (bad signature, wrong network, unreachable facilitator)
    # IS the answer worth having.
    generic = failure.split("*)", 2)[-1]
    assert "answer worth having" in generic
    assert "insufficient" not in generic, "the alarm now fires on an empty wallet again"


def test_the_handoff_names_the_key_file_the_script_actually_uses():
    """The runbook told the owner the payer key was at ~/.hubvibe-payer-key;
    the script has always written ~/.hubvibe-wallet-key. Nobody hit it while
    only scripts read the file -- but the owner asking "who is 0x104f, and
    why should I send it money" (2026-09-08) is answered by deriving the
    address from that key on the box, and a runbook that names a file which
    does not exist turns the one available proof into another dead end.

    Read off both files rather than restated, so they cannot drift again --
    the same guard the facilitator defaults already carry.
    """
    root = Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "first-paid-call.sh").read_text()
    handoff = (root / "docs" / "HANDOFF.md").read_text()

    default = re.search(r'WALLET_FILE="\$\{HUBVIBE_WALLET_FILE:-\$\{HOME:-/tmp\}/([^}"]+)\}"', script)
    assert default, "the script's wallet-file default is no longer where this test looks"
    name = default.group(1)

    assert name in handoff, (
        f"the script writes the payer key to ~/{name}, and the handoff does not say so"
    )
    for wrong in ("hubvibe-payer-key",):
        assert wrong not in handoff, f"the handoff still names ~/{wrong}, which nothing writes"


@pytest.mark.parametrize("marker", [
    {"CLOUD_SHELL": "true"},
    {"DEVSHELL_PROJECT_ID": "resolver-time"},
])
def test_google_cloud_shell_is_refused_by_name(tmp_path, marker):
    """The paying wallet and its key live on the box. Run from Cloud Shell --
    a temporary container with its own home directory -- this script finds no
    key, or a leftover from an old session, and reports a wallet problem when
    the only problem is the machine. The owner hit exactly that on 2026-09-08.
    Refuse before any wallet is read, so the diagnosis is the cause and not
    the symptom, and say where to run it instead."""
    # A perfectly good key is present: being in Cloud Shell must lose to
    # nothing, or the guard is decorative.
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "HUBVIBE_WALLET_FILE": str(key_file), "BASE": "http://127.0.0.1:9"}
    env.update(marker)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Cloud Shell" in combined, "the message must name the terminal it refused"
    assert "Browser terminal" in combined or "ssh root@" in combined, \
        "refusing without saying where to run it strands the reader"
    assert "No payment was attempted" in combined
    # It must stop BEFORE resolving a wallet -- otherwise the reader is told
    # about a key when the real fault is the machine.
    assert "wallet key from" not in combined


def test_a_file_that_is_not_a_phrase_is_stepped_over_not_fatal(tmp_path):
    """A five-word leftover in a home directory is litter, not a payment
    instruction. It aborted the owner's whole run on 2026-09-08 with a word
    count -- hiding both the working key beside it and the real problem. Warn,
    name the file, and use the key."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    phrase_file = tmp_path / "phrase"
    phrase_file.write_text("these are only five words\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file),
             "HUBVIBE_WALLET_PHRASE_FILE": str(phrase_file),
             "BASE": "http://127.0.0.1:9"},
    )
    combined = result.stdout + result.stderr
    assert "wallet key from" in combined, "the real wallet was not reached"
    assert str(phrase_file) in combined, "the junk file must be named so it can be deleted"
    assert "could not derive a wallet" not in combined, \
        "litter is being reported as a failed payment instruction"


def test_a_malformed_HUBVIBE_WALLET_MNEMONIC_is_still_an_error(tmp_path):
    """Exporting the variable IS an instruction. Silently paying from some
    other wallet because the instruction was malformed would be worse than
    stopping -- the money would leave an account the owner did not choose."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file),
             "HUBVIBE_WALLET_MNEMONIC": "these are only five words",
             "BASE": "http://127.0.0.1:9"},
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "HUBVIBE_WALLET_MNEMONIC is not a recovery phrase" in combined
    assert "wallet key from" not in combined, "it fell through to another wallet"


# --- Which facilitator can possibly index this payment ----------------------
#
# In x402 the RESOURCE SERVER calls its own facilitator to verify and settle;
# the payer is never told which one. A Bazaar entry can therefore only appear
# where the node's PaymentPayload actually landed. This script used to read
# Dexter's index unconditionally and, finding nothing, report "indexing may
# lag" -- which is false, not merely unhelpful, when the node settles through
# a facilitator that runs no Bazaar at all. The owner hit exactly that on
# 2026-09-08: money moved, the audit came back, and the final line sent them
# to re-check an index their payment had never reached.


def _facilitator_note(tmp_path, env_body=None, **extra_env):
    """Run the script far enough to print which index it will read.

    BASE points at a closed port, so it stops at the live 402 and spends
    nothing; the facilitator line is printed before any of that.
    """
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "HUBVIBE_WALLET_FILE": str(key_file), "BASE": "http://127.0.0.1:9"}
    if env_body is not None:
        node_env = tmp_path / "node.env"
        node_env.write_text(env_body)
        env["HUBVIBE_NODE_ENV_FILE"] = str(node_env)
    else:
        env["HUBVIBE_NODE_ENV_FILE"] = str(tmp_path / "absent.env")
    env.update(extra_env)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )
    return result.stdout + result.stderr


def test_the_index_it_reads_is_the_one_the_node_settles_through(tmp_path):
    """The node's own .env is the only authority on which facilitator sees
    the payment, and this script runs on the box, so the file is right there.
    Read it -- do not assume the repo's current default is what the live
    service was installed with. The box was installed on xpay.sh before the
    defaults moved to Dexter, which is how this went wrong for real."""
    out = _facilitator_note(
        tmp_path, "X402_PAY_TO_ADDRESS=0xabc\nX402_FACILITATOR_URL=https://facilitator.xpay.sh\n"
    )
    assert "https://facilitator.xpay.sh" in out, (
        "the script is reading some other facilitator's index than the node's"
    )
    assert "node.env" in out, "it must say where it learned that, or it cannot be checked"


def test_a_trailing_slash_or_quotes_in_the_env_do_not_become_a_different_host(tmp_path):
    """`.env` files are hand-edited. A quoted or slash-terminated value is the
    same facilitator, and treating it as a different one puts the script back
    to guessing."""
    out = _facilitator_note(tmp_path, 'X402_FACILITATOR_URL="https://x402.dexter.cash/"\n')
    assert "https://x402.dexter.cash" in out
    assert '"' not in out.split("Bazaar index will be read from")[1].split("--")[0]


def test_an_exported_FACILITATOR_still_wins(tmp_path):
    """Exporting it is someone deliberately asking about another index."""
    out = _facilitator_note(
        tmp_path,
        "X402_FACILITATOR_URL=https://facilitator.xpay.sh\n",
        FACILITATOR="https://x402.dexter.cash",
    )
    assert "https://x402.dexter.cash" in out
    assert "you exported" in out


def test_an_unreadable_node_env_is_called_unconfirmed_not_assumed(tmp_path):
    """Falling back to the default is fine. Falling back silently is not:
    every later line about the index then rests on a guess, and the reader
    has no way to know it."""
    out = _facilitator_note(tmp_path, None)
    assert "https://facilitator.payai.network" in out
    assert "UNCONFIRMED" in out, "a guessed facilitator must not read as a known one"


def test_lag_is_only_claimed_for_the_facilitator_that_settled():
    """Static, because reaching this line costs $0.03. 'Indexing may lag' is
    a promise that waiting will work. It must be made only where the payment
    actually landed; everywhere else the honest answer names the .env to
    check and the switch that fixes it."""
    text = SCRIPT.read_text()
    tail = text[text.index('step "Re-reading the Bazaar index"'):]
    lag = tail.index("may simply lag")
    guard = tail.index('[ "$FACILITATOR" = "$NODE_FACILITATOR" ]')
    assert guard < lag, "the lag message is not gated on the node's own facilitator"

    unknown = tail[tail.index("\nelse", lag):]
    assert "grep X402_FACILITATOR_URL" in unknown, (
        "the not-our-index branch must name the command that settles the question"
    )
    assert "switch-facilitator.sh" in unknown, (
        "and the one that fixes it, or the reader is left with a diagnosis and no exit"
    )


def test_a_facilitator_with_no_index_names_the_switch_before_the_money_is_spent():
    """Learning that the node can never be indexed AFTER paying is learning it
    too late to act on cheaply."""
    text = SCRIPT.read_text()
    baseline = text[text.index('step "Baselining the facilitator\'s Bazaar index"'):
                    text.index('step "Paying for one real call')]
    assert "switch-facilitator.sh" in baseline, (
        "the no-index warning does not say how to get an index"
    )


# --- Recognising ourselves in someone else's index --------------------------


def _index_hits(blob, pay_to="0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
                base="https://hubvibe-io.com"):
    """Drive the script's own index matcher, extracted, with no network."""
    text = SCRIPT.read_text()
    start = text.index("index_hits() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    prog = "BASE=%s\nPAY_TO=%s\n%s\nindex_hits \"$1\"\n" % (
        shlex.quote(base), shlex.quote(pay_to), text[start:end]
    )
    result = subprocess.run(
        ["bash", "-c", prog, "_", blob], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_a_lowercased_address_in_the_index_is_still_us():
    """EIP-55 checksumming is presentation. Facilitators serialise addresses
    however their own storage happens to hold them, and a case-sensitive
    match calls a listed node unlisted -- permanently, and identically to
    never being listed at all. The owner's 2026-09-08 run reported no entry;
    this is the first thing that has to be ruled out before anyone concludes
    the payment did not register."""
    checksummed = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
    assert _index_hits('[{"payTo":"%s"}]' % checksummed.lower()) == 1
    assert _index_hits('[{"payTo":"%s"}]' % checksummed) == 1
    assert _index_hits('[{"payTo":"%s"}]' % checksummed.upper()) == 1


def test_an_index_keyed_by_resource_url_still_finds_us():
    """A Bazaar record's primary key is the resource, and some serialisations
    carry no payTo at all. Looking only for an address then misses an entry
    that is plainly ours."""
    assert _index_hits('[{"resource":"https://hubvibe-io.com/audit/wcag"}]') == 1


def test_somebody_elses_entry_is_not_counted_as_ours():
    """The match has to be able to say no, or 'INDEXED' means nothing."""
    assert _index_hits('[{"payTo":"0x0000000000000000000000000000000000000001",'
                       '"resource":"https://example.com/x"}]') == 0
    # A host that merely contains ours as a substring is a different host.
    assert _index_hits('[{"resource":"https://hubvibe-io.com.evil.test/x"}]') == 1, (
        "documented limit: substring matching accepts this; the count is a "
        "delta against a pre-payment baseline, so it cannot silently pass"
    )


# --- A delivered audit is not a paid one ------------------------------------


def _settlement_verdict(result_json, spent="0.0300", tx=""):
    """Drive the script's post-payment branch over one audit body.

    Extracted and run standalone: reaching it for real costs $0.03 and needs
    a facilitator, and this is the branch that decides whether the owner is
    told revenue started.
    """
    text = SCRIPT.read_text()
    start = text.index("SPENT=$(printf '%s' \"$PAID\" | cut -f2)")
    end = text.index('step "Re-reading the Bazaar index"')
    block = text[start:end]
    prog = (
        "PAID=$(printf '%%s\\t%%s\\t%%s\\t%%s' OK %s %s %s)\n"
        "SCRIPT_DIR=/repo\nPAY_TO=0xabc\n"
        "ok()   { printf 'OK %%s\\n' \"$1\"; }\n"
        "warn() { printf 'NOTE %%s\\n' \"$1\"; }\n"
        "%s" % (shlex.quote(spent), shlex.quote(tx or ""),
                shlex.quote(result_json), block)
    )
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)


FAILED_BODY = ('{"status": "ok", "pass": true, "billing_warning": "payment '
               'settlement failed after the audit ran; this call was not charged"}')


def test_a_refused_settle_is_never_reported_as_settled():
    """The node delivers the audit anyway -- deliberately, the lesser evil
    versus charging for undelivered work -- and admits it in billing_warning.
    Reading only the HTTP status turns that admission into a success report.
    On 2026-09-08 this script printed `settled $0.03 and the audit returned a
    result` directly above a body saying the call was not charged, and the
    owner was told revenue had started when no money had moved."""
    r = _settlement_verdict(FAILED_BODY)
    assert r.returncode == 1, "a call that was not paid for must not exit 0"
    assert "OK settled" not in r.stdout, "it still claims a settlement that did not happen"
    assert "NOT paid" in r.stdout
    # And it must hand over the command that finds the reason. The pattern
    # itself is pinned against the module's log lines by
    # test_the_settle_diagnostic_matches_every_failure_log; here it only has
    # to be offered at all.
    assert "x402 settle" in r.stdout, "the log query that names the reason is not offered"


def test_a_refused_settle_does_not_blame_the_deployed_revision():
    """A settle that never happened has no hash to report. Blaming the
    deployed revision for the missing receipt sends the reader off to rebuild
    a node that is working."""
    r = _settlement_verdict(FAILED_BODY)
    assert "predates" not in r.stdout, (
        "a refused settle is being reported as a stale deployment"
    )


def test_a_pending_settle_says_the_call_IS_charged():
    """Pending is not free. Telling someone their call was not charged when
    the transfer is in flight invites a second payment for one audit."""
    r = _settlement_verdict('{"billing_warning": "payment settlement is pending '
                            'on-chain (transaction 0xdead); this call is being charged"}')
    assert r.returncode == 0
    assert "IS charged" in r.stdout
    assert "do not pay again" in r.stdout.lower()


def test_an_unknown_settle_warns_against_re_running():
    """The transfer may still land. A re-run here is how one audit gets paid
    for twice."""
    r = _settlement_verdict('{"billing_warning": "payment settlement status is '
                            'unknown: the facilitator did not answer in time"}')
    assert r.returncode == 0
    assert "pay twice" in r.stdout


def test_a_clean_body_still_reports_a_settlement():
    """The guard must not swallow the case it exists to let through."""
    r = _settlement_verdict('{"status": "ok", "pass": true, "violations": []}',
                            tx="0xfeed")
    assert r.returncode == 0
    assert "OK settled $0.0300" in r.stdout
    assert "basescan.org/tx/0xfeed" in r.stdout


def test_the_settle_diagnostic_matches_every_failure_log():
    """A grep is only as good as the string the code actually prints.

    settle_sync fails three ways and logs three different sentences:
    "x402 settle REFUSED after delivery", "x402 settle TIMED OUT after
    delivery", and -- via _log_rejection on the generic except -- "x402
    settle FAILED before the facilitator could answer". On 2026-09-08 the
    owner was handed `grep "settle REFUSED"` for a run whose body said
    settlement FAILED. It printed nothing, which reads as "the node logged
    no failure" rather than "you asked for the wrong sentence", and the box
    looked broken when it was working exactly as written.

    So: whatever this script tells the owner to grep must match ALL THREE
    lines as the code emits them. Both sides are read off their files.
    """
    script = SCRIPT.read_text()
    module = (REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py").read_text()

    # The pattern the script hands over, taken out of the command it prints.
    printed = re.search(r'grep -i "([^"]+)" \| tail', script)
    assert printed, "the script no longer prints a greppable settle diagnostic"
    pattern = printed.group(1).lower()

    # Every settle-failure sentence, taken out of the module. The stage is a
    # %s at the logging call, so reconstruct it the way logging would.
    sentences = [
        line.strip().strip('"').replace("x402 %s ", "x402 settle ")
        for line in module.splitlines()
        if '"x402 %s ' in line or '"x402 settle ' in line
    ]
    failures = [s for s in sentences if any(
        w in s for w in ("REFUSED", "TIMED OUT", "FAILED", "REJECTED"))]
    assert len(failures) >= 3, (
        f"expected at least three settle/verify failure log lines, found {failures}"
    )

    missed = [s for s in failures if pattern not in s.lower()]
    assert not missed, (
        f"the script tells the owner to grep {pattern!r}, which does not match: "
        f"{missed} -- an unmatched failure logs silence and reads as no failure"
    )


def test_the_diagnostic_does_not_depend_on_the_compose_project_name():
    """`docker compose logs` run from a directory whose project name does not
    match the running stack prints nothing and exits 0 -- the same silence as
    a real absence of matching lines. Offer a fallback that asks the daemon
    directly, so an empty first answer can be told apart from a wrong one."""
    script = SCRIPT.read_text()
    block = script[script.index("settlement failed after the audit ran"):]
    block = block[:block.index("exit 1")]
    assert "docker ps -qf" in block and "docker logs" in block, (
        "no compose-independent fallback is offered for reading the node's log"
    )
