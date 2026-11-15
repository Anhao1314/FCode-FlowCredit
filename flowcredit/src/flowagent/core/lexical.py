"""Shared lexical tokens for code retrieval and operator-memory recall.

One tokenizer keeps both indexes consistent: identifiers are indexed whole and
split on ``_`` / camelCase, CJK runs become bigrams, and common English
connectors are dropped. It is intentionally small and dependency-free; this is
not a replacement for embeddings, it is the deterministic baseline.
"""

from __future__ import annotations

import re

__all__ = ["tokens_for"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u3400-\u9fff]+", re.UNICODE)
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_CJK = re.compile(r"^[\u3400-\u9fff]+$")
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "be", "with", "this", "that", "it", "as", "by", "at", "from", "we", "you",
}


def _split_identifier(token: str) -> list[str]:
    """``parseActionBlock`` → itself, parse, action, block."""
    out = [token]
    if "_" in token:
        out.extend(part for part in token.split("_") if part)
    spaced = _CAMEL.sub(r"\1 \2", token)
    if spaced != token:
        out.extend(part for part in spaced.split() if part)
    return out


def tokens_for(text: str) -> list[str]:
    """Index/query tokens: identifiers, their parts, and CJK bigrams."""
    out: list[str] = []
    for raw in _WORD.findall(text or ""):
        lowered = raw.lower()
        if lowered in _STOP:
            continue
        if _CJK.match(raw) and len(raw) > 1:
            out.extend(raw[i:i + 2] for i in range(len(raw) - 1))
            continue
        for token in _split_identifier(raw):
            token = token.lower()
            if len(token) >= 2 or token.isdigit():
                out.append(token)
    return out