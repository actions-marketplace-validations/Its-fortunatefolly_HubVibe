"""Guards for scripts/go-live.sh -- the one command that turns both rails on.

This script points a live service's payment rails at real recipients and then
deploys, so what matters is what it refuses and what it never leaves behind:

  * an address nobody holds the key to must never be advertised, even though
    it is 0x + 40 hex and passes every format gate in this repo;
  * one rail failing must not take the other down with it;
  * a rail it turns OFF must not stay deployed pointing at the recipient it
    just refused;
  * both rails must land in ONE revision, and the run must end in a SOURCE
    deploy -- `services update` alone mints a revision carrying the same
    container image, which is how a merged fix can sit behind an old image.

The unaffirmed-address case is not hypothetical. 0x2b3b...0256 sat deployed on
this service as X402_PAY_TO_ADDRESS and the owner does not recognise it.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go-live.sh"

OWNER_WALLET = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
UNRECOGNISED = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"
TEST_CONSTANT = "0x32b08c5e927c69877d0fcab35618c265674922bc"
GOOD_TEMPO = "0x1111111111111111111111111111111111111111"
SHORT = "0x32b08c5e927c69877d0fcab35618c265674922b"
ZERO = "0x" + "0" * 40
FACILITATOR = "https://facilitator.payai.network"


def _service_json(env):
    return json.dumps(
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"env": [{"name": k, "value": v} for k, v in env.items()]}
                        ]
                    }
                }
            }
        }
    )


def _run(tmp_path, live_env=None, curl_body=None, secret="sk_live_fake", env=None):
    """Drive the script with gcloud, curl and the deploy handoff stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    svc_json = tmp_path / "service.json"
    svc_json.write_text(_service_json(live_env or {}))

    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"run services describe"*) cat "{svc_json}" ;;\n'
        f'  *"secrets versions access"*) printf "%s" "{secret}" ;;\n'
        '  *"run services update"*) echo "UPDATE_INVOKED $*" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)

    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\nprintf '%s' \"$STUB_CURL_BODY\"\n"
    )
    (bin_dir / "curl").chmod(0o755)

    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_repo / "scripts" / "go-live.sh").write_text(SCRIPT.read_text())
    (fake_repo / "scripts" / "repair-and-deploy.sh").write_text(
        "#!/usr/bin/env bash\necho DEPLOY_HANDOFF\n"
    )

    full_env = dict(os.environ)
    full_env["PATH"] = f"{bin_dir}:{full_env['PATH']}"
    full_env["STUB_CURL_BODY"] = curl_body or ""
    for leaked in ("X402_PAY_TO_ADDRESS", "MPP_TEMPO_RECIPIENT_ADDRESS",
                   "STRIPE_SECRET_KEY", "RAILS"):
        full_env.pop(leaked, None)
    if env:
        full_env.update(env)

    return subprocess.run(
        ["bash", str(fake_repo / "scripts" / "go-live.sh")],
        capture_output=True, text=True, env=full_env, timeout=60,
    )


def _update_calls(result):
    return [ln for ln in result.stdout.splitlines() if "UPDATE_INVOKED" in ln]


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_both_rails_land_in_one_revision(tmp_path):
    """Two `services update` calls means two revisions and a window where the
    service is half-configured. The whole point of one script is one write."""
    result = _run(tmp_path, curl_body='{"address": "%s"}' % GOOD_TEMPO)
    calls = _update_calls(result)
    assert len(calls) == 1, result.stdout
    assert f"X402_PAY_TO_ADDRESS={OWNER_WALLET}" in calls[0]
    assert f"MPP_TEMPO_RECIPIENT_ADDRESS={GOOD_TEMPO}" in calls[0]
    assert "DEPLOY_HANDOFF" in result.stdout


def test_the_facilitator_is_set_with_the_address_never_alone(tmp_path):
    """is_configured() needs both. Setting one leaves the rail inert while the
    console shows a variable that looks like progress."""
    result = _run(tmp_path, curl_body='{"address": "%s"}' % GOOD_TEMPO)
    call = _update_calls(result)[0]
    assert f"X402_FACILITATOR_URL={FACILITATOR}" in call


def test_an_unrecognised_deployed_address_is_replaced_not_reused(tmp_path):
    """0x2b3b... is 0x + 40 hex and passes every shape gate in this repo. It
    sat deployed here and the owner does not hold its key. Shape is not
    ownership, and 'already set and well-formed' is the wrong question."""
    result = _run(
        tmp_path,
        live_env={"X402_PAY_TO_ADDRESS": UNRECOGNISED,
                  "X402_FACILITATOR_URL": FACILITATOR},
        curl_body='{"address": "%s"}' % GOOD_TEMPO,
    )
    call = _update_calls(result)[0]
    assert f"X402_PAY_TO_ADDRESS={OWNER_WALLET}" in call
    assert UNRECOGNISED not in call


def test_an_unaffirmed_address_supplied_by_hand_stops_everything(tmp_path):
    """Supplying it deliberately is not affirmation -- it is the paste that
    put it there in the first place."""
    result = _run(
        tmp_path,
        curl_body='{"address": "%s"}' % GOOD_TEMPO,
        env={"X402_PAY_TO_ADDRESS": UNRECOGNISED},
    )
    assert "nobody here holds the key" in result.stdout
    assert not _update_calls(result)
    assert "DEPLOY_HANDOFF" not in result.stdout


def test_the_test_suite_constant_is_refused_as_a_tempo_recipient(tmp_path):
    """0x32b0... exists to make the rail inspectable locally. A truncated
    paste of it was deployed once and only mppx ever noticed."""
    result = _run(
        tmp_path,
        curl_body='{"address": "%s"}' % TEST_CONSTANT,
        env={"MPP_TEMPO_RECIPIENT_ADDRESS": TEST_CONSTANT},
    )
    assert "nobody here holds the key" in result.stdout
    assert not _update_calls(result)


