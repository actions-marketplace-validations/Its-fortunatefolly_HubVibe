"""Guards for scripts/repair-secrets.sh.

This script touches Secret Manager on a live project, so the failures that
matter are destructive ones and confidently-wrong ones.

The incident it exists to prevent: a secret was overwritten by hand under the
wrong name, and the attempted fix -- disabling the bad version -- made things
worse. `latest` resolves to the highest version NUMBER regardless of state, so
disabling the newest version does not fall back to the previous one, it makes
`latest` unreadable and any container mounting it fails to start.

So the two properties tested hardest are: it only ever adds versions (never
disables or destroys), and it refuses to invent a value it cannot find.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "repair-secrets.sh"

# Reproduces the live situation: one secret, newest version disabled and
# unreadable, an older enabled version holding a real-looking Stripe key.
_STUB = r"""#!/usr/bin/env bash
case "$*" in
  *"run services describe"*)
    cat <<'J'
{"spec":{"template":{"spec":{"containers":[{"env":[
 {"name":"AUDIT_API_KEY","valueFrom":{"secretKeyRef":
  {"name":"SECRET_STRIPE_KEY","key":"latest"}}}]}]}}}}
J
    ;;
  *"versions access latest"*)
    [ -f "$STATE_DIR/added" ] && printf 'sk_live_ABC123XYZ' || exit 1
    ;;
  *"versions access 4"*) printf 'sk_live_ABC123XYZ' ;;
  *"versions list"*) printf '5\n4\n3\n' ;;
  *"versions add"*) cat >/dev/null; touch "$STATE_DIR/added"; echo "Created version [6]" ;;
  *) exit 1 ;;
esac
echo "$*" >> "$STATE_DIR/calls.log"
"""

# No enabled version holds anything -- the value is simply gone.
_STUB_UNRECOVERABLE = r"""#!/usr/bin/env bash
case "$*" in
  *"run services describe"*)
    cat <<'J'
{"spec":{"template":{"spec":{"containers":[{"env":[
 {"name":"GEMINI_API_KEY","valueFrom":{"secretKeyRef":
  {"name":"gemini-api-key","key":"latest"}}}]}]}}}}
J
    ;;
  *"versions list"*) printf '' ;;
  *) exit 1 ;;
esac
"""


def _run(tmp_path, args=(), stub=_STUB):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gcloud").write_text(stub)
    (bin_dir / "gcloud").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STATE_DIR"] = str(tmp_path)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_it_changes_nothing_without_apply(tmp_path):
    """A script that repairs secrets the moment you run it is one nobody dares
    run. Report is the default."""
    result = _run(tmp_path, [])
    assert "Nothing was changed" in result.stdout
    assert "WOULD copy" in result.stdout
    assert not (tmp_path / "added").exists()


def test_apply_repairs_by_copying_the_newest_usable_version(tmp_path):
    result = _run(tmp_path, ["--apply"])
    assert "REPAIRED" in result.stdout
    assert (tmp_path / "added").exists()


def test_it_never_disables_or_destroys_anything(tmp_path):
    """Disabling the newest version is what turned a wrong value into an
    unstartable container. The repair must be additive only."""
    _run(tmp_path, ["--apply"])
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "versions disable" not in calls
    assert "versions destroy" not in calls
    text = SCRIPT.read_text()
    assert "versions disable" not in text
    assert "versions destroy" not in text


def test_it_refuses_to_invent_a_value_it_cannot_find(tmp_path):
    """When no version holds anything, the value has to come from its source.
    Fabricating a placeholder here is how a live rail starts silently
    failing while still advertising itself as configured."""
    result = _run(tmp_path, ["--apply"], stub=_STUB_UNRECOVERABLE)
    assert "cannot be repaired" in result.stdout
    assert "REPAIRED" not in result.stdout


def test_it_reports_the_env_var_to_secret_wiring(tmp_path):
    """Which secret backs which env var is the thing that was guessed wrong
    and caused the incident."""
    result = _run(tmp_path, [])
    assert "AUDIT_API_KEY" in result.stdout
    assert "SECRET_STRIPE_KEY" in result.stdout


def test_it_never_prints_a_secret_value(tmp_path):
    """Output goes into screenshots and chat logs."""
    for args in ([], ["--apply"]):
        result = _run(tmp_path / f"run{len(args)}", args)
        assert "sk_live_ABC123XYZ" not in result.stdout
        assert "ABC123" not in result.stdout


def test_it_warns_about_a_trailing_newline(tmp_path):
    """A stored newline is invisible everywhere except an exact comparison,
    and cannot be reproduced in an HTTP header -- so a key stored with one can
    never be sent correctly by any client."""
    stub = r"""#!/usr/bin/env bash
case "$*" in
  *"run services describe"*)
    cat <<'J'
{"spec":{"template":{"spec":{"containers":[{"env":[
 {"name":"AUDIT_API_KEY","valueFrom":{"secretKeyRef":
  {"name":"sudul-api-key","key":"latest"}}}]}]}}}}
J
    ;;
  *"versions access latest"*) printf 'somekey\n' ;;
  *) exit 1 ;;
esac
"""
    result = _run(tmp_path, [], stub=stub)
    assert "ends in a newline" in result.stdout


def test_unknown_arguments_are_rejected(tmp_path):
    result = _run(tmp_path, ["--force"])
    assert result.returncode != 0
    assert "unknown argument" in result.stderr


_STUB_WRONG_SHAPE = r"""#!/usr/bin/env bash
case "$*" in
  *"run services describe"*)
    cat <<'J'
{"spec":{"template":{"spec":{"containers":[{"env":[
 {"name":"STRIPE_SECRET_KEY","valueFrom":{"secretKeyRef":
  {"name":"SECRET_STRIPE_KEY","key":"latest"}}}]}]}}}}
J
    ;;
  *"versions access latest"*)
     [ -f "$STATE_DIR/added" ] && printf 'sk_live_REAL' || printf 'hubvibe-verify-key' ;;
  *"versions access 5"*) printf 'hubvibe-verify-key' ;;
  *"versions access 4"*) printf 'sk_live_REAL' ;;
  *"versions list"*) printf '5\n4\n3\n' ;;
  *"versions add"*) cat >/dev/null; touch "$STATE_DIR/added"; echo "Created [6]" ;;
  *) exit 1 ;;
esac
"""


def test_a_readable_but_wrong_value_is_caught(tmp_path):
    """The actual incident. A hand-written test string landed in the Stripe key
    secret. It read back fine, so a readability check calls it healthy -- and
    every Stripe call then fails at the API instead of at startup, which is far
    harder to notice."""
    result = _run(tmp_path, [], stub=_STUB_WRONG_SHAPE)
    assert "WRONG SHAPE" in result.stdout
    assert "expected something starting 'sk_'" in result.stdout
    assert "Nothing was changed" in result.stdout


def test_a_wrong_value_is_repaired_from_the_newest_correct_version(tmp_path):
    result = _run(tmp_path, ["--apply"], stub=_STUB_WRONG_SHAPE)
    assert "newest correct-looking version: 4" in result.stdout
    assert "REPAIRED" in result.stdout
    assert "starts 'sk_'" in result.stdout


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
