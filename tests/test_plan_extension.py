"""Governed plan addition: the model proposes steps, the host decides.

A general delegation's tasks are not knowable in advance, so its plan has to be
extensible or the run cannot finish: every action settles nothing, the plan never
completes, and the round limit ends it. These tests pin the mechanism that fixes
that *without* handing the model the plan — a proposal is a request, the host
validates it through the same gate a step faces, an accepted extension is
persisted before it can drive anything, and a refusal is an event the run
continues from rather than a death sentence.

The completion contract is deliberately untouched, so the tests also pin what an
added step still cannot do: mark itself done, redefine verification, or make the
run succeed without a real, verified observation.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import (  # noqa: E402
    MAX_PLAN_ADDITIONS_PER_PROPOSAL,
    PLAN_VERSION,
    AgentError,
    validate_plan,
)
from flowagent.core.plan import Plan, Todo  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.control import RoundControl, ToolControl  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.plan_extension import (  # noqa: E402
    MAX_PLAN_TODOS,
    validate_additions,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402

SPEC = {"mode": "open", "reportPath": "SUMMARY.md"}

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


def make_workspace(root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "stats.py").write_text(BUGGY, encoding="utf-8")
    (root / "test_stats.py").write_text(TEST, encoding="utf-8")
    (root / "README.md").write_text(README, encoding="utf-8")


def step(tool: str, arguments: dict, todo_id: str = "a1") -> dict:
    return {"id": todo_id, "tool": tool, "arguments": arguments}


def addition(tid: str, tool: str, arguments: dict | None = None, **extra) -> dict:
    entry = {
        "id": tid,
        "text": extra.pop("text", f"step {tid}"),
        "tool": tool,
        "done_when": extra.pop("done_when", f"observed:{tid}"),
        "arguments": arguments or {},
    }
    entry.update(extra)
    return entry


class PlannedProvider:
    """One (action, additions) pair per round; a stop when the script runs out."""

    name = "planned"
    model = "planned-v1"

    def __init__(self, rounds: list[tuple[dict, list[dict]]]) -> None:
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
        action, additions = self.rounds.pop(0)
        plan: dict = {"version": PLAN_VERSION, "steps": [action]}
        if additions:
            plan["additions"] = additions
        return Proposal(plan=plan, provider=self.name, model=self.model,
                        tokens=20, cost_microusd=20, latency_ms=1, todo_id=action.get("id"))


class RegistrySwapControl:
    """An operator who withdraws a capability while the run is in flight.

    Swaps the registry the gate reads, rather than editing the task's grant: the
    store treats ``model_grant`` as immutable after creation (``IMMUTABLE_FIELD_
    CHANGED``), so a grant edit mid-run is rejected — which is a pre-existing
    property of the task contract, recorded in the Phase A report and not
    changed here. Swapping the registry proves the same thing about dispatch:
    the plan accepted the step, and the gate still re-asked at execution time.
    """

    def __init__(self, harness, replacement, *, after_round: int) -> None:
        self.harness = harness
        self.replacement = replacement
        self.after_round = after_round
        self.swapped = False

    def before_round(self, *, round_no: int, task, plan) -> RoundControl:
        if round_no > self.after_round and not self.swapped:
            self.harness.registry = self.replacement
            self.swapped = True
        return RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task, step, decision) -> ToolControl:
        return ToolControl(task=task, allowed=True)


class Stack:
    """Real store, world, registry, receipts and harness."""

    def __init__(self, tmp: str, spec: dict = SPEC, *, make_ws: bool = True,
                 command_runner: Any = None) -> None:
        self.tmp = pathlib.Path(tmp)
        self.store = TaskStore(self.tmp / "task.sqlite")
        self.ws = self.tmp / "ws"
        if make_ws:
            make_workspace(self.ws)
        self.world = GENERAL_DOMAIN.create_world(
            spec=spec,
            sandbox=WorkspaceSandbox(self.ws),
            command_runner=command_runner,
        )
        self.registry = ToolRegistry(GENERAL_DOMAIN.build_tools(self.world))
        self.receipts = ReceiptStore(self.store)

    def harness(self, provider, *, limits: BudgetLimits | None = None) -> Harness:
        return Harness(
            store=self.store,
            registry=self.registry,
            provider=provider,
            world=self.world,
            limits=limits or BudgetLimits(24, 48, 400_000, 400_000),
            receipts=self.receipts,
            verifier=lambda observation: GENERAL_DOMAIN.verify(self.world, observation),
            state_fn=GENERAL_DOMAIN.summarize_state,
            instruction=GENERAL_DOMAIN.instruction,
            prompt_profile=GENERAL_DOMAIN.prompt_profile,
            plan_extension=GENERAL_DOMAIN.plan_extension,
        )

    def task(self, harness, task_id: str = "task-plan"):
        scenario = get_scenario("general-research")
        return harness.create(
            task_id=task_id,
            objective="修好这个失败的测试，并写一份 SUMMARY.md",
            principal="test",
            binding={"scenario": scenario.key},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )

    def run(self, rounds, *, max_rounds: int = 12, control=None, task_id: str = "task-plan"):
        provider = PlannedProvider(rounds)
        harness = self.harness(provider)
        task = self.task(harness, task_id)
        recorder = TraceRecorder(harness.store, provider_name=provider.name, model_name=provider.model)
        loop = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(self.world),
            max_rounds=max_rounds,
            observer=recorder,
            control=control,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        final = loop.run(task.id)
        return provider, harness, recorder, loop, final

    def close(self) -> None:
        self.store.close()


def scripted_fix_rounds() -> list[tuple[dict, list[dict]]]:
    """E1's shape: explore, read, run, then declare the repair once known."""

    return [
        (step("ls_workspace", {}), [
            addition("p_list", "ls_workspace"),
            addition("p_read", "read_file", {"path": "stats.py"}),
            addition("p_run", "run_command", {"command": "python3", "args": ["test_stats.py"]}),
        ]),
        (step("read_file", {"path": "stats.py"}), []),
        (step("run_command", {"command": "python3", "args": ["test_stats.py"]}), [
            # The anchor is only knowable after the read, so the edit step is
            # declared here — and its full argument set is schema-checked at
            # plan time, which is the point.
            addition("p_fix", "edit_file", {
                "path": "stats.py",
                "old_text": "    return sum(values) // len(values)",
                "new_text": "    return sum(values) / len(values)",
            }),
            addition("p_verify", "run_command", {"command": "python3", "args": ["test_stats.py"]}),
        ]),
        (step("edit_file", {
            "path": "stats.py",
            "old_text": "    return sum(values) // len(values)",
            "new_text": "    return sum(values) / len(values)",
        }), []),
        (step("run_command", {"command": "python3", "args": ["test_stats.py"]}), []),
        (step("write_file", {"path": "SUMMARY.md",
                             "content": "# Fix report\n\n`mean()` used floor division; switched to true "
                                        "division. `python3 test_stats.py` passes.\n"}), []),
    ]


