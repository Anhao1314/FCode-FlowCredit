"""``spawn_subagents``: 一次审批过的并行扇出。

设计要点（2026-11 子代理批次，依据 Codex / Claude Code / Anthropic 研究系统
的收敛做法与 Cognition 的反例）：

* **子代理是只读研究员**：直接读父工作区（共享只读，零拷贝零合并），
  各自拥有独立的上下文窗口。可写子代理（own worktree/分支）是 v2。
* **阻塞式 join**：工具调用创建子运行并等待全部终态，摘要作为本工具的
  观察回给模型——父模型的循环协议零改动。审计读到的就是这一次工具调用。
* **放大要有意识**：effect 是 write，进审批集；审批卡上明示乘数。
* **深度硬限 1**：由 console 侧的通道拒绝（这里只做参数整形）。

没有通道的部署（chat、离线单测）诚实地不注册这个工具——能力缺席
不是假装失败。
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_READ, EFFECT_WRITE, Param, ToolSpec
from . import summaries

__all__ = ["MAX_SPAWN_OBJECTIVES", "OBJECTIVE_CHARS", "build_spawn_tool"]


def _out(name: str, kind: str, required: bool = True, nullable: bool = False) -> Param:
    return Param(name=name, kind=kind, required=required, nullable=nullable)


def build_subagent_status_tool(status: Callable[[], dict[str, Any]]) -> ToolSpec:
    """Non-blocking peek at this run's children. Read effect."""

    def handler(_: dict[str, Any]) -> dict[str, Any]:
        result = status()
        if not isinstance(result, dict):
            raise AgentError("SUBAGENT_CHANNEL_INVALID", "status channel returned no payload")
        return result

    return ToolSpec(
        name="subagent_status",
        summarize=summaries.summarize_subagent_status,
        summary="List this run's subagent runs with their current status (non-blocking)",
        effect=EFFECT_READ,
        params=(),
        handler=handler,
        outputs=(
            _out("children", "array"),
            _out("count", "integer"),
        ),
        version="1.0",
    )


#: A child-artifact read is a file read; receipts carry the content hash.
CHILD_READ_PATH_CHARS = 300


def build_subagent_read_tool(read: Callable[..., dict[str, Any]]) -> ToolSpec:
    """Read one file from a child subagent's workspace (jailed to own children)."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = str(arguments.get("run_id") or "").strip()
        path = str(arguments.get("path") or "").strip()
        if not run_id or not path:
            raise AgentError(
                "SUBAGENT_READ_ARGS_REQUIRED",
                "run_id and path are both required",
            )
        if len(path) > CHILD_READ_PATH_CHARS:
            raise AgentError("SUBAGENT_READ_PATH_TOO_LONG", "path is too long")
        result = read(run_id, path)
        if not isinstance(result, dict):
            raise AgentError("SUBAGENT_CHANNEL_INVALID", "read channel returned no payload")
        return result

    return ToolSpec(
        name="subagent_read",
        summarize=summaries.summarize_subagent_read,
        summary="Read one file from a child subagent's workspace (only your own children; path jailed)",
        effect=EFFECT_READ,
        params=(
            Param(name="run_id", kind="string", required=True,
                  description="run id of the child subagent (from spawn_subagents result)"),
            Param(name="path", kind="string", required=True,
                  description="path relative to the child's workspace root"),
        ),
        handler=handler,
        outputs=(
            _out("path", "string"),
            _out("content", "string"),
            _out("sha256", "string"),
            _out("bytes", "integer"),
            _out("child_run_id", "string"),
        ),
        version="1.0",
    )

MAX_SPAWN_OBJECTIVES = 4
OBJECTIVE_CHARS = 600


def build_spawn_tool(spawner: Callable[[list[str]], dict[str, Any]]) -> ToolSpec:
    """The tool, bound to one run's orchestration channel (or to none)."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        raw = arguments.get("objectives")
        if not isinstance(raw, list):
            raise AgentError(
                "SPAWN_OBJECTIVES_REQUIRED",
                "objectives must be a list of child task objectives (1-4 strings)",
            )
        objectives = [str(item or "") for item in raw[:MAX_SPAWN_OBJECTIVES + 4]]
        # 数量/深度/长度/预算的硬限由 console 通道执行（那是审批承诺的记账方）；
        # 这里只做最便宜的结构防呆，让明显的误用不用走一趟进程边界。
        cleaned = [item.strip() for item in objectives if item.strip()]
        if not cleaned:
            raise AgentError(
                "SPAWN_OBJECTIVES_REQUIRED",
                "at least one non-empty objective is required",
            )
        # 扁平参数 → 驼峰 budget dict(通道与审批承诺都认规范形)
        budget = {
            "maxCalls": arguments.get("budget_max_calls"),
            "maxReads": arguments.get("budget_max_reads"),
            "maxCostMicrousd": arguments.get("budget_max_cost_microusd"),
            "maxTokens": arguments.get("budget_max_tokens"),
        }
        budget = {k: v for k, v in budget.items() if v is not None} or None
        result = spawner({"objectives": cleaned, "budget": budget})
        if not isinstance(result, dict):
            raise AgentError("SPAWN_CHANNEL_INVALID", "orchestration channel returned no digest")
        return result

    return ToolSpec(
        name="spawn_subagents",
        summarize=summaries.summarize_spawn_subagents,
        summary=(
            "Spawn 1-4 subagent runs in parallel (each writes into its own sandbox), "
            "wait for all of them to finish, and return a bounded digest per child"
        ),
        effect=EFFECT_WRITE,
        params=(
            Param(
                name="objectives",
                kind="array",
                required=True,
                description=(
                    "1-4 self-contained child objectives; each child is a researcher "
                    "over its own sandbox and cannot see the other children"
                ),
            ),
            # 预算覆盖用四个扁平整数参数(kind=object 不被参数 schema 支持,
            # 与 MCP flat schema 同一教训);封底直接写进 min/max。
            Param(
                name="budget_max_calls",
                kind="integer",
                required=False,
                minimum=1, maximum=24,
                description="optional per-child budget: max tool calls (1-24)",
            ),
            Param(
                name="budget_max_reads",
                kind="integer",
                required=False,
                minimum=1, maximum=64,
                description="optional per-child budget: max file reads (1-64)",
            ),
            Param(
                name="budget_max_cost_microusd",
                kind="integer",
                required=False,
                minimum=1, maximum=1_000_000,
                description="optional per-child budget: max cost in µUSD (1-1,000,000)",
            ),
            Param(
                name="budget_max_tokens",
                kind="integer",
                required=False,
                minimum=1, maximum=200_000,
                description="optional per-child budget: max tokens (1-200,000)",
            ),
        ),
        handler=handler,
        outputs=(
            _out("children", "array"),
            _out("timed_out", "boolean"),
        ),
        version="1.0",
    )


