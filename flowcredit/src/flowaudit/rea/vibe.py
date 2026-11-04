"""Bounded source-quality scan for AI-generated code patterns.

Vibe coding produces specific, detectable quality patterns that differ
from human-written code:

* god files / god functions (agents generate long monolithic code)
* TODO/FIXME density (agents leave markers they never follow up on)
* silent exception swallowing (bare except/empty catch — agents often
  suppress errors to make tests pass)
* dead imports (agents import modules they end up not using)
* repeated inline literals (agents hardcode the same value in many places)

These are **quality facts**, not security findings. They feed the VIBE
rule categories in the rule engine; the host oracle decides severity.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["scan_code_quality"]

SOURCE_SUFFIXES = {".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", "vendor"}
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "test-", "/tests/", "/test/")
MAX_FILE_LINES = 1200
GOD_FILE_LINES = 500
GOD_FUNCTION_LINES = 80
MAX_TODO_DENSITY = 0.02  # 2% of lines
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 2000
MAX_TOTAL_BYTES = 64 * 1024 * 1024

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|WORKAROUND)\b", re.IGNORECASE)
_SILENT_CATCH_RE = re.compile(
    r"(?:except\s*(?:Exception)?\s*(?:as\s+\w+)?\s*:\s*\n\s+pass"
    r"|except\s*\w*\s*:\s*pass"
    r"|catch\s*\([^)]*\)\s*\{\s*\}"
    r"|catch\s*\{)"
)
_MAGIC_NUMBER_RE = re.compile(r"(?<![.\w])(\d{3,})(?![.\w])")
_IMPORT_RE = re.compile(
    r"^(?:import\s+(\S+)|from\s+(\S+)\s+import)", re.MULTILINE
)


def _is_test(relative: str) -> bool:
    lowered = relative.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def _walk_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                return files
            files.append(path)
    return files


def _scan_python(path: Path, text: str, lines: list[str],
                 rel: str) -> dict[str, Any]:
    """Python-specific analysis using the ast module."""
    facts: dict[str, Any] = {}
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        facts["parse_error"] = True
        return facts

    # God functions
    god_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno
            if length > GOD_FUNCTION_LINES:
                god_functions.append({
                    "name": node.name, "line": node.lineno, "lines": length,
                })
    if god_functions:
        facts["god_functions"] = god_functions

    # Dead imports (imported but never referenced by name in the rest of the file)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports.add(name)
    used_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attr = node
            while isinstance(attr, ast.Attribute):
                attr = attr.value
            if isinstance(attr, ast.Name):
                used_names.add(attr.id)
    dead_imports = sorted(imports - used_names - {"*"})
    if dead_imports:
        facts["dead_imports"] = dead_imports

    return facts


def _scan_generic(text: str, lines: list[str], rel: str) -> dict[str, Any]:
    """Language-agnostic scans (regex-based)."""
    facts: dict[str, Any] = {}

    # Silent catch
    catches = []
    for i, line in enumerate(lines, start=1):
        if _SILENT_CATCH_RE.search(line):
            catches.append({"file": rel, "line": i})
            if len(catches) >= 20:
                break
    if catches:
        facts["silent_catches"] = catches

    # Repeated magic numbers (same number >= 3 times, excluding test files)
    counter: dict[str, int] = {}
    for line in lines:
        for m in _MAGIC_NUMBER_RE.finditer(line):
            counter[m.group(1)] = counter.get(m.group(1), 0) + 1
    hot = [(num, count) for num, count in counter.items() if count >= 5]
    hot.sort(key=lambda x: -x[1])
    if hot:
        facts["repeated_literals"] = [
            {"value": num, "count": count} for num, count in hot[:10]
        ]

    return facts


def scan_code_quality(root: Path) -> dict[str, Any]:
    """Scan a source tree for AI-generated code quality patterns.

    Returns bounded facts; the rule engine turns them into candidates,
    and the host oracle decides which are findings.
    """
    root = Path(root)
    if not root.is_dir():
        return {"status": "not_a_directory"}

    findings_by_file: list[dict[str, Any]] = []
    god_files: list[dict[str, Any]] = []
    todo_hotspots: list[dict[str, Any]] = []
    total_files = 0
    total_lines = 0
    truncated = False

    for path in _walk_source_files(root):
        relative = str(path.relative_to(root))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            truncated = True
            continue
        try:
            text = path.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        line_count = len(lines)
        total_files += 1
        total_lines += line_count

        entry: dict[str, Any] = {"path": relative, "lines": line_count,
                                 "bytes": size, "test": _is_test(relative)}

        # God file (skip tests)
        if not _is_test(relative) and line_count > GOD_FILE_LINES:
            god_files.append({"path": relative, "lines": line_count})

        # TODO/FIXME density (skip tests)
        if not _is_test(relative):
            todos = [(i, line.strip()[:200]) for i, line in enumerate(lines, 1)
                     if _TODO_RE.search(line)]
            density = len(todos) / max(line_count, 1)
            if todos and density > MAX_TODO_DENSITY:
                todo_hotspots.append({
                    "path": relative, "count": len(todos),
                    "density": round(density, 4),
                    "lines": [t[0] for t in todos[:10]],
                    "samples": [t[1] for t in todos[:5]],
                })

        # Language-specific analysis
        file_facts: dict[str, Any] = {}
        if path.suffix == ".py":
            file_facts = _scan_python(path, text, lines, relative)
        file_facts.update(_scan_generic(text, lines, relative))

        if file_facts:
            entry["issues"] = file_facts
            findings_by_file.append(entry)

    return {
        "status": "observed",
        "total_files": total_files,
        "total_lines": total_lines,
        "god_files": god_files,
        "todo_hotspots": todo_hotspots,
        "issue_files": findings_by_file,
        "truncated": truncated,
        "thresholds": {
            "god_file_lines": GOD_FILE_LINES,
            "god_function_lines": GOD_FUNCTION_LINES,
            "max_todo_density": MAX_TODO_DENSITY,
        },
    }