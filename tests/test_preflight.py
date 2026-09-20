"""Guards for the preflight in scripts/repair-and-deploy.sh.

Four separate live-environment problems reached production undetected: a
missing Firestore database (every authenticated call 500'd), a secret holding
a test string where a Stripe key belonged, min-instances=1 burning ~$137/month
against no traffic, and a pay-to address one character short of a valid EVM
address.

They have nothing in common except that nothing verified the deployed
environment. Tests passed, deploys succeeded, and verify-live.sh reported
28/28 while the paid path was dead. The preflight closes that gap by refusing
to deploy into an environment it can see is broken.

The pay-to check gets the most attention here because its first version was
itself an instance of the bug: it grepped `flattened` output for
`name: X402_PAY_TO_ADDRESS`, gcloud pads that format with alignment spaces,
so the pattern matched nothing and the check silently skipped -- producing
output that looked exactly like a pass. Every branch is asserted now,
including "not configured", so silence is never the answer.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "repair-and-deploy.sh"

# Not the test-suite constant (0x32b08c...): the deploy script now strips
# that one as an address nobody holds the key to, so using it as the "good"
# fixture here would assert the opposite of what the script does.
GOOD_ADDR = "0x1111111111111111111111111111111111111111"   # 0x + 40 hex
SHORT_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922b"    # 0x + 39 hex
UNAFFIRMED_ADDR = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"  # deployed, unrecognised

_PAY_TO_GOOD = [{"name": "X402_PAY_TO_ADDRESS", "value": GOOD_ADDR}]
_PAY_TO_SHORT = [{"name": "X402_PAY_TO_ADDRESS", "value": SHORT_ADDR}]
_PAY_TO_SECRET = [
    {"name": "X402_PAY_TO_ADDRESS",
     "valueFrom": {"secretKeyRef": {"name": "s", "key": "latest"}}}
]
_FACILITATOR_ONLY = [{"name": "X402_FACILITATOR_URL", "value": "https://f.example"}]
_NO_X402 = [{"name": "PUBLIC_BASE_URL", "value": "https://x"}]


def _stub(env=None, firestore_ok=True, min_scale="0"):
    body = json.dumps({
        # Real describe output always carries the service URL; the deploy
        # pins PUBLIC_BASE_URL to it.
        "status": {"url": "https://hubvibe-stub-uc.a.run.app"},
        "spec": {"template": {
            "metadata": {"annotations": {"autoscaling.knative.dev/minScale": min_scale}},
            "spec": {"containers": [{"env": env if env is not None else _PAY_TO_GOOD}]},
        }}
    })
    firestore = "echo ok; exit 0" if firestore_ok else "exit 1"
    return f"""#!/usr/bin/env bash
case "$*" in
  *"secrets describe"*) exit 0 ;;
  *"firestore databases describe"*) {firestore} ;;
  *"--format=json"*) cat <<'J'
{body}
J
    ;;
  *minScale*) echo "{min_scale}" ;;
  *"run services describe"*)
    printf 'name:  STRIPE_SECRET_KEY\\n  secretKeyRef.name:  SECRET_STRIPE_KEY\\n' ;;
  *"run services update"*) echo "UPDATE_INVOKED $*" >> "$GCLOUD_CALL_LOG"; echo "UPDATE_INVOKED $*" ;;
  *"run deploy"*) echo "DEPLOY_INVOKED $*" >> "$GCLOUD_CALL_LOG"; echo "DEPLOY_INVOKED" ;;
  *) exit 0 ;;
