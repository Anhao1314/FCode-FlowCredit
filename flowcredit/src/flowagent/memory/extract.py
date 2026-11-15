"""Conservative operator-memory extraction.

Only operator-authored text is eligible. The extractor never reads model
output, tool results, web pages or workspace files: a prompt injection that
reaches the model must not become a durable instruction.

The rules are deliberately narrow. A sentence is stored when it either says
"记住" explicitly, or contains a durable preference/decision marker such as
"以后都…", "我们项目统一…", "始终…", "改用…". Ordinary task instructions
("不要改测试", "修好这个 bug") stay in the conversation and are not promoted
to cross-thread memory.
"""

from __future__ import annotations

import re

__all__ = ["extract_memories", "MAX_AUTO_PER_MESSAGE"]

MAX_AUTO_PER_MESSAGE = 3
MAX_MEMORY_CHARS = 600

_SENTENCE = re.compile(r"(?<=[。！？!?；;\n])")
_EXPLICIT = re.compile(r"(?:请?记住|记一下|帮我记住|remember)\s*[:：,，]?\s*(.+)", re.IGNORECASE)
_DURABLE = re.compile(
    r"(?:以后|今后|后续|从今往后|下次|今后都).{0,50}"
    r"(?:都|一律|统一|默认|始终|总是|使用|采用|不要|别|必须|记得|改成|换成|用)"
    r"|(?:我们|本项目|这个项目|这个仓库|团队).{0,30}"
    r"(?:统一|约定|规范|标准|默认|一律|始终|使用|采用)"
    r"|(?:统一|一律|始终|总是|默认).{0,30}"
    r"(?:使用|采用|改成|换成|不要|别|必须|用)"
    r"|(?:改用|换成|统一成|纠正|修正).{0,50}",
)
_QUESTION = re.compile(r"[?？]\s*$")


def _clean(sentence: str) -> str:
    text = sentence.strip().strip("-—*• \t")
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_MEMORY_CHARS].strip()


def extract_memories(text: str) -> list[dict[str, object]]:
    """Return at most three high-confidence durable memories."""
    found: list[dict[str, object]] = []
    seen: set[str] = set()
    for sentence in _SENTENCE.split(str(text or "")):
        clean = _clean(sentence)
        if len(clean) < 6 or len(clean) > MAX_MEMORY_CHARS:
            continue
        explicit = _EXPLICIT.search(clean)
        if explicit:
            candidate = _clean(explicit.group(1))
            confidence = 0.95
        elif not _QUESTION.search(clean) and _DURABLE.search(clean):
            candidate = clean
            confidence = 0.6
        else:
            continue
        if len(candidate) < 4 or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        found.append({"text": candidate, "confidence": confidence})
        if len(found) >= MAX_AUTO_PER_MESSAGE:
            break
    return found