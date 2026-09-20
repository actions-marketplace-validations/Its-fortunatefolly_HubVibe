"""Switching facilitators must never leave the node unable to take money.

The node refuses to advertise a rail its facilitator cannot verify, so a
facilitator that is down or does not list `exact` on Base turns the live
node into one that answers 200 on /health and sells nothing -- silently.
These tests drive the real script with docker and curl stubbed, against a
fake node, through the three outcomes that matter: the switch holds, the
rail vanishes and is rolled back, and the rail changes underneath us.
"""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "switch-facilitator.sh"
OWNER = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"

OLD = "https://facilitator.xpay.sh"
NEW = "https://x402.dexter.cash"


def _challenge(pay_to=OWNER, amount="30000"):
    return json.dumps({
        "error": "payment_required",
        "price": "$0.03",
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact", "network": "base", "maxAmountRequired": amount,
            "payTo": pay_to, "maxTimeoutSeconds": 300,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        }],
    })


def _stub_env(tmp_path, *, after_switch, index_status="200"):
    """A fake box: deploy/vps/.env, a docker that records, and a curl whose
    402 answer depends on which facilitator .env currently names."""
    compose_dir = tmp_path / "deploy" / "vps"
    compose_dir.mkdir(parents=True)
    (compose_dir / ".env").write_text(f"DOMAIN=hubvibe-io.com\nX402_FACILITATOR_URL={OLD}\n")
    (compose_dir / "docker-compose.yml").write_text("services: {}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$STUB_LOG\"\nexit 0\n")

    # curl: /health always 200; the 402 body is chosen by reading .env, so the
    # stub reacts to the switch exactly as a real node would.
    (bin_dir / "curl").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        for a in "$@"; do case "$a" in
          */health) exit 0 ;;
          */discovery/resources) printf '%s' '{{"items":[]}}' > /dev/null; printf '{index_status}'; exit 0 ;;
        esac; done
        FAC=$(grep '^X402_FACILITATOR_URL=' "$STUB_ENV" | cut -d= -f2-)
        if [ "$FAC" = "{OLD}" ]; then printf '%s' '{_challenge()}'
        else printf '%s' '{after_switch}'
        fi
        """))
    for name in ("docker", "curl"):
        (bin_dir / name).chmod(0o755)

    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_ENV": str(compose_dir / ".env"),
        "COMPOSE_DIR": str(compose_dir),
        "BASE": "https://hubvibe-io.com",
    }
    return compose_dir / ".env", env


def _run(env, *args, timeout=300):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=timeout, env=env
    )


def test_bash_parses_the_script():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_url_prints_usage_and_changes_nothing(tmp_path):
    env_file, env = _stub_env(tmp_path, after_switch=_challenge())
    result = _run(env)
    assert result.returncode == 1
    assert "Usage:" in result.stdout
    assert OLD in env_file.read_text()


@pytest.mark.parametrize("bad", ["x402.dexter.cash", "http://x402.dexter.cash"])
def test_a_non_https_facilitator_is_refused(tmp_path, bad):
    env_file, env = _stub_env(tmp_path, after_switch=_challenge())
    result = _run(env, bad)
    assert result.returncode == 1
    assert "https://" in result.stdout
    assert OLD in env_file.read_text()


@pytest.mark.parametrize(
    "shell_env",
    [{"CLOUD_SHELL": "true"}, {"DEVSHELL_PROJECT_ID": "resolver-time"}],
)
def test_google_cloud_shell_is_refused_by_name(tmp_path, shell_env):
    """Run there on 2026-09-07, the only complaint was a missing .env: a
    symptom that reads as "the stack is broken" and sends the reader after a
    file instead of into the right terminal. The cause has to be named, and
    named BEFORE the .env check -- which also means a Cloud Shell that
    happens to hold a checkout with an .env is still refused."""
    env_file, env = _stub_env(tmp_path, after_switch=_challenge())
    result = _run(env | shell_env, NEW)

    assert result.returncode == 1
    assert "Cloud Shell" in result.stdout
    assert "ssh root@" in result.stdout, "the message must say where to run it instead"
    assert OLD in env_file.read_text(), "the env file was touched in the wrong terminal"
    assert not Path(env["STUB_LOG"]).exists(), "docker was invoked in Cloud Shell"


def test_a_facilitator_that_keeps_the_rail_live_is_kept(tmp_path):
    env_file, env = _stub_env(tmp_path, after_switch=_challenge())
    result = _run(env, NEW)
    assert result.returncode == 0, result.stdout
    assert "x402 still live" in result.stdout
    assert f"X402_FACILITATOR_URL={NEW}" in env_file.read_text()
    assert "discovery/resources" in result.stdout


def test_a_facilitator_that_kills_the_rail_is_rolled_back(tmp_path):
    """THE test. A node that stops advertising x402 answers /health 200 and
    sells nothing; only reading the 402 catches it, and only a rollback
    fixes it."""
    dead = json.dumps({"error": "payment_required", "accepts": [], "price": "$0.03"})
    env_file, env = _stub_env(tmp_path, after_switch=dead)
    result = _run(env, NEW)
    assert result.returncode == 1
    assert "rolling back" in result.stdout
    assert "restored" in result.stdout
    assert f"X402_FACILITATOR_URL={OLD}" in env_file.read_text(), "the node was left on a dead facilitator"


def test_a_rail_that_changes_across_the_switch_is_rolled_back(tmp_path):
    """A different recipient or price after the switch means the new
    facilitator prices or routes differently. Rolling back beats guessing."""
    moved = _challenge(pay_to="0x37555E884c5EbA10f6E816DbecEA30965B9b38C0")
    env_file, env = _stub_env(tmp_path, after_switch=moved)
    result = _run(env, NEW)
    assert result.returncode == 1
    assert "CHANGED across the switch" in result.stdout
    assert f"X402_FACILITATOR_URL={OLD}" in env_file.read_text()


def test_switching_to_the_same_facilitator_touches_nothing(tmp_path):
    env_file, env = _stub_env(tmp_path, after_switch=_challenge())
    result = _run(env, OLD)
    assert result.returncode == 0
    assert "nothing to change" in result.stdout
    assert not (tmp_path / "docker.log").exists(), "restarted the stack for a no-op"


def test_a_facilitator_with_no_index_is_reported_not_hidden(tmp_path):
    """Settling and indexing are different capabilities. A facilitator that
    only settles is usable, but a paid call through it registers nothing --
    which is the whole reason for switching, so it must be said."""
    env_file, env = _stub_env(tmp_path, after_switch=_challenge(), index_status="404")
    result = _run(env, NEW)
    assert result.returncode == 0
    assert "may run no index" in result.stdout
    assert f"X402_FACILITATOR_URL={NEW}" in env_file.read_text()
