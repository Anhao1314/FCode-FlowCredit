"""The capability runtime: stage loop, action gate, cached checks, typed result."""

from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
import unittest
from dataclasses import replace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.capabilities.artifacts import Action, ArtifactStore  # noqa: E402
from flowagent.capabilities.checks import (  # noqa: E402
    CheckContext, CheckRegistry, CheckResult,
)
from flowagent.capabilities.manifest import (  # noqa: E402
    BudgetSpec, CapabilityManifest, CheckSpec, StageSpec,
)
from flowagent.capabilities.result import CapabilityResult  # noqa: E402
from flowagent.capabilities.runtime import (  # noqa: E402
    STAGE_ACTION_DENIED, ActionGate, CapabilityRuntime,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402


class Scripted:
    """One action per round, then nothing (an exhausted stand-in proposer)."""

    def __init__(self, *actions: Any) -> None:
        self.actions = list(actions)
        self.packets: list[dict[str, Any]] = []

    def propose(self, packet: dict[str, Any], *, tools: tuple[str, ...]) -> Any:
        self.packets.append(packet)
        return self.actions.pop(0) if self.actions else None


def counting_check(counter: dict[str, int]):
    def check(context: CheckContext, registry: CheckRegistry) -> CheckResult:
        counter["n"] += 1
        ok = "DONE" in context.artifact.content
        return CheckResult(
            name="always.pass", passed=ok, status="pass" if ok else "fail",
            score=100 if ok else 0,
            issues=() if ok else ("artifact does not contain DONE",),
            required_fixes=() if ok else ({"code": "NOT_DONE", "message": "add DONE"},),
            artifact_sha256=context.artifact.sha256,
        )
    return check


def registry_for(counter: dict[str, int]) -> CheckRegistry:
    return CheckRegistry().register("always.pass", counting_check(counter))


def one_stage_manifest(calls: int = 4) -> CapabilityManifest:
    return CapabilityManifest(
        kind="test.runtime", when="unit tests", input_schema={}, output_schema={},
        stages=(StageSpec("only", ("edit_file",), ("always.pass",),
                          BudgetSpec(calls=calls, tokens=1_000)),),
        checks=(CheckSpec("always.pass"),),
        artifact_path="index.html", result_path="RESULT.md",
    )


class Stack:
    def __init__(self, tmp: str, content: str = "ONE\n") -> None:
        self.sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
        self.sandbox.write("index.html", content)
        self.store = ArtifactStore(self.sandbox, path="index.html", result_path="RESULT.md")

    def runtime(self, proposer, manifest=None, counter=None) -> CapabilityRuntime:
        counter = counter or {"n": 0}
        return CapabilityRuntime(
            manifest=manifest or one_stage_manifest(),
            store=self.store, checks=registry_for(counter),
            proposer=proposer, gate=ActionGate(), child_run_id="cap-test-0001",
        )


class RuntimeLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = Stack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success_is_a_typed_result_with_integer_budget(self) -> None:
        proposer = Scripted(Action("edit_file", {"old_text": "ONE", "new_text": "DONE"},
                                   tokens=100, cost_microusd=7))
        result = self.stack.runtime(proposer).run("make it done")
        self.assertTrue(result.succeeded)
        payload = result.to_dict()
        self.assertEqual(set(payload), {
            "kind", "status", "stage", "artifact", "quality", "required_fixes",
            "budget", "reason", "child_run_id",
        })
        self.assertEqual(payload["kind"], "test.runtime")
        self.assertEqual(payload["stage"], "only")
        self.assertEqual(payload["artifact"]["sha256"], self.stack.store.snapshot().sha256)
        self.assertEqual(payload["budget"], {
            "calls_used": 1,
            "model_calls": 1,
            "attempts": 1,
            "valid_actions": 1,
            "repairs": 0,
            "tokens_used": 100,
            "cost_microusd": 7,
        })
        self.assertEqual(payload["reason"], "")
        self.assertEqual(payload["child_run_id"], "cap-test-0001")
        self.assertNotIn("journal", payload)
        self.assertTrue(any(item["event"] == "action_applied" for item in result.journal))
        # The packed state carries the stage's allowed tools and remaining budget.
        packet = proposer.packets[0]
        self.assertEqual(packet["tools"], ["edit_file"])
        self.assertEqual(packet["remaining_budget"]["calls"], 4)
        self.assertFalse(packet["artifact"]["scene_nonempty"])

    def test_empty_action_is_denied_without_touching_the_artifact(self) -> None:
        before = self.stack.store.snapshot()
        runtime = self.stack.runtime(Scripted(Action("", {})))
        result = runtime.run("empty")
        denied = [item for item in result.journal if item["event"] == "action_denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["code"], STAGE_ACTION_DENIED)
        self.assertEqual(self.stack.store.snapshot().sha256, before.sha256)
        self.assertEqual(result.status, "failed")

    def test_out_of_stage_tool_is_denied(self) -> None:
        before = self.stack.store.snapshot()
        runtime = self.stack.runtime(Scripted(
            Action("write_file", {"path": "RESULT.md", "content": "x"}),
        ))
        result = runtime.run("wrong tool")
        denied = [item for item in result.journal if item["event"] == "action_denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["code"], STAGE_ACTION_DENIED)
        self.assertIn("not allowed in stage", denied[0]["message"])
        self.assertEqual(self.stack.store.snapshot().sha256, before.sha256)
        self.assertFalse(self.stack.store.result_exists())

    def test_budget_exhaustion_returns_a_typed_failure(self) -> None:
        manifest = one_stage_manifest(calls=1)
        runtime = self.stack.runtime(
            Scripted(Action("edit_file", {"old_text": "ONE", "new_text": "TWO"})),
            manifest=manifest,
        )
        result = runtime.run("never passes")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.stage, "only")
        self.assertEqual(result.reason, "ARTIFACT_CHECK_FAILED")
        self.assertEqual(result.to_dict()["budget"]["calls_used"], 1)
        self.assertTrue(result.required_fixes)

    def test_stage_token_budget_is_a_real_stop_condition(self) -> None:
        runtime = self.stack.runtime(Scripted(
            Action("edit_file", {"old_text": "ONE", "new_text": "TWO"}, tokens=1_000),
        ))
        result = runtime.run("token bound")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.to_dict()["budget"]["calls_used"], 1)
        self.assertEqual(result.to_dict()["budget"]["tokens_used"], 1_000)

    def test_stage_cost_budget_is_a_real_stop_condition(self) -> None:
        manifest = one_stage_manifest(calls=10)
        stage = manifest.stages[0]
        budget = replace(stage.budget, tokens=0, cost_microusd=50)
        manifest = replace(manifest, stages=(replace(stage, budget=budget),))
        runtime = self.stack.runtime(Scripted(
            Action("edit_file", {"old_text": "ONE", "new_text": "TWO"}, cost_microusd=50),
        ), manifest=manifest)
        result = runtime.run("cost bound")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.to_dict()["budget"]["calls_used"], 1)
        self.assertEqual(result.to_dict()["budget"]["cost_microusd"], 50)

    def test_two_rounds_without_artifact_delta_stop_the_runtime(self) -> None:
        manifest = one_stage_manifest(calls=10)
        no_op = Action("edit_file", {"old_text": "ONE", "new_text": "ONE"})
        runtime = self.stack.runtime(Scripted(no_op, no_op, no_op), manifest=manifest)
        result = runtime.run("loop")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.to_dict()["budget"]["calls_used"], 2)
        self.assertEqual(result.reason, "ARTIFACT_CHECK_FAILED")

    def test_failed_result_reports_the_stage_without_a_schema_error(self) -> None:
        runtime = self.stack.runtime(Scripted())
        result = runtime.run("no actions")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "NO_TOOL_CALL")
        for key in ("kind", "status", "stage", "artifact", "quality", "budget", "reason"):
            self.assertIn(key, result.to_dict())


class CheckCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = Stack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_artifact_derived_check_is_cached_by_hash(self) -> None:
        counter = {"n": 0}
        registry = registry_for(counter)
        revision = self.stack.store.snapshot()
        context = CheckContext(
            store=self.stack.store, artifact=revision,
            manifest=one_stage_manifest(), stage=one_stage_manifest().stages[0],
        )
        first = registry.run(("always.pass",), context)
        second = registry.run(("always.pass",), context)
        self.assertEqual(counter["n"], 1, "the same hash must not recompute the check")
        self.assertFalse(first.results[0].cached)
        self.assertTrue(second.results[0].cached)
        self.assertEqual(first.status, second.status)

    def test_a_changed_artifact_invalidates_the_cache(self) -> None:
        counter = {"n": 0}
        registry = registry_for(counter)
        manifest = one_stage_manifest()
        first_ctx = CheckContext(
            store=self.stack.store, artifact=self.stack.store.snapshot(),
            manifest=manifest, stage=manifest.stages[0],
        )
        registry.run(("always.pass",), first_ctx)
        self.stack.store.apply(Action("edit_file", {"old_text": "ONE", "new_text": "DONE"}))
        second_ctx = CheckContext(
            store=self.stack.store, artifact=self.stack.store.snapshot(),
            manifest=manifest, stage=manifest.stages[0],
        )
        after = registry.run(("always.pass",), second_ctx)
        self.assertEqual(counter["n"], 2)
        self.assertTrue(after.passed)


