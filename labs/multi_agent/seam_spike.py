"""Seam spike: prove `plan_steered` can seed the Loop before it runs.

Read-only experiment; no core changes. The plan has three todos:
t1 list_documents -> t2 read_document(doc_fy2024) -> s3 report_blocked.
t1/t2 are blueprint ids so FinancePlanner can settle them; s3 is a lab-only
terminal that lets the run stop honestly instead of hitting PLAN_EXHAUSTED.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from flowagent.core.plan import Plan, Todo
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
from flowagent.trajectory.exporter import verify_trace
from flowagent.trajectory.recorder import TraceRecorder


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fcode-seam-") as tmp:
        root = Path(tmp)
        store = TaskStore(root / "task.sqlite")
        world = FinanceWorld()
        registry = ToolRegistry(build_finance_tools(world))
        receipts = ReceiptStore(store)
        provider = MockProvider(policy="research")
        harness = Harness(
            store=store,
            registry=registry,
            provider=provider,
            world=world,
            limits=BudgetLimits(max_calls=5, max_reads=5, max_cost_microusd=5_000, max_tokens=5_000),
            receipts=receipts,
            verifier=lambda observation: verify_observation(world, observation),
        )
        allowed = ["list_documents", "read_document", "report_blocked"]
        task = harness.create(
            task_id="seam-plan-steered-0001",
            objective="seam spike: prove plan_steered injection",
            principal="seam-spike",
            binding={"scenario": "seam-spike", "corpus": "synthetic-authored-v1"},
            allowed_tools=allowed,
            grant={
                "grant_id": "seam-grant",
                "revision": 1,
                "allowed_tools": allowed,
                "max_calls": 5,
                "max_reads": 5,
                "max_cost_microusd": 5_000,
                "max_tokens": 5_000,
            },
        )

        plan = Plan(
            goal=task.objective,
            revision=1,
            todos=[
                Todo("t1", "列出资料", "list_documents", "listed"),
                Todo("t2", "读取 FY2024", "read_document", "read:doc_fy2024", {"doc_id": "doc_fy2024"}),
                Todo("s3", "测试终止：如实受阻", "report_blocked", "reported",
                     {"reason": "insufficient_evidence"}),
            ],
        )
        task = harness.record(task, "plan_steered", {"plan": plan.to_dict(), "reason": "seam-spike"})

        loop = Loop(
            harness,
            planner=FinancePlanner(),
            max_rounds=5,
            result_tools=FINANCE_DOMAIN.result_tools,
        )
        loaded = store.load(task.id)
        rebuilt = loop.rebuild_plan(loaded, loop.restore(loaded))
        rebuilt_tools = [todo.tool for todo in rebuilt.todos]
        expected_tools = ["list_documents", "read_document", "report_blocked"]
        assert rebuilt_tools == expected_tools, (rebuilt_tools, expected_tools)

        recorder = TraceRecorder(
            store, provider_name=provider.name, model_name=provider.model
        )
        task = Loop(
            harness,
            planner=FinancePlanner(),
            max_rounds=5,
            observer=recorder,
            result_tools=FINANCE_DOMAIN.result_tools,
        ).run(task.id)

        events = store.events(task.id)
        started = [
            event["payload"]["detail"]["tool"]
            for event in events
            if event["type"] == "tool_started"
        ]
        assert started == expected_tools, (started, expected_tools)
        assert task.status == "failed", task.status
        assert (task.failure or {}).get("code") == "INSUFFICIENT_EVIDENCE", task.failure
        stopped = [
            event["payload"].get("detail", {}).get("code")
            for event in events if event["type"] == "task_stopped"
        ]
        assert stopped == ["MODEL_REPORTED_BLOCKED"], stopped
        trace = recorder.build(store.load(task.id))
        verification = verify_trace(trace)
        assert verification["verified"]
        assert any(event["type"] == "plan_steered" for event in events)

        print(json.dumps({
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "failure_code": (task.failure or {}).get("code"),
            "rebuilt_tools": rebuilt_tools,
            "started_tools": started,
            "events": len(events),
            "chain_head": verification["head_hash"],
        }, ensure_ascii=False, indent=2))
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
