"""The harness loop (S1/S3): streamed propose → check → call → append.

Ported behavior from the dissected harness, implemented in Python:

* one thin loop, no domain knowledge;
* streamed model calls with a wall-clock deadline per call;
* every tool call crosses an explicit check point and emits
  ``tool.check`` / ``tool.call`` / ``tool.result`` events;
* arguments are schema-validated before the handler runs, and every failure
  becomes a ``tool_result`` the model can read — the loop only stops on
  ``max_turns`` or a provider/deadline failure;
* before each turn an optional ``Compaction`` may replace history with a
  nine-section summary (``compact_boundary``), and an optional ``SessionStore``
  records every message to a JSONL event tree.

Deadline mechanics: the provider stream is pumped from a daemon thread into a
queue, so a stalled stream cannot block us past the deadline. A timed-out
pump thread is abandoned (Python threads cannot be force-killed); adapters
should also watch ``StreamRequest.cancel``. The same join-with-timeout
pattern bounds tool execution.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .compaction import Compaction
from .evidence import EvidenceChain
from .events import EventBus
from .hooks import HookRunner
from .provider import (
    Done,
    StreamRequest,
    StreamTimeout,
    StreamUsage,
    StreamingProvider,
    ToolUse,
    collect_stream,
)
from .session import SessionStore
from .tools import (
    ToolCallContext,
    ToolInputError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    validate_input,
)

__all__ = [
    "CheckDecision",
    "HarnessCore",
    "HarnessResult",
    "HarnessTimeout",
]


class HarnessTimeout(StreamTimeout):
    code = "CALL_DEADLINE"


@dataclass(frozen=True)
class CheckDecision:
    allow: bool = True
    reason: str = ""
    #: Hooks may rewrite arguments (PreToolUse ``updatedInput``); applied before
    #: schema validation and execution.
    updated_input: Mapping[str, Any] | None = None


Checker = Callable[[str, Mapping[str, Any], ToolCallContext], CheckDecision]


@dataclass
class HarnessResult:
    status: str  # completed | max_turns | failed
    turns: int
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    usage: StreamUsage = field(default_factory=StreamUsage)
    code: str = ""
    error: str = ""
    compactions: int = 0
    evidence_head: str = ""
    evidence_receipts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class HarnessCore:
    """One streaming tool-use loop over an injected provider."""

    def __init__(
        self,
        provider: StreamingProvider,
        tools: ToolRegistry,
        *,
        checker: Checker | None = None,
        bus: EventBus | None = None,
        hooks: HookRunner | None = None,
        compaction: Compaction | None = None,
        session: SessionStore | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.checker = checker or (lambda _name, _args, _ctx: CheckDecision())
        self.bus = bus or EventBus()
        #: Lifecycle hooks owned by the loop: SessionStart/PostToolUse/Stop.
        #: PreToolUse hooks belong to the permission pipeline (the checker).
        self.hooks = hooks
        #: Optional context manager and transcript tree (S3).
        self.compaction = compaction
        self.session = session
        self._compaction_blocked = False
        #: Evidence receipts (M1): every dispatch outcome, hash-chained.
        self.evidence = EvidenceChain()

    def run(
        self,
        messages: Iterable[Mapping[str, Any]] | None = None,
        *,
        system: str = "",
        tool_names: Iterable[str] | None = None,
        max_turns: int = 8,
        call_deadline_ms: int = 120_000,
        tool_deadline_ms: int = 60_000,
        max_tokens: int | None = None,
    ) -> HarnessResult:
        conversation: list[dict[str, Any]] = [dict(item) for item in (messages or [])]
        allowed = list(tool_names) if tool_names is not None else self.tools.names()
        schemas = self.tools.describe(allowed)
        self.bus.emit("tool.describe", names=allowed, count=len(schemas))
        self._compaction_blocked = False
        self._hook("SessionStart", {
            "system_chars": len(system or ""),
            "message_count": len(conversation),
            "tool_count": len(schemas),
        })
        if self.session is not None:
            self.session.append("session_start", {
                "system_chars": len(system or ""),
                "tool_count": len(schemas),
            })
            for item in conversation:
                self.session.record_message(
                    str(item.get("role") or "user"), item.get("content"))
        usage = StreamUsage()
        turns = 0
        compactions = 0

        while True:
            if turns >= max_turns:
                self.bus.emit("loop.end", status="max_turns", turns=turns)
                return self._result(
                    status="max_turns", turns=turns, messages=conversation,
                    events=[e.to_dict() for e in self.bus.history],
                    usage=usage, code="MAX_TURNS", compactions=compactions,
                )

            if self.compaction is not None:
                conversation, compact_usage, did_compact = self._maybe_compact(
                    conversation)
                usage = _add_usage(usage, compact_usage)
                compactions += 1 if did_compact else 0

            turns += 1
            self.bus.emit("loop.turn", turn=turns)
            request = StreamRequest(
                system=system, messages=conversation, tools=schemas,
                max_tokens=max_tokens,
            )
            try:
                collected = collect_stream(
                    self.provider, request, deadline_ms=call_deadline_ms)
            except StreamTimeout as error:
                self.bus.emit("loop.error", code=error.code, message=str(error))
                return self._result(
                    status="failed", turns=turns, messages=conversation,
                    events=[e.to_dict() for e in self.bus.history],
                    usage=usage, code=error.code, error=str(error),
                    compactions=compactions,
                )
            except Exception as error:  # noqa: BLE001 — provider faults are terminal
                self.bus.emit(
                    "loop.error", code="PROVIDER_ERROR",
                    message=f"{type(error).__name__}: {error}",
                )
                return self._result(
                    status="failed", turns=turns, messages=conversation,
                    events=[e.to_dict() for e in self.bus.history],
                    usage=usage, code="PROVIDER_ERROR",
                    error=f"{type(error).__name__}: {error}",
                    compactions=compactions,
                )

            text, reasoning = collected.text, collected.reasoning
            tool_calls, done = collected.tool_calls, collected.done

            usage = _add_usage(usage, done.usage)
            assistant: dict[str, Any] = {"role": "assistant", "content": []}
            if text:
                assistant["content"].append({"type": "text", "text": text})
            for call in tool_calls:
                assistant["content"].append({
                    "type": "tool_use", "id": call.id,
                    "name": call.name, "input": dict(call.input),
                })
            conversation.append(assistant)
            if self.session is not None:
                self.session.record_message("assistant", assistant["content"])
            self.bus.emit(
                "assistant.message", turn=turns, stop_reason=done.stop_reason,
                text_chars=len(text), tool_calls=len(tool_calls),
                usage=done.usage.to_dict(),
            )

            if not tool_calls:
                self._hook("Stop", {"turns": turns, "stop_reason": done.stop_reason})
                self.bus.emit("loop.end", status="completed", turns=turns)
                return self._result(
                    status="completed", turns=turns, messages=conversation,
                    events=[e.to_dict() for e in self.bus.history], usage=usage,
                    compactions=compactions,
                )

            results: list[dict[str, Any]] = []
            for call in tool_calls:
                results.append(self._dispatch(call, turn=turns,
                                              tool_deadline_ms=tool_deadline_ms))
            conversation.append({"role": "user", "content": results})
            if self.session is not None:
                self.session.record_message("user", results)

    # ----------------------------------------------------------------- private

    def _result(self, **kwargs: Any) -> HarnessResult:
        """Every terminal result carries the run's evidence chain head."""
        return HarnessResult(
            evidence_head=self.evidence.head(),
            evidence_receipts=self.evidence.to_list(),
            **kwargs,
        )

    def _maybe_compact(
        self, conversation: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], StreamUsage, bool]:
        """Replace history with a summary when the context budget is spent."""
        if self.compaction is None or self._compaction_blocked:
            return conversation, StreamUsage(), False
        try:
            if not self.compaction.should_compact(conversation):
                return conversation, StreamUsage(), False
            outcome = self.compaction.compact(conversation)
        except Exception as error:  # noqa: BLE001 — compaction is best-effort
            self._compaction_blocked = True
            code = str(getattr(error, "code", "") or "COMPACT_FAILED")
            self.bus.emit(
                "compact.error", code=code,
                message=f"{type(error).__name__}: {error}",
            )
            return conversation, StreamUsage(), False
        payload = {
            key: value for key, value in outcome.boundary.items()
            if key != "type"
        }
        self.bus.emit(
            str(outcome.boundary.get("type") or "compact.boundary"), **payload)
        if self.session is not None:
            self.session.record_boundary(
                before_tokens=outcome.before_tokens,
                after_tokens=outcome.after_tokens,
                summary_sha256=str(outcome.boundary.get("summary_sha256") or ""),
                trigger=str(outcome.boundary.get("trigger") or "auto"),
            )
            self.session.record_message("user", outcome.messages[0]["content"])
        return list(outcome.messages), outcome.usage, True

    def _dispatch(
        self, call: ToolUse, *, turn: int, tool_deadline_ms: int,
    ) -> dict[str, Any]:
        started = time.monotonic()

        def finish(result: ToolResult, args_used: Mapping[str, Any]) -> dict[str, Any]:
            """M1: one receipt per dispatch outcome — executed, denied,
            invalid or unknown. "Executed without a receipt" is not
            representable at this layer."""
            receipt = self.evidence.append(
                tool=call.name, tool_use_id=call.id, args=args_used,
                result_content=result.content, ok=not result.is_error,
                code=result.code,
                duration_ms=int((time.monotonic() - started) * 1000),
                world=(result.data or {}).get("evidence")
                if isinstance(result.data, Mapping) else None,
            )
            self.bus.emit("tool.evidence", receipt=receipt.to_dict())
            return self._result_block(call, result)

        spec = self.tools.get(call.name)
        context = ToolCallContext(
            call_id=call.id, tool_name=call.name, turn=turn, events=self.bus,
        )
        if spec is None:
            result = ToolResult.error("TOOL_UNKNOWN", f"unknown tool {call.name!r}")
            return finish(result, dict(call.input))
        decision = self.checker(call.name, call.input, context)
        effective = (
            dict(decision.updated_input)
            if decision.updated_input is not None
            else dict(call.input)
        )
        self.bus.emit(
            "tool.check", tool_use_id=call.id, tool=call.name,
            allow=decision.allow, reason=decision.reason,
            updated=effective != dict(call.input),
        )
        if not decision.allow:
            result = ToolResult.error(
                "TOOL_DENIED", decision.reason or "tool call denied")
            return finish(result, effective)
        try:
            validate_input(spec.input_schema, effective)
        except ToolInputError as error:
            result = ToolResult.error(error.code, str(error))
            return finish(result, effective)

        self.bus.emit("tool.call", tool_use_id=call.id, tool=call.name)
        result = _run_with_deadline(
            spec, effective, context, tool_deadline_ms)
        self.bus.emit(
            "tool.result", tool_use_id=call.id, tool=call.name,
            ok=not result.is_error, code=result.code,
        )
        result = self._post_tool(call, effective, result)
        return finish(result, effective)

    def _post_tool(
        self, call: ToolUse, args: Mapping[str, Any], result: ToolResult,
    ) -> ToolResult:
        """Run PostToolUse hooks; a blocked hook feeds its reason back."""
        if self.hooks is None:
            return result
        outcome = self._hook("PostToolUse", {
            "tool_name": call.name,
            "tool_input": dict(args),
            "tool_response": {
                "content": str(result.content or "")[:4000],
                "is_error": bool(result.is_error),
                "code": result.code,
            },
        }, tool=call.name)
        if outcome is None or not outcome.blocked:
            return result
        feedback = (str(result.content or "").rstrip()
                    + f"\n\n[PostToolUse] {outcome.reason}").strip()
        return ToolResult(
            content=feedback, is_error=result.is_error,
            code=result.code, data=result.data,
        )

    def _hook(self, event: str, payload: Mapping[str, Any],
              *, tool: str | None = None):
        """Run loop-owned hooks and surface one chain-safe event if any ran."""
        if self.hooks is None:
            return None
        outcome = self.hooks.run(event, payload, tool=tool)
        if outcome.results:
            self.bus.emit(
                "hook.result", hook=event, count=len(outcome.results),
                blocked=outcome.blocked, reason=outcome.reason[:300],
                commands=[item.get("command", "") for item in outcome.results],
                updated_input=outcome.updated_input is not None,
            )
        return outcome

    def _result_block(self, call: ToolUse, result: ToolResult) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": str(result.content or ""),
            "is_error": bool(result.is_error),
        }
        if result.code:
            block["code"] = result.code
        if result.data is not None:
            # Typed tool payloads ride along for the transcript/UI; providers
            # only forward ``content`` to the model.
            block["data"] = dict(result.data)
        return block


