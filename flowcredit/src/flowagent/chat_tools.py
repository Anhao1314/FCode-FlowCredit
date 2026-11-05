"""对话内工具循环的治理层（Agentic Chat · Phase 1+2）。

与 run 相同的纪律，换成对话的上下文：
- **只读工具**（`ls_workspace` / `read_file`）在所有档位可直接执行；
- **写工具**（`write_file` / `edit_file` / `run_command`）需要逐调用审批——
  审批通道由 `chat_answer` 通过 ChatService 的 pending 注册表实现；拒绝/
  超时都是可见事件。写入会提交到工作区 git（每次写一个 commit，可回退）。
- **门是同一道**：`runtime.permissions.Permissions`（注册表/允许清单/效果
  类/参数 schema 五道检查）。权限显式构造，不依赖 run 的 Task
  （chat 的线程 Task 的 `allowed_tools` 创建后不可变）。
- **先记账后执行**：每个工具调用扣一次 read 预算；事件先落链再执行。
- **结果可复核**：观察结果带 `result_hash` 与 trust 入链，SSE 只发截断预览。

本模块提供构造、审批预览与执行函数；事件与流编排在 `chat_answer.py`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from urllib.parse import urlsplit

from .core.contracts import AgentError
from .runtime.budget import Budget
from .runtime.permissions import Permissions
from .tools.general.commands import CommandPolicy
from .tools.general.fetch import UrlFetcher
from .tools.general.sandbox import WorkspaceSandbox
from .tools.general.tools import build_general_tools
from .tools.registry import ToolRegistry

__all__ = [
    "CHAT_TOOL_NAMES",
    "remember_key",
    "run_command_verdict",
    "fetch_url_info",
    "MAX_CALLS_PER_ROUND",
    "MAX_RESULT_CHARS",
    "build_chat_registry",
    "build_chat_permissions",
    "to_openai_tools",
    "execute_chat_tool",
]

#: 只读工具：任何档位直接执行。
READ_TOOLS = ("ls_workspace", "read_file")
#: 写工具：需要逐调用审批（APPROVAL_TOOLS 与 run 侧同集）。
WRITE_TOOLS = ("write_file", "edit_file", "run_command")
#: 网络读取：逐调用审批（按域记住）。
NETWORK_TOOLS = ("fetch_url",)
#: Phase 1 名字保留：旧调用点用它表示"注册表允许的全部工具"。
CHAT_TOOL_NAMES = READ_TOOLS + WRITE_TOOLS + NETWORK_TOOLS
#: 与 run 侧同口径的审批集（fetch_url 单独按域处理）。
APPROVAL_TOOLS = WRITE_TOOLS

#: 单轮模型最多返回多少个工具调用（超出部分截断并显式标注）。
MAX_CALLS_PER_ROUND = 4

#: 单条工具结果进入模型上下文的截断长度（完整结果在链上）。
MAX_RESULT_CHARS = 4000


def build_chat_registry(workspace_root: str | Path) -> ToolRegistry:
    """工具注册表：general 的同一实现，过滤到只读 + 写工具（无网络）。"""
    sandbox = WorkspaceSandbox(Path(workspace_root))
    fetcher = UrlFetcher()
    specs = build_general_tools(sandbox, fetcher)
    allowed = set(READ_TOOLS) | set(WRITE_TOOLS) | set(NETWORK_TOOLS)
    return ToolRegistry([spec for spec in specs if spec.name in allowed])


def build_chat_permissions(registry: ToolRegistry) -> Permissions:
    """显式权限：允许注册表里的工具，效果 read+write。

    注意：这里的"允许"只是注册表级白名单；逐调用审批由
    chat_answer 的 approval 通道按档位执行（consequential/all）。
    """
    allowed = list(registry.names)
    return Permissions(
        registry,
        allowed_tools=tuple(allowed),
        grant={"allowed_tools": list(allowed)},
        allowed_effects=("read", "write", "network"),
    )


_KIND_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
}


def to_openai_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """ToolSpec → OpenAI tools 数组（function calling 的请求形状）。"""
    tools: list[dict[str, Any]] = []
    for name in registry.names:
        spec = registry.get(name)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in spec.params:
            schema = param.to_schema()
            schema.pop("required", None)
            schema.pop("nullable", None)
            schema["type"] = _KIND_MAP.get(str(schema.get("type")), "string")
            if param.enum is not None:
                schema["enum"] = list(param.enum)
            properties[param.name] = schema
            if param.required:
                required.append(param.name)
        tools.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.summary,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return tools


def run_command_verdict(ctx, parsed: dict[str, Any]) -> dict[str, Any]:
    """run_command 的白名单判定（纯函数，不触文件系统/进程）。

    返回 argv、cwd、白名单判定（allowed/readonly/refused + 拒绝原因）。
    与 run 侧 classify 同语义：readonly=True 表示该命令可证只读。
    """
    sandbox = ctx.get("sandbox")
    command = str(parsed.get("command") or "")
    args = [str(a) for a in parsed.get("args") or []]
    argv = " ".join([command, *args])
    policy = CommandPolicy()
    try:
        rule, checked = policy.resolve(command, args, sandbox)
        readonly = policy.classify(command, args, sandbox) is False
        return {"argv": argv, "cwd": str(ctx.get("sandbox_root") or ""),
                "whitelist": "allowed", "readonly": readonly,
                "program": rule.program, "arg_policy": rule.arg_policy}
    except AgentError as error:
        return {"argv": argv, "whitelist": "refused",
                "reason": error.message, "code": error.code}


def fetch_url_info(parsed: dict[str, Any]) -> dict[str, Any]:
    """fetch_url 的审批信息：URL、域名。"""
    url = str(parsed.get("url") or "")
    host = urlsplit(url).hostname or ""
    return {"url": url, "host": host}


def remember_key(name: str, parsed: dict[str, Any]) -> str:
    """「允许并不再询问」的粒度：run_command 按精确 argv，fetch_url 按域名，
    其余按工具名。防止"批准一条命令 = 本回合任意命令放行"。"""
    if name == "run_command":
        args = json.dumps([str(a) for a in parsed.get("args") or []], ensure_ascii=False)
        return f"run_command:{parsed.get('command')}:{args}"
    if name == "fetch_url":
        host = urlsplit(str(parsed.get("url") or "")).hostname or ""
        return f"fetch_url:{host}"
    return name


def approval_preview(ctx, name: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """审批卡预览：不给执行结果，只给"将发生什么"。

    edit_file → 真实 diff（sandbox.edit_preview，什么都不写）；
    write_file → 真实 diff（diff_preview 对比当前文件）+ 路径/字符数/哈希；
    run_command → 完整 argv + cwd + 白名单判定；
    fetch_url → URL 与域名；其余工具返回空对象（不审批）。
    """
    sandbox = ctx.get("sandbox") if isinstance(ctx, dict) else None
    try:
        if name == "edit_file" and sandbox is not None:
            return sandbox.edit_preview(
                str(parsed.get("path") or ""),
                str(parsed.get("old_text") or ""),
                str(parsed.get("new_text") or ""),
            )
        if name == "write_file" and sandbox is not None:
            import hashlib
            content = str(parsed.get("content") or "")
            path = str(parsed.get("path") or "")
            info = {
                "path": path,
                "content_chars": len(content),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            try:
                # diff_preview 与 write 同一 jail/归一化——审批所见即所写。
                info.update(sandbox.diff_preview(path, content))
            except AgentError as error:
                info["preview_error"] = error.message
            return info
        if name == "run_command":
            return run_command_verdict(ctx, parsed)
        if name == "fetch_url":
            return fetch_url_info(parsed)
    except Exception as error:  # noqa: BLE001 — 预览失败也要让操作员看到原因
        return {"preview_error": getattr(error, "message", str(error))}
    return {}


def execute_chat_tool(
    registry: ToolRegistry,
    permissions: Permissions,
    budget: Budget,
    name: str,
    arguments: str | dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """门 → 扣 read 预算 → 执行。

    返回 ``(observation, error)``：成功时 error 为 None；拒绝/失败时 observation
    为 None，error 带 code/message（调用方负责落链与下发）。
    """
    try:
        parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
    except ValueError:
        return None, {"code": "TOOL_ARGUMENTS_INVALID", "message": "arguments is not valid JSON"}
    if not isinstance(parsed, dict):
        return None, {"code": "TOOL_ARGUMENTS_INVALID", "message": "arguments must be an object"}
    decision = permissions.gate(name, parsed)
    if not decision.allowed:
        return None, {"code": decision.code or "TOOL_DENIED",
                      "message": decision.reason or "tool call refused by the host gate"}
    try:
        budget.charge_read()
    except AgentError as error:
        return None, {"code": error.code, "message": error.message}
    try:
        observation = registry.execute(name, parsed)
    except AgentError as error:
        return None, {"code": error.code, "message": error.message}
    return observation.to_dict(), None