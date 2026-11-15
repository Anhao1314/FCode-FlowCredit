"""双引擎等价性：GraphLoop（LangGraph 编排）与原生 Loop 逐事件对齐。

迁移验收线：同一场景、同一输入，两个引擎的事件类型序列、终态、结果必须一致
——编排层换了，治理语义一个字节都不能漂。
langgraph 未安装时跳过（仓库本体保持零依赖）。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    import langgraph  # noqa: F401

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False

from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import (  # noqa: E402
    FINANCE_DOMAIN,
    verify_observation as verify_finance,
)
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.engine import build_loop  # noqa: E402
from flowagent.runtime.graph_loop import GraphLoop  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _event_types(store: TaskStore, task_id: str) -> list[str]:
    return [event["type"] for event in store.events(task_id)]


@unittest.skipUnless(_HAS_LANGGRAPH, "langgraph is not installed")
class FinanceParityTest(unittest.TestCase):
    """同一 finance 场景：原生 vs LangGraph 编排，逐事件对齐。"""

    def _run(self, engine: str):
        from unittest import mock

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scenario = get_scenario("full")
        store = TaskStore(Path(tmp.name) / "task.sqlite")
        self.addCleanup(store.close)
        world = FinanceWorld(partial=scenario.partial_corpus)
        harness = Harness(
            store=store,
            registry=ToolRegistry(build_finance_tools(world)),
            provider=MockProvider(policy=scenario.policy),
            world=world,
            verifier=lambda observation: verify_finance(world, observation),
            limits=scenario.limits,
            receipts=ReceiptStore(store),
        )
        task = harness.create(
            task_id=f"task-parity-{engine}",
            objective=scenario.objective,
            principal="test",
            binding={},
            allowed_tools=list(scenario.allowed_tools),
            grant=dict(scenario.grant),
        )
        with mock.patch.dict(os.environ, {"FCODE_ENGINE": engine}):
            loop = build_loop(
                harness=harness,
                planner=FinancePlanner(),
                max_rounds=scenario.max_rounds,
                result_tools=FINANCE_DOMAIN.result_tools,
            )
        self.assertIsInstance(loop, (Loop, GraphLoop))
        if engine == "langgraph":
            self.assertIsInstance(loop, GraphLoop)
        else:
            self.assertNotIsInstance(loop, GraphLoop)
        final = loop.run(task.id)
        return final, _event_types(store, task.id)

    def test_native_and_graph_produce_identical_chains(self) -> None:
        native_final, native_events = self._run("native")
        graph_final, graph_events = self._run("langgraph")
        self.assertEqual(native_final.status, "succeeded")
        self.assertEqual(graph_final.status, "succeeded", "LangGraph 编排必须同判定")
        self.assertEqual(native_events, graph_events,
                         "事件序列必须逐类型对齐（含顺序）")
        self.assertEqual(native_final.result, graph_final.result,
                         "验证产物必须一致")
        # 预算口径也必须一致：编排换了，计费不能漂
        native_budget = self._run("native")[0]
        graph_budget = self._run("langgraph")[0]
        self.assertEqual(
            native_budget.model.attempts, graph_budget.model.attempts)


if __name__ == "__main__":
    unittest.main()