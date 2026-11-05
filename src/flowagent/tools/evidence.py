"""Evidence ingestion: files on disk become anchored, hashed observations.

Three boundaries make this a *read-only evidence* tool rather than a file
reader with extra steps:

* **Sandbox root.** Only paths that resolve inside the store root are readable;
  absolute paths and ``..`` escapes are refused. A tool that can read anywhere
  is not a read-only tool, it is an exfiltration tool.
* **Byte-level provenance.** Every ingested document keeps its SHA-256 and
  size, so the observation can be traced back to exact bytes.
* **Bounded reads.** ``read_slice`` returns a line range with anchors and caps
  the window; whole-document dumps are what blow up contexts and traces.

The store is in-memory: restarting the process forgets what was ingested, but
the same file re-ingests to the same ``doc_id`` (content-addressed), so a
resumed run can rebuild its evidence deterministically.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.contracts import AgentError

__all__ = ["EvidenceDoc", "EvidenceStore"]

MAX_BYTES = 2 * 1024 * 1024
MAX_SLICE_LINES = 80


@dataclass(frozen=True)
class EvidenceDoc:
    doc_id: str
    path: str
    kind: str
    sha256: str
    size: int
    lines: list[str]
    rows: int | None = None
    columns: list[str] | None = None
    metadata: dict[str, Any] | None = None


class EvidenceStore:
    """Content-addressed evidence under one sandbox root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._docs: dict[str, EvidenceDoc] = {}

    # ------------------------------------------------------------- ingestion

    def _resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise AgentError("EVIDENCE_PATH_DENIED", "path must be a non-empty string")
        raw = Path(path)
        if raw.is_absolute():
            raise AgentError("EVIDENCE_PATH_DENIED", "absolute paths are refused; use a path inside the evidence root")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise AgentError("EVIDENCE_PATH_DENIED", f"{path!r} escapes the evidence root")
        if not candidate.is_file():
            raise AgentError("EVIDENCE_NOT_FOUND", f"{path!r} is not a file under the evidence root")
        if candidate.stat().st_size > MAX_BYTES:
            raise AgentError("EVIDENCE_TOO_LARGE", f"{path!r} exceeds {MAX_BYTES} bytes")
        return candidate

    def ingest(self, path: str) -> dict[str, Any]:
        resolved = self._resolve(path)
        data = resolved.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        doc_id = f"evidence:{sha[:12]}"
        if doc_id in self._docs:
            doc = self._docs[doc_id]
        else:
            suffix = resolved.suffix.lower()
            kind = {".csv": "csv", ".json": "json", ".md": "markdown", ".txt": "text"}.get(suffix, "text")
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            rows: int | None = None
            columns: list[str] | None = None
            if kind == "csv" and lines:
                reader = list(csv.reader(io.StringIO(text)))
                rows = max(0, len(reader) - 1)
                columns = reader[0] if reader else []
            elif kind == "json":
                try:
                    parsed = json.loads(text)
                    lines = json.dumps(parsed, ensure_ascii=False, indent=2).splitlines()
                except ValueError:
                    kind = "text"  # malformed JSON is still readable evidence
            doc = EvidenceDoc(
                doc_id=doc_id,
                path=str(resolved.relative_to(self.root)),
                kind=kind,
                sha256=sha,
                size=len(data),
                lines=lines,
                rows=rows,
                columns=columns,
            )
            self._docs[doc_id] = doc
        return {
            "doc_id": doc.doc_id,
            "kind": doc.kind,
            "path": doc.path,
            "size": doc.size,
            "sha256": doc.sha256,
            "lines": len(doc.lines),
            "rows": doc.rows,
            "columns": doc.columns,
            "metadata": doc.metadata or {},
        }

    def ingest_text(
        self,
        text: str,
        *,
        name: str,
        kind: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add collected text as evidence with a content-addressed id.

        The text already came through a host-owned source; this method only
        anchors it. The virtual path is for display and search results; no file
        is written outside the store root.
        """
        if not isinstance(text, str) or not text.strip():
            raise AgentError("EVIDENCE_EMPTY", "collected source produced no readable text")
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise AgentError("EVIDENCE_TOO_LARGE", f"collected text exceeds {MAX_BYTES} bytes")
        sha = hashlib.sha256(data).hexdigest()
        doc_id = f"evidence:{sha[:12]}"
        if doc_id not in self._docs:
            slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "source").lower()).strip("-") or "source"
            doc = EvidenceDoc(
                doc_id=doc_id,
                path=f"collected/{slug}-{sha[:12]}.txt",
                kind=kind,
                sha256=sha,
                size=len(data),
                lines=text.splitlines(),
                metadata=dict(metadata or {}),
            )
            self._docs[doc_id] = doc
        doc = self._docs[doc_id]
        return {
            "doc_id": doc.doc_id,
            "kind": doc.kind,
            "path": doc.path,
            "size": doc.size,
            "sha256": doc.sha256,
            "lines": len(doc.lines),
            "rows": doc.rows,
            "columns": doc.columns,
            "metadata": doc.metadata or {},
        }

    # ---------------------------------------------------------------- queries

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for doc in self._docs.values():
            for number, line in enumerate(doc.lines, start=1):
                if needle in line.lower():
                    hits.append({
                        "doc_id": doc.doc_id,
                        "path": doc.path,
                        "source": (doc.metadata or {}).get("source", ""),
                        "line": number,
                        "snippet": line.strip()[:200],
                    })
                    if len(hits) >= limit:
                        return {"query": query, "hits": hits, "scanned": len(self._docs), "truncated": True}
        return {"query": query, "hits": hits, "scanned": len(self._docs), "truncated": False}

    def read_slice(self, doc_id: str, start: int = 1, end: int | None = None) -> dict[str, Any]:
        doc = self._docs.get(doc_id)
        if doc is None:
            raise AgentError("EVIDENCE_NOT_FOUND", f"{doc_id!r} has not been ingested in this session")
        total = len(doc.lines)
        first = max(1, int(start))
        last = total if end is None else max(first, int(end))
        last = min(last, first + MAX_SLICE_LINES - 1, total)
        if first > total:
            first, last = total, total
        window = [{"line": number, "text": doc.lines[number - 1]} for number in range(first, last + 1)]
        return {
            "doc_id": doc.doc_id,
            "start": first,
            "end": last,
            "lines": window,
            "truncated": last < total,
            "total_lines": total,
            "metadata": doc.metadata or {},
        }
