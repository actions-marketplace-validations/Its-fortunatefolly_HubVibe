"""The Solana hash top-up: a USDC transfer on Solana redeemed for a prepaid key.

The transaction every test starts from is REAL: tests/fixtures/
solana-usdc-transfer-2tQi2n.json is the jsonParsed getTransaction result of
the $5.00 x402 settlement of 2026-09-23 (slot 449595753), 5,000,000 atomic
USDC from 28HFaU... to the node's Solana wallet J1K4md.... It carries no
challenge reference, so as-is it must be refused; the success paths add a
reference to a copy, exactly as a Solana Pay wallet would.

Keys are minted by the real billing.issue_prepaid_key into a temporary
SQLite key store and then spent through a real paid route, so "a key was
issued" is proven by the key buying work, not by a return value.
"""

import copy
import importlib.util
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "solana-usdc-transfer-2tQi2n.json"
PAY_TO = "J1K4mdbvXEbJLRKpgxFcA7s66LWMCYHu71ye6Hz2icSh"
PAYER = "28HFaUbw5ZRmcYiQw89kGDbnDhBFSdL1Lx1dHESguAiw"
REAL_SIG = "2tQi2nGu2iPE4NBAp3JE5KYgDRFQHQhqvc6gADvdQsnf7M4gCvYx87mC4DBw6KmkRizjuTF2UbmNzAhB5qC4LPkn"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
REAL_TX = json.loads(FIXTURE.read_text())


def _drop_cache():
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)


