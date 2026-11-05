"""Context management: tool summarize contracts, domain state views, and the
budgeted observation window (compaction-as-event)."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.memory.context import WorkingMemory, default_summarize  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.summaries import (  # noqa: E402
    FINANCE_SUMMARIZERS,
    summarize_read_document,
    summarize_state as finance_state,
)
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.domains.general import summarize_state as general_state  # noqa: E402
from flowagent.tools.general.summaries import (  # noqa: E402
    summarize_fetch_url,
    summarize_read_file,
    summarize_run_command,
    summarize_write_file,
)
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def observation(tool: str, result: dict, arguments: dict | None = None) -> Observation:
    return Observation(
        tool=tool,
        arguments=arguments or {},
        result=result,
        result_hash="r" * 64,
    )


class SummarizeContractTests(unittest.TestCase):
    """The memory layer must not know tool names: specs carry their own
    compressors and the registry exposes them."""

    def test_finance_specs_carry_summarizers(self) -> None:
        registry = ToolRegistry(build_finance_tools(FinanceWorld()))
        found = registry.summarizers()
        for name in FINANCE_SUMMARIZERS:
            self.assertIn(name, found, name)

    def test_general_specs_carry_summarizers(self) -> None:
        from flowagent.tools.general.world import GeneralWorld

        world = GeneralWorld({"mode": "script"})
        registry = ToolRegistry(world.build_tools())
        found = registry.summarizers()
        for name in (
            "ls_workspace", "read_file", "write_file", "edit_file",
            "run_command", "fetch_url",
        ):
            self.assertIn(name, found, name)

    def test_read_file_summary_never_carries_a_file_body(self) -> None:
        summary = summarize_read_file({
            "path": "results.md",
            "content": "x" * 50_000,
            "sha256": "a" * 64,
            "bytes": 50_000,
        })
        joined = str(summary)
        self.assertLess(len(joined), 900)
        self.assertEqual(summary["bytes"], 50_000)
        self.assertEqual(summary["sha256"], "a" * 64)
        self.assertIn("head", summary)
        self.assertEqual(summary.get("chars"), 50_000)
        # bounded head, not the body: far below the raw size
        self.assertLess(len(summary["head"]), 700)

    def test_run_command_keeps_verdict_not_log(self) -> None:
        summary = summarize_run_command({
            "command": "python fib.py",
            "exit_code": 0,
            "duration_ms": 320,
            "output": "line\n" * 400,
            "readonly": True,
        })
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["command"], "python fib.py")
        self.assertIn("head", summary)
        self.assertNotIn("line\nline\nline\nline\nline", str(summary))

    def test_fetch_url_marks_content_truth(self) -> None:
        summary = summarize_fetch_url({
            "url": "https://example.com",
            "status": 200,
            "title": "Example",
            "bytes": 1_256,
            "content_truth": "not-verified",
            "text": "body" * 2_000,
            "latency_ms": 90,
        })
        self.assertEqual(summary["content_truth"], "not-verified")
        self.assertNotIn("body", str(summary))

    def test_default_summarize_is_shape_based_and_bounded(self) -> None:
        summary = default_summarize({
            "flag": True,
            "count": 3,
            "name": "thing",
            "blob": "y" * 900,
            "items": ["a", "b", "c"],
            "nested": {"x": 1, "y": 2},
        })
        self.assertEqual(summary["flag"], True)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["items"], {"count": 3, "first": "a"})
        self.assertEqual(summary["nested"], {"keys": ["x", "y"]})
        self.assertEqual(summary["blob"]["chars"], 900)
        self.assertLess(len(str(summary["blob"])), 400)

    def test_finance_summary_keysets_are_the_v10_contract(self) -> None:
        # read_document reference shape
        self.assertEqual(
            sorted(summarize_read_document({
                "id": "doc_fy2024", "period": "FY2024", "unit": "CNY",
                "basis": "IFRS", "revenue": 100, "gross_profit": 10,
            })),
            ["basis", "gross_profit", "id", "period", "revenue", "unit"],
        )


class DomainStateTests(unittest.TestCase):
    def test_general_state_card_projects_files_commands_fetches(self) -> None:
        state = general_state([
            observation("write_file", {"path": "fib.py", "bytes": 120, "changed": True, "sha256_after": "f" * 64, "commit": "c1"}),
            observation("edit_file", {"path": "fib.py", "changed": True, "lines_added": 2, "lines_removed": 1}),
            observation("read_file", {"path": "results.md"}),
            observation("run_command", {"command": "python fib.py", "exit_code": 0}),
            observation("run_command", {"command": "git status", "exit_code": 1}),
            observation("fetch_url", {"url": "https://example.com", "status": 200}),
        ])
        self.assertEqual(state["files_written"][0]["path"], "fib.py")
        self.assertEqual(state["files_edited"], ["fib.py"])
        self.assertEqual(state["files_read"], ["results.md"])
        self.assertEqual(state["commands"][1]["exit_code"], 1)
        self.assertEqual(state["fetches"][0]["status"], 200)
        self.assertTrue(any("git status" in b for b in state["blockers"]))

    def test_general_state_counts_a_failed_run_once_as_blocker(self) -> None:
        state = general_state([
            observation("run_command", {"command": "pytest", "exit_code": 1}),
            observation("run_command", {"command": "pytest", "exit_code": 1}),
        ])
        self.assertEqual(len(state["blockers"]), 2)

    def test_neutral_fallback_without_a_domain_state_fn(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[])
        memory.record(1, observation("anything", {"x": 1}))
        state = memory.state()
        self.assertEqual(state["rounds_recorded"], 1)
        self.assertEqual(state["tools_used"], ["anything"])

    def test_finance_state_still_pinned_by_its_own_module(self) -> None:
        state = finance_state([
            observation("list_documents", {"documents": [{"id": "doc_fy2024"}]}),
            observation("read_document", {"id": "doc_fy2024", "found": True}),
            observation("calculate_margin", {
                "computed": True,
                "blockers": [],
                "margin_bp_2024": 1000,
                "margin_bp_2025": 750,
                "change_bp": -250,
            }),
        ])
        self.assertTrue(state["computed"])
        self.assertEqual(state["margins"]["change_bp"], -250)


class BudgetedWindowTests(unittest.TestCase):
    def memory(self, budget: int) -> WorkingMemory:
        return WorkingMemory(
            objective="t",
            tool_catalog=[],
            summarizers={"generic": default_summarize},
            observation_budget=budget,
        )

    def record_many(self, memory: WorkingMemory, count: int, size: int = 400) -> None:
        for index in range(1, count + 1):
            memory.record(
                index,
                observation("generic", {"blob": "z" * size, "n": index}, {"k": index}),
            )

    def test_under_budget_keeps_everything_inline(self) -> None:
        memory = self.memory(budget=100_000)
        self.record_many(memory, 5)
        context = memory.build(6, {})
        self.assertEqual(len(context["observations"]), 5)
        self.assertNotIn("observation_index", context)

    def test_over_budget_folds_oldest_into_index(self) -> None:
        memory = self.memory(budget=1_500)
        self.record_many(memory, 10)
        context = memory.build(11, {})
        self.assertIn("observation_index", context)
        inline = context["observations"]
        index = context["observation_index"]
        self.assertGreater(len(index), 0)
        self.assertGreater(len(inline), 0)
        # newest rounds stay inline, oldest are folded, nothing overlaps
        inline_rounds = [o["round"] for o in inline]
        index_rounds = [row["round"] for row in index]
        self.assertEqual(inline_rounds[-1], 10)
        self.assertEqual(index_rounds[0], 1)
        self.assertFalse(set(inline_rounds) & set(index_rounds))
        # the fold must actually bring the payload under budget
        from flowagent.core.hashing import canonical_json

        total = sum(len(canonical_json(o)) for o in inline)
        self.assertLessEqual(total, 1_500)
        # index rows are one-liners with a result-hash pointer
        row = index[0]
        self.assertEqual(row["tool"], "generic")
        self.assertEqual(row["result_hash"], "r" * 12)
        self.assertIn("k=1", row["arguments_head"])

    def test_fold_raises_one_pending_compaction_marker(self) -> None:
        memory = self.memory(budget=1_500)
        self.record_many(memory, 10)
        memory.build(11, {})
        marker = memory.pending_compaction
        self.assertIsNotNone(marker)
        self.assertEqual(marker["from_round"], 1)
        self.assertGreaterEqual(marker["through_round"], 1)
        # same window again: no second marker until the fold deepens
        memory.pending_compaction = None
        memory.build(12, {})
        self.assertIsNone(memory.pending_compaction)

    def test_store_keeps_every_record_even_when_prompt_folds(self) -> None:
        memory = self.memory(budget=1_500)
        self.record_many(memory, 10)
        context = memory.build(11, {})
        # prompt-level forgetting only: the memory still holds all records
        self.assertEqual(len(memory.records), 10)
        self.assertEqual(len(context["observations"]) + len(context["observation_index"]), 10)


class EventWiringTests(unittest.TestCase):
    """The loop turns a fresh fold into a context_compacted chain event."""

    def test_compaction_event_lands_in_the_chain(self) -> None:
        import tempfile

        from flowagent.core.receipts import ReceiptStore
        from flowagent.core.store import TaskStore
        from flowagent.memory.context import WorkingMemory
        from flowagent.providers.mock import MockProvider
        from flowagent.runtime.harness import Harness
        from flowagent.tools.base import EFFECT_READ, Param, ToolSpec
        from flowagent.tools.registry import ToolRegistry

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")

            def handler(_: dict) -> dict:
                return {"ok": True, "blob": "z" * 400}

            spec = ToolSpec(
                name="generic",
                summary="test tool",
                effect=EFFECT_READ,
                params=(Param(name="k", kind="integer", required=False),),
                handler=handler,
                outputs=(),
                summarize=default_summarize,
            )
            harness = Harness(
                store=store,
                registry=ToolRegistry([spec]),
                provider=MockProvider(),
                world=None,
                limits=BudgetLimits(60, 40, 300_000, 200_000),
                receipts=ReceiptStore(store),
                verifier=lambda obs: {"verified": True, "verification": "test", "result": obs.result},
                state_fn=general_state,
            )
            task = harness.create(
                task_id="compact-test",
                objective="fold",
                principal="test",
                binding={"version": "fcode.intake/v1"},
                allowed_tools=["generic"],
                grant={},
            )
            # record ten rounds, then build with a tight budget so the fold
            # happens and the loop's event emission path can be exercised
            for index in range(1, 11):
                task, observation = harness.execute(
                    task, {"tool": "generic", "arguments": {"k": index}}, round_no=index
                )
            memory = WorkingMemory(
                objective=task.objective,
                tool_catalog=harness.registry.catalog(),
                summarizers=harness.registry.summarizers(),
                state_fn=harness.state_fn,
                observation_budget=1_000,
            )
            # replay from the store exactly like Loop.restore
            for event in store.events(task.id):
                if event["type"] != "tool_succeeded":
                    continue
                detail = event["payload"].get("detail", {})
                memory.record(
                    int(detail.get("round", 0)),
                    Observation(
                        tool=detail["tool"],
                        arguments=detail.get("arguments", {}),
                        result=detail["result"],
                        result_hash=detail["result_hash"],
                    ),
                )
            context = memory.build(11, {})
            self.assertIn("observation_index", context)
            self.assertIsNotNone(memory.pending_compaction)
            task = harness.record(task, "context_compacted", dict(memory.pending_compaction))
            memory.pending_compaction = None
            events = [e["type"] for e in store.events(task.id)]
            self.assertIn("context_compacted", events)
            detail = next(
                e["payload"]["detail"]
                for e in store.events(task.id)
                if e["type"] == "context_compacted"
            )
            self.assertEqual(detail["from_round"], 1)
            self.assertGreater(detail["folded_count"], 0)
            # the chain still verifies with the compaction event in it
            store.verify_chain(task.id)


if __name__ == "__main__":
    unittest.main()
