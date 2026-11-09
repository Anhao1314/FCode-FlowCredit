"""预算延展通道（S2a）：长任务在批前申请加预算的封底逻辑。

原则：**追加要有封底、要有理由、要能被账本证明。**
- 单次申请任一维度 ≤ 原始上限的 50%；
- 累计追加 ≤ 原始上限（总预算最多翻倍）；
- reason 必填（给审批人看）；
- 已授予的追加量由通道自己记账（进程内存），原始上限取 driver 构造时的
  limits——两者都不随追加漂移，重复申请不能绕过封底。

审批由运行侧 RunControl 对 effect=write 的工具统一执行（consequential/all
走审批卡，off 自动放行）；本模块只管"批不批得下来"的数值判定与生效。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from flowagent.core.contracts import AgentError

__all__ = ["BudgetChannel"]

#: reason 上限：与 report_blocked 的 600 字一致。
MAX_REASON_CHARS = 600


class BudgetChannel:
    """One run's budget-extension channel (driver duck-typed)."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        #: 原始上限由驱动在 limits 定稿后写入 driver.base_limits（懒解析：
        #: 通道必须在 _build_world 之前就存在，而 limits 在其后才算得出来）。
        self._granted_calls = 0
        self._granted_cost = 0

    @property
    def _base(self) -> Any:
        return getattr(self._driver, "base_limits", None) or self._driver.limits

    def _validate_amount(self, name: str, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise AgentError(
                "BUDGET_EXTENSION_INVALID",
                f"{name} must be an integer",
                details={"field": name},
            )
        if value < 0:
            raise AgentError(
                "BUDGET_EXTENSION_INVALID",
                f"{name} must be non-negative",
                details={"field": name},
            )
        return value

    def request(
        self,
        *,
        reason: str,
        extra_calls: Any = None,
        extra_cost_microusd: Any = None,
    ) -> dict[str, Any]:
        text = str(reason or "").strip()
        if not text:
            raise AgentError(
                "BUDGET_EXTENSION_INVALID",
                "a reason is required: budget extensions are operator decisions",
            )
        if len(text) > MAX_REASON_CHARS:
            raise AgentError(
                "BUDGET_EXTENSION_INVALID",
                f"reason must be at most {MAX_REASON_CHARS} characters",
            )
        calls = self._validate_amount("extra_calls", extra_calls)
        cost = self._validate_amount("extra_cost_microusd", extra_cost_microusd)
        if calls == 0 and cost == 0:
            raise AgentError(
                "BUDGET_EXTENSION_INVALID",
                "request at least one dimension: extra_calls or extra_cost_microusd",
            )
        # 封底：单次 ≤ 原始 50%，累计 ≤ 原始 100%（总预算最多翻倍）。
        # 超出封底时**夹取而不是拒绝**：治理边界内可获得的部分照给，并把
        # "申请了多少、实际给了多少、还剩多少额度"如实回给模型——拒绝会
        # 终结运行，而通常模型只是要得比封底多（真实踩过：+10/封底 4）。
        base = self._base
        requested_calls, requested_cost = calls, cost
        calls = min(calls, max(1, base.max_calls // 2))
        cost = min(cost, max(1, base.max_cost_microusd // 2))
        calls = max(0, min(calls, base.max_calls - self._granted_calls))
        cost = max(0, min(cost, base.max_cost_microusd - self._granted_cost))
        if calls == 0 and cost == 0:
            raise AgentError(
                "BUDGET_EXTENSION_EXHAUSTED",
                "no extension headroom remains for this run "
                f"(granted so far: +{self._granted_calls} calls / +{self._granted_cost} µUSD)",
                details={"cap": "cumulative", "dimension": "both"},
            )
        capped = (calls, cost) != (requested_calls, requested_cost)
        current = self._driver.harness.limits
        extended = replace(
            current,
            max_calls=current.max_calls + calls,
            max_cost_microusd=current.max_cost_microusd + cost,
        )
        # 运行中生效：harness.budget() 每轮从 harness.limits 取新值。
        self._driver.harness.limits = extended
        self._driver.limits = extended
        self._granted_calls += calls
        self._granted_cost += cost
        return {
            "granted": True,
            "extra_calls": calls,
            "extra_cost_microusd": cost,
            "requested_calls": requested_calls,
            "requested_cost_microusd": requested_cost,
            "capped": capped,
            "reason": text,
            "limits": extended.to_dict(),
        }
