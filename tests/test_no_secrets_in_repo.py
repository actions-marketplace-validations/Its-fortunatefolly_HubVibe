"""This repository is PUBLIC. Nothing in it may be a credential.

Not a style rule: the repo is public (confirmed 2026-09-07), so anything
committed here is world-readable forever, and git history keeps it even
after a later commit removes it. The only reliable moment to catch a secret
is before it is committed -- which is what this test is for.

It scans TRACKED files only (what the world can actually read), and it
tolerates the fixtures that must look like credentials in order to prove
redaction works: those live in tests/ and are obvious fakes.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each pattern is a shape that is a credential wherever it appears.
CREDENTIALS = {
    "Stripe live secret key": r"sk_live_[A-Za-z0-9]{20,}",
    "Stripe live restricted key": r"rk_live_[A-Za-z0-9]{20,}",
    "Stripe webhook signing secret": r"whsec_[A-Za-z0-9]{24,}",
    "AWS access key id": r"AKIA[0-9A-Z]{16}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{36}",
    "GitHub fine-grained token": r"github_pat_[A-Za-z0-9_]{50,}",
    "Slack token": r"xox[baprs]-[A-Za-z0-9-]{20,}",
    "private key block": r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY",
    "EVM private key": r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])",
}

# Values that MATCH a pattern above and are provably not secrets.
ALLOWED = {
    # The ERC-20 Transfer event topic0 -- a public constant of the standard,
    # identical in every contract on every chain.
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    # Uniswap V4 PoolId (keccak256 of the PoolKey: currencies, fee, tick
    # spacing, hooks) for the Base ETH/USDC fee=3000/tickSpacing=60 pool --
    # a deterministic, publicly computable identifier, not a secret.
    # .claude/skills/base-mcp/plugins/uniswap.md
    "0xe070797535b13431808f8fc81fdbe7b41362960ed0b55bc2b6117c49c51b7eb9",
    # Bankr launch-API example response `poolId` for a token launch -- a
    # public pool identifier, not a secret.
    # .claude/skills/base-mcp/plugins/bankr.md
    "0x2fee469c920ad9cd8d7fed1510c6034531e0f9fb7c94dbeea35623a358b7580f",
}

# Identifiers that are not credentials but should not sit in a public repo.
PRIVATE_IDENTIFIERS = {
    "Stripe account id": r"acct_[A-Za-z0-9]{16,}",
    "Stripe payment link": r"buy\.stripe\.com/[A-Za-z0-9]{10,}",
}


def _tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    ).stdout
    for name in out.split("\0"):
        if not name:
            continue
        path = REPO_ROOT / name
        try:
            yield name, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue


def test_no_tracked_file_carries_a_credential():
    offenders = []
    for name, text in _tracked_text_files():
        # tests/ must contain credential-SHAPED fixtures to prove the code
        # redacts them; that is the point of those files.
        fixture_file = name.startswith("tests/")
        for label, pattern in CREDENTIALS.items():
            for match in re.finditer(pattern, text):
                value = match.group(0)
                if value in ALLOWED:
                    continue
                if fixture_file and label != "private key block":
                    continue
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{name}:{line}: {label} ({value[:14]}...)")
    assert not offenders, "credentials in a PUBLIC repo:\n" + "\n".join(offenders)


def test_no_tracked_file_exposes_a_private_identifier():
    """Not credentials, but nothing here needs them and the repo is public:
    a Stripe account id and live payment-link URLs for plans that no longer
    exist (removed 2026-09-07)."""
    offenders = []
    for name, text in _tracked_text_files():
        if name == Path(__file__).name or name.endswith("test_no_secrets_in_repo.py"):
            continue
        for label, pattern in PRIVATE_IDENTIFIERS.items():
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{name}:{line}: {label} ({match.group(0)})")
    assert not offenders, "private identifiers in a PUBLIC repo:\n" + "\n".join(offenders)


def test_no_env_or_key_file_is_tracked():
    """deploy/vps/.env holds live Stripe keys on the box; a wallet key file
    holds money. Neither may ever be committed."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    ).stdout.split("\n")
    bad = [
        f for f in tracked
        if f and not f.endswith(".example")
        and re.search(r"(^|/)\.env$|wallet.*key|\.pem$|\.p12$|id_rsa", f)
    ]
    assert not bad, "secret-bearing files are tracked: " + ", ".join(bad)

    gitignore = (REPO_ROOT / ".gitignore").read_text()
    for required in ("deploy/vps/.env", "__pycache__", ".venv"):
        assert required in gitignore, f".gitignore does not cover {required}"
