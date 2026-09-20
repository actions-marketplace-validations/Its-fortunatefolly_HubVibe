"""The stub facilitator in scripts/simulate-paid-call.py has to have teeth.

The simulation's whole value is that what it accepts, a real facilitator
accepts, and what a real facilitator refuses, it refuses. A stub that says
"valid" to everything would turn the end-to-end run into a status-code
check -- the same form-not-function trap this repo has fallen into four
times. So the checks below sign a real EIP-3009 authorization the way the
x402 client does and confirm the stub verifies it, then tamper with one
field at a time and confirm each tampering is named.
"""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "simulate-paid-call.py"

pytest.importorskip("x402")
pytest.importorskip("eth_account")


def _load():
    spec = importlib.util.spec_from_file_location("simulate_paid_call", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulate_paid_call"] = module
    spec.loader.exec_module(module)
    return module


sim = _load()

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


def _signed_body(*, version=2, amount="30000", pay_to=PAY_TO, tamper=None):
    """A verify/settle request body carrying an authorization signed by a
    throwaway key -- the shape the x402 HTTP facilitator client sends."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
    from x402.mechanisms.evm.types import ExactEIP3009Authorization

    payer = Account.from_key("0x" + "33" * 32)
    now = int(time.time())
    auth = ExactEIP3009Authorization(
        from_address=payer.address,
        to=pay_to,
        value=amount,
        valid_after=str(now - 60),
        valid_before=str(now + 300),
        nonce="0x" + "ab" * 32,
    )
    domain, types, primary, message = build_typed_data_for_signing(
        auth, 8453, USDC_BASE, "USD Coin", "2"
    )
    signable = encode_typed_data(
        domain_data={
            "name": domain.name,
            "version": domain.version,
            "chainId": domain.chain_id,
            "verifyingContract": domain.verifying_contract,
        },
        message_types={primary: types[primary]},
        message_data=message,
    )
    signature = payer.sign_message(signable).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    authorization = {
        "from": auth.from_address,
        "to": auth.to,
        "value": auth.value,
        "validAfter": auth.valid_after,
        "validBefore": auth.valid_before,
        "nonce": auth.nonce,
    }
    if tamper:
        tamper(authorization)
    requirements = {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": USDC_BASE,
        "payTo": pay_to,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }
    if version == 2:
        requirements["amount"] = amount
        payload = {
            "x402Version": 2,
            "payload": {"signature": signature, "authorization": authorization},
            "accepted": dict(requirements),
            "resource": {"url": "https://node.test/audit/wcag"},
        }
    else:
        requirements["maxAmountRequired"] = amount
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": {"signature": signature, "authorization": authorization},
        }
    return {"x402Version": version, "paymentPayload": payload, "paymentRequirements": requirements}


def test_a_genuinely_signed_v2_payment_verifies():
    assert sim._check_payment(sim._normalise(_signed_body())) is None


def test_a_genuinely_signed_v1_payment_verifies_under_the_legacy_name():
    """v1 bodies say "base"; the node's requirements say "eip155:8453". Both
    are chain 8453 and the stub must not call that a network mismatch."""
    assert sim._check_payment(sim._normalise(_signed_body(version=1))) is None


def test_a_forged_signature_is_refused():
    body = _signed_body()
    sig = body["paymentPayload"]["payload"]["signature"]
    body["paymentPayload"]["payload"]["signature"] = sig[:-4] + ("0000" if sig[-4:] != "0000" else "1111")
    assert sim._check_payment(sim._normalise(body)) == "invalid_signature"


def test_a_payment_to_someone_else_is_refused():
    body = _signed_body(pay_to=PAY_TO)
    body["paymentRequirements"]["payTo"] = "0x" + "99" * 20
    assert sim._check_payment(sim._normalise(body)) == "recipient_mismatch"


def test_underpaying_is_refused():
    body = _signed_body(amount="30000")
    body["paymentRequirements"]["amount"] = "100000"  # challenged $0.10, signed $0.03
    reason = sim._check_payment(sim._normalise(body))
    assert reason == "invalid_exact_evm_payload_authorization_value_mismatch"


def test_an_authorization_edited_after_signing_is_refused():
    """Changing the recipient inside the authorization -- so it matches the
    requirements -- must still fail, because the signature was over the
    original. This is the check that makes the stub a verifier and not a
    field comparer."""

    def redirect(authorization):
        authorization["to"] = "0x" + "99" * 20

    body = _signed_body(tamper=redirect)
    body["paymentRequirements"]["payTo"] = "0x" + "99" * 20
    assert sim._check_payment(sim._normalise(body)) == "invalid_signature"


def test_an_expired_authorization_is_refused():
    def expire(authorization):
        authorization["validBefore"] = str(int(time.time()) - 1)

    reason = sim._check_payment(sim._normalise(_signed_body(tamper=expire)))
    assert reason == "invalid_exact_evm_payload_authorization_valid_before"


def test_the_supported_response_is_the_one_xpay_returned():
    """The node's #82 gate reads /supported. The stub mirrors the response
    the owner read off xpay.sh on 2026-09-02 -- both names for Base mainnet
    -- so a simulation pass means the live vocabulary passes the gate."""
    kinds = {(k["x402Version"], k["network"]) for k in sim.SUPPORTED["kinds"]}
    assert (2, "eip155:8453") in kinds
    assert (1, "base") in kinds


def test_help_runs_without_the_service_installed():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert "--no-index" in result.stdout


def test_the_stub_facilitator_speaks_keep_alive():
    """HTTP/1.0 closes every connection and hid the cross-event-loop
    connection-reuse bug (56 of 96 concurrent payments rejected). A stub that
    is easier on the code than a real facilitator is not a simulation."""
    handler = sim._make_handler(sim.FacilitatorState(index=True), "0x" + "11" * 20)
    assert handler.protocol_version == "HTTP/1.1"