esac
"""


def _run(tmp_path, extra_env=None, **kwargs):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gcloud").write_text(_stub(**kwargs))
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("KEEP_WARM", None)
    # The script sends the min-instances repair's output to /dev/null, so a
    # marker on stdout cannot prove it ran. The stub logs every invocation to
    # this file, which survives any redirection the script does.
    call_log = tmp_path / "gcloud_calls.log"
    call_log.write_text("")
    env["GCLOUD_CALL_LOG"] = str(call_log)
    if extra_env:
        env.update(extra_env)
    # verify-live.sh makes real network calls with retries; the preflight is
    # what is under test here, not the post-deploy check.
    env["SKIP_VERIFY"] = "1"
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120
    )
    result.gcloud_calls = call_log.read_text()
    return result


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_missing_firestore_database_blocks_the_deploy(tmp_path):
    """The outage that returned 500 to every authenticated caller."""
    result = _run(tmp_path, firestore_ok=False)
    assert "no Firestore" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_a_short_pay_to_address_blocks_the_deploy(tmp_path):
    """39 hex characters looks right at a glance. The service would advertise
    a crypto rail while every settlement fails -- indistinguishable, from
    outside, from nobody buying."""
    result = _run(tmp_path, env=_PAY_TO_SHORT)
    assert "NOT a valid EVM address" in result.stdout
    assert "39" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_a_well_formed_pay_to_address_passes(tmp_path):
    result = _run(tmp_path, env=_PAY_TO_GOOD)
    assert "well-formed EVM address" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_the_zero_address_blocks_the_deploy(tmp_path):
    """0x + 40 zeros is SHAPE-valid -- it passes the hex check that catches
    the short address above -- but address(0) is unownable and USDC reverts
    transfers to it. This exact value shipped once, discovered live on
    2026-08-18: x402 advertised, every possible payment unreceivable, and
    from our side identical to zero demand. The shape gate alone blessed it."""
    zero = [{"name": "X402_PAY_TO_ADDRESS",
             "value": "0x0000000000000000000000000000000000000000"}]
    result = _run(tmp_path, env=zero)
    assert "ZERO ADDRESS" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_a_short_tempo_recipient_is_REPAIRED_not_refused(tmp_path):
    """The script is called repair-and-deploy, and refusing was the wrong half.

    A malformed tempo recipient cannot settle either way -- the app's own guard
    refuses to advertise it -- so stopping the deploy over it removed nothing
    and cost a round trip: "run this other gcloud command, then start again".
    That friction sat on the one path that puts a service back into service.
    It is stripped as part of the repair instead, and the deploy proceeds.

    The value itself is real: `mppx validate` found a 39-hex recipient live on
    all six paid routes, a truncated paste of the test-suite constant.
    """
    env = _PAY_TO_GOOD + [
        {"name": "MPP_TEMPO_RECIPIENT_ADDRESS", "value": SHORT_ADDR}
    ]
    result = _run(tmp_path, env=env)
    assert "not a valid EVM address" in result.stdout
    assert "39" in result.stdout
    # Repaired: the removal is in the update, and the deploy is not blocked.
    assert "--remove-env-vars=MPP_TEMPO_RECIPIENT_ADDRESS" in result.stdout


def test_a_zero_tempo_recipient_is_also_repaired(tmp_path):
    """Shape-valid and unownable. Same treatment: nothing can settle to it."""
    env = _PAY_TO_GOOD + [
        {
            "name": "MPP_TEMPO_RECIPIENT_ADDRESS",
            "value": "0x0000000000000000000000000000000000000000",
        }
    ]
    result = _run(tmp_path, env=env)
    assert "zero address" in result.stdout
    assert "--remove-env-vars=MPP_TEMPO_RECIPIENT_ADDRESS" in result.stdout


def test_a_malformed_pay_to_address_still_STOPS_the_deploy(tmp_path):
    """The asymmetry is deliberate. X402_PAY_TO_ADDRESS is where money lands,
    so a human picks its replacement rather than having it quietly deleted;
    the tempo recipient has no valid use in that state and no such choice to
    make."""
    result = _run(tmp_path, env=_PAY_TO_SHORT)
    assert "DEPLOY_INVOKED" not in result.stdout
    assert "--remove-env-vars=X402_PAY_TO_ADDRESS" not in result.stdout


def test_a_secret_backed_tempo_recipient_is_never_silently_deleted(tmp_path):
    """Its value cannot be read here, so it cannot be judged malformed.
    Removing it on a guess would delete a working rail's recipient."""
    env = _PAY_TO_GOOD + [
        {"name": "MPP_TEMPO_RECIPIENT_ADDRESS",
         "valueFrom": {"secretKeyRef": {"name": "s", "key": "latest"}}}
    ]
    result = _run(tmp_path, env=env)
    assert "--remove-env-vars=MPP_TEMPO_RECIPIENT_ADDRESS" not in result.stdout
    assert "comes from Secret Manager" in result.stdout


