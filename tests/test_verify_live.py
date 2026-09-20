"""Guards for scripts/verify-live.sh's paid-path check.

Why this file exists: verify-live.sh reported 28/28 passing while the live
service returned HTTP 500 to every authenticated caller. It only ever asserted
that paid routes refuse UNauthenticated requests -- the cheap half of the
contract -- so the revenue path could be completely dead and the checker would
still be green.

The paid-path block is extracted and driven against a stubbed curl here, so
each HTTP status it can encounter is exercised without touching the network or
spending money. The distinctions matter: 500 and 402 and 502 have entirely
different causes, and a checker that collapses them is barely better than none.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-live.sh"

_HARNESS_PREAMBLE = """#!/usr/bin/env bash
set -uo pipefail
BASE="https://stub"
PASSES=0; FAILURES=0
pass() { echo "PASS|$1"; PASSES=$((PASSES+1)); }
fail() { echo "FAIL|$1"; FAILURES=$((FAILURES+1)); }
"""


def _paid_path_block():
    """The paid-path section of the script, on its own."""
    text = SCRIPT.read_text()
    start = text.index('echo "The paid path:')
    end = text.index('echo "-----------------------------------------------"', start)
    return text[start:end]


def _run_block(tmp_path, status, body='{"error":"x"}', api_key="k"):
    """Drive the paid-path block with curl stubbed to return `status`."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text(
        '#!/usr/bin/env bash\n'
        'out=""\n'
        'for ((i=1;i<=$#;i++)); do a=${!i}; [ "$a" = "-o" ] && { j=$((i+1)); out=${!j}; }; done\n'
        f"[ -n \"$out\" ] && cat > \"$out\" <<'BODY'\n{body}\nBODY\n"
        f'printf "%s" "{status}"\n'
    )
    (stub_dir / "curl").chmod(0o755)

    # Stub gcloud to fail. lib-api-key.sh resolves a key out of Secret Manager
    # when HUBVIBE_API_KEY is unset, so on a real Cloud Shell the api_key=None
    # case below silently becomes "a key WAS found" and the skip-path test
    # fails there while passing in CI. Whether a key exists has to be a fact of
    # the test, not of the machine it runs on.
    (stub_dir / "gcloud").write_text("#!/usr/bin/env bash\nexit 1\n")
    (stub_dir / "gcloud").chmod(0o755)

    harness = tmp_path / "block.sh"
    harness.write_text(_HARNESS_PREAMBLE + _paid_path_block())
    # The block resolves the API key via lib-api-key.sh, found relative to
    # $BASH_SOURCE -- which here is this harness, not scripts/. Copy the real
    # lib next to it so the resolution path under test is the real one.
    shutil.copy(REPO_ROOT / "scripts" / "lib-api-key.sh", tmp_path / "lib-api-key.sh")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env.pop("HUBVIBE_API_KEY", None)
    if api_key is not None:
        env["HUBVIBE_API_KEY"] = api_key

    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=60
    )


def _block(start_marker, end_marker):
    text = SCRIPT.read_text()
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def _deployed_image_block():
    """The 'is the box running this checkout' section, on its own."""
    return _block('LOCAL_INDEX="${REPO_DIR:-}', 'echo "Discovery surface')


def _run_deployed_image_block(tmp_path, *, local_html, live_html):
    """Drive that block against a fake checkout and a stubbed live page."""
    index = tmp_path / "wcag-audit-engine" / "app" / "static" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text(local_html)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text('#!/usr/bin/env bash\nprintf "%s" "$STUB_HTML"\n')
    (stub_dir / "curl").chmod(0o755)

    harness = tmp_path / "block.sh"
    harness.write_text(
        _HARNESS_PREAMBLE + f'REPO_DIR="{tmp_path}"\n' + _deployed_image_block()
    )
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_HTML"] = live_html
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=60
    )


def _page(app_id):
    return f'<head><meta name="base:app_id" content="{app_id}" /></head>'


# --- Is the deployed node running THIS checkout? ----------------------------
#
# Every other check here passes against a stale image: an old container
# answers 200 on every route and serves a perfectly good 402. On 2026-09-07
# main carried a new Base app_id for hours while the live page served the
# old one, and the only symptom anywhere was a domain verification that
# silently never completed.


