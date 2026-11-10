"""Governance team roster: durable run log, auditor and verifier roles."""

from __future__ import annotations

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

    def start_run(self, target, adapter=None):
        self.started.append((target, adapter))
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

    def test_snapshot_lists_two_idle_bots(self):
        snapshot = self.service.snapshot()
        self.assertEqual([bot["id"] for bot in snapshot["bots"]], ["auditor", "verifier"])
        self.assertTrue(all(bot["status"] == "idle" for bot in snapshot["bots"]))
        self.assertTrue(all(bot["last"] is None for bot in snapshot["bots"]))

    def test_auditor_run_records_findings(self):
        run = self.service.start_run("auditor", "fixtures/demo-target")
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["audit_id"], "demo-2026")
        self.assertEqual(settled["findings"], 2)
        self.assertEqual(settled["evidence"], 2)
        self.assertEqual(self.audit.started, [("fixtures/demo-target", None)])
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
        fired = self.service.fire_due(now)
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
        self.assertIn(approved["status"], ("queued", "running"))
        settled = wait_run(self.service, run["run_id"])
        self.assertEqual(settled["status"], "done")
        self.assertEqual(settled["trigger"], "routine")

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


if __name__ == "__main__":
    unittest.main()