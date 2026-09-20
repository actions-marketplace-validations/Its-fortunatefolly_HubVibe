"""The VPS deploy stack must refuse bad money configuration before it
touches the machine, and its pieces must agree with each other.

The installer is driven for real (bash, subprocess) through every branch
that can run without Docker -- which is exactly the set of branches that
guard money. The compose file and env example are parsed, not grepped,
because a stack whose pieces disagree (a variable the compose file needs
that the example never names, a port Caddy expects that the service does
not listen on) fails at 2 a.m. on a box with nobody watching.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "vps-install.sh"
VPS_DIR = REPO_ROOT / "deploy" / "vps"

ZERO = "0x" + "0" * 40
UNAFFIRMED = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"
TEST_CONSTANT = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _run(*args, env=None, tmp_path=None):
    """Drive the installer with docker STUBBED to fail its plugin check.

    The machine running the tests may genuinely have Docker (this repo's
    sandbox does), and without the stub the happy-path test sailed past the
    Docker gate, wrote deploy/vps/.env into the working tree and started a
    real compose build -- a test with side effects on the repo and the
    host. The stub makes every run stop deterministically at the compose
    check, which is the first line past the money gates.
    """
    import tempfile

    stub_dir = tempfile.mkdtemp(dir=str(tmp_path) if tmp_path else None)
    stub = Path(stub_dir) / "docker"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    merged = {"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": "/tmp"}
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=merged, cwd=REPO_ROOT,
    )


def test_bash_parses_the_script():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_domain_prints_usage_and_installs_nothing():
    result = _run()
    assert result.returncode == 1
    assert "Usage:" in result.stdout
    assert "DNS A record" in result.stdout


@pytest.mark.parametrize("bad", ["https://audits.example.com", "not-a-domain"])
def test_a_url_or_non_domain_is_refused(bad):
    result = _run(bad)
    assert result.returncode == 1
    assert "STOP" in result.stdout


@pytest.mark.parametrize(
    "address,why",
    [
        (ZERO, "zero address"),
        (UNAFFIRMED, "nobody here holds the key"),
        (TEST_CONSTANT, "nobody here holds the key"),
        ("0xabc", "40 hex"),
        ("0x" + "g" * 40, "40 hex"),
    ],
)
def test_an_unpayable_recipient_stops_the_install_before_docker(address, why):
    """The money gate runs FIRST. Every one of these has bitten this repo:
    the zero address shipped live, the unaffirmed address sat deployed, the
    test constant was pasted truncated. On a fresh box the script must die
    on them before Docker is even checked for -- 'Nothing was installed'."""
    result = _run("audits.example.com", env={"X402_PAY_TO_ADDRESS": address})
    assert result.returncode == 1, result.stdout
    assert "Nothing was installed" in result.stdout
    assert "Docker" not in result.stdout.split("STOP")[0].split("==> Checking the x402")[0], (
        "docker was touched before the money gate"
    )


def test_the_affirmed_default_passes_the_gate_and_proceeds_to_docker():
    """With a good recipient, the next stop is the Docker check -- where the
    stubbed docker fails its compose-plugin probe, which is exactly the
    proof the money gate passed and nothing was written."""
    result = _run("audits.example.com")
    assert "passes every gate" in result.stdout
    assert "Checking Docker" in result.stdout
    assert result.returncode == 1, "the stubbed docker must stop the run before any write"
    assert "Compose plugin is missing" in result.stdout
    assert not (VPS_DIR / ".env").exists(), "the test run wrote .env into the repo"


def _compose() -> dict:
    return yaml.safe_load((VPS_DIR / "docker-compose.yml").read_text())


def test_compose_parses_and_wires_the_service_correctly():
    compose = _compose()
    service = compose["services"]["hubvibe"]
    env = service["environment"]
    # The identity is the domain, never the box.
    assert env["PUBLIC_BASE_URL"] == "https://${DOMAIN}"
    # Off-Google key store, on a persistent volume -- a key the top-up sold
    # must survive a container restart or the money it holds is destroyed.
    assert env["KEY_STORE"] == "sqlite"
    data_dir = env["KEY_STORE_SQLITE_PATH"].rsplit("/", 1)[0]
    assert any(v.split(":")[1] == data_dir for v in service["volumes"]), (
        "the SQLite path is not on a mounted volume; every restart would wipe the balances"
    )
    # Exactly one trusted proxy (Caddy) fronts the service.
    assert env["RATE_LIMIT_PROXY_DEPTH"] == "1"
    assert service["restart"] == "unless-stopped"
    assert "healthcheck" in service


def test_caddy_terminates_tls_and_proxies_to_the_service_port():
    compose = _compose()
    caddy = compose["services"]["caddy"]
    assert "80:80" in caddy["ports"] and "443:443" in caddy["ports"]
    caddyfile = (VPS_DIR / "Caddyfile").read_text()
    assert "reverse_proxy hubvibe:8080" in caddyfile
    assert "{$DOMAIN}" in caddyfile
    assert caddy["environment"]["DOMAIN"] == "${DOMAIN}", (
        "Caddy never sees DOMAIN, so it would serve nothing"
    )
    # And 8080 is the port the image actually listens on.
    dockerfile = (REPO_ROOT / "wcag-audit-engine" / "Dockerfile").read_text()
    assert "8080" in dockerfile


def test_the_env_example_names_every_variable_the_stack_reads():
    example = (VPS_DIR / ".env.example").read_text()
    for var in ("DOMAIN", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "MAX_CONCURRENT_AUDITS"):
        assert f"{var}=" in example, f"{var} missing from .env.example"
    # Stripe stays opt-in: present as documentation, commented out.
    assert "# STRIPE_SECRET_KEY=" in example


def test_the_installer_writes_only_intended_defaults():
    """The .env the installer writes is read straight off the script text:
    the affirmed wallet and an https facilitator as defaults, overridable,
    and never an AUDIT_API_KEY (an unmetered bypass has no place in a
    default production env). WHICH facilitator is pinned by
    test_the_deploy_default_facilitator_matches_the_scripts_that_pay, not
    by name here -- naming it here is how the installer and the paying
    scripts drifted apart in the first place."""
    script = SCRIPT.read_text()
    assert 'DEFAULT_X402_PAY_TO="0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"' in script
    assert 'FACILITATOR="${X402_FACILITATOR_URL:-https://' in script
    assert 'chmod 600 "$ENV_FILE"' in script, "the env file holds Stripe keys; it must not be world-readable"
    writes = script.split("Writing deploy/vps/.env", 1)[1]
    assert "AUDIT_API_KEY=" not in writes.split("---")[0].replace("for var in", ""), (
        "the installer must never write the unmetered bypass key by default"
    )


def test_a_rerun_keeps_the_existing_env():
    script = SCRIPT.read_text()
    assert 'if [ -f "$ENV_FILE" ]' in script
    assert "keeping the keys" in script, (
        "a re-run that rewrote .env would destroy live Stripe keys"
    )
    # The whole file is never regenerated on the re-run path.
    rerun = script[script.index('if [ -f "$ENV_FILE" ]'):script.index("else\n  {")]
    assert "> \"$ENV_FILE\"" not in rerun, "a re-run must not truncate .env"


def test_a_rerun_updates_the_address_the_operator_explicitly_set():
    """Updating only DOMAIN was a trap. An operator changing the address they
    are paid at re-ran this, saw it succeed, and went on being paid at the old
    address with nothing said. Only what they set in the shell is overwritten,
    so a default can never clobber a deliberate value."""
    script = SCRIPT.read_text()
    rerun = script[script.index('if [ -f "$ENV_FILE" ]'):script.index("else\n  {")]
    assert 'upsert_env X402_PAY_TO_ADDRESS' in rerun
    assert 'upsert_env X402_FACILITATOR_URL' in rerun
    assert '[ -n "${X402_PAY_TO_ADDRESS:-}" ]' in rerun, (
        "the default must not overwrite a deliberately different address"
    )
    assert '[ -n "${X402_FACILITATOR_URL:-}" ]' in rerun


def test_the_env_file_is_locked_down_on_every_path():
    """.env.example tells the operator to copy it into place, and it can hold
    live Stripe keys. chmod inside the create branch left every such file at
    whatever mode it arrived with."""
    script = SCRIPT.read_text()
    after = script[script.index("  } > \"$ENV_FILE\""):]
    assert 'chmod 600 "$ENV_FILE"' in after
    body = after[:after.index('chmod 600 "$ENV_FILE"')]
    assert body.count("fi") >= 1, "chmod must sit after the if/else, not inside it"


def test_the_installer_says_who_gets_paid():
    """The one fact an operator has to confirm in seconds, read from the file
    the node will actually load rather than from the installing shell."""
    script = SCRIPT.read_text()
    assert "paid to $(grep '^X402_PAY_TO_ADDRESS=' \"$ENV_FILE\"" in script


# --- 2026-09-06: the two mistakes the first real install could make ---


def test_the_installer_refuses_to_run_in_google_cloud_shell(tmp_path):
    """The owner pasted the one-liner into Cloud Shell -- a temporary
    terminal, not a server -- and it failed only because a folder happened
    to exist. Refuse by name, before Docker is touched."""
    result = _run("hubvibe-io.com", env={"CLOUD_SHELL": "true"}, tmp_path=tmp_path)
    assert result.returncode == 1
    assert "Cloud Shell" in result.stdout
    assert "Nothing was installed" in result.stdout
    assert "Checking Docker" not in result.stdout, "Cloud Shell got as far as Docker"

    result = _run("hubvibe-io.com", env={"DEVSHELL_PROJECT_ID": "resolver-time"}, tmp_path=tmp_path)
    assert result.returncode == 1
    assert "Cloud Shell" in result.stdout


def test_container_logs_are_rotated():
    """Docker's json-file driver keeps every line forever unless told
    otherwise. One INFO line per settlement plus the access log fills a
    small box's disk in months, and a full disk takes the node down."""
    compose = _compose()
    for name in ("hubvibe", "caddy"):
        logging = compose["services"][name].get("logging") or {}
        assert logging.get("driver") == "json-file", f"{name}: no rotating log driver"
        options = logging.get("options") or {}
        assert options.get("max-size"), f"{name}: no max-size on its logs"
        assert options.get("max-file"), f"{name}: no max-file on its logs"


