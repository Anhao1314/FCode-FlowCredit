"""P0 regressions: argv side effects, atomic writes and actual usage settlement."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import ChatProviderConfig, ChatService  # noqa: E402
from flowagent.core.contracts import AgentError, PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.budget import Budget, BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.general.commands import CommandRunner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class _SandboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.sandbox = WorkspaceSandbox(self.root / "workspace")
        self.sandbox.ensure()
        self.runner = CommandRunner(self.sandbox)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CommandSideEffectRegressionTests(_SandboxCase):
    def test_find_delete_is_not_readonly_and_never_dispatches(self) -> None:
        self.sandbox.write("victim.txt", "keep")
        args = ["victim.txt", "-delete"]
        self.assertTrue(self.runner.policy.classify("find", args, self.sandbox))
        with self.assertRaises(AgentError) as caught:
            self.runner.run("find", args)
        self.assertEqual(caught.exception.code, "COMMAND_FLAG_REFUSED")
        self.assertTrue((self.sandbox.root / "victim.txt").exists())

    def test_git_diff_output_is_not_readonly_and_never_dispatches(self) -> None:
        args = ["diff", "--output=patch.txt"]
        self.assertTrue(self.runner.policy.classify("git", args, self.sandbox))
        with self.assertRaises(AgentError) as caught:
            self.runner.run("git", args)
        self.assertEqual(caught.exception.code, "COMMAND_GIT_FLAG_REFUSED")
        self.assertFalse((self.sandbox.root / "patch.txt").exists())

    def test_grep_file_pattern_is_jailed_but_workspace_pattern_file_works(self) -> None:
        outside = self.root / "outside-patterns.txt"
        outside.write_text("SECRET_PATTERN\n", encoding="utf-8")
        self.sandbox.write("needle.txt", "SECRET_PATTERN\n")
        with self.assertRaises(AgentError) as caught:
            self.runner.run("grep", ["-f", str(outside), "needle.txt"])
        self.assertEqual(caught.exception.code, "SANDBOX_PATH_DENIED")

        self.sandbox.write("patterns.txt", "SECRET_PATTERN\n")
        result = self.runner.run("grep", ["-f", "patterns.txt", "needle.txt"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("SECRET_PATTERN", result["output"])

    def test_wc_files0_from_is_fail_closed(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.runner.run("wc", ["--files0-from=list.txt"])
        self.assertEqual(caught.exception.code, "COMMAND_FLAG_REFUSED")

    def test_git_amend_is_refused(self) -> None:
        self.sandbox.write("history.txt", "one\n")
        with self.assertRaises(AgentError) as caught:
            self.runner.run("git", ["commit", "--amend", "-m", "rewritten"])
        self.assertEqual(caught.exception.code, "COMMAND_GIT_FLAG_REFUSED")


class SandboxAtomicWriteRegressionTests(_SandboxCase):
    def test_predicted_legacy_temporary_symlink_cannot_write_outside(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("ORIGINAL", encoding="utf-8")
        predicted = self.sandbox.root / f".note.txt.tmp-{os.getpid()}"
        predicted.symlink_to(outside)

        self.sandbox.write("note.txt", "SAFE")

        self.assertEqual(outside.read_text(encoding="utf-8"), "ORIGINAL")
        self.assertEqual(self.sandbox.read("note.txt")["content"], "SAFE")

    def test_ls_does_not_follow_file_symlinks(self) -> None:
        outside = self.root / "outside-secret.txt"
        outside.write_text("OUTSIDE_CONTENT", encoding="utf-8")
        (self.sandbox.root / "link.txt").symlink_to(outside)
        self.assertEqual(self.sandbox.ls(), [])


class ActualUsageProvider:
    name = "audit"
    model = "audit-model"

    def propose(self, context) -> Proposal:
        return Proposal(
            plan={
                "version": PLAN_VERSION,
                "steps": [{"id": "s1", "tool": "list_documents", "arguments": {}}],
            },
            provider=self.name,
            model=self.model,
            tokens=1_500,
            cost_microusd=750,
        )


class BudgetSettlementRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(pathlib.Path(self._tmp.name) / "tasks.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_settle_reconciles_actual_and_exposes_overage(self) -> None:
        budget = Budget(self.store, "scope", BudgetLimits(2, 0, 200, 1_000))
        budget.charge_call(tokens=100, cost_microusd=20)
        budget.settle_call(
            reserved_tokens=100,
            reserved_cost_microusd=20,
            actual_tokens=1_200,
            actual_cost_microusd=250,
        )
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["tokens_used"], 1_200)
        self.assertEqual(snapshot["cost_used_microusd"], 250)
        self.assertEqual(snapshot["tokens_over_limit"], 200)
        self.assertEqual(snapshot["cost_over_limit_microusd"], 50)
        self.assertEqual(snapshot["over_limit"], ["cost", "tokens"])

    def test_settle_adjusts_a_lower_actual_without_touching_call_count(self) -> None:
        budget = Budget(self.store, "scope", BudgetLimits(2, 0, 200, 1_000))
        budget.charge_call(tokens=500, cost_microusd=100)
        budget.settle_call(
            reserved_tokens=500,
            reserved_cost_microusd=100,
            actual_tokens=300,
            actual_cost_microusd=60,
        )
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["calls_used"], 1)
        self.assertEqual(snapshot["tokens_used"], 300)
        self.assertEqual(snapshot["cost_used_microusd"], 60)

    def test_harness_settles_provider_reported_usage(self) -> None:
        world = FinanceWorld()
        registry = ToolRegistry(build_finance_tools(world))
        provider = ActualUsageProvider()
        harness = Harness(
            store=self.store,
            registry=registry,
            provider=provider,
            world=world,
            limits=BudgetLimits(2, 2, 2_000, 2_000),
            receipts=ReceiptStore(self.store),
        )
        task = harness.create(
            task_id="usage-task",
            objective="inspect",
            principal="test",
            binding={},
            allowed_tools=["list_documents"],
            grant={"allowed_tools": ["list_documents"]},
        )
        task, proposal = harness.propose(task, {"round": 1, "state": {}})
        self.assertIsNotNone(proposal)
        snapshot = harness.budget(task).snapshot()
        self.assertEqual(snapshot["tokens_used"], 1_500)
        self.assertEqual(snapshot["cost_used_microusd"], 750)


class _StreamingUsageProvider:
    def stream(self, messages):
        yield {"delta": "done"}
        yield {
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            }
        }
        yield {"finish_reason": "stop"}


class ChatSettlementRegressionTests(unittest.TestCase):
    def test_chat_settles_actual_usage_and_blocks_the_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ChatService(pathlib.Path(temporary) / "chat.sqlite")
            try:
                config = ChatProviderConfig(
                    key="mock",
                    label="Audit",
                    model="audit",
                    max_completion_tokens=10,
                    input_price_microusd_per_mtok=1_000,
                    output_price_microusd_per_mtok=1_000,
                )
                environment = {
                    "FCODE_CHAT_MAX_COST_MICROUSD": "100",
                    "FCODE_CHAT_MAX_TOKENS": "3_000".replace("_", ""),
                    "FCODE_CHAT_MOCK_DELAY": "0",
                }
                with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                    service, "_provider", return_value=(config, _StreamingUsageProvider())
                ):
                    events = list(service.send(thread_id=None, text="budget probe", model="mock"))
                    done = next(event for event in events if event["type"] == "done")
                    self.assertEqual(done["budget"]["cost_used_microusd"], 2_000)
                    self.assertEqual(done["budget"]["tokens_used"], 2_000_000)
                    self.assertEqual(done["budget"]["over_limit"], ["cost", "tokens"])

                    second = list(service.send(
                        thread_id=done["thread_id"], text="must not dispatch", model="mock",
                    ))
                    self.assertEqual(second[0]["type"], "error")
                    self.assertEqual(second[0]["code"], "BUDGET_EXCEEDED")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
