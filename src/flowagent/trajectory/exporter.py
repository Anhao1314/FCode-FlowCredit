"""Trace export and independent re-verification.

Two functions matter here.

``export_trace`` writes the document the viewer opens. It uses indented JSON for
readability, which is safe precisely because the hash chain is computed over
canonical JSON of the *event fields*, not over the file bytes. A reader who
reformats the file does not break verification; a reader who edits an event does.

``verify_trace`` is the same check the viewer performs in JavaScript, kept in
Python so a test can assert it without a browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.hashing import digest

__all__ = ["export_trace", "verify_trace", "TraceTampered"]


class TraceTampered(Exception):
    """Raised when a trace no longer matches its own hash chain."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def export_trace(trace: dict[str, Any], path: str | Path) -> Path:
    """Write *trace* as indented UTF-8 JSON and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def verify_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Recompute every event hash and check linkage plus the recorded head.

    Returns a small summary on success. Raises :class:`TraceTampered` with a
    stable code on failure, so the CLI and the tests can distinguish
    "edited event" from "missing event".
    """
    events = trace.get("events")
    if not isinstance(events, list) or not events:
        raise TraceTampered("TRACE_EMPTY", "the trace contains no events")

    previous: str | None = None
    for index, event in enumerate(events, start=1):
        if event.get("sequence") != index:
            raise TraceTampered("TRACE_GAP", f"event {index} is missing or out of order")
        if event.get("prev_hash") != previous:
            raise TraceTampered("TRACE_BROKEN", f"event {index} does not link to its predecessor")
        expected = digest(
            {
                "sequence": event["sequence"],
                "type": event["type"],
                "payload": event["payload"],
                "prev_hash": event["prev_hash"],
                "at": event["at"],
            }
        )
        if expected != event.get("hash"):
            raise TraceTampered("TRACE_TAMPERED", f"event {index} hash mismatch")
        previous = event["hash"]

    chain = trace.get("chain") or {}
    if chain.get("head_hash") != previous:
        raise TraceTampered("TRACE_HEAD_MISMATCH", "recorded head hash does not match the last event")
    if chain.get("event_count") != len(events):
        raise TraceTampered("TRACE_HEAD_MISMATCH", "recorded event count does not match")

    return {"verified": True, "events": len(events), "head_hash": previous}