@pytest.fixture
def app(monkeypatch, tmp_path):
    """main.py loaded fresh against a temp key store and a temp replay ledger,
    with the Solana top-up configured and every sibling module re-read, then
    dropped again so no later test inherits this configuration."""
    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("SOLANA_HASH_LEDGER_PATH", str(tmp_path / "solana-hashes.db"))
    monkeypatch.setenv("X402_SOLANA_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setenv("SOLANA_HASH_CHALLENGE_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    for var in ("SOLANA_RPC_URL", "SOLANA_HASH_PAY_TO", "MPP_CHALLENGE_SECRET",
                "AUDIT_API_KEY", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    _drop_cache()
    spec = importlib.util.spec_from_file_location("wcag_main_solana_hash", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.workers is not None:
        module.workers.ledger.reset_for_tests()
        module.workers.router.configure(
            authorize_and_rate_limit=module._authorize_and_rate_limit,
            bill=module._bill, deliver=module._deliver,
            failed_response=module._failed_audit_response,
            node_version=module.SERVICE_VERSION)
    yield module
    if module.workers is not None:
        module.workers.ledger.reset_for_tests()
    _drop_cache()


@pytest.fixture
def client(app):
    return TestClient(app.app)


def _rpc_serving(tx, genesis=None, calls=None):
    def rpc(method, params):
        if calls is not None:
            calls.append(method)
        if method == "getGenesisHash":
            return genesis or "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        return copy.deepcopy(tx)
    return rpc


def _bound(reference, memo=False, tx=None):
    """The real transfer, carrying a challenge reference the way a Solana Pay
    wallet adds it (read-only account key) or an exchange adds a memo."""
    t = copy.deepcopy(tx or REAL_TX)
    if memo:
        t["transaction"]["message"]["instructions"].append({
            "program": "spl-memo", "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
            "parsed": f"hubvibe:{reference}", "stackHeight": None})
    else:
        t["transaction"]["message"]["accountKeys"].append(
            {"pubkey": reference, "signer": False, "writable": False, "source": "transaction"})
    return t


def _sig(app, n):
    return app.solana_hash_verifier.b58encode(bytes([n]) * 64)


def _challenge(app, amount=5_000_000, before_payment=60):
    """A challenge issued shortly before the real transfer landed."""
    return app.solana_hash_verifier.issue_challenge(amount, now=REAL_TX["blockTime"] - before_payment)


def _redeem(client, signature, token):
    return client.post("/pay/solana/redeem", json={"tx_signature": signature, "challenge_token": token})


def _balance(app, key):
    return app.billing.lookup_key(key)["prepaid_balance_cents"]


# --- the real transaction -----------------------------------------------------------

def test_the_real_settlement_reads_as_five_dollars_from_the_payer(app):
    movement = app.solana_hash_verifier.usdc_movement(REAL_TX, PAY_TO)
    assert movement == {"received_atomic": 5_000_000, "payer": PAYER}
    assert app.solana_hash_verifier.usdc_movement(REAL_TX, PAYER)["received_atomic"] < 0


def test_an_unbound_payment_is_refused_even_though_it_paid_us(app, client, monkeypatch):
    """The real transfer paid the right wallet the right amount, but carries
    no reference: anyone watching the wallet could present it. Refused."""
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(REAL_TX))
    challenge = _challenge(app)
    response = _redeem(client, REAL_SIG, challenge["challenge_token"])
    assert response.status_code == 400
    assert response.json()["reason"] == "reference_missing"
    assert response.json()["credited"] is False


# --- the whole path over HTTP -----------------------------------------------------------

def test_challenge_redeem_then_the_key_buys_work(app, client, monkeypatch):
    challenge = client.post("/pay/solana/challenge", json={"amount_usd": 5}).json()
    assert challenge["status"] == "ok" and challenge["amount_atomic"] == "5000000"
    assert challenge["pay_to"] == PAY_TO
    assert f"reference={challenge['reference']}" in challenge["solana_pay_url"]
    # Re-issue with a timestamp before the real transfer so it falls inside
    # the window; the HTTP route stamps "now", which is after it.
    challenge = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(challenge["reference"])))

    response = _redeem(client, REAL_SIG, challenge["challenge_token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["credit_cents"] == 500 and body["uncredited_atomic"] == 0
    assert body["idempotent_replay"] is False
    assert body["payment"] == {
        "rail": "solana-hash", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "pay_to": PAY_TO,
        "payer": PAYER, "amount_atomic": "5000000", "tx_hash": REAL_SIG,
        "slot": REAL_TX["slot"], "block_time": REAL_TX["blockTime"]}
    key = body["api_key"]
    assert _balance(app, key) == 500

    # The key is spendable on a real paid route, and is debited for it.
    work = client.post("/work/stats/probability", headers={"X-API-Key": key},
                       json={"points": [[1, 2], [2, 4.1], [3, 5.9]]})
    assert work.status_code == 200, work.text
    assert _balance(app, key) == 450

    # Redeeming again (a lost response) returns the same key, and nothing new.
    again = _redeem(client, REAL_SIG, challenge["challenge_token"])
    assert again.status_code == 200
    assert again.json()["api_key"] == key and again.json()["idempotent_replay"] is True
    assert _balance(app, key) == 450


def test_the_top_up_is_advertised_only_where_it_can_be_redeemed(app, client, monkeypatch):
    entry = client.get("/.well-known/agent.json").json()["payment"]["solana_topup"]
    assert entry["challenge"].endswith("/pay/solana/challenge")
    assert entry["redeem"].endswith("/pay/solana/redeem")
    assert entry["limits_usd"] == [0.5, 100.0]
    assert "/pay/solana/challenge" in client.get("/llms.txt").text
    monkeypatch.setenv("SOLANA_HASH_CHALLENGE_SECRET", "short")
    assert "solana_topup" not in client.get("/.well-known/agent.json").json()["payment"]


@pytest.mark.parametrize("body, reason", [
    ({"amount_usd": 0.1}, "invalid_amount"),
    ({"amount_usd": 101}, "invalid_amount"),
    ({"amount_usd": 5.0000001}, "invalid_amount"),
    ({"amount_usd": True}, "invalid_amount"),
    ({"amount_usd": "five"}, "invalid_amount"),
    ({}, "invalid_amount"),
    ([1, 2], "invalid_request"),
])
def test_a_challenge_for_an_unsellable_amount_is_refused(client, body, reason):
    response = client.post("/pay/solana/challenge", json=body)
    assert response.status_code == 400
    assert response.json()["reason"] == reason


# --- replays -------------------------------------------------------------------------------

def test_each_transfer_and_each_challenge_redeems_once(app, client, monkeypatch):
    first = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(first["reference"])))
    assert _redeem(client, REAL_SIG, first["challenge_token"]).status_code == 200

    second = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(second["reference"])))
    response = _redeem(client, REAL_SIG, second["challenge_token"])
    assert response.status_code == 409 and response.json()["reason"] == "signature_already_redeemed"

    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(first["reference"])))
    response = _redeem(client, _sig(app, 7), first["challenge_token"])
    assert response.status_code == 409 and response.json()["reason"] == "challenge_already_redeemed"


