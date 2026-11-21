"""Context compaction (S3): thresholded 9-section summary with boundaries.

Ported behavior from the dissected harness:

* auto-compaction triggers from real usage against an effective budget:
  ``min(window * 0.95, window - reserve)`` floored at 100k tokens, and is
  disabled for small windows;
* the summarizer first drafts ``<analysis>`` (a scratchpad) and then emits a
  fixed nine-section ``<summary>``;
* when a summary is regenerated, the earlier summary's *All user messages*
  section is carried over verbatim (``keepEarlierAsks``);
* a ``compact_boundary`` event carries before/after token counts and the
  summary hash so the session tree can explain the jump.

The summarizer call is a plain text completion (no tools) and shares the
stream deadline used by the loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .provider import (
    StreamRequest,
    StreamUsage,
    StreamingProvider,
    collect_stream,
)

__all__ = [
    "COMPACTION_MARKER",
    "Compaction",
    "CompactionConfig",
    "CompactionError",
    "CompactionResult",
    "SUMMARY_SECTIONS",
    "SUMMARY_SYSTEM",
    "build_summary_prompt",
    "ensure_earlier_asks",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "extract_section",
    "render_conversation",
    "strip_analysis",
]

SUMMARY_SECTIONS = (
    "Primary Request and Intent",
    "Key Technical Concepts",
    "Files and Code Sections",
    "Errors and fixes",
    "Problem Solving",
    "All user messages",
    "Pending Tasks",
    "Current Work",
    "Optional Next Step",
)

SUMMARY_SYSTEM = (
    "You summarize an agent conversation so a fresh context can continue the "
    "work without the original transcript. Be concrete and keep every "
    "identifier (paths, commands, hashes, test names) intact."
)

COMPACTION_MARKER = (
    "This session is being continued from a previous conversation. "
    "The summary below covers the earlier portion of the work."
)

_SECTION_NUMBERS = {title: index + 1 for index, title in enumerate(SUMMARY_SECTIONS)}

TokenCounter = Callable[[Sequence[Mapping[str, Any]]], int]


class CompactionError(RuntimeError):
    code = "COMPACT_FAILED"


@dataclass(frozen=True)
class CompactionConfig:
    window_tokens: int = 200_000
    threshold: float = 0.95
    reserve_tokens: int = 60_000
    min_trigger_tokens: int = 100_000
    idle_compact_seconds: int = 3600
    summary_max_tokens: int = 8192
    deadline_ms: int = 120_000
    max_summary_input_chars: int = 400_000
    max_block_chars: int = 20_000

    @property
    def trigger_tokens(self) -> int | None:
        """Effective auto-compaction trigger, or ``None`` when disabled."""
        if self.window_tokens <= self.min_trigger_tokens:
            return None
        budget = max(1, self.window_tokens - self.reserve_tokens)
        return max(
            self.min_trigger_tokens,
            min(int(self.window_tokens * self.threshold), budget),
        )


@dataclass
class CompactionResult:
    summary: str
    messages: list[dict[str, Any]]
    boundary: dict[str, Any]
    usage: StreamUsage = field(default_factory=StreamUsage)
    before_tokens: int = 0
    after_tokens: int = 0


def estimate_text_tokens(text: str) -> int:
    """Cheap deterministic estimate: ~4 ASCII chars/token, 1 per CJK char."""
    value = str(text or "")
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    other = len(value) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + other)


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        total += estimate_text_tokens(text) + 4
    return total


def render_conversation(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_block_chars: int = 20_000,
) -> str:
    """Flatten a conversation into summarizer-readable text, bounded."""
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"{role.upper()}: {_clip(content, max_block_chars)}")
            continue
        if not isinstance(content, list):
            lines.append(f"{role.upper()}: {_clip(str(content), max_block_chars)}")
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            kind = str(block.get("type") or "text")
            if kind == "text":
                lines.append(f"{role.upper()}: {_clip(str(block.get('text') or ''), max_block_chars)}")
            elif kind == "tool_use":
                payload = json.dumps(dict(block.get("input") or {}), ensure_ascii=False)
                lines.append(
                    f"{role.upper()} [tool_use {block.get('name')}]: "
                    f"{_clip(payload, max_block_chars)}")
            elif kind == "tool_result":
                flag = " (error)" if block.get("is_error") else ""
                lines.append(
                    f"TOOL_RESULT{flag} [{block.get('tool_use_id')}]: "
                    f"{_clip(str(block.get('content') or ''), max_block_chars)}")
            elif kind == "reasoning":
                continue
            else:
                lines.append(f"{role.upper()} [{kind}]: "
                             f"{_clip(json.dumps(block, ensure_ascii=False), max_block_chars)}")
    return "\n".join(lines)


def build_summary_prompt(
    conversation: str,
    *,
    earlier_summary: str = "",
    max_chars: int = 400_000,
) -> str:
    sections = "\n".join(
        f"{index + 1}. {title}" for index, title in enumerate(SUMMARY_SECTIONS)
    )
    parts = [
        "Your task is to create a detailed summary of the conversation so far.",
        "First draft a private analysis inside <analysis>...</analysis>: "
        "chronology, every user request, decisions, errors and fixes, and what "
        "is still pending. Then write the final summary inside "
        "<summary>...</summary>.",
        "The summary must contain exactly these sections, numbered and titled:",
        sections,
        "Section 6 must list every user message, quoted as closely as possible.",
    ]
    earlier = str(earlier_summary or "").strip()
    if earlier:
        old_asks = extract_section(earlier, "All user messages")
        parts.append(
            "This conversation was compacted before. The earlier summary is below.")
        parts.append(f"<earlier_summary>\n{_clip(earlier, max_chars // 2)}\n</earlier_summary>")
        if old_asks:
            parts.append(
                "Its 'All user messages' section is authoritative. Carry every "
                "entry over verbatim into section 6 of the new summary:")
            parts.append(f"<earlier_user_messages>\n{_clip(old_asks, max_chars // 4)}\n"
                         "</earlier_user_messages>")
    parts.append("<conversation>")
    parts.append(_clip(conversation, max_chars))
    parts.append("</conversation>")
    return "\n\n".join(parts)


class Compaction:
    def __init__(
        self,
        provider: StreamingProvider,
        *,
        config: CompactionConfig | None = None,
        system: str = SUMMARY_SYSTEM,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or CompactionConfig()
        self.system = system
        self._count = token_counter or estimate_messages_tokens

    # ------------------------------------------------------------------ trigger

    def estimate(self, messages: Sequence[Mapping[str, Any]]) -> int:
        return int(self._count(messages))

    def should_compact(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        idle_seconds: int = 0,
    ) -> bool:
        trigger = self.config.trigger_tokens
        if trigger is None:
            return False
        if int(idle_seconds) >= self.config.idle_compact_seconds > 0:
            # Idle compaction is for real transcripts, not a one-line chat.
            return self.estimate(messages) >= 1000
        return self.estimate(messages) >= trigger

    # ---------------------------------------------------------------- compaction

    def compact(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        earlier_summary: str = "",
        trigger: str = "auto",
    ) -> CompactionResult:
        before_tokens = self.estimate(messages)
        earlier = str(earlier_summary or "").strip() or _existing_summary(messages)
        prompt = build_summary_prompt(
            render_conversation(
                messages, max_block_chars=self.config.max_block_chars),
            earlier_summary=earlier,
            max_chars=self.config.max_summary_input_chars,
        )
        collected = collect_stream(
            self.provider,
            StreamRequest(
                system=self.system,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt}]}],
                tools=[],
                max_tokens=self.config.summary_max_tokens,
            ),
            deadline_ms=self.config.deadline_ms,
        )
        if collected.tool_calls:
            raise CompactionError("summarizer attempted a tool call")
        summary = strip_analysis(collected.text)
        if not summary:
            raise CompactionError("summarizer returned an empty summary")
        if earlier:
            summary = ensure_earlier_asks(summary, earlier)
        continuation = _continuation_message(summary)
        summary_sha = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        boundary = {
            "type": "compact_boundary",
            "trigger": trigger,
            "before_tokens": int(before_tokens),
            "after_tokens": int(estimate_messages_tokens(continuation)),
            "summary_sha256": summary_sha,
            "sections": len(SUMMARY_SECTIONS),
        }
        return CompactionResult(
            summary=summary,
            messages=continuation,
            boundary=boundary,
            usage=collected.done.usage,
            before_tokens=before_tokens,
            after_tokens=boundary["after_tokens"],
        )


# --------------------------------------------------------------------- summary text


def strip_analysis(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?is)<analysis>.*?</analysis>", "", value)
    match = re.search(r"(?is)<summary>(.*?)</summary>", value)
    if match:
        value = match.group(1)
    return value.strip()


def extract_section(summary: str, title: str) -> str:
    pattern = re.compile(
        rf"(?ims)^\s*\d+[.)]\s*{re.escape(title)}\s*:?[ \t]*\n"
        rf"(.*?)(?=^\s*\d+[.)]\s*[A-Za-z]|\Z)"
    )
    match = pattern.search(str(summary or ""))
    return match.group(1).strip() if match else ""


def replace_section(summary: str, title: str, body: str) -> str:
    text = str(summary or "")
    pattern = re.compile(
        rf"(?ims)(^\s*\d+[.)]\s*{re.escape(title)}\s*:?[ \t]*\n)(.*?)"
        rf"(?=^\s*\d+[.)]\s*[A-Za-z]|\Z)"
    )
    if pattern.search(text):
        return pattern.sub(
            lambda match: match.group(1) + body.strip() + "\n\n", text, count=1).strip()
    number = _SECTION_NUMBERS.get(title, 10)
    return f"{text.rstrip()}\n\n{number}. {title}:\n{body.strip()}".strip()


def ensure_earlier_asks(summary: str, earlier_summary: str) -> str:
    """``keepEarlierAsks``: never lose the earlier summary's user asks."""
    old = extract_section(earlier_summary, "All user messages")
    if not old:
        return summary
    new = extract_section(summary, "All user messages")
    if old in new:
        return summary
    merged = old if not new else f"{old}\n{new}"
    return replace_section(summary, "All user messages", merged)


def _existing_summary(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in messages:
        text = _message_text(message.get("content"))
        if COMPACTION_MARKER not in text:
            continue
        match = re.search(r"(?is)<summary>(.*?)</summary>", text)
        if match:
            return match.group(1).strip()
    return ""


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return str(content or "")


def _continuation_message(summary: str) -> list[dict[str, Any]]:
    return [{
        "role": "user",
        "content": [{"type": "text", "text": (
            f"{COMPACTION_MARKER}\n\n<summary>\n{summary}\n</summary>\n\n"
            "Continue from where it left off. Do not repeat completed work."
        )}],
    }]


def _clip(text: str, limit: int) -> str:
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    half = max(1, limit // 2)
    return (value[:half] + "\n…[truncated]…\n" + value[-half:])
