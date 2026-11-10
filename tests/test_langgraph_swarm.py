"""LangGraph three-agent pilot tests (skipped without the optional dependency)."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None

if HAS_LANGGRAPH:
    from labs.langgraph_swarm.workflow import (
        GENESIS,
        Manifest,
        build_graph,
        initial_state,
        resume_swarm,
    )


def fake_auditor(target: str, run_dir: Path) -> dict:
    bundle = run_dir / "audit" / "audit-bundle.json"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("{}", encoding="utf-8")
    return {
        "audit_id": "demo-2026-11-10-aaaaaa",
        "bundle": str(bundle),
        "findings": [
            {
                "finding_id": "F-1",
                "root_cause_group": "approval-memory",
                "category": "AGI-02",
                "severity": "high",
            },
            {
                "finding_id": "F-2",
                "root_cause_group": "god-file",
                "category": "VIBE-01",
                "severity": "medium",
            },
        ],
        "poc_evidence": ["POC-0001"],
        "ruleset_version": 10,
        "chain_verification": {"head_hash": "abc"},
    }


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed")
class LangGraphSwarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.run_id = "lg-test"
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _auditor(self, target: str, run_dir: Path) -> dict:
        self.calls += 1
        return fake_auditor(target, run_dir)

    def test_graph_pauses_at_approval_and_resumes_without_reaudit(self):
        app = build_graph(self._auditor)
        result = app.invoke(
            initial_state(self.run_id, "fixtures/demo-target", self.run_dir),
            config={"configurable": {"thread_id": self.run_id}},
        )
        self.assertIn("__interrupt__", result)
        self.assertEqual(self.calls, 1, "auditor ran once before approval")
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["type"], "handoff_approval")
        self.assertEqual(payload["from"], "auditor")

        final = resume_swarm(app, self.run_id, True)
        self.assertEqual(final["status"], "verified")
        self.assertEqual(self.calls, 1, "resume must not replay completed nodes")
        self.assertEqual([item["from"] for item in final["handoffs"]], ["auditor", "planner"])
        self.assertTrue(final["verification"]["passed"])
        self.assertEqual(final["plan"]["finding_id"], "F-1")

        manifest = Manifest(self.run_dir / "manifest.jsonl")
        check = manifest.verify()
        self.assertTrue(check["ok"], check)

    def test_rejection_ends_graph_without_plan(self):
        app = build_graph(self._auditor)
        app.invoke(
            initial_state("lg-reject", "fixtures/demo-target", self.run_dir),
            config={"configurable": {"thread_id": "lg-reject"}},
        )
        final = resume_swarm(app, "lg-reject", False)
        self.assertEqual(final["status"], "rejected")
        self.assertNotIn("plan", final)
        self.assertEqual(final["approvals"][-1]["approved"], False)

    def test_handoff_chain_links_to_genesis_and_previous_record(self):
        app = build_graph(self._auditor)
        app.invoke(
            initial_state("lg-chain", "fixtures/demo-target", self.run_dir),
            config={"configurable": {"thread_id": "lg-chain"}},
        )
        final = resume_swarm(app, "lg-chain", True)
        first, second = final["handoffs"]
        self.assertEqual(first["prev_handoff_hash"], GENESIS)
        self.assertEqual(second["prev_handoff_hash"], first["record_hash"])

    def test_manifest_tamper_is_detected(self):
        app = build_graph(self._auditor)
        app.invoke(
            initial_state("lg-tamper", "fixtures/demo-target", self.run_dir),
            config={"configurable": {"thread_id": "lg-tamper"}},
        )
        resume_swarm(app, "lg-tamper", True)
        path = self.run_dir / "manifest.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["payload"]["from"] = "attacker"
        lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(Manifest(path).verify()["ok"])


if __name__ == "__main__":
    unittest.main()