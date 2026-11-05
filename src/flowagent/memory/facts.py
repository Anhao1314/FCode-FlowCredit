"""Bi-temporal fact memory: corrections append, nothing is overwritten.

Case memory answers "what did we do?"; fact memory answers "what did we
believe, when, and on whose authority?". A fact row stores two timelines:

* **valid time** — the fiscal period the number describes;
* **transaction time** — the moment a signed observation recorded it.

A correction inserts a new row and links it to the row it replaces. The old
row stays readable, so ``known_at`` can reproduce yesterday's answer even after
today's restatement. This is the part a mutable "latest value" table gets
wrong: overwriting destroys the only evidence that the belief ever changed.

Extraction is deterministic and lives with the domain
(:mod:`flowagent.tools.finance.facts`). This module only stores and queries —
it never asks a model to decide what a fact is.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# Fact identity/construction is pure and lives in core so the domain extractor
# can build rows without importing memory. Re-exported for existing callers.
from ..core.facts import fact_dates, logical_key, make_fact

__all__ = ["FactStore", "fact_dates", "logical_key", "make_fact"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    logical_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    period TEXT NOT NULL,
    value_int INTEGER,
    value_text TEXT,
    unit TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_run TEXT NOT NULL,
    source_event INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    source_result_hash TEXT NOT NULL,
    supersedes TEXT,
    superseded_by TEXT,
    extractor_version TEXT NOT NULL,
    entities TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(logical_key);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject, predicate, period);
CREATE INDEX IF NOT EXISTS idx_facts_run ON facts(source_run);
"""


class FactStore:
    """SQLite-backed assertions. The schema is created lazily on first use."""

    def __init__(self, path: Path | None = None) -> None:
        from .cases import default_db_path

        self.path = Path(path) if path is not None else default_db_path()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
            self._ready = True
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ write

    def upsert_run(self, *, run_id: str, facts: list[dict[str, Any]]) -> int:
        """Append the assertions a finished run produced. Returns rows inserted.

        Facts are supplied by the domain extractor. Identical values are
        consolidated; a different value supersedes the current row for the same
        logical key without touching it.
        """
        inserted = 0
        with self._connect() as conn:
            for fact in sorted(facts, key=lambda item: (item["source_event"], item["predicate"])):
                existing = conn.execute(
                    "SELECT * FROM facts WHERE fact_id=?", (fact["fact_id"],)
                ).fetchone()
                if existing is not None:
                    continue
                key = logical_key(fact["subject"], fact["predicate"], fact["period"])
                latest = conn.execute(
                    "SELECT * FROM facts WHERE logical_key=? AND archived=0"
                    " ORDER BY recorded_at DESC, source_event DESC, fact_id DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if latest is not None and (
                    latest["value_int"] == fact["value_int"]
                    and latest["value_text"] == fact["value_text"]
                    and latest["unit"] == fact["unit"]
                ):
                    # The same claim observed again is not a correction. The
                    # observation stays in its trace; the fact book stays clean.
                    continue
                supersedes = latest["fact_id"] if latest is not None else None
                if supersedes is not None:
                    conn.execute(
                        "UPDATE facts SET superseded_by=? WHERE fact_id=? AND superseded_by IS NULL",
                        (fact["fact_id"], supersedes),
                    )
                conn.execute(
                    """INSERT INTO facts
                    (fact_id, logical_key, subject, predicate, period, value_int, value_text,
                     unit, valid_from, valid_to, recorded_at, source_run, source_event,
                     source_hash, source_result_hash, supersedes, superseded_by,
                     extractor_version, entities, archived)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        fact["fact_id"], key, fact["subject"], fact["predicate"], fact["period"],
                        fact["value_int"], fact["value_text"], fact["unit"],
                        fact["valid_from"], fact["valid_to"], fact["recorded_at"],
                        run_id, fact["source_event"], fact["source_hash"],
                        fact["source_result_hash"], supersedes, None,
                        fact["extractor_version"],
                        json.dumps(fact["entities"], ensure_ascii=False),
                    ),
                )
                inserted += 1
        return inserted

    def tombstone_run(self, run_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE facts SET archived=1 WHERE source_run=?", (run_id,))
            return cursor.rowcount

    # ------------------------------------------------------------------- read

    def query(
        self,
        *,
        subject: str = "",
        predicate: str = "",
        period: str = "",
        as_of: str = "",
        known_at: str = "",
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return facts valid at ``as_of`` and recorded by ``known_at``.

        By default only the latest assertion per logical key is returned.
        ``include_superseded`` returns the correction history instead, which is
        what an audit view wants.
        """
        clauses = ["archived=0"]
        values: list[Any] = []
        for column, value in (("subject", subject), ("predicate", predicate), ("period", period)):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if known_at:
            clauses.append("recorded_at <= ?")
            values.append(known_at)
        if as_of:
            clauses.append("(valid_from='' OR valid_from <= ?)")
            values.append(as_of)
            clauses.append("(valid_to='' OR valid_to >= ?)")
            values.append(as_of)
        with self._connect() as conn:
            raw = list(conn.execute(
                "SELECT * FROM facts WHERE " + " AND ".join(clauses) +
                " ORDER BY recorded_at ASC, source_event ASC, fact_id ASC",
                values,
            ).fetchall())
        rows = [self._as_dict(row) for row in raw]
        if include_superseded:
            return rows[: max(0, int(limit))]
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[logical_key(row["subject"], row["predicate"], row["period"])] = row
        current = list(latest.values())
        current.sort(key=lambda row: (row["subject"], row["predicate"], row["period"]))
        return current[: max(0, int(limit))]

    def history(self, *, subject: str, predicate: str, period: str) -> list[dict[str, Any]]:
        """Every assertion for one logical key, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE subject=? AND predicate=? AND period=?"
                " AND archived=0 ORDER BY recorded_at ASC, source_event ASC, fact_id ASC",
                (subject, predicate, period),
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def count(self, *, include_archived: bool = False) -> int:
        query = "SELECT COUNT(*) AS n FROM facts" + ("" if include_archived else " WHERE archived=0")
        with self._connect() as conn:
            return int(conn.execute(query).fetchone()["n"])

    @staticmethod
    def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "fact_id": row["fact_id"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "period": row["period"],
            "value_int": row["value_int"],
            "value_text": row["value_text"],
            "unit": row["unit"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "recorded_at": row["recorded_at"],
            "source_run": row["source_run"],
            "source_event": row["source_event"],
            "source_hash": row["source_hash"],
            "supersedes": row["supersedes"],
            "superseded_by": row["superseded_by"],
            "extractor_version": row["extractor_version"],
            "entities": json.loads(row["entities"]),
        }