class EnvelopeShapeTests(unittest.TestCase):
    """The wire contract: additions are bounded, well-formed, and host-owned."""

    def _plan(self, additions) -> dict:
        return {"version": PLAN_VERSION, "steps": [step("read_file", {"path": "x.py"})],
                "additions": additions}

    def test_a_proposal_without_additions_is_unchanged(self) -> None:
        validated = validate_plan({"version": PLAN_VERSION, "steps": [step("read_file", {"path": "x"})]})
        self.assertEqual(validated.additions, ())
        self.assertNotIn("additions", validated.to_dict())

    def test_an_empty_additions_list_means_no_request(self) -> None:
        """Declining to extend is a legitimate answer, not a malformed one."""
        validated = validate_plan(self._plan([]))
        self.assertEqual(validated.additions, ())
        self.assertNotIn("additions", validated.to_dict())

    def test_a_legal_addition_is_carried_on_the_validated_proposal(self) -> None:
        """A."""
        validated = validate_plan(self._plan([addition("p1", "read_file", {"path": "y.py"})]))
        self.assertEqual(len(validated.additions), 1)
        self.assertEqual(validated.additions[0]["id"], "p1")
        self.assertEqual(validated.to_dict()["additions"][0]["tool"], "read_file")

    def test_an_unknown_envelope_key_is_still_refused(self) -> None:
        payload = self._plan([])
        payload["surprise"] = 1
        with self.assertRaises(AgentError) as ctx:
            validate_plan(payload)
        self.assertEqual(ctx.exception.code, "PLAN_SCHEMA_INVALID")

    def test_missing_required_fields_are_refused(self) -> None:
        """F."""
        for drop in ("id", "text", "tool", "done_when"):
            with self.subTest(dropping=drop):
                entry = addition("p1", "read_file", {"path": "y.py"})
                entry.pop(drop)
                with self.assertRaises(AgentError) as ctx:
                    validate_plan(self._plan([entry]))
                self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")
                self.assertIn(drop, ctx.exception.message)

    def test_host_owned_fields_are_refused_not_stripped(self) -> None:
        """F, J. A proposal may not write status, evidence, required or reason."""
        for field, value in (("status", "done"), ("evidence", ["x"]), ("required", False),
                             ("reason", "already finished")):
            with self.subTest(field=field):
                entry = addition("p1", "read_file", {"path": "y.py"}, **{field: value})
                with self.assertRaises(AgentError) as ctx:
                    validate_plan(self._plan([entry]))
                self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")
                self.assertIn(field, ctx.exception.message)

    def test_ids_must_be_well_formed_and_unique(self) -> None:
        """F."""
        for bad in ("P1", "1p", "", "p" * 41, "p one"):
            with self.subTest(id=bad):
                with self.assertRaises(AgentError):
                    validate_plan(self._plan([addition(bad, "read_file", {"path": "y.py"})]))
        with self.assertRaises(AgentError) as ctx:
            validate_plan(self._plan([
                addition("p1", "read_file", {"path": "y.py"}),
                addition("p1", "read_file", {"path": "z.py"}),
            ]))
        self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")

    def test_an_id_already_in_the_steps_is_refused(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            validate_plan({
                "version": PLAN_VERSION,
                "steps": [step("read_file", {"path": "x.py"}, "a1")],
                "additions": [addition("a1", "read_file", {"path": "y.py"})],
            })
        self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")

    def test_the_quantity_bound_is_enforced(self) -> None:
        """G."""
        entries = [addition(f"p{i}", "read_file", {"path": f"{i}.py"})
                   for i in range(MAX_PLAN_ADDITIONS_PER_PROPOSAL + 1)]
        with self.assertRaises(AgentError) as ctx:
            validate_plan(self._plan(entries))
        self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")
        # Exactly at the bound is fine.
        validate_plan(self._plan(entries[:MAX_PLAN_ADDITIONS_PER_PROPOSAL]))

    def test_fields_must_be_the_declared_type(self) -> None:
        """F."""
        for field, value in (("text", 5), ("done_when", 5), ("tool", 5), ("arguments", "nope")):
            with self.subTest(field=field, value=value):
                entry = addition("p1", "read_file", {"path": "y.py"})
                entry[field] = value
                with self.assertRaises(AgentError) as ctx:
                    validate_plan(self._plan([entry]))
                self.assertEqual(ctx.exception.code, "PLAN_ADDITION_INVALID")

    def test_oversized_text_and_arguments_are_refused(self) -> None:
        """G."""
        entry = addition("p1", "read_file", {"path": "y.py"}, text="x" * 401)
        with self.assertRaises(AgentError):
            validate_plan(self._plan([entry]))
        entry = addition("p1", "read_file", {"path": "y.py"}, done_when="x" * 201)
        with self.assertRaises(AgentError):
            validate_plan(self._plan([entry]))
        entry = addition("p1", "write_file", {"path": "y.py", "content": "x" * (97 * 1024)})
        with self.assertRaises(AgentError):
            validate_plan(self._plan([entry]))


class HostValidationTests(unittest.TestCase):
    """The authority checks, in isolation from the loop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = Stack(self._tmp.name)
        self.scenario = get_scenario("general-research")
        self.harness = self.stack.harness(PlannedProvider([]))
        self.task = self.stack.task(self.harness)
        self.permissions = self.harness.permissions(self.task)
        self.plan = Plan(goal="g", todos=[
            Todo(id="out", text="artifact", tool="write_file", done_when="written",
                 arguments={"path": "SUMMARY.md"}),
        ])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmp.cleanup()

    def _judge(self, additions, *, allowed_tools=None, grant=None, enabled=True, plan=None,
               result_tools=GENERAL_DOMAIN.result_tools):
        perm = self.permissions
        if allowed_tools is not None or grant is not None:
            from flowagent.runtime.permissions import Permissions

            perm = Permissions(
                self.stack.registry,
                list(allowed_tools if allowed_tools is not None else self.scenario.allowed_tools),
                dict(grant if grant is not None else self.scenario.grant),
            )
        shape = validate_plan({
            "version": PLAN_VERSION,
            "steps": [step("read_file", {"path": "x.py"})],
            "additions": additions,
        }).additions
        return validate_additions(
            list(shape), plan=plan or self.plan, registry=self.stack.registry,
            permissions=perm, enabled=enabled, result_tools=result_tools,
        )

    def test_a_legal_addition_is_accepted(self) -> None:
        """A."""
        verdict = self._judge([addition("p1", "read_file", {"path": "stats.py"})])
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(verdict.added_ids(), ["p1"])
        todo = verdict.accepted[0]
        self.assertEqual(todo.tool, "read_file")
        # The host, not the proposer, decides state: pending, no evidence, required.
        self.assertEqual(todo.status, "pending")
        self.assertEqual(todo.evidence, ())
        self.assertTrue(todo.required)

    def test_an_unregistered_tool_is_refused(self) -> None:
        """E."""
        verdict = self._judge([addition("p1", "write_document", {"doc_id": "d"})])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "TOOL_NOT_REGISTERED")

    def test_a_schema_invalid_argument_set_is_refused(self) -> None:
        """F. A step whose arguments cannot be formed is not a step yet."""
        verdict = self._judge([addition("p1", "edit_file", {"path": "stats.py"})])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "TOOL_ARGUMENTS_INVALID")
        self.assertIn("old_text", verdict.rejected[0]["message"])

    def test_a_tool_outside_the_task_scope_is_refused(self) -> None:
        """H."""
        verdict = self._judge(
            [addition("p1", "read_file", {"path": "stats.py"})],
            allowed_tools=["write_file"],
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "TOOL_NOT_IN_TASK_SCOPE")

    def test_a_tool_outside_the_effect_grant_is_refused(self) -> None:
        """H. A read-only grant cannot be extended by proposing a write step."""
        read_only = dict(self.scenario.grant, allowed_effects=["read"])
        verdict = self._judge(
            [addition("p1", "write_file", {"path": "notes.md", "content": "x"})],
            grant=read_only,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "TOOL_EFFECT_DENIED")

    def test_a_control_tool_is_not_a_step(self) -> None:
        """C. report_blocked declares a stop; it cannot be a plan step."""
        verdict = self._judge([addition("p1", "report_blocked", {"reason": "give up"})])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "PLAN_ADDITION_CONTROL_TOOL_REFUSED")

    def test_a_duplicate_id_is_refused(self) -> None:
        """D."""
        for colliding in ("out",):
            with self.subTest(id=colliding):
                verdict = self._judge([addition(colliding, "read_file", {"path": "stats.py"})])
                self.assertFalse(verdict.ok)
                self.assertIn(verdict.rejected[0]["code"],
                              ("PLAN_ADDITION_DUPLICATE_ID", "PLAN_ADDITION_INVALID"))

    def test_a_second_step_for_the_result_target_is_refused(self) -> None:
        """D, J. The domain's result obligation is not duplicated."""
        verdict = self._judge([addition("p9", "write_file",
                                        {"path": "SUMMARY.md", "content": "again"})])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "PLAN_ADDITION_RESULT_DUPLICATE")

    def test_the_plan_size_bound_is_enforced(self) -> None:
        """G."""
        full = Plan(goal="g", todos=[
            Todo(id=f"t{i}", text="x", tool="read_file", done_when="d", arguments={"path": f"{i}.py"})
            for i in range(MAX_PLAN_TODOS)
        ])
        verdict = self._judge([addition("p1", "read_file", {"path": "new.py"})], plan=full)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "PLAN_ADDITION_PLAN_FULL")

    def test_a_disabled_domain_refuses_with_its_own_reason(self) -> None:
        """H. Finance keeps its plan; the refusal says which, not 'illegal'."""
        verdict = self._judge([addition("p1", "read_file", {"path": "stats.py"})], enabled=False)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "PLAN_EXTENSION_DISABLED")

    def test_one_bad_entry_refuses_the_whole_extension(self) -> None:
        """All-or-nothing: a half-applied plan is not auditable."""
        verdict = self._judge([
            addition("p1", "read_file", {"path": "stats.py"}),
            addition("p2", "write_document", {"doc_id": "d"}),
        ])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.accepted, [])
        self.assertEqual(len(verdict.rejected), 1)
        self.assertEqual(verdict.rejected[0]["id"], "p2")

    def test_judging_changes_nothing_by_itself(self) -> None:
        """I, R. Validation is a read: no plan mutation, no budget, no execution."""
        before = self.plan.to_dict()
        row_before = self.stack.store.budget_row(f"task:{self.task.id}")
        self._judge([addition("p1", "read_file", {"path": "stats.py"})])
        self._judge([addition("p2", "write_document", {"doc_id": "d"})])
        self.assertEqual(self.plan.to_dict(), before)
        self.assertEqual(self.stack.store.budget_row(f"task:{self.task.id}"), row_before)

    def test_odd_input_never_raises(self) -> None:
        """Q. A refusal is a value, not an exception."""
        for odd in (None, {}, "text", 7, [1, 2], [{"id": None}], [[]]):
            with self.subTest(odd=odd):
                additions = odd if isinstance(odd, list) else None
                if odd is None:
                    verdict = validate_additions(
                        None, plan=self.plan, registry=self.stack.registry,
                        permissions=self.permissions, enabled=True,
                    )
                    self.assertFalse(verdict.present)
                    continue
                if not isinstance(odd, list):
                    continue
                verdict = validate_additions(
                    odd, plan=self.plan, registry=self.stack.registry,
                    permissions=self.permissions, enabled=True,
                )
                self.assertFalse(verdict.ok)


