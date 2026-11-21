"""Slice P1：主代理规划子代理——契约、审批门、按计划派发。

全离线：bots 用桩（start_run/get_run），planner 用桩或 fake provider；
验证的是治理语义，不是审计管线本身。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from gov_plans import (  # noqa: E402
    PLAN_STEP_LIMIT,
    GovPlanService,
    PLANNER_SYSTEM,
    _validate_steps,
    verify_plan_events,
)


class FakeAudit:
    """audit 服务的桩：bundle/chain 可注入失败。"""

    def __init__(self, *, chain_broken=False):
        self.chain_broken = chain_broken

    def bundle(self, audit_id):
        return {"audit_id": audit_id, "findings": []}

    def chain(self, audit_id):
        return {"chain_intact": not self.chain_broken, "count": 3}


class FakeBots:
    """Records dispatches; runs finish after two polls (or fail on demand)."""

    def __init__(self, *, fail_for=(), audit=None):
        self.audit = audit or FakeAudit()
        self.started: list[tuple[str, str]] = []
        self.runs: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.counter = 0
        self.fail_for = set(fail_for)

    def start_run(self, bot_id, target, *, trigger="manual",
                  require_approval=False, source="team"):
        self.counter += 1
        run_id = f"bot-{self.counter}"
        self.runs[run_id] = {
            "run_id": run_id, "bot_id": bot_id, "target": target,
            "status": "queued", "findings": 3, "evidence": 2,
            "audit_id": f"audit-{self.counter}", "chain_intact": True,
            "error": "",
        }
        self.started.append((bot_id, target))
        return dict(self.runs[run_id])

    def get_run(self, run_id):
        run = self.runs[run_id]
        self.polls[run_id] = self.polls.get(run_id, 0) + 1
        if run["status"] == "queued" and self.polls[run_id] >= 2:
            run["status"] = "failed" if run["bot_id"] in self.fail_for else "done"
            # 真实 GovBotService.get_run 把 triage_json 投影成 dict triage
            if run["status"] == "done" and run["bot_id"] == "analyst":
                run["triage"] = {"priorities": []}
        return dict(run)


def wait_terminal(service, plan_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plan = service.get(plan_id)
        if plan and plan["status"] in ("completed", "failed", "rejected"):
            return plan
        time.sleep(0.02)
    return service.get(plan_id)


def stub_planner(steps):
    return lambda _objective, _target: steps


AUDIT_STEPS = [
    {"agent_type": "auditor", "objective": "审计目标", "target": "",
     "deps": [], "done_when": "audit bundle 生成"},
    {"agent_type": "verifier", "objective": "复核签名链", "deps": [1],
     "done_when": "链验证通过"},
    {"agent_type": "analyst", "objective": "给出跟进优先级", "deps": [1],
     "done_when": "优先级列表产出"},
]


class ValidationTests(unittest.TestCase):
    def test_unknown_agent_type_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _validate_steps([{"agent_type": "coder", "objective": "x"}],
                            plan_target="")
        self.assertIn("不在注册表里", str(caught.exception))

    def test_auditor_needs_an_explicit_target(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _validate_steps(
                [{"agent_type": "auditor", "objective": "x", "target": ""}],
                plan_target="")
        self.assertIn("需要 target", str(caught.exception))
        normalized = _validate_steps(
            [{"agent_type": "auditor", "objective": "x", "target": ""}],
            plan_target="/tmp/target")
        self.assertEqual(normalized[0]["target"], "")

    def test_deps_must_reference_earlier_steps(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _validate_steps([
                {"agent_type": "verifier", "objective": "x", "deps": [2]},
                {"agent_type": "auditor", "objective": "y", "target": "/t"},
            ], plan_target="")
        self.assertIn("更早的步骤", str(caught.exception))
        normalized = _validate_steps(
            [{"agent_type": "auditor", "objective": "x", "target": "/t"},
             {"agent_type": "verifier", "objective": "y", "deps": [1, 1]}],
            plan_target="")
        self.assertEqual(normalized[1]["deps"], "1")

    def test_step_limit_is_bounded(self) -> None:
        steps = [{"agent_type": "analyst", "objective": f"s{i}"} for i in range(7)]
        with self.assertRaises(ValueError) as caught:
            _validate_steps(steps, plan_target="")
        self.assertIn(f"最多 {PLAN_STEP_LIMIT} 步", str(caught.exception))


class PlanLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bots = FakeBots()
        self.service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "governance.sqlite",
            planner=stub_planner(AUDIT_STEPS), poll_interval=0.01,
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.tmp.cleanup()

    def test_propose_records_a_durable_plan(self) -> None:
        result = self.service.propose("审计 demo-target", target="/tmp/target")
        self.assertTrue(result["ok"])
        plan = self.service.get(result["id"])
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual([step["agent_type"] for step in plan["steps"]],
                         ["auditor", "verifier", "analyst"])
        self.assertEqual(plan["steps"][0]["deps"], [])
        self.assertEqual(plan["step_counts"]["pending"], 3)
        # 没有批准就没有任何派发
        self.assertEqual(self.bots.started, [])

    def test_approve_dispatches_in_dependency_order(self) -> None:
        plan = self.service.propose("审计 demo-target", target="/tmp/target")
        approved = self.service.approve(plan["id"])
        self.assertEqual(approved["status"], "executing")
        final = wait_terminal(self.service, plan["id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual([item[0] for item in self.bots.started],
                         ["auditor", "verifier", "analyst"])
        # verifier/analyst 显式接上游审计产物（audit_id），不靠“最近一次”猜
        self.assertEqual(self.bots.started[1], ("verifier", "audit-1"))
        self.assertEqual(self.bots.started[2], ("analyst", "audit-1"))
        for step in final["steps"]:
            self.assertTrue(step["result"]["verified"], step["result"])
            self.assertTrue(step["result"]["checks"])
        # M2：默认检查集按角色注入
        self.assertEqual(final["steps"][0]["checks"],
                         ["bundle.exists", "chain.intact"])
        self.assertEqual(final["steps"][2]["checks"], ["triage.recorded"])
        for step in final["steps"]:
            self.assertEqual(step["status"], "succeeded")
            self.assertTrue(step["run_id"])
            self.assertEqual(step["result"]["status"], "done")
        self.assertEqual(final["step_counts"]["succeeded"], 3)

    def test_failed_step_skips_the_rest_and_fails_the_plan(self) -> None:
        self.bots.fail_for.add("auditor")
        plan = self.service.propose("审计 demo-target", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual([item[0] for item in self.bots.started], ["auditor"])
        statuses = [step["status"] for step in final["steps"]]
        self.assertEqual(statuses, ["failed", "skipped", "skipped"])

    def test_reject_never_dispatches(self) -> None:
        plan = self.service.propose("审计 demo-target", target="/tmp/target")
        rejected = self.service.reject(plan["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.bots.started, [])

    def test_planner_failure_is_an_explicit_error_not_a_plan(self) -> None:
        def broken(_objective, _target):
            raise ValueError("规划器输出不是合法 JSON")

        service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "g2.sqlite",
            planner=broken, poll_interval=0.01,
        )
        result = service.propose("审计 demo-target", target="/tmp/target")
        self.assertFalse(result["ok"])
        self.assertIn("不是合法 JSON", result["error"])
        self.assertEqual(service.plans(), [])


class ModelPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bots = FakeBots()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_model_planner_is_parsed_and_meta_recorded(self) -> None:
        class FakeProvider:
            model = "glm-test"

            def complete(self, system, user, *, max_tokens, json_mode):
                assert "审计员" in system or "auditor" in system
                assert "demo-target" in user
                return (json.dumps({"steps": [
                    {"agent_type": "auditor", "objective": "审计",
                     "deps": [], "done_when": "bundle"},
                ]}), {"total_tokens": 321, "cost_microusd": 55})

        service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "governance.sqlite",
            provider_factory=lambda: FakeProvider(), poll_interval=0.01,
        )
        result = service.propose("审计 demo-target", target="/tmp/demo-target")
        self.assertTrue(result["ok"], result.get("error"))
        plan = service.get(result["id"])
        self.assertEqual(plan["planner_model"], "glm-test")
        self.assertEqual(plan["planner_tokens"], 321)
        self.assertEqual(plan["planner_cost_microusd"], 55)
        self.assertEqual([step["agent_type"] for step in plan["steps"]],
                         ["auditor"])

    def test_missing_provider_fails_closed(self) -> None:
        service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "g2.sqlite",
            provider_factory=lambda: None, poll_interval=0.01,
        )
        result = service.propose("审计 demo-target", target="/tmp/target")
        self.assertFalse(result["ok"])
        self.assertIn("规划器不可用", result["error"])

    def test_planner_system_prompt_pins_the_registry_contract(self) -> None:
        self.assertIn("auditor", PLANNER_SYSTEM)
        self.assertIn("不得发明角色", PLANNER_SYSTEM)
        self.assertIn("最多 6 步", PLANNER_SYSTEM)


if __name__ == "__main__":
    unittest.main()


class CheckGateTests(unittest.TestCase):
    """M2：bot 说完成不算——声明检查全过才算 succeeded。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bots = FakeBots()
        self.service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "governance.sqlite",
            planner=stub_planner([
                {"agent_type": "auditor", "objective": "审计目标",
                 "deps": [], "done_when": "bundle"},
                {"agent_type": "verifier", "objective": "复核链",
                 "deps": [1], "done_when": "链通过"},
            ]),
            poll_interval=0.01,
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.tmp.cleanup()

    def test_triage_recorded_accepts_row_and_projection_shapes(self) -> None:
        """bot_runs 行是字符串 triage_json；get_run 投影成 dict triage——
        真模型 smoke 抓到检查只认行形状导致误判失败，两种都必须过。"""
        from gov_checks import run_gov_checks

        ctx = {"audit": self.bots.audit, "bots": self.bots, "result": {}}
        row_shape = run_gov_checks(["triage.recorded"],
                                   {**ctx, "run": {"triage_json": '{"priorities":[]}'}})
        self.assertTrue(row_shape[0]["ok"])
        projection_shape = run_gov_checks(["triage.recorded"],
                                          {**ctx, "run": {"triage": {"priorities": []}}})
        self.assertTrue(projection_shape[0]["ok"])
        missing = run_gov_checks(["triage.recorded"], {**ctx, "run": {}})
        self.assertFalse(missing[0]["ok"])

    def test_unknown_check_name_is_rejected_at_propose(self) -> None:
        result = self.service.propose("审计", target="/tmp/target", steps=[
            {"agent_type": "auditor", "objective": "审计", "deps": [],
             "checks": ["model.says.ok"]},
        ])
        self.assertFalse(result["ok"])
        self.assertIn("不在宿主注册表里", result["error"])
        self.assertEqual(self.service.plans(), [])

    def test_zero_checks_is_not_representable(self) -> None:
        result = self.service.propose("审计", target="/tmp/target", steps=[
            {"agent_type": "auditor", "objective": "审计", "deps": [],
             "checks": []},
        ])
        self.assertFalse(result["ok"])
        self.assertIn("至少一个宿主检查", result["error"])

    def test_broken_chain_fails_the_step_with_check_detail(self) -> None:
        self.bots.audit = FakeAudit(chain_broken=True)
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        self.assertEqual(final["status"], "failed")
        step = final["steps"][0]
        self.assertEqual(step["status"], "failed")
        failed = [c for c in step["result"]["checks"] if not c["ok"]]
        self.assertEqual([c["name"] for c in failed], ["chain.intact"])
        self.assertFalse(step["result"]["verified"])
        self.assertIn("声明检查未通过", final["error"])

    def test_unreachable_bundle_fails_bundle_exists(self) -> None:
        class NoBundleAudit:
            def bundle(self, audit_id):
                return ({"error": "not found"}, 404)

            def chain(self, audit_id):
                return {"chain_intact": True, "count": 0}

        self.bots.audit = NoBundleAudit()
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        self.assertEqual(final["status"], "failed")
        names = [c["name"] for c in final["steps"][0]["result"]["checks"]
                 if not c["ok"]]
        self.assertEqual(names, ["bundle.exists"])

    def test_checks_are_chain_safe_json(self) -> None:
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        for step in final["steps"]:
            for check in step["result"]["checks"]:
                json.dumps(check)  # 不得抛异常（无浮点/不可序列化对象）


