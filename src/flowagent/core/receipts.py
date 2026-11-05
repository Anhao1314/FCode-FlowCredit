"""Signed receipts: the only thing that can resolve an unknown outcome.

When a call is dispatched and the result never comes back, the runtime refuses to
guess. It also refuses to accept the provider's own word: a receipt is only
usable if it is signed with this store's key and matches the intent exactly. That
is why ``load`` verifies before returning, and why a wrong reference produces a
stable error code rather than a silent fallback.

The key lives next to the data, which is honest about the threat model: this
protects against a confused or buggy caller, not against someone who already owns
the database file. The docs say so explicitly rather than implying otherwise.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from typing import Any

from .contracts import AgentError
from .hashing import canonical_json, digest
from .store import TaskStore, utc_now

__all__ = ["ReceiptStore", "RECEIPT_VERSION"]

RECEIPT_VERSION = "flowagent.model-receipt/v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt (
    ref       TEXT PRIMARY KEY,
    payload   TEXT NOT NULL,
    signature TEXT NOT NULL,
    at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ReceiptStore:
    """Issues and verifies HMAC-signed model receipts."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        # executescript commits implicitly, so it must not run inside an explicit
        # BEGIN/COMMIT block; doing so leaves the connection with no transaction
        # to roll back and breaks the next error path.
        store._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------- keys

    def _key(self) -> bytes:
        """Return the store's signing key, creating it once."""
        conn: sqlite3.Connection = self._store._conn
        with self._store.transaction():
            row = conn.execute("SELECT value FROM meta WHERE key='receipt_key'").fetchone()
            if row is None:
                key = secrets.token_bytes(32)
                conn.execute("INSERT INTO meta (key, value) VALUES ('receipt_key', ?)", (key.hex(),))
                return key
            return bytes.fromhex(row["value"])

    def _sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self._key(), canonical_json(payload).encode("utf-8"), "sha256").hexdigest()

    # ---------------------------------------------------------------- issuing

    def issue(self, *, intent: dict[str, Any], plan: dict[str, Any], usage: dict[str, Any]) -> str:
        """Persist a signed receipt and return its reference.

        The reference is derived from the intent and the plan, so the same call
        always produces the same reference. That makes "which receipt belongs to
        this round" a computation rather than a lookup the caller can fudge.
        """
        payload = {
            "version": RECEIPT_VERSION,
            "intent": intent,
            "plan": plan,
            "usage": usage,
            "external_billing": "not-verified",
        }
        ref = digest({"intent": intent, "plan": plan})
        with self._store.transaction():
            self._store._conn.execute(
                "INSERT OR REPLACE INTO receipt (ref, payload, signature, at) VALUES (?,?,?,?)",
                (ref, canonical_json(payload), self._sign(payload), utc_now()),
            )
        return ref

    def expected_ref(self, intent: dict[str, Any], plan: dict[str, Any]) -> str:
        """Compute the reference a matching receipt would have."""
        return digest({"intent": intent, "plan": plan})

    # --------------------------------------------------------------- verifying

    def load(self, ref: str | None) -> dict[str, Any]:
        """Return a verified receipt, or raise.

        A missing reference is ``MODEL_RECEIPT_REQUIRED``; a bad signature or a
        mismatched reference is ``MODEL_RECEIPT_UNTRUSTED``. Neither is
        recoverable by guessing.
        """
        if not isinstance(ref, str) or not ref:
            raise AgentError("MODEL_RECEIPT_REQUIRED", "reconciliation needs a receipt reference")
        row = self._store._conn.execute("SELECT payload, signature FROM receipt WHERE ref=?", (ref,)).fetchone()
        if row is None:
            raise AgentError("MODEL_RECEIPT_REQUIRED", f"no receipt stored for {ref[:12]}…")
        payload = json.loads(row["payload"])
        expected = self._sign(payload)
        if not hmac.compare_digest(expected, row["signature"]):
            raise AgentError("MODEL_RECEIPT_UNTRUSTED", "receipt signature does not match this store")
        return payload

    def count(self) -> int:
        return int(self._store._conn.execute("SELECT COUNT(*) AS n FROM receipt").fetchone()["n"])
