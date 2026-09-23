"""Solana `hash` rail: a USDC transfer on Solana, redeemed for a prepaid key.

The Solana counterpart of the MPP `evm` hash rail in mpp_payments.py, with
one deliberate difference in what it sells: a payment here buys a PREPAID
KEY (billing.issue_prepaid_key), not one call. The payer sends USDC on
Solana mainnet itself, then presents the transaction signature; this module
reads the transaction from a Solana JSON-RPC node and releases a key worth
what actually arrived. No signature from the payer is ever verified, so a
wallet that cannot sign x402 payloads can still buy.

WHY A CHALLENGE, NOT A BARE SIGNATURE

A transaction signature is public the moment it lands: anyone watching the
recipient wallet sees it. A rail that released a key to whoever presented a
matching signature first would let an observer front-run every payer and
walk off with their credit. So a payment must be bound to the party who will
redeem it, the Solana Pay way:

  1. issue_challenge(amount) returns a fresh random `reference` and an
     HMAC-signed `challenge_token`. The token goes only to the requester.
  2. The payer's transfer carries the reference -- as an extra read-only
     account key (Solana Pay `reference`), or as an SPL Memo reading
     `hubvibe:<reference>` for wallets that cannot add account keys.
  3. redeem(signature, challenge_token) checks the token's HMAC, that the
     finalized transaction carries that reference, and that it moved at
     least the challenged USDC amount to our wallet.

An observer sees the reference on-chain but never the token, and cannot
forge one without the secret.

FAILS CLOSED, EVERYWHERE

Missing configuration, a non-HTTPS RPC, an RPC on the wrong cluster, a
transaction that is not finalized, failed, older than its challenge, short
of the amount, paid to anyone else or in any other mint, a forged or
altered token, a signature or challenge already redeemed, an unreachable
key store -- each is an error payload with `credited: false`. The only path
to a key is a verified transfer that has just been recorded as redeemed.

REPLAY: A DURABLE LEDGER, NOT A SET

The EVM hash rail guards replays with an in-process set, which is enough
for one call. It is not enough for a key: a restart would forget the set
and the same transfer could be redeemed again for fresh credit. So every
redemption is written to SQLite on the persistent volume
(SOLANA_HASH_LEDGER_PATH, next to the worker ledger) BEFORE the key is
minted, with the transaction signature as PRIMARY KEY and the reference
UNIQUE, so two concurrent redemptions of one transfer cannot both win.
Columns use the worker ledger's names for the same facts (tx_hash, payer,
pay_to, amount_atomic, asset, network).

A redemption whose key could not be minted deletes its own row, so the
payer's retry succeeds. A row left `claimed` by a crash between the two
steps is never re-taken automatically -- the key may exist -- and is
reported as `claim_stuck` for the operator to resolve.

Configuration (environment):
  X402_SOLANA_PAY_TO_ADDRESS     our wallet, the same one the x402 Solana
                                 rail pays (override: SOLANA_HASH_PAY_TO)
  SOLANA_HASH_CHALLENGE_SECRET   HMAC secret (fallback MPP_CHALLENGE_SECRET)
  SOLANA_RPC_URL                 https only; default the public mainnet node
  SOLANA_HASH_LEDGER_PATH        default /data/hubvibe-solana-hashes.db
  SOLANA_HASH_MIN_ATOMIC         default 500000     ($0.50)
  SOLANA_HASH_MAX_ATOMIC         default 100000000  ($100.00)
  SOLANA_HASH_CHALLENGE_TTL      seconds, default 900

Served by POST /pay/solana/challenge and POST /pay/solana/redeem (app/main.py),
and advertised under `payment.solana_topup` in /.well-known/agent.json.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote, urlparse

import httpx

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
# Full genesis hash of Solana mainnet-beta; the CAIP-2 id above is its prefix.
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
RAIL = "solana-hash"
USDC_DECIMALS = 6
ATOMIC_PER_CENT = 10_000
MEMO_PREFIX = "hubvibe:"
_MEMO_PROGRAMS = {
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}
_MAC_DOMAIN = b"hubvibe/solana-hash/v1|"
_STUCK_AFTER_SECONDS = 120
_RPC_TIMEOUT = 10.0

# Same wording main._attach_issued_key hands a payer, so a key reads the
# same whichever rail sold it.
API_KEY_NOTE = (
    "Prepaid credit from your top-up, minus this call. Send it as the "
    "X-API-Key header on subsequent requests until the balance runs out; "
    "there is no account and nothing to log in to."
)


class Refused(Exception):
    """A redemption that must not release a key. `reason` is the slug the
    caller branches on; `retryable` says whether the same request can
    succeed later (a transaction still finalizing) or never will."""

    def __init__(self, reason: str, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


def _error(exc: Refused) -> dict:
    return {"status": "error", "reason": exc.reason, "detail": exc.detail,
            "retryable": exc.retryable, "credited": False}


# --- base58 (Bitcoin alphabet, as Solana uses) --------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for char in text:
        if char not in _B58_INDEX:
            raise ValueError("not base58")
        n = n * 58 + _B58_INDEX[char]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def _is_b58_of(text, size: int) -> bool:
    if not isinstance(text, str) or not text or len(text) > 100:
        return False
    try:
        return len(b58decode(text)) == size
    except ValueError:
        return False


# --- configuration --------------------------------------------------------------

def _pay_to() -> Optional[str]:
    value = (os.environ.get("SOLANA_HASH_PAY_TO")
             or os.environ.get("X402_SOLANA_PAY_TO_ADDRESS") or "").strip()
    if not _is_b58_of(value, 32) or b58decode(value) == b"\0" * 32:
        return None
    return value


def _secret() -> Optional[bytes]:
    raw = (os.environ.get("SOLANA_HASH_CHALLENGE_SECRET")
           or os.environ.get("MPP_CHALLENGE_SECRET") or "")
    if len(raw) < 16:
        return None
    # A key of its own, so a token from this rail can never verify as an MPP
    # challenge (or the reverse) even when the two share a configured secret.
    return hmac.new(raw.encode(), _MAC_DOMAIN, hashlib.sha256).digest()


def _rpc_url() -> Optional[str]:
    url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
    return url if urlparse(url).scheme == "https" and urlparse(url).netloc else None


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def limits_atomic() -> tuple:
    """(minimum, maximum) top-up in atomic USDC."""
    return (_int_env("SOLANA_HASH_MIN_ATOMIC", 500_000),
            _int_env("SOLANA_HASH_MAX_ATOMIC", 100_000_000))


def _ledger_path() -> str:
    return os.environ.get("SOLANA_HASH_LEDGER_PATH", "/data/hubvibe-solana-hashes.db")


def unavailable_reason() -> str:
    if _pay_to() is None:
        return "no valid Solana recipient (X402_SOLANA_PAY_TO_ADDRESS)"
    if _secret() is None:
        return "no challenge secret of 16+ characters (SOLANA_HASH_CHALLENGE_SECRET)"
    if _rpc_url() is None:
        return "SOLANA_RPC_URL must be an https:// URL"
    return ""


def configured() -> bool:
    return unavailable_reason() == ""


# --- challenges -------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(body: str) -> str:
    return _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def issue_challenge(amount_atomic: int, now: Optional[float] = None) -> dict:
    """A fresh reference and the token that alone can redeem a payment
    carrying it. Returns the standard payload; never raises."""
    if not configured():
        return _error(Refused("not_configured", unavailable_reason()))
    low, high = limits_atomic()
    if isinstance(amount_atomic, bool) or not isinstance(amount_atomic, int) \
            or not low <= amount_atomic <= high:
        return _error(Refused(
            "invalid_amount",
            f"amount_atomic must be an integer from {low} to {high} "
            f"(USDC has {USDC_DECIMALS} decimals: 1000000 = $1.00)."))
    issued = int(now if now is not None else time.time())
    expires = issued + _int_env("SOLANA_HASH_CHALLENGE_TTL", 900)
    reference = b58encode(secrets.token_bytes(32))
    pay_to = _pay_to()
    claim = {"v": 1, "ref": reference, "amt": amount_atomic, "to": pay_to,
             "mint": USDC_MINT, "net": NETWORK, "iat": issued, "exp": expires}
    body = _b64(json.dumps(claim, sort_keys=True, separators=(",", ":")).encode())
    ui_amount = f"{amount_atomic / 10 ** USDC_DECIMALS:.6f}".rstrip("0").rstrip(".")
    memo = MEMO_PREFIX + reference
    return {
        "status": "ok",
        "challenge_token": f"{body}.{_mac(body)}",
        "reference": reference,
        "memo": memo,
        "amount_atomic": str(amount_atomic),
        "amount_usd": amount_atomic / 10 ** USDC_DECIMALS,
        "asset": USDC_MINT,
        "network": NETWORK,
        "pay_to": pay_to,
        "expires_at": expires,
        "solana_pay_url": (f"solana:{pay_to}?amount={ui_amount}&spl-token={USDC_MINT}"
                           f"&reference={reference}&memo={quote(memo, safe='')}"),
        "instructions": (
            f"Send at least {ui_amount} USDC on Solana mainnet to {pay_to} before "
            f"expires_at, with `reference` as an extra read-only account key on the "
            f"transfer (Solana Pay) or an SPL Memo reading exactly `{memo}`. Then "
            "redeem the transaction signature together with challenge_token. Keep "
            "the token private: it is what proves the payment is yours."),
    }


def _read_challenge(token) -> dict:
    """The claim inside a token we issued, or Refused. Expiry is NOT checked
    here: it bounds when the payment may happen, not when it is redeemed."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 2048:
        raise Refused("invalid_challenge", "challenge_token is missing or malformed.")
    body, mac = token.split(".")
    if not hmac.compare_digest(mac, _mac(body)):
        raise Refused("invalid_challenge", "challenge_token was not issued by this node.")
    try:
        claim = json.loads(_unb64(body))
    except Exception:
        raise Refused("invalid_challenge", "challenge_token is malformed.")
    if claim.get("v") != 1 or claim.get("mint") != USDC_MINT or claim.get("net") != NETWORK:
        raise Refused("invalid_challenge", "challenge_token names another asset or network.")
    if claim.get("to") != _pay_to():
        raise Refused("invalid_challenge",
                      "challenge_token names a recipient this node no longer uses; request a new one.")
    if not _is_b58_of(claim.get("ref"), 32) or not isinstance(claim.get("amt"), int) \
            or not isinstance(claim.get("exp"), int):
        raise Refused("invalid_challenge", "challenge_token is malformed.")
    return claim


