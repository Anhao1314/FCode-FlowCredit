"""预算延展通道（S1）：模型工具、operator、resume 共用一套封底。

原则：**追加要有封底、要有理由、要能被账本证明。**

- 原始上限（base）是 Run 创建时价格缩放后的不可变值；
- 单次任一维度 ≤ base 的 50%，累计 ≤ base（总预算最多翻倍）；
- reason 必填（给审批人看）；
- 计算与提交分离：``request`` 只规划，``apply`` 只在 ``tool_succeeded``
  与 ``budget_adjusted`` 同一事务提交成功之后才更新内存预算；
- request_id 由宿主生成并写入事件，重放同一 request_id 幂等；不同
  request_id 的相同 delta 是两次独立申请（仍受封底约束）。
"""

from __future__ import annotations

import uuid
from typing import Any

from flowagent.core.contracts import AgentError
from flowagent.runtime.budget import BudgetAdjustment, plan_adjustment

__all__ = ["BudgetChannel"]

#: reason 上限：与 report_blocked 的 600 字一致。
MAX_REASON_CHARS = 600


class BudgetChannel:
    """One run's budget-extension channel (driver duck-typed)."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    @property
    def _base(self) -> Any:
        return getattr(self._driver, "base_limits", None) or self._driver.limits

    @property
    def _current(self) -> Any:
        return self._driver.harness.limits

    def _decision_source(self) -> str:
        approval = str(getattr(getattr(self._driver, "run", None), "approval", "") or "")
        return "configured_policy_auto" if approval == "off" else "operator"

    def request(
        self,
        *,
        reason: str,
        extra_calls: Any = None,
        extra_cost_microusd: Any = None,
        extra_tokens: Any = None,
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
        adjustment: BudgetAdjustment = plan_adjustment(
            self._base,
            self._current,
            delta={
                "calls": extra_calls,
                "cost_microusd": extra_cost_microusd,
                "tokens": extra_tokens,
            },
        )
        if adjustment.exhausted:
            raise AgentError(
                "BUDGET_EXTENSION_EXHAUSTED",
                "no extension headroom remains for this run",
                details={"requested": adjustment.requested},
            )
        request_id = f"budget-{uuid.uuid4().hex[:16]}"
        return {
            "granted": True,
            "request_id": request_id,
            "decision_source": self._decision_source(),
            "reason": text,
            "before": adjustment.before.to_dict(),
            "after": adjustment.after.to_dict(),
            "effective": adjustment.after.to_dict(),
            "requested": adjustment.requested,
            "granted_dimensions": adjustment.granted,
            "capped_dimensions": adjustment.capped,
            "exhausted": adjustment.exhausted,
            # Compatibility keys used by earlier callers/tests.
            "extra_calls": adjustment.granted["calls"],
            "extra_cost_microusd": adjustment.granted["cost_microusd"],
            "extra_tokens": adjustment.granted["tokens"],
            "requested_calls": adjustment.requested["calls"],
            "requested_cost_microusd": adjustment.requested["cost_microusd"],
            "requested_tokens": adjustment.requested["tokens"],
            "capped": any(adjustment.capped.values()),
            "limits": adjustment.after.to_dict(),
        }

    def audit_detail(self, result: dict[str, Any]) -> dict[str, Any]:
        """The chain fact committed beside ``tool_succeeded``."""
        return {
            "policy_version": 2,
            "request_id": result.get("request_id"),
            "by": "model-request",
            "decision_source": result.get("decision_source"),
            "reason": result.get("reason"),
            "before": result.get("before"),
            "after": result.get("after"),
            "requested": result.get("requested"),
            "granted": result.get("granted_dimensions"),
            "capped": result.get("capped_dimensions"),
        }

    def apply(self, result: dict[str, Any]) -> None:
        """Install a committed adjustment. Idempotent for the same request."""
        current = self._current
        before = dict(result.get("before") or {})
        after = dict(result.get("after") or {})
        current_values = current.to_dict()
        if current_values == after:
            return
        if current_values != before:
            raise AgentError(
                "BUDGET_APPLY_CONFLICT",
                "the committed budget adjustment no longer matches the live limits",
            )
        target = type(current)(**after)
        self._driver.harness.limits = target
        self._driver.limits = target
