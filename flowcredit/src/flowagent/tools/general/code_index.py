"""The workspace's semantic-ish index: BM25 over code and text chunks.

This is deliberately not an embedding store. It is the first retrieval layer
that works offline, deterministically, with no new dependency:

* every text/code file is split into overlapping line windows;
* identifiers are indexed whole and split on ``_`` / camelCase, so a query for
  ``calculate invoice`` finds ``calculate_invoice``;
* CJK runs contribute bigrams, so ``计价口径`` finds ``订单计价口径``;
* ranking is BM25 with a small path boost and an exact-phrase bonus.

The index is built lazily on the first search and cached for the lifetime of
the tool registry (one run, or one chat service). It is a read-only snapshot:
files written after the first search are visible through grep/read_file, not
through this index. Staying read-only keeps the governance story unchanged —
no new effect class, no new approval path.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from ...core.lexical import tokens_for

__all__ = ["WorkspaceIndex", "CODE_INDEX_VERSION"]

CODE_INDEX_VERSION = "1.0"

#: Caps keep a pathological repository from becoming a memory problem. Hitting
#: any cap sets ``truncated=True``; the model is told the index is partial.
MAX_FILES = 2_000
MAX_CHUNKS = 8_000
MAX_FILE_BYTES = 256_000
MAX_TOTAL_CHARS = 8_000_000
CHUNK_LINES = 60
OVERLAP_LINES = 12
MAX_QUERY_TERMS = 12
SNIPPET_CHARS = 560

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "work", "runs", "artifacts",
    "coverage", ".next", ".turbo",
}
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".md", ".mdx", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json",
    ".json5", ".ini", ".cfg", ".conf", ".css", ".scss", ".sass", ".less",
    ".html", ".htm", ".xml", ".svg", ".graphql", ".gql", ".proto", ".lua",
    ".r", ".m", ".mm", ".pl", ".ex", ".exs", ".erl", ".clj", ".cljs",
    ".gradle", ".properties", ".env.example",
}
_TEXT_NAMES = {
    "Dockerfile", "Makefile", "makefile", "Justfile", "Procfile", "Gemfile",
    "Rakefile", "Brewfile", "README", "LICENSE", "CHANGELOG", "AGENTS.md",
}
_SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Pipfile.lock", "composer.lock", "Cargo.lock", "go.sum",
}
_SENSITIVE_NAMES = {
    "credentials", "credentials.json", "secrets", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".pyc"}

@dataclass(frozen=True)
class _Chunk:
    path: str
    start_line: int
    end_line: int
    text: str


def _read_text(path: Path) -> str | None:
    """Read one UTF-8-ish text file, refusing binaries and oversize bodies."""
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


def _chunk_file(relative: str, text: str) -> list[_Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[_Chunk] = []
    step = max(1, CHUNK_LINES - OVERLAP_LINES)
    for start in range(0, len(lines), step):
        body = "\n".join(lines[start:start + CHUNK_LINES])
        if not body.strip():
            continue
        chunks.append(_Chunk(
            path=relative,
            start_line=start + 1,
            end_line=min(len(lines), start + CHUNK_LINES),
            text=body,
        ))
        if start + CHUNK_LINES >= len(lines):
            break
    return chunks


class WorkspaceIndex:
    """One workspace's lazily-built BM25 index. Thread-safe by one lock."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._chunks: list[_Chunk] = []
        self._postings: dict[str, dict[int, int]] = {}
        self._lengths: list[int] = []
        self._path_tokens: list[set[str]] = []
        self._avgdl = 0.0
        self._files = 0
        self._truncated = False
        self._built = False
        self._build_ms = 0

    # ------------------------------------------------------------------ build

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name in {".github"} or (name not in _SKIP_DIRS and not name.startswith("."))
            )
            for name in sorted(filenames):
                if name in _SKIP_NAMES:
                    continue
                lowered = name.lower()
                suffix = Path(name).suffix.lower()
                if lowered == ".env" or lowered.startswith(".env.") \
                        or lowered in _SENSITIVE_NAMES \
                        or suffix in _SENSITIVE_SUFFIXES:
                    continue
                if suffix not in _TEXT_SUFFIXES and name not in _TEXT_NAMES:
                    continue
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                yield path

    def build(self) -> None:
        with self._lock:
            if self._built:
                return
            started = time.perf_counter()
            total_chars = 0
            chunks: list[_Chunk] = []
            files = 0
            truncated = False
            for path in self._iter_files():
                if files >= MAX_FILES or len(chunks) >= MAX_CHUNKS or total_chars >= MAX_TOTAL_CHARS:
                    truncated = True
                    break
                text = _read_text(path)
                if text is None:
                    continue
                relative = path.relative_to(self.root).as_posix()
                for chunk in _chunk_file(relative, text):
                    chunks.append(chunk)
                    total_chars += len(chunk.text)
                    if len(chunks) >= MAX_CHUNKS or total_chars >= MAX_TOTAL_CHARS:
                        truncated = True
                        break
                files += 1
                if truncated:
                    break
            postings: dict[str, dict[int, int]] = {}
            lengths: list[int] = []
            for chunk_id, chunk in enumerate(chunks):
                counts = Counter(tokens_for(chunk.text))
                lengths.append(sum(counts.values()))
                for token, count in counts.items():
                    postings.setdefault(token, {})[chunk_id] = count
            self._chunks = chunks
            self._postings = postings
            self._lengths = lengths
            self._path_tokens = [set(tokens_for(chunk.path)) for chunk in chunks]
            self._avgdl = (sum(lengths) / len(lengths)) if lengths else 0.0
            self._files = files
            self._truncated = truncated
            self._build_ms = int((time.perf_counter() - started) * 1000)
            self._built = True

    # ----------------------------------------------------------------- search

    def _ensure(self) -> None:
        if not self._built:
            self.build()

    def _score(self, chunk_id: int, terms: list[str]) -> float:
        total = 0.0
        length = self._lengths[chunk_id] or 1
        total_chunks = max(1, len(self._chunks))
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            tf = postings.get(chunk_id, 0)
            if not tf:
                continue
            df = len(postings)
            idf = math.log(1.0 + (total_chunks - df + 0.5) / (df + 0.5))
            denom = tf + 1.2 * (1.0 - 0.75 + 0.75 * length / (self._avgdl or 1.0))
            total += idf * (tf * (1.2 + 1.0)) / denom
            if term in self._path_tokens[chunk_id]:
                total += 0.8
        return total

    @staticmethod
    def _snippet(text: str, terms: list[str]) -> tuple[str, int]:
        low = text.lower()
        positions = [low.find(term) for term in terms if low.find(term) >= 0]
        pos = min(positions) if positions else 0
        start = max(0, pos - 160)
        end = min(len(text), start + SNIPPET_CHARS)
        line_start = text.rfind("\n", 0, start)
        if line_start >= 0:
            start = line_start + 1
        body = text[start:end].rstrip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return prefix + body + suffix, text.count("\n", 0, start)

    def search(self, query: str, *, limit: int = 8, subtree: str = "") -> dict[str, Any]:
        self._ensure()
        terms = []
        for token in tokens_for(query):
            if token not in terms:
                terms.append(token)
            if len(terms) >= MAX_QUERY_TERMS:
                break
        if not terms:
            raise AgentError(
                "SEARCH_QUERY_INVALID",
                "query must contain at least one letter, number, or CJK phrase",
            )
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = 8
        wanted = max(1, min(wanted, 12))
        prefix = subtree.strip().strip("/")
        if prefix:
            prefix += "/"

        candidates: set[int] = set()
        for term in terms:
            candidates.update(self._postings.get(term, {}))
        best_by_path: dict[str, tuple[float, int]] = {}
        phrase = query.strip().lower()
        for chunk_id in candidates:
            chunk = self._chunks[chunk_id]
            if prefix and not chunk.path.startswith(prefix):
                continue
            score = self._score(chunk_id, terms)
            if phrase and len(phrase) >= 3 and phrase in chunk.text.lower():
                score += 4.0
            current = best_by_path.get(chunk.path)
            if current is None or score > current[0]:
                best_by_path[chunk.path] = (score, chunk_id)

        ranked = sorted(
            best_by_path.items(),
            key=lambda item: (-item[1][0], item[0]),
        )[:wanted]
        results = []
        for path, (score, chunk_id) in ranked:
            chunk = self._chunks[chunk_id]
            snippet, offset = self._snippet(chunk.text, terms)
            start_line = chunk.start_line + offset
            results.append({
                "path": path,
                "start_line": start_line,
                "end_line": start_line + max(0, snippet.count("\n")),
                "score": round(score, 4),
                "snippet": snippet,
            })
        return {
            "query": query,
            "files": results,
            "count": len(results),
            "indexed_files": self._files,
            "indexed_chunks": len(self._chunks),
            "truncated": self._truncated,
            "index_ms": self._build_ms,
        }