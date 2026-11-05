"""Artifact memory: the general agent's cross-task fact layer.

Case memory answers "what did we do"; this answers "what exists and who
produced it". For the general domain a **fact is an artifact**: a file with a
path, a content hash, the signed run that produced it and the round. That is
harder than a model-extracted semantic note — it can be re-verified against
the workspace at any time — and it needs no extraction model at all.

Design mirrors the case book: a deterministic projection of signed traces,
one SQLite file, rebuildable, no third-party dependency, forgetting only
lowers rank (an archived run's artifacts stay queryable, labelled archived).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["ArtifactRecord", "ArtifactBook", "project_artifacts", "default_artifact_db_path"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    produced_by TEXT NOT NULL,
    chain_head TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_sha ON artifacts(sha256);
"""

#: Tools whose success records mean "this file now exists with this hash".
PRODUCING_TOOLS = ("write_file", "edit_file")


def default_artifact_db_path() -> Path:
    override = os.environ.get("FCODE_ARTIFACT_DB")
    if override:
        return Path(override)
    import sys

    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "FCode"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fcode"
    base.mkdir(parents=True, exist_ok=True)
    return base / "artifacts.sqlite"


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    round: int
    path: str
    sha256: str
    bytes: int
    produced_by: str
    chain_head: str
    created_at: str
    archived: bool = False


def project_artifacts(trace: dict[str, Any], *, run_id: str) -> list[ArtifactRecord]:
    """Derive the artifact set from a trace. Pure function of the signed log.

    A later write to the same path supersedes earlier rows *in the projection*
    (the last write wins for "what exists"), but the store keeps every row —
    history is never overwritten, matching the fact-book's bitemporal stance.
    """
    task = trace.get("task") or {}
    chain = trace.get("chain") or {}
    chain_head = str(chain.get("head") or "")
    created_at = str(task.get("created_at") or datetime.now(timezone.utc).isoformat())

    rows: list[ArtifactRecord] = []
    for record in trace.get("rounds") or []:
        observation = record.get("observation") or {}
        tool = observation.get("tool") or ""
        if tool not in PRODUCING_TOOLS:
            continue
        result = observation.get("result") or {}
        if result.get("changed") is False:
            continue
        sha = result.get("sha256_after") or ""
        path = result.get("path") or ""
        if not sha or not path:
            continue
        rows.append(ArtifactRecord(
            artifact_id=f"{run_id}:{record.get('index', 0)}",
            run_id=run_id,
            round=int(record.get("index") or 0),
            path=str(path),
            sha256=str(sha),
            bytes=int(result.get("bytes") or 0),
            produced_by=tool,
            chain_head=chain_head,
            created_at=created_at,
        ))
    return rows


class ArtifactBook:
    """SQLite store for artifact records; rebuildable from traces.

    One short-lived connection per operation, exactly like the case and fact
    books: the console ingests from a run's worker thread while the HTTP
    threads read, and a connection created in ``__init__`` belongs to whichever
    thread built it (sqlite refuses it elsewhere — that is not a theoretical
    concern: it turned a finished run into a stuck one).
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------------- writing

    def record(self, artifacts: list[ArtifactRecord]) -> int:
        with self._connect() as conn:
            for artifact in artifacts:
                conn.execute(
                    "INSERT OR REPLACE INTO artifacts"
                    " (artifact_id, run_id, round, path, sha256, bytes, produced_by, chain_head, created_at, archived)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        artifact.artifact_id, artifact.run_id, artifact.round,
                        artifact.path, artifact.sha256, artifact.bytes,
                        artifact.produced_by, artifact.chain_head,
                        artifact.created_at, 1 if artifact.archived else 0,
                    ),
                )
        return len(artifacts)

    def ingest_trace(self, trace: dict[str, Any], *, run_id: str) -> int:
        return self.record(project_artifacts(trace, run_id=run_id))

    def archive_run(self, run_id: str) -> int:
        """Tombstone a run's artifacts; rows stay queryable, labelled archived."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE artifacts SET archived = 1 WHERE run_id = ?", (run_id,)
            )
        return cursor.rowcount

    # ---------------------------------------------------------------- reading

    def by_path(self, path: str) -> list[ArtifactRecord]:
        """Every known version of one workspace path, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE path = ? ORDER BY round DESC, created_at DESC",
                (path,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def by_sha(self, sha256: str) -> list[ArtifactRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE sha256 = ? ORDER BY created_at DESC",
                (sha256,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def latest(self, path: str) -> ArtifactRecord | None:
        rows = self.by_path(path)
        return rows[0] if rows else None

    def search(self, contains: str, limit: int = 20) -> list[ArtifactRecord]:
        """Substring match on path — explainable, like the case book's retrieval."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE path LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{contains}%", limit),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def count(self, *, archived: bool | None = None) -> int:
        with self._connect() as conn:
            if archived is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM artifacts WHERE archived = ?",
                    (1 if archived else 0,),
                ).fetchone()
        return int(row["n"] or 0)

    def close(self) -> None:
        """Nothing to release: every operation closes its own connection."""
        return None

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            round=int(row["round"]),
            path=row["path"],
            sha256=row["sha256"],
            bytes=int(row["bytes"]),
            produced_by=row["produced_by"],
            chain_head=row["chain_head"],
            created_at=row["created_at"],
            archived=bool(row["archived"]),
        )
