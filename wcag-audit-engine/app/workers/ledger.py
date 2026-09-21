"""Per-call accounting for the worker network: what we charged, what the
provider cost us, how long it took, and whether it worked.

WHY THIS EXISTS

The audit routes need no ledger: one price, one provider (our own browser),
and the pay-to wallet's balance is the revenue number. The worker network is
the opposite -- many capabilities, each with a real provider bill behind it.
Without a per-call record there is no way to answer the only two questions
that matter commercially: which capabilities agents come back for, and which
ones we are selling below cost.

Margin is DERIVED at read time from measured numbers, never asserted at write
time. A worker whose provider cost we cannot measure records the usage and
reports the margin as unknown, rather than quietly assuming the call was free.

WHY IT CAN NEVER RAISE

This sits in the request path of a call the caller has already paid for. A
ledger failure must cost us the row, never the customer's result.

Money is in MICROS (1e-6 USD): a token-priced call costing $0.000075 is
normal, and cents would round most real costs to zero.
"""

import datetime
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("hubvibe.workers.ledger")

DEFAULT_PATH = "/data/hubvibe-workers.db"
_MICROS = 1_000_000

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_configured_path: Optional[str] = None
_last_error: Optional[str] = None
_degraded = False


def _path() -> str:
    return os.environ.get("WORKER_LEDGER_PATH", DEFAULT_PATH)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_calls (
    call_id TEXT PRIMARY KEY, idempotency_key TEXT, worker TEXT NOT NULL,
    path TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
    status TEXT NOT NULL, failure_reason TEXT, failure_stage TEXT,
    price_micros INTEGER NOT NULL, provider_cost_micros INTEGER,
    cost_measured INTEGER NOT NULL DEFAULT 0, provider_used TEXT,
    providers_tried TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER, payer TEXT, tx_hash TEXT,
    settled INTEGER NOT NULL DEFAULT 0, rail TEXT
);
CREATE INDEX IF NOT EXISTS idx_wc_worker ON worker_calls(worker);
CREATE INDEX IF NOT EXISTS idx_wc_started ON worker_calls(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wc_idem
    ON worker_calls(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS worker_provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL,
    provider TEXT NOT NULL, attempt INTEGER NOT NULL, started_at REAL NOT NULL,
    latency_ms INTEGER, ok INTEGER NOT NULL, failure_reason TEXT,
    cost_micros INTEGER, cost_measured INTEGER NOT NULL DEFAULT 0, usage TEXT
);
CREATE INDEX IF NOT EXISTS idx_wpc_call ON worker_provider_calls(call_id);

CREATE TABLE IF NOT EXISTS worker_results (
    idempotency_key TEXT PRIMARY KEY, call_id TEXT NOT NULL,
    worker TEXT NOT NULL, stored_at REAL NOT NULL, state TEXT NOT NULL,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS monitor_snapshots (
    url TEXT PRIMARY KEY, content_hash TEXT NOT NULL, text TEXT,
    title TEXT, saved_at REAL NOT NULL
);
"""


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS
# cannot add them to a database that already exists (the box has one), so
# they are added here, once, when missing. Each is nullable: old rows simply
# have no hash, and the receipt says so rather than inventing one.
_RECEIPT_COLUMNS = (
    ("request_hash", "TEXT"),   # sha256 of the canonical request body
    ("result_hash", "TEXT"),    # sha256 of the canonical delivered result
    ("network", "TEXT"),        # CAIP-2 network the payment settled on
    ("asset", "TEXT"),          # asset contract the payment was made in
    ("pay_to", "TEXT"),         # wallet the payment was made to
    ("amount_atomic", "INTEGER"),  # exact settled amount in the asset's atomic units
    ("node_version", "TEXT"),   # SERVICE_VERSION of the node that ran the job
)


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(worker_calls)")}
    for name, kind in _RECEIPT_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE worker_calls ADD COLUMN {name} {kind}")
    conn.commit()


def canonical_hash(value) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace), prefixed with
    the algorithm so a verifier knows exactly what to recompute from the
    body it received."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _connect() -> Optional[sqlite3.Connection]:
    """Open the ledger once, degrading to in-memory if the path is unwritable.

    In-memory rather than off: a node with a read-only volume still answers
    "what did this instance do", and health reports the degradation. Recording
    nothing silently is what would let us believe a capability is profitable
    because no cost was ever logged.
    """
    global _conn, _configured_path, _last_error, _degraded
    target = _path()
    if _conn is not None and _configured_path == target:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    for candidate, degraded in ((target, False), (":memory:", True)):
        try:
            if candidate != ":memory:":
                Path(candidate).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(candidate, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            if candidate != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
            _conn, _configured_path, _degraded = conn, target, degraded
            _last_error = f"ledger degraded to in-memory: {target} unwritable" if degraded else None
            if degraded:
                log.warning("worker ledger degraded to in-memory (%s unwritable)", target)
            return _conn
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {exc}"
            continue
    return None


def _safe_connect() -> Optional[sqlite3.Connection]:
    """_connect(), guaranteed not to raise.

    _connect already handles a failing database by degrading to in-memory, but
    the guarantee this module makes is absolute: nothing in here may raise into
    the request path of a call the caller has already paid for. So the
    connection attempt itself is wrapped too, and a catastrophic failure
    (a broken sqlite3, an unreadable environment) costs us the row and nothing
    else. Every public function goes through this rather than _connect.
    """
    try:
        return _connect()
    except Exception as exc:  # pragma: no cover - defensive
        _note(exc)
        return None


def reset_for_tests() -> None:
    global _conn, _configured_path, _last_error, _degraded
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _configured_path = _last_error = None
        _degraded = False


def status() -> dict:
    with _lock:
        conn = _safe_connect()
        return {"available": conn is not None, "degraded": _degraded,
                "path": _path(), "last_error": _last_error}


def usd_to_micros(amount_usd: float) -> int:
    return int(round(float(amount_usd) * _MICROS))


def micros_to_usd(micros: Optional[int]) -> Optional[float]:
    return None if micros is None else round(micros / _MICROS, 6)


def _note(exc: Exception) -> None:
    global _last_error
    _last_error = f"{type(exc).__name__}: {exc}"
    log.warning("worker ledger write failed: %s", _last_error)


def open_call(call_id, worker, path, price_usd, idempotency_key=None,
              payer=None, rail=None, request_hash=None, node_version=None) -> None:
    """Written BEFORE the provider runs, so a crash leaves evidence."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO worker_calls (call_id, idempotency_key, worker, "
                "path, started_at, status, price_micros, payer, rail, request_hash, "
                "node_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, idempotency_key, worker, path, time.time(), "running",
                 usd_to_micros(price_usd), payer, rail, request_hash, node_version))
            conn.commit()
        except Exception as exc:
            _note(exc)


