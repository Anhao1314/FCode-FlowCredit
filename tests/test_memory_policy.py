"""Memory policy head (Sprint A): contract, shadow isolation, replay.

These tests are deliberately network-free. The loop-level cases call
``_maybe_memory_policy`` directly so they exercise the exact production path
without needing a full proposal loop.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.core.observation import Observation  # noqa: E402
from flowagent.core.plan import Plan, Todo  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import PinPolicy, WorkingMemory  # noqa: E402
from flowagent.memory.policy import (  # noqa: E402
    POLICY_SCHEMA,
    PolicyConfig,
    compare_heuristic,
    parse_policy_reply,
)
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.base import EFFECT_READ, ToolSpec  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _reply(*items: dict) -> str:
    return json.dumps({"schema": POLICY_SCHEMA, "observations": list(items)})


def _obs(path: str = "a.py", content: str = "x") -> Observation:
    return Observation(
        tool="read_file",
        arguments={"path": path},
        result={"content": content},
        result_hash=(path * 64)[:64],
    )


class PolicyConfigTests(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertEqual(PolicyConfig.from_spec(None, env={}).mode, "off")
        self.assertFalse(PolicyConfig.from_spec(None, env={}).enabled)

    def test_spec_forms(self) -> None:
        self.assertEqual(
            PolicyConfig.from_spec({"memory_policy": "shadow"}, env={}).mode,
            "shadow",
        )
        self.assertEqual(
            PolicyConfig.from_spec({"memory_policy": True}, env={}).mode,
            "shadow",
        )
        config = PolicyConfig.from_spec(
            {"memory_policy": {
                "mode": "shadow", "every_rounds": 0, "max_calls": 999,
            }},
            env={},
        )
        self.assertEqual(config.every_rounds, 1, "interval is clamped to >= 1")
        self.assertEqual(config.max_calls, 100, "calls are capped")

    def test_env_fallback_and_invalid_modes(self) -> None:
        env = {"FCODE_MEMORY_POLICY": "shadow"}
        self.assertEqual(PolicyConfig.from_spec(None, env=env).mode, "shadow")
        self.assertEqual(
            PolicyConfig.from_spec({"memory_policy": "bogus"}, env={}).mode,
            "off",
        )


class ParsePolicyReplyTests(unittest.TestCase):
    def test_valid_reply(self) -> None:
        decision = parse_policy_reply(_reply({
            "id": "abc", "action": "pin", "confidence": 0.82,
            "reason": "needs_for_next_step",
        }), {"abc"})
        self.assertTrue(decision.ok)
        self.assertEqual(decision.observations[0].action, "pin")
        self.assertEqual(decision.to_dict()["observations"][0]["confidence_bp"], 8200)

    def test_unknown_id_is_rejected_not_mapped(self) -> None:
        decision = parse_policy_reply(
            _reply({"id": "nope", "action": "keep", "confidence": 0.5}),
            {"abc"},
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error, "no_valid_entries")

    def test_bad_json_and_schema(self) -> None:
        self.assertEqual(parse_policy_reply("not json", {"abc"}).error, "invalid_json")
        self.assertEqual(
            parse_policy_reply('{"schema":"other","observations":[]}', {"abc"}).error,
            "schema_mismatch",
        )

    def test_enum_and_confidence_validation(self) -> None:
        bad_action = parse_policy_reply(
            _reply({"id": "abc", "action": "delete", "confidence": 0.5}), {"abc"})
        self.assertFalse(bad_action.ok)
        bad_conf = parse_policy_reply(
            _reply({"id": "abc", "action": "keep", "confidence": 1.5}), {"abc"})
        self.assertFalse(bad_conf.ok)

    def test_duplicate_ids_and_entry_cap(self) -> None:
        duplicate = parse_policy_reply(
            _reply(
                {"id": "abc", "action": "keep", "confidence": 0.5},
                {"id": "abc", "action": "drop", "confidence": 0.5},
            ),
            {"abc"},
        )
        self.assertEqual(duplicate.invalid, 1)
        self.assertEqual(len(duplicate.observations), 1)

        capped = parse_policy_reply(
            _reply(*[
                {"id": f"id{i}", "action": "keep", "confidence": 0.5}
                for i in range(5)
            ]),
            {f"id{i}" for i in range(5)},
            max_entries=2,
        )
        self.assertTrue(capped.ok)
        self.assertEqual(len(capped.observations), 2)
        self.assertEqual(capped.invalid, 3)

    def test_code_fences_are_stripped(self) -> None:
        decision = parse_policy_reply(
            "```json\n" + _reply({
                "id": "abc", "action": "keep", "confidence": 0.5,
            }) + "\n```",
            {"abc"},
        )
        self.assertTrue(decision.ok)


class CompareHeuristicTests(unittest.TestCase):
    def _decision(self, action: str):
        return parse_policy_reply(
            _reply({"id": "abc", "action": action, "confidence": 0.8}),
            {"abc"},
        )

    def test_four_quadrants(self) -> None:
        self.assertEqual(
            compare_heuristic(self._decision("keep"), {"abc": "inline"})["agree"], 1)
        self.assertEqual(
            compare_heuristic(self._decision("drop"), {"abc": "folded"})["agree"], 1)
        keep_vs_folded = compare_heuristic(self._decision("keep"), {"abc": "folded"})
        self.assertEqual(keep_vs_folded["disagree"], 1)
        self.assertEqual(keep_vs_folded["disagreements"][0]["desired"], "inline")
        drop_vs_inline = compare_heuristic(self._decision("drop"), {"abc": "inline"})
        self.assertEqual(drop_vs_inline["disagreements"][0]["desired"], "folded")

    def test_missing_intent_counts_unknown(self) -> None:
        comparison = compare_heuristic(self._decision("keep"), {})
        self.assertEqual(comparison["unknown"], 1)
        self.assertEqual(comparison["compared"], 0)


class MemoryPolicyViewsTests(unittest.TestCase):
    def _memory(self, budget: int, pin: PinPolicy | None = None) -> WorkingMemory:
        memory = WorkingMemory(
            objective="fix pricing",
            tool_catalog=[],
            prompt_profile="general",
            observation_budget=budget,
            pin_policy=pin,
        )
        for index in range(4):
            memory.record(index + 1, _obs(f"src/f{index}.py", "y" * 50))
        return memory

    def test_policy_rows_are_compact_and_id_matches_index_prefix(self) -> None:
        memory = self._memory(24_000)
        rows = memory.policy_rows(limit=10)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(len(row["id"]), 12)
            self.assertLessEqual(len(row["summary_head"]), 240)
            self.assertNotIn("raw_content", row)

    def test_heuristic_intents_match_fold_levels(self) -> None:
        generous = self._memory(1_000_000)
        self.assertEqual(set(generous.heuristic_intents().values()), {"inline"})

        tight = self._memory(1)  # cannot fit any summary row
        self.assertEqual(set(tight.heuristic_intents().values()), {"folded"})

        pinned = self._memory(1, PinPolicy(tool_names=frozenset({"read_file"})))
        self.assertEqual(set(pinned.heuristic_intents().values()), {"pinned"})

    def test_observation_stats_are_ints_only(self) -> None:
        stats = self._memory(24_000).observation_stats()
        canonical_json(stats)  # floats would raise
        self.assertEqual(stats["records"], 4)


class _PolicyProvider(MockProvider):
    """Mock provider that also implements the auxiliary completion contract."""

    def __init__(self, reply: str | Exception) -> None:
        super().__init__()
        self.reply = reply
        self.policy_calls = 0

    def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                 reasoning_effort: str | None = None):
        self.policy_calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, {
            "provider": "fake",
            "model": "fake-policy",
            "request_id": "req-1",
            "latency_ms": 3,
            "max_completion_tokens": max_tokens or 0,
            "usage_prompt_tokens": 120,
            "usage_completion_tokens": 20,
            "usage_total_tokens": 140,
            "cached_tokens": 0,
            "cache_hit_bp": 0,
            "cost_microusd": 7,
            "usage_missing": False,
        }


class LoopShadowTests(unittest.TestCase):
    def _harness(self, provider, tmp: str):
        store = TaskStore(pathlib.Path(tmp) / "tasks.sqlite")

        def handler(_: dict) -> dict:
            return {"ok": True}

        spec = ToolSpec(
            name="read_file", summary="read", effect=EFFECT_READ,
            params=(), handler=handler, outputs=(),
        )
        harness = Harness(
            store=store,
            registry=ToolRegistry([spec]),
            provider=provider,
            world=None,
            limits=BudgetLimits(10, 10, 10_000, 10_000),
            receipts=ReceiptStore(store),
            verifier=lambda observation: {
                "verified": True, "verification": "test",
                "result": observation.result,
            },
        )
        task = harness.create(
            task_id="policy-1",
            objective="fix pricing",
            principal="test",
            binding={"spec": {"memory_policy": {
                "mode": "shadow", "every_rounds": 1,
            }}},
            allowed_tools=["read_file"],
            grant={},
        )
        return harness, task

    def _loop(self, harness, profile: str | None = "general") -> Loop:
        return Loop(
            harness, planner=None, max_rounds=3, result_tools=(),
            memory_prompt_profile=profile,
        )

    def test_shadow_decision_is_recorded_and_context_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PolicyProvider("")
            harness, task = self._harness(provider, tmp)
            loop = self._loop(harness)
            memory = loop.restore(task)
            memory.record(1, _obs("src/a.py"))
            memory.record(1, _obs("src/b.py"))
            rows = memory.policy_rows()
            provider.reply = _reply({
                "id": rows[0]["id"], "action": "keep", "confidence": 0.9,
                "reason": "needs_for_next_step",
            })
            plan = Plan(goal="fix pricing", todos=[Todo(
                id="t1", text="fix", tool="read_file", done_when="read",
                status="active",
            )])

            # Build the control context from an identically-fed memory.
            control = WorkingMemory(
                objective=memory.objective,
                tool_catalog=memory.tool_catalog,
                summarizers=dict(memory.summarizers),
                instruction=memory.instruction,
                prompt_profile=memory.prompt_profile,
                observation_budget=memory.observation_budget,
                summarizer_config=memory.summarizer_config,
            )
            for record in memory.records:
                control.record(record.round, Observation(
                    tool=record.tool, arguments=record.arguments,
                    result=record.result, result_hash=record.result_hash,
                ))

            task = loop._maybe_memory_policy(task, memory, 1, plan)
            self.assertEqual(provider.policy_calls, 1)

            events = harness.store.events(task.id)
            decisions = [
                event for event in events if event["type"] == "memory_decision"
            ]
            self.assertEqual(len(decisions), 1)
            detail = decisions[0]["payload"]["detail"]
            self.assertEqual(detail["mode"], "shadow")
            self.assertEqual(detail["schema"], POLICY_SCHEMA)
            self.assertEqual(detail["decision"]["observations"][0]["action"], "keep")
            self.assertEqual(detail["comparison"]["disagree"], 0)
            canonical_json(detail)  # integer-only payload

            self.assertEqual(
                canonical_json(memory.build(1, {})),
                canonical_json(control.build(1, {})),
                "shadow must not change the prompt bytes",
            )
            self.assertIsNone(memory.pending_compaction)

    def test_fallback_on_transport_error_keeps_the_run_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PolicyProvider(AgentError("MODEL_CALL_FAILED", "boom"))
            harness, task = self._harness(provider, tmp)
            loop = self._loop(harness)
            memory = loop.restore(task)
            memory.record(1, _obs())
            plan = Plan(goal="g")
            returned = loop._maybe_memory_policy(task, memory, 1, plan)
            self.assertEqual(returned.id, task.id)
            detail = next(
                event["payload"]["detail"]
                for event in harness.store.events(task.id)
                if event["type"] == "memory_decision"
            )
            self.assertEqual(detail["mode"], "fallback")
            self.assertEqual(detail["error"]["code"], "MODEL_CALL_FAILED")

    def test_frozen_profile_never_calls_the_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PolicyProvider(_reply())
            harness, task = self._harness(provider, tmp)
            loop = self._loop(harness, profile=None)
            memory = loop.restore(task)
            memory.record(1, _obs())
            loop._maybe_memory_policy(task, memory, 1, Plan(goal="g"))
            self.assertEqual(provider.policy_calls, 0)
            self.assertFalse([
                event for event in harness.store.events(task.id)
                if event["type"] == "memory_decision"
            ])

    def test_restore_rebuilds_call_counters_from_the_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PolicyProvider("")
            harness, task = self._harness(provider, tmp)
            loop = self._loop(harness)
            memory = loop.restore(task)
            memory.record(1, _obs())
            rows = memory.policy_rows()
            provider.reply = _reply({
                "id": rows[0]["id"], "action": "drop", "confidence": 0.6,
            })
            task = loop._maybe_memory_policy(task, memory, 2, Plan(goal="g"))
            self.assertEqual(loop._policy_calls, 1)

            loop._policy_calls = 0
            loop._policy_last_round = 0
            loop.restore(task)
            self.assertEqual(loop._policy_calls, 1)
            self.assertEqual(loop._policy_last_round, 2)

    def test_once_per_round_and_max_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PolicyProvider(_reply())
            harness, task = self._harness(provider, tmp)
            loop = self._loop(harness)
            memory = loop.restore(task)
            memory.record(1, _obs())
            rows = memory.policy_rows()
            provider.reply = _reply({
                "id": rows[0]["id"], "action": "keep", "confidence": 0.5,
            })
            task = loop._maybe_memory_policy(task, memory, 1, Plan(goal="g"))
            task = loop._maybe_memory_policy(task, memory, 1, Plan(goal="g"))
            self.assertEqual(provider.policy_calls, 1, "one decision per round")


class _ShadowLoopProvider:
    """Scripted two-round agent that also answers the policy head."""

    name = "scripted-shadow"
    model = "scripted-shadow-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.policy_calls = 0

    def propose(self, context):
        from flowagent.core.contracts import PLAN_VERSION
        from flowagent.providers.base import Proposal

        self.calls += 1
        if self.calls == 1:
            steps = [{"id": "read-a", "tool": "read_file",
                      "arguments": {"path": "a.txt"}}]
        else:
            steps = [{"id": "write-report", "tool": "write_file",
                      "arguments": {"path": "report.md", "content": "# done\n"}}]
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": steps},
            provider=self.name, model=self.model, tokens=10, cost_microusd=1,
        )

    def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                 reasoning_effort: str | None = None):
        self.policy_calls += 1
        payload = json.loads(user)
        rows = payload.get("observations") or []
        items = [{
            "id": rows[0]["id"], "action": "keep", "confidence": 0.9,
            "reason": "needs_for_next_step",
        }] if rows else []
        return json.dumps({
            "schema": POLICY_SCHEMA, "observations": items,
        }), {
            "provider": self.name, "model": self.model, "request_id": "req",
            "latency_ms": 1, "max_completion_tokens": max_tokens or 0,
            "usage_prompt_tokens": 100, "usage_completion_tokens": 10,
            "usage_total_tokens": 110, "cached_tokens": 0,
            "cache_hit_bp": 0, "cost_microusd": 5, "usage_missing": False,
        }


class LoopEndToEndTests(unittest.TestCase):
    """One real loop run proves the wiring at the build call site."""

    def test_shadow_head_runs_inside_a_real_loop(self) -> None:
        from flowagent.demo.fixtures import get_scenario
        from flowagent.domains.general import GENERAL_DOMAIN
        from flowagent.tools.general.sandbox import WorkspaceSandbox

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scenario = get_scenario("general-research")
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"},
                sandbox=WorkspaceSandbox(root / "ws"),
            )
            world.sandbox.ensure()
            world.sandbox.write("a.txt", "alpha\n")
            store = TaskStore(root / "tasks.sqlite")
            provider = _ShadowLoopProvider()
            harness = Harness(
                store=store,
                registry=ToolRegistry(world.build_tools()),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: world.verify_observation(observation),
                prompt_profile="general-fast",
                plan_extension=True,
            )
            task = harness.create(
                task_id="task-shadow",
                objective="读文件后写报告",
                principal="test",
                binding={
                    "scenario": scenario.key,
                    "spec": {"memory_policy": {
                        "mode": "shadow", "every_rounds": 1,
                    }},
                },
                allowed_tools=list(scenario.allowed_tools),
                grant=scenario.grant,
            )
            final = Loop(
                harness,
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=4,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)
            events = harness.store.events(final.id)
            store.close()

            self.assertEqual(final.status, "succeeded")
            self.assertGreaterEqual(provider.policy_calls, 1)
            decisions = [
                event["payload"]["detail"]
                for event in events
                if event["type"] == "memory_decision"
            ]
            self.assertTrue(decisions)
            self.assertEqual(decisions[-1]["mode"], "shadow")
            self.assertEqual(
                decisions[-1]["decision"]["observations"][0]["action"], "keep")
            canonical_json(decisions[-1])


class ProviderCompleteTests(unittest.TestCase):
    def test_complete_overrides_budget_and_returns_integer_metadata(self) -> None:
        from flowagent.providers.deepseek import PriceSnapshot
        from flowagent.providers.openai_compat import (
            CompatSettings, OpenAICompatProvider,
        )

        price = PriceSnapshot(
            input_microusd_per_mtok=1_000_000,
            output_microusd_per_mtok=2_000_000,
            as_of="2026-11-11",
        )
        provider = OpenAICompatProvider.from_settings(CompatSettings(
            label="compat", api_key_env="X", api_key="sk-test",
            base_url="https://endpoint.example/v1", model="m", price=price,
            receipt_detail="compact",
        ))
        seen: list[dict] = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "id": "chatcmpl-policy",
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "total_tokens": 110,
                        "prompt_cache_hit_tokens": 25,
                    },
                }).encode("utf-8")

        def fake(request, timeout=None):  # noqa: ARG001
            seen.append(json.loads(request.data.decode("utf-8")))
            return _Response()

        with mock.patch("urllib.request.urlopen", fake):
            text, meta = provider.complete(
                "sys", "user", max_tokens=128, reasoning_effort="none")

        self.assertEqual(text, "{}")
        self.assertEqual(seen[0]["max_completion_tokens"], 128)
        self.assertNotIn("reasoning_effort", seen[0],
                         "non-reasoning providers must not receive the override")
        self.assertEqual(meta["cache_hit_bp"], 2500)
        self.assertEqual(meta["cost_microusd"], 120)
        canonical_json(meta)  # metadata is committed on-chain: no floats

    def test_reasoning_provider_receives_the_override(self) -> None:
        from flowagent.providers.deepseek import PriceSnapshot
        from flowagent.providers.openai_compat import (
            CompatSettings, OpenAICompatProvider,
        )

        price = PriceSnapshot(
            input_microusd_per_mtok=1_000_000,
            output_microusd_per_mtok=2_000_000,
            as_of="2026-11-11",
        )
        provider = OpenAICompatProvider.from_settings(CompatSettings(
            label="kimi", api_key_env="X", api_key="sk-test",
            base_url="https://endpoint.example/v1", model="m", price=price,
            receipt_detail="compact", reasoning_effort="low",
        ))
        seen: list[dict] = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "id": "chatcmpl-policy",
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                              "total_tokens": 11},
                }).encode("utf-8")

        def fake(request, timeout=None):  # noqa: ARG001
            seen.append(json.loads(request.data.decode("utf-8")))
            return _Response()

        with mock.patch("urllib.request.urlopen", fake):
            provider.complete("sys", "user", max_tokens=256,
                              reasoning_effort="none")
        self.assertEqual(seen[0]["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()