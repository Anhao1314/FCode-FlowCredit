"""Governance context core: policy files, memory injection, sectioned snapshot."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from gov_chat import GovChatService, _fit_sections, _section  # noqa: E402
from gov_policy import load_policy  # noqa: E402


class FakeAudit:
    def __init__(self, bundles):
        self._bundles = bundles

    def bundles(self):
        return {"bundles": self._bundles}

    def bundle(self, name):
        for item in self._bundles:
            if name in (item.get("dir"), item.get("audit_id")):
                return {
                    **item,
                    "target": {"identity": item.get("target") or ""},
                    "toolchain": {"ruleset_version": "11"},
                    "poc_evidence_refs": ["POC-0001"],
                    "findings": [{
                        "finding_id": "F-POC-0001",
                        "category": "WEB-06",
                        "severity": "medium",
                        "root_cause_group": "missing-security-headers",
                        "poc_refs": ["POC-0001"],
                    }],
                    "uncovered_surfaces": [{"surface": "dynamic probing"}],
                }
        return {"error": "not found"}, 404


class FakeGovBots:
    def __init__(self, bundles=None):
        self.audit = FakeAudit(bundles or [])

    def snapshot(self):
        return {"bots": [{
            "id": "auditor", "name": "审计员",
            "charter": "对目标执行离线确定性审计。",
        }]}

    def runs(self, limit=200):
        return [
            {"run_id": "bot-old", "bot_id": "auditor", "target": "a",
             "status": "done", "findings": 1, "audit_id": "a-2026-01-01-aaaa"},
            {"run_id": "bot-new", "bot_id": "auditor", "target": "a",
             "status": "pending", "findings": 0, "audit_id": ""},
        ]

    def get_run(self, run_id):
        return next((run for run in self.runs() if run.get("run_id") == run_id), None)


def bundle(audit_id: str, target: str = "a", findings: int = 1) -> dict:
    return {
        "dir": audit_id,
        "audit_id": audit_id,
        "target": target,
        "findings": findings,
        "severity": {"high": 1, "medium": 0, "low": 0},
        "unhandled": findings,
    }


class PolicyTests(unittest.TestCase):
    def test_hierarchy_order_comments_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "repo"
            user = root / "user"
            project.mkdir()
            user.mkdir()
            (user / "GOVERNANCE.md").write_text(
                "<!-- 用户备注 -->\n用户制度：先写证据。\n", encoding="utf-8")
            (project / "shared.md").write_text("项目共享：只读目标。\n", encoding="utf-8")
            (project / "GOVERNANCE.md").write_text(
                "项目制度：\n@shared.md\n", encoding="utf-8")
            (project / "GOVERNANCE.local.md").write_text(
                "本机制度：审计包加密。\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FCODE_CONFIG_DIR": str(user)}):
                policy = load_policy(project)
        self.assertIn("用户制度", policy["text"])
        self.assertIn("项目共享", policy["text"])
        self.assertIn("本机制度", policy["text"])
        self.assertNotIn("用户备注", policy["text"])
        self.assertLess(policy["text"].index("用户制度"),
                        policy["text"].index("本机制度"))
        self.assertEqual(len(policy["files"]), 4)
        self.assertFalse(policy["truncated"])

    def test_import_escape_is_skipped_with_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "repo"
            project.mkdir()
            (project / "GOVERNANCE.md").write_text(
                "@../../outside.md\n本机制度：保留。\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "FCODE_CONFIG_DIR": str(root / "user")}):
                policy = load_policy(project)
        self.assertNotIn("outside", policy["text"])
        self.assertTrue(any("越出制度目录" in note for note in policy["notes"]))


class GovernanceContextTests(unittest.TestCase):
    def _service(self, tmp: str, **kwargs) -> GovChatService:
        return GovChatService(
            pathlib.Path(tmp) / "gov-chat.sqlite", None,
            FakeGovBots([bundle("a-2026-02-02-bbbb"),
                         bundle("z-2026-03-03-cccc")]),
            **kwargs,
        )

    def test_operator_memory_is_injected_into_governance_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            service.memory.remember("审计目标只读，处置必须人裁决", source="chat:test")
            thread = service._create_thread("治理")
            messages = service._prompt_messages(thread.id)
            system = messages[0]["content"]
        self.assertIn("[操作员长期记忆", system)
        self.assertIn("处置必须人裁决", system)
        self.assertTrue(service._last_context["memory_injected"])

    def test_policy_is_injected_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "GOVERNANCE.md").write_text(
                "制度：审计结论只信证据链。\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "FCODE_CONFIG_DIR": str(root / "user")}):
                service = self._service(tmp, policy_root=root)
                thread = service._create_thread("治理")
                messages = service._prompt_messages(thread.id)
                meta = service._last_context
                system = messages[0]["content"]
        self.assertIn("治理制度", system)
        self.assertIn("审计结论只信证据链", system)
        self.assertEqual(len(meta["policy_sha256"]), 64)
        self.assertTrue(meta["policy_files"])

    def test_snapshot_audits_are_newest_first_and_sectioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            text = service.governance_snapshot()
            meta = service.snapshot()
        self.assertIn("### 最近审计", text)
        self.assertIn("### 待批准运行", text)
        self.assertLess(text.index("z-2026-03-03-cccc"),
                        text.index("a-2026-02-02-bbbb"))
        self.assertLessEqual(len(text), 6000)
        self.assertEqual(meta["chars"], len(text))
        self.assertEqual(len(meta["sha256"]), 64)

    def test_fit_sections_drops_lowest_priority_first(self) -> None:
        sections = [
            _section("角色", 40, 500, ["角色："] + ["- 很长的角色说明" for _ in range(20)]),
            _section("最近审计", 95, 2000, ["最近审计：", "- audit-1", "- audit-2"]),
        ]
        kept, trimmed = _fit_sections(sections, budget=80)
        lines = {item["name"]: item["lines"] for item in kept}
        # The roster is trimmed first; the audit section keeps every row.
        self.assertLess(len(lines["角色"]), 7)
        self.assertEqual(len(lines["最近审计"]), 3)
        self.assertEqual(trimmed, ["角色"])


class AuditOrderingTests(unittest.TestCase):
    def test_bundles_are_sorted_newest_first(self) -> None:
        from audit_service import AuditService

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ("alpha-2026-01-01-aaaa", "zeta-2026-03-03-bbbb"):
                directory = root / name
                directory.mkdir()
                (directory / "audit-bundle.json").write_text(json.dumps({
                    "audit_id": name,
                    "target": {"identity": name},
                    "findings": [],
                    "poc_evidence_refs": [],
                }), encoding="utf-8")
            service = AuditService(repo=root, root=root)
            bundles = service.bundles()["bundles"]
        self.assertEqual([item["dir"] for item in bundles],
                         ["zeta-2026-03-03-bbbb", "alpha-2026-01-01-aaaa"])


class FakeCompactionProvider:
    def stream(self, messages, tools=None):
        yield {"delta": "<analysis>隐藏草稿</analysis>"
                        "<summary>## 目标\n操作员在跟进同一目标的两次审计。</summary>"}


class FakeProviderConfig:
    key = "fake"


class ToolTests(unittest.TestCase):
    def test_governance_chat_tools_are_read_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GovChatService(
                pathlib.Path(tmp) / "gov-chat.sqlite", None,
                FakeGovBots([bundle("a-2026-02-02-bbbb")]),
            )
            context = service._tool_context()
            names = set(context["registry"].names)
            list_obs = context["registry"].execute("list_audits", {"limit": 1})
            audit_obs = context["registry"].execute(
                "read_audit", {"audit_id": "a-2026-02-02-bbbb"})
            run_obs = context["registry"].execute("read_run", {"run_id": "bot-new"})
            service.memory.remember("审计目标只读", source="chat:test")
            memory_obs = context["registry"].execute(
                "read_memory", {"query": "审计"})
        self.assertEqual(names, {"list_audits", "read_audit", "read_run", "read_memory"})
        self.assertEqual(list_obs.result["count"], 1)
        self.assertEqual(audit_obs.result["findings"][0]["finding_id"], "F-POC-0001")
        self.assertEqual(audit_obs.result["uncovered"], ["dynamic probing"])
        self.assertEqual(run_obs.result["status"], "pending")
        self.assertEqual(memory_obs.result["count"], 1)
        self.assertTrue(memory_obs.result["memories"][0]["text"].startswith("审计目标只读"))


class CompactionTests(unittest.TestCase):
    def test_compaction_stores_summary_and_reinjects_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GovChatService(
                pathlib.Path(tmp) / "gov-chat.sqlite", None, FakeGovBots())
            service._provider = lambda model: (FakeProviderConfig(), FakeCompactionProvider())
            thread = service._create_thread("治理")
            thread = service._append(thread, "chat_user", {
                "id": "u1", "text": "第一轮问题"})
            thread = service._append(thread, "chat_assistant", {
                "id": "a1", "text": "第一轮回答", "status": "ok"})
            with mock.patch.dict(os.environ, {"FCODE_GOV_COMPACT_TOKENS": "1"}):
                payload = service._maybe_compact(thread, "fake")
                messages = service._prompt_messages(thread.id)
            stored = service.last_compaction(thread.id)
        self.assertIsNotNone(payload)
        self.assertEqual(stored["summary_sha256"], payload["summary_sha256"])
        self.assertNotIn("隐藏草稿", stored["summary"])
        self.assertIn("操作员在跟进同一目标", stored["summary"])
        system = messages[0]["content"]
        self.assertIn("更早对话的压缩摘要", system)
        self.assertIn("操作员在跟进同一目标", system)

    def test_compaction_below_threshold_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GovChatService(
                pathlib.Path(tmp) / "gov-chat.sqlite", None, FakeGovBots())
            service._provider = lambda model: (FakeProviderConfig(), FakeCompactionProvider())
            thread = service._create_thread("治理")
            thread = service._append(thread, "chat_user", {"id": "u1", "text": "你好"})
            with mock.patch.dict(os.environ, {"FCODE_GOV_COMPACT_TOKENS": "50000"}):
                payload = service._maybe_compact(thread, "fake")
                self.assertIsNone(payload)
                self.assertIsNone(service.last_compaction(thread.id))


if __name__ == "__main__":
    unittest.main()
