"""The state snapshot must record everything and reveal nothing.

Its whole reason to exist is safe-keeping, which makes the one way it could
go wrong catastrophic: a snapshot that read a secret VALUE or the wallet's
private key would put them in a plain-text file the owner is told to
download and keep. These pin the read-only, names-not-values contract.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "snapshot-state.sh"


def test_bash_parses_the_script():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_snapshot_never_reads_a_secret_value():
    """`secrets list` (names) is the ceiling. `versions access` is the call
    that returns a VALUE, and it must never appear here."""
    script = SCRIPT.read_text()
    assert "secrets list" in script
    assert "versions access" not in script, "the snapshot would write a secret value to disk"


def test_the_wallet_key_is_derived_to_an_address_and_never_printed():
    script = SCRIPT.read_text()
    assert "Account.from_key" in script, "the payer address is part of the record"
    assert 'print("payer address:"' in script
    # The one way to leak the key is to print/cat the file itself.
    assert "cat" not in [
        line.split()[0] for line in script.splitlines()
        if ".hubvibe-wallet-key" in line and line.strip() and not line.strip().startswith("#")
    ], "the key file is printed raw"


def test_every_probe_survives_failure():
    """With billing disabled most gcloud calls error; the error text IS the
    record. A bare call would kill the script at the first dead API and the
    snapshot would be a fragment."""
    script = SCRIPT.read_text()
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("gcloud ") or stripped.startswith("curl "):
            raise AssertionError(f"unguarded probe (must go through try()): {stripped[:60]}")
    assert 'try()' in script and "|| printf" in script


def test_the_snapshot_is_read_only():
    """Nothing here may change state: no deletes, no updates, no deploys."""
    script = SCRIPT.read_text()
    for forbidden in ("gcloud run deploy", "services update", "delete", "set-env",
                      "secrets create", "git push", "docker "):
        assert forbidden not in script, f"the snapshot is not read-only: {forbidden!r}"


def test_the_snapshot_reads_the_facilitator_instead_of_restating_it():
    """A hardcoded facilitator in the FACTS block said xpay.sh for a day
    after the repo moved to Dexter. A snapshot exists to be saved and
    trusted later, so a fact it restates from memory is a fact that goes
    stale silently."""
    text = SCRIPT.read_text()
    assert "facilitator.xpay.sh" not in text, "the retired facilitator is hardcoded again"
    assert "deploy/vps/.env" in text, "does not read the live box's configured facilitator"
    assert "vps-install.sh" in text, "no fallback to what a fresh install would write"
    assert "facilitator: $SNAP_FACILITATOR" in text, "the FACTS block is not using the read value"


def test_the_snapshot_names_the_facilitator_actually_configured(tmp_path):
    """Drive the real script far enough to print FACTS: with a deploy .env
    present it must quote that facilitator, not the repo default."""
    import shutil
    import subprocess

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "deploy" / "vps").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / SCRIPT.name)
    shutil.copy(REPO_ROOT / "scripts" / "vps-install.sh", repo / "scripts" / "vps-install.sh")
    (repo / "deploy" / "vps" / ".env").write_text(
        "DOMAIN=hubvibe-io.com\nX402_FACILITATOR_URL=https://facilitator.example\n"
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name)],
        capture_output=True, text=True, timeout=300, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert "facilitator: https://facilitator.example (from deploy/vps/.env on this box)" in result.stdout, (
        result.stdout[-600:]
    )