def _run_with_deadline(
    spec: ToolSpec,
    args: dict[str, Any],
    context: ToolCallContext,
    timeout_ms: int,
) -> ToolResult:
    if timeout_ms <= 0:
        raise ValueError("tool_deadline_ms must be positive")
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = spec.handler(args, context)
        except BaseException as error:  # noqa: BLE001 — relayed below
            box["error"] = error

    thread = threading.Thread(
        target=work, daemon=True, name=f"harness-tool-{spec.name}")
    thread.start()
    thread.join(timeout=timeout_ms / 1000)
    if thread.is_alive():
        return ToolResult.error(
            "TOOL_TIMEOUT", f"tool {spec.name!r} exceeded {timeout_ms}ms")
    if "error" in box:
        error = box["error"]
        if isinstance(error, ToolResult):
            return error
        return ToolResult.error(
            "TOOL_FAILED", f"{type(error).__name__}: {error}")
    value = box.get("value")
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, Mapping):
        return ToolResult.ok(
            content=str(value.get("content") or ""), data=value)
    return ToolResult.ok(content=str(value if value is not None else ""))


def _add_usage(left: StreamUsage, right: StreamUsage) -> StreamUsage:
    return StreamUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cost_microusd=left.cost_microusd + right.cost_microusd,
        cached_tokens=left.cached_tokens + right.cached_tokens,
    )
