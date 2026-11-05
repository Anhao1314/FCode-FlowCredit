"""``report_blocked`` in the general domain: an honest stop is not a denial.

The shared system prompt instructs every model to propose ``report_blocked``
when the evidence runs out or the plan cannot proceed. The general registry did
not carry that tool, so a model that *followed* the instruction was refused as
``TOOL_NOT_REGISTERED`` and the run ended ``denied`` — the audit chain recorded
a governance violation where the host's own prompt had asked for a stop.

These tests pin the corrected contract: registered, gated like any other tool,
terminal outcome ``failed / INSUFFICIENT_EVIDENCE`` (never ``succeeded``), the
stop durable on the chain, replay-stable, and with unknown-tool strictness and
budget accounting untouched.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.plan import Plan, Todo  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.permissions import Permissions  # noqa: E402
from flowagent.runtime.policy import ActionChoice, compile_packet  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import (  # noqa: E402
    GENERAL_TOOLS,
    MAX_BLOCK_REASON_CHARS,
    build_general_tools,
)
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402

SPEC = {"mode": "script", "scriptPath": "script.py"}


class ProposalProvider:
    """Proposes one fixed step every round. Deterministic, offline."""

    name = "test-fixed"
    model = "test-fixed-v1"

    def __init__(self, tool: str, arguments: dict) -> None:
        self.tool = tool
        self.arguments = dict(arguments)
        self.calls = 0

    def propose(self, context: dict) -> Proposal:
        self.calls += 1
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [
                {"id": "s1", "tool": self.tool, "arguments": dict(self.arguments)},
            ]},
            provider=self.name,
            model=self.model,
            tokens=10,
            cost_microusd=10,
            latency_ms=1,
            todo_id=None,
        )


class StopPolicy:
    """Chooses the report_blocked candidate specifically (packet path)."""

    name = "test-stop-policy"
    model = "test-stop-policy-v1"

    def __init__(self) -> None:
        self.chosen: list[str] = []

    def choose(self, packet) -> ActionChoice:
        for candidate in packet.candidates:
            if candidate.tool == "report_blocked":
                self.chosen.append(candidate.id)
                return ActionChoice(candidate.id)
        raise AssertionError(f"no report_blocked candidate in {[c.tool for c in packet.candidates]}")


class BlockedAwarePlanner:
    """An open step plus an *optional* blocked one.

    The optional blocked todo is what makes ``compile_packet`` offer the stop
    candidate while the plan itself stays open — the exact condition the packet
    path uses to reach a stop the provider path also has to be able to reach.
    """

    def __init__(self, tool: str, arguments: dict) -> None:
        self.tool = tool
        self.arguments = dict(arguments)

    def decompose(self, goal: str) -> Plan:
        return Plan(goal=goal, todos=[
            Todo(id="o1", text="an open step", tool=self.tool,
                 done_when="any", arguments=dict(self.arguments)),
            Todo(id="b1", text="a step that cannot proceed", tool=self.tool,
                 done_when="never", arguments=dict(self.arguments),
                 status="blocked", reason="no evidence", required=False),
        ])

    def advance(self, plan: Plan, observation) -> Plan:
        return plan


class Stack:
    """The real stack: store, world, registry, receipts, harness."""

    def __init__(self, tmp: str, spec: dict = SPEC) -> None:
        self.db = pathlib.Path(tmp) / "task.sqlite"
        self.store = TaskStore(self.db)
        self.world = GENERAL_DOMAIN.create_world(
            spec=spec, sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
        )
        self.registry = ToolRegistry(GENERAL_DOMAIN.build_tools(self.world))
        self.receipts = ReceiptStore(self.store)

    def harness(self, provider, limits) -> Harness:
        return Harness(
            store=self.store,
            registry=self.registry,
            provider=provider,
            world=self.world,
            limits=limits,
            receipts=self.receipts,
            verifier=lambda observation: GENERAL_DOMAIN.verify(self.world, observation),
            state_fn=GENERAL_DOMAIN.summarize_state,
            instruction=GENERAL_DOMAIN.instruction,
        )

    def create_task(self, scenario, task_id: str = "task-stop"):
        harness = self.harness(ProposalProvider("read_file", {"path": "x"}), scenario.limits)
        return harness.create(
            task_id=task_id,
            objective="完成一项通用委托",
            principal="test",
            binding={"scenario": scenario.key},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )

    def close(self) -> None:
        self.store.close()


def _fetcher() -> UrlFetcher:
    return UrlFetcher()


class RegistrationTests(unittest.TestCase):
    """A. Registered on every production path, with the right classification."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_native_general_toolset_registers_report_blocked(self) -> None:
        registry = ToolRegistry(build_general_tools(self.sandbox, _fetcher()))
        self.assertIn("report_blocked", registry.names)
        self.assertEqual(tuple(registry.names), GENERAL_TOOLS)
        # A declaration, not a world effect: it must not need a write grant.
        self.assertEqual(registry.get("report_blocked").effect, "read")

    def test_domain_built_registry_registers_it_on_demo_and_console_paths(self) -> None:
        # `demo/runner.build_stack` and `viewer/console_manager` both assemble
        # the registry through this one call, so one assertion covers both.
        world = GENERAL_DOMAIN.create_world(spec=SPEC, sandbox=self.sandbox)
        names = [spec.name for spec in GENERAL_DOMAIN.build_tools(world)]
        self.assertIn("report_blocked", names)

    def test_general_scenario_scope_and_grant_both_include_it(self) -> None:
        scenario = get_scenario("general-research")
        # The gate checks all three scopes; missing from either list would
        # still be a refusal, just with a different code.
        self.assertIn("report_blocked", scenario.allowed_tools)
        self.assertIn("report_blocked", scenario.grant["allowed_tools"])

    def test_the_gate_lets_a_valid_stop_through(self) -> None:
        scenario = get_scenario("general-research")
        registry = ToolRegistry(build_general_tools(self.sandbox, _fetcher()))
        gate = Permissions(registry, list(scenario.allowed_tools), scenario.grant)
        decision = gate.gate("report_blocked", {"reason": "no test entry point exists"})
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.code, "PASS")

    def test_it_is_not_an_approval_tool(self) -> None:
        # An honest stop must not depend on an operator clicking: a silent
        # operator would turn "insufficient evidence" into APPROVAL_TIMEOUT.
        self.assertNotIn("report_blocked", GENERAL_DOMAIN.approval_tools)
        self.assertNotIn("report_blocked", get_scenario("general-research").approval_tools)

    def test_reason_bound_is_declared_on_the_schema(self) -> None:
        registry = ToolRegistry(build_general_tools(self.sandbox, _fetcher()))
        reason = registry.get("report_blocked").params[0]
        self.assertEqual(reason.name, "reason")
        self.assertTrue(reason.required)
        self.assertEqual(reason.max_length, MAX_BLOCK_REASON_CHARS)


