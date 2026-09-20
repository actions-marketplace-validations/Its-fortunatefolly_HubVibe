"""scripts/launch.sh: one command from a fresh Cloud Shell to a first paid call.

It exists so the owner never again has to type three commands in the right
order from a phone. What it must get right: check billing FIRST (nothing
serves without it, and the deploy script's own error for that case is two
steps in), print the enable link when billing is off, and never deploy in
that state.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch.sh"


def _run(tmp_path, billing_output: str, exit_code: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"billing projects describe"*) printf "%s" "{billing_output}"; exit {exit_code} ;;\n'
        "  *) echo GCLOUD_CALLED_$1_$2; exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    # A git stub so the checkout step cannot reach the network in a test.
    (bin_dir / "git").write_text("#!/usr/bin/env bash\necho GIT_CALLED_$1; exit 0\n")
    (bin_dir / "git").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    (tmp_path / "checkout" / ".git").mkdir(parents=True)  # an existing clone: the reset path
    # The checkout's own cost sweep, stubbed clean so the flow reaches the deploy.
    (tmp_path / "checkout" / "scripts").mkdir()
    (tmp_path / "checkout" / "scripts" / "cost-sweep.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    env["HUBVIBE_DIR"] = str(tmp_path / "checkout")
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)


def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_billing_off_prints_the_enable_link_and_deploys_nothing(tmp_path):
    result = _run(tmp_path, "False")
    assert result.returncode == 1
    assert "billing is OFF" in result.stdout
    assert "https://console.developers.google.com/billing/enable?project=resolver-time" in result.stdout
    assert "GIT_CALLED" not in result.stdout, "touched the checkout with billing off"
    assert "Deploying" not in result.stdout


def test_an_unreadable_billing_state_is_shown_not_guessed(tmp_path):
    result = _run(tmp_path, "ERROR: (gcloud.billing) PERMISSION_DENIED", exit_code=1)
    assert result.returncode == 1
    assert "PERMISSION_DENIED" in result.stdout
    assert "Deploying" not in result.stdout


def test_billing_on_proceeds_to_the_checkout_and_the_deploy(tmp_path):
    result = _run(tmp_path, "True")
    assert "billing is enabled" in result.stdout
    assert "GIT_CALLED_" in result.stdout, "the checkout step never ran git"
    assert "Deploying" in result.stdout


def test_the_order_is_billing_then_checkout_then_deploy_then_pay():
    text = SCRIPT.read_text()
    assert text.index("Billing on") < text.index("Checkout at origin/main") < text.index(
        "repair-and-deploy.sh ||"
    ) < text.index("exec bash scripts/first-paid-call.sh")
