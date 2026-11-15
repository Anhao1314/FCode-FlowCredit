"""Persistent Work projects: the container a run belongs to.

Before this store, a "project" was a browser-local label (``fa.projects`` in
localStorage) with no path, no verification command and no server-side
identity. A Codex-style Work surface needs the opposite: a durable project the
server knows, so a run can be bound to it, listed under it, and audited with it.

The store is deliberately small. It owns identity (id), the absolute directory
path, an optional verification command, and archive state. Run statistics are
composed by :class:`viewer.console_manager.RunManager` from the runs themselves
— the project row never becomes a second source of truth about a run.

Archiving is the only deletion. A run's ``projectId`` is part of its history;
physically deleting the container would orphan that history.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["ProjectStore", "normalize_project_path"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    verification TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
"""


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_project_path(raw: Any) -> str:
    """Validate an operator-supplied directory and return the canonical path.

    Reuses the same validator the delegation spec uses, so a project and a
    hand-written ``spec.project.path`` cannot disagree about what "absolute"
    means. The directory must exist: a Work project whose path is a typo is a
    dead container, and failing at creation is cheaper than failing a run.
    """
    from flowagent.tools.general.project import validate_project_spec

    try:
        path = validate_project_spec({"path": raw})["path"]
    except ValueError as error:
        raise ValueError(str(error)) from error
    if not os.path.isdir(path):
        raise ValueError(f"项目目录不存在：{path}")
    return path


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("项目名称不能为空")
    if len(name) > 80:
        raise ValueError("项目名称最多 80 个字符")
    return name


def _clean_verification(raw: Any) -> str:
    text = str(raw or "").strip()
    if len(text) > 300:
        raise ValueError("验证命令最多 300 个字符")
    return text


class ProjectStore:
    """SQLite-backed project registry beside the runs directory."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ------------------------------------------------------------------ write

    def create(self, name: Any, path: Any, verification: Any = "") -> dict[str, Any]:
        clean_name = _clean_name(name)
        clean_path = normalize_project_path(path)
        clean_verification = _clean_verification(verification)
        with self._lock:
            existing = self.by_path(clean_path)
            if existing is not None:
                raise ValueError(f"该项目目录已注册：{existing['name']}")
            project_id = f"prj-{uuid.uuid4().hex[:8]}"
            self._db.execute(
                "INSERT INTO projects (id, name, path, verification, created, archived)"
                " VALUES (?,?,?,?,?,0)",
                (project_id, clean_name, clean_path, clean_verification, _now_text()),
            )
            self._db.commit()
            return self.get(project_id)  # type: ignore[return-value]

    def update(
        self,
        project_id: str,
        *,
        name: Any = None,
        verification: Any = None,
        archived: Any = None,
    ) -> dict[str, Any]:
        current = self.get(project_id)
        if current is None:
            raise KeyError(project_id)
        next_name = current["name"] if name is None else _clean_name(name)
        next_verification = (
            current["verification"] if verification is None
            else _clean_verification(verification)
        )
        next_archived = (
            int(bool(current["archived"])) if archived is None
            else int(bool(archived))
        )
        with self._lock:
            self._db.execute(
                "UPDATE projects SET name=?, verification=?, archived=? WHERE id=?",
                (next_name, next_verification, next_archived, project_id),
            )
            self._db.commit()
            return self.get(project_id)  # type: ignore[return-value]

    def archive(self, project_id: str) -> dict[str, Any]:
        return self.update(project_id, archived=True)

    # ------------------------------------------------------------------- read

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM projects WHERE id=?", (str(project_id or ""),)
            ).fetchone()
        return dict(row) if row is not None else None

    def by_path(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM projects WHERE path=?", (str(path or ""),)
            ).fetchone()
        return dict(row) if row is not None else None

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        if not include_archived:
            query += " WHERE archived=0"
        query += " ORDER BY created DESC, rowid DESC"
        with self._lock:
            rows = self._db.execute(query).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()