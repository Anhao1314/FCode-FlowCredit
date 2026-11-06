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
from ..base import EFFECT_WRITE, Param, ToolSpec
from . import summaries

__all__ = ["MAX_SPAWN_OBJECTIVES", "OBJECTIVE_CHARS", "build_spawn_tool"]


def _out(name: str, kind: str, required: bool = True, nullable: bool = False) -> Param:
    return Param(name=name, kind=kind, required=required, nullable=nullable)

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
        # 数量/深度/长度的硬限由 console 通道执行（那是审批承诺的记账方）；
        # 这里只做最便宜的结构防呆，让明显的误用不用走一趟进程边界。
        cleaned = [item.strip() for item in objectives if item.strip()]
        if not cleaned:
            raise AgentError(
                "SPAWN_OBJECTIVES_REQUIRED",
                "at least one non-empty objective is required",
            )
        result = spawner(cleaned)
        if not isinstance(result, dict):
            raise AgentError("SPAWN_CHANNEL_INVALID", "orchestration channel returned no digest")
        return result

    return ToolSpec(
        name="spawn_subagents",
        summarize=summaries.summarize_spawn_subagents,
        summary=(
            "Spawn 1-4 read-only subagent runs in parallel over this workspace, "
            "wait for all of them to finish, and return a bounded digest per child"
        ),
        effect=EFFECT_WRITE,
        params=(
            Param(
                name="objectives",
                kind="array",
                required=True,
                description=(
                    "1-4 self-contained child objectives; each child is a read-only "
                    "researcher over this workspace and cannot see the other children"
                ),
            ),
        ),
        handler=handler,
        outputs=(
            _out("children", "array"),
            _out("timed_out", "boolean"),
        ),
        version="1.0",
    )


