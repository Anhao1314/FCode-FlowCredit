"""Artifact store: the only place a write-effect tool may put bytes.

Properties that make agent output auditable rather than merely persisted:

* **Content-addressed.** A section is identified by the SHA-256 of its exact
  content, so re-running the same proposal cannot create a second copy, and
  the trace can name the artifact by hash rather than by a path someone could
  later overwrite.
* **Atomic.** Bytes go to a temp file, are fsynced, then renamed into place.
  A crash either leaves no artifact or leaves the complete one — never a
  half-written section that a report might cite.
* **Append-only.** Files are never rewritten; a new claim is a new file. The
  working paper is the ordered set of artifacts, not a mutable document.

Crash boundary worth stating honestly: if the process dies between the rename
and the committed event, an orphan artifact can exist without a chain entry.
It is harmless (nothing cites it) and detectable (its hash is on disk); the
reverse — an event that cites a missing artifact — cannot happen because the
event is written after the rename.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ..core.contracts import AgentError

__all__ = ["ArtifactStore"]

MAX_TITLE = 120
MAX_BODY = 8000
MAX_CITATIONS = 20


class ArtifactStore:
    """Append-only, content-addressed sections under one sandbox root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def append_section(
        self,
        *,
        title: str,
        body: str,
        citations: list[str] | None = None,
    ) -> dict[str, Any]:
        citations = list(citations or [])
        if not isinstance(title, str) or not title.strip():
            raise AgentError("ARTIFACT_INVALID", "title must be a non-empty string")
        if len(title) > MAX_TITLE:
            raise AgentError("ARTIFACT_INVALID", f"title exceeds {MAX_TITLE} characters")
        if not isinstance(body, str) or not body.strip():
            raise AgentError("ARTIFACT_INVALID", "body must be a non-empty string")
        if len(body) > MAX_BODY:
            raise AgentError("ARTIFACT_INVALID", f"body exceeds {MAX_BODY} characters")
        if len(citations) > MAX_CITATIONS:
            raise AgentError("ARTIFACT_INVALID", f"at most {MAX_CITATIONS} citations")
        for citation in citations:
            if not isinstance(citation, str) or not citation.strip():
                raise AgentError("ARTIFACT_INVALID", "citations must be non-empty strings")

        # No timestamps or random ids inside the content: identical claims must
        # hash identically, otherwise "same proposal twice" silently duplicates.
        payload = (
            f"# {title.strip()}\n\n{body.strip()}\n\n"
            + "## 引用\n"
            + ("\n".join(f"- {item.strip()}" for item in citations) if citations else "- （无）")
            + "\n"
        )
        encoded = payload.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{digest[:16]}.md"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / name
        if not target.exists():
            tmp = self.root / f".tmp-{digest[:16]}"
            tmp.write_bytes(encoded)
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        return {
            "artifact_id": f"artifact:{digest[:16]}",
            "path": name,
            "sha256": digest,
            "bytes": len(encoded),
            "section": title.strip(),
            "citations": len(citations),
        }
