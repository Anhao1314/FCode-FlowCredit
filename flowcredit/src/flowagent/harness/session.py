"""Session tree + file history (S3): append-only JSONL with parentUuid.

Ported shape from the dissected harness:

* one JSONL file per session under ``<root>/projects/<project-slug>/``;
* every event carries ``uuid`` / ``parentUuid`` / ``type`` / ``atMs``, so the
  transcript is a tree rather than a flat log;
* ``rewind(uuid)`` moves the write cursor: later events branch from that node
  while the old branch stays intact (fork, never destructive);
* compaction records a ``compact_boundary`` event instead of rewriting
  history;
* file contents are snapshotted by sha256 before an edit, so a rewind can
  restore the bytes a message saw.

The store is deliberately small and synchronous; wiring it into tool calls
belongs to the Work migration stage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

__all__ = [
    "FileHistory",
    "SessionStore",
    "project_slug",
]

Clock = Callable[[], float]
UuidFactory = Callable[[], str]


def project_slug(cwd: str | os.PathLike[str]) -> str:
    """Map an absolute working directory to a filesystem-safe project key."""
    resolved = str(Path(cwd).resolve())
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved).strip("-")
    return slug or "root"


class SessionStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        *,
        session_id: str | None = None,
        clock: Clock = time.time,
        uuid_factory: UuidFactory = lambda: uuid4().hex,
    ) -> None:
        self.root = Path(root)
        self.cwd = str(Path(cwd).resolve())
        self.session_id = session_id or uuid4().hex[:16]
        self._clock = clock
        self._uuid = uuid_factory
        self.path = self.root / "projects" / project_slug(self.cwd) / f"{self.session_id}.jsonl"
        self.warnings: list[str] = []
        self._events: list[dict[str, Any]] | None = None
        self._cursor: str | None = None

    # ------------------------------------------------------------------ events

    def append(
        self,
        type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        parent: str | None = None,
        role: str | None = None,
        content: Any = None,
    ) -> dict[str, Any]:
        events = self.events()
        parent_uuid = parent if parent is not None else (
            self._cursor or (events[-1]["uuid"] if events else None))
        event: dict[str, Any] = {
            "uuid": self._uuid(),
            "parentUuid": parent_uuid,
            "type": str(type),
            "atMs": int(self._clock() * 1000),
            "sessionId": self.session_id,
            "cwd": self.cwd,
        }
        if role is not None:
            event["role"] = role
        if content is not None:
            event["content"] = content
        for key, value in dict(payload or {}).items():
            if key not in ("uuid", "parentUuid", "type", "atMs", "sessionId", "cwd"):
                event[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events = events + [event]
        return event

    def record_message(self, role: str, content: Any, *,
                       parent: str | None = None) -> dict[str, Any]:
        return self.append("message", {"role": role}, parent=parent,
                           role=role, content=content)

    def record_boundary(
        self,
        *,
        before_tokens: int,
        after_tokens: int,
        summary_sha256: str,
        trigger: str = "auto",
        parent: str | None = None,
    ) -> dict[str, Any]:
        return self.append(
            "compact_boundary",
            {
                "trigger": str(trigger),
                "beforeTokens": int(before_tokens),
                "afterTokens": int(after_tokens),
                "summarySha256": str(summary_sha256),
            },
            parent=parent,
        )

    def events(self) -> list[dict[str, Any]]:
        if self._events is not None:
            return self._events
        events: list[dict[str, Any]] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._events = events
            return events
        for number, line in enumerate(raw.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError as error:
                self.warnings.append(f"line {number}: {error}")
                continue
            if not isinstance(event, dict) or not event.get("uuid"):
                self.warnings.append(f"line {number}: missing uuid")
                continue
            events.append(event)
        self._events = events
        return events

    # ------------------------------------------------------------------- tree

    def rewind(self, uuid: str) -> None:
        """Point the write cursor at an existing event (non-destructive)."""
        if uuid not in {event["uuid"] for event in self.events()}:
            raise KeyError(f"unknown session event {uuid!r}")
        self._cursor = uuid

    def clear_rewind(self) -> None:
        self._cursor = None

    def children(self, uuid: str) -> list[dict[str, Any]]:
        return [event for event in self.events()
                if event.get("parentUuid") == uuid]

    def chain(self, tip: str | None = None) -> list[dict[str, Any]]:
        events = self.events()
        if not events:
            return []
        index = {event["uuid"]: event for event in events}
        current = tip or self._cursor or events[-1]["uuid"]
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current and current in index and current not in seen:
            seen.add(current)
            event = index[current]
            chain.append(event)
            current = event.get("parentUuid")
        return list(reversed(chain))

    def messages(self, tip: str | None = None) -> list[dict[str, Any]]:
        return [
            {"role": event.get("role", "user"), "content": event.get("content")}
            for event in self.chain(tip)
            if event.get("type") == "message"
        ]

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.events()[-max(0, int(limit)):]


class FileHistory:
    """Hash-addressed file snapshots, one manifest per session."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        session_id: str,
        clock: Clock = time.time,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.directory = self.root / "file-history" / session_id
        self.blob_dir = self.directory / "blobs"
        self.manifest_path = self.directory / "manifest.jsonl"
        self._clock = clock

    def snapshot(self, path: str | os.PathLike[str], *,
                 message_uuid: str = "") -> str:
        target = Path(path)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            self._record({
                "messageUuid": str(message_uuid),
                "path": str(target),
                "sha256": "",
                "exists": False,
            })
            return ""
        digest = hashlib.sha256(data).hexdigest()
        blob = self.blob_dir / digest[:2] / digest
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            temporary = blob.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, blob)
        self._record({
            "messageUuid": str(message_uuid),
            "path": str(target),
            "sha256": digest,
            "exists": True,
            "bytes": len(data),
        })
        return digest

    def read(self, sha256: str) -> bytes:
        digest = str(sha256 or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid snapshot digest {sha256!r}")
        return (self.blob_dir / digest[:2] / digest).read_bytes()

    def restore(self, sha256: str, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".restore")
        temporary.write_bytes(self.read(sha256))
        os.replace(temporary, target)

    def entries(self) -> list[dict[str, Any]]:
        try:
            raw = self.manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _record(self, row: Mapping[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        body = dict(row)
        body["atMs"] = int(self._clock() * 1000)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
