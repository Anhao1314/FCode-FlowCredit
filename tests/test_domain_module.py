"""Domain modules: the kernel hosts them without knowing their nouns.

Each module supplies world, tools, planner and verifier on its own; the
kernel's only remaining duty is to refuse a result the module cannot
independently recompute.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.domains import get_domain, list_domains  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class DomainModuleTests(unittest.TestCase):
    def test_registry_exposes_the_general_and_reference_modules(self) -> None:
        self.assertIs(get_domain("general-work"), GENERAL_DOMAIN)
        self.assertIs(get_domain("finance-research"), FINANCE_DOMAIN)
        self.assertEqual(
            {module.key for module in list_domains()},
            {"general-work", "finance-research"},
        )
        # Registration order is the console's default preference: general first.
        self.assertEqual(list_domains()[0].key, "general-work")
        with self.assertRaises(KeyError):
            get_domain("audit")

    def test_general_module_declares_its_own_answer_and_template(self) -> None:
        self.assertEqual(GENERAL_DOMAIN.result_tools, ("write_file",))
        self.assertEqual(GENERAL_DOMAIN.report_template, "generic")
        self.assertEqual(GENERAL_DOMAIN.approval_tools, ("write_file", "edit_file", "run_command"))
        self.assertIsNotNone(GENERAL_DOMAIN.make_planner(
            GENERAL_DOMAIN.create_world(spec={"sources": ["https://example.com/doc"]}),
        ).decompose("调研 example.com"))

    def test_module_supplies_world_tools_and_planner(self) -> None:
        world = FINANCE_DOMAIN.create_world()
        tools = FINANCE_DOMAIN.build_tools(world)
        names = [tool.name for tool in tools]
        self.assertEqual(len(names), 16)
        self.assertEqual(names[:7], [
            "list_documents", "read_document", "normalize_units",
            "reconcile_basis", "check_restatement", "calculate_margin", "report_blocked",
        ])
        self.assertIsNotNone(FINANCE_DOMAIN.make_planner().decompose("任意目标"))
        self.assertIn("working_paper_append", FINANCE_DOMAIN.approval_tools)

    def test_verifier_recomputes_and_rejects_non_results(self) -> None:
        world = FinanceWorld()
        # An observation that never computed a margin is not verifiable.
        with self.assertRaises(AgentError) as ctx:
            verify_observation(world, Observation(tool="read_document", arguments={}, result={"id": "doc"}))
        self.assertEqual(ctx.exception.code, "RESULT_INCOMPLETE")

    def test_verifier_detects_a_tampered_result(self) -> None:
        world = FinanceWorld()
        # Prepare the world exactly as the planner would.
        world.read_document("doc_fy2024")
        world.read_document("doc_fy2025")
        world.check_restatement("doc_fy2024")
        world.reconcile_basis("doc_fy2025_adjusted")
        world.normalize_units("doc_fy2024_restated")
        world.normalize_units("doc_fy2025")
        truth = world.calculate_margin()
        tampered = dict(truth)
        tampered["change_bp"] = truth["change_bp"] + 1
        with self.assertRaises(AgentError) as ctx:
            verify_observation(world, Observation(
                tool="calculate_margin", arguments={}, result=tampered,
            ))
        self.assertEqual(ctx.exception.code, "RESULT_VERIFICATION_FAILED")
        verified = verify_observation(world, Observation(
            tool="calculate_margin", arguments={}, result=truth,
        ))
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["verification"], "runtime-recomputed")

    def test_harness_without_a_verifier_refuses_to_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            world = FinanceWorld()
            harness = Harness(
                store=store,
                registry=ToolRegistry(build_finance_tools(world)),
                provider=MockProvider(),
                world=world,
                limits=BudgetLimits(12, 20, 20_000, 20_000),
                receipts=ReceiptStore(store),
            )
            with self.assertRaises(AgentError) as ctx:
                harness.verify(Observation(tool="calculate_margin", arguments={}, result={"computed": True}))
            self.assertEqual(ctx.exception.code, "RESULT_INCOMPLETE")
            store.close()

    def test_module_composes_workspace_tools_exactly_once(self) -> None:
        names = [tool.name for tool in FINANCE_DOMAIN.build_tools(FinanceWorld())]
        self.assertEqual(len(names), len(set(names)), "a tool must not appear twice")
        for workspace_name in (
            "ingest_file", "search_corpus", "read_slice",
            "working_paper_append", "search_cases", "read_case", "query_facts", "collect_source", "fetch_source",
        ):
            self.assertIn(workspace_name, names)


if __name__ == "__main__":
    unittest.main()