def test_a_well_formed_tempo_recipient_passes(tmp_path):
    env = _PAY_TO_GOOD + [
        {"name": "MPP_TEMPO_RECIPIENT_ADDRESS", "value": GOOD_ADDR}
    ]
    result = _run(tmp_path, env=env)
    assert "MPP_TEMPO_RECIPIENT_ADDRESS is a well-formed EVM address" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_an_absent_tempo_recipient_says_so_rather_than_skipping(tmp_path):
    """A branch that prints nothing converts "unverified" into "verified" in
    the reader's head -- this file's oldest lesson."""
    result = _run(tmp_path, env=_PAY_TO_GOOD)
    assert "tempo rail is not configured" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_secret_backed_address_is_reported_as_unchecked(tmp_path):
    """Its value cannot be read here. Claiming a pass would be worse than
    saying nothing, so it says exactly what it did not check."""
    result = _run(tmp_path, env=_PAY_TO_SECRET)
    assert "comes from Secret Manager" in result.stdout
    assert "well-formed EVM address" not in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_facilitator_without_a_destination_blocks_the_deploy(tmp_path):
    """A rail advertised with nowhere to send the money."""
    result = _run(tmp_path, env=_FACILITATOR_ONLY)
    assert "is not set" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_x402_being_absent_is_stated_not_silent(tmp_path):
    """The original bug produced no line at all, which read as a pass. Every
    path must say something -- including 'there was nothing to check'."""
    result = _run(tmp_path, env=_NO_X402)
    assert "x402 is not configured" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_nothing_parses_config_out_of_flattened_output():
    """gcloud's `flattened` format pads names with alignment spaces, so any
    `grep 'name: X'` pattern silently matches nothing. That bug shipped twice
    here -- once in the pay-to check (which then reported nothing at all) and
    once in the Stripe secret lookup (which minted a revision on every run).
    Both now read JSON; nothing should go back to grepping the padded form."""
    text = SCRIPT.read_text()
    assert "flattened(" not in text
    assert "grep -A1 'name:" not in text
    assert "grep -A5 'name:" not in text
    assert "--format=json" in text


def test_a_correctly_configured_service_is_not_repaired_again(tmp_path):
    """The churn bug: CURRENT_SECRET came back empty every run, so the script
    concluded the repair was always needed and created a revision each time.
    The service reached revision 62 that way, while the docstring promised
    re-running "does not create pointless revisions"."""
    env = [
        {"name": "STRIPE_SECRET_KEY",
         "valueFrom": {"secretKeyRef": {"name": "SECRET_STRIPE_KEY", "key": "latest"}}},
    ]
    result = _run(tmp_path, env=env)
    assert "already points at SECRET_STRIPE_KEY" in result.stdout
    assert "nothing to repair" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout


def test_a_plain_value_is_still_repointed_at_the_secret(tmp_path):
    """The fix must not make the script stop repairing what genuinely needs it."""
    env = [{"name": "STRIPE_SECRET_KEY", "value": "sk_live_plainvalue"}]
    result = _run(tmp_path, env=env)
    assert "will repoint" in result.stdout
    assert "UPDATE_INVOKED" in result.stdout


def test_min_instances_is_set_to_zero_not_merely_reported(tmp_path):
    """This used to only warn and print the gcloud command for a human. It
    warned on every deploy for weeks while the meter ran and the bill reached
    $300. A warning nobody reads is not a control -- the money leaves either
    way. Keeping an instance warm has no valid use at zero paid traffic, so
    it is repaired like any other setting with no valid use."""
    result = _run(tmp_path, min_scale="1")
    assert "min-instances=1" in result.stdout
    assert "--min-instances=0" in result.gcloud_calls, result.gcloud_calls
    # Still not fatal: idle billing is money, not brokenness.
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_zero_min_instances_service_is_left_alone(tmp_path):
    """The repair must fire on the fault, not on every deploy -- an
    unconditional update mints a pointless revision every run, which this
    repo has already paid for once."""
    result = _run(tmp_path, min_scale="0")
    assert "--min-instances" not in result.gcloud_calls, result.gcloud_calls


