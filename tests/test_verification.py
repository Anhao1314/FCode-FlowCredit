"""Declared result obligations: the runtime re-runs the check that decides success.

Before this, a general delegation's success condition was the artifact's file
state. A run whose test failed (exit code 1, honestly recorded) and whose summary
claimed the work was done still ended ``succeeded``. The log was truthful and the
result was wrong.

These tests pin the closure, and they are deliberately written so that editing a
status string cannot make them pass: every assertion is about a terminal state
paired with evidence the runtime itself produced.

The decisive one is ``test_e1_negative_claimed_success_does_not_override_a_failing_test``.
A ``succeeded`` there is a test failure.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION, AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.verification import (  # noqa: E402
    VERIFICATION_SOURCE_SPEC,
    build_obligation,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402

BUGGY = '''"""Payroll statistics helper."""


def mean(values):
    # BUG: floor division truncates
    return sum(values) // len(values)
'''
TEST = '''import unittest

from stats import mean


class MeanTests(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2]), 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=1)
'''
README = "# payroll-stats\n\n`stats.py` holds the helpers. Run `python3 test_stats.py`.\n"

#: The operator's declaration: the check that must pass for this task to count.
DECLARATION = {"name": "the payroll statistics unit tests",
               "command": "python3", "args": ["test_stats.py"], "expectExitCode": 0}


def make_workspace(root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "stats.py").write_text(BUGGY, encoding="utf-8")
    (root / "test_stats.py").write_text(TEST, encoding="utf-8")
    (root / "README.md").write_text(README, encoding="utf-8")


def step(tool: str, arguments: dict, todo_id: str = "a1") -> dict:
    return {"id": todo_id, "tool": tool, "arguments": arguments}


def add(tid: str, tool: str, arguments: dict | None = None) -> dict:
    return {"id": tid, "text": f"step {tid}", "tool": tool,
            "done_when": f"observed:{tid}", "arguments": arguments or {}}


class PlannedProvider:
    name = "planned"
    model = "planned-v1"

    def __init__(self, rounds: list[tuple]) -> None:
        self.rounds = list(rounds)
        self.contexts: list[dict] = []

    def propose(self, context: dict) -> Proposal:
        self.contexts.append(context)
        if not self.rounds:
            return Proposal(
                plan={"version": PLAN_VERSION, "steps": [
                    step("report_blocked", {"reason": "the scripted provider ran out of rounds"}, "stop"),
                ]},
                provider=self.name, model=self.model, tokens=5, cost_microusd=5, latency_ms=1,
            )
        current = self.rounds.pop(0)
        action, additions = current[0], current[1]
        evidence = current[2] if len(current) > 2 else None
        plan: dict = {"version": PLAN_VERSION, "steps": [action]}
        if additions:
            plan["additions"] = additions
        if evidence is not None:
            plan["evidence"] = evidence
        return Proposal(plan=plan, provider=self.name, model=self.model,
                        tokens=20, cost_microusd=20, latency_ms=1, todo_id=action.get("id"))


class UnknownRunner:
    """Every command it is asked to run ends with an unknown outcome.

    The fixture scripts a task that runs no command of its own, so the *first*
    command dispatched is the runtime's own verification run. It carries a
    ``sandbox`` because it stands in for a real ``CommandRunner``, which the
    runtime redirects at the snapshot before dispatching.
    """

    def __init__(self, sandbox: Any = None) -> None:
        self.sandbox = sandbox
        self.calls = 0

    def run(self, command, args, timeout_seconds=None):
        self.calls += 1
        raise AgentError("COMMAND_TIMEOUT", "daemon did not answer in time",
                         details={"outcome": "unknown"})


class Stack:
    def __init__(self, tmp: str, spec: dict, *, command_runner: Any = None,
                 limits: BudgetLimits | None = None) -> None:
        self.tmp = pathlib.Path(tmp)
        self.store = TaskStore(self.tmp / "task.sqlite")
        self.ws = self.tmp / "ws"
        make_workspace(self.ws)
        self.world = GENERAL_DOMAIN.create_world(
            spec=spec, sandbox=WorkspaceSandbox(self.ws), command_runner=command_runner,
        )
        self.registry = ToolRegistry(GENERAL_DOMAIN.build_tools(self.world))
        self.limits = limits or BudgetLimits(24, 48, 400_000, 400_000)
        self.receipts = ReceiptStore(self.store)

    def harness(self, provider, *, registry=None, verification: Any = "__from_world__") -> Harness:
        if verification == "__from_world__":
            verification = GENERAL_DOMAIN.make_verification(self.world)
        return Harness(
            store=self.store,
            registry=registry or self.registry,
            provider=provider,
            world=self.world,
            limits=self.limits,
            receipts=self.receipts,
            verifier=lambda observation: GENERAL_DOMAIN.verify(self.world, observation),
            state_fn=GENERAL_DOMAIN.summarize_state,
            instruction=GENERAL_DOMAIN.instruction,
            prompt_profile=GENERAL_DOMAIN.prompt_profile,
            plan_extension=GENERAL_DOMAIN.plan_extension,
            verification=verification,
        )

    def run(self, rounds, *, max_rounds: int = 12, registry=None, verification="__from_world__",
            task_id: str = "task-verify"):
        provider = PlannedProvider(rounds)
        harness = self.harness(provider, registry=registry, verification=verification)
        scenario = get_scenario("general-research")
        task = harness.create(
            task_id=task_id,
            objective="修好 stats.py 里失败的测试，并写一份 SUMMARY.md",
            principal="test",
            binding={"scenario": scenario.key},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        recorder = TraceRecorder(harness.store, provider_name=provider.name, model_name=provider.model)
        final = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(self.world),
            max_rounds=max_rounds,
            observer=recorder,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        return provider, harness, recorder, final

    def tests_pass(self) -> bool:
        done = subprocess.run([sys.executable, "test_stats.py"], cwd=self.ws,
                              capture_output=True, text=True, timeout=60)
        return done.returncode == 0

    def events(self, harness, task) -> list[dict]:
        return harness.store.events(task.id)

    def close(self) -> None:
        self.store.close()


def spec_with(declaration: dict | None = None, **extra) -> dict:
    spec = {"mode": "open", "reportPath": "SUMMARY.md"}
    if declaration is not None:
        spec["verification"] = declaration
    spec.update(extra)
    return spec


# --------------------------------------------------------------------- shapes

class DeclarationTests(unittest.TestCase):
    """A declaration is host input, and a bad one is refused at creation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_valid_declaration_becomes_a_bounded_obligation(self) -> None:
        obligation = build_obligation(DECLARATION)
        self.assertEqual(obligation.tool, "run_command")
        self.assertEqual(obligation.arguments, {"command": "python3", "args": ["test_stats.py"]})
        self.assertEqual(obligation.expect, {"exit_code": 0})
        self.assertEqual(obligation.source, VERIFICATION_SOURCE_SPEC)
        self.assertEqual(obligation.step(),
                         {"tool": "run_command", "arguments": {"command": "python3",
                                                               "args": ["test_stats.py"]}})

    def test_the_declaration_is_the_only_input_to_the_step(self) -> None:
        """Nothing about the dispatched action can come from a proposal."""
        obligation = build_obligation(DECLARATION)
        for mutation in (0, 1, 99):
            with self.subTest(mutation=mutation):
                self.assertEqual(obligation.step()["arguments"], obligation.arguments)

    def test_no_declaration_means_no_obligation(self) -> None:
        self.assertIsNone(build_obligation(None))

    def test_malformed_declarations_are_refused_at_creation(self) -> None:
        for bad in (
            {"command": "/bin/sh"},                      # a path, not a bare name
            {"command": "python3", "args": [1]},         # args must be strings
            {"command": "python3", "expectExitCode": "0"},
            {"command": "python3", "timeoutSeconds": 600},
            {"command": "python3", "surprise": 1},
            {"args": ["x"]},                             # no command
            5, "text",
        ):
            with self.subTest(declaration=bad):
                with self.assertRaises(AgentError) as ctx:
                    build_obligation(bad)
                self.assertEqual(ctx.exception.code, "VERIFICATION_DECLARATION_INVALID")

    def test_a_declared_obligation_must_not_break_world_creation(self) -> None:
        with self.assertRaises(AgentError):
            GENERAL_DOMAIN.create_world(spec=spec_with("/bin/sh"),
                                        sandbox=WorkspaceSandbox(self.tmp / "ws"))


