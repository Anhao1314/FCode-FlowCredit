"""Fixer write path: validate a patch in an isolated copy, then require approval.

Boundaries:
- only source repositories inside the audit repo are writable; artifacts (.app)
  are refused — a signed bundle cannot be patched through this path;
- the original tree is untouched until an explicit approve; validation happens
  in ``work/fixes/<id>/worktree``;
- a patch may only touch paths inside the audited target and may not use
  absolute paths, ``..``, ``.git``, or binary patches;
- approval applies with ``git apply --check`` first, then starts a fresh audit
  run; the fixer never marks a finding fixed. Only the new audit decides.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

__all__ = ["GovFixService", "parse_patch_paths", "MAX_PATCH_BYTES"]

MAX_PATCH_BYTES = 200_000
MAX_WORKTREE_FILES = 5_000
MAX_WORKTREE_BYTES = 100 * 1024 * 1024
_GENERATED = re.compile(r"^[a-z0-9][a-z0-9.-]{1,63}$")


def _within(child: Path, root: Path) -> bool:
    return child == root or root in child.parents


def _norm_patch_path(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value == "/dev/null":
        return ""
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"patch path escapes the target: {value!r}")
    if path.parts and path.parts[0] == ".git":
        raise ValueError("patch may not touch .git")
    return path.as_posix()


def parse_patch_paths(patch: str) -> list[str]:
    """Return the unique paths a unified diff touches; fail on unsafe paths."""
    paths: list[str] = []
    for line in str(patch or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for raw in (parts[2], parts[3]):
                    value = _norm_patch_path(raw)
                    if value and value not in paths:
                        paths.append(value)
        elif line.startswith("--- ") or line.startswith("+++ "):
            value = _norm_patch_path(line[4:].split("\t", 1)[0])
            if value and value not in paths:
                paths.append(value)
    return paths


class GovFixService:
    def __init__(self, audit: Any, reaudit: Callable[[str], dict] | None,
                 root: Path | str | None = None) -> None:
        self.audit = audit
        self.reaudit = reaudit
        base = getattr(audit, "root", None)
        self.root = Path(root or (Path(base).parent / "fixes")).resolve() if base else None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ create

    def create(self, audit_id: str, finding_id: str, patch: str) -> dict:
        if self.root is None:
            raise ValueError("fix storage is unavailable")
        bundle = self.audit.bundle(audit_id)
        if isinstance(bundle, tuple):
            raise ValueError("audit bundle not found")
        target = bundle.get("target") or {}
        identity = str(target.get("identity") or "").strip()
        if str(target.get("profile") or "") == "artifact":
            raise ValueError("fixes only support source repositories, not artifact bundles")
        finding = next((item for item in bundle.get("findings") or []
                        if str(item.get("finding_id") or "") == finding_id), None)
        if finding is None:
            raise ValueError("finding not found in this audit")
        repo = getattr(self.audit, "repo", None)
        if repo is None:
            raise ValueError("fixes require a repository")
        target_path = Path(identity).expanduser().resolve()
        if not _within(target_path, Path(repo).resolve()):
            raise ValueError("fix target escapes the repository")
        if not target_path.is_dir():
            raise ValueError("fix target is not a directory")

        text = str(patch or "").strip()
        if not text:
            raise ValueError("patch is required")
        if len(text.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ValueError(f"patch exceeds {MAX_PATCH_BYTES} bytes")
        if "GIT binary patch" in text:
            raise ValueError("binary patches are not accepted")
        paths = parse_patch_paths(text)
        if not paths:
            raise ValueError("patch does not touch any file")
        for relative in paths:
            resolved = (target_path / relative).resolve()
            if not _within(resolved, target_path):
                raise ValueError(f"patch path escapes the target: {relative!r}")

        fix_id = f"fix-{uuid.uuid4().hex[:12]}"
        fix_dir = self.root / fix_id
        worktree = fix_dir / "worktree"
        fix_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._copy_target(target_path, worktree)
            patch_path = fix_dir / "changes.patch"
            patch_path.write_text(text + ("\n" if not text.endswith("\n") else ""),
                                  encoding="utf-8")
            check = self._apply(worktree, patch_path, check=True)
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "").strip().splitlines()
                raise ValueError(f"patch does not apply: {(detail[-1] if detail else 'check failed')[:300]}")
            record = {
                "id": fix_id,
                "audit_id": str(bundle.get("audit_id") or audit_id),
                "finding_id": finding_id,
                "target": str(target_path),
                "status": "proposed",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "patch_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "files": paths,
                "patch_preview": text[:4000],
            }
            self._write(fix_dir, record)
            return record
        except Exception:
            shutil.rmtree(fix_dir, ignore_errors=True)
            raise

    # ------------------------------------------------------------------ decisions

    def approve(self, fix_id: str) -> dict:
        with self._lock:
            fix = self.get(fix_id)
            if fix is None:
                raise ValueError("fix not found")
            if fix.get("status") != "proposed":
                raise ValueError("fix is not waiting for approval")
            target = Path(str(fix.get("target") or "")).expanduser().resolve()
            repo = getattr(self.audit, "repo", None)
            if repo is None or not _within(target, Path(repo).resolve()) or not target.is_dir():
                raise ValueError("fix target is no longer available")
            fix_dir = self.root / fix_id
            patch_path = fix_dir / "changes.patch"
            check = self._apply(target, patch_path, check=True)
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "").strip().splitlines()
                raise ValueError(f"patch no longer applies: {(detail[-1] if detail else 'check failed')[:300]}")
            applied = self._apply(target, patch_path, check=False)
            if applied.returncode != 0:
                detail = (applied.stderr or applied.stdout or "").strip().splitlines()
                raise RuntimeError(f"git apply failed: {(detail[-1] if detail else 'apply failed')[:300]}")
            receipt = {
                "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "target": str(target),
                "patch_sha256": fix.get("patch_sha256"),
                "files": fix.get("files") or [],
            }
            (fix_dir / "apply.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            fix["status"] = "applied"
            fix["applied_at"] = receipt["applied_at"]
            if self.reaudit is not None:
                try:
                    run = self.reaudit(str(target)) or {}
                    fix["reaudit_run_id"] = str(run.get("run_id") or "")
                except Exception as error:  # noqa: BLE001 — patch is applied; surface re-audit failure
                    fix["reaudit_error"] = f"{type(error).__name__}: {error}"[:300]
            self._write(fix_dir, fix)
            return fix

    def reject(self, fix_id: str) -> dict:
        with self._lock:
            fix = self.get(fix_id)
            if fix is None:
                raise ValueError("fix not found")
            if fix.get("status") != "proposed":
                raise ValueError("fix is not waiting for approval")
            fix["status"] = "rejected"
            self._write(self.root / fix_id, fix)
            return fix

    # ------------------------------------------------------------------ reads

    def get(self, fix_id: str) -> dict | None:
        if self.root is None or not _GENERATED.match(str(fix_id or "")):
            return None
        path = self.root / fix_id / "fix.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, limit: int = 20) -> list[dict]:
        if self.root is None or not self.root.is_dir():
            return []
        rows: list[dict] = []
        for path in sorted(self.root.glob("*/fix.json"), reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if len(rows) >= max(1, min(int(limit), 50)):
                break
        return rows

    # ------------------------------------------------------------------ helpers

    def _write(self, fix_dir: Path, record: dict) -> None:
        tmp = fix_dir / "fix.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(fix_dir / "fix.json")

    @staticmethod
    def _copy_target(source: Path, destination: Path) -> None:
        ignore = shutil.ignore_patterns(
            ".git", "node_modules", ".venv", "venv", "__pycache__",
            "work", "runs", "artifacts",
        )
        shutil.copytree(source, destination, symlinks=False, ignore=ignore)
        files = 0
        size = 0
        for path in destination.rglob("*"):
            if path.is_file():
                files += 1
                size += path.stat().st_size
                if files > MAX_WORKTREE_FILES or size > MAX_WORKTREE_BYTES:
                    raise ValueError("fix target is too large for an isolated copy")

    @staticmethod
    def _apply(cwd: Path, patch_path: Path, *, check: bool) -> subprocess.CompletedProcess[str]:
        command = ["git", "apply", "--whitespace=nowarn"]
        if check:
            command.append("--check")
        command.append(str(patch_path))
        return subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=120,
        )