class LoopIntegrationTests(unittest.TestCase):
    """The loop: acceptance, refusal, persistence, replay and the gates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = Stack(self._tmp.name)

    def tearDown(self) -> None:
        self.stack.close()
        self._tmp.cleanup()

    def test_an_accepted_extension_is_recorded_and_attributed(self) -> None:
        """B, C. On the chain, as a model proposal the host accepted."""
        rounds = scripted_fix_rounds()[:3]
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=6)
        events = harness.store.events(final.id)
        extensions = [
            e for e in events
            if e["type"] == "plan_steered"
            and (e["payload"].get("detail") or {}).get("action") == "extend"
        ]
        self.assertEqual(len(extensions), 2)
        first = extensions[0]["payload"]["detail"]
        self.assertEqual(first["by"], "model")
        self.assertEqual(first["added"], ["p_list", "p_read", "p_run"])
        # The request itself is on the chain too, from plan_validated.
        validated = [e for e in events if e["type"] == "plan_validated"]
        self.assertIn("additions", validated[0]["payload"]["detail"]["plan"])
        # And the accepted step became eligible: the plan grew, and the run
        # executed the actions the todos named.
        self.assertEqual(first["plan"]["revision"], 2)
        executed = [e["payload"]["detail"]["tool"] for e in events if e["type"] == "tool_started"]
        self.assertEqual(executed[:3], ["ls_workspace", "read_file", "run_command"])

    def test_the_chain_verifies_after_acceptance_and_refusal(self) -> None:
        """N."""
        rounds = [
            (step("ls_workspace", {}), [
                addition("p_list", "ls_workspace"),
                addition("p_bad", "write_document", {"doc_id": "d"}),   # refused → whole set refused
            ]),
            (step("ls_workspace", {}), [addition("p_list", "ls_workspace")]),  # accepted on retry
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, recorder, _, final = self.stack.run(rounds, max_rounds=6)
        kinds = [e["type"] for e in harness.store.events(final.id)]
        self.assertIn("control_rejected", kinds)
        self.assertIn("plan_steered", kinds)
        verify_trace(recorder.build(harness.store.load(final.id)))
        self.assertEqual(final.status, "succeeded")

    def test_a_refusal_is_an_event_the_run_continues_from(self) -> None:
        """K. No poison: the refusal reaches the model, and work goes on."""
        rounds = [
            (step("ls_workspace", {}), [addition("p_bad", "write_document", {"doc_id": "d"})]),
            (step("ls_workspace", {}), [addition("p_list", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "succeeded")
        events = harness.store.events(final.id)
        rejected = [e for e in events if e["type"] == "control_rejected"]
        self.assertEqual(len(rejected), 1)
        detail = rejected[0]["payload"]["detail"]
        self.assertEqual(detail["kind"], "plan_extension")
        self.assertEqual(detail["by"], "model")
        self.assertEqual(detail["rejected"][0]["code"], "TOOL_NOT_REGISTERED")
        # The reason reached the next proposal's context.
        self.assertTrue(
            any("rejected_proposals" in c for c in provider.contexts[1:]),
            "the refusal never reached a later context",
        )
        # And the refused todo never entered the plan.
        self.assertNotIn("p_bad", [t["id"] for t in final.plan_state["todos"]])

    def test_an_accepted_extension_survives_a_restart(self) -> None:
        """D, E. Replay reconstructs the plan and repeats no execution."""
        rounds = scripted_fix_rounds()
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=12)
        self.assertEqual(final.status, "succeeded")
        accepted_ids = [t["id"] for t in final.plan_state["todos"]]
        self.assertIn("p_list", accepted_ids)
        self.assertIn("p_verify", accepted_ids)
        events_before = len(harness.store.events(final.id))
        dispatches_before = len([e for e in harness.store.events(final.id)
                                 if e["type"] == "tool_started"])
        self.stack.close()

        # A new process over the same database.
        reopened = Stack(self._tmp.name, make_ws=False)
        reloaded = reopened.store.load(final.id)
        self.assertEqual(reloaded.status, "succeeded")
        second = PlannedProvider([])
        resume_harness = reopened.harness(second)
        resume_loop = Loop(
            resume_harness,
            planner=GENERAL_DOMAIN.make_planner(reopened.world),
            max_rounds=12,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        memory = resume_loop.restore(reloaded)
        plan = resume_loop.rebuild_plan(reloaded, memory)
        self.assertEqual([t.id for t in plan.todos], accepted_ids)
        self.assertEqual([t.status for t in plan.todos],
                         [t["status"] for t in reloaded.plan_state["todos"]])
        self.assertEqual(second.contexts, [])
        self.assertEqual(len(reopened.store.events(final.id)), events_before)
        self.assertEqual(
            len([e for e in reopened.store.events(final.id) if e["type"] == "tool_started"]),
            dispatches_before,
        )
        reopened.close()
        self.stack = Stack(self._tmp.name, make_ws=False)  # keep tearDown happy

    def test_a_rejected_extension_is_not_in_the_replayed_plan(self) -> None:
        """D. A refusal cannot come back as accepted work."""
        rounds = [
            (step("ls_workspace", {}), [
                addition("p_ok", "ls_workspace"),
                addition("p_bad", "write_document", {"doc_id": "d"}),
            ]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, _, loop, final = self.stack.run(rounds, max_rounds=6)
        self.assertEqual(final.status, "succeeded")
        ids = [t["id"] for t in final.plan_state["todos"]]
        self.assertNotIn("p_ok", ids)
        self.assertNotIn("p_bad", ids)
        opened = [e for e in harness.store.events(final.id) if e["type"] == "plan_steered"]
        self.assertEqual(opened, [])
        # Replay agrees.
        memory = loop.restore(harness.store.load(final.id))
        replayed = loop.rebuild_plan(harness.store.load(final.id), memory)
        self.assertEqual([t.id for t in replayed.todos], ids)

    def test_the_budget_only_charges_model_calls_and_real_reads(self) -> None:
        """I. Proposing does not obtain extra budget."""
        rounds = [
            (step("ls_workspace", {}), [addition("p_list", "ls_workspace")]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=6)
        events = harness.store.events(final.id)
        row = self.stack.store.budget_row(f"task:{final.id}")
        self.assertEqual(row["calls_used"], len([e for e in events if e["type"] == "model_started"]))
        self.assertEqual(row["reads_used"], len([e for e in events if e["type"] == "tool_started"]))
        self.assertEqual(len(provider.contexts), row["calls_used"])

    def test_an_added_step_still_faces_the_gate_at_dispatch(self) -> None:
        """R. Plan-time validation never replaces dispatch-time authorization."""
        rounds = [
            (step("ls_workspace", {}), [addition("p_read", "read_file", {"path": "stats.py"})]),
            (step("read_file", {"path": "stats.py"}), []),
        ]
        provider = PlannedProvider(rounds)
        harness = self.stack.harness(provider)
        task = self.stack.task(harness)
        # The registry the gate will read from round two onward no longer
        # offers the tool the plan just accepted a step for.
        narrowed = ToolRegistry([
            spec for spec in GENERAL_DOMAIN.build_tools(self.stack.world)
            if spec.name != "read_file"
        ])
        control = RegistrySwapControl(harness, narrowed, after_round=1)
        final = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(self.stack.world),
            max_rounds=4,
            control=control,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        self.assertTrue(control.swapped, "the capability was never withdrawn")
        events = harness.store.events(final.id)
        denied = [e for e in events if e["type"] == "tool_denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["payload"]["detail"]["code"], "TOOL_NOT_REGISTERED")
        self.assertEqual(final.status, "denied")
        # The todo had been accepted the round before — acceptance was a plan
        # decision only, and the read never ran.
        self.assertIn("p_read", [t["id"] for t in harness.store.load(final.id).plan_state["todos"]])
        self.assertEqual(
            [e["payload"]["detail"]["tool"] for e in events if e["type"] == "tool_started"],
            ["ls_workspace"],
        )

    def test_an_added_step_cannot_make_the_run_succeed_by_itself(self) -> None:
        """J, P. The verifier still decides, and an unsettled step blocks."""
        rounds = [
            (step("ls_workspace", {}), [addition("p_claim", "ls_workspace")]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=4)
        # p_claim was settled by the ls observation, so the run may finish — but
        # only because the artifact was independently verified first.
        self.assertEqual(final.status, "succeeded")
        self.assertIsNotNone(final.result)
        self.assertEqual(final.result["verification"], "file-state-recomputed")
        kinds = [e["type"] for e in harness.store.events(final.id)]
        self.assertIn("verification_started", kinds)

    def test_an_unsettled_added_step_stops_the_run_honestly(self) -> None:
        """J. A step that never happened is not a finished plan."""
        rounds = [
            (step("ls_workspace", {}), [addition("p_never", "read_file", {"path": "stats.py"})]),
            (step("write_file", {"path": "SUMMARY.md", "content": "# done\n"}), []),
        ]
        provider, harness, _, _, final = self.stack.run(rounds, max_rounds=3)
        self.assertEqual(final.status, "failed")
        self.assertNotEqual(final.status, "succeeded")
        self.assertIsNone(final.result)
        kinds = [e["type"] for e in harness.store.events(final.id)]
        self.assertNotIn("verification_started", kinds)

    def test_operator_steering_still_wins_over_a_model_extension(self) -> None:
        """M. skip/promote keep their meaning and their precedence."""
        # One round only, so the second added todo is still open when the
        # operator acts on it — a settled todo cannot be skipped, by design
        # (``Plan.mark`` refuses to move a done todo backwards).
        rounds = [
            (step("ls_workspace", {}), [
                addition("p_list", "ls_workspace"),
                addition("p_read", "read_file", {"path": "stats.py"}),
            ]),
        ]
        provider, harness, _, loop, final = self.stack.run(rounds, max_rounds=1)
        plan = loop.rebuild_plan(
            harness.store.load(final.id), loop.restore(harness.store.load(final.id)),
        )
        statuses = {t.id: t.status for t in plan.todos}
        self.assertEqual(statuses["p_list"], "done")
        self.assertEqual(statuses["p_read"], "pending")

        # The operator skips a todo the model added — the same command the console
        # sends, building the revision the same way it does (the operator path
        # bumps the revision explicitly; ``Plan.mark`` alone does not).
        marked = plan.mark("p_read", "abandoned", reason="operator skipped this step")
        skipped = Plan(goal=marked.goal, todos=marked.todos, revision=plan.revision + 1)
        task = harness.record(harness.store.load(final.id), "plan_steered", {
            "round": 9, "action": "skip", "todo_id": "p_read", "note": "operator skipped this step",
            "plan": skipped.to_dict(), "by": "operator",
        })
        replayed = loop.rebuild_plan(task, loop.restore(task))
        after = {t.id: t.status for t in replayed.todos}
        self.assertEqual(after["p_read"], "abandoned")
        self.assertEqual(replayed.revision, plan.revision + 1)

    def test_an_unknown_execution_result_is_still_not_retried(self) -> None:
        """O. Plan extension does not reopen the unknown-result contract."""

        class ExplodingRunner:
            """Reports an unknown outcome, exactly as a killed deadline does."""

            def run(self, command, args, timeout_seconds=None):
                raise AgentError("COMMAND_TIMEOUT", "deadline", details={"outcome": "unknown"})

        with tempfile.TemporaryDirectory() as tmp:
            stack = Stack(tmp, command_runner=ExplodingRunner())
            rounds = [
                (step("ls_workspace", {}), [addition("p_run", "run_command",
                                                     {"command": "python3", "args": ["x.py"]})]),
                (step("run_command", {"command": "python3", "args": ["x.py"]}), []),
            ]
            provider, harness, _, _, final = stack.run(rounds, max_rounds=4)
            events = harness.store.events(final.id)
            self.assertEqual(final.status, "failed")
            self.assertEqual(final.failure["code"], "TOOL_OUTCOME_UNKNOWN")
            starts = [e for e in events if e["type"] == "tool_started"
                      and e["payload"]["detail"]["tool"] == "run_command"]
            self.assertEqual(len(starts), 1, "an unknown result was dispatched twice")
            stack.close()


class BlueprintCompatibilityTests(unittest.TestCase):
    """L. The blueprints and their verification contract are untouched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_script_mode_still_walks_its_blueprint(self) -> None:
        stack = Stack(self._tmp.name, spec={"mode": "script", "scriptPath": "s.py"})
        draft = ("def total(xs):\n    return sum(xs) - 1\n\n"
                 "print('total:', total([1, 2, 3]))\n")
        rounds = [
            (step("write_file", {"path": "s.py", "content": draft}), []),
            (step("run_command", {"command": "python3", "args": ["s.py"]}), []),
            (step("edit_file", {"path": "s.py", "old_text": "    return sum(xs) - 1",
                                "new_text": "    return sum(xs)"}), []),
            (step("run_command", {"command": "python3", "args": ["s.py"]}), []),
            (step("write_file", {"path": "results.md", "content": "# results\n"}), []),
        ]
        provider, harness, recorder, _, final = stack.run(rounds, max_rounds=8)
        self.assertEqual(final.status, "succeeded", final.failure)
        verify_trace(recorder.build(harness.store.load(final.id)))
        # The blueprint's own composition still filed both runs into the plan's
        # results todo — the proposer's own content is what landed on disk, which
        # is why the assertion is about the plan rather than the file.
        results_todo = next(t for t in final.plan_state["todos"] if t["tool"] == "write_file"
                            and t["arguments"].get("path") == "results.md")
        composed = results_todo["arguments"]["content"]
        self.assertIn("首次运行（修复前）", composed)
        self.assertIn("第 2 次运行", composed)
        # Both runs' real output, and the repair the model proposed.
        self.assertIn("total: 5", composed)
        self.assertIn("total: 6", composed)
        self.assertIn("return sum(xs) - 1` → `return sum(xs)", composed)
        stack.close()

    def test_a_blueprint_domain_refuses_a_model_extension(self) -> None:
        """The closed plan is a host decision, and it says so."""
        stack = Stack(self._tmp.name, spec={"mode": "script", "scriptPath": "s.py"})
        harness = stack.harness(PlannedProvider([]))
        # The script blueprint's domain-level flag is what a run carries; here we
        # assert the *validator* refuses an extension when the domain is closed.
        harness.plan_extension = False
        task = stack.task(harness)
        verdict = validate_additions(
            [addition("p1", "read_file", {"path": "stats.py"})],
            plan=Plan(goal="g"), registry=stack.registry,
            permissions=harness.permissions(task), enabled=harness.plan_extension,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.rejected[0]["code"], "PLAN_EXTENSION_DISABLED")
        stack.close()

    def test_open_mode_is_recognised_and_its_artifact_is_the_hosts_choice(self) -> None:
        # An operator-named artifact wins in every mode.
        for mode in ("open", "script", "research"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    world = GENERAL_DOMAIN.create_world(
                        spec={"mode": mode, "reportPath": "SUMMARY.md"},
                        sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
                    )
                    self.assertEqual(world.mode, mode)
                    self.assertEqual(world.report_path, "SUMMARY.md")
        # Without a name each mode keeps its own default.
        for mode, expected in (("open", "report.md"), ("script", "results.md"),
                               ("research", "research.md"), ("nonsense", "research.md")):
            with self.subTest(mode=mode, default=expected):
                with tempfile.TemporaryDirectory() as tmp:
                    world = GENERAL_DOMAIN.create_world(
                        spec={"mode": mode},
                        sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
                    )
                    self.assertEqual(world.report_path, expected)
        # Open mode's plan is exactly the artifact obligation — no blueprint.
        with tempfile.TemporaryDirectory() as tmp:
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
            )
            plan = GENERAL_DOMAIN.make_planner(world).decompose("goal")
            self.assertEqual([t.id for t in plan.todos], ["out"])
            self.assertEqual(plan.todos[0].tool, "write_file")
            self.assertTrue(plan.todos[0].required)
            self.assertTrue(GENERAL_DOMAIN.plan_extension)
        # An unknown mode is research, and research still demands its sources.
        with tempfile.TemporaryDirectory() as tmp:
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "nonsense"}, sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
            )
            with self.assertRaises(ValueError):
                GENERAL_DOMAIN.make_planner(world).decompose("goal")