# --- the transaction ---------------------------------------------------------------

_genesis_ok = False


def _rpc(method: str, params: list):
    """One JSON-RPC call to the configured node. Raises on any failure."""
    response = httpx.post(
        _rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"User-Agent": "hubvibe-solana-hash/1"}, timeout=_RPC_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def _check_cluster(rpc: Callable) -> None:
    """The node must be mainnet: USDC on any other cluster is worth nothing."""
    global _genesis_ok
    if _genesis_ok:
        return
    if rpc("getGenesisHash", []) != MAINNET_GENESIS:
        raise Refused("wrong_cluster", "The configured Solana RPC is not mainnet-beta.")
    _genesis_ok = True


def _usdc_by_owner(balances) -> dict:
    totals = {}
    for entry in balances or []:
        if entry.get("mint") != USDC_MINT or not entry.get("owner"):
            continue
        amount = int((entry.get("uiTokenAmount") or {}).get("amount") or 0)
        totals[entry["owner"]] = totals.get(entry["owner"], 0) + amount
    return totals


def usdc_movement(tx: dict, recipient: str) -> dict:
    """USDC the recipient gained in this transaction (atomic units, from the
    token balances the node recorded before and after), and the single owner
    whose USDC fell, if there was exactly one."""
    meta = tx.get("meta") or {}
    pre = _usdc_by_owner(meta.get("preTokenBalances"))
    post = _usdc_by_owner(meta.get("postTokenBalances"))
    received = post.get(recipient, 0) - pre.get(recipient, 0)
    senders = [owner for owner in set(pre) | set(post)
               if owner != recipient and post.get(owner, 0) < pre.get(owner, 0)]
    return {"received_atomic": received, "payer": senders[0] if len(senders) == 1 else None}


