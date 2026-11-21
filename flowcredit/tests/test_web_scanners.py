"""P1 外部扫描刀：可用性探测、降级语义、URL 靶子门。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from web_scanners import run_scanners, scanner_availability  # noqa: E402


class AvailabilityTests(unittest.TestCase):
    def test_availability_reports_three_tools(self) -> None:
        out = scanner_availability()
        for key in ("retire", "gitleaks", "webcrack"):
            self.assertIn(key, out)
            self.assertIn("available", out[key])
            self.assertIn("reason", out[key])

    def test_run_scanners_never_raises_and_reports_degradation(self) -> None:
        with unittest.mock.patch("web_scanners.scanner_availability",
                                 return_value={"retire": {"available": False, "path": "", "reason": "not installed"},
                                               "gitleaks": {"available": False, "path": "", "reason": "not installed"},
                                               "webcrack": {"available": False, "path": "", "reason": "not installed"}}):
            with tempfile.TemporaryDirectory() as tmp:
                out = run_scanners(pathlib.Path(tmp))
            self.assertFalse(out["retire"]["available"])
            self.assertEqual(out["retire"]["reason"], "not installed")
            self.assertTrue(out["webcrack"]["experimental"])




class URLGateTests(unittest.TestCase):
    """gov_bots：URL 靶子 → 治理化快照 → 审计快照目录；快照失败 → run failed。"""

    def _service(self, tmp: pathlib.Path):
        from gov_bots import GovBotService

        class FakeAudit:
            def __init__(self) -> None:
                self.root = tmp / "audit"
                self.repo = tmp
                self.started = []

            def start_run(self, target, adapter=None, *, target_md=None, osv_snapshot=None):
                self.started.append(target)
                return "job-x"

            def job_status(self, job_id):
                return {"status": "done", "audit_id": "a-1", "findings": 0, "evidence": 0}

        class Bots(GovBotService):
            pass

        import sqlite3
        svc = Bots.__new__(Bots)
        svc.audit = FakeAudit()
        svc.db_path = tmp / "gov.sqlite"
        import gov_bots as gb
        conn = sqlite3.connect(svc.db_path)
        conn.executescript(gb._SCHEMA if hasattr(gb, "_SCHEMA") else "")
        # _update 需要 _conn；最简：monkeypatch _update/_finish
        updates = []
        svc._update = lambda run_id, **kw: updates.append(kw)
        finishes = []
        svc._finish = lambda run_id, **kw: finishes.append(kw)
        svc._after_audit = lambda run_id, audit_id: None
        return svc, updates, finishes

    def test_url_target_snapshots_then_audits_snapshot_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, updates, finishes = self._service(pathlib.Path(tmp))
            snap = pathlib.Path(tmp) / "snap"
            snap.mkdir()
            (snap / "index.html").write_text("<html></html>")
            with mock.patch("web_snapshot.snapshot_url") as fake_snap:
                fake_snap.return_value = {
                    "source_url": "https://example.com", "fetched_at": "t",
                    "robots": {"path_allowed": True},
                    "security_headers": {"missing": []},
                    "assets": {"fetched": 2, "requested": 2},
                }
                svc._run_auditor("r-1", "https://example.com")
            started = svc.audit.started[0]
            self.assertIn("workspace-general", started, "审计目标应为快照目录")
            self.assertTrue(any(u.get("target") == started for u in updates))
            self.assertTrue(finishes and finishes[0].get("status") == "done")

    def test_snapshot_failure_fails_the_run_without_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, updates, finishes = self._service(pathlib.Path(tmp))
            with mock.patch("web_snapshot.snapshot_url",
                            side_effect=ValueError("robots.txt 拒绝该路径")):
                svc._run_auditor("r-2", "https://example.com/admin")
            self.assertEqual(svc.audit.started, [], "快照拒绝不得起审计")
            self.assertTrue(finishes and "robots" in str(finishes[0].get("error")))


if __name__ == "__main__":
    unittest.main()