# ------------------------------------------------------------ decisive tests

class OutputContractTests(unittest.TestCase):
    """Nothing about this may be satisfied by file state alone."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_command_recorded_during_execution_is_never_a_verification(self) -> None:
        """The A/B distinction: only the runtime's own run counts."""
        from flowagent.tools.base import Observation

        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        observation = Observation(
            tool="run_command",
            arguments={"command": "python3", "args": ["test_stats.py"]},
            result={"command": "python3", "exit_code": 0, "output": "OK", "computed": True,
                    "bytes": 2, "truncated": False, "duration_ms": 5, "readonly": False},
            result_hash="deadbeef",
        )
        # No verification window is open, so this recorded run cannot pass.
        with self.assertRaises(AgentError) as ctx:
            stack.world.verify_observation(observation)
        self.assertEqual(ctx.exception.code, "RESULT_INCOMPLETE")
        self.assertIn("not a verification result", ctx.exception.message)
        stack.close()

    def test_the_file_state_verifier_is_untouched(self) -> None:
        from flowagent.tools.base import Observation

        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        record = stack.world.sandbox.write("notes.txt", "hello\n")
        verified = stack.world.verify_observation(Observation(
            tool="write_file", arguments={"path": "notes.txt"},
            result={**record, "computed": True},
            result_hash="abc",
        ))
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["verification"], "file-state-recomputed")
        self.assertEqual(verified["result"]["content_truth"], "runtime-verified-file-state")
        # Unrelated tools are still refused.
        with self.assertRaises(AgentError):
            stack.world.verify_observation(Observation(
                tool="read_file", arguments={"path": "notes.txt"},
                result=record, result_hash="abc",
            ))
        stack.close()