def test_caddy_serves_www_as_a_redirect_and_caps_request_bodies():
    """The runbook says to point www at the box too. A Caddyfile that names
    only the apex would refuse a certificate for www and close the
    connection. And the app's body cap must be mirrored at the edge."""
    import os
    import re
    import shutil

    text = (VPS_DIR / "Caddyfile").read_text()
    apex = re.search(r"^\{\$DOMAIN\}\s*\{(.*?)^\}", text, re.S | re.M)
    www = re.search(r"^www\.\{\$DOMAIN\}\s*\{(.*?)^\}", text, re.S | re.M)
    assert apex and "reverse_proxy hubvibe:8080" in apex.group(1)
    assert www, "no www site block"
    assert re.search(r"redir\s+https://\{\$DOMAIN\}\{uri\}\s+permanent", www.group(1))
    assert "reverse_proxy" not in www.group(1), "www must redirect, not serve a second identity"
    assert re.search(r"request_body\s*\{\s*max_size\s+4MB", apex.group(1))

    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    if caddy:
        result = subprocess.run(
            [caddy, "validate", "--config", str(VPS_DIR / "Caddyfile"), "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=60, env={"DOMAIN": "example.com", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr + result.stdout


def test_the_deploy_default_facilitator_matches_the_scripts_that_pay():
    """A node installed against one facilitator while first-paid-call.sh
    checks another indexes nothing and reports it as failure. The live box
    was installed on xpay.sh after #66 moved everything else to Dexter;
    pinning them together is what stops that recurring.

    first-paid-call.sh now prefers the live node's own X402_FACILITATOR_URL
    over any default, which is the real fix. This pin still matters for the
    case that produced it: a box whose `.env` cannot be read from where the
    script runs falls back to this default, and a fallback that disagrees
    with the installer is the same bug one step further along."""
    import re

    def default(path, var):
        text = (REPO_ROOT / path).read_text()
        # Either `X="${X:-https://...}"` or a plain `X="https://..."` used as
        # the last resort inside a resolution branch.
        m = re.search(
            rf'^\s*{var}="(?:\$\{{[A-Z0-9_]+:-)?(https://[^}}"]+)\}}?"', text, re.M
        )
        assert m, f"no default facilitator found in {path}"
        return m.group(1)

    installer = default("scripts/vps-install.sh", "FACILITATOR")
    payer = default("scripts/first-paid-call.sh", "FACILITATOR")
    assert installer == payer, (
        f"vps-install.sh installs against {installer} but first-paid-call.sh "
        f"checks {payer} -- a paid call would register the node nowhere"
    )

    env_example = (VPS_DIR / ".env.example").read_text()
    assert f"X402_FACILITATOR_URL={installer}" in env_example, (
        ".env.example names a different facilitator than the installer writes"
    )
    # And the only safe way to change it on a live box is named where an
    # operator reading the file will see it.
    assert "switch-facilitator.sh" in env_example


# --- The facilitator gate ---------------------------------------------------
#
# The recipient gate above catches a wallet that cannot receive. This catches
# the other half of the same failure, and it is the quieter one: a facilitator
# that cannot settle on Base makes the node drop every rail at startup, so the
# box answers /health 200 and sells nothing with no error anywhere.


def _run_with_facilitator(tmp_path, supported_body, *, supported_code="200", index_code="404"):
    """Drive the installer with docker AND curl stubbed, so the facilitator
    gate sees exactly the /supported answer this test is about."""
    import subprocess as sp
    import textwrap

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (stub_dir / "curl").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        OUT=""; PREV=""
        for a in "$@"; do
          [ "$PREV" = "-o" ] && OUT="$a"
          PREV="$a"
        done
        for a in "$@"; do case "$a" in
          */discovery/resources)
            [ -n "$OUT" ] && printf '%s' '{{"items":[]}}' > "$OUT"
            printf '{index_code}'; exit 0 ;;
        esac; done
        [ -n "$OUT" ] && printf '%s' '{supported_body}' > "$OUT"
        printf '{supported_code}'
        """))
    for name in ("docker", "curl"):
        (stub_dir / name).chmod(0o755)
    return sp.run(
        ["bash", str(SCRIPT), "hubvibe-io.com"], capture_output=True, text=True, timeout=90,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}, cwd=REPO_ROOT,
    )


_BASE_V2 = '{"kinds":[{"x402Version":2,"scheme":"exact","network":"eip155:8453"}]}'
_BASE_V1 = '{"kinds":[{"x402Version":1,"scheme":"exact","network":"base"}]}'
_SEPOLIA_ONLY = '{"kinds":[{"x402Version":2,"scheme":"exact","network":"eip155:84532"}]}'
_SEPOLIA_V1_ONLY = '{"kinds":[{"x402Version":1,"scheme":"exact","network":"base-sepolia"}]}'


@pytest.mark.parametrize("body", [_BASE_V2, _BASE_V1])
def test_a_facilitator_that_settles_base_mainnet_passes_the_gate(tmp_path, body):
    """Either vocabulary is enough to take money: v2's CAIP-2 name or v1's."""
    result = _run_with_facilitator(tmp_path, body)
    assert "settles exact/Base mainnet" in result.stdout
    assert "Checking Docker" in result.stdout, "the gate blocked a usable facilitator"


@pytest.mark.parametrize("body", [_SEPOLIA_ONLY, _SEPOLIA_V1_ONLY])
def test_a_facilitator_that_cannot_settle_base_stops_the_install(tmp_path, body):
    """THE test. Reachable and wrong is a definite misconfiguration, and the
    node's own failure mode for it is silence -- so it must stop here.

    Both testnet names are checked because both CONTAIN a mainnet name:
    eip155:84532 contains eip155:8453, and base-sepolia contains base. A
    substring match passed a testnet-only facilitator on the first run of
    this test -- the precise bug the gate exists to catch."""
    result = _run_with_facilitator(tmp_path, body)
    assert result.returncode == 1
    assert "does not list exact on Base mainnet" in result.stdout
    assert "Nothing was installed" in result.stdout
    assert "Checking Docker" not in result.stdout, "it went on to install anyway"
    assert not (VPS_DIR / ".env").exists(), "wrote an .env for a facilitator that cannot settle"


def test_an_unreachable_facilitator_warns_but_still_installs(tmp_path):
    """An outage is not a misconfiguration. The node re-reads /supported and
    fails closed on its own, so refusing to install would be worse."""
    result = _run_with_facilitator(tmp_path, "", supported_code="000")
    assert "did not answer" in result.stdout
    assert "Checking Docker" in result.stdout
    assert "payment-status.sh" in result.stdout, "must say how to see the rail later"


def test_a_facilitator_with_no_index_is_flagged_not_hidden(tmp_path):
    """Settling and indexing are different capabilities. xpay.sh settles and
    indexes nothing, which is how the live node ended up unable to register
    itself no matter how many payments it took."""
    result = _run_with_facilitator(tmp_path, _BASE_V2, index_code="404")
    assert "serves no /discovery/resources" in result.stdout
    assert "switch-facilitator.sh" in result.stdout
    assert "Checking Docker" in result.stdout, "no index is a warning, not a refusal"


def test_a_facilitator_with_an_index_says_the_paid_call_will_register(tmp_path):
    result = _run_with_facilitator(tmp_path, _BASE_V2, index_code="200")
    assert "runs a Bazaar index" in result.stdout
    assert "Checking Docker" in result.stdout


def test_compose_gives_inflight_audits_room_to_finish():
    """A page load is allowed 30s and a settle waits on chain inclusion, but
    Docker's default stop grace is 10s. `docker compose up -d --build` on a
    busy node therefore SIGKILLed calls a customer had already paid for."""
    import yaml

    compose = yaml.safe_load(
        (REPO_ROOT / "deploy" / "vps" / "docker-compose.yml").read_text()
    )
    grace = compose["services"]["hubvibe"].get("stop_grace_period")
    assert grace is not None, "no stop_grace_period: in-flight paid audits die on redeploy"
    seconds = int(str(grace).rstrip("s"))
    assert seconds >= 35, f"{grace} is under the 30s a page load may take"