def test_keep_warm_opts_back_in(tmp_path):
    """Once paid volume makes cold starts worth avoiding, the owner must be
    able to keep it warm without this undoing the choice on every deploy."""
    result = _run(tmp_path, min_scale="1", extra_env={"KEEP_WARM": "1"})
    assert "--min-instances" not in result.gcloud_calls, result.gcloud_calls
    assert "leaving it warm" in result.stdout


def test_a_healthy_environment_deploys(tmp_path):
    result = _run(tmp_path)
    assert "preflight passed" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_preflight_runs_before_the_deploy(tmp_path):
    """Checking after deploying would leave a broken revision serving."""
    result = _run(tmp_path)
    assert result.stdout.index("Preflight") < result.stdout.index("DEPLOY_INVOKED")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


def test_an_unaffirmed_pay_to_address_is_stripped_not_deployed(tmp_path):
    """0x2b3b... sat deployed on this service as X402_PAY_TO_ADDRESS and the
    owner does not recognise it. It is 0x + 40 hex, so every shape gate in
    this repo said yes -- shape is not ownership.

    It is REMOVED rather than blocking the deploy: refusing would leave the
    running revision advertising it, so stopping is the option that points
    money at a stranger for longer.
    """
    result = _run(tmp_path, env=[{"name": "X402_PAY_TO_ADDRESS",
                                  "value": UNAFFIRMED_ADDR}])
    assert "NOBODY HERE HOLDS THE KEY TO" in result.stdout
    updates = [ln for ln in result.stdout.splitlines() if "UPDATE_INVOKED" in ln]
    assert updates, result.stdout
    assert "--remove-env-vars" in updates[0]
    assert "X402_PAY_TO_ADDRESS" in updates[0]
    assert "X402_FACILITATOR_URL" in updates[0]
    assert "DEPLOY_INVOKED" in result.stdout


def test_an_unaffirmed_tempo_recipient_is_stripped_too(tmp_path):
    result = _run(tmp_path, env=[{"name": "MPP_TEMPO_RECIPIENT_ADDRESS",
                                  "value": UNAFFIRMED_ADDR}])
    assert "nobody here holds the key" in result.stdout
    updates = [ln for ln in result.stdout.splitlines() if "UPDATE_INVOKED" in ln]
    assert "MPP_TEMPO_RECIPIENT_ADDRESS" in updates[0]


def test_a_recipient_the_owner_controls_is_left_alone(tmp_path):
    """The guard must refuse two named addresses, not become a third gate that
    strips the wallet this rail exists to pay into."""
    result = _run(tmp_path, env=[{"name": "X402_PAY_TO_ADDRESS", "value": GOOD_ADDR}])
    assert "NOBODY HERE HOLDS THE KEY TO" not in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


# --- every gcloud invocation names the project ------------------------------
#
# A fresh Cloud Shell has no default project. gcloud does not treat that as an
# error: `secrets describe` fails, `secrets list` prints nothing, `run services
# describe` finds nothing. repair-and-deploy.sh then reported "no secret named
# SECRET_STRIPE_KEY" over a secret that exists, and refused to deploy -- on the
# second Cloud Shell session of the night, after the first (which had the
# project set) disconnected. The one deploy command could not run in a fresh
# shell, and its error pointed at the wrong thing.
#
# "Invocation" here means gcloud in command position: at the start of a line,
# after `if`/`!`, inside `$( )`, or after a pipe. A `gcloud` inside a string
# printed for a human (a `die`/`warn` message) is not one. `gcloud auth` and
# `gcloud config` are excluded: they are project-less by nature, and `config
# set project` is precisely the human instruction the message carries.

import re as _re

