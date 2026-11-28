"""Operator knowledge base: deterministic BM25 over explicitly configured docs.

This is the R1 retrieval layer for vertical agents: the operator names the
knowledge roots (policy docs, product specs, runbooks), the host indexes them,
and the model may only *search* them through a read-effect tool. It is
deliberately not an embedding store — no new dependency, no network, no paid
model — and it is not the workspace code index: knowledge sources are
host-configured, may live outside the run sandbox, and cite documents rather
than code.

Chunking is markdown-aware: heading blocks become chunks (so a citation names
the section), long bodies fall back to overlapping line windows. Every hit
returns its source, path, title, heading path and line range, so the model can
quote the evidence instead of inventing it.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from ...core.hashing import digest
from ...core.lexical import tokens_for

__all__ = [
    "KnowledgeSource",
    "KnowledgeIndex",
    "KNOWLEDGE_INDEX_VERSION",
    "build_knowledge_context",
    "knowledge_sources_from_env",
    "summarize_search_knowledge",
]

KNOWLEDGE_INDEX_VERSION = "1.0"

#: Caps mirror the workspace index: a pathological corpus degrades honestly
#: (``truncated=true``) instead of taking the run down.
MAX_FILES = 2_000
MAX_CHUNKS = 8_000
MAX_FILE_BYTES = 512_000
MAX_TOTAL_CHARS = 8_000_000
MAX_QUERY_TERMS = 12
SNIPPET_CHARS = 640
#: Heading blocks larger than this are split into line windows so one huge
#: section cannot dominate the length normalisation.
MAX_HEADING_CHARS = 1_600
WINDOW_LINES = 60
OVERLAP_LINES = 12

_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
_SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}
_SENSITIVE_NAMES = {
    "credentials", "credentials.json", "secrets", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class KnowledgeSource:
    """One host-configured knowledge root. The model never names paths here."""

    name: str
    root: Path


@dataclass(frozen=True)
class _Chunk:
    source: str
    path: str
    title: str
    headings: str
    start_line: int
    end_line: int
    text: str


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


def _split_long_body(source: str, path: str, title: str, headings: str,
                     start_line: int, body: str) -> list[_Chunk]:
    """Fallback windows for a heading block that is too large to be one chunk."""
    lines = body.splitlines()
    chunks: list[_Chunk] = []
    step = max(1, WINDOW_LINES - OVERLAP_LINES)
    offset = 0
    while offset < len(lines):
        window = lines[offset:offset + WINDOW_LINES]
        text = "\n".join(window)
        if text.strip():
            chunks.append(_Chunk(
                source=source, path=path, title=title, headings=headings,
                start_line=start_line + offset,
                end_line=start_line + offset + len(window) - 1,
                text=text,
            ))
        offset += step
        if offset + WINDOW_LINES > len(lines) and chunks:
            break
    return chunks or [_Chunk(
        source=source, path=path, title=title, headings=headings,
        start_line=start_line, end_line=start_line, text=body,
    )]


def _chunk_markdown(source: str, path: str, text: str) -> list[_Chunk]:
    """Heading-aware chunks: ``# Title`` opens the document, ``## Section``
    opens a section; code fences do not start new headings."""
    lines = text.splitlines()
    chunks: list[_Chunk] = []
    title = Path(path).stem
    headings: list[str] = []
    block_start = 0
    in_fence = False

    def flush(end_line: int) -> None:
        body = "\n".join(lines[block_start:end_line]).strip()
        if not body:
            return
        heading_path = " > ".join(headings) if headings else ""
        if len(body) <= MAX_HEADING_CHARS:
            chunks.append(_Chunk(
                source=source, path=path, title=title, headings=heading_path,
                start_line=block_start + 1, end_line=end_line, text=body,
            ))
        else:
            chunks.extend(_split_long_body(
                source, path, title, heading_path, block_start + 1, body,
            ))

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(stripped)
        if match is None:
            continue
        flush(index)
        level = len(match.group(1))
        label = match.group(2).strip()
        headings = headings[:max(0, level - 1)]
        headings.append(label)
        if level == 1 and title == Path(path).stem:
            title = label
        block_start = index
    flush(len(lines))
    return chunks


class KnowledgeIndex:
    """BM25 over the operator's configured knowledge roots. Thread-safe."""

    def __init__(self, sources: list[KnowledgeSource]) -> None:
        self.sources = list(sources)
        self._lock = threading.Lock()
        self._chunks: list[_Chunk] = []
        self._postings: dict[str, dict[int, int]] = {}
        self._lengths: list[int] = []
        self._title_tokens: list[set[str]] = []
        self._avgdl = 0.0
        self._files = 0
        self._truncated = False
        self._built = False
        self._build_ms = 0

    # ------------------------------------------------------------------ build

    def _iter_files(self, source: KnowledgeSource):
        root = source.root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in _SKIP_DIRS and not name.startswith(".")
            )
            for name in sorted(filenames):
                lowered = name.lower()
                suffix = Path(name).suffix.lower()
                stem = Path(name).stem.lower()
                if lowered in _SKIP_NAMES or lowered in _SENSITIVE_NAMES \
                        or stem in _SENSITIVE_NAMES \
                        or suffix in _SENSITIVE_SUFFIXES or suffix not in _SUFFIXES:
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
            for source in self.sources:
                if files >= MAX_FILES or len(chunks) >= MAX_CHUNKS \
                        or total_chars >= MAX_TOTAL_CHARS:
                    truncated = True
                    break
                for path in self._iter_files(source):
                    if files >= MAX_FILES or len(chunks) >= MAX_CHUNKS \
                            or total_chars >= MAX_TOTAL_CHARS:
                        truncated = True
                        break
                    text = _read_text(path)
                    if text is None:
                        continue
                    try:
                        relative = path.relative_to(source.root).as_posix()
                    except ValueError:
                        relative = path.name
                    for chunk in _chunk_markdown(source.name, relative, text):
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
            self._title_tokens = [set(tokens_for(f"{chunk.title} {chunk.headings}"))
                                  for chunk in chunks]
            self._avgdl = (sum(lengths) / len(lengths)) if lengths else 0.0
            self._files = files
            self._truncated = truncated
            self._build_ms = int((time.perf_counter() - started) * 1000)
            self._built = True

    def _ensure(self) -> None:
        if not self._built:
            self.build()

    # ----------------------------------------------------------------- search

    def _score(self, chunk_id: int, terms: list[str], phrase: str) -> float:
        total = 0.0
        length = self._lengths[chunk_id] or 1
        total_chunks = max(1, len(self._chunks))
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            tf = postings.get(chunk_id, 0)
            if tf:
                df = len(postings)
                idf = math.log(1.0 + (total_chunks - df + 0.5) / (df + 0.5))
                denom = tf + 1.2 * (1.0 - 0.75 + 0.75 * length / (self._avgdl or 1.0))
                total += idf * (tf * (1.2 + 1.0)) / denom
            if term in self._title_tokens[chunk_id]:
                total += 1.2
        if phrase and len(phrase) >= 3 and phrase in self._chunks[chunk_id].text.lower():
            total += 4.0
        return total

    @staticmethod
    def _snippet(text: str, terms: list[str]) -> tuple[str, int]:
        low = text.lower()
        positions = [low.find(term) for term in terms if low.find(term) >= 0]
        pos = min(positions) if positions else 0
        start = max(0, pos - 180)
        end = min(len(text), start + SNIPPET_CHARS)
        line_start = text.rfind("\n", 0, start)
        if line_start >= 0:
            start = line_start + 1
        body = text[start:end].rstrip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return prefix + body + suffix, text.count("\n", 0, start)

    def search(self, query: str, *, limit: int = 6,
               source: str = "") -> dict[str, Any]:
        self._ensure()
        terms: list[str] = []
        for token in tokens_for(query):
            if token not in terms:
                terms.append(token)
            if len(terms) >= MAX_QUERY_TERMS:
                break
        if not terms:
            raise AgentError(
                "KNOWLEDGE_QUERY_INVALID",
                "query must contain at least one letter, number, or CJK phrase",
            )
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = 6
        wanted = max(1, min(wanted, 10))
        wanted_source = str(source or "").strip()

        candidates: set[int] = set()
        for term in terms:
            candidates.update(self._postings.get(term, {}))
        phrase = query.strip().lower()
        scored: list[tuple[float, int]] = []
        for chunk_id in candidates:
            chunk = self._chunks[chunk_id]
            if wanted_source and chunk.source != wanted_source:
                continue
            scored.append((self._score(chunk_id, terms, phrase), chunk_id))
        scored.sort(key=lambda item: (-item[0], self._chunks[item[1]].source,
                                      self._chunks[item[1]].path,
                                      self._chunks[item[1]].start_line))
        results = []
        for score, chunk_id in scored[:wanted]:
            chunk = self._chunks[chunk_id]
            snippet, offset = self._snippet(chunk.text, terms)
            start_line = chunk.start_line + offset
            results.append({
                "source": chunk.source,
                "path": chunk.path,
                "title": chunk.title,
                "headings": chunk.headings,
                "start_line": start_line,
                "end_line": start_line + max(0, snippet.count("\n")),
                # BM25 is only an internal ranking signal. Publish it as integer
                # basis points: the observation enters the hash chain, and the
                # evidence store rejects floats.
                "score_bp": int(round(score * 10_000)),
                "snippet": snippet,
            })
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "sources": [
                {"name": item.name, "root": str(item.root)}
                for item in self.sources
            ],
            "indexed_files": self._files,
            "indexed_chunks": len(self._chunks),
            "truncated": self._truncated,
            "index_ms": self._build_ms,
        }


