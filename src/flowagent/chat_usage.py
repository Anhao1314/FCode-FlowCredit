"""Chat token/cost reservation and provider-reported usage settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chat_provider import _cost, _estimate_tokens
from .runtime.budget import Budget


@dataclass
class AnswerAccounting:
    budget: Budget
    prompt_estimate: int
    reserved_tokens: int
    reserved_cost: int

    @classmethod
    def prepare(cls, service, thread_id: str, messages: list[dict], config) -> "AnswerAccounting":
        prompt_estimate = _estimate_tokens(messages)
        reserved_tokens = prompt_estimate + config.max_completion_tokens
        reserved_cost = _cost(prompt_estimate, config.max_completion_tokens, config)
        budget = Budget(service.store, f"chat:{thread_id}", service.limits())
        return cls(budget, prompt_estimate, reserved_tokens, reserved_cost)

    def reserve(self) -> None:
        self.budget.charge_call(
            tokens=self.reserved_tokens, cost_microusd=self.reserved_cost,
        )

    def snapshot(self) -> dict[str, Any]:
        return self.budget.snapshot()

    def settle(self, usage: dict[str, Any], text: str, latency_ms: int) -> dict[str, Any]:
        prompt_tokens = int(usage.get("prompt_tokens") or self.prompt_estimate)
        completion_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 3))
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "latency_ms": latency_ms,
        }

    def settle_cost(self, config, usage: dict[str, Any]) -> int:
        actual_cost = _cost(usage["prompt_tokens"], usage["completion_tokens"], config)
        self.budget.settle_call(
            reserved_tokens=self.reserved_tokens,
            reserved_cost_microusd=self.reserved_cost,
            actual_tokens=usage["total_tokens"],
            actual_cost_microusd=actual_cost,
        )
        return actual_cost
