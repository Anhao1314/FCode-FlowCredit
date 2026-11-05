"""The per-run workspace sandbox: path-jailed, git-versioned, hash-receipted.

Three properties, all enforced *here* — never in the UI, never in a prompt:

* **Path jail.** Every path is normalised and resolved before use. Absolute
  paths, ``..`` components and symlink escapes are refused with a stable code.
  ``Path.resolve()`` follows symlinks, so a link planted inside the sandbox
  that points outside resolves outside and fails the containment check.

* **Versioned writes.** The sandbox root is a git repository (created lazily).
  Every ``write`` commits, so the approval dialog can show a real diff and an
  auditor can replay the file history next to the event chain. Git is invoked
  with an inline identity, so no global git config is read or required.

* **Hash receipts.** Writes record the file's sha256 before and after. The
  domain verifier recomputes the after-hash from disk; the recorded numbers
  are never taken on the tool's word.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError

__all__ = ["WorkspaceSandbox", "MAX_WRITE_CHARS", "MAX_EDIT_LINES", "DIFF_PREVIEW_LINES"]

#: One write is bounded well below the 96 KiB plan-argument ceiling, so a
#: proposal can never smuggle a payload the tool would refuse after approval.
MAX_WRITE_CHARS = 65_536
#: One edit is bounded in *changed lines* (additions + removals from the
#: unified diff), not bytes: a bounded, reviewable change is the whole point
#: of edit_file. Larger refactors must be split into separate proposals.
MAX_EDIT_LINES = 200
#: The approval dialog shows the head of the diff; the chain keeps everything.
DIFF_PREVIEW_LINES = 240

_GIT_IDENTITY = [
    "-c", "user.name=fcode-runtime",
    "-c", "user.email=runtime@fcode.local",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WorkspaceSandbox:
    """One run's workspace directory, jailed at ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ layout

    def ensure(self) -> None:
        """Create the directory and its git repository (idempotent)."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root / ".git").exists():
                self._git(self.root, "init", "--quiet", "--initial-branch=main")

    def _git(self, cwd: Path, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *_GIT_IDENTITY, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as error:
            raise AgentError(
                "SANDBOX_UNAVAILABLE",
                "git is required for the workspace sandbox but was not found",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AgentError("SANDBOX_UNAVAILABLE", f"git {args[0]} timed out") from error
        if completed.returncode != 0:
            raise AgentError(
                "SANDBOX_UNAVAILABLE",
                f"git {args[0]} failed: {completed.stderr.strip()[:200]}",
            )
        return completed.stdout

    # -------------------------------------------------------------- path jail

    def resolve(self, path: str) -> tuple[Path, str]:
        """Normalise, jail and return ``(absolute, relative_posix)``.

        Refused before any filesystem touch: absolute paths, ``~``-escapes,
        ``..`` components, empty components, NUL bytes, backslashes (kept out
        so a Windows-shaped path can never smuggle a separator past the jail).
        ``symlink escapes`` are refused by the resolve-then-contain check.
        """
        from pathlib import PurePosixPath

        if not isinstance(path, str) or not path.strip():
            raise AgentError("SANDBOX_PATH_DENIED", "path must be a non-empty relative path")
        if "\x00" in path or "\\" in path:
            raise AgentError("SANDBOX_PATH_DENIED", f"path {path!r} contains a forbidden character")
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or path.startswith("/") or path.startswith("~"):
            raise AgentError(
                "SANDBOX_PATH_DENIED",
                f"path {path!r} must be relative to the workspace root",
            )
        parts = candidate.parts
        if any(part in ("..", ".") for part in parts):
            raise AgentError(
                "SANDBOX_PATH_DENIED",
                f"path {path!r} must not contain '.' or '..' components",
            )
        if any(part == "" for part in path.split("/")):
            raise AgentError("SANDBOX_PATH_DENIED", f"path {path!r} has an empty component")
        if not parts:
            raise AgentError("SANDBOX_PATH_DENIED", "path must name a file")
        relative = "/".join(parts)
        if relative.startswith(".git") or "/.git" in f"/{relative}":
            raise AgentError("SANDBOX_PATH_DENIED", "the sandbox metadata directory is not addressable")

        root_resolved = self.root.resolve()
        target = (root_resolved / relative).resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise AgentError(
                "SANDBOX_PATH_DENIED",
                f"path {path!r} resolves outside the workspace sandbox",
            )
        return target, relative

    # ------------------------------------------------------------------- reads

    def ls(self) -> list[dict[str, Any]]:
        """List files (never ``.git``) with size and content hash."""
        self.ensure()
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            # Never follow a workspace link while hashing an inventory. Direct
            # reads still use resolve(); listings fail closed by omitting links.
            if path.is_symlink() or not path.is_file() or self._is_metadata(path):
                continue
            data = path.read_bytes()
            relative = path.relative_to(self.root).as_posix()
            out.append({
                "path": relative,
                "bytes": len(data),
                "sha256": _sha256(data),
            })
        return out

    def read(self, path: str) -> dict[str, Any]:
        target, relative = self.resolve(path)
        if not target.exists() or target.is_dir():
            raise AgentError("SANDBOX_FILE_NOT_FOUND", f"{relative!r} does not exist in the workspace")
        data = target.read_bytes()
        return {
            "path": relative,
            "content": data.decode("utf-8", errors="replace"),
            "sha256": _sha256(data),
            "bytes": len(data),
        }

    # ------------------------------------------------------------------ writes

    def write(self, path: str, content: str) -> dict[str, Any]:
        """Write one file atomically and commit it.

        The before/after hashes are the write's receipt; the commit makes the
        change replayable next to the event chain. A rewrite with identical
        content produces no commit (git has nothing to record) — the receipt
        still carries both hashes, so the no-op is visible, not silent.
        """
        if not isinstance(content, str):
            raise AgentError("SANDBOX_WRITE_DENIED", "content must be a string")
        if len(content) > MAX_WRITE_CHARS:
            raise AgentError(
                "SANDBOX_WRITE_DENIED",
                f"content exceeds {MAX_WRITE_CHARS} characters",
            )
        self.ensure()
        target, relative = self.resolve(path)
        if target.exists() and target.is_dir():
            raise AgentError("SANDBOX_WRITE_DENIED", f"{relative!r} is a directory")
        before: str | None = None
        if target.exists():
            before = _sha256(target.read_bytes())
        after = _sha256(content.encode("utf-8"))
        # Preserve the same bounded, host-computed diff shown at approval time
        # in the executed observation. The review thread can then render the
        # actual before/after change rather than inferring one from metadata.
        preview = self.diff_preview(relative, content)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A predictable PID-only name let a pre-planted symlink redirect the
        # write before replace(). mkstemp creates a random file with O_EXCL in
        # the already-jailed parent, so an existing link is never followed.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.tmp-", dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        commit = ""
        changed = before != after
        if changed:
            with self._lock:
                self._git(self.root, "add", "-A", "--", relative)
                # --allow-empty is deliberately absent: an unchanged rewrite
                # must not fabricate history.
                self._git(self.root, "commit", "--quiet", "-m", f"write_file: {relative}", "--", relative)
                head = self._git(self.root, "rev-parse", "--short", "HEAD").strip()
                commit = head
        return {
            "path": relative,
            "bytes": len(content.encode("utf-8")),
            "sha256_before": before,
            "sha256_after": after,
            "commit": commit,
            "changed": changed,
            "diff": preview["diff"],
            "truncated": preview["truncated"],
        }

    # ------------------------------------------------------------------- edit

    def edit(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """Replace one unique anchor block; commit and receipt like a write.

        The anchor must match exactly once — a fuzzy or many-way match would
        make the *approval* (which shows the diff for this exact edit) a lie.
        Missing files are refused here: creating is ``write_file``'s contract,
        and blurring the two would let an "edit" smuggle a new file.
        """
        target, relative = self.resolve(path)
        if not target.exists() or target.is_dir():
            raise AgentError("SANDBOX_EDIT_TARGET_MISSING", f"{relative!r} does not exist; edits never create files")
        current = target.read_text(encoding="utf-8", errors="replace")
        new_content, changed_lines, _candidates = self._apply_anchor(current, old_text, new_text, relative)
        if changed_lines > MAX_EDIT_LINES:
            raise AgentError(
                "SANDBOX_EDIT_TOO_LARGE",
                f"this edit changes {changed_lines} lines; the cap is {MAX_EDIT_LINES} — split it into smaller edits",
            )
        additions, removals, _total = self._diff_stat(current, new_content)
        record = self.write(relative, new_content)
        record.update({"lines_added": additions, "lines_removed": removals})
        return record

    def edit_preview(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """The approval-side twin of :meth:`edit`: nothing is written.

        Returns the diff of the edit this proposal would make, or the refusal
        the execution would get (missing file, no match, ambiguous anchor,
        oversized change) so the operator approves exactly what can run.
        """
        try:
            target, relative = self.resolve(path)
            if not target.exists() or target.is_dir():
                raise AgentError("SANDBOX_EDIT_TARGET_MISSING", f"{relative!r} does not exist")
            current = target.read_text(encoding="utf-8", errors="replace")
            new_content, changed_lines, _candidates = self._apply_anchor(current, old_text, new_text, relative)
            if changed_lines > MAX_EDIT_LINES:
                raise AgentError(
                    "SANDBOX_EDIT_TOO_LARGE",
                    f"this edit changes {changed_lines} lines; the cap is {MAX_EDIT_LINES}",
                )
            preview = self.diff_preview(relative, new_content)
        except AgentError as error:
            return {"path": path, "created": False, "additions": 0, "removals": 0,
                    "truncated": False, "diff": "", "error": f"{error.code}: {error.message}"}
        preview["edit"] = True
        return preview

    def _apply_anchor(
        self, current: str, old_text: str, new_text: str, relative: str
    ) -> tuple[str, int, list[int]]:
        """Locate the anchor, enforce uniqueness, return the new content."""
        if not old_text:
            raise AgentError("SANDBOX_EDIT_ANCHOR_INVALID", "old_text must be a non-empty anchor block")
        occurrences: list[int] = []
        start = 0
        while True:
            found = current.find(old_text, start)
            if found < 0:
                break
            occurrences.append(found)
            start = found + 1  # overlapping anchors count separately: they are still ambiguous
            if len(occurrences) > 8:
                break
        if not occurrences:
            line = self._nearest_line_hint(current, old_text)
            hint = f" (nearest anchor starts near line {line})" if line else ""
            raise AgentError(
                "SANDBOX_EDIT_ANCHOR_NOT_FOUND",
                f"old_text does not appear in {relative!r}{hint}",
            )
        if len(occurrences) > 1:
            lines = [current.count("\n", 0, offset) + 1 for offset in occurrences[:8]]
            raise AgentError(
                "SANDBOX_EDIT_ANCHOR_AMBIGUOUS",
                f"old_text matches {len(occurrences)} places in {relative!r} "
                f"(first at lines {lines}); make the anchor unique",
            )
        new_content = current.replace(old_text, new_text, 1)
        return new_content, self._diff_stat(current, new_content)[2], occurrences

    @staticmethod
    def _nearest_line_hint(current: str, old_text: str) -> int | None:
        """Best-effort line hint from the anchor's first surviving line."""
        first_line = next((line.strip() for line in old_text.splitlines() if line.strip()), "")
        if not first_line:
            return None
        offset = current.find(first_line)
        if offset < 0:
            return None
        return current.count("\n", 0, offset) + 1

    @staticmethod
    def _diff_stat(current: str, new_content: str) -> tuple[int, int, int]:
        """(additions, removals, total changed lines) from a unified diff."""
        diff_lines = list(difflib.unified_diff(
            current.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            n=0,
        ))
        additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removals = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        return additions, removals, additions + removals

    # ----------------------------------------------------------------- preview

    def diff_preview(self, path: str, content: str) -> dict[str, Any]:
        """The unified diff the approval dialog shows *before* execution.

        Host-side by construction: it compares the proposed content with the
        file's current state. It is a preview of exactly what ``write`` would
        do — same normalisation, same jail — nothing is executed.
        """
        target, relative = self.resolve(path)
        current = ""
        exists = target.exists() and not target.is_dir()
        if exists:
            current = target.read_text(encoding="utf-8", errors="replace")
        diff_lines = list(difflib.unified_diff(
            current.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        ))
        preview = "".join(diff_lines[:DIFF_PREVIEW_LINES])
        truncated = len(diff_lines) > DIFF_PREVIEW_LINES
        additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removals = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        return {
            "path": relative,
            "created": not exists,
            "additions": additions,
            "removals": removals,
            "truncated": truncated,
            "diff": preview,
        }

    # ----------------------------------------------------------------- private

    def _is_metadata(self, path: Path) -> bool:
        return ".git" in path.relative_to(self.root).parts
