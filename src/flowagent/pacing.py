"""Process-wide dispatch pacing, shared by the agent and chat stacks.

Kimi's organization RPM limit belongs to the endpoint, not to the caller. The
delegation engine and the chat surface used to keep separate timers, so two
callers could dispatch inside the same interval and collect 429s. This module
keeps one reservation table per ``(provider, base URL)`` and hands out slots
to whoever asks first.

A reservation is not a sleep: the caller learns how long it must wait, and the
slot is already booked for it. Concurrent callers therefore queue instead of
all reading the same "last call" timestamp and dispatching together.

The clock is ``time.monotonic``; the table is process-local. A fresh process
starts empty, which is why the agent engine can ask its first dispatch to wait
a conservative full interval (a sibling worker may have just called), while
the interactive chat surface starts immediately.
"""

from __future__ import annotations

import threading
import time
from typing import Any

__all__ = ["pace_key", "reserve_slot", "reset", "snapshot"]

_LOCK = threading.Lock()
#: key -> the earliest monotonic time at which the next caller may dispatch.
_NEXT: dict[str, float] = {}


def pace_key(provider: str, base_url: str) -> str:
    """One key per endpoint: the same provider at two URLs is two limits."""
    return f"{str(provider or '').strip().lower()}|{str(base_url or '').strip().rstrip('/')}"


def reserve_slot(
    key: str,
    interval: float,
    *,
    first_call_waits: bool = False,
    now: float | None = None,
) -> float:
    """Reserve the next dispatch slot and return the seconds to wait.

    ``interval <= 0`` disables pacing. With no entry for *key*,
    ``first_call_waits`` decides whether the first caller waits a full interval
    or may start immediately; later callers always wait until the previous
    reservation plus one interval.
    """
    if interval <= 0:
        return 0.0
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        target = _NEXT.get(key)
        if target is None:
            target = moment + interval if first_call_waits else moment
        target = max(moment, target)
        _NEXT[key] = target + interval
    return target - moment


def reset(key: str | None = None) -> None:
    """Forget reservations, for tests and for an explicit operator reset."""
    with _LOCK:
        if key is None:
            _NEXT.clear()
        else:
            _NEXT.pop(key, None)


def snapshot(now: float | None = None) -> dict[str, Any]:
    """A read-only view for diagnostics: key -> seconds until its next slot."""
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        return {key: max(0.0, target - moment) for key, target in _NEXT.items()}