def test_a_node_running_this_checkout_passes(tmp_path):
    result = _run_deployed_image_block(
        tmp_path, local_html=_page("6a83832901463168d7e651ca"),
        live_html=_page("6a83832901463168d7e651ca"),
    )
    assert "PASS|" in result.stdout, result.stdout
    assert "FAIL|" not in result.stdout


def test_a_node_running_an_older_image_is_caught_and_named(tmp_path):
    """THE test. The failure must name both ids and the command that fixes
    it -- 'git pull' on the box is the thing people do instead, and it
    changes nothing the world can see."""
    result = _run_deployed_image_block(
        tmp_path, local_html=_page("6a83832901463168d7e651ca"),
        live_html=_page("6a8383066ea1f57fed333625"),
    )
    assert "FAIL|" in result.stdout, result.stdout
    assert "6a8383066ea1f57fed333625" in result.stdout, "the live value is not named"
    assert "6a83832901463168d7e651ca" in result.stdout, "the expected value is not named"
    assert "--build" in result.stdout, "the fix command is missing"


def test_a_live_page_with_no_tag_at_all_is_a_failure(tmp_path):
    result = _run_deployed_image_block(
        tmp_path, local_html=_page("6a83832901463168d7e651ca"),
        live_html="<head><title>old</title></head>",
    )
    assert "FAIL|" in result.stdout
    assert "none" in result.stdout


def test_a_checkout_with_no_tag_says_so_rather_than_passing_silently(tmp_path):
    """A check that silently skips converts 'unverified' into 'verified' in
    the reader's head -- this repo has paid for that twice."""
    result = _run_deployed_image_block(
        tmp_path, local_html="<head><title>no tag</title></head>",
        live_html=_page("6a83832901463168d7e651ca"),
    )
    assert "PASS|" not in result.stdout
    assert "FAIL|" not in result.stdout
    assert "NOTE" in result.stdout


def _challenge_block():
    """The x402 challenge section, on its own."""
    return _block('echo "402 challenge is machine-actionable"', 'echo "MCP endpoint')


def _discovery_block():
    """The Bazaar / capability-discovery section, on its own."""
    return _block(
        'echo "Machine discovery:', "# A manifest that describes its inputs"
    )


_SPEC_ENTRY = {
    "scheme": "exact",
    "network": "base",
    "maxAmountRequired": "30000",
    "resource": "https://stub/audit/wcag",
    "description": "HubVibe site audit",
    "mimeType": "application/json",
    "payTo": "0x32b08c5e927c69877d0fcab35618c265674922bc",
    "maxTimeoutSeconds": 300,
    "asset": "0xa0b8",
    "extra": {},
}

_BAZAAR = {"bazaar": {"info": {"input": {"type": "http", "method": "POST"}}}}


def _run_x402_block(
    tmp_path,
    block,
    methods=("x402", "mpp-tempo", "stripe_api_key"),
    accepts=(),
    v2_header=True,
    bazaar=True,
    manifest_json=None,
    mpp_header=True,
):
    """Drive an x402-aware block against a stubbed node.

    `methods` is what /.well-known/agent.json claims; the rest is what the 402
    actually carries. The whole point of the checks under test is that those
    two can disagree, so the harness has to be able to make them disagree.
    """
    import json

    challenge = {
        "error": "payment_required",
        "price_usd": 0.03,
        "x402Version": 1,
        "accepts": list(accepts),
        # The API-key rail rides in other_rails, and a separate check asserts
        # the manifest and the challenge agree about it -- so the stub has to
        # keep those two in step or every run trips a check it is not testing.
        "other_rails": (
            [{"protocol": "api_key", "method": "stripe_api_key"}]
            if "stripe_api_key" in methods
            else []
        ),
    }
    if bazaar:
        challenge["extensions"] = _BAZAAR
    headers = "HTTP/2 402\r\n"
    if mpp_header:
        headers += "www-authenticate: Payment realm=stub\r\n"
    if v2_header:
        headers += "payment-required: eyJ4NDAyVmVyc2lvbiI6Mn0=\r\n"
    manifest = (
        manifest_json
        if manifest_json is not None
        else json.dumps(
            {
                "payment": {"methods": list(methods)},
                "endpoints": [],
            }
        )
    )

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in *agent.json*) printf "%s" "$STUB_MANIFEST"; exit 0;; esac; done\n'
        'for a in "$@"; do [ "$a" = "-D" ] && { printf "%s" "$STUB_HEADERS"; exit 0; }; done\n'
        'printf "%s" "$STUB_CHALLENGE"\n'
    )
    (stub_dir / "curl").chmod(0o755)

    harness = tmp_path / "block.sh"
    harness.write_text(_HARNESS_PREAMBLE + block)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_MANIFEST"] = manifest
    env["STUB_HEADERS"] = headers
    env["STUB_CHALLENGE"] = json.dumps(challenge)
    # The challenge block computes this for itself and overwrites it; the
    # discovery block reads it as already-established state.
    env["X402_STATE"] = "on" if "x402" in methods else "off"
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=60
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_live_x402_rail_still_has_to_be_payable(tmp_path):
    """The 2026-08-27 checks, unchanged: when the manifest says x402 is on, the
    402 must carry a spec-shaped accepts[] and the v2 header."""
    result = _run_x402_block(tmp_path, _challenge_block(), accepts=[_SPEC_ENTRY])
    assert "FAIL|" not in result.stdout, result.stdout
    assert "spec-shaped" in result.stdout