def test_the_replay_ledger_survives_a_restart(app, monkeypatch):
    verifier = app.solana_hash_verifier
    challenge = _challenge(app)
    tx = _bound(challenge["reference"])
    minted = []
    assert verifier.redeem(REAL_SIG, challenge["challenge_token"], rpc=_rpc_serving(tx),
                           issue_key=lambda c: minted.append(c) or f"key-{len(minted)}")["status"] == "ok"
    # A second instance of the module: nothing in memory, the same ledger file.
    spec = importlib.util.spec_from_file_location("solana_hash_restarted", verifier.__file__)
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    other = _challenge(app)
    result = restarted.redeem(REAL_SIG, other["challenge_token"], rpc=_rpc_serving(_bound(other["reference"])),
                              issue_key=lambda c: minted.append(c) or "never")
    assert result["reason"] == "signature_already_redeemed"
    assert minted == [500]
    mode = os.stat(os.environ["SOLANA_HASH_LEDGER_PATH"]).st_mode & 0o777
    assert mode == 0o600


def test_concurrent_redemptions_of_one_transfer_mint_one_key(app):
    verifier = app.solana_hash_verifier
    challenge = _challenge(app)
    tx = _bound(challenge["reference"])
    minted, results = [], []
    lock = threading.Lock()

    def issue(cents):
        with lock:
            minted.append(cents)
            return f"key-{len(minted)}"

    def attempt():
        results.append(verifier.redeem(REAL_SIG, challenge["challenge_token"],
                                       rpc=_rpc_serving(tx), issue_key=issue))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert minted == [500]
    assert {r.get("api_key") for r in results if r["status"] == "ok"} == {"key-1"}
    assert {r["reason"] for r in results if r["status"] != "ok"} <= {"claim_pending"}


def test_an_interrupted_redemption_is_never_retaken(app, client, monkeypatch):
    challenge = _challenge(app)
    db = sqlite3.connect(os.environ["SOLANA_HASH_LEDGER_PATH"])
    db.execute(app.solana_hash_verifier._SCHEMA)
    db.execute(
        "INSERT INTO solana_hash_claims (tx_hash, reference, status, pay_to, amount_atomic, "
        "asset, network, claimed_at) VALUES (?, ?, 'claimed', ?, 5000000, ?, ?, 0)",
        (REAL_SIG, challenge["reference"], PAY_TO, app.solana_hash_verifier.USDC_MINT,
         app.solana_hash_verifier.NETWORK))
    db.commit()
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(challenge["reference"])))
    response = _redeem(client, REAL_SIG, challenge["challenge_token"])
    assert response.status_code == 409
    assert response.json()["reason"] == "claim_stuck" and response.json()["retryable"] is False


# --- what the chain must show --------------------------------------------------------------

def _failed(tx):
    tx["meta"]["err"] = {"InstructionError": [2, {"Custom": 1}]}
    return tx


def _usdt(tx):
    for balance in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]:
        balance["mint"] = USDT_MINT
    return tx


@pytest.mark.parametrize("amount, before, mutate, reason", [
    (6_000_000, 60, None, "underpaid"),
    (5_000_000, 60, _failed, "transaction_failed"),
    (5_000_000, 10_000, None, "paid_after_expiry"),
    (5_000_000, 60, _usdt, "not_paid_to_us"),
])
def test_a_transfer_that_does_not_satisfy_its_challenge_is_refused(
        app, client, monkeypatch, amount, before, mutate, reason):
    challenge = _challenge(app, amount=amount, before_payment=before)
    tx = _bound(challenge["reference"])
    if mutate is not None:
        tx = mutate(tx)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(tx))
    response = _redeem(client, _sig(app, 10), challenge["challenge_token"])
    assert response.status_code == 400, response.text
    assert response.json()["reason"] == reason and response.json()["credited"] is False


