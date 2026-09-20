"""Guards for scripts/probe-facilitators.sh.

This script decides which facilitator the money and the Bazaar listing go
through, so a misread here sends the next session down the wrong path for
days -- which is the failure mode this repo has paid for four times.

The case that matters most is not a clean 404. `facilitator.xpay.sh` answers
GET /discovery/resources with **HTTP 200** carrying `{"message":"Not Found"}`,
so a status-code check alone reports "has a Bazaar index" for a facilitator
that indexes nothing. That is the same shape as every other bug in this
codebase's history: a check that proves form rather than function.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "probe-facilitators.sh"

_SUPPORTED_BASE = '{"kinds":[{"scheme":"exact","network":"eip155:8453"}]}'
_SUPPORTED_LEGACY = '{"kinds":[{"scheme":"exact","network":"base"}]}'
_SUPPORTED_OTHER = '{"kinds":[{"scheme":"exact","network":"eip155:137"}]}'
_SUPPORTED_SEPOLIA_ONLY = '{"kinds":[{"x402Version":2,"scheme":"exact","network":"eip155:84532"},{"x402Version":1,"scheme":"exact","network":"base-sepolia"}]}'
_INDEX = '{"x402Version":2,"items":[{"resource":"https://a.example"}]}'
_NOT_AN_INDEX = '{"message":"Not Found"}'


def _run(tmp_path, supported, supported_code, discovery, discovery_code):
    """Drive the probe against one stubbed facilitator."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Only probe.example answers. The script APPENDS arguments to its built-in
    # candidate list rather than replacing it, so the real defaults are probed
    # too -- they must come back unreachable, or every assertion here would be
    # about whichever default the stub happened to answer for.
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'url="${!#}"\n'
        "case \"$url\" in\n"
        '  *probe.example*supported*) printf \'%s\\n__CODE__%s\' "$STUB_SUPPORTED" "$STUB_SUPPORTED_CODE";;\n'
        '  *probe.example*discovery*) printf \'%s\\n__CODE__%s\' "$STUB_DISCOVERY" "$STUB_DISCOVERY_CODE";;\n'
        "  *) printf '\\n__CODE__000';;\n"
        "esac\n"
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_SUPPORTED"] = supported
    env["STUB_SUPPORTED_CODE"] = supported_code
    env["STUB_DISCOVERY"] = discovery
    env["STUB_DISCOVERY_CODE"] = discovery_code
    # One candidate, passed as an argument, so the defaults do not fire.
    return subprocess.run(
        ["bash", str(SCRIPT), "https://probe.example"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_200_that_is_not_an_index_is_not_counted_as_one(tmp_path):
    """THE case. xpay.sh answers /discovery/resources with 200 and
    {"message":"Not Found"} -- a status-only check calls that a Bazaar index
    and sends everyone chasing a listing that can never appear."""
    result = _run(tmp_path, _SUPPORTED_BASE, "200", _NOT_AN_INDEX, "200")
    assert "not an index" in result.stdout
    assert "KEYLESS WINNER" not in result.stdout
    assert "No keyless facilitator" in result.stdout


def test_settles_and_indexes_is_the_only_winning_combination(tmp_path):
    result = _run(tmp_path, _SUPPORTED_BASE, "200", _INDEX, "200")
    assert "KEYLESS WINNER" in result.stdout
    assert "Use this facilitator" in result.stdout
    assert "X402_FACILITATOR_URL=https://probe.example" in result.stdout


def test_a_legacy_only_facilitator_is_reported_unusable_not_a_winner(tmp_path):
    """This test used to assert the opposite -- that the legacy name "base"
    counts as Base -- on the reasoning that rejecting it would discard a
    working option on a spelling. Simulation showed it is not a working
    option: the x402 server library builds every payment's requirements
    under the CAIP-2 name and only does so when /supported lists that exact
    name, so against a legacy-only facilitator the node can verify nothing.
    A probe that calls such a facilitator a winner sends the deploy straight
    at the thing that produced two rejected live payments."""
    result = _run(tmp_path, _SUPPORTED_LEGACY, "200", _INDEX, "200")
    assert "legacy name only" in result.stdout
    assert "cannot use this facilitator" in result.stdout
    assert "KEYLESS WINNER" not in result.stdout


def test_a_testnet_only_facilitator_is_not_reported_as_a_mainnet_settler(tmp_path):
    """"eip155:84532" (Base Sepolia) contains "eip155:8453". A substring
    match called x402.org/facilitator -- Sepolia only -- a Base mainnet
    settler on 2026-09-12, the exact bug the handoff's own rule names."""
    result = _run(tmp_path, _SUPPORTED_SEPOLIA_ONLY, "200", _INDEX, "200")
    assert "Base mainnet listed" not in result.stdout, "eip155:84532 matched as eip155:8453"
    assert "Base mainnet not offered" in result.stdout
    assert "KEYLESS WINNER" not in result.stdout


def test_an_index_on_the_wrong_network_does_not_win(tmp_path):
    """Indexing is worthless if it cannot settle what this service charges."""
    result = _run(tmp_path, _SUPPORTED_OTHER, "200", _INDEX, "200")
    assert "Base mainnet not offered" in result.stdout
    assert "KEYLESS WINNER" not in result.stdout


@pytest.mark.parametrize("code", ["401", "403"])
def test_a_credentialed_facilitator_is_reported_not_silently_dropped(tmp_path, code):
    """A credentialed facilitator answers 401. That is a policy gate, not an
    absence -- and the protocol is permissionless, so a facilitator whose
    credentials come without a business review is still usable. Saying so
    keeps the option visible instead of burying it as a failure."""
    result = _run(tmp_path, "unauthorized", code, "unauthorized", code)
    assert "credentials required" in result.stdout
    assert "business review" in result.stdout


def test_an_unreachable_host_is_a_failure_not_a_pass(tmp_path):
    result = _run(tmp_path, "", "000", "", "000")
    assert "unreachable" in result.stdout
    assert "KEYLESS WINNER" not in result.stdout


def test_the_probe_only_reads(tmp_path):
    """It must be safe to run against a live money rail: GETs only, no wallet,
    no POST to /verify or /settle."""
    text = SCRIPT.read_text()
    assert "-X POST" not in text
    assert "/settle" not in text.replace("# ", "")