def test_a_live_rail_with_an_invented_accepts_shape_still_fails(tmp_path):
    """The bug that made the rail unpayable for months. Making the checks
    state-aware must not have made this one skippable."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        accepts=[{"protocol": "x402", "price": "$0.03", "pay_to": "0xabc"}],
    )
    assert "FAIL|" in result.stdout
    assert "missing" in result.stdout


def test_a_deliberately_off_rail_is_green_not_red(tmp_path):
    """x402 switched off is a correct, fail-closed state -- not two red lines.

    A checker that reports FAIL for the state the operator just asked for is a
    checker people stop reading, and worse, it invites the next session to
    'fix' a rail that was turned off on purpose."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("mpp-tempo", "stripe_api_key"),
        accepts=[],
        v2_header=False,
    )
    assert "FAIL|" not in result.stdout, result.stdout
    assert "x402 is OFF and accepts[] is empty" in result.stdout
    assert "no v2 PAYMENT-REQUIRED header" in result.stdout


def test_an_off_rail_that_is_still_advertised_in_accepts_fails(tmp_path):
    """The failure that actually matters when the rail is off: the config said
    stop, and the 402 kept selling it anyway."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("mpp-tempo",),
        accepts=[_SPEC_ENTRY],
        v2_header=False,
    )
    assert "FAIL|" in result.stdout
    assert "advertising a rail" in result.stdout


def test_an_off_rail_still_sending_the_v2_header_fails(tmp_path):
    """A v2 client reads PAYMENT-REQUIRED before it reads the body, so an
    orphaned header sells the rail on its own."""
    result = _run_x402_block(
        tmp_path, _challenge_block(), methods=("mpp-tempo",), accepts=[], v2_header=True
    )
    assert "FAIL|" in result.stdout
    assert "still carries a v2 PAYMENT-REQUIRED" in result.stdout


def test_an_unreadable_manifest_fails_rather_than_guessing(tmp_path):
    """Every x402 check asks a different question depending on the rail state.
    With no answer, the honest result is FAIL -- guessing 'off' would report a
    live unpayable rail as a clean run."""
    result = _run_x402_block(
        tmp_path, _challenge_block(), manifest_json="<html>502</html>"
    )
    assert "FAIL|" in result.stdout
    assert "no way to tell" in result.stdout


def test_an_off_rail_carrying_a_bazaar_record_fails(tmp_path):
    """The discovery record advertises this node to facilitators as a payable
    resource. Emitting it while the rail is off invites exactly the payment the
    shutdown exists to prevent."""
    result = _run_x402_block(
        tmp_path, _discovery_block(), methods=("mpp-tempo",), bazaar=True
    )
    assert "FAIL|" in result.stdout
    assert "still carries a Bazaar discovery record" in result.stdout


def test_an_off_rail_with_no_bazaar_record_is_a_counted_pass(tmp_path):
    """This branch used to print a NOTE and count nothing. A branch that cannot
    go red is exactly how 28/28 was reported over a dead paid path."""
    result = _run_x402_block(
        tmp_path, _discovery_block(), methods=("mpp-tempo",), bazaar=False
    )
    assert "FAIL|" not in result.stdout, result.stdout
    assert "PASS|" in result.stdout
    assert "nothing to index" in result.stdout


def test_a_live_rail_with_no_bazaar_record_fails(tmp_path):
    """Payable but unindexable is a real defect, not a footnote."""
    result = _run_x402_block(tmp_path, _discovery_block(), bazaar=False)
    assert "FAIL|" in result.stdout
    assert "no Bazaar record" in result.stdout


def test_a_real_audit_result_passes(tmp_path):
    result = _run_block(tmp_path, "200", body='{"pass":true,"violations":[]}')
    assert "PASS|" in result.stdout
    assert "FAIL|" not in result.stdout


def test_a_200_without_an_audit_result_is_a_failure(tmp_path):
    """A 200 carrying an error object is still a dead path. Checking only the
    status code would call that healthy."""
    result = _run_block(tmp_path, "200", body='{"error":"nope"}')
    assert "FAIL|" in result.stdout
    assert "no audit result" in result.stdout


def test_500_fails_loudly_and_points_at_the_traceback(tmp_path):
    """The exact production outage. A 500 here means the paid path is dead;
    the only useful next step is the server-side traceback."""
    result = _run_block(tmp_path, "500", body="Internal Server Error")
    assert "FAIL|" in result.stdout
    assert "PAID PATH IS DEAD" in result.stdout
    assert "gcloud logging read" in result.stdout


def test_402_fails_and_names_the_key_store_as_a_possible_cause(tmp_path):
    """Now that a dead key store degrades to 402 rather than 500, a 402 on an
    authenticated call is ambiguous between 'bad key' and 'no key store'. The
    message has to say so or the next debugging step is a guess."""
    result = _run_block(tmp_path, "402")
    assert "FAIL|" in result.stdout
    assert "key store" in result.stdout


def test_502_is_not_a_failure_because_auth_worked(tmp_path):
    """502 means the audit could not run against the target site. Nothing was
    billed and the paid path is alive -- failing the deploy check over someone
    else's website being down is how a checker gets ignored."""
    result = _run_block(tmp_path, "502")
    assert "FAIL|" not in result.stdout
    assert "paid path is alive" in result.stdout