_SCRIPTS_THAT_TOUCH_THE_PROJECT = [
    "repair-and-deploy.sh",
    "go-live.sh",
    "lib-api-key.sh",
    "repair-secrets.sh",
    "measure-call-cost.sh",
]

_INVOCATION = _re.compile(r'^\s*(?:if\s+|!\s+|\w+=\$\(\s*|\|\s*)?gcloud\s+(\S+)')


def _gcloud_invocations(text):
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        m = _INVOCATION.match(line)
        if not m:
            continue
        # `gcloud billing ...` is scoped to a billing ACCOUNT, not a project;
        # its project, where one is needed, is a positional argument.
        if m.group(1) in ("auth", "config", "billing"):
            continue
        yield line.strip()


@pytest.mark.parametrize("name", _SCRIPTS_THAT_TOUCH_THE_PROJECT)
def test_every_gcloud_invocation_names_the_project(name):
    text = (REPO_ROOT / "scripts" / name).read_text()
    invocations = list(_gcloud_invocations(text))
    assert invocations, f"{name}: the invocation regex matched nothing -- check the test, not the script"
    bare = [ln for ln in invocations
            if "--project" not in ln and "project_args" not in ln]
    assert not bare, f"{name}: gcloud invoked without --project:\n  " + "\n  ".join(bare)


def test_the_deploy_script_defaults_the_project_rather_than_inheriting_it():
    text = SCRIPT.read_text()
    assert 'PROJECT="${PROJECT:-resolver-time}"' in text


