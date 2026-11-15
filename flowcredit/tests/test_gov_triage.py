"""受控 triage 的边界契约。

1. 输入是有界事实，findings 截断、不可信数据框架写在 system prompt；
2. 输出只能是优先级提议：引用真实 finding、动作只允许 reaudit，越界整体拒绝；
3. 模型缺位/失败 fail-closed：审计已完成的终态不被改写；
4. shadow 只落库；active 只生成一条待操作员批准的完整复扫。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from gov_bots import GovBotService  # noqa: E402
from gov_triage import (  # noqa: E402
    GovTriage, MAX_FINDINGS, ProviderUnavailable, parse_plan, triage_context,
)

BUNDLE = {
    "audit_id": "demo-2026-11-13-aaaaaa",
    "target": {"identity": "/repo/fixtures/demo-target"},
    "findings": [
        {"finding_id": "F-POC-0001", "category": "AGI-02",
         "severity": "high", "root_cause_group": "approval-memory",
         "poc_refs": ["POC-0001"]},
        {"finding_id": "F-POC-0002", "category": "VIBE-07",
         "severity": "medium", "root_cause_group": "hardcoded-secret",
         "poc_refs": ["POC-0002"]},
    ],
    "poc_evidence_refs": ["POC-0001", "POC-0002"],
    "uncovered_surfaces": [{"surface": "dynamic probing"}],
}

PLAN_TEXT = json.dumps({
    "summary": "先复核 AGI-02 审批记忆，其次补 VIBE-07 的复扫。",
    "confidence": "high",
    "focus_categories": ["AGI-02", "VIBE-07"],
    "priorities": [
        {"finding_id": "F-POC-0001", "action": "reaudit",
         "why": "approval-memory 直接影响治理门控"},
    ],
})


class FakeProvider:
    model = "fake-triage"

    def __init__(self, text=PLAN_TEXT, error=None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def complete(self, system, user, *, max_tokens=None, reasoning_effort=None,
                 json_mode=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        return self.text, {"model": self.model, "usage_total_tokens": 123,
                           "cost_microusd": 45}


class TriageContextTests(unittest.TestCase):
    def test_findings_are_capped_and_untrusted(self) -> None:
        bundle = dict(BUNDLE)
        bundle["findings"] = [
            {"finding_id": f"F-{i}", "category": "AGI-02", "severity": "high",
             "root_cause_group": "g", "poc_refs": []}
            for i in range(MAX_FINDINGS + 10)
        ]
        context = triage_context(bundle)
        self.assertEqual(context["findings_total"], MAX_FINDINGS + 10)
        self.assertEqual(len(context["findings_in_context"]), MAX_FINDINGS)
        self.assertEqual(context["target"], "/repo/fixtures/demo-target")
        self.assertEqual(context["regression"], {})

    def test_prompt_has_hard_boundaries_and_untrusted_data_frame(self) -> None:
        provider = FakeProvider()
        GovTriage(provider_factory=lambda key: provider).execute(BUNDLE)
        call = provider.calls[0]
        self.assertIn("不做裁决", call["system"])
        self.assertIn("不可信数据", call["system"])
        self.assertIn("reaudit", call["system"])
        self.assertIn("F-POC-0001", call["user"])
        self.assertTrue(call["json_mode"], "triage 是结构化输出")


class ParsePlanTests(unittest.TestCase):
    def findings(self):
        return triage_context(BUNDLE)["findings_in_context"]

    def test_valid_plan(self) -> None:
        plan = parse_plan("```json\n" + PLAN_TEXT + "\n```", self.findings())
        self.assertEqual(plan["confidence"], "high")
        self.assertEqual(plan["priorities"][0]["finding_id"], "F-POC-0001")
        self.assertEqual(plan["priorities"][0]["action"], "reaudit")

    def test_unknown_finding_is_rejected(self) -> None:
        text = json.dumps({"priorities": [
            {"finding_id": "F-FAKE", "action": "reaudit", "why": "编的"}]})
        with self.assertRaisesRegex(ValueError, "不存在的 finding"):
            parse_plan(text, self.findings())

    def test_non_reaudit_action_is_rejected(self) -> None:
        text = json.dumps({"priorities": [
            {"finding_id": "F-POC-0001", "action": "ignore", "why": "跳过"}]})
        with self.assertRaisesRegex(ValueError, "不支持的动作"):
            parse_plan(text, self.findings())

    def test_missing_reason_is_rejected(self) -> None:
        text = json.dumps({"priorities": [
            {"finding_id": "F-POC-0001", "action": "reaudit"}]})
        with self.assertRaisesRegex(ValueError, "缺少理由"):
            parse_plan(text, self.findings())

    def test_non_json_output_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_plan("我觉得应该先看审批记忆。", self.findings())

    def test_provider_unavailable_fails_closed(self) -> None:
        with self.assertRaises(ProviderUnavailable):
            GovTriage(provider_factory=None).execute(BUNDLE)
        with self.assertRaises(ProviderUnavailable):
            GovTriage(provider_factory=lambda key: None).execute(BUNDLE)


class FakeAudit:
    def __init__(self, bundle=BUNDLE):
        self._bundle = bundle

    def bundle(self, audit_id):
        return self._bundle


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = GovBotService(
            FakeAudit(), pathlib.Path(self._tmp.name) / "governance.sqlite",
            start_scheduler=False, provider_factory=lambda key: self.provider,
        )
        self.addCleanup(self.service._db.close)
        self.provider = FakeProvider()
        self.run_id = self._seed_run()

    def _seed_run(self, parent_run_id=None, run_id="bot-seed"):
        with self.service._lock:
            self.service._db.execute(
                "INSERT INTO bot_runs (run_id, bot_id, trigger, target, status,"
                " audit_id, findings, evidence, parent_run_id, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, "auditor", "manual", "/repo/fixtures/demo-target",
                 "done", BUNDLE["audit_id"], 2, 2, parent_run_id,
                 "2026-11-13T10:00:00", "2026-11-13T10:00:10"),
            )
            self.service._db.commit()
        return run_id

    def test_shadow_records_plan_without_spawning(self) -> None:
        self.service.set_settings("auditor", {"triage_mode": "shadow"})
        self.service._after_audit(self.run_id, BUNDLE["audit_id"])
        run = self.service.get_run(self.run_id)
        self.assertEqual(run["triage_status"], "done")
        self.assertEqual(run["triage"]["mode"], "shadow")
        self.assertEqual(run["triage"]["priorities"][0]["finding_id"], "F-POC-0001")
        self.assertFalse(run.get("triage_run_id"))

    def test_active_spawns_one_pending_reaudit(self) -> None:
        self.service.set_settings("auditor", {"triage_mode": "active"})
        self.service._after_audit(self.run_id, BUNDLE["audit_id"])
        source = self.service.get_run(self.run_id)
        child_id = source.get("triage_run_id")
        self.assertTrue(child_id, "active 生成待批准复扫")
        child = self.service.get_run(child_id)
        self.assertEqual(child["status"], "pending")
        self.assertEqual(child["parent_run_id"], self.run_id)
        self.assertEqual(child["bot_id"], "auditor")
        self.assertEqual(child["target"], "/repo/fixtures/demo-target")

    def test_active_with_no_priorities_spawns_nothing(self) -> None:
        self.service.set_settings("auditor", {"triage_mode": "active"})
        self.provider.text = json.dumps({"summary": "无需跟进", "priorities": []})
        self.service._after_audit(self.run_id, BUNDLE["audit_id"])
        run = self.service.get_run(self.run_id)
        self.assertEqual(run["triage_status"], "done")
        self.assertFalse(run.get("triage_run_id"))

    def test_provider_failure_is_recorded_and_audit_is_unchanged(self) -> None:
        self.service.set_settings("auditor", {"triage_mode": "active"})
        self.provider.error = RuntimeError("provider down")
        self.service._after_audit(self.run_id, BUNDLE["audit_id"])
        run = self.service.get_run(self.run_id)
        self.assertEqual(run["status"], "done", "审计终态不被 triage 改写")
        self.assertEqual(run["triage_status"], "error")
        self.assertIn("provider down", run["triage_error"])
        self.assertFalse(run.get("triage_run_id"))

    def test_off_mode_makes_no_call(self) -> None:
        self.service._after_audit(self.run_id, BUNDLE["audit_id"])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.service.get_run(self.run_id)["triage_status"], None)

    def test_triage_spawned_reaudit_does_not_recurse(self) -> None:
        child = self._seed_run(parent_run_id=self.run_id, run_id="bot-child")
        self.service.set_settings("auditor", {"triage_mode": "active"})
        self.service._after_audit(child, BUNDLE["audit_id"])
        self.assertIsNone(self.service.get_run(child)["triage_status"])
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