class PlanChainTests(unittest.TestCase):
    """M3：计划生命周期哈希链——审批是签名事件，篡改可证伪。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bots = FakeBots()
        self.service = GovPlanService(
            self.bots, pathlib.Path(self.tmp.name) / "governance.sqlite",
            planner=stub_planner(AUDIT_STEPS), poll_interval=0.01,
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.tmp.cleanup()

    def test_full_lifecycle_produces_a_verifiable_chain(self) -> None:
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        self.assertEqual(final["status"], "completed")
        events = final["events"]
        types = [event["type"] for event in events]
        self.assertEqual(types, [
            "plan.proposed", "plan.approved",
            "step.dispatched", "step.result",
            "step.dispatched", "step.result",
            "step.dispatched", "step.result",
            "plan.finalized",
        ])
        ok, reason = verify_plan_events(events)
        self.assertTrue(ok, reason)
        self.assertTrue(final["chain_intact"])
        self.assertEqual(final["chain_count"], len(types))
        # 审批事件钉住所批准的提案哈希
        approved = events[1]["payload"]
        self.assertEqual(approved["proposal_sha256"], events[0]["sha256"])
        self.assertEqual(approved["approved_by"], "operator")

    def test_step_result_events_carry_check_facts(self) -> None:
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        results = [event for event in final["events"]
                   if event["type"] == "step.result"]
        for event in results:
            self.assertTrue(event["payload"]["verified"])
            self.assertTrue(event["payload"]["checks"])

    def test_tampered_plan_event_is_proven_broken(self) -> None:
        plan = self.service.propose("审计", target="/tmp/target")
        final = wait_terminal(self.service, self.service.approve(plan["id"])["id"])
        events = [dict(event) for event in final["events"]]
        events[1]["payload"]["approved_by"] = "nobody"
        ok, reason = verify_plan_events(events)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", reason)

    def test_rejected_plan_has_a_short_chain(self) -> None:
        plan = self.service.propose("审计", target="/tmp/target")
        rejected = self.service.reject(plan["id"])
        types = [event["type"] for event in rejected["events"]]
        self.assertEqual(types, ["plan.proposed", "plan.rejected"])
        ok, reason = verify_plan_events(rejected["events"])
        self.assertTrue(ok, reason)
        self.assertEqual(self.bots.started, [])


if __name__ == "__main__":
    unittest.main()