def test_an_empty_secret_list_is_reported_as_no_project_not_a_missing_secret(tmp_path):
    """Zero secrets in this project is gcloud not seeing the project. Saying
    "no secret named SECRET_STRIPE_KEY" there sent the owner looking for a
    secret that was there the whole time."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"secrets describe"*) exit 1 ;;\n'
        '  *"secrets list"*) exit 0 ;;\n'   # prints nothing: no project
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKIP_VERIFY"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            env=env, timeout=60)
    assert "not seeing the project" in result.stdout
    assert "gcloud config set project resolver-time" in result.stdout
    assert "no secret named" not in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_an_empty_secret_list_prints_what_gcloud_actually_said(tmp_path):
    """On 2026-09-04 the project WAS set and --project WAS passed, and the
    script still printed 'gcloud config set project' -- a guess, because it
    had thrown gcloud's stderr away. The message gcloud printed is the fault;
    it must reach the owner, with the fix that matches it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"secrets describe"*) exit 1 ;;\n'
        '  *"secrets list"*) echo "ERROR: (gcloud.secrets.list) PERMISSION_DENIED: '
        'Permission secretmanager.secrets.list denied for resource projects/resolver-time" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKIP_VERIFY"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            env=env, timeout=60)
    assert "gcloud said: ERROR: (gcloud.secrets.list) PERMISSION_DENIED" in result.stdout
    assert "roles/secretmanager.viewer" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_billing_disabled_is_named_as_billing_not_as_a_missing_role(tmp_path):
    """The 2026-09-04 root cause. Google phrases it as a permission error
    ("does not have permission to access projects instance ... This API
    method requires billing to be enabled ... reason: BILLING_DISABLED"),
    so a permission-first hint sends the owner to IAM. Billing off shuts
    Secret Manager, Cloud Run and the node at once; the hint must say so and
    hand over the billing console link."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"secrets describe"*) exit 1 ;;\n'
        '  *"secrets list"*) cat >&2 <<MSG\n'
        "ERROR: (gcloud.secrets.list) [owner@example.com] does not have permission to access "
        "projects instance [resolver-time] (or it may not exist): This API method requires "
        "billing to be enabled. Please enable billing on project #resolver-time by visiting "
        "https://console.developers.google.com/billing/enable?project=resolver-time then retry.\n"
        "  reason: BILLING_DISABLED\n"
        "MSG\n"
        "exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKIP_VERIFY"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            env=env, timeout=60)
    assert "BILLING IS DISABLED on resolver-time" in result.stdout
    assert "https://console.developers.google.com/billing/enable?project=resolver-time" in result.stdout
    assert "roles/secretmanager.viewer" not in result.stdout, "sent to IAM for a billing fault"
    assert "DEPLOY_INVOKED" not in result.stdout


def test_the_deploy_pins_capacity_instead_of_inheriting_it():
    """Memory, CPU, concurrency, instance cap, request timeout and startup CPU
    boost are bill-or-outage decisions. A deploy that inherits whatever the
    last revision had leaves them to chance; 512 MiB (Cloud Run's default)
    OOMs under concurrent Chromium contexts, and no instance cap is an
    unbounded bill under a flood of free 402s."""
    text = SCRIPT.read_text().replace("\\\n", " ")
    deploy = [ln for ln in text.splitlines() if ln.lstrip().startswith("gcloud run deploy")]
    assert len(deploy) == 1
    line = deploy[0]
    for flag in ("--memory=", "--cpu=", "--concurrency=", "--max-instances=", "--timeout=", "--cpu-boost"):
        assert flag in line, f"deploy line lacks {flag}"
    assert 'MAX_INSTANCES:-3' in line, "the default instance cap is not 3 -- the ceiling on a bad day's bill"
    assert 'MEMORY:-2Gi' in line


def test_the_deploy_installs_a_spend_alert_and_never_blocks_on_it(tmp_path):
    """The bill reached $300 with zero revenue because nothing was watching.
    The deploy now creates Google's own budget alert on the project's billing
    account. It must be idempotent (one alert, not one per deploy), must name
    the manual command when it cannot, and must never stop the deploy."""
    text = SCRIPT.read_text()
    block = text[text.index("# 5. Spend guard"):text.index('if [ "$PREFLIGHT_FAILED" -ne 0 ]')]
    assert "gcloud billing budgets create" in block
    assert "gcloud billing budgets list" in block, "not idempotent: no check for an existing alert"
    assert "--filter-projects" in block, "the alert is not scoped to this project"
    assert "PREFLIGHT_FAILED=1" not in block, "a failed alert must not block the deploy"
    assert "billing/enable?project=" in block, "an unlinked billing account is not explained"

    # Driven: gcloud stub where the project has no billing account. The
    # deploy must still be reached.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"secrets describe"*) exit 0 ;;\n'
        '  *"services describe"*) echo \'{"status":{"url":"https://hubvibe-stub-uc.a.run.app"},"spec":{"template":{"spec":{"containers":[{"env":[]}]}}}}\' ;;\n'
        '  *"billing projects describe"*) exit 1 ;;\n'
        '  *"run deploy"*) echo DEPLOY_INVOKED ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKIP_VERIFY"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    assert "no billing account is linked" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout, result.stdout


# --- 2026-09-06: a Cloud Run deploy must not advertise the parked domain ---


def test_a_cloud_run_deploy_advertises_the_service_url_not_the_parked_domain(tmp_path):
    """main.py's PUBLIC_BASE_URL default became https://hubvibe-io.com when
    the identity moved to the domain -- correct on the VPS behind Caddy, and
    wrong on Cloud Run until that domain points here: every 402 would name
    resource URLs on a registrar parking page and the Bazaar would catalogue
    them. The deploy pins the identity to the service's own URL."""
    result = _run(tmp_path)
    deploys = [line for line in result.gcloud_calls.splitlines() if line.startswith("DEPLOY_INVOKED")]
    assert deploys, result.stdout + result.stderr
    assert "--update-env-vars=PUBLIC_BASE_URL=https://hubvibe-stub-uc.a.run.app" in deploys[0]
    assert "hubvibe-io.com" not in deploys[0]


def test_an_operator_can_name_the_identity_once_the_domain_points_at_cloud_run(tmp_path):
    result = _run(tmp_path, extra_env={"PUBLIC_BASE_URL": "https://hubvibe-io.com"})
    deploys = [line for line in result.gcloud_calls.splitlines() if line.startswith("DEPLOY_INVOKED")]
    assert deploys, result.stdout + result.stderr
    assert "--update-env-vars=PUBLIC_BASE_URL=https://hubvibe-io.com" in deploys[0]