class StructuralIndependenceTests(unittest.TestCase):
    """The capability layer must not reach into the conversation kernel."""

    FORBIDDEN_MODULES = {
        "flowagent.core.plan", "flowagent.runtime.loop",
        "flowagent.tools.general.planner", "flowagent.runtime.plan_extension",
    }
    FORBIDDEN_TOKENS = ("Planner", "submit_plan", "_settle", "Plan(", "todo", "advance(")

    def test_capability_modules_never_import_a_plan_or_settlement_layer(self) -> None:
        package = ROOT / "src" / "flowagent" / "capabilities"
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module in self.FORBIDDEN_MODULES or module.endswith("core.plan"):
                        offenders.append(f"{path.name}: {module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in self.FORBIDDEN_MODULES:
                            offenders.append(f"{path.name}: {alias.name}")
        self.assertEqual(offenders, [])

    def test_runtime_source_has_no_checklist_semantics(self) -> None:
        package = ROOT / "src" / "flowagent" / "capabilities"
        for path in sorted(package.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for token in self.FORBIDDEN_TOKENS:
                    self.assertNotIn(token, text, f"{path.name} contains {token!r}")

    def test_second_slice_uses_a_work_adapter_not_a_general_tool(self) -> None:
        """Console may route Work, but the model still gets no nested-run tool."""
        from flowagent.tools.general.tools import GENERAL_TOOLS  # noqa: PLC0415

        self.assertNotIn("run_capability", GENERAL_TOOLS)
        self.assertNotIn("motion.render", GENERAL_TOOLS)
        driver = ROOT / "viewer" / "console_run_driver.py"
        adapter = ROOT / "src" / "flowagent" / "runtime" / "capability.py"
        self.assertIn("flowagent.capabilities", driver.read_text(encoding="utf-8"))
        adapter_text = adapter.read_text(encoding="utf-8")
        self.assertIn("self.harness.execute", adapter_text)
        self.assertIn("self.harness.propose", adapter_text)


class TypedResultContractTests(unittest.TestCase):
    def test_typed_result_is_canonical_json_safe(self) -> None:
        from flowagent.core.hashing import canonical_json  # noqa: PLC0415

        result = CapabilityResult.succeeded_result(
            kind="motion.render", stage="delivery",
            artifact={"path": "index.html", "sha256": "abc", "bytes": 12},
            quality={"status": "pass", "score": 100,
                     "coverage": {"complete": True, "covered": [], "required": []}},
            budget={"calls_used": 7, "tokens_used": 180_000, "cost_microusd": 50_000},
            child_run_id="cap-test", journal=(),
        )
        encoded = canonical_json(result.to_dict())
        self.assertIsInstance(encoded, str)
        self.assertIn('"cost_microusd":50000', encoded)


if __name__ == "__main__":
    unittest.main()
