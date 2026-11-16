"""``apply_child_changes``: 把写子代理的隔离拷贝改动显式应用到父工作区。

治理语义（S2b）：
* **应用是 write effect**：进运行侧审批集——操作员批准的是「哪个子代理的
  哪些文件落进父工作区」，不是笼统的"合并"。
* **逐文件冲突检测**（在通道里）：父在子代理运行期间改过同一文件即拒绝，
  绝不用旧子版本覆盖父的新改动。
* **封底**：一次最多 20 个文件 / 2MB；删除不自动应用（只报告）。
* paths 缺省 = 全部已变更文件（增改）；可用 paths 分批。
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_WRITE, Param, ToolSpec
from . import summaries

__all__ = ["build_apply_child_changes_tool"]


def build_apply_child_changes_tool(apply: Callable[..., dict[str, Any]]) -> ToolSpec:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = apply(
                run_id=str(arguments.get("child_run_id") or ""),
                paths=arguments.get("paths") or [],
            )
        except AgentError:
            raise
        except ValueError as error:
            raise AgentError("APPLY_CHANGES_REFUSED", str(error)[:300]) from error
        except Exception as error:  # noqa: BLE001 — 通道异常归一为可读失败
            raise AgentError("APPLY_CHANGES_FAILED", str(error)[:300]) from error
        if not isinstance(result, dict):
            raise AgentError("APPLY_CHANGES_FAILED", "apply channel returned no payload")
        return result

    return ToolSpec(
        name="apply_child_changes",
        summarize=summaries.summarize_apply_child_changes,
        summary=(
            "Apply a write-subagent's isolated workspace changes into your workspace. "
            "Per-file conflict detection: files you changed meanwhile are refused. "
            "Omit paths to apply all modified/added files; deletions are only reported."
        ),
        effect=EFFECT_WRITE,
        params=(
            Param(name="child_run_id", kind="string", required=True,
                  description="The child run id returned by spawn_subagents"),
            Param(name="paths", kind="array", required=False,
                  description="Optional subset of relative file paths to apply"),
        ),
        handler=handler,
        outputs=(
            Param(name="applied", kind="array"),
            Param(name="conflicts", kind="array"),
            Param(name="unchanged", kind="array"),
            Param(name="refused", kind="array"),
            Param(name="deleted_reported", kind="array"),
        ),
        version="1.0",
    )
