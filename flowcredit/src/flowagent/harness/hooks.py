"""Lifecycle hooks (S2): ten events, matchers, command execution.

Ported protocol from the dissected harness:

* hooks are operator-configured *shell commands* attached to a lifecycle
  event with an optional matcher (``Bash`` / ``Edit|Write`` / ``*``);
* the event payload is written to stdin as JSON;
* a command may block the action by exiting with status 2 (stderr becomes the
  reason), or by printing ``{"decision":"block","reason":...}``;
* a command may rewrite tool input by printing ``{"updatedInput": {...}}``;
* managed-source hooks run before user hooks, so organization policy wins.

The core consumes PreToolUse through the permission pipeline and PostToolUse
after a tool result; the runner itself is event-generic, so the other eight
events are ready for session/compaction wiring in later stages.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "HOOK_EVENTS",
    "HookOutcome",
    "HookRunner",
    "HookSpec",
]

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "SessionEnd",
)

_SOURCE_ORDER = ("managed", "project", "user", "local")
Executor = Callable[[str, bytes, float], tuple[int, str, str]]


@dataclass(frozen=True)
class HookSpec:
    event: str
    command: str
    matcher: str = ""
    timeout_ms: int = 60_000
    source: str = "user"

    def __post_init__(self) -> None:
        if self.event not in HOOK_EVENTS:
            raise ValueError(f"unknown hook event {self.event!r}")
        if not str(self.command or "").strip():
            raise ValueError("hook command must not be empty")

    def matches(self, tool: str | None) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        if tool is None:
            return False
        return any(
            _fnmatch(tool, part.strip())
            for part in self.matcher.split("|")
            if part.strip()
        )


@dataclass
class HookOutcome:
    blocked: bool = False
    reason: str = ""
    updated_input: Mapping[str, Any] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)


def _fnmatch(value: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(value, pattern)


def _default_execute(command: str, payload: bytes, timeout_s: float,
                     *, cwd: str | None, env: Mapping[str, str] | None) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        shell=True,
        input=payload,
        capture_output=True,
        text=False,
        timeout=timeout_s,
        cwd=cwd,
        env=dict(env) if env is not None else None,
    )
    return (
        completed.returncode,
        (completed.stdout or b"").decode("utf-8", "replace"),
        (completed.stderr or b"").decode("utf-8", "replace"),
    )


class HookRunner:
    def __init__(
        self,
        hooks: Iterable[HookSpec] = (),
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        execute: Callable[[str, bytes, float], tuple[int, str, str]] | None = None,
    ) -> None:
        self._hooks = list(hooks)
        self._cwd = cwd
        self._env = dict(os.environ) if env is None else dict(env)
        self._execute = execute or (
            lambda command, payload, timeout_s: _default_execute(
                command, payload, timeout_s, cwd=self._cwd, env=self._env))

    def add(self, hook: HookSpec) -> None:
        self._hooks.append(hook)

    def run(
        self,
        event: str,
        payload: Mapping[str, Any],
        *,
        tool: str | None = None,
    ) -> HookOutcome:
        """Run every matching hook in managed→user order, stopping on block."""
        matched = [hook for hook in self._ordered() if hook.event == event]
        outcome = HookOutcome()
        body = json.dumps({**payload, "event": event}, ensure_ascii=False).encode("utf-8")
        for hook in matched:
            if not hook.matches(tool):
                continue
            try:
                code, stdout, stderr = self._execute(
                    hook.command, body, hook.timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                outcome.results.append({
                    "command": hook.command, "ok": False,
                    "code": "HOOK_TIMEOUT", "source": hook.source,
                })
                continue
            parsed = _parse_output(stdout)
            record = {
                "command": hook.command, "source": hook.source,
                "exit_code": int(code), "ok": code == 0,
            }
            if parsed.get("updatedInput") is not None:
                record["updated_input"] = True
                outcome.updated_input = dict(parsed["updatedInput"])
            decision = str(parsed.get("decision") or "")
            if code == 2 or decision == "block":
                outcome.blocked = True
                outcome.reason = str(
                    parsed.get("reason") or stderr.strip()
                    or "blocked by hook")
                record["blocked"] = True
                outcome.results.append(record)
                break
            if code != 0:
                record["code"] = "HOOK_ERROR"
                record["stderr"] = stderr.strip()[:300]
            outcome.results.append(record)
        return outcome

    def _ordered(self) -> list[HookSpec]:
        order = {source: index for index, source in enumerate(_SOURCE_ORDER)}
        return sorted(
            self._hooks,
            key=lambda hook: order.get(hook.source, len(order)),
        )


def _parse_output(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    updated = value.get("updatedInput", value.get("updated_input"))
    if isinstance(updated, Mapping):
        value["updatedInput"] = dict(updated)
    return value