def test_a_reference_carried_in_a_memo_is_accepted(app, client, monkeypatch):
    challenge = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc",
                        _rpc_serving(_bound(challenge["reference"], memo=True)))
    response = _redeem(client, _sig(app, 20), challenge["challenge_token"])
    assert response.status_code == 200 and _balance(app, response.json()["api_key"]) == 500


# --- tokens ------------------------------------------------------------------------------------

def test_a_forged_altered_or_stale_token_is_refused(app, client, monkeypatch):
    verifier = app.solana_hash_verifier
    challenge = _challenge(app)
    monkeypatch.setattr(verifier, "_rpc", _rpc_serving(_bound(challenge["reference"])))
    body, mac = challenge["challenge_token"].split(".")
    claim = json.loads(verifier._unb64(body))
    claim["amt"] = 1
    lowered = verifier._b64(json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()) + "." + mac
    for token in (lowered, "not-a-token", None):
        response = _redeem(client, _sig(app, 30), token)
        assert response.status_code == 400 and response.json()["reason"] == "invalid_challenge"
    monkeypatch.setenv("SOLANA_HASH_PAY_TO", PAYER)  # the node's wallet changed
    response = _redeem(client, _sig(app, 30), challenge["challenge_token"])
    assert response.json()["reason"] == "invalid_challenge"
    monkeypatch.delenv("SOLANA_HASH_PAY_TO")
    response = _redeem(client, "not base58!", challenge["challenge_token"])
    assert response.status_code == 400 and response.json()["reason"] == "invalid_signature"


# --- fail closed on infrastructure ----------------------------------------------------------

def test_an_unreadable_chain_is_retryable_and_credits_nothing(app, client, monkeypatch):
    challenge = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(None))
    response = _redeem(client, _sig(app, 40), challenge["challenge_token"])
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert response.json()["reason"] == "not_found_or_not_finalized"

    def down(method, params):
        raise ConnectionError("rpc down")

    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", down)
    response = _redeem(client, _sig(app, 40), challenge["challenge_token"])
    assert response.status_code == 503 and response.json()["reason"] == "rpc_unavailable"


def test_a_node_on_the_wrong_cluster_or_without_https_is_refused(app, client, monkeypatch):
    challenge = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc",
                        _rpc_serving(_bound(challenge["reference"]), genesis=DEVNET_GENESIS))
    response = _redeem(client, _sig(app, 50), challenge["challenge_token"])
    assert response.status_code == 400 and response.json()["reason"] == "wrong_cluster"
    monkeypatch.setenv("SOLANA_RPC_URL", "http://api.mainnet-beta.solana.com")
    response = _redeem(client, _sig(app, 50), challenge["challenge_token"])
    assert response.status_code == 503 and response.json()["reason"] == "not_configured"


def test_without_a_secret_nothing_is_issued_or_redeemed(app, client, monkeypatch):
    challenge = _challenge(app)
    monkeypatch.setenv("SOLANA_HASH_CHALLENGE_SECRET", "short")
    assert client.post("/pay/solana/challenge", json={"amount_usd": 5}).status_code == 503
    response = _redeem(client, _sig(app, 60), challenge["challenge_token"])
    assert response.status_code == 503 and response.json()["reason"] == "not_configured"


def test_a_key_store_failure_releases_the_claim_so_the_retry_succeeds(app, client, monkeypatch):
    challenge = _challenge(app)
    monkeypatch.setattr(app.solana_hash_verifier, "_rpc", _rpc_serving(_bound(challenge["reference"])))
    real_issue = app.billing.issue_prepaid_key

    def offline(cents):
        raise RuntimeError("key store offline")

    monkeypatch.setattr(app.billing, "issue_prepaid_key", offline)
    response = _redeem(client, _sig(app, 70), challenge["challenge_token"])
    assert response.status_code == 503 and response.json()["reason"] == "key_store_unavailable"
    monkeypatch.setattr(app.billing, "issue_prepaid_key", real_issue)
    response = _redeem(client, _sig(app, 70), challenge["challenge_token"])
    assert response.status_code == 200 and _balance(app, response.json()["api_key"]) == 500