class PositiveTests(unittest.TestCase):
    """E1: the bug is fixed, and the runtime's own re-run is what says so."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rounds(self) -> list[tuple[dict, list[dict]]]:
        return [
            (step("ls_workspace", {}), [
                add("p_list", "ls_workspace"),
                add("p_read", "read_file", {"path": "stats.py"}),
                add("p_run", "run_command", {"command": "python3", "args": ["test_stats.py"]}),
            ]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("run_command", {"command": "python3", "args": ["test_stats.py"]}), [
                add("p_fix", "edit_file", {
                    "path": "stats.py",
                    "old_text": "    return sum(values) // len(values)",
                    "new_text": "    return sum(values) / len(values)",
                }),
            ]),
            (step("edit_file", {
                "path": "stats.py",
                "old_text": "    return sum(values) // len(values)",
                "new_text": "    return sum(values) / len(values)",
            }), []),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Fix report\n\n`mean()` used floor division; the declared "
                                            "check passes.\n"}), []),
        ]

    def test_e1_positive_succeeds_only_after_the_declared_check_runs(self) -> None:
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        self.assertFalse(stack.tests_pass(), "the fixture must start broken")
        provider, harness, recorder, final = stack.run(self._rounds(), max_rounds=12)
        events = stack.events(harness, final)

        self.assertEqual(final.status, "succeeded", final.failure)
        # The runtime dispatched the declared command itself, inside the window.
        obligations = [e for e in events if e["type"] == "verification_obligation"]
        self.assertEqual(len(obligations), 1)
        detail = obligations[0]["payload"]["detail"]
        self.assertEqual(detail["source"], VERIFICATION_SOURCE_SPEC)
        self.assertEqual(detail["arguments"], {"command": "python3", "args": ["test_stats.py"]})
        self.assertTrue(detail["snapshot"]["sha256"])
        self.assertIs(detail["snapshot"]["artifact"]["matched"], True)
        # ...and the judgement is on the terminal result, with honest labels.
        self.assertEqual(final.result["result_truth"], "runtime-recomputed-command-result")
        self.assertEqual(final.result["semantic_truth"], "not-verified")
        self.assertEqual(final.result["file_state"], "file-state-recomputed")
        self.assertEqual(len(final.result["obligations"]), 1)
        self.assertEqual(final.result["obligations"][0]["exit_code"], 0)
        self.assertEqual(final.result["obligations"][0]["expect_exit_code"], 0)
        results = [e for e in events if e["type"] == "verification_result"]
        self.assertEqual([r["payload"]["detail"]["verified"] for r in results], [True])
        verify_trace(recorder.build(harness.store.load(final.id)))
        # Independent inspection of the workspace, not the model's word.
        self.assertTrue(stack.tests_pass(), "the test still fails on disk")
        stack.close()

    def test_the_task_run_and_the_verification_run_are_distinguishable(self) -> None:
        """The task's own failing run stays on the chain as evidence only."""
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        provider, harness, recorder, final = stack.run(self._rounds(), max_rounds=12)
        events = stack.events(harness, final)
        runs = [e for e in events if e["type"] == "tool_succeeded"
                and e["payload"]["detail"]["tool"] == "run_command"]
        exits = [run["payload"]["detail"]["result"]["exit_code"] for run in runs]
        # The task's own run (1) and the runtime's verification run (0), in order.
        self.assertEqual(exits, [1, 0])
        # The verification run is the one inside the obligation window.
        obligation_seq = [e["sequence"] for e in events if e["type"] == "verification_obligation"][0]
        self.assertGreater(runs[-1]["sequence"], obligation_seq)
        self.assertLess(runs[0]["sequence"], obligation_seq)
        stack.close()


