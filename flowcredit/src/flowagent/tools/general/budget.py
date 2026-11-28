"""``request_budget_extension``: 长任务在批前申请加预算。

治理语义（S1）：

* **追加必须有理由**：reason 是给审批人看的，空理由拒绝——扩展预算不是
  静默能力，是操作员决策。
* **审批门后追加**：effect=write 且进运行侧审批集——consequential/all 档
  走审批卡，off 档自动放行但仍留观察记录；追加后的 limits 会出现在
  后续每轮的 budget_after 快照里（trace 可证）。
* **封底写死在通道里**：单次申请 ≤ 原始上限的 50%，累计追加 ≤ 原始上限
  （总预算最多翻倍）。calls / cost / tokens 三个维度同一公式。
* **提交与生效原子**：工具结果与 ``budget_adjusted`` 在同一事务提交；
  只有提交成功后才把新上限装进内存（见 ``ToolSpec.audit``）。
* 没有通道的部署（chat/子代理）诚实地不注册此工具。
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_WRITE, Param, ToolAudit, ToolSpec
from . import summaries

__all__ = ["TOOL_NAME", "build_budget_extension_tool"]

TOOL_NAME = "request_budget_extension"


def build_budget_extension_tool(
    request: Callable[..., dict[str, Any]],
    *,
    channel: Any = None,
) -> ToolSpec:
    """Build the extension tool from a channel (driver duck-typed)."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = request(
                reason=str(arguments.get("reason") or ""),
                extra_calls=arguments.get("extra_calls"),
                extra_cost_microusd=arguments.get("extra_cost_microusd"),
                extra_tokens=arguments.get("extra_tokens"),
            )
        except AgentError:
            raise
        except Exception as error:  # noqa: BLE001 — 通道异常归一为可读拒绝
            raise AgentError("BUDGET_EXTENSION_FAILED", str(error)[:300]) from error
        if not isinstance(result, dict) or not result.get("granted"):
            raise AgentError(
                "BUDGET_EXTENSION_DENIED",
                "budget extension was not granted",
                details={"result": result if isinstance(result, dict) else None},
            )
        return result

    def audit(result: dict[str, Any]) -> ToolAudit | None:
        if channel is None or not result.get("request_id"):
            return None
        return ToolAudit(
            event="budget_adjusted",
            detail=channel.audit_detail(result),
            apply=lambda: channel.apply(result),
        )

    return ToolSpec(
        name=TOOL_NAME,
        summarize=summaries.summarize_budget_extension,
        summary=(
            "Request additional run budget (extra model calls, cost µUSD and/or "
            "tokens) with a reason. Requires operator approval; budgets never "
            "change silently."
        ),
        effect=EFFECT_WRITE,
        params=(
            Param(name="reason", kind="string", required=True,
                  description="Why more budget is needed (shown to the approver)"),
            Param(name="extra_calls", kind="integer", required=False,
                  description="Additional model calls to request (default 0)"),
            Param(name="extra_cost_microusd", kind="integer", required=False,
                  description="Additional cost budget in µUSD to request (default 0)"),
            Param(name="extra_tokens", kind="integer", required=False,
                  description="Additional token budget to request (default 0)"),
        ),
        handler=handler,
        audit=audit,
        outputs=(
            Param(name="granted", kind="boolean"),
            Param(name="request_id", kind="string"),
            Param(name="extra_calls", kind="integer"),
            Param(name="extra_cost_microusd", kind="integer"),
            Param(name="extra_tokens", kind="integer"),
        ),
        version="1.1",
    )
