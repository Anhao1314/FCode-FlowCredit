"""Bounded static facts for bundled JavaScript sidecars.

REA's JavaScript application analysis is the right tool for a source tree,
but an installed bundle also ships a Node/npm runtime under the sidecar.
Running the full graph there would produce hundreds of megabytes and drown
the facts the audit actually needs. This module therefore does a small,
deterministic **source-only** scan:

* only ``.ts/.js/.mjs/.cjs`` files;
* ``runtime/``, ``node_modules/`` and build output are never read;
* tests are inventoried but excluded from security patterns;
* each file is bounded, and the total is bounded;
* secret-like matches retain file/line/kind, never the literal.

These are host facts, not REA Evidence. The adapter pins them in
``facts.json``; rules only produce candidates, and the host oracle still
decides.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["scan_sidecars", "scan_sidecar"]

SOURCE_SUFFIXES = (".ts", ".js", ".mjs", ".cjs")
SKIP_DIRS = {
    "runtime", "node_modules", ".git", "dist", "build", "coverage",
    ".next", ".cache", "vendor",
}
TEST_MARKERS = (".test.", ".spec.", "-test.", "test-harness", "tests/")
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_FILES = 80
MAX_MATCHES = 20

_LISTENER = re.compile(r"\b(?:createServer|createHttpServer|createNetServer|\.listen)\s*\(")
_AUTH = re.compile(
    r"(?i)(authorization|bearer|timingSafeEqual|x-evox-secret|webhook_secret|"
    r"rpc_secret|verifyToken|authToken|shared_secret)"
)
_DANGEROUS = re.compile(
    r"(?i)(child_process|execFile\(|\bexec\(|\bspawn\(|\bfork\(|\beval\(|"
    r"new Function|vm\.runIn)"
)
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
    r"['\"][A-Za-z0-9_\-]{16,}['\"]"
)
_NPM = re.compile(r"(?i)\b(?:npm\s+(?:install|ci|exec)|npx\s+[@A-Za-z])")


def _is_test(relative: str) -> bool:
    lowered = relative.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def _collect_lines(path: Path, text: str, pattern: re.Pattern[str],
                   *, kind: str | None = None) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not pattern.search(line):
            continue
        record: dict[str, Any] = {"file": path, "line": number}
        if kind is not None:
            record["kind"] = kind
        matches.append(record)
        if len(matches) >= MAX_MATCHES:
            break
    return matches


def scan_sidecar(root: Path) -> dict[str, Any]:
    """Scan one sidecar directory and return bounded source facts."""
    root = Path(root)
    files: list[dict[str, Any]] = []
    listeners: list[dict[str, Any]] = []
    auth: list[dict[str, Any]] = []
    dangerous: list[dict[str, Any]] = []
    secrets: list[dict[str, Any]] = []
    npm_install: list[dict[str, Any]] = []
    total = 0
    truncated = False
    test_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            relative = str(path.relative_to(root))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
                truncated = True
                continue
            try:
                raw = path.read_bytes()[:MAX_FILE_BYTES]
            except OSError:
                continue
            total += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            is_test = _is_test(relative)
            files.append({
                "path": relative,
                "bytes": len(raw),
                "sha256": digest,
                "test": is_test,
            })
            if is_test:
                test_files += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            for item in _collect_lines(path, text, _LISTENER):
                listeners.append({**item, "file": relative})
            for item in _collect_lines(path, text, _AUTH):
                auth.append({**item, "file": relative})
            for item in _collect_lines(path, text, _DANGEROUS):
                dangerous.append({**item, "file": relative})
            for item in _collect_lines(path, text, _SECRET, kind="key-shaped"):
                secrets.append({**item, "file": relative})
            for item in _collect_lines(path, text, _NPM):
                npm_install.append({**item, "file": relative})
            if len(files) >= MAX_FILES:
                truncated = True
                break

    return {
        "name": root.name,
        "path": root.name,
        "files": files,
        "source_files": sum(1 for item in files if not item["test"]),
        "test_files": test_files,
        "runtime_bundled": (root / "runtime").is_dir(),
        "listeners": listeners,
        "auth_markers": auth,
        "listener_without_auth": [] if auth else listeners,
        "dangerous_calls": dangerous,
        "secret_like": secrets,
        "npm_install": npm_install,
        "truncated": truncated,
    }


def scan_sidecars(app_root: Path) -> list[dict[str, Any]]:
    """Scan ``Contents/Resources/*bridge*`` and ``*sidecar*`` directories."""
    resources = Path(app_root) / "Contents" / "Resources"
    if not resources.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for child in sorted(resources.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        lowered = child.name.lower()
        if "bridge" not in lowered and "sidecar" not in lowered:
            continue
        found.append(scan_sidecar(child))
    return found