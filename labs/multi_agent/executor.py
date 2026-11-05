"""Execute exactly one child run with the existing FCode runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowagent.core.hashing import canonical_json
from flowagent.core.plan import Plan
from flowagent.core.receipts import ReceiptStore
from flowagent.core.store import TaskStore
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation
from flowagent.providers.mock import MockProvider
from flowagent.runtime.budget import BudgetLimits
from flowagent.runtime.harness import Harness
from flowagent.runtime.loop import Loop
from flowagent.tools.finance.fixtures import FinanceWorld
from flowagent.tools.finance.planner import FinancePlanner
from flowagent.tools.finance.tools import build_finance_tools
from flowagent.tools.registry import ToolRegistry
from flowagent.trajectory.exporter import export_trace, verify_trace
from flowagent.trajectory.recorder import TraceRecorder

from .fixtures import ToyWorld, build_toy_tools, verify_toy
from .state import ChildResult

__all__ = ["ChildExecutor"]


class ChildExecutor:
    """One child task, one runtime Loop, no role awareness inside the core."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(
        self,
        *,
        run_id: str,
        spec: dict[str, Any],
        plan: Plan,
        attempt: int,
        limits: BudgetLimits,
    ) -> ChildResult:
        task_id = f"{run_id}-a{attempt}"
        child_dir = self.root / "children" / task_id
        child_dir.mkdir(parents=True, exist_ok=True)
        store = TaskStore(child_dir / "task.sqlite")

        domain = spec.get("domain", "finance")
        if domain == "toy":
            world = ToyWorld()
            registry = ToolRegistry(build_toy_tools(world))
            verifier = lambda observation: verify_toy(world, observation)  # noqa: E731
        else:
            world = FinanceWorld(partial=bool(spec.get("partial_corpus")))
            registry = ToolRegistry(build_finance_tools(world))
            verifier = lambda observation: verify_observation(world, observation)  # noqa: E731

        provider = MockProvider(
            policy=str(spec.get("policy") or "research"),
            fail_at_call=spec.get("fail_at_call"),
        )
        receipts = ReceiptStore(store)
        harness = Harness(
            store=store,
            registry=registry,
            provider=provider,
            world=world,
            limits=limits,
            receipts=receipts,
            verifier=verifier,
        )
        allowed = list(spec.get("allowed_tools") or [])
        task = harness.create(
            task_id=task_id,
            objective=str(spec.get("objective") or ""),
            principal="multi-agent-executor",
            binding={
                "run_id": run_id,
                "attempt": attempt,
                "role": "executor",
                "domain": domain,
            },
            allowed_tools=allowed,
            grant=dict(spec.get("grant") or {}),
        )
        task = harness.record(task, "plan_steered", {
            "plan": plan.to_dict(),
            "reason": "multi-agent-planner",
        })

        recorder = TraceRecorder(store, provider_name=provider.name, model_name=provider.model)
        task = Loop(
            harness,
            planner=FinancePlanner(),
            max_rounds=int(spec.get("max_rounds") or 12),
            observer=recorder,
            result_tools=FINANCE_DOMAIN.result_tools,
        ).run(task.id)

        events = store.events(task.id)
        budget = harness.budget(task).snapshot()
        trace = recorder.build(store.load(task.id))
        trace_path = export_trace(trace, child_dir / "trace.json")
        verification = verify_trace(trace)
        tools = [
            (event["payload"]["detail"]["tool"], canonical_json(event["payload"]["detail"]["arguments"]))
            for event in events
            if event["type"] == "tool_started"
        ]
        failure = task.failure or {}
        result = ChildResult(
            task_id=task_id,
            attempt=attempt,
            runtime_status=task.status,
            trace_path=str(trace_path),
            chain_head=verification["head_hash"],
            calls=int(budget.get("calls_used") or 0),
            reads=int(budget.get("reads_used") or 0),
            cost_microusd=int(budget.get("cost_used_microusd") or 0),
            tokens=int(budget.get("tokens_used") or 0),
            error_class=str(failure.get("code") or ""),
            failure_code=str(failure.get("code") or ""),
            tool_signatures=tools,
            task_db=str(child_dir / "task.sqlite"),
            event_count=len(events),
        )
        store.close()
        return result