def test_an_unexpected_status_still_fails(tmp_path):
    result = _run_block(tmp_path, "418")
    assert "FAIL|" in result.stdout


def test_skipping_without_a_key_is_loud_not_silent(tmp_path):
    """Silence is what let the outage live. A skipped paid-path check must say
    that the most important thing was not verified."""
    result = _run_block(tmp_path, "200", api_key=None)
    assert "SKIP" in result.stdout
    assert "NOT verified" in result.stdout
    assert "FAIL|" not in result.stdout


def test_the_paid_check_uses_the_cheapest_route():
    """It spends real money on every run; $0.03 rather than the $0.10 bundle."""
    block = _paid_path_block()
    assert "/audit/wcag" in block
    assert "/audit/bundle" not in block


def test_the_paid_check_sends_a_resolved_key_not_a_bare_export():
    """It used to require `export HUBVIBE_API_KEY=...`, so a fresh shell meant
    the paid path was skipped -- which is how the one check that answers "can
    this take money" went unrun while everything else looked green."""
    block = _paid_path_block()
    assert "X-API-Key: $PAID_KEY" in block
    assert "hv_resolve_api_key" in block
    assert "X-API-Key: $HUBVIBE_API_KEY" not in block


@pytest.mark.parametrize("status", ["200", "500", "402", "502"])
def test_every_documented_status_has_a_branch(status):
    """A status that falls through to the catch-all reports a bare number and
    no next step, which is the failure mode this whole file exists to prevent."""
    assert re.search(rf"^\s+{status}\)", _paid_path_block(), re.MULTILINE)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


def test_mpp_off_with_no_challenge_is_green(tmp_path):
    """mpp-stripe sits below Stripe's 0.50 USD SPT floor at these prices and
    mpp-tempo has no recipient -- both deliberate. The checker asserted the
    header unconditionally and went red for the state the operator chose,
    which is how a checker teaches people to ignore it."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("x402", "stripe_api_key"),
        accepts=[_SPEC_ENTRY],
        mpp_header=False,
    )
    assert "FAIL|" not in result.stdout, result.stdout
    assert "MPP is off and no WWW-Authenticate" in result.stdout


def test_mpp_advertised_but_no_challenge_still_fails(tmp_path):
    """MPP's only real channel IS that header. Advertising the method without
    sending it leaves a rail an agent cannot actually use."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("mpp-tempo", "stripe_api_key"),
        accepts=[],
        v2_header=False,
        mpp_header=False,
    )
    assert "FAIL|" in result.stdout
    assert "sends no WWW-Authenticate challenge" in result.stdout