def test_a_short_tempo_address_is_never_set(tmp_path):
    result = _run(tmp_path, curl_body='{"address": "%s"}' % SHORT)
    assert "39" in result.stdout
    assert SHORT not in "".join(_update_calls(result))


def test_the_zero_address_is_never_set(tmp_path):
    result = _run(tmp_path, curl_body='{"address": "%s"}' % ZERO)
    assert "ZERO ADDRESS" in result.stdout
    assert ZERO not in "".join(_update_calls(result))


def test_a_failed_mint_leaves_tempo_off_and_x402_live(tmp_path):
    """One rail failing must not take the other down. Revenue on one rail is
    the entire objective; an all-or-nothing script trades it for tidiness."""
    result = _run(
        tmp_path,
        curl_body='{"error": {"message": "Unrecognized request URL"}}',
    )
    assert "Stripe refused" in result.stdout
    assert "Unrecognized request URL" in result.stdout
    call = _update_calls(result)[0]
    assert f"X402_PAY_TO_ADDRESS={OWNER_WALLET}" in call
    assert "MPP_TEMPO_RECIPIENT_ADDRESS=" not in call
    assert "DEPLOY_HANDOFF" in result.stdout


def test_a_refused_recipient_is_removed_from_the_service(tmp_path):
    """Failing closed in this script is worthless if the revision it deploys
    still carries the recipient it refused -- the rail stays advertised."""
    result = _run(
        tmp_path,
        live_env={"MPP_TEMPO_RECIPIENT_ADDRESS": SHORT},
        curl_body='{"error": {"message": "no"}}',
        env={"RAILS": "tempo"},
    )
    call = _update_calls(result)[0]
    assert "--remove-env-vars" in call
    assert "MPP_TEMPO_RECIPIENT_ADDRESS" in call.split("--remove-env-vars")[1]


def test_a_usable_deployed_tempo_address_is_reused_not_reminted(tmp_path):
    """Minting on every run accumulates deposit addresses on the account."""
    result = _run(
        tmp_path,
        live_env={"MPP_TEMPO_RECIPIENT_ADDRESS": GOOD_TEMPO},
        curl_body='{"error": {"message": "should not be called"}}',
    )
    assert "should not be called" not in result.stdout
    assert "from the live service" in result.stdout


def test_a_nested_mint_response_shape_is_also_read(tmp_path):
    """A preview API's response shape is not a promise."""
    result = _run(tmp_path, curl_body='{"tempo": {"address": "%s"}}' % GOOD_TEMPO)
    assert f"MPP_TEMPO_RECIPIENT_ADDRESS={GOOD_TEMPO}" in _update_calls(result)[0]


def test_rails_x402_does_not_touch_tempo(tmp_path):
    result = _run(
        tmp_path,
        live_env={"MPP_TEMPO_RECIPIENT_ADDRESS": GOOD_TEMPO},
        curl_body='{"error": {"message": "should not be called"}}',
        env={"RAILS": "x402"},
    )
    call = _update_calls(result)[0]
    assert "MPP_TEMPO_RECIPIENT_ADDRESS" not in call
    assert "not touched" in result.stdout


def test_an_already_correct_service_mints_no_revision(tmp_path):
    """Safe to re-run. A healthy deployment must not churn revisions."""
    result = _run(
        tmp_path,
        live_env={
            "X402_PAY_TO_ADDRESS": OWNER_WALLET,
            "X402_FACILITATOR_URL": FACILITATOR,
            "MPP_TEMPO_RECIPIENT_ADDRESS": GOOD_TEMPO,
        },
        curl_body='{"error": {"message": "should not be called"}}',
    )
    assert not _update_calls(result)
    assert "no revision needed" in result.stdout
    # It still deploys: the code may be behind even when the config is right.
    assert "DEPLOY_HANDOFF" in result.stdout


def test_it_ends_in_a_source_deploy_not_an_env_var_write():
    """`services update --update-env-vars` mints a revision carrying the SAME
    container image. A merged fix behind an old image has cost days here."""
    text = SCRIPT.read_text()
    assert "repair-and-deploy.sh" in text
    assert "flattened(" not in text


def test_the_stripe_key_is_read_not_typed():
    text = SCRIPT.read_text()
    assert "secrets versions access" in text
    assert "sk_live" not in text


# --- guards that lived in the per-rail scripts' tests, now that those scripts
# --- are stubs pointing here. Each pins something go-live.sh must keep doing.


def test_the_stripe_api_version_is_pinned_and_overridable():
    """The deposit-address endpoint is preview-only. An older version 404s it,
    which reads as 'crypto is not enabled on this account' rather than 'wrong
    version' -- a wrong diagnosis that has cost this project days before."""
    text = SCRIPT.read_text()
    assert "STRIPE_API_VERSION" in text
    assert "2026-07-29.preview" in text
    assert 'Stripe-Version: $STRIPE_API_VERSION' in text


def test_nothing_is_ever_minted_for_x402():
    """Stripe does MPP, not x402 (owner's fact). The one deposit-address mint
    in this script is the tempo one. A second, for x402, would be a request
    for a product Stripe does not sell -- and its failure message would send
    someone to Stripe support about it."""
    text = SCRIPT.read_text()
    assert "x402-setup" not in text
    mints = [ln for ln in text.splitlines() if "crypto/deposit_addresses" in ln]
    assert len(mints) == 1, mints
    networks = [ln.strip() for ln in text.splitlines() if '-d "network=' in ln]
    assert networks == ['-d "network=$TEMPO_NETWORK" 2>&1)"'], networks