def knowledge_sources_from_env(raw: str | None = None) -> list[KnowledgeSource]:
    """Parse ``FCODE_KNOWLEDGE_ROOTS`` (``name=path`` pairs or bare paths).

    Host configuration only: the model never names knowledge roots. A bare
    path gets its directory name as the source label; invalid entries are
    skipped with no error so a stale mount cannot take a run down.
    """
    text = (raw if raw is not None else os.environ.get("FCODE_KNOWLEDGE_ROOTS", "")).strip()
    if not text:
        return []
    sources: list[KnowledgeSource] = []
    seen: set[str] = set()
    for part in text.split(os.pathsep):
        entry = part.strip()
        if not entry:
            continue
        name, sep, path_text = entry.partition("=")
        if sep:
            name = name.strip() or Path(path_text.strip()).name
            path_text = path_text.strip()
        else:
            path_text = entry
            name = Path(entry).name or entry
        root = Path(path_text).expanduser().resolve()
        if not root.is_dir() or str(root) in seen:
            continue
        seen.add(str(root))
        sources.append(KnowledgeSource(name=name, root=root))
    return sources


def summarize_search_knowledge(result: dict[str, Any]) -> dict[str, Any]:
    """Keep the query and the top citations; the chain keeps the full result."""
    results = result.get("results") or []
    return {
        "query": result.get("query"),
        "count": result.get("count"),
        "indexed_files": result.get("indexed_files"),
        "indexed_chunks": result.get("indexed_chunks"),
        "truncated": result.get("truncated"),
        "results": [
            {
                "source": item.get("source"),
                "path": item.get("path"),
                "title": item.get("title"),
                "headings": item.get("headings"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "snippet": str(item.get("snippet") or "")[:360],
            }
            for item in results[:3]
        ],
    }


CONTEXT_MAX_RESULTS = 3
CONTEXT_MAX_CHARS = 2_400


def build_knowledge_context(
    sources: list[KnowledgeSource] | tuple[KnowledgeSource, ...],
    query: str,
    *,
    max_results: int = CONTEXT_MAX_RESULTS,
    max_chars: int = CONTEXT_MAX_CHARS,
) -> dict[str, Any] | None:
    """Prefetch a bounded knowledge block for a Work objective.

    `search_knowledge` is model-initiated; this is the host-initiated baseline.
    The point is that a Work delegation should not burn its first rounds
    discovering that policy/runbook knowledge exists. Returns ``None`` when
    there are no sources, no query, or no useful hits: an empty block must not
    masquerade as context.

    The block is deterministic for a given corpus/query, bounded by characters,
    and hashed so the run binding can prove exactly which prefetched context
    entered the prompt.
    """
    usable = [item for item in (sources or []) if isinstance(item, KnowledgeSource)]
    objective = str(query or "").strip()
    if not usable or not objective:
        return None
    index = KnowledgeIndex(usable)
    result = index.search(objective, limit=max_results)
    hits = [item for item in (result.get("results") or []) if item.get("snippet")]
    if not hits:
        return None

    lines: list[str] = [
        "【知识库上下文】以下内容来自操作员配置的知识库，只是候选证据；",
        "引用前可用 read_file_raw / read_file 打开原文件确认。",
    ]
    kept: list[dict[str, Any]] = []
    truncated = False
    for hit in hits:
        citation = {
            "source": hit.get("source"),
            "path": hit.get("path"),
            "title": hit.get("title"),
            "headings": hit.get("headings"),
            "start_line": hit.get("start_line"),
            "end_line": hit.get("end_line"),
            "score_bp": hit.get("score_bp"),
            "snippet": str(hit.get("snippet") or ""),
        }
        section = (
            f"- [{citation['source']}] {citation['path']}"
            + (f" · {citation['headings']}" if citation["headings"] else "")
            + f" · lines {citation['start_line']}-{citation['end_line']}\n"
            + f"  {citation['snippet']}"
        )
        candidate = "\n".join([*lines, section])
        if len(candidate) > max_chars:
            truncated = True
            break
        lines.append(section)
        kept.append(citation)
    if not kept:
        return None
    block = "\n".join(lines)
    return {
        "query": objective,
        "block": block,
        "chars": len(block),
        "truncated": truncated,
        "citations": kept,
        "indexed_files": result.get("indexed_files"),
        "indexed_chunks": result.get("indexed_chunks"),
        # sha256 of the exact prompt block, so the binding proves the bytes.
        "block_sha256": digest(block),
        "index_ms": result.get("index_ms"),
    }