def record_provider_call(call_id, provider, attempt, started_at, ok,
                         latency_ms=None, failure_reason=None, cost_micros=None,
                         cost_measured=False, usage=None) -> None:
    """One row per provider ATTEMPT, including failures -- they cost latency
    the caller paid for, and at some providers they cost money too."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT INTO worker_provider_calls (call_id, provider, attempt, started_at, "
                "latency_ms, ok, failure_reason, cost_micros, cost_measured, usage) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (call_id, provider, attempt, started_at, latency_ms, 1 if ok else 0,
                 failure_reason, cost_micros, 1 if cost_measured else 0, usage))
            conn.commit()
        except Exception as exc:
            _note(exc)


def close_call(call_id, status_value, latency_ms=None, provider_used=None,
               providers_tried=None, attempts=0, failure_reason=None,
               failure_stage=None, settled=False, tx_hash=None, payer=None,
               result_hash=None, network=None, asset=None, pay_to=None,
               amount_atomic=None) -> None:
    """Finish the row, summing attempt costs.

    `cost_measured` is 1 only when EVERY attempt reported a measured cost; one
    unmeasured attempt makes the total a lower bound, and the margin report
    excludes the call rather than understating what we spent.
    """
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_micros),0) AS total, COUNT(*) AS n, "
                "SUM(CASE WHEN cost_measured=1 THEN 1 ELSE 0 END) AS measured "
                "FROM worker_provider_calls WHERE call_id=?", (call_id,)).fetchone()
            total, n, measured = (row["total"], row["n"], row["measured"]) if row else (0, 0, 0)
            conn.execute(
                "UPDATE worker_calls SET finished_at=?, status=?, latency_ms=?, provider_used=?, "
                "providers_tried=?, attempts=?, failure_reason=?, failure_stage=?, settled=?, "
                "tx_hash=?, provider_cost_micros=?, cost_measured=?, payer=COALESCE(?,payer), "
                "result_hash=COALESCE(?,result_hash), network=COALESCE(?,network), "
                "asset=COALESCE(?,asset), pay_to=COALESCE(?,pay_to), "
                "amount_atomic=COALESCE(?,amount_atomic) WHERE call_id=?",
                (time.time(), status_value, latency_ms, provider_used, providers_tried,
                 attempts, failure_reason, failure_stage, 1 if settled else 0, tx_hash,
                 total, 1 if (n > 0 and measured == n) else 0, payer,
                 result_hash, network, asset, pay_to, amount_atomic, call_id))
            conn.commit()
        except Exception as exc:
            _note(exc)


# --- receipts ---------------------------------------------------------------
#
# A receipt is the worker_calls row, read back in a fixed machine-readable
# shape. It asserts nothing the row does not record: `paid` is the settled
# flag, `delivered` is the status, and the outcome is derived from the two,
# so a settled payment whose job did not deliver can never read as delivered.

RECEIPT_PREFIX = "rcpt_"


def receipt_id_for(call_id: str) -> str:
    return RECEIPT_PREFIX + call_id


def call_id_for(receipt_id: str) -> Optional[str]:
    if not receipt_id or not receipt_id.startswith(RECEIPT_PREFIX):
        return None
    call_id = receipt_id[len(RECEIPT_PREFIX):]
    return call_id if call_id.isalnum() else None


def get_call(call_id: str) -> Optional[dict]:
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM worker_calls WHERE call_id=?", (call_id,)).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            _note(exc)
            return None


def _iso(ts) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def outcome_of(row: dict) -> str:
    paid = bool(row.get("settled"))
    status = row.get("status")
    if paid:
        return "paid_delivered" if status == "ok" else "paid_failed"
    if status == "ok":
        # Delivered on a credential rail (API key, subscription): no x402
        # settlement to point at, which is different from "not paid for".
        return "delivered_not_settled"
    if status == "refused":
        return "unpaid_refused"
    if status == "running":
        return "running"
    return "unpaid_failed"


def receipt_for(call_id: str) -> Optional[dict]:
    row = get_call(call_id)
    if row is None:
        return None
    outcome = outcome_of(row)
    paid = bool(row.get("settled"))
    delivered = row.get("status") == "ok"
    return {
        "receipt_version": 1,
        "receipt_id": receipt_id_for(call_id),
        "request_id": call_id,
        "outcome": outcome,
        "paid": paid,
        "delivered": delivered,
        "payment": {
            "rail": row.get("rail"),
            "payer": row.get("payer"),
            "pay_to": row.get("pay_to"),
            # The settled amount exactly as the rail reported it; older rows
            # (before this column) fall back to the catalog price in micros,
            # which for USDC (6 decimals) is the same number.
            "amount_atomic": (row.get("amount_atomic") or row.get("price_micros")) if paid else None,
            "amount_usd": micros_to_usd(row.get("price_micros")) if paid else None,
            "asset": row.get("asset"),
            "network": row.get("network"),
            "tx_hash": row.get("tx_hash"),
            "settled": paid,
        },
        "request": {
            "worker": row.get("worker"),
            "path": row.get("path"),
            "price_usd": micros_to_usd(row.get("price_micros")),
            "request_hash": row.get("request_hash"),
        },
        "execution": {
            "status": row.get("status"),
            "failure_reason": row.get("failure_reason"),
            "failure_stage": row.get("failure_stage"),
            "provider_used": row.get("provider_used"),
            "attempts": row.get("attempts"),
            "latency_ms": row.get("latency_ms"),
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(row.get("finished_at")),
        },
        "delivery": {
            "delivered": delivered,
            "result_hash": row.get("result_hash") if delivered else None,
        },
        "node_version": row.get("node_version"),
        "timestamp": _iso(row.get("finished_at") or row.get("started_at")),
        "hash_alg": "sha256 over canonical JSON (sort_keys, separators (',', ':'), utf-8)",
    }


# --- idempotency ------------------------------------------------------------
#
# The payment rail already refuses a REPLAYED x402 signature (one nonce, one
# use). What it cannot see is a caller who timed out, signed a SECOND valid
# authorization, and re-sent the same job -- two valid payments for one piece
# of work. The fix is a caller-controlled key plus the verify/settle split
# x402 already gives us: a duplicate returns the stored result and NEVER
# calls _bill, so the second payment is verified (committed, nothing moved)
# and then dropped. No refund path is needed because no settlement happens.

def claim_idempotency(key: str, call_id: str, worker: str) -> tuple:
    """("claimed"|"in_progress"|"done"|"unavailable", result_json_or_None)."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return ("unavailable", None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, result_json FROM worker_results WHERE idempotency_key=?",
                (key,)).fetchone()
            if row is not None:
                state = row["state"]
                conn.commit()
                return ("done", row["result_json"]) if state == "done" else ("in_progress", None)
            conn.execute(
                "INSERT INTO worker_results (idempotency_key, call_id, worker, stored_at, state) "
                "VALUES (?,?,?,?,?)", (key, call_id, worker, time.time(), "running"))
            conn.commit()
            return ("claimed", None)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _note(exc)
            return ("unavailable", None)


