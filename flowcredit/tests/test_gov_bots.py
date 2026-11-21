"""Governance team roster: durable run log, auditor and verifier roles."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "viewer"))

from gov_bots import GovBotService  # noqa: E402


class FakeAudit:
    def __init__(self):
        self.jobs = {"job-1": {"status": "done", "audit_id": "demo-2026", "findings": 2, "evidence": 2}}
        self.bundle_list = [
            {"dir": "zzz-2026-11-09-aaaaaa"},
            {"dir": "demo-2026-11-10-bbbbbb"},
        ]
        self.started: list[tuple[str, str | None]] = []

    def start_run(self, target, adapter=None, *, target_md=None, osv_snapshot=None):
        self.started.append((target, adapter, target_md, osv_snapshot))
        return "job-1"

    def job_status(self, job_id):
        return self.jobs.get(job_id)

    def bundles(self):
        return {"bundles": self.bundle_list}

    def bundle(self, name):
        if name == "missing":
            return {"error": "not found"}, 404
        return {"audit_id": name, "findings": [{"finding_id": "F-1"}], "poc_evidence_refs": ["POC-0001"]}

    def chain(self, name):
        if name == "broken":
            return {"error": "no session"}, 500
        return {"chain_intact": True, "count": 12, "events": ["a", "b"]}

    def regression(self, name):
        return {"known": [1], "new": [1, 2], "fixed": [], "regressed": [1]}


class FakeTriageProvider:
    model = "fake-triage"

    def complete(self, system, user, *, max_tokens=None, reasoning_effort=None,
                 json_mode=None):
        return json.dumps({
            "summary": "先看审批记忆",
            "confidence": "high",
            "focus_categories": ["AGI-02"],
            "priorities": [{
                "finding_id": "F-1", "action": "reaudit", "why": "证据直接指向门控",
            }],
        }), {"model": self.model, "usage_total_tokens": 12, "cost_microusd": 7}


class BlockingAudit(FakeAudit):
    """每个目标一个独立 job，测试蜂群并发时由测试显式放行。"""

    def __init__(self):
        super().__init__()
        self.jobs = {}
        self._count = 0

    def start_run(self, target, adapter=None, *, target_md=None, osv_snapshot=None):
        self._count += 1
        job_id = f"job-{self._count}"
        self.jobs[job_id] = {"status": "running"}
        self.started.append((target, adapter, target_md, osv_snapshot))
        return job_id

    def finish(self, job_id: str, status: str = "done") -> None:
        self.jobs[job_id] = {
            "status": status,
            "audit_id": f"audit-{job_id}" if status == "done" else None,
            "findings": 1 if status == "done" else 0,
            "evidence": 1 if status == "done" else 0,
            "error": "boom" if status == "failed" else None,
        }


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not settle")


def wait_run(service: GovBotService, run_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run and run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.01)
    raise AssertionError(f"run did not settle: {service.get_run(run_id)}")


class GovBotServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = FakeAudit()
        self.service = GovBotService(
            self.audit, Path(self.tmp.name) / "governance.sqlite", start_scheduler=False,
        )

    def tearDown(self):
        for thread in list(self.service._threads.values()):
            thread.join(timeout=2)
        self.service.shutdown()
        self.tmp.cleanup()

    def test_snapshot_lists_three_idle_bots(self):
        snapshot = self.service.snapshot()
        self.assertEqual([bot["id"] for bot in snapshot["bots"]],
                         ["auditor", "verifier", "analyst"])
        self.assertTrue(all(bot["status"] == "idle" for bot in snapshot["bots"]))
        self.assertTrue(all(bot["last"] is None for bot in snapshot["bots"]))

    def test_auditor_run_records_findings(self):
        run = self.service.start_run("auditor", "fixtures/demo-target")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["audit_id"], "demo-2026")
        self.assertEqual(settled["findings"], 2)
        self.assertEqual(settled["evidence"], 2)
        self.assertEqual(self.audit.started, [("fixtures/demo-target", None, None, None)])
        snapshot = self.service.snapshot()
        auditor = snapshot["bots"][0]
        self.assertEqual(auditor["status"], "idle")
        self.assertEqual(auditor["counts"]["runs"], 1)
        self.assertEqual(auditor["last"]["run_id"], run["run_id"])

    def test_verifier_uses_latest_bundle_when_target_blank(self):
        run = self.service.start_run("verifier", "")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["target"], "demo-2026-11-10-bbbbbb")
        self.assertEqual(settled["status"], "done")
        self.assertTrue(settled["chain_intact"])
        self.assertEqual(settled["events"], 12)
        self.assertEqual(settled["regression"], {"known": 1, "new": 2, "fixed": 0, "regressed": 1})

    def test_verifier_prefers_latest_completed_auditor_run(self):
        """目标留空以运行流为准，不再按目录名猜“最新”。"""
        audit_run = self.service.start_run("auditor", "fixtures/demo-target")
        wait_run(self.service, audit_run["run_id"])
        run = self.service.start_run("verifier", "")
        self.assertEqual(run["target"], "demo-2026")
        self.assertEqual(wait_run(self.service, run["run_id"])["status"], "done")

    def test_analyst_runs_read_only_triage(self):
        self.service._provider_factory = lambda key: FakeTriageProvider()
        run = self.service.start_run("analyst", "demo-2026")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["audit_id"], "demo-2026")
        self.assertEqual(settled["findings"], 0, "分析员不产生 finding")
        self.assertEqual(settled["triage"]["mode"], "manual")
        self.assertEqual(settled["triage"]["priorities"][0]["finding_id"], "F-1")
        self.assertEqual(settled["triage"]["tokens"], 12)
        # 分析员只读：没有 chains/regression 事实，也不写 bundle
        self.assertFalse(settled["regression"])

    def test_analyst_failure_is_visible_without_touching_evidence(self):
        run = self.service.start_run("analyst", "demo-2026")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["triage_status"], "error")
        self.assertIn("ProviderUnavailable", settled["triage_error"])

    def test_analyst_cannot_own_routines(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.create_routine("analyst", {
                "target": "demo-2026", "frequency": "每天", "time": "09:00",
            })
        self.assertIn("manual-only", str(ctx.exception))

    def test_verifier_failure_is_recorded_not_hidden(self):
        run = self.service.start_run("verifier", "broken")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "failed")
        self.assertIn("chain unavailable", settled["error"])

    def test_routine_is_validated_and_listed_with_next_run(self):
        routine = self.service.create_routine("auditor", {
            "target": "fixtures/demo-target", "frequency": "每天", "time": "09:00",
        })
        self.assertEqual(routine["bot_id"], "auditor")
        self.assertTrue(routine["enabled"])
        self.assertTrue(routine["next"])
        listed = self.service.routines("auditor")
        self.assertEqual([item["id"] for item in listed], [routine["id"]])
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["bots"][0]["counts"]["routines"], 1)
        self.assertEqual(len(snapshot["bots"][0]["routines"]), 1)

    def test_routine_fires_due_run_with_trigger(self):
        now = datetime.now()
        due = (now - timedelta(minutes=1)).strftime("%H:%M")
        routine = self.service.create_routine("auditor", {
            "target": "fixtures/demo-target", "frequency": "每天", "time": due,
        })
        fired = self.service.fire_due(now + timedelta(days=1))
        self.assertEqual(len(fired), 1)
        run = wait_run(self.service, fired[0]["run_id"])
        self.assertEqual(run["trigger"], "routine")
        self.assertEqual(run["status"], "done")
        updated = self.service.get_routine(routine["id"])
        self.assertEqual(updated["last_run_id"], run["run_id"])
        self.assertEqual(updated["last_status"], "done")

    def test_paused_routine_does_not_fire_and_can_be_deleted(self):
        now = datetime.now()
        due = (now - timedelta(minutes=1)).strftime("%H:%M")
        routine = self.service.create_routine("auditor", {
            "target": "fixtures/demo-target", "frequency": "每天", "time": due,
        })
        self.service.set_routine_enabled(routine["id"], False)
        self.assertEqual(self.service.fire_due(now), [])
        self.service.set_routine_enabled(routine["id"], True)
        self.assertTrue(self.service.delete_routine(routine["id"]))
        self.assertEqual(self.service.routines("auditor"), [])

    def test_daily_budget_blocks_further_runs(self):
        self.service.set_settings("auditor", {"daily_run_limit": 1, "routine_approval": False})
        run = self.service.start_run("auditor", "fixtures/demo-target")
        wait_run(self.service, run["run_id"])
        with self.assertRaises(ValueError) as ctx:
            self.service.start_run("auditor", "fixtures/demo-target")
        self.assertIn("预算已用完", str(ctx.exception))

    def test_manual_console_run_persists_without_bot_budget(self):
        """对话 /挖洞 是同一条运行流，但不占 Bot 预算，也不影响团队预算。"""
        self.service.set_settings("auditor", {"daily_run_limit": 1, "routine_approval": False})
        team = self.service.start_run("auditor", "fixtures/demo-target")
        wait_run(self.service, team["run_id"])
        with self.assertRaises(ValueError):
            self.service.start_run("auditor", "fixtures/demo-target")

        manual = self.service.start_manual_run(
            "fixtures/demo-target",
            target_md="fixtures/evox/target.md",
            osv_snapshot="work/audit/osv.json",
        )
        self.assertEqual(manual["source"], "console")
        self.assertEqual(manual["trigger"], "manual")
        self.assertEqual(self.audit.started[-1][2:], (
            "fixtures/evox/target.md", "work/audit/osv.json"))
        settled = wait_run(self.service, manual["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["audit_id"], "demo-2026")
        self.assertEqual(settled["findings"], 2)
        # 手动 run 不进入 _runs_today；团队预算仍保持耗尽状态。
        self.assertEqual(self.service._runs_today("auditor"), 1)
        self.assertEqual(self.service.runs("auditor")[0]["source"], "console")
        # 手动 run 复用创建时已启动的审计作业，不能二次启动。
        self.assertEqual(len(self.audit.started), 2)
        with self.assertRaises(ValueError):
            self.service.start_run("auditor", "fixtures/demo-target")

    def test_manual_console_run_skips_triage(self):
        self.service.set_settings("auditor", {"triage_mode": "active"})
        run = self.service.start_manual_run("fixtures/demo-target")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertIsNone(settled["triage_status"], "对话手动挖洞不隐式调用模型")

    def test_manual_run_requires_target(self):
        with self.assertRaises(ValueError):
            self.service.start_manual_run("")

    def test_settings_are_validated(self):
        saved = self.service.set_settings("auditor", {
            "daily_run_limit": 3, "routine_approval": True,
        })
        self.assertEqual(saved["daily_run_limit"], 3)
        self.assertTrue(saved["routine_approval"])
        for bad in (0, 51):
            with self.assertRaises(ValueError):
                self.service.set_settings("auditor", {"daily_run_limit": bad})
        with self.assertRaises(ValueError):
            self.service.set_settings("nope", {"daily_run_limit": 1})

    def test_routine_approval_creates_pending_then_approve(self):
        self.service.set_settings("auditor", {"routine_approval": True})
        run = self.service.start_run("auditor", "fixtures/demo-target", trigger="routine")
        self.assertEqual(run["status"], "pending")
        pending = self.service.snapshot()["bots"][0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["counts"]["pending"], 1)
        approved = self.service.approve_run(run["run_id"])
        self.assertIn(approved["status"], ("queued", "running", "done"))
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["trigger"], "routine")

    def test_pending_approval_blocks_a_second_scheduled_request(self):
        self.service.set_settings("auditor", {"routine_approval": True, "daily_run_limit": 5})
        now = datetime.now()
        due = (now - timedelta(minutes=1)).strftime("%H:%M")
        self.service.create_routine("auditor", {
            "target": "fixtures/demo-target", "frequency": "每天", "time": due,
        })
        first = self.service.fire_due(now + timedelta(days=1))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "pending")
        # The unresolved approval is an active operator decision. A second
        # scheduled fire must not enqueue a duplicate pending run.
        self.assertEqual(self.service.fire_due(now + timedelta(days=2)), [])
        routine_runs = [
            run for run in self.service.runs("auditor") if run["trigger"] == "routine"
        ]
        self.assertEqual(len(routine_runs), 1)
        self.service.reject_run(first[0]["run_id"])
        third = self.service.fire_due(now + timedelta(days=3))
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["status"], "pending")

    def test_routine_rejection_is_recorded_and_frees_budget(self):
        self.service.set_settings("auditor", {"routine_approval": True, "daily_run_limit": 1})
        run = self.service.start_run("auditor", "fixtures/demo-target", trigger="routine")
        rejected = self.service.reject_run(run["run_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.service._runs_today("auditor"), 0)
        with self.assertRaises(ValueError):
            self.service.reject_run(run["run_id"])

    def test_skill_from_completed_run_and_delete(self):
        run = self.service.start_run("auditor", "fixtures/demo-target")
        settled = wait_run(self.service, run["run_id"])
        skill = self.service.create_skill(settled["run_id"])
        self.assertEqual(skill["bot_id"], "auditor")
        self.assertIn("demo-target", skill["name"])
        self.assertEqual(skill["source_run_id"], settled["run_id"])
        self.assertEqual(self.service.skills("auditor")[0]["id"], skill["id"])
        with self.assertRaises(ValueError):
            self.service.create_skill(settled["run_id"])
        self.assertTrue(self.service.delete_skill(skill["id"]))
        self.assertEqual(self.service.skills("auditor"), [])

    def test_invalid_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            self.service.start_run("nope", "x")
        with self.assertRaises(ValueError):
            self.service.start_run("auditor", "  ")
        self.audit.bundle_list = []
        with self.assertRaises(ValueError):
            self.service.start_run("verifier", "")

    def test_restart_marks_interrupted_runs_failed(self):
        run = self.service.start_run("auditor", "fixtures/demo-target")
        wait_run(self.service, run["run_id"])
        # Simulate a row left running by a killed process.
        with self.service._lock:
            self.service._db.execute(
                "UPDATE bot_runs SET status='running', finished_at=NULL WHERE run_id=?",
                (run["run_id"],),
            )
            self.service._db.commit()
        restarted = GovBotService(
            self.audit, Path(self.tmp.name) / "governance.sqlite", start_scheduler=False,
        )
        recovered = restarted.get_run(run["run_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("server restarted", recovered["error"])


class GovSwarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = BlockingAudit()
        self.service = GovBotService(
            self.audit, Path(self.tmp.name) / "governance.sqlite", start_scheduler=False,
        )

    def tearDown(self):
        for job_id, job in list(self.audit.jobs.items()):
            if job.get("status") == "running":
                self.audit.finish(job_id)
        for thread in list(self.service._threads.values()):
            thread.join(timeout=2)
        self.service.shutdown()
        self.service._db.close()
        self.tmp.cleanup()

    def test_swarm_respects_concurrency_and_finishes(self):
        swarm = self.service.create_swarm(
            ["a", "b", "c"], max_concurrency=2,
            target_md="fixtures/evox/target.md", osv_snapshot="work/audit/osv.json",
        )
        wait_until(lambda: len(self.audit.jobs) == 2)
        current = self.service.get_swarm(swarm["id"])
        self.assertEqual(current["counts"]["running"], 2)
        self.assertEqual(current["counts"]["queued"], 1)
        self.assertEqual(self.audit.started[0][2:],
                         ("fixtures/evox/target.md", "work/audit/osv.json"))

        self.audit.finish("job-1")
        wait_until(lambda: self.service.get_run(current["runs"][0]["run_id"])["status"] == "done")
        self.service._pump_swarms()
        wait_until(lambda: len(self.audit.jobs) == 3)
        self.audit.finish("job-2")
        self.audit.finish("job-3")
        for run in self.service.get_swarm(swarm["id"])["runs"]:
            wait_run(self.service, run["run_id"])
        self.service._pump_swarms()
        wait_until(lambda: self.service.get_swarm(swarm["id"])["status"] == "done")
        final = self.service.get_swarm(swarm["id"])
        self.assertEqual(final["counts"]["done"], 3)
        self.assertEqual(final["counts"]["running"], 0)

    def test_swarm_cancel_stops_queue_but_lets_running_finish(self):
        swarm = self.service.create_swarm(["a", "b", "c"], max_concurrency=1)
        wait_until(lambda: len(self.audit.jobs) == 1)
        cancelled = self.service.cancel_swarm(swarm["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["counts"]["rejected"], 2)
        self.assertEqual(cancelled["counts"]["running"], 1)
        self.audit.finish("job-1")
        wait_until(lambda: self.service.get_run(cancelled["runs"][0]["run_id"])["status"] == "done")
        self.service._pump_swarms()
        self.assertEqual(len(self.audit.jobs), 1, "cancel 后不再启动排队目标")
        self.assertEqual(self.service.get_swarm(swarm["id"])["status"], "cancelled")

    def test_swarm_retry_requeues_failed_targets(self):
        swarm = self.service.create_swarm(["a", "b"], max_concurrency=2)
        wait_until(lambda: len(self.audit.jobs) == 2)
        self.audit.finish("job-1", "failed")
        self.audit.finish("job-2", "failed")
        for run in self.service.get_swarm(swarm["id"])["runs"]:
            wait_run(self.service, run["run_id"])
        self.service._pump_swarms()
        self.assertEqual(self.service.get_swarm(swarm["id"])["status"], "failed")

        retried = self.service.retry_swarm(swarm["id"])
        self.assertEqual(retried["status"], "running")
        self.assertEqual(retried["counts"]["running"], 2, "retry 立即按并发启动")
        wait_until(lambda: len(self.audit.jobs) == 4)
        self.audit.finish("job-3")
        self.audit.finish("job-4")
        for run in self.service.get_swarm(swarm["id"])["runs"]:
            wait_run(self.service, run["run_id"])
        self.service._pump_swarms()
        wait_until(lambda: self.service.get_swarm(swarm["id"])["status"] == "done")

    def test_swarm_validates_targets_and_budget(self):
        with self.assertRaises(ValueError):
            self.service.create_swarm([])
        with self.assertRaises(ValueError):
            self.service.create_swarm([f"target-{i}" for i in range(21)])
        self.service.set_settings("auditor", {"daily_run_limit": 1})
        with self.assertRaises(ValueError) as ctx:
            self.service.create_swarm(["a", "b"])
        self.assertIn("剩余预算", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()