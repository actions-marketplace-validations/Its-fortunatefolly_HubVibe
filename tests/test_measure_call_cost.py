"""Guards for scripts/measure-call-cost.sh.

The script spends real money (it drives real metered audit calls), so the
failure that matters is it running when it should have refused, or reporting a
cost derived from calls that never produced an audit. Both are tested here.

The measurement itself needs gcloud and the live service, so what is exercised
is the script's argument handling and its refusals -- not a real run.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "measure-call-cost.sh"


import pytest


@pytest.fixture(scope="module")
def _no_key_env(tmp_path_factory):
    """A PATH where no API key can be resolved, on ANY machine.

    Clearing HUBVIBE_API_KEY is not enough: scripts/lib-api-key.sh falls back
    to reading the key out of Secret Manager via gcloud, so on a real Cloud
    Shell the "no way to pay" premise these tests rest on quietly evaporates
    and they fail there while passing in CI. That is the same
    environment-dependent green this repo has been bitten by before, just
    pointed the other way: here the test depended on gcloud being ABSENT.

    Stubbing gcloud to fail makes "no key is available" a fact of the test
    rather than a fact of the machine.
    """
    import os

    bin_dir = tmp_path_factory.mktemp("nokey_bin")
    (bin_dir / "gcloud").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_dir / "gcloud").chmod(0o755)

    env = dict(os.environ)
    env.pop("HUBVIBE_API_KEY", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def _run(args, env=None, base_env=None, **kwargs):
    import os

    full_env = dict(base_env if base_env is not None else os.environ)
    full_env.pop("HUBVIBE_API_KEY", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=full_env, timeout=60, **kwargs
    )


def test_the_script_exists_and_is_valid_bash():
    assert SCRIPT.is_file()
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_it_refuses_to_run_without_a_way_to_pay(_no_key_env):
    """Without a key every call 402s, no audit runs, and the script would
    report the cost of serving a payment challenge -- a plausible-looking
    number that answers the wrong question."""
    result = _run(["--calls", "1"], base_env=_no_key_env)
    assert result.returncode != 0
    assert "HUBVIBE_API_KEY" in result.stderr


def test_unknown_arguments_are_rejected_rather_than_ignored():
    """A typo'd flag must not silently fall through to a default that spends
    money on the wrong endpoint."""
    result = _run(["--iterations", "500"], env={"HUBVIBE_API_KEY": "x"})
    assert result.returncode != 0
    assert "unknown argument" in result.stderr


def test_help_works_without_a_key_and_spends_nothing():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "per-call" in result.stdout.lower() or "cost" in result.stdout.lower()


def test_the_published_prices_it_compares_against_match_the_catalog():
    """The margin verdict is only meaningful if these are the real rates."""
    text = SCRIPT.read_text()
    assert 'PRICE_BUNDLE="0.15"' in text
    assert 'PRICE_SINGLE="0.05"' in text


def test_rates_are_overridable_rather_than_hardcoded():
    """Cloud Run pricing differs by tier and changes over time; a script that
    bakes one number in gives a confidently wrong answer elsewhere."""
    text = SCRIPT.read_text()
    for var in ("CPU_RATE", "MEM_RATE", "REQ_RATE"):
        assert f'{var}="${{{var}:-' in text, f"{var} must be env-overridable"


def test_non_200_calls_are_excluded_from_the_measurement():
    """A failed call ran no audit and was never billed. Averaging it in would
    understate the cost of the calls that did work."""
    text = SCRIPT.read_text()
    assert "EXCLUDED" in text
    assert 'if [ "$code" != "200" ]; then' in text


def test_the_first_call_is_discarded_as_cold_start():
    text = SCRIPT.read_text()
    assert "warmup" in text
    assert "discarded" in text


def test_it_warns_about_min_instances_idle_billing():
    """At low volume, idle billing dominates per-call compute entirely. A cost
    report that omits it is misleading in exactly the situation the service is
    in today."""
    text = SCRIPT.read_text()
    assert "min-instances" in text
    assert "idle billing" in text


def test_service_config_is_read_from_json_not_a_delimited_projection():
    """A multi-field gcloud --format='value[delimiter=","](a,b,c)' returned the
    whole tuple for every field, and cut(1) turned that into confident garbage
    ("cpu=2 2Gi 4 memory=2 2Gi 4 ..."). Parse the whole record instead."""
    text = SCRIPT.read_text()
    assert "--format=json" in text
    assert 'value[delimiter=","]' not in text


def test_a_failed_run_says_what_each_status_means():
    """A measurement that fails is only useful if it names the next step. 500,
    402 and 502 have completely different causes and fixes."""
    text = SCRIPT.read_text()
    for code in ("402)", "500)", "502)", "000)"):
        assert code in text, f"no guidance for HTTP {code}"
    assert "gcloud logging read" in text, "the 500 path must point at the traceback"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