def _instructions(tx: dict) -> list:
    message = (tx.get("transaction") or {}).get("message") or {}
    found = list(message.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        found.extend(inner.get("instructions") or [])
    return found


def carries_reference(tx: dict, reference: str) -> bool:
    """The reference as an account key of the transaction (Solana Pay), or in
    an SPL Memo as `hubvibe:<reference>`."""
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = {k.get("pubkey") if isinstance(k, dict) else k
            for k in message.get("accountKeys") or []}
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.update(loaded.get("writable") or [])
    keys.update(loaded.get("readonly") or [])
    if reference in keys:
        return True
    memo = MEMO_PREFIX + reference
    for ix in _instructions(tx):
        if ix.get("programId") in _MEMO_PROGRAMS and isinstance(ix.get("parsed"), str) \
                and memo in ix["parsed"]:
            return True
    return False


def verify_transaction(tx: Optional[dict], claim: dict) -> dict:
    """The on-chain facts of a payment that satisfies `claim`, or Refused.
    Pure: `tx` is getTransaction's jsonParsed result at finalized commitment."""
    if not tx:
        raise Refused("not_found_or_not_finalized",
                      "The transaction is not finalized on Solana mainnet yet (or does not "
                      "exist). Finality takes about 15 seconds; retry.", retryable=True)
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        raise Refused("transaction_failed", f"The transaction failed on-chain: {meta['err']}.")
    block_time = tx.get("blockTime")
    if not isinstance(block_time, int):
        raise Refused("no_block_time", "The node reported no block time for this transaction.")
    if block_time > claim["exp"]:
        raise Refused("paid_after_expiry",
                      "The payment landed after the challenge expired; it is not bound to it.")
    if not carries_reference(tx, claim["ref"]):
        raise Refused("reference_missing",
                      "The transaction does not carry this challenge's reference (as an account "
                      "key or a `hubvibe:<reference>` memo), so it cannot be tied to you.")
    movement = usdc_movement(tx, claim["to"])
    received = movement["received_atomic"]
    if received <= 0:
        raise Refused("not_paid_to_us", "The transaction moved no USDC to this node's wallet.")
    if received < claim["amt"]:
        raise Refused("underpaid",
                      f"{received} atomic USDC arrived; the challenge requires {claim['amt']}.")
    return {"received_atomic": received, "payer": movement["payer"],
            "slot": tx.get("slot"), "block_time": block_time}


# --- the replay ledger ---------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS solana_hash_claims (
    tx_hash       TEXT PRIMARY KEY,
    reference     TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL,
    payer         TEXT,
    pay_to        TEXT NOT NULL,
    amount_atomic INTEGER NOT NULL,
    asset         TEXT NOT NULL,
    network       TEXT NOT NULL,
    slot          INTEGER,
    block_time    INTEGER,
    credit_cents  INTEGER,
    api_key       TEXT,
    claimed_at    REAL NOT NULL,
    issued_at     REAL
)
"""
_ledger_lock = threading.Lock()


@contextlib.contextmanager
def _ledger():
    """One autocommit connection, serialized in-process and always closed.
    Across processes, BEGIN IMMEDIATE in redeem() does the serializing."""
    with _ledger_lock:
        path = _ledger_path()
        fresh = not os.path.exists(path)
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            if fresh:
                # It holds issued keys, so it gets the key store's permissions.
                os.chmod(path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            yield conn
        finally:
            conn.close()


def _row(conn, column: str, value):
    return conn.execute(f"SELECT * FROM solana_hash_claims WHERE {column} = ?",
                        (value,)).fetchone()


def _success(row, replay: bool) -> dict:
    return {
        "status": "ok",
        "api_key": row["api_key"],
        "api_key_note": API_KEY_NOTE,
        "credit_cents": row["credit_cents"],
        "credit_usd": row["credit_cents"] / 100,
        "uncredited_atomic": row["amount_atomic"] - row["credit_cents"] * ATOMIC_PER_CENT,
        "payment": {"rail": RAIL, "network": row["network"], "asset": row["asset"],
                    "pay_to": row["pay_to"], "payer": row["payer"],
                    "amount_atomic": str(row["amount_atomic"]), "tx_hash": row["tx_hash"],
                    "slot": row["slot"], "block_time": row["block_time"]},
        "idempotent_replay": replay,
    }


def _prior(conn, tx_signature: str, claim: dict, now: float) -> Optional[dict]:
    """What an earlier redemption of this signature or reference means for
    this one: its key again, a refusal, or None when both are unused."""
    row = _row(conn, "tx_hash", tx_signature)
    if row is not None:
        if row["reference"] != claim["ref"]:
            raise Refused("signature_already_redeemed",
                          "This transaction was already redeemed under another challenge.")
        if row["status"] == "issued":
            # Only the holder of this reference's token gets here, and a
            # payer whose response was lost must be able to recover the key.
            return _success(row, replay=True)
        if now - row["claimed_at"] < _STUCK_AFTER_SECONDS:
            raise Refused("claim_pending", "This payment is being redeemed right now; retry "
                          "in a few seconds.", retryable=True)
        raise Refused("claim_stuck", "This payment's redemption was interrupted before the key "
                      "was recorded. The operator must resolve it; do not pay again.")
    if _row(conn, "reference", claim["ref"]) is not None:
        raise Refused("challenge_already_redeemed",
                      "This challenge was already redeemed by another transaction; "
                      "request a new challenge for a new payment.")
    return None


# --- redemption ------------------------------------------------------------------------

def _default_issue_key() -> Callable[[int], str]:
    """billing.issue_prepaid_key, from the instance main.py loaded when there
    is one (same sys.modules name), so both write through one key store."""
    try:
        from . import billing  # type: ignore

        return billing.issue_prepaid_key
    except ImportError:
        import importlib.util
        import sys
        from pathlib import Path

        name = "wcag_audit_engine_billing"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(
                name, Path(__file__).resolve().parent / "billing.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return module.issue_prepaid_key


def redeem(tx_signature: str, challenge_token: str, *,
           rpc: Optional[Callable] = None,
           issue_key: Optional[Callable[[int], str]] = None) -> dict:
    """Release a prepaid key for a verified Solana USDC payment.

    Returns the standard payload -- `status: ok` with the key, its credit and
    the payment facts, or `status: error` with `reason`, `retryable` and
    `credited: false`. Never raises. `rpc` and `issue_key` are injectable
    for tests; by default they are the configured node and billing's minter.
    """
    try:
        if not configured():
            raise Refused("not_configured", unavailable_reason())
        if not _is_b58_of(tx_signature, 64):
            raise Refused("invalid_signature",
                          "tx_signature must be a base58 Solana transaction signature.")
        claim = _read_challenge(challenge_token)
        now = time.time()

        with _ledger() as conn:
            prior = _prior(conn, tx_signature, claim, now)
        if prior is not None:
            return prior

        call = rpc or _rpc
        try:
            _check_cluster(call)
            tx = call("getTransaction", [tx_signature, {
                "encoding": "jsonParsed", "commitment": "finalized",
                "maxSupportedTransactionVersion": 0}])
        except Refused:
            raise
        except Exception as exc:
            raise Refused("rpc_unavailable",
                          f"The Solana RPC could not be read ({type(exc).__name__}).",
                          retryable=True)
        facts = verify_transaction(tx, claim)
        credit_cents = facts["received_atomic"] // ATOMIC_PER_CENT

        # Record the redemption BEFORE minting: the PRIMARY KEY and UNIQUE
        # reference make this the single point where two concurrent
        # redemptions of one payment are told apart.
        with _ledger() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = _prior(conn, tx_signature, claim, now)
                if prior is not None:
                    conn.execute("ROLLBACK")
                    return prior
                conn.execute(
                    "INSERT INTO solana_hash_claims (tx_hash, reference, status, payer, pay_to, "
                    "amount_atomic, asset, network, slot, block_time, credit_cents, claimed_at) "
                    "VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tx_signature, claim["ref"], facts["payer"], claim["to"],
                     facts["received_atomic"], USDC_MINT, NETWORK, facts["slot"],
                     facts["block_time"], credit_cents, now))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        try:
            api_key = (issue_key or _default_issue_key())(credit_cents)
            if not isinstance(api_key, str) or not api_key:
                raise ValueError("the key store returned no key")
        except Exception as exc:
            with _ledger() as conn:
                conn.execute("DELETE FROM solana_hash_claims WHERE tx_hash = ? AND status = 'claimed'",
                             (tx_signature,))
            raise Refused("key_store_unavailable",
                          f"The payment verified but no key could be issued "
                          f"({type(exc).__name__}); nothing was redeemed, retry.",
                          retryable=True)

        with _ledger() as conn:
            conn.execute("UPDATE solana_hash_claims SET status = 'issued', api_key = ?, "
                         "issued_at = ? WHERE tx_hash = ?", (api_key, time.time(), tx_signature))
            return _success(_row(conn, "tx_hash", tx_signature), replay=False)
    except Refused as exc:
        return _error(exc)
    except Exception as exc:
        return _error(Refused("internal_error",
                              f"Redemption failed ({type(exc).__name__}); nothing was redeemed.",
                              retryable=True))