def complete_idempotency(key: str, result_json: str) -> None:
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            conn.execute("UPDATE worker_results SET state='done', result_json=?, stored_at=? "
                         "WHERE idempotency_key=?", (result_json, time.time(), key))
            conn.commit()
        except Exception as exc:
            _note(exc)


def release_idempotency(key: str) -> None:
    """Drop the claim for a job that FAILED: it was not billed, so the caller
    is entitled to retry the same key. Leaving it would turn one provider
    outage into a permanently poisoned request key."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            conn.execute("DELETE FROM worker_results WHERE idempotency_key=?", (key,))
            conn.commit()
        except Exception as exc:
            _note(exc)


# --- reporting --------------------------------------------------------------

def summary(since_seconds: Optional[float] = None) -> list:
    """Per-worker commercial summary. Margin reported ONLY for calls whose
    provider cost was fully measured; the rest are counted as unmeasured."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return []
        try:
            where, params = ("WHERE started_at >= ?", (time.time() - since_seconds,)) \
                if since_seconds else ("", ())
            rows = conn.execute(f"""
                SELECT worker, COUNT(*) AS calls,
                  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
                  SUM(CASE WHEN settled=1 THEN price_micros ELSE 0 END) AS revenue_micros,
                  SUM(CASE WHEN settled=1 AND cost_measured=1 THEN provider_cost_micros ELSE 0 END) AS mc,
                  SUM(CASE WHEN settled=1 AND cost_measured=1 THEN price_micros ELSE 0 END) AS mr,
                  SUM(CASE WHEN settled=1 AND cost_measured=0 THEN 1 ELSE 0 END) AS unmeasured_calls,
                  COUNT(DISTINCT payer) AS payers,
                  CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms
                FROM worker_calls {where} GROUP BY worker
                ORDER BY revenue_micros DESC, calls DESC""", params).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                mr, mc = d.pop("mr") or 0, d.pop("mc") or 0
                d["revenue_usd"] = micros_to_usd(d.pop("revenue_micros") or 0)
                d["measured_revenue_usd"] = micros_to_usd(mr)
                d["measured_provider_cost_usd"] = micros_to_usd(mc)
                d["measured_gross_profit_usd"] = micros_to_usd(mr - mc) if mr > 0 else None
                d["measured_gross_margin_pct"] = round(100.0 * (mr - mc) / mr, 2) if mr > 0 else None
                calls = d.get("calls") or 0
                d["success_rate_pct"] = round(100.0 * (d.get("ok") or 0) / calls, 2) if calls else None
                out.append(d)
            return out
        except Exception as exc:
            _note(exc)
            return []


