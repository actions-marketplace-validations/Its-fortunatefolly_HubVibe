"""A SQLite key store wearing the slice of Firestore's API that billing.py uses.

Why this exists: every per-call rail (x402, MPP) settles without touching
Google, but the API-key side -- subscriber keys, prepaid balances bought
through the MPP top-up, monthly quotas, one-off reports -- lived in
Firestore, which made "deploy anywhere but Cloud Run" quietly impossible:
the paid rails would run and the key the top-up just sold could not be
written. On a single-instance deployment (a VPS behind Caddy), SQLite is
the correct store: one file, transactional, zero standing cost, and no
third party that can suspend it.

Deliberately a Firestore *shim* rather than a second storage interface:
billing.py keeps one code path, and this module implements exactly the
calls it makes -- collection().document().get/set,
snapshot .exists/.to_dict()/.get(field), and transactions with
update / set(merge=True). Nothing more is implemented, so a new Firestore
call in billing.py fails loudly here instead of silently diverging.

Concurrency: every transaction runs under BEGIN IMMEDIATE, which takes the
write lock up front -- two concurrent debits of the same prepaid key
serialize, so both can never read the same balance and both succeed. That
is the same guarantee Firestore's transactions give, and on ONE instance it
is stronger than the multi-instance Cloud Run deployment had. WAL mode so
reads never block behind a writer; busy_timeout so a briefly-held lock is
waited out rather than surfaced as an error (which billing.py would
correctly fail closed on, refusing a caller who has already paid).

Documents are JSON rows keyed by "collection/doc_id". Field values here are
strings, numbers, bools and None -- the shapes billing.py writes -- so JSON
round-trips them exactly.
"""

import json
import os
import sqlite3
from typing import Optional


class Snapshot:
    """What a read returns; mirrors google.cloud.firestore DocumentSnapshot."""

    __slots__ = ("_data",)

    def __init__(self, data: Optional[dict]):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return dict(self._data) if self._data is not None else None

    def get(self, field: str):
        # Firestore's DocumentSnapshot.get raises KeyError on a missing
        # field; matching that keeps billing.py's error handling identical
        # across backends instead of turning a missing field into None and
        # a TypeError three lines later.
        if self._data is None or field not in self._data:
            raise KeyError(field)
        return self._data[field]


class DocumentReference:
    __slots__ = ("_store", "path")

    def __init__(self, store: "SqliteKeyStore", path: str):
        self._store = store
        self.path = path

    def get(self, transaction: Optional["Transaction"] = None) -> Snapshot:
        return self._store._read(self.path, transaction)

    def set(self, data: dict, merge: bool = False) -> None:
        self._store._write(self.path, data, merge=merge, transaction=None)


class CollectionReference:
    __slots__ = ("_store", "_name")

    def __init__(self, store: "SqliteKeyStore", name: str):
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> DocumentReference:
        return DocumentReference(self._store, f"{self._name}/{doc_id}")


class Transaction:
    """Write handle bound to one BEGIN IMMEDIATE connection."""

    __slots__ = ("_store", "_conn")

    def __init__(self, store: "SqliteKeyStore", conn: sqlite3.Connection):
        self._store = store
        self._conn = conn

    def update(self, ref: DocumentReference, fields: dict) -> None:
        self._store._write(ref.path, fields, merge=True, transaction=self)

    def set(self, ref: DocumentReference, data: dict, merge: bool = False) -> None:
        self._store._write(ref.path, data, merge=merge, transaction=self)


class SqliteKeyStore:
    def __init__(self, path: str):
        self._path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS documents ("
                " path TEXT PRIMARY KEY,"
                " data TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # --- the Firestore surface billing.py calls -----------------------------

    def collection(self, name: str) -> CollectionReference:
        return CollectionReference(self, name)

    def run_in_transaction(self, fn):
        """Run fn(transaction) atomically; billing._run_transactional calls
        this when the store is SQLite (Firestore's own decorator otherwise).

        BEGIN IMMEDIATE, not DEFERRED: the write lock is taken before fn
        reads, so a concurrent transaction on the same document waits here
        rather than both reading the same prepaid balance and both spending
        it -- the double-spend this method exists to make impossible.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(Transaction(self, conn))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- storage ------------------------------------------------------------

    def _read(self, path: str, transaction: Optional[Transaction]) -> Snapshot:
        if transaction is not None:
            row = transaction._conn.execute(
                "SELECT data FROM documents WHERE path = ?", (path,)
            ).fetchone()
        else:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT data FROM documents WHERE path = ?", (path,)
                ).fetchone()
        return Snapshot(json.loads(row[0]) if row else None)

    def _write(self, path: str, data: dict, *, merge: bool, transaction: Optional[Transaction]) -> None:
        def _apply(conn: sqlite3.Connection) -> None:
            if merge:
                row = conn.execute(
                    "SELECT data FROM documents WHERE path = ?", (path,)
                ).fetchone()
                current = json.loads(row[0]) if row else {}
                current.update(data)
                payload = current
            else:
                payload = dict(data)
            conn.execute(
                "INSERT INTO documents (path, data) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET data = excluded.data",
                (path, json.dumps(payload)),
            )

        if transaction is not None:
            _apply(transaction._conn)
        else:
            with self._connect() as conn:
                _apply(conn)