class NegativeTests(unittest.TestCase):
    """E1': a summary claiming success must not override a failing check."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rounds(self) -> list[tuple[dict, list[dict]]]:
        return [
            (step("ls_workspace", {}), [
                add("p_list", "ls_workspace"),
                add("p_read", "read_file", {"path": "stats.py"}),
            ]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Fix report\n\nThe bug is fixed and "
                                            "`python3 test_stats.py` passes with exit code 0.\n"}), []),
        ]

    def test_e1_negative_claimed_success_does_not_override_a_failing_test(self) -> None:
        """DECISIVE. A `succeeded` here is a test failure."""
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        provider, harness, recorder, final = stack.run(self._rounds(), max_rounds=8)
        events = stack.events(harness, final)

        self.assertEqual(final.status, "failed", "a false success claim was accepted")
        self.assertEqual(final.failure["code"], "RESULT_VERIFICATION_FAILED")
        self.assertIsNone(final.result)
        kinds = [e["type"] for e in events]
        self.assertNotIn("task_succeeded", kinds)
        # The failing evidence is on the chain, and so is the refusal to pass.
        runs = [e for e in events if e["type"] == "tool_succeeded"
                and e["payload"]["detail"]["tool"] == "run_command"]
        self.assertEqual(runs[-1]["payload"]["detail"]["result"]["exit_code"], 1)
        verdicts = [e for e in events if e["type"] == "verification_result"]
        self.assertEqual(len(verdicts), 1)
        self.assertIs(verdicts[0]["payload"]["detail"].get("verified"), False)
        # The artifact really was written — that is exactly why file state alone
        # was never enough.
        self.assertTrue((stack.ws / "SUMMARY.md").exists())
        verify_trace(recorder.build(harness.store.load(final.id)))
        self.assertFalse(stack.tests_pass(), "the fixture must still be broken")
        stack.close()

    def test_an_irrelevant_passing_command_cannot_substitute(self) -> None:
        """The model runs something that passes; the declared check still decides."""
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        stack.world.sandbox.write("always_ok.py", "print('looks fine')\n")
        rounds = [
            (step("ls_workspace", {}), [add("q_list", "ls_workspace")]),
            # A whitelisted command that exits 0 and proves nothing.
            (step("run_command", {"command": "python3", "args": ["always_ok.py"]}), []),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Done\n\nI ran a check and it passed.\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=8)
        self.assertEqual(final.status, "failed", final.result)
        self.assertEqual(final.failure["code"], "RESULT_VERIFICATION_FAILED")
        events = stack.events(harness, final)
        exits = [e["payload"]["detail"]["result"]["exit_code"] for e in events
                 if e["type"] == "tool_succeeded"
                 and e["payload"]["detail"]["tool"] == "run_command"]
        # The irrelevant run passed (0); the declared obligation still failed (1).
        self.assertEqual(exits, [0, 1])
        stack.close()

    def test_declaring_nothing_cannot_pass_a_declared_obligation(self) -> None:
        """An empty plan extension plus an artifact is not a passing result."""
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        rounds = [
            (step("ls_workspace", {}), [add("r_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# All good\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "RESULT_VERIFICATION_FAILED")
        stack.close()


class AuthorizationTests(unittest.TestCase):
    """Tests 4 and 7: refused before execution, and revalidated at dispatch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rounds(self) -> list[tuple[dict, list[dict]]]:
        return [
            (step("ls_workspace", {}), [add("s_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]

    def test_an_unauthorized_declared_command_is_refused_at_assembly(self) -> None:
        """The host's command policy is the authority on what may run.

        Refused while the run is assembled — before a task, a plan or a read
        exists — so there is nothing to execute, nothing to charge and nothing
        that could appear as an accepted obligation in a replay.
        """
        with self.assertRaises(AgentError) as ctx:
            GENERAL_DOMAIN.build_tools(GENERAL_DOMAIN.create_world(
                spec=spec_with({"name": "shell out", "command": "sh", "args": ["-c", "exit 0"]}),
                sandbox=WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws"),
            ))
        self.assertEqual(ctx.exception.code, "VERIFICATION_DECLARATION_INVALID")
        self.assertIn("COMMAND_NOT_WHITELISTED", ctx.exception.message)

    def test_the_obligation_is_revalidated_at_dispatch(self) -> None:
        """Withdrawing authorization after acceptance still stops the run.

        The withdrawal uses a mechanism the runtime actually supports — the
        registry the gate reads. Editing ``model_grant`` mid-run is *not* such a
        mechanism: the store treats it as immutable, so claiming otherwise would
        be a fiction.
        """
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        narrowed = ToolRegistry([
            spec for spec in GENERAL_DOMAIN.build_tools(stack.world)
            if spec.name != "run_command"
        ])
        provider, harness, recorder, final = stack.run(
            self._rounds(), max_rounds=6, registry=narrowed,
        )
        events = stack.events(harness, final)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "VERIFICATION_NOT_AUTHORIZED")
        verdicts = [e for e in events if e["type"] == "verification_result"]
        self.assertEqual(verdicts[0]["payload"]["detail"]["reason"], "TOOL_NOT_REGISTERED")
        self.assertNotIn("task_succeeded", [e["type"] for e in events])
        stack.close()


class UnknownAndBudgetTests(unittest.TestCase):
    """Tests 5 and 6: unknown is never a pass, and never runs outside budget."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_unknown_verification_outcome_is_not_a_pass_and_is_not_retried(self) -> None:
        runner = UnknownRunner(sandbox=WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws"))
        stack = Stack(self._tmp.name, spec_with(DECLARATION), command_runner=runner)
        # The task itself runs no command, so the first dispatch is the runtime's.
        rounds = [
            (step("ls_workspace", {}), [add("u_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        events = stack.events(harness, final)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(runner.calls, 1, "an unknown verification outcome was re-dispatched")
        # The chain says what happened: the dispatch was recorded as unknown and
        # the task failed closed on the ordinary path.
        kinds = [e["type"] for e in events]
        self.assertIn("tool_unknown", kinds)
        self.assertIn("task_failed", kinds)
        self.assertNotIn("task_succeeded", kinds)
        self.assertNotIn("verification_result", kinds,
                         "an unresolvable run must not also record a verdict")
        stack.close()

    def test_verification_cannot_run_outside_the_budget(self) -> None:
        """If the required reservation cannot be made, there is no success."""
        # Exactly enough reads for the task's own actions, none for verification.
        limits = BudgetLimits(max_calls=12, max_reads=2, max_cost_microusd=400_000, max_tokens=400_000)
        stack = Stack(self._tmp.name, spec_with(DECLARATION), limits=limits)
        rounds = [
            (step("ls_workspace", {}), [add("b_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        events = stack.events(harness, final)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "BUDGET_EXCEEDED")
        self.assertNotIn("task_succeeded", [e["type"] for e in events])
        row = stack.store.budget_row(f"task:{final.id}")
        self.assertLessEqual(row["reads_used"], 2, "verification read outside the budget")
        stack.close()


class ReplayTests(unittest.TestCase):
    """Test 8: obligations and their verdicts are durable, and not re-run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_verification_history_survives_a_restart(self) -> None:
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        stack.world.sandbox.write("stats.py", BUGGY.replace("// len", "/ len"))
        rounds = [
            (step("ls_workspace", {}), [add("t_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# fixed\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "succeeded", final.failure)
        events_before = len(stack.events(harness, final))
        dispatches_before = len([e for e in stack.events(harness, final)
                                 if e["type"] == "tool_started"])
        stack.close()

        reopened = TaskStore(pathlib.Path(self._tmp.name) / "task.sqlite")
        reloaded = reopened.load(final.id)
        self.assertEqual(reloaded.status, "succeeded")
        self.assertEqual(reloaded.result["obligations"][0]["exit_code"], 0)
        self.assertEqual(reloaded.result["result_truth"], "runtime-recomputed-command-result")
        events = reopened.events(final.id)
        self.assertEqual(len(events), events_before)
        self.assertEqual(len([e for e in events if e["type"] == "tool_started"]), dispatches_before)
        kinds = [e["type"] for e in events]
        self.assertIn("verification_obligation", kinds)
        self.assertIn("verification_result", kinds)
        # The verdict precedes the terminal event, so the order reads as a story.
        self.assertLess(kinds.index("verification_result"), kinds.index("task_succeeded"))
        reopened.close()

    def test_a_failed_verification_replays_as_failed(self) -> None:
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        rounds = [
            (step("ls_workspace", {}), [add("f_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# claimed fixed\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "failed")
        stack.close()
        reopened = TaskStore(pathlib.Path(self._tmp.name) / "task.sqlite")
        reloaded = reopened.load(final.id)
        self.assertEqual(reloaded.status, "failed")
        self.assertEqual(reloaded.failure["code"], "RESULT_VERIFICATION_FAILED")
        verdicts = [e for e in reopened.events(final.id) if e["type"] == "verification_result"]
        self.assertIs(verdicts[0]["payload"]["detail"]["verified"], False)
        reopened.close()


class UnchangedBehaviourTests(unittest.TestCase):
    """Test 9: a task that declares nothing keeps the old, honestly labelled shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_declaration_still_succeeds_and_says_what_was_verified(self) -> None:
        stack = Stack(self._tmp.name, spec_with(None))
        rounds = [
            (step("ls_workspace", {}), [add("n_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# notes\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "succeeded", final.failure)
        self.assertEqual(final.result["file_state"], "file-state-recomputed")
        self.assertEqual(final.result["obligations"], [])
        # The honest label: nothing about a *result* was established.
        self.assertEqual(final.result["result_truth"], "no-result-obligation-declared")
        self.assertEqual(final.result["semantic_truth"], "not-verified")
        kinds = [e["type"] for e in stack.events(harness, final)]
        self.assertNotIn("verification_obligation", kinds)
        verify_trace(recorder.build(harness.store.load(final.id)))
        stack.close()

    def test_the_snapshot_leaves_the_workspace_alone(self) -> None:
        """Verification must not disturb the artifact it is verifying."""
        stack = Stack(self._tmp.name, spec_with(DECLARATION))
        stack.world.sandbox.write("stats.py", BUGGY.replace("// len", "/ len"))
        rounds = [
            (step("ls_workspace", {}), [add("c_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# fixed\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "succeeded", final.failure)
        # The verification run happened somewhere else: no byte-compiled cache
        # in the real workspace, and the artifact hash still matches.
        self.assertFalse((stack.ws / "__pycache__").exists(),
                         "the verification run mutated the workspace it verified")
        summary = (stack.ws / "SUMMARY.md").read_bytes()
        self.assertEqual(hashlib.sha256(summary).hexdigest(), final.result["sha256"])
        snapshot = pathlib.Path(self._tmp.name) / "verify-snapshot"
        self.assertTrue(snapshot.is_dir(), "no snapshot was taken")
        stack.close()

    def test_every_run_keeps_a_verifiable_chain(self) -> None:
        """Test 10: no hashing, serialization or ordering change."""
        for declaration, rounds in (
            (DECLARATION, [(step("ls_workspace", {}), [add("h_list", "ls_workspace")]),
                           (step("write_file", {"path": "SUMMARY.md", "content": "# x\n"}), [])]),
            (None, [(step("ls_workspace", {}), [add("h2_list", "ls_workspace")]),
                    (step("write_file", {"path": "SUMMARY.md", "content": "# y\n"}), [])]),
        ):
            with self.subTest(declared=declaration is not None), tempfile.TemporaryDirectory() as tmp:
                stack = Stack(tmp, spec_with(declaration))
                provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
                trace = recorder.build(harness.store.load(final.id))
                verify_trace(trace)
                sequences = [event["sequence"] for event in harness.store.events(final.id)]
                self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
                stack.close()


class EvidenceLabelTests(unittest.TestCase):
    """B1: an artifact's bytes, its sources, and its claims are three questions.

    Hashes settle the first. They never settle the third, and the labels say so
    instead of letting "runtime-verified" be read as "true".
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stats_digest(self, stack: Stack) -> str:
        return hashlib.sha256((stack.ws / "stats.py").read_bytes()).hexdigest()

    def test_e2_negative_an_unsupported_claim_is_not_semantically_verified(self) -> None:
        """E2': the summary names a file that does not exist.

        The run may still complete on its artifact obligation — nothing here
        claims otherwise — but the result must say plainly that no source was
        cited and that no claim was verified for truth.
        """
        stack = Stack(self._tmp.name, spec_with(None))
        rounds = [
            (step("ls_workspace", {}), [add("e_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Repo overview\n\n- `deploy.sh`: the production deploy entry "
                                            "point\n- `stats.py`: the helper\n"}), []),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        events = stack.events(harness, final)
        self.assertEqual(final.status, "succeeded", final.failure)
        self.assertFalse((stack.ws / "deploy.sh").exists(), "the fixture must make the claim false")
        # The three questions, separately answered.
        self.assertEqual(final.result["file_state"], "file-state-recomputed")
        self.assertEqual(final.result["source_links"], "not-declared")
        self.assertEqual(final.result["semantic_truth"], "not-verified")
        self.assertIn("not read for truth", final.result["truth_note"])
        self.assertEqual(final.result["sources"], [])
        # Nothing about the result may be read as "the deploy.sh claim is true".
        self.assertNotIn("deploy.sh", final.result.get("truth_note", ""))
        verify_trace(recorder.build(harness.store.load(final.id)))
        stack.close()

    def test_a_claim_citing_a_source_that_does_not_exist_fails_the_run(self) -> None:
        """A citation asserts support. Asserting support that is not there fails."""
        stack = Stack(self._tmp.name, spec_with(None))
        rounds = [
            (step("ls_workspace", {}), [add("u_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Repo overview\n\n- `deploy.sh`: the production deploy entry point\n"}),
             [], [{"claim": "there is a deploy entry point", "tool": "read_file",
                   "path": "deploy.sh", "sha256": "b" * 64}]),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        events = stack.events(harness, final)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "EVIDENCE_REFERENCE_UNRESOLVED")
        self.assertNotIn("task_succeeded", [e["type"] for e in events])
        checked = [e for e in events if e["type"] == "evidence_checked"]
        self.assertEqual(len(checked), 1)
        detail = checked[0]["payload"]["detail"]
        self.assertEqual(detail["resolved"], 0)
        self.assertEqual(detail["unresolved"][0]["path"], "deploy.sh")
        self.assertEqual(detail["semantic_truth"], "not-verified")
        verify_trace(recorder.build(harness.store.load(final.id)))
        stack.close()

    def test_a_real_citation_resolves_and_is_still_not_semantic_verification(self) -> None:
        """The distinction the labels exist to keep: linked ≠ true."""
        stack = Stack(self._tmp.name, spec_with(None))
        digest = self._stats_digest(stack)
        rounds = [
            (step("ls_workspace", {}), [add("v_list", "ls_workspace"),
                                        add("v_read", "read_file", {"path": "stats.py"})]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Repo overview\n\n- `stats.py`: holds `mean()`\n"}),
             [], [{"claim": "stats.py holds the mean() helper", "tool": "read_file",
                   "path": "stats.py", "sha256": digest}]),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=8)
        events = stack.events(harness, final)
        self.assertEqual(final.status, "succeeded", final.failure)
        self.assertEqual(final.result["source_links"], "resolved")
        self.assertEqual(len(final.result["sources"]), 1)
        self.assertEqual(final.result["sources"][0]["path"], "stats.py")
        self.assertEqual(final.result["sources"][0]["sha256"], digest)
        self.assertIsNotNone(final.result["sources"][0]["observed_round"])
        # And yet: the claim itself is still not verified.
        self.assertEqual(final.result["semantic_truth"], "not-verified")
        self.assertIn("not something hashes decide", final.result["truth_note"])
        verify_trace(recorder.build(harness.store.load(final.id)))
        stack.close()

    def test_a_citation_of_a_wrong_digest_is_unresolved(self) -> None:
        """Citing the right file with the wrong content is still a false citation."""
        stack = Stack(self._tmp.name, spec_with(None))
        rounds = [
            (step("ls_workspace", {}), [add("w_list", "ls_workspace"),
                                        add("w_read", "read_file", {"path": "stats.py"})]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("write_file", {"path": "SUMMARY.md", "content": "# notes\n"}),
             [], [{"claim": "stats.py is unchanged", "tool": "read_file",
                   "path": "stats.py", "sha256": "c" * 64}]),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=8)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "EVIDENCE_REFERENCE_UNRESOLVED")
        stack.close()

    def test_malformed_evidence_is_refused_as_a_malformed_proposal(self) -> None:
        stack = Stack(self._tmp.name, spec_with(None))
        rounds = [
            (step("ls_workspace", {}), [add("m_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# x\n"}),
             [], [{"claim": "c", "tool": "read_file", "path": "p", "sha256": "not-a-digest"}]),
        ]
        provider, harness, recorder, final = stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "EVIDENCE_INVALID")
        events = stack.events(harness, final)
        self.assertIn("model_invalid", [e["type"] for e in events])
        self.assertNotIn("task_succeeded", [e["type"] for e in events])
        stack.close()


if __name__ == "__main__":
    unittest.main()