def repeat_payers(since_seconds: Optional[float] = None) -> list:
    """Payers who bought more than once -- the recurring-demand signal nothing
    else in this system can see (the wallet shows money, not who returned)."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return []
        try:
            where = "WHERE payer IS NOT NULL AND settled=1"
            params: tuple = ()
            if since_seconds:
                where += " AND started_at >= ?"
                params = (time.time() - since_seconds,)
            rows = conn.execute(f"""
                SELECT payer, COUNT(*) AS paid_calls, COUNT(DISTINCT worker) AS distinct_workers,
                  SUM(price_micros) AS spend_micros, MIN(started_at) AS first_seen,
                  MAX(started_at) AS last_seen
                FROM worker_calls {where} GROUP BY payer HAVING paid_calls > 1
                ORDER BY spend_micros DESC""", params).fetchall()
            return [{**dict(r), "spend_usd": micros_to_usd(r["spend_micros"])} for r in rows]
        except Exception as exc:
            _note(exc)
            return []


# --- monitor.snapshot / monitor.check state ---------------------------------
#
# The only piece of state in this service that OUTLIVES a single call: two
# separate paid purchases (snapshot, then check) share one row per URL so
# the second purchase can say what changed since the first.

def save_monitor_snapshot(url: str, content_hash: str, text: str,
                          title: Optional[str]) -> None:
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT INTO monitor_snapshots (url, content_hash, text, title, saved_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "content_hash=excluded.content_hash, text=excluded.text, "
                "title=excluded.title, saved_at=excluded.saved_at",
                (url, content_hash, text, title, time.time()))
            conn.commit()
        except Exception as exc:
            _note(exc)


def get_monitor_snapshot(url: str) -> Optional[dict]:
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT content_hash, text, title, saved_at FROM monitor_snapshots "
                "WHERE url=?", (url,)).fetchone()
            if row is None:
                return None
            return {"content_hash": row["content_hash"], "text": row["text"] or "",
                    "title": row["title"],
                    "age_seconds": round(time.time() - row["saved_at"], 1)}
        except Exception as exc:
            _note(exc)
            return None


def provider_health(since_seconds: Optional[float] = None) -> list:
    """Measured per-provider reliability -- the only honest basis for any
    success-rate claim."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return []
        try:
            where, params = ("WHERE started_at >= ?", (time.time() - since_seconds,)) \
                if since_seconds else ("", ())
            rows = conn.execute(f"""
                SELECT provider, COUNT(*) AS attempts, SUM(ok) AS ok,
                  CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms,
                  SUM(COALESCE(cost_micros,0)) AS cost_micros
                FROM worker_provider_calls {where} GROUP BY provider
                ORDER BY attempts DESC""", params).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                a = d["attempts"] or 0
                d["success_rate_pct"] = round(100.0 * (d["ok"] or 0) / a, 2) if a else None
                d["cost_usd"] = micros_to_usd(d.pop("cost_micros") or 0)
                out.append(d)
            return out
        except Exception as exc:
            _note(exc)
            return []
