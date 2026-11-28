"""Work coding context pack: a bounded first-round map of the repo.

`search_codebase` is model-initiated. This module gives Work a deterministic,
host-built starting point: candidate implementation files, related tests,
top-level symbols/imports and the declared verification command, selected from
the isolated workspace and bound to the task.

The pack is evidence, not authority. Every entry points at a file the model can
still read; no symbol or snippet is treated as a permission, and no block is
injected when the objective has no useful hits.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ...core.hashing import digest
from ...core.lexical import tokens_for
from .code_index import WorkspaceIndex

__all__ = ["build_code_context", "CODE_CONTEXT_VERSION"]

CODE_CONTEXT_VERSION = "1.0"

MAX_CANDIDATE_FILES = 5
MAX_TEST_FILES = 4
MAX_SYMBOL_FILES = 4
MAX_FILE_BYTES = 256_000
MAX_BLOCK_CHARS = 3_200
MAX_SNIPPET_CHARS = 420
MAX_QUERY_TERMS = 12

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".shell-home", ".shell-tmp", ".cache", ".next",
    ".nuxt", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "target", "out", "coverage", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
}
_SOURCE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".swift", ".c", ".cc",
    ".cpp", ".hpp", ".cs", ".sh",
}
_TEST_MARKERS = ("/tests/", "/test/", "test_", "test", "_test", ".test.", ".spec.")
_SENSITIVE_NAMES = {
    "credentials", "credentials.json", "secrets", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".pyc"}

_JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?"
    r"(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\())",
    re.MULTILINE,
)
_JS_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+[^;\n]+from\s+['\"]([^'\"]+)['\"]"
    r"|const\s+[^;\n]+=\s*require\(['\"]([^'\"]+)['\"]\))",
    re.MULTILINE,
)


def _read_text(path: Path) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > MAX_FILE_BYTES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    return (
        "tests" in parts
        or "test" in parts
        or any(part.startswith("test_") for part in parts)
        or any(part.endswith("_test") for part in parts)
        or ".test." in lowered
        or ".spec." in lowered
    )


def _is_sensitive(path: Path) -> bool:
    lowered = path.name.lower()
    suffix = path.suffix.lower()
    stem = path.stem.lower()
    return (
        lowered in _SENSITIVE_NAMES
        or stem in _SENSITIVE_NAMES
        or suffix in _SENSITIVE_SUFFIXES
    )


def _text_files(root: Path, *, max_files: int = 2_000) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or _is_sensitive(path):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            files.append(path)
            if len(files) >= max_files:
                return files
    return files


def _is_source(path: Path) -> bool:
    return path.suffix.lower() in _SOURCE_SUFFIXES


def _bp(score: float) -> int:
    """BM25 is a ranking signal only; publish integer basis points."""
    return int(round(max(0.0, float(score or 0.0)) * 10_000))


def _snippet(text: str, terms: list[str]) -> tuple[str, int, int]:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - 180)
    end = min(len(text), start + MAX_SNIPPET_CHARS)
    line_start = text.rfind("\n", 0, start)
    if line_start >= 0:
        start = line_start + 1
    body = text[start:end].rstrip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    first_line = text.count("\n", 0, start) + 1
    last_line = first_line + body.count("\n")
    return prefix + body + suffix, first_line, last_line


def _python_symbols(path: Path, content: str) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return [], []
    symbols: list[str] = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if len(symbols) < 16:
                symbols.append(f"{node.name}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if len(imports) < 10:
                imports.append(f"{node.module}:{node.lineno}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if len(imports) >= 10:
                    break
                imports.append(f"{alias.name}:{node.lineno}")
    return symbols, imports


def _js_symbols(content: str) -> tuple[list[str], list[str]]:
    symbols: list[str] = []
    for number, line in enumerate(content.splitlines(), start=1):
        match = _JS_SYMBOL_RE.match(line)
        if match:
            name = next((item for item in match.groups() if item), None)
            if name and len(symbols) < 16:
                symbols.append(f"{name}:{number}")
        if len(symbols) >= 16:
            break
    imports: list[str] = []
    for number, line in enumerate(content.splitlines(), start=1):
        match = _JS_IMPORT_RE.match(line)
        if match:
            module = next((item for item in match.groups() if item), "")
            if module and len(imports) < 10:
                imports.append(f"{module}:{number}")
        if len(imports) >= 10:
            break
    return symbols, imports


def _file_meta(root: Path, path: Path, *, score_bp: int, start_line: int,
               end_line: int, snippet: str, reason: str) -> dict[str, Any] | None:
    relative = path.relative_to(root).as_posix()
    sha = _sha256(path)
    if not sha:
        return None
    return {
        "path": relative,
        "sha256": sha,
        "bytes": path.stat().st_size,
        "score_bp": score_bp,
        "start_line": start_line,
        "end_line": end_line,
        "snippet": snippet[:MAX_SNIPPET_CHARS],
        "reason": reason,
    }


def _verification_record(verification: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(verification, dict):
        return None
    command = str(verification.get("command") or "").strip()
    if not command:
        return None
    args = [str(item) for item in (verification.get("args") or [])]
    return {"command": command, "args": args, "argv": [command, *args]}


def build_code_context(
    root: str | Path,
    objective: str,
    *,
    verification: dict[str, Any] | None = None,
    max_candidate_files: int = MAX_CANDIDATE_FILES,
    max_test_files: int = MAX_TEST_FILES,
    max_block_chars: int = MAX_BLOCK_CHARS,
) -> dict[str, Any] | None:
    """Build a bounded coding context pack for one Work objective.

    Returns ``None`` when the workspace has no useful hits or the objective has
    no searchable terms. This is fail-closed: an empty context must not be
    dressed up as repo knowledge.

    The returned ``block_sha256`` is the exact SHA-256 of the prompt block, so
    a task binding can prove which bytes entered the first round.
    """
    objective = str(objective or "").strip()
    if not objective:
        return None
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return None

    terms: list[str] = []
    for token in tokens_for(objective):
        if token not in terms:
            terms.append(token)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    if not terms:
        return None

    index = WorkspaceIndex(root_path)
    result = index.search(objective, limit=12)
    query_terms = list(terms)

    implementation: dict[str, dict[str, Any]] = {}
    tests: dict[str, dict[str, Any]] = {}
    for hit in result.get("files") or []:
        relative = str(hit.get("path") or "")
        path = root_path / relative
        if not path.is_file() or _is_sensitive(path):
            continue
        score_bp = _bp(hit.get("score"))
        snippet, first_line, last_line = _snippet(
            str(hit.get("snippet") or ""), query_terms,
        )
        meta = _file_meta(
            root_path, path, score_bp=score_bp,
            start_line=int(hit.get("start_line") or first_line),
            end_line=int(hit.get("end_line") or last_line),
            snippet=snippet,
            reason="objective match",
        )
        if meta is None:
            continue
        bucket = tests if _is_test_path(relative) else implementation
        current = bucket.get(relative)
        if current is None or meta["score_bp"] > current["score_bp"]:
            bucket[relative] = meta

    # Add tests whose filename/module matches an implementation candidate even
    # when BM25 ranked the implementation higher and omitted the test.
    candidates = sorted(
        implementation.values(),
        key=lambda item: (-item["score_bp"], item["path"]),
    )[:max_candidate_files]
    candidate_stems = {Path(item["path"]).stem.lower() for item in candidates}
    for path in _text_files(root_path):
        relative = path.relative_to(root_path).as_posix()
        if not _is_test_path(relative) or not _is_source(path):
            continue
        lowered = relative.lower()
        if not any(stem and stem in lowered for stem in candidate_stems):
            continue
        content = _read_text(path)
        if content is None:
            continue
        snippet, first_line, last_line = _snippet(content, query_terms)
        meta = _file_meta(
            root_path, path, score_bp=0,
            start_line=first_line, end_line=last_line, snippet=snippet,
            reason="matches candidate module",
        )
        if meta is not None and relative not in tests:
            tests[relative] = meta
    related_tests = sorted(
        tests.values(),
        key=lambda item: (-item["score_bp"], item["path"]),
    )[:max_test_files]

    if not candidates and not related_tests:
        return None

    symbol_files: list[dict[str, Any]] = []
    for item in candidates:
        path = root_path / item["path"]
        content = _read_text(path)
        if content is None:
            continue
        if path.suffix.lower() == ".py":
            symbols, imports = _python_symbols(path, content)
        elif path.suffix.lower() in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
            symbols, imports = _js_symbols(content)
        else:
            symbols, imports = [], []
        if not symbols and not imports:
            continue
        symbol_files.append({
            "path": item["path"],
            "symbols": symbols,
            "imports": imports,
        })
        if len(symbol_files) >= MAX_SYMBOL_FILES:
            break

    verification_record = _verification_record(verification)
    truncated = bool(result.get("truncated")) or len(
        result.get("files") or []
    ) > max_candidate_files + max_test_files

    lines = [
        "【代码上下文】以下是从当前工作区推导出的候选起点；",
        "编辑前用 read_file/read_file_raw 打开原文件确认，不要只依赖片段。",
    ]
    if candidates:
        lines.append("候选实现文件：")
        for item in candidates:
            symbols = next(
                (entry["symbols"] for entry in symbol_files
                 if entry["path"] == item["path"]),
                [],
            )
            suffix = f" · symbols {', '.join(symbols[:4])}" if symbols else ""
            lines.append(
                f"- {item['path']} · lines {item['start_line']}-{item['end_line']} "
                f"· score_bp {item['score_bp']}{suffix}\n  {item['snippet']}"
            )
    if related_tests:
        lines.append("相关测试：")
        for item in related_tests:
            lines.append(
                f"- {item['path']} · lines {item['start_line']}-{item['end_line']}\n"
                f"  {item['snippet']}"
            )
    if symbol_files:
        lines.append("导入/符号边：")
        for item in symbol_files:
            imports = ", ".join(item["imports"][:4]) or "—"
            lines.append(f"- {item['path']} · imports {imports}")
    if verification_record:
        lines.append(
            "验证命令（模型可运行公共检查；最终仍由宿主独立验证）："
            f"`{verification_record['argv']}`"
        )
    block = "\n".join(lines)
    if len(block) > max_block_chars:
        # The ellipsis is part of the declared bound, so the final block can
        # never exceed max_block_chars even when the last kept line ends in
        # whitespace or a combining character.
        block = block[:max(0, max_block_chars - 2)].rstrip() + "\n…"
        truncated = True

    return {
        "version": CODE_CONTEXT_VERSION,
        "query": objective,
        "block": block,
        "chars": len(block),
        "truncated": truncated,
        "block_sha256": digest(block),
        "candidate_files": candidates,
        "related_tests": related_tests,
        "symbol_edges": symbol_files,
        "verification": verification_record,
        "indexed_files": int(result.get("indexed_files") or 0),
        "indexed_chunks": int(result.get("indexed_chunks") or 0),
        "index_ms": int(result.get("index_ms") or 0),
    }