def test_an_orphaned_mpp_challenge_fails(tmp_path):
    """The other half: a header still going out for a method the manifest no
    longer lists invites a caller to pay a rail this node says it cannot
    settle."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("x402", "stripe_api_key"),
        accepts=[_SPEC_ENTRY],
        mpp_header=True,
    )
    assert "FAIL|" in result.stdout
    assert "still sends a WWW-Authenticate" in result.stdout


def test_at_least_one_machine_rail_must_be_live(tmp_path):
    """Rails go dark individually for good reasons. All of them dark at once is
    a tollbooth with no coin slot, and nothing else in the checker says so --
    the old unconditional MPP assertion was standing in for this question and
    answering it wrongly."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("stripe_api_key",),
        accepts=[],
        v2_header=False,
        mpp_header=False,
    )
    assert "FAIL|" in result.stdout
    assert "NO machine payment rail" in result.stdout


def test_x402_alone_satisfies_the_machine_rail_check(tmp_path):
    """x402 on its own is a complete autonomous path: no account, no human."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("x402", "stripe_api_key"),
        accepts=[_SPEC_ENTRY],
        mpp_header=False,
    )
    assert "a machine payment rail is live: x402" in result.stdout
    assert "FAIL|" not in result.stdout


def test_the_checker_proves_the_deployed_node_refuses_internal_targets():
    """A node that fetches 169.254.169.254 or its own loopback on request is a
    free proxy into the deployment. The live checker must ask, for each of
    the three canonical internal targets, and demand a 400."""
    text = SCRIPT.read_text()
    block = text[text.index("Target URL gate"):]
    for internal in ("169.254.169.254", "127.0.0.1", "metadata.google.internal"):
        assert internal in block, f"the checker never probes {internal}"
    assert '"400"' in block
    assert "ALLOW_PRIVATE_TARGETS" in block, "the fix for a failing gate is not named"


def test_the_checker_verifies_the_node_it_was_ASKED_to_verify():
    """It read only $1, while every other script here reads $BASE. So
    `BASE=http://... bash scripts/verify-live.sh` -- the form the runbook and
    habit both produce -- silently checked PRODUCTION instead. Against a
    healthy local node that read as 34 failures (2026-09-06); against a
    broken production it would have reported someone else's node passing.
    A verifier that checks a different thing than it was asked to is worse
    than no verifier."""
    import subprocess

    script = REPO_ROOT / "scripts" / "verify-live.sh"
    text = script.read_text()
    assert 'BASE="${1:-${BASE:-' in text, "BASE is not read from the environment"

    def target(env=None, args=()):
        # The header line names what it is about to check, and it is printed
        # before the first network call -- so read that line and stop, rather
        # than waiting for a full live verification to finish.
        #
        # Waiting was the bug. This asserts one thing about argument parsing,
        # and waiting made it depend on whether a live node answers and on how
        # long 25 probes take. On the CI runner, which reaches nothing, the
        # retry backoff on the FIRST probe alone ran past the timeout, and the
        # test failed for a reason it does not test (2026-09-08). Killing the
        # process at the header makes it deterministic and near-instant
        # everywhere.
        proc = subprocess.Popen(
            ["bash", str(script), *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", **(env or {})},
        )
        seen = []
        try:
            for line in proc.stdout:
                seen.append(line)
                if "live verification:" in line:
                    return line.split("live verification:")[1].strip()
            raise AssertionError(f"no target line in output: {''.join(seen)[:300]}")
        finally:
            proc.kill()
            proc.stdout.close()
            proc.wait(timeout=30)

    assert target(env={"BASE": "http://127.0.0.1:18080"}) == "http://127.0.0.1:18080"
    # A positional argument still wins, so existing habits keep working.
    assert target(env={"BASE": "http://127.0.0.1:18080"}, args=("http://127.0.0.1:19090",)) \
        == "http://127.0.0.1:19090"
    # And with neither, it checks production, as before.
    assert target() == "https://hubvibe-io.com"
