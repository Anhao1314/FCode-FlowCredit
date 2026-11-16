"""Artifact store: revisions, hashes and anchored edits.

One artifact per capability run (``index.html`` for motion). All writes go
through the existing ``WorkspaceSandbox`` so the jail, per-write git commit and
hash receipt stay exactly where they are today; this module only adds a
monotonic revision counter and two pure scene helpers the checks use.

There is no settlement here. A revision is *the bytes on disk*, nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Action", "ArtifactRevision", "ArtifactStore"]

_SCENE = re.compile(
    r"<svg\b[^>]*\bid\s*=\s*[\"']scene[\"'][^>]*>(.*?)</svg\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ELEMENT = re.compile(r"<[A-Za-z][^>]*>")


@dataclass(frozen=True)
class Action:
    """One capability-internal tool call proposed by the scripted proposer."""

    tool: str
    arguments: dict[str, Any]
    tokens: int = 0
    cost_microusd: int = 0
    model_calls: int = 1
    note: str = ""


@dataclass(frozen=True)
class ArtifactRevision:
    """The artifact's state after one applied action."""

    path: str
    revision: int
    sha256: str
    bytes: int
    content: str
    changed: bool

    def identity(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


class ArtifactStore:
    """Reads and edits one file through the host sandbox."""

    def __init__(self, sandbox: Any, *, path: str = "index.html", result_path: str = "FCODE_RESULT.md") -> None:
        self.sandbox = sandbox
        self.path = path
        self.result_path = result_path
        self.revision = 0

    # --------------------------------------------------------------- snapshot

    def read(self, path: str | None = None) -> dict[str, Any]:
        return self.sandbox.read(path or self.path)

    def snapshot(self) -> ArtifactRevision:
        record = self.read()
        return ArtifactRevision(
            path=str(record.get("path") or self.path),
            revision=self.revision,
            sha256=str(record.get("sha256") or ""),
            bytes=int(record.get("bytes") or 0),
            content=str(record.get("content") or ""),
            changed=False,
        )

    def identity(self) -> dict[str, Any]:
        return self.snapshot().identity()

    # ------------------------------------------------------------ pure helpers

    def content(self) -> str:
        return str(self.read().get("content") or "")

    def scene_body(self) -> str:
        """The inside of ``<svg id="scene">``, or an empty string."""
        match = _SCENE.search(self.content())
        return match.group(1) if match else ""

    def scene_nonempty(self) -> bool:
        """True when the scene holds at least one element tag.

        Comments (including the ``<!-- SECTION: scene -->`` anchor) and
        whitespace are not implementation. This is the same rule the host uses
        to decide whether a capability may leave its draft stage.
        """
        body = _COMMENT.sub("", self.scene_body())
        return bool(_ELEMENT.search(body))

    def result_exists(self) -> bool:
        try:
            self.read(self.result_path)
        except Exception:  # noqa: BLE001 — a missing result is a failed check, not a crash
            return False
        return True

    # ----------------------------------------------------------------- mutate

    def refresh(self, *, changed: bool = False) -> ArtifactRevision:
        """Read the bytes after a host-governed action and advance the revision.

        The library path calls ``apply`` below. A Work Harness adapter executes
        through its own event chain instead, then calls this method; both paths
        therefore produce the same hash-bound revision semantics.
        """
        after = self.snapshot()
        result = ArtifactRevision(
            path=after.path,
            revision=self.revision + 1,
            sha256=after.sha256,
            bytes=after.bytes,
            content=after.content,
            changed=changed,
        )
        self.revision = result.revision
        return result

    def apply(self, action: Action) -> ArtifactRevision:
        """Apply one gated action; return the new revision.

        Observation actions (``motion_check``/``verify``) do not touch the
        artifact and therefore report ``changed=False`` with the same hash —
        which is what makes an unchanged-artifact streak detectable.
        """
        before = self.snapshot()
        tool = action.tool
        if tool == "edit_file":
            old_text = action.arguments.get("old_text")
            new_text = action.arguments.get("new_text") or ""
            if not isinstance(old_text, str) or not old_text:
                raise ValueError("edit_file requires a non-empty old_text anchor")
            self.sandbox.edit(action.arguments.get("path") or self.path, old_text, new_text)
        elif tool == "write_file":
            self.sandbox.write(
                action.arguments.get("path") or self.result_path,
                str(action.arguments.get("content") or ""),
            )
        elif tool in ("motion_check", "verify"):
            pass  # observation only; the check layer does the work
        else:
            raise ValueError(f"unsupported capability action {tool!r}")
        return self.refresh(changed=self.snapshot().sha256 != before.sha256)

    def restore(self, content: str) -> ArtifactRevision:
        """Restore a known-good artifact as a new revision.

        Used by the runtime's composition-regression guard: a later edit that
        fixes one metric by moving a wheel out of the scene must not become the
        base for the next proposal or the delivered artifact.
        """
        before = self.snapshot()
        self.sandbox.write(self.path, content)
        return self.refresh(changed=self.snapshot().sha256 != before.sha256)
