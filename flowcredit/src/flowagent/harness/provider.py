"""Streaming provider contract for the new harness core.

The core never talks to an HTTP client directly. A provider yields typed
stream events; the core assembles them into a message. That keeps the loop
testable with fakes and lets the real adapter (DeepSeek/OpenAI-compatible)
land in the migration stage without touching the loop.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, Union

__all__ = [
    "CollectedStream",
    "Done",
    "ReasoningDelta",
    "StreamEvent",
    "StreamRequest",
    "StreamTimeout",
    "StreamUsage",
    "StreamingProvider",
    "TextDelta",
    "ToolUse",
    "collect_stream",
]


@dataclass
class StreamUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0
    cached_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cost_microusd": int(self.cost_microusd),
            "cached_tokens": int(self.cached_tokens),
        }


@dataclass(frozen=True)
class StreamRequest:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    cancel: Any = None  # threading.Event; adapters should stop when set
    max_tokens: int | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Done:
    stop_reason: str = "end_turn"
    usage: StreamUsage = field(default_factory=StreamUsage)


StreamEvent = Union[TextDelta, ReasoningDelta, ToolUse, Done]


class StreamingProvider(Protocol):
    def stream(self, request: StreamRequest) -> Iterator[StreamEvent]:
        ...


class StreamTimeout(RuntimeError):
    """The provider stream exceeded its wall-clock deadline."""

    code = "CALL_DEADLINE"


@dataclass
class CollectedStream:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolUse] = field(default_factory=list)
    done: Done = field(default_factory=Done)


def collect_stream(
    provider: StreamingProvider,
    request: StreamRequest,
    *,
    deadline_ms: int,
) -> CollectedStream:
    """Consume one streamed model call with a wall-clock deadline.

    The provider iterator is pumped from a daemon thread so a stalled stream
    cannot block the caller past ``deadline_ms``. A timed-out pump thread is
    abandoned (Python threads cannot be force-killed); adapters should watch
    ``StreamRequest.cancel`` too.
    """
    if deadline_ms <= 0:
        raise ValueError("deadline_ms must be positive")
    cancel = request.cancel or threading.Event()
    pending: queue.Queue[tuple[str, Any]] = queue.Queue()
    stream = provider.stream(StreamRequest(
        system=request.system,
        messages=request.messages,
        tools=request.tools,
        cancel=cancel,
        max_tokens=request.max_tokens,
    ))

    def pump() -> None:
        try:
            for event in stream:
                pending.put(("event", event))
            pending.put(("end", None))
        except BaseException as error:  # noqa: BLE001 — relayed to the caller
            pending.put(("error", error))

    threading.Thread(target=pump, daemon=True, name="harness-stream").start()
    deadline = time.monotonic() + deadline_ms / 1000
    collected = CollectedStream()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel.set()
            raise StreamTimeout(f"model stream exceeded {deadline_ms}ms deadline")
        try:
            kind, item = pending.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue
        if kind == "error":
            raise item
        if kind == "end":
            break
        if isinstance(item, TextDelta):
            text_parts.append(item.text)
        elif isinstance(item, ReasoningDelta):
            reasoning_parts.append(item.text)
        elif isinstance(item, ToolUse):
            collected.tool_calls.append(item)
        elif isinstance(item, Done):
            collected.done = item
    collected.text = "".join(text_parts)
    collected.reasoning = "".join(reasoning_parts)
    return collected
