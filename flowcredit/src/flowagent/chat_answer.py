"""One chat answer's streaming lifecycle, separate from thread management.

Chat is agentic since Phase 1: when the selected provider supports tool
calling, the answer runs as a bounded multi-round loop — model proposes tool
calls, the host gates and executes read-only tools, results go back into the
conversation, until the model answers or the round cap is reached. Providers
without tool support keep the historical single-pass text behaviour.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Iterator

from .chat_tools import (
    APPROVAL_TOOLS,
    MAX_CALLS_PER_ROUND,
    MAX_RESULT_CHARS,
    approval_preview,
    execute_chat_tool,
)
from .chat_usage import AnswerAccounting
from .core.contracts import AgentError
from .pacing import pace_key, reserve_slot
from .tools.trust import trust_for_tool


def _max_tool_rounds() -> int:
    try:
        value = int(os.environ.get("FCODE_CHAT_MAX_TOOL_ROUNDS", "6") or 6)
    except ValueError:
        value = 6
    return max(1, min(value, 16))


def _preview(value: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """Bound what enters the message projection / SSE payload.

    The full result is already in the chain (``chat_tool_result``); the UI and
    later prompts get a truncated copy so one big file cannot bloat the thread.
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}


class AnswerStream:
    def __init__(self, service, thread, user_message: dict[str, Any], model: str | None,
                 approval: str | None = None) -> None:
        self.service = service
        #: 审批档位：off/consequential/all（与 run 同语义）。默认 consequential。
        self.approval_mode = approval if approval in ("off", "consequential", "all") \
            else "consequential"
        #: 本回合内"记住"的已批工具名（consequential/all 下免重复审批）。
        self.remembered: set[str] = set()
        self.thread = thread
        self.thread_id = thread.id
        self.user_message = user_message
        self.model = model
        self.assistant_id = f"a-{secrets.token_hex(6)}"
        self.started = time.monotonic()
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.usage: dict[str, Any] = {}
        self.finish_reason = ""
        self.finalised = False
        #: Ordered answer content: {"type":"text","text"} / {"type":"tool",...}
        self.blocks: list[dict[str, Any]] = []
        self.round_usages: list[dict[str, Any]] = []
        self.tool_rounds = 0

    # ------------------------------------------------------------------ lifecycle

    def events(self) -> Iterator[dict[str, Any]]:
        try:
            self.config, self.provider = self.service._provider(self.model)
        except Exception as error:  # noqa: BLE001 — configuration errors belong in the chain
            yield self._configuration_error(error)
            return
        messages = self.service._prompt_messages(self.thread_id)
        self.accounting = AnswerAccounting.prepare(
            self.service, self.thread_id, messages, self.config,
        )
        try:
            self.accounting.reserve()
        except AgentError as error:
            yield from self._budget_error(error)
            return
        yield self._start_event()
        yield from self._wait_for_slot()
        tool_ctx = self.service._tool_context() if getattr(self.config, "supports_tools", False) else None
        try:
            if tool_ctx is not None:
                yield from self._tool_loop(messages, tool_ctx)
            else:
                yield from self._consume(messages)
            yield self._complete()
        except Exception as error:  # noqa: BLE001 — malformed provider chunks are errors
            yield self._stream_error(error)
        finally:
            if not self.finalised:
                self._cancel()

    # ------------------------------------------------------------------ events

    def _error_detail(self, code: str, message: str) -> dict[str, Any]:
        return {
            "assistant_id": self.assistant_id,
            "user_id": self.user_message["id"],
            "code": code,
            "message": message,
            "text": "",
        }

    def _configuration_error(self, error: Exception) -> dict[str, Any]:
        code = getattr(error, "code", "CHAT_CONFIG_INVALID")
        message = getattr(error, "message", None) or f"{type(error).__name__}: {error}"
        self.thread = self.service._append(
            self.thread, "chat_error", self._error_detail(code, message),
        )
        self.finalised = True
        return {
            "type": "error",
            "thread_id": self.thread_id,
            "user_id": self.user_message["id"],
            "assistant_id": self.assistant_id,
            "code": code,
            "message": message,
        }

    def _budget_error(self, error: AgentError) -> Iterator[dict[str, Any]]:
        self.thread = self.service._append(
            self.thread, "chat_error", self._error_detail(error.code, error.message),
        )
        self.finalised = True
        yield {
            "type": "error",
            "thread_id": self.thread_id,
            "user_id": self.user_message["id"],
            "assistant_id": self.assistant_id,
            "code": error.code,
            "message": error.message,
            "budget": self.accounting.snapshot(),
        }

    def _start_event(self) -> dict[str, Any]:
        return {
            "type": "start",
            "thread_id": self.thread_id,
            "thread_title": self.thread.objective or "新对话",
            "user": self.service._public_message(self.user_message),
            "assistant_id": self.assistant_id,
            "provider": {
                "key": self.config.key, "label": self.config.label, "model": self.config.model,
            },
            "reserved": {
                "tokens": self.accounting.reserved_tokens,
                "cost_microusd": self.accounting.reserved_cost,
            },
            "budget": self.accounting.snapshot(),
        }

    def _wait_for_slot(self) -> Iterator[dict[str, Any]]:
        wait = reserve_slot(
            pace_key(self.config.key, self.config.base_url), self.config.min_interval_seconds,
        )
        if wait > 0:
            yield {"type": "wait", "seconds": int(wait + 0.999), "provider": self.config.label}
            remaining = wait
            while remaining > 0:
                time.sleep(min(0.25, remaining))
                remaining -= 0.25

    # ------------------------------------------------------------------ content

    def _append_text(self, chunk: str) -> None:
        self.text_parts.append(chunk)
        if self.blocks and self.blocks[-1]["type"] == "text":
            self.blocks[-1]["text"] += chunk
        else:
            self.blocks.append({"type": "text", "text": chunk})

    # ------------------------------------------------------------------ transport

    def _consume(self, messages: list[dict]) -> Iterator[dict[str, Any]]:
        """Single-pass text turn (providers without tool support)."""
        round_started = time.monotonic()
        for item in self.provider.stream(messages):
            if "delta" in item:
                chunk = str(item["delta"])
                self._append_text(chunk)
                yield {"type": "delta", "text": chunk, "assistant_id": self.assistant_id}
            elif "reasoning" in item:
                chunk = str(item["reasoning"])
                self.reasoning_parts.append(chunk)
                yield {"type": "reasoning", "text": chunk, "assistant_id": self.assistant_id}
            elif "usage" in item and isinstance(item["usage"], dict):
                self.usage = item["usage"]
            elif "finish_reason" in item:
                self.finish_reason = str(item["finish_reason"])
        self._settle_round(round_started, "".join(self.text_parts))

    # ------------------------------------------------------------------ agent loop

    def _settle_round(self, round_started: float, round_text: str) -> None:
        latency_ms = int((time.monotonic() - round_started) * 1000)
        usage = self.accounting.settle(self.usage, round_text, latency_ms)
        cost = self.accounting.settle_cost(self.config, usage)
        self.round_usages.append({**usage, "cost_microusd": cost})

    def _append_chain(self, event: str, detail: dict[str, Any]) -> None:
        self.thread = self.service._append(self.thread, event, {
            "assistant_id": self.assistant_id,
            "user_id": self.user_message["id"],
            **detail,
        })

    def _tool_loop(self, messages: list[dict], tool_ctx) -> Iterator[dict[str, Any]]:
        registry = tool_ctx["registry"]
        permissions = tool_ctx["permissions"]
        tool_defs = tool_ctx["tool_defs"]
        max_rounds = _max_tool_rounds()
        for round_index in range(1, max_rounds + 1):
            if round_index > 1:
                # 每轮按增长后的上下文重新预留；预算不够直接终止（由调用方落链）。
                self.accounting = AnswerAccounting.prepare(
                    self.service, self.thread_id, messages, self.config,
                )
                self.accounting.reserve()
            self.usage = {}
            round_started = time.monotonic()
            round_text_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            for item in self.provider.stream(messages, tools=tool_defs):
                if "delta" in item:
                    chunk = str(item["delta"])
                    self._append_text(chunk)
                    round_text_parts.append(chunk)
                    yield {"type": "delta", "text": chunk, "assistant_id": self.assistant_id}
                elif "reasoning" in item:
                    chunk = str(item["reasoning"])
                    self.reasoning_parts.append(chunk)
                    yield {"type": "reasoning", "text": chunk, "assistant_id": self.assistant_id}
                elif "usage" in item and isinstance(item["usage"], dict):
                    self.usage = item["usage"]
                elif "tool_calls" in item and isinstance(item["tool_calls"], list):
                    calls = [call for call in item["tool_calls"] if isinstance(call, dict)]
                elif "finish_reason" in item:
                    self.finish_reason = str(item["finish_reason"])
            self._settle_round(round_started, "".join(round_text_parts))
            if not calls:
                return
            if len(calls) > MAX_CALLS_PER_ROUND:
                calls = calls[:MAX_CALLS_PER_ROUND]
                note = f"（本轮工具调用超过 {MAX_CALLS_PER_ROUND} 个，只执行前 {MAX_CALLS_PER_ROUND} 个）"
                self._append_text(note)
                yield {"type": "delta", "text": note, "assistant_id": self.assistant_id}
            self.tool_rounds = round_index
            tool_calls_payload: list[dict[str, Any]] = []
            tool_messages: list[dict[str, Any]] = []
            for position, call in enumerate(calls):
                call_id = str(call.get("id") or f"call_{round_index}_{position}")
                name = str(call.get("name") or "")
                arguments = call.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                block = {
                    "type": "tool", "call_id": call_id, "name": name,
                    "arguments": arguments, "round": round_index, "status": "running",
                }
                self.blocks.append(block)
                self._append_chain("chat_tool_started", {
                    "call_id": call_id, "name": name, "arguments": arguments,
                    "round": round_index,
                })
                yield {
                    "type": "tool_call", "call_id": call_id, "name": name,
                    "arguments": arguments, "round": round_index,
                    "assistant_id": self.assistant_id,
                }
                # 参数先解析：坏参数在审批/执行前就被拒（可修复错误）。
                try:
                    parsed = json.loads(arguments or "{}")
                    if not isinstance(parsed, dict):
                        raise ValueError("arguments must be an object")
                except ValueError as error:
                    error_info = {"code": "TOOL_ARGUMENTS_INVALID", "message": str(error)}
                    block.update(status="error", error=error_info)
                    self._append_chain("chat_tool_result", {
                        "call_id": call_id, "name": name, "round": round_index,
                        "status": "error", "error": error_info,
                    })
                    yield {"type": "tool_result", "call_id": call_id, "status": "error",
                           "error": error_info, "round": round_index,
                           "assistant_id": self.assistant_id}
                    tool_messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": f"ERROR {error_info['code']}: {error_info['message']}",
                    })
                    continue
                # 门控前移：未注册/不在允许清单的工具在审批前就被拒——
                # 审批只服务"允许执行"的调用，不为死请求停留。
                gate = permissions.gate(name, parsed)
                if not gate.allowed:
                    error_info = {"code": gate.code or "TOOL_DENIED",
                                  "message": gate.reason or "tool call refused by the host gate"}
                    block.update(status="error", error=error_info)
                    self._append_chain("chat_tool_result", {
                        "call_id": call_id, "name": name, "round": round_index,
                        "status": "error", "error": error_info,
                    })
                    yield {"type": "tool_result", "call_id": call_id, "status": "error",
                           "error": error_info, "round": round_index,
                           "assistant_id": self.assistant_id}
                    tool_messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": f"ERROR {error_info['code']}: {error_info['message']}",
                    })
                    continue
                # 写工具按档位审批：consequential 只审写工具，all 全审；
                # 本回合内 remember 过的工具名免重复审批。
                needs_approval = (
                    self.approval_mode == "all"
                    or (self.approval_mode == "consequential" and name in APPROVAL_TOOLS)
                ) and name not in self.remembered
                if needs_approval:
                    preview = approval_preview(tool_ctx, name, parsed)
                    approval_id = f"ap-{round_index}-{position}-{call_id[:8]}"
                    self._append_chain("chat_tool_approval", {
                        "call_id": call_id, "name": name, "round": round_index,
                        "arguments": arguments, "preview": _preview(preview, 2000),
                        "mode": self.approval_mode,
                    })
                    yield {
                        "type": "tool_approval", "approval_id": approval_id,
                        "call_id": call_id, "name": name, "arguments": arguments,
                        "preview": _preview(preview, 2000), "round": round_index,
                        "mode": self.approval_mode,
                        "assistant_id": self.assistant_id,
                    }
                    decision = self.service.request_tool_approval(
                        self.thread_id, approval_id,
                        {"call_id": call_id, "name": name, "arguments": arguments,
                         "preview": preview},
                        timeout=300.0,
                    )
                    self._append_chain("chat_tool_decision", {
                        "approval_id": approval_id, "call_id": call_id,
                        "allow": bool(decision.get("allow")),
                        "remember": bool(decision.get("remember")),
                        "reason": str(decision.get("reason") or ""),
                    })
                    if not decision.get("allow"):
                        error_info = {
                            "code": "OPERATOR_DENIED",
                            "message": str(decision.get("reason") or "操作员拒绝了这个工具调用"),
                        }
                        block.update(status="denied", error=error_info)
                        self._append_chain("chat_tool_result", {
                            "call_id": call_id, "name": name, "round": round_index,
                            "status": "denied", "error": error_info,
                        })
                        yield {"type": "tool_result", "call_id": call_id, "status": "error",
                               "error": error_info, "round": round_index,
                               "assistant_id": self.assistant_id}
                        tool_messages.append({
                            "role": "tool", "tool_call_id": call_id,
                            "content": f"ERROR {error_info['code']}: {error_info['message']}",
                        })
                        continue
                    if decision.get("remember"):
                        self.remembered.add(name)
                observation, error = execute_chat_tool(
                    registry, permissions, self.accounting.budget, name, parsed,
                )
                if error is not None:
                    block.update(status="error", error=error)
                    self._append_chain("chat_tool_result", {
                        "call_id": call_id, "name": name, "round": round_index,
                        "status": "error", "error": error,
                    })
                    yield {
                        "type": "tool_result", "call_id": call_id, "status": "error",
                        "error": error, "round": round_index,
                        "assistant_id": self.assistant_id,
                    }
                    tool_messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": f"ERROR {error.get('code')}: {error.get('message')}",
                    })
                else:
                    result = observation.get("result") or {}
                    result_hash = observation.get("result_hash") or ""
                    trust = trust_for_tool(name).value
                    preview = _preview(result)
                    block.update(status="ok", result=preview,
                                 result_hash=result_hash, trust=trust)
                    self._append_chain("chat_tool_result", {
                        "call_id": call_id, "name": name, "round": round_index,
                        "status": "ok", "result": result,
                        "result_hash": result_hash, "trust": trust,
                    })
                    yield {
                        "type": "tool_result", "call_id": call_id, "status": "ok",
                        "result": preview, "result_hash": result_hash,
                        "trust": trust, "round": round_index,
                        "assistant_id": self.assistant_id,
                    }
                    tool_messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str)[:MAX_RESULT_CHARS],
                    })
                tool_calls_payload.append({
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": arguments},
                })
            messages.append({
                "role": "assistant", "content": "", "tool_calls": tool_calls_payload,
            })
            messages.extend(tool_messages)
        # 轮数用尽仍有未完成的调用：如实说明，不制造"已完成"的假象。
        note = f"（已达到工具调用轮数上限 {max_rounds}，先停在这里）"
        self.finish_reason = "tool_rounds_exhausted"
        self._append_text(note)
        yield {"type": "delta", "text": note, "assistant_id": self.assistant_id}

    # ------------------------------------------------------------------ completion

    def _aggregate_usage(self) -> dict[str, Any]:
        if not self.round_usages:
            return self.accounting.settle(self.usage, "".join(self.text_parts),
                                          int((time.monotonic() - self.started) * 1000))
        prompt = sum(int(item.get("prompt_tokens") or 0) for item in self.round_usages)
        completion = sum(int(item.get("completion_tokens") or 0) for item in self.round_usages)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "latency_ms": int((time.monotonic() - self.started) * 1000),
        }

    def _tool_summary(self) -> list[dict[str, Any]]:
        summary = []
        for block in self.blocks:
            if block["type"] != "tool":
                continue
            item = {
                "call_id": block["call_id"], "name": block["name"],
                "arguments": block["arguments"], "round": block["round"],
                "status": block["status"],
            }
            if block.get("error"):
                item["error"] = block["error"]
                item["content"] = f"ERROR {block['error'].get('code')}: {block['error'].get('message')}"
            elif "result" in block:
                item["content"] = json.dumps(block["result"], ensure_ascii=False,
                                             default=str)[:MAX_RESULT_CHARS]
            if block.get("result_hash"):
                item["result_hash"] = block["result_hash"]
            summary.append(item)
        return summary

    def _complete(self) -> dict[str, Any]:
        from .chat import CHAT_VERSION, parse_action_block

        raw_text = "".join(self.text_parts)
        usage = self._aggregate_usage()
        if self.round_usages:
            # 每轮已用 settle_cost 记账；汇总只做求和，不能再次 charge。
            actual_cost = sum(int(item.get("cost_microusd") or 0) for item in self.round_usages)
        else:
            actual_cost = self.accounting.settle_cost(self.config, usage)
        text, action = parse_action_block(raw_text)
        receipt_ref = self.service.receipts.issue(
            intent={
                "thread_id": self.thread_id,
                "user_message_id": self.user_message["id"],
                "assistant_id": self.assistant_id,
            },
            plan={"version": CHAT_VERSION, "kind": "answer", "model": self.config.model},
            usage={
                "provider": self.config.key,
                "model": self.config.model,
                "tokens": usage["total_tokens"],
                "cost_microusd": actual_cost,
                "latency_ms": usage["latency_ms"],
                "finish_reason": self.finish_reason or "stop",
                "tool_rounds": self.tool_rounds,
                "external_billing": "not-verified",
            },
        )
        public_usage = {
            **usage,
            "cost_microusd": actual_cost,
            "reserved_tokens": self.accounting.reserved_tokens,
            "reserved_cost_microusd": self.accounting.reserved_cost,
        }
        detail = {
            "id": self.assistant_id,
            "user_id": self.user_message["id"],
            "text": text,
            "reasoning": "".join(self.reasoning_parts),
            "status": "ok",
            "usage": public_usage,
            "receipt_ref": receipt_ref,
            "provider": self.config.key,
            "model": self.config.model,
        }
        if action:
            detail["action"] = action
        if any(block["type"] == "tool" for block in self.blocks):
            detail["blocks"] = list(self.blocks)
            detail["tool_calls"] = self._tool_summary()
        self.thread = self.service._append(self.thread, "chat_assistant", detail)
        self.finalised = True
        message = {
            "id": self.assistant_id,
            "role": "assistant",
            "text": text,
            "reasoning": detail["reasoning"],
            "status": "ok",
            "usage": public_usage,
            "receipt_ref": receipt_ref,
            "provider": self.config.key,
            "model": self.config.model,
            "error": {},
            "at": "",
            "seq": self.thread.sequence,
        }
        # Same rule as the replay: no proposal, no action field. The client
        # treats a present-but-empty action as "render the proposal card".
        if action:
            message["action"] = action
        if "blocks" in detail:
            message["blocks"] = detail["blocks"]
            message["tool_calls"] = detail["tool_calls"]
        return {
            "type": "done", "message": message,
            "budget": self.accounting.snapshot(), "thread_id": self.thread_id,
        }

    def _stream_error(self, error: Exception) -> dict[str, Any]:
        text = "".join(self.text_parts)
        code = error.code if isinstance(error, AgentError) else "CHAT_STREAM_FAILED"
        message = error.message if isinstance(error, AgentError) else f"{type(error).__name__}: {error}"
        detail = {
            "assistant_id": self.assistant_id,
            "user_id": self.user_message["id"],
            "code": code,
            "message": message,
            "text": text,
            "reasoning": "".join(self.reasoning_parts),
            "usage": self.usage,
        }
        if self.blocks:
            detail["blocks"] = list(self.blocks)
        self.thread = self.service._append(self.thread, "chat_error", detail)
        self.finalised = True
        return {
            "type": "error", "code": code, "message": message,
            "assistant_id": self.assistant_id, "partial": text,
            "budget": self.accounting.snapshot(),
        }

    def _cancel(self) -> None:
        detail = {
            "assistant_id": self.assistant_id,
            "user_id": self.user_message["id"],
            "text": "".join(self.text_parts),
            "reasoning": "".join(self.reasoning_parts),
            "usage": self.usage,
            "provider": self.config.key,
            "model": self.config.model,
        }
        if self.blocks:
            detail["blocks"] = list(self.blocks)
        self.service._append(self.thread, "chat_cancelled", detail)