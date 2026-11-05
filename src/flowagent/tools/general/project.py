"""Bounded, isolated project workspaces and baseline-relative Git delivery.

The source checkout is never a tool working directory.  A run receives a copy
without repository metadata, dependencies, credentials or symbolic links; the
copy gets its own Git baseline and every subsequent diff is relative to it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from .sandbox import WorkspaceSandbox

__all__ = [
    "MAX_PROJECT_BYTES",
    "MAX_PROJECT_FILES",
    "PROJECT_REPORT_PATH",
    "import_project",
    "project_delivery",
    "validate_project_spec",
]

MAX_PROJECT_FILES = 2_000
MAX_PROJECT_BYTES = 32 * 1024 * 1024
PROJECT_REPORT_PATH = "FCODE_RESULT.md"

_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", "coverage", "htmlcov", "work", "runs",
}
_EXCLUDED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".pyc", ".pyo"}
_SAFE_ENV_FILES = {".env.example", ".env.sample", ".env.template"}
_DELIVERY_EXCLUDES = (
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    "node_modules/", "dist/", "build/", "target/",
)


def _project_error(code: str, message: str) -> AgentError:
    return AgentError(code, message)


def validate_project_spec(project: Any) -> dict[str, str]:
    """Validate the operator's project declaration without reading its files."""
    if not isinstance(project, dict) or set(project) != {"path"}:
        raise ValueError("project must be an object containing only path")
    path = project["path"]
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("project.path must be a non-empty absolute directory path")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("project.path must be absolute")
    return {"path": str(candidate.resolve())}


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".env") and name not in _SAFE_ENV_FILES:
        return True
    if name in {"credentials", "credentials.json", "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    return path.suffix.lower() in _EXCLUDED_SUFFIXES


def import_project(
    source: str | Path,
    sandbox: WorkspaceSandbox,
    *,
    max_files: int = MAX_PROJECT_FILES,
    max_bytes: int = MAX_PROJECT_BYTES,
) -> dict[str, Any]:
    """Copy a bounded project into a fresh sandbox and commit its baseline."""
    source_root = Path(source).expanduser()
    try:
        resolved = source_root.resolve(strict=True)
    except OSError as error:
        raise _project_error("PROJECT_NOT_FOUND", f"project directory is not readable: {source_root}") from error
    if not resolved.is_dir():
        raise _project_error("PROJECT_NOT_DIRECTORY", "project.path must name a directory")
    destination = sandbox.root.resolve()
    if destination == resolved or destination in resolved.parents or resolved in destination.parents:
        raise _project_error("PROJECT_COPY_RECURSIVE", "project and run workspace must not contain one another")
    if sandbox.root.exists() and any(sandbox.root.iterdir()):
        raise _project_error("PROJECT_WORKSPACE_NOT_EMPTY", "a project can only be imported into an empty run workspace")

    sandbox.root.mkdir(parents=True, exist_ok=True)
    copied_files = 0
    copied_bytes = 0
    skipped_dirs = 0
    skipped_files = 0
    skipped_links = 0
    try:
        for current, directories, filenames in os.walk(resolved, topdown=True, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for name in directories:
                child = current_path / name
                if child.is_symlink():
                    skipped_links += 1
                elif name.lower() in _EXCLUDED_DIRS:
                    skipped_dirs += 1
                else:
                    kept.append(name)
                    (destination / child.relative_to(resolved)).mkdir(parents=True, exist_ok=True)
            directories[:] = kept
            for name in filenames:
                child = current_path / name
                relative = child.relative_to(resolved)
                if child.is_symlink():
                    skipped_links += 1
                    continue
                if _is_sensitive(child):
                    skipped_files += 1
                    continue
                if not child.is_file():
                    skipped_files += 1
                    continue
                size = child.stat().st_size
                copied_files += 1
                copied_bytes += size
                if copied_files > max_files or copied_bytes > max_bytes:
                    raise _project_error(
                        "PROJECT_TOO_LARGE",
                        f"project exceeds the import limit ({max_files} files / {max_bytes} bytes)",
                    )
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target, follow_symlinks=False)
    except AgentError:
        raise
    except OSError as error:
        raise _project_error("PROJECT_IMPORT_FAILED", f"cannot copy project: {error}") from error
    if copied_files == 0:
        raise _project_error("PROJECT_EMPTY", "project contains no importable files")

    sandbox.ensure()
    exclude = sandbox.root / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n# FCode run-internal and generated files\n")
        for pattern in _DELIVERY_EXCLUDES:
            stream.write(f"{pattern}\n")
    sandbox._git(sandbox.root, "add", "-A", "--", ".")
    sandbox._git(sandbox.root, "commit", "--quiet", "-m", "fcode: imported project baseline")
    baseline = sandbox._git(sandbox.root, "rev-parse", "HEAD").strip()
    return {
        "source": str(resolved),
        "name": resolved.name,
        "files": copied_files,
        "bytes": copied_bytes,
        "skipped": {"directories": skipped_dirs, "files": skipped_files, "symlinks": skipped_links},
        "baselineCommit": baseline,
        "isolatedCopy": True,
    }


def project_delivery(sandbox: WorkspaceSandbox, project: dict[str, Any]) -> dict[str, Any]:
    """Return metadata plus a binary-safe patch of code changes only."""
    baseline = str(project.get("baselineCommit") or "")
    if not baseline:
        raise _project_error("PROJECT_BASELINE_MISSING", "project baseline is unavailable")
    sandbox.ensure()
    # Include agent-created source files, but neither the result report nor the
    # dependency/cache directories that test commands may generate.
    pathspec = [".", f":(exclude){PROJECT_REPORT_PATH}"]
    sandbox._git(sandbox.root, "add", "-A", "--", *pathspec)
    patch = sandbox._git(
        sandbox.root, "diff", "--cached", "--binary", "--full-index", baseline, "--", *pathspec,
    )
    names = sandbox._git(sandbox.root, "diff", "--cached", "--name-status", baseline, "--", *pathspec)
    stat = sandbox._git(sandbox.root, "diff", "--cached", "--stat", baseline, "--", *pathspec).strip()
    files = []
    for line in names.splitlines():
        status, _, path = line.partition("\t")
        files.append({"status": status, "path": path})
    return {
        **{key: value for key, value in project.items() if key != "patch"},
        "filesChanged": files,
        "stat": stat,
        "patchBytes": len(patch.encode("utf-8")),
        "patch": patch,
        "reportExcluded": PROJECT_REPORT_PATH,
    }
