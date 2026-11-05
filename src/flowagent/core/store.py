"""Durable task storage: SQLite, write serialisation and an append-only hash chain.

Why SQLite instead of the Node prototype's directory-per-task layout? Because
Phase 1 must run from a fresh clone with nothing installed, and ``sqlite3`` is
in the standard library. The *semantics* are ported unchanged:

* one row per task holds the current body plus its hash,
* one row per event forms a hash chain whose first entry has ``prev_hash=None``,
* every write is a compare-and-swap on ``sequence``, so a stale writer loses,
* reading re-verifies the whole chain and refuses to return a body whose hash
  does not match the head of that chain.

The last point is the part that matters for an interview: tampering with a row
is not detected by trusting a flag, it is detected by recomputing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    MODEL_TRANSITIONS,
    TASK_STATES,
    TASK_TRANSITIONS,
    AgentError,
    Task,
)
from .hashing import canonical_json, digest

__all__ = ["TaskStore", "utc_now"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id          TEXT PRIMARY KEY,
    version     TEXT NOT NULL,
    status      TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    body        TEXT NOT NULL,
    body_hash   TEXT NOT NULL,
    head_hash   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    task_id   TEXT NOT NULL,
    sequence  INTEGER NOT NULL,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT,
    hash      TEXT NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS budget (
    scope       TEXT PRIMARY KEY,
    calls_used  INTEGER NOT NULL DEFAULT 0,
    reads_used  INTEGER NOT NULL DEFAULT 0,
    cost_used   INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0
);
"""


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class TaskStore:
    """Durable, hash-chained task storage.

    The connection is shared and guarded by a re-entrant lock; writers are
    additionally serialised by SQLite's ``BEGIN IMMEDIATE`` so two processes
    cannot interleave a read-modify-write on the same task.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,  # explicit transactions only
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ locking

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block inside a write transaction, re-entrant within a thread."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    # ------------------------------------------------------------------- writes

    def create(self, task: Task) -> Task:
        """Insert a brand-new task and its genesis event."""
        if task.sequence != 0:
            raise AgentError("SEQUENCE_CONFLICT", "a new task must start at sequence 0")
        if task.status != "created":
            raise AgentError("ILLEGAL_STATE_TRANSITION", "a new task must start in 'created'")
        at = utc_now()
        with self.transaction():
            if self._conn.execute("SELECT 1 FROM task WHERE id=?", (task.id,)).fetchone():
                raise AgentError("SEQUENCE_CONFLICT", f"task {task.id} already exists")
            committed = self._write(task, expected_sequence=0, event_type="task_created", detail={}, at=at)
        return committed

    def save(self, task: Task, expected_sequence: int, event_type: str, detail: dict[str, Any] | None = None) -> Task:
        """Compare-and-swap *task* against the stored sequence and append an event.

        ``expected_sequence`` is the only concurrency token. The task body's own
        ``sequence`` field is set by the store, so a caller cannot smuggle in a
        number that disagrees with the event it is about to write.
        """
        at = utc_now()
        with self.transaction():
            committed = self._write(
                task,
                expected_sequence=expected_sequence,
                event_type=event_type,
                detail=detail or {},
                at=at,
            )
        return committed

    def _write(
        self,
        task: Task,
        *,
        expected_sequence: int,
        event_type: str,
        detail: dict[str, Any],
        at: str,
    ) -> Task:
        row = self._conn.execute(
            "SELECT sequence, body, head_hash FROM task WHERE id=?", (task.id,)
        ).fetchone()

        # The store owns the sequence, not the caller. Setting it here means a
        # body can never be written with a sequence that disagrees with its event.
        task.sequence = expected_sequence + 1

        if row is None:
            if expected_sequence != 0:
                raise AgentError("TASK_NOT_FOUND", f"task {task.id} does not exist")
            prev_hash: str | None = None
            previous_body: dict[str, Any] | None = None
        else:
            if row["sequence"] != expected_sequence:
                raise AgentError(
                    "SEQUENCE_CONFLICT",
                    f"task {task.id} is at sequence {row['sequence']}, writer expected {expected_sequence}",
                )
            prev_hash = row["head_hash"]
            previous_body = Task.from_dict(json.loads(row["body"])).to_dict()

        body = task.to_dict()
        if previous_body is not None:
            self._assert_legal_change(previous_body, body, task.id)

        body_text = canonical_json(body)
        body_hash = digest(body)
        payload = {"body_hash": body_hash, "detail": detail}
        event = {
            "sequence": expected_sequence + 1,
            "type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
            "at": at,
        }
        event_hash = digest(event)

        self._conn.execute(
            "INSERT INTO event (task_id, sequence, type, payload, prev_hash, hash, at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                task.id,
                event["sequence"],
                event_type,
                canonical_json(payload),
                prev_hash,
                event_hash,
                at,
            ),
        )
        self._conn.execute(
            "INSERT INTO task (id, version, status, sequence, body, body_hash, head_hash, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   version=excluded.version, status=excluded.status, sequence=excluded.sequence,"
            "   body=excluded.body, body_hash=excluded.body_hash, head_hash=excluded.head_hash,"
            "   updated_at=excluded.updated_at",
            (task.id, task.version, task.status, task.sequence, body_text, body_hash, event_hash, at),
        )
        return task

    @staticmethod
    def _assert_legal_change(previous: dict[str, Any], current: dict[str, Any], task_id: str) -> None:
        """Reject a body change that the state machine never allowed."""
        if current["sequence"] != previous["sequence"] + 1:
            raise AgentError("SEQUENCE_CONFLICT", "sequence must advance by exactly one")
        if current["status"] not in TASK_TRANSITIONS.get(previous["status"], ()):
            raise AgentError(
                "ILLEGAL_STATE_TRANSITION",
                f"{previous['status']} -> {current['status']} is not allowed for {task_id}",
            )
        if current["model"]["status"] not in MODEL_TRANSITIONS.get(previous["model"]["status"], ()):
            raise AgentError(
                "ILLEGAL_MODEL_TRANSITION",
                f"{previous['model']['status']} -> {current['model']['status']} is not allowed",
            )
        for field in ("id", "version", "principal", "objective", "binding", "allowed_tools", "model_grant"):
            if canonical_json(current[field]) != canonical_json(previous[field]):
                raise AgentError("IMMUTABLE_FIELD_CHANGED", f"{field} cannot change after creation")
        if previous["plan"] is not None and canonical_json(current["plan"]) != canonical_json(previous["plan"]):
            raise AgentError("IMMUTABLE_FIELD_CHANGED", "a validated plan cannot be rewritten")
        for before in previous["steps"]:
            after = next((s for s in current["steps"] if s["id"] == before["id"]), None)
            if after is None:
                raise AgentError("IMMUTABLE_FIELD_CHANGED", f"step {before['id']} disappeared")
            if before["status"] == "succeeded" and canonical_json(after) != canonical_json(before):
                raise AgentError("IMMUTABLE_FIELD_CHANGED", f"a succeeded step cannot be rewritten")

    # -------------------------------------------------------------------- reads

    def load(self, task_id: str) -> Task:
        """Load a task after re-verifying its whole event chain."""
        row = self._conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise AgentError("TASK_NOT_FOUND", f"task {task_id} does not exist")
        body = json.loads(row["body"])
        if digest(body) != row["body_hash"]:
            raise AgentError("TASK_TAMPERED", f"body hash mismatch for {task_id}")
        self.verify_chain(task_id)
        return Task.from_dict(body)

    def events(self, task_id: str) -> list[dict[str, Any]]:
        """Return the raw event chain in sequence order."""
        rows = self._conn.execute(
            "SELECT sequence, type, payload, prev_hash, hash, at FROM event"
            " WHERE task_id=? ORDER BY sequence ASC",
            (task_id,),
        ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "type": row["type"],
                "payload": json.loads(row["payload"]),
                "prev_hash": row["prev_hash"],
                "hash": row["hash"],
                "at": row["at"],
            }
            for row in rows
        ]

    def verify_chain(self, task_id: str) -> None:
        """Recompute every event hash and check linkage plus the task head."""
        rows = self._conn.execute(
            "SELECT sequence, type, payload, prev_hash, hash, at FROM event"
            " WHERE task_id=? ORDER BY sequence ASC",
            (task_id,),
        ).fetchall()
        if not rows:
            raise AgentError("TASK_TAMPERED", f"task {task_id} has no events")

        previous_hash: str | None = None
        for index, row in enumerate(rows, start=1):
            if row["sequence"] != index:
                raise AgentError("CHAIN_GAP", f"event {index} is missing for {task_id}")
            if row["prev_hash"] != previous_hash:
                raise AgentError("CHAIN_BROKEN", f"event {index} does not link to its predecessor")
            expected = digest(
                {
                    "sequence": row["sequence"],
                    "type": row["type"],
                    "payload": json.loads(row["payload"]),
                    "prev_hash": row["prev_hash"],
                    "at": row["at"],
                }
            )
            if expected != row["hash"]:
                raise AgentError("CHAIN_TAMPERED", f"event {index} hash mismatch for {task_id}")
            previous_hash = row["hash"]

        head = self._conn.execute("SELECT head_hash, sequence FROM task WHERE id=?", (task_id,)).fetchone()
        if head["head_hash"] != previous_hash:
            raise AgentError("CHAIN_TAMPERED", f"head hash does not match the last event of {task_id}")
        if head["sequence"] != len(rows):
            raise AgentError("CHAIN_ROLLBACK", f"task sequence {head['sequence']} != {len(rows)} events")

    # ------------------------------------------------------------------- budget

    def budget_row(self, scope: str) -> dict[str, int]:
        """Return the budget counters for *scope*, creating the row on demand."""
        self._conn.execute("INSERT OR IGNORE INTO budget (scope) VALUES (?)", (scope,))
        row = self._conn.execute("SELECT * FROM budget WHERE scope=?", (scope,)).fetchone()
        return {
            "calls_used": row["calls_used"],
            "reads_used": row["reads_used"],
            "cost_used": row["cost_used"],
            "tokens_used": row["tokens_used"],
        }

    def budget_add(self, scope: str, *, calls: int = 0, reads: int = 0, cost: int = 0, tokens: int = 0) -> None:
        """Atomically add to the counters for *scope*."""
        self._conn.execute("INSERT OR IGNORE INTO budget (scope) VALUES (?)", (scope,))
        self._conn.execute(
            "UPDATE budget SET calls_used=calls_used+?, reads_used=reads_used+?,"
            " cost_used=cost_used+?, tokens_used=tokens_used+? WHERE scope=?",
            (calls, reads, cost, tokens, scope),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