class HonestStopTests(unittest.TestCase):
    """B, C, D, F, I: accepted, honest, durable, never successful."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scenario = get_scenario("general-research")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stop_run(self, reason: str):
        stack = Stack(self._tmp.name)
        provider = ProposalProvider("report_blocked", {"reason": reason})
        harness = stack.harness(provider, self.scenario.limits)
        task = stack.create_task(self.scenario)
        recorder = TraceRecorder(harness.store, provider_name=provider.name, model_name=provider.model)
        loop = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(stack.world),
            max_rounds=self.scenario.max_rounds,
            observer=recorder,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        final = loop.run(task.id)
        return stack, harness, recorder, provider, final

    def test_valid_stop_is_not_misclassified_as_unregistered(self) -> None:
        """B."""
        _, _, _, provider, final = self._stop_run("the workspace has no test entry point")
        self.assertEqual(provider.calls, 1)
        self.assertIsNotNone(final.failure)
        self.assertNotEqual(final.failure["code"], "TOOL_NOT_REGISTERED")
        self.assertEqual(final.status, "failed")

    def test_valid_stop_ends_failed_with_insufficient_evidence(self) -> None:
        """C."""
        reason = "the workspace has no test entry point"
        _, _, _, _, final = self._stop_run(reason)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(final.failure["message"], reason)
        self.assertFalse(final.failure["retryable"])
        # Nothing was verified, so no result may exist.
        self.assertIsNone(final.result)

    def test_chain_carries_the_stop_marker_and_reason_and_verifies(self) -> None:
        """D."""
        reason = "the workspace has no test entry point"
        stack, harness, recorder, _, final = self._stop_run(reason)
        events = harness.store.events(final.id)
        stopped = [e for e in events if e["type"] == "task_stopped"]
        self.assertEqual(len(stopped), 1)
        detail = stopped[0]["payload"]["detail"]
        self.assertEqual(detail["code"], "MODEL_REPORTED_BLOCKED")
        self.assertEqual(detail["model_reason"], reason)
        # The declaration is itself a gated observation with its reason recorded.
        succeeded = [e for e in events if e["type"] == "tool_succeeded"]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0]["payload"]["detail"]["tool"], "report_blocked")
        self.assertEqual(succeeded[0]["payload"]["detail"]["result"]["reason"], reason)
        # No verification ran, and the chain still recomputes.
        kinds = [e["type"] for e in events]
        self.assertNotIn("task_succeeded", kinds)
        self.assertNotIn("verification_started", kinds)
        verify_trace(recorder.build(harness.store.load(final.id)))
        stack.close()

    def test_the_model_cannot_use_the_stop_to_declare_success(self) -> None:
        """F."""
        stack, harness, _, _, final = self._stop_run("blocked")
        kinds = [e["type"] for e in harness.store.events(final.id)]
        self.assertNotIn("task_succeeded", kinds)
        self.assertNotIn("verification_started", kinds)
        self.assertIsNone(final.result)
        stored = harness.store.load(final.id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure["code"], "INSUFFICIENT_EVIDENCE")
        stack.close()

    def test_budget_and_receipt_accounting_is_unchanged(self) -> None:
        """I."""
        stack, harness, _, provider, final = self._stop_run("blocked")
        events = harness.store.events(final.id)
        row = stack.store.budget_row(f"task:{final.id}")
        model_started = [e for e in events if e["type"] == "model_started"]
        tool_started = [e for e in events if e["type"] == "tool_started"]
        self.assertEqual(len(model_started), provider.calls)
        # Calls and reads are still charged before dispatch.
        self.assertEqual(row["calls_used"], len(model_started))
        self.assertEqual(row["reads_used"], len(tool_started))
        self.assertEqual(final.model.attempts, len(model_started))
        # The round receipt the runtime keeps still names the provider.
        self.assertEqual(final.model.receipt["provider"], provider.name)
        self.assertEqual(final.model.receipt["external_billing"], "not-verified")
        stack.close()


class ReplayTests(unittest.TestCase):
    """E. A new process reconstructs the same stop and dispatches nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scenario = get_scenario("general-research")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reopening_the_store_preserves_the_stop_without_redispatch(self) -> None:
        reason = "the repository has no test entry point"
        stack = Stack(self._tmp.name)
        provider = ProposalProvider("report_blocked", {"reason": reason})
        harness = stack.harness(provider, self.scenario.limits)
        task = stack.create_task(self.scenario)
        final = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(stack.world),
            max_rounds=self.scenario.max_rounds,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        events_before = len(harness.store.events(final.id))
        dispatches_before = len(
            [e for e in harness.store.events(final.id) if e["type"] == "tool_started"]
        )
        stack.close()

        # A new process: the same durable database, fresh objects.
        reopened = Stack(self._tmp.name)
        reloaded = reopened.store.load(final.id)
        self.assertEqual(reloaded.status, "failed")
        self.assertEqual(reloaded.failure["code"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(reloaded.failure["message"], reason)
        verify_trace(TraceRecorder(
            reopened.store, provider_name="replay", model_name="replay",
        ).build(reloaded))

        # What a resume does before deciding anything: replay the log.
        second = ProposalProvider("report_blocked", {"reason": reason})
        resume_harness = reopened.harness(second, self.scenario.limits)
        resume_loop = Loop(
            resume_harness,
            planner=GENERAL_DOMAIN.make_planner(reopened.world),
            max_rounds=self.scenario.max_rounds,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        memory = resume_loop.restore(reloaded)
        plan = resume_loop.rebuild_plan(reloaded, memory)

        # Replay is a read: no model call, no dispatch, no new event.
        self.assertEqual(second.calls, 0)
        self.assertEqual(len(reopened.store.events(final.id)), events_before)
        self.assertEqual(
            len([e for e in reopened.store.events(final.id) if e["type"] == "tool_started"]),
            dispatches_before,
        )
        # The declared blocker survives into the replayed state card.
        self.assertIn(reason, memory.state()["blockers"])
        self.assertEqual(plan.goal, reloaded.plan_state["goal"])
        reopened.close()


class RejectionTests(unittest.TestCase):
    """G, H. Malformed stops and unrelated unknown tools stay refused."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scenario = get_scenario("general-research")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, tool: str, arguments: dict):
        stack = Stack(self._tmp.name)
        provider = ProposalProvider(tool, arguments)
        harness = stack.harness(provider, self.scenario.limits)
        task = stack.create_task(self.scenario)
        final = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(stack.world),
            max_rounds=self.scenario.max_rounds,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        events = harness.store.events(final.id)
        stack.close()
        return provider, final, events

    def test_missing_reason_is_refused_without_dispatch(self) -> None:
        """G."""
        provider, final, events = self._run("report_blocked", {})
        self.assertEqual(final.status, "denied")
        self.assertEqual(final.failure["code"], "TOOL_ARGUMENTS_INVALID")
        # Shape errors are repairable, bounded, and never dispatched.
        self.assertEqual(provider.calls, 3)
        self.assertEqual([e for e in events if e["type"] == "tool_started"], [])
        self.assertEqual([e for e in events if e["type"] == "tool_succeeded"], [])
        self.assertEqual(
            [e["payload"]["detail"]["attempt"] for e in events
             if e["type"] == "proposal_rejected"],
            [1, 2],
        )

    def test_oversized_reason_is_refused_at_the_gate(self) -> None:
        """G."""
        _, final, events = self._run(
            "report_blocked", {"reason": "x" * (MAX_BLOCK_REASON_CHARS + 1)},
        )
        self.assertEqual(final.status, "denied")
        self.assertEqual(final.failure["code"], "TOOL_ARGUMENTS_INVALID")
        self.assertEqual([e for e in events if e["type"] == "tool_started"], [])

    def test_non_string_reason_is_refused_at_the_gate(self) -> None:
        """G."""
        _, final, _ = self._run("report_blocked", {"reason": 5})
        self.assertEqual(final.status, "denied")
        self.assertEqual(final.failure["code"], "TOOL_ARGUMENTS_INVALID")

    def test_an_unknown_parameter_is_refused(self) -> None:
        """G. A model inventing a parameter has left the schema."""
        _, final, _ = self._run(
            "report_blocked", {"reason": "ok", "stop_the_world": True},
        )
        self.assertEqual(final.status, "denied")
        self.assertEqual(final.failure["code"], "TOOL_ARGUMENTS_INVALID")

    def test_an_unrelated_unknown_tool_is_still_denied_with_zero_reads(self) -> None:
        """H. Registering the stop tool must not weaken unknown-tool strictness."""
        _, final, events = self._run("write_document", {"doc_id": "doc_fy2024"})
        self.assertEqual(final.status, "denied")
        self.assertEqual(final.failure["code"], "TOOL_NOT_REGISTERED")
        self.assertEqual([e for e in events if e["type"] == "tool_started"], [])
        self.assertEqual(len([e for e in events if e["type"] == "tool_denied"]), 1)


class PathAgreementTests(unittest.TestCase):
    """J. Packet and provider paths agree about what stopping means."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scenario = get_scenario("general-research")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_packet_mode_offers_a_stop_candidate_that_passes_the_gate(self) -> None:
        stack = Stack(self._tmp.name)
        harness = stack.harness(ProposalProvider("read_file", {"path": "x"}), self.scenario.limits)
        task = stack.create_task(self.scenario)
        memory = Loop(
            harness,
            planner=BlockedAwarePlanner("read_file", {"path": "x"}),
            max_rounds=1,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).restore(task)
        packet = compile_packet(
            objective=task.objective,
            round_no=1,
            plan=BlockedAwarePlanner("read_file", {"path": "x"}).decompose(task.objective),
            memory=memory,
            registry=stack.registry,
            budget={"calls_remaining": 5},
        )
        stop = [c for c in packet.candidates if c.tool == "report_blocked"]
        self.assertEqual([c.tool for c in packet.candidates], ["read_file", "report_blocked"])
        self.assertEqual(len(stop), 1)
        # The candidate the compiler offers must pass the real gate — otherwise
        # the packet path would hand the model a choice it has to refuse.
        gate = Permissions(stack.registry, list(self.scenario.allowed_tools), self.scenario.grant)
        decision = gate.gate(stop[0].tool, dict(stop[0].arguments))
        self.assertTrue(decision.allowed, decision.reason)
        stack.close()

    def test_a_packet_chosen_stop_ends_exactly_like_the_provider_path(self) -> None:
        stack = Stack(self._tmp.name)
        policy = StopPolicy()
        harness = stack.harness(ProposalProvider("read_file", {"path": "x"}), self.scenario.limits)
        task = stack.create_task(self.scenario)
        final = Loop(
            harness,
            planner=BlockedAwarePlanner("read_file", {"path": "x"}),
            max_rounds=4,
            policy=policy,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        self.assertTrue(policy.chosen, "the policy never saw a stop candidate")
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.failure["code"], "INSUFFICIENT_EVIDENCE")
        # The packet path prefers the domain's own blocked reason over the
        # model's enum — the same rule the provider path uses.
        self.assertEqual(final.failure["message"], "no evidence")
        kinds = [e["type"] for e in harness.store.events(final.id)]
        self.assertIn("policy_decided", kinds)
        detail = [e for e in harness.store.events(final.id)
                  if e["type"] == "task_stopped"][0]["payload"]["detail"]
        self.assertEqual(detail["code"], "MODEL_REPORTED_BLOCKED")
        self.assertNotIn("task_succeeded", kinds)
        stack.close()


if __name__ == "__main__":
    unittest.main()