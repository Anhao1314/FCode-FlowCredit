"""S5: run capability model calls through the new harness core.

The capability runtime still owns stages, gates, budgets and artifact checks.
This adapter only replaces *how one proposal is obtained*:

    old: Harness._propose -> OpenAICompatProvider.propose (buffered call)
    new: Harness.propose_with -> CoreBackedProvider.propose
         -> HarnessCore.run (streamed deadline, schema-validated tool call)

The new provider keeps the old ``Provider`` interface, so the surrounding
retry/budget/state-machine logic in :class:`Harness` is unchanged. Tool calls
are *captured*, never executed: the capability gate remains the only executor.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator, Mapping

from ..core.contracts import PLAN_VERSION, AgentError
from ..harness import (
    Done,
    EventBus,
    HarnessCore,
    ReasoningDelta,
    StreamRequest,
    StreamUsage,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
)
from ..providers.base import Proposal
from ..providers.openai_compat import (
    _has_usage,
    _provider_cached_tokens,
    _usage_tokens,
)
from ..providers.prompt import prompt_profile_for, render_prompt

__all__ = ["CoreBackedProvider", "OpenAICompatHarnessProvider"]

_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")


class OpenAICompatHarnessProvider:
    """Adapt one OpenAI-compatible client to the harness ``StreamingProvider``.

    The client still performs the SSE dispatch (``_post_stream``), so transport
    retries, partial-stream accounting and usage recovery stay exactly where
    they were; this adapter only re-shapes the finished message into typed
    harness events.
    """

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.last_error: BaseException | None = None
        self.last_message: dict[str, Any] = {}
        self.last_usage: dict[str, Any] = {}
        self.request_id = ""
        self.cost_microusd = 0

    def stream(self, request: StreamRequest) -> Iterator[Any]:
        provider = self.provider
        client = getattr(provider, "client", None)
        if client is None:
            raise AgentError("MODEL_CALL_FAILED", "provider has no chat client")
        system = request.system
        user = _user_text(request.messages)
        tools = [_to_openai_tool(spec) for spec in request.tools]
        try:
            body = client._build_body(
                system,
                user,
                tools=tools or None,
                tool_choice="auto" if tools else None,
                max_completion_tokens=request.max_tokens,
                reasoning_effort=None,
                json_mode=False if tools else True,
                stream=True,
            )
            message, usage, request_id = client._post_stream(body)
        except AgentError as error:
            self.last_error = error
            raise
        self.last_message = dict(message or {})
        self.last_usage = dict(usage or {})
        self.request_id = str(request_id or "")
        prompt_tokens, completion_tokens, _total = _usage_tokens(self.last_usage)
        self.cost_microusd = (
            int(provider.price.cost(self.last_usage))
            if _has_usage(self.last_usage) else 0
        )
        content = str(self.last_message.get("content") or "")
        reasoning = str(self.last_message.get("reasoning_content") or "")
        calls = list(self.last_message.get("tool_calls") or [])
        if not calls and not content.strip():
            maker = getattr(provider, "_empty_content_error", None)
            if callable(maker):
                error = maker(self.last_message)
                self.last_error = error
                raise error
            error = AgentError("EMPTY_CONTENT", "the provider returned an empty reply")
            self.last_error = error
            raise error
        if reasoning:
            yield ReasoningDelta(reasoning)
        if content:
            yield TextDelta(content)
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_arguments = function.get("arguments")
            try:
                arguments = _arguments(raw_arguments)
            except AgentError as error:
                self.last_error = error
                raise
            yield ToolUse(
                str(call.get("id") or f"call_{index + 1}"),
                name,
                arguments,
            )
        yield Done(
            stop_reason="tool_calls" if calls else "end_turn",
            usage=StreamUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cost_microusd=self.cost_microusd,
                cached_tokens=_provider_cached_tokens(self.last_usage),
            ),
        )


class CoreBackedProvider:
    """Old ``Provider`` interface backed by ``HarnessCore``.

    ``propose(context)`` renders the same prompt the old provider would, runs a
    one-turn harness loop with the same native tool schemas, and turns the
    captured tool call into the same ``Proposal`` shape.
    """

    def __init__(self, provider: Any) -> None:
        self.wrapped = provider
        self.name = str(getattr(provider, "name", "openai-compat"))
        self.model = str(getattr(provider, "model", ""))
        self.native_tool_schemas: list[dict[str, Any]] = []
        self.last_result = None
        self.last_adapter: OpenAICompatHarnessProvider | None = None
        self.last_context: dict[str, Any] = {}

    def set_native_tools(self, tools: list[dict[str, Any]]) -> None:
        self.native_tool_schemas = list(tools or [])
        setter = getattr(self.wrapped, "set_native_tools", None)
        if callable(setter):
            setter(list(tools or []))

    def reserve(self, context: dict[str, Any]) -> tuple[int, int]:
        reserve = getattr(self.wrapped, "reserve", None)
        if callable(reserve):
            return reserve(context)
        return (1_000, 500)

    def _completion_budget(self, context: dict[str, Any]) -> int | None:
        budget = getattr(self.wrapped, "_completion_budget", None)
        return budget(context) if callable(budget) else None

    def propose(self, context: dict[str, Any]) -> Proposal:
        self.last_context = dict(context)
        specs = _capture_specs(self.native_tool_schemas)
        if not specs:
            raise AgentError(
                "PROPOSAL_MALFORMED",
                "capability native tools are missing from the request",
            )
        system, user = render_prompt(context)
        adapter = OpenAICompatHarnessProvider(self.wrapped)
        core = HarnessCore(adapter, ToolRegistry(specs), bus=EventBus())
        completion_override = self._completion_budget(context)
        timeout_ms = int(float(getattr(self.wrapped, "timeout", 120.0)) * 1000) + 5_000
        started = time.monotonic()
        result = core.run(
            [{"role": "user", "content": [{"type": "text", "text": user}]}],
            system=system,
            tool_names=[spec.name for spec in specs],
            max_turns=1,
            call_deadline_ms=timeout_ms,
            max_tokens=completion_override,
        )
        self.last_result = result
        self.last_adapter = adapter
        latency_ms = int((time.monotonic() - started) * 1000)
        captured = _tool_uses(result.messages)
        # One capture turn intentionally ends on MAX_TURNS: the assistant
        # message already carries the action, and no second model call is due.
        capture_turn = result.code == "MAX_TURNS" and bool(captured)
        if not result.completed and not capture_turn:
            if adapter.last_error is not None:
                raise adapter.last_error
            raise AgentError(
                "MODEL_CALL_FAILED",
                result.error or f"harness stopped with {result.code}",
            )

        calls = captured
        if not calls:
            raise AgentError(
                "PROPOSAL_MALFORMED",
                "native reply carried no usable tool calls",
                details={"raw_content": adapter.last_message.get("content", "")[:120]},
            )
        call = calls[0]
        if call.name not in {spec.name for spec in specs}:
            raise AgentError(
                "PROPOSAL_MALFORMED",
                f"tool {call.name!r} was not offered in this stage",
            )
        todo_id = None
        plan = {
            "version": PLAN_VERSION,
            "steps": [{
                "id": _safe_id(call.id),
                "tool": call.name,
                "arguments": dict(call.input),
            }],
        }

        tokens = int(result.usage.input_tokens + result.usage.output_tokens)
        cost = int(result.usage.cost_microusd)
        profile = prompt_profile_for(context)
        prompt_tokens, completion_tokens, total_tokens = _usage_tokens(
            adapter.last_usage)
        cached_tokens = _provider_cached_tokens(adapter.last_usage)
        metadata: dict[str, Any] = {
            "prompt_version": profile.version,
            "billing": "external",
            "provider_request_id": adapter.request_id,
            "native_tools": True,
            "completion_tokens_override": int(completion_override or 0),
            "harness": "core",
            "usage_prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "cache_hit_bp": int(round(
                cached_tokens / prompt_tokens * 10_000)) if prompt_tokens else 0,
            "dropped_tool_calls": max(0, len(calls) - 1),
        }
        if result.evidence_head:
            # M1: the proposal round's evidence chain head — binds the plan
            # receipt stream to the harness loop that produced it.
            metadata["evidence_head"] = result.evidence_head
        if adapter.last_usage.get("stream"):
            metadata.update({
                "stream": True,
                "stream_chunks": int(adapter.last_usage.get("stream_chunks") or 0),
                "stream_digest": str(adapter.last_usage.get("stream_digest") or ""),
            })
        if not _has_usage(adapter.last_usage):
            metadata["usage_missing"] = True

        raw_sink = getattr(self.wrapped, "raw_sink", None)
        if callable(raw_sink):
            raw_sink({
                "round": context.get("round"),
                "provider": self.name,
                "model": self.model,
                "request_id": adapter.request_id,
                "content": str(adapter.last_message.get("content") or ""),
                "usage": dict(adapter.last_usage),
                "tool_calls": list(adapter.last_message.get("tool_calls") or []),
                "harness": "core",
            })

        issue = getattr(self.wrapped, "issue_receipt", None)
        intent = context.get("intent")
        if callable(issue) and isinstance(intent, dict):
            ref = issue(
                intent=intent,
                plan=plan,
                usage={
                    "provider": self.name,
                    "model": self.model,
                    "tokens": total_tokens,
                    "cost_microusd": cost,
                    "request_id": adapter.request_id,
                    "transport_retries": int(
                        adapter.last_usage.get("transport_retries") or 0),
                },
            )
            metadata["hmac_receipt_ref"] = ref

        return Proposal(
            plan=plan,
            provider=self.name,
            model=self.model,
            tokens=tokens,
            cost_microusd=cost,
            latency_ms=latency_ms,
            todo_id=todo_id,
            metadata=metadata,
        )


def _capture_specs(schemas: list[dict[str, Any]]) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for entry in schemas:
        function = entry.get("function") if isinstance(entry, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "")
        if not name:
            continue
        specs.append(ToolSpec(
            name=name,
            description=str(function.get("description") or ""),
            input_schema=function.get("parameters")
            if isinstance(function.get("parameters"), Mapping)
            else {"type": "object", "properties": {}},
            handler=lambda _args, _context: ToolResult.ok("captured"),
        ))
    return specs


def _to_openai_tool(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": spec.get("input_schema") or {"type": "object"},
        },
    }


def _user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            ]
            joined = "\n".join(part for part in parts if part)
            if joined:
                return joined
    return ""


def _tool_uses(messages: list[dict[str, Any]]) -> list[ToolUse]:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        calls = [
            ToolUse(str(block.get("id") or ""), str(block.get("name") or ""),
                    dict(block.get("input") or {}))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "tool_use"
        ]
        if calls:
            return calls
    return []


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "{}")
    try:
        value = json.loads(text)
    except ValueError as error:
        raise AgentError(
            "PROPOSAL_MALFORMED",
            f"tool arguments are not valid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise AgentError("PROPOSAL_MALFORMED", "tool arguments must be an object")
    return value


def _safe_id(value: str) -> str:
    text = str(value or "")
    return text if _ID_RE.fullmatch(text) else "a1"