class EndToEndTests(unittest.TestCase):
    """D1, D2. The tasks the audit used to show the gap, through the real Loop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = Stack(self._tmp.name)

    def tearDown(self) -> None:
        self.stack.close()
        self._tmp.cleanup()

    def _tests_pass(self) -> bool:
        done = subprocess.run(
            [sys.executable, "test_stats.py"], cwd=self.stack.ws,
            capture_output=True, text=True, timeout=60,
        )
        return done.returncode == 0

    def test_e1_fix_a_failing_test_through_the_real_loop(self) -> None:
        """D1. The artifact and the test result are inspected independently."""
        self.assertFalse(self._tests_pass(), "the fixture must start broken")
        provider, harness, recorder, _, final = self.stack.run(scripted_fix_rounds(), max_rounds=12)
        self.assertEqual(final.status, "succeeded", final.failure)
        events = harness.store.events(final.id)
        # Every action was authorized by the host gate...
        self.assertEqual([e for e in events if e["type"] == "tool_denied"], [])
        # ...the plan really was extended by the model...
        self.assertGreaterEqual(
            len([e for e in events if e["type"] == "plan_steered"
                 and e["payload"]["detail"].get("by") == "model"]), 2)
        # ...the failing-then-passing runs are on the chain with their codes...
        codes = [e["payload"]["detail"]["result"].get("exit_code") for e in events
                 if e["type"] == "tool_succeeded"
                 and e["payload"]["detail"]["tool"] == "run_command"]
        self.assertEqual(codes, [1, 0])
        # ...and the runtime verified the summary it accepted.
        self.assertEqual(final.result["verification"], "file-state-recomputed")
        self.assertEqual(final.result["path"], "SUMMARY.md")
        verify_trace(recorder.build(harness.store.load(final.id)))
        # Independent inspection of the workspace, not the model's word.
        self.assertIn("sum(values) / len(values)", (self.stack.ws / "stats.py").read_text(encoding="utf-8"))
        self.assertTrue(self._tests_pass(), "the test still fails on disk")

    def test_e2_understand_an_unfamiliar_repository(self) -> None:
        """D2. The reading list is the model's, the artifact is the host's."""
        rounds = [
            (step("ls_workspace", {}), [
                addition("q_list", "ls_workspace"),
                addition("q_read", "read_file", {"path": "stats.py"}),
                addition("q_test", "read_file", {"path": "test_stats.py"}),
                addition("q_doc", "read_file", {"path": "README.md"}),
            ]),
            (step("read_file", {"path": "stats.py"}), []),
            (step("read_file", {"path": "test_stats.py"}), []),
            (step("read_file", {"path": "README.md"}), []),
            (step("write_file", {"path": "SUMMARY.md",
                                 "content": "# Repo overview\n\n"
                                            "- `stats.py`: `mean()` helper, used by the payroll job\n"
                                            "- `test_stats.py`: the unittest entry point\n"
                                            "- `README.md`: how to run the tests\n"}), []),
        ]
        provider, harness, recorder, _, final = self.stack.run(rounds, max_rounds=12)
        self.assertEqual(final.status, "succeeded", final.failure)
        ids = [t["id"] for t in final.plan_state["todos"]]
        for expected in ("out", "q_list", "q_read", "q_test", "q_doc"):
            self.assertIn(expected, ids)
        events = harness.store.events(final.id)
        reads = [e["payload"]["detail"]["result"]["path"] for e in events
                 if e["type"] == "tool_succeeded" and e["payload"]["detail"]["tool"] == "read_file"]
        self.assertEqual(reads, ["stats.py", "test_stats.py", "README.md"])
        summary = (self.stack.ws / "SUMMARY.md").read_text(encoding="utf-8")
        for name in ("stats.py", "test_stats.py", "README.md"):
            self.assertIn(name, summary)
        verify_trace(recorder.build(harness.store.load(final.id)))
        self.assertEqual(final.result["verification"], "file-state-recomputed")


if __name__ == "__main__":
    unittest.main()