"""Ordered harness events (the hook seam).

Every lifecycle step emits one immutable event: turn boundaries, tool
describe/check/call/result, assistant messages and terminal states. S2 will
attach hook subscribers to this bus; S1 only records and forwards them.

Event payloads are plain JSON-able dictionaries with string keys and integer
timestamps: they are candidates for the hash chain, where ``canonical_json``
rejects floats by design.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

__all__ = ["EventBus", "HarnessEvent", "now_ms"]


def now_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass(frozen=True)
class HarnessEvent:
    type: str
    at_ms: int
    tool_use_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "at_ms": int(self.at_ms),
            "tool_use_id": self.tool_use_id,
            "payload": dict(self.payload),
        }


class EventBus:
    """Append-only event log plus best-effort subscribers.

    Subscriber failures must never break the loop: a broken hook is a
    side-channel problem, and the run's own result stays authoritative.
    """

    def __init__(self) -> None:
        self.history: list[HarnessEvent] = []
        self._subscribers: list[Callable[[HarnessEvent], None]] = []

    def subscribe(self, callback: Callable[[HarnessEvent], None]) -> None:
        self._subscribers.append(callback)

    def emit(
        self,
        event_type: str,
        *,
        tool_use_id: str = "",
        **payload: Any,
    ) -> HarnessEvent:
        event = HarnessEvent(
            type=event_type,
            at_ms=now_ms(),
            tool_use_id=tool_use_id,
            payload=dict(payload),
        )
        self.history.append(event)
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 — hooks are side channels
                continue
        return event

    def types(self) -> list[str]:
        return [event.type for event in self.history]
