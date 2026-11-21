"""Governance chat: normal model conversation + non-executing gov_action proposals."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.chat_protocol import (  # noqa: E402
    parse_action_block,
    parse_gov_action_block,
)
from gov_chat import GovChatService  # noqa: E402


def gov_block(payload) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "```gov_action\n" + body + "\n```"


class GovActionProtocolTests(unittest.TestCase):
    def test_valid_audit_proposal_is_split_from_prose(self) -> None:
        text, action = parse_gov_action_block(
            "我先安排审计员。\n\n"
            + gov_block({
                "bot_id": "auditor",
                "action": "audit",
                "target": "fixtures/demo-target",
                "reason": "验证最近改动",
            })
            + "\n"
        )
        self.assertEqual(text, "我先安排审计员。")
        self.assertEqual(action["kind"], "gov")
        self.assertEqual(action["bot_id"], "auditor")
        self.assertEqual(action["action"], "audit")
        self.assertEqual(action["target"], "fixtures/demo-target")
        self.assertEqual(action["reason"], "验证最近改动")

    def test_verifier_may_leave_target_empty(self) -> None:
        _, action = parse_gov_action_block(gov_block({
            "bot_id": "verifier", "target": "",
        }))
        self.assertEqual(action["action"], "verify")
        self.assertEqual(action["target"], "")

    def test_auditor_requires_a_target(self) -> None:
        text, action = parse_gov_action_block(gov_block({
            "bot_id": "auditor", "action": "audit", "target": " ",
        }))
        self.assertIsNone(action)
        self.assertIn("gov_action", text)

    def test_role_action_mismatch_is_refused(self) -> None:
        _, action = parse_gov_action_block(gov_block({
            "bot_id": "auditor", "action": "verify", "target": "x",
        }))
        self.assertIsNone(action)

    def test_unknown_role_and_oversize_are_refused(self) -> None:
        self.assertIsNone(parse_gov_action_block(gov_block({
            "bot_id": "admin", "action": "audit", "target": "x",
        }))[1])
        self.assertIsNone(parse_gov_action_block(gov_block({
            "bot_id": "auditor", "action": "audit", "target": "x" * 600,
        }))[1])

    def test_explicit_parser_keeps_fcode_action_contract(self) -> None:
        text, action = parse_action_block(
            "旧提案\n```fcode_action\n"
            + json.dumps({"goal": "写一个脚本", "mode": "script"})
            + "\n```"
        )
        self.assertEqual(text, "旧提案")
        self.assertEqual(action["goal"], "写一个脚本")
        self.assertNotIn("kind", action)
        _, gov = parse_action_block(
            "治理\n" + gov_block({"bot_id": "analyst", "target": "demo-2026"}),
            block="gov_action",
        )
        self.assertEqual(gov["kind"], "gov")
        self.assertEqual(gov["action"], "analyze")


class FakeAudit:
    def bundles(self):
        return {"bundles": [{
            "audit_id": "demo-2026", "target": "fixtures/demo-target",
            "findings": 3, "severity": {"high": 2, "medium": 1, "low": 0},
            "unhandled": 2,
        }]}


class FakeGovBots:
    audit = FakeAudit()

    def snapshot(self):
        return {"bots": [{
            "id": "auditor", "name": "审计员",
            "charter": "对目标执行离线确定性审计。",
        }]}

    def runs(self, limit=200):
        return [
            {"run_id": "bot-1", "bot_id": "auditor", "target": "fixtures/demo-target",
             "status": "pending", "findings": 0, "audit_id": ""},
            {"run_id": "bot-2", "bot_id": "auditor", "target": "fixtures/demo-target",
             "status": "done", "findings": 3, "audit_id": "demo-2026"},
        ]


class GovChatServiceTests(unittest.TestCase):
    def test_snapshot_is_bounded_and_data_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GovChatService(
                pathlib.Path(tmp) / "gov-chat.sqlite", None, FakeGovBots(),
            )
            text = service.governance_snapshot()
            self.assertIn("待批准运行：1 条", text)
            self.assertIn("bot-1", text)
            self.assertIn("demo-2026", text)
            self.assertIn("findings=3", text)
            self.assertLessEqual(len(text), 6000)

    def test_prompt_uses_governance_profile_and_action_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GovChatService(
                pathlib.Path(tmp) / "gov-chat.sqlite", None, FakeGovBots(),
            )
            thread = service._create_thread("治理对话")
            messages = service._prompt_messages(thread.id)
            system = messages[0]["content"]
            self.assertIn("gov_action", system)
            self.assertIn("不能", system)
            self.assertIn("当前治理快照", system)
            self.assertEqual(service.action_block, "gov_action")


class PostExplanationTests(unittest.TestCase):
    """解读输出进对话：宿主把已成文的解读追加进线程链，不伪造轮次。"""

    def _service(self, tmp: str) -> GovChatService:
        return GovChatService(
            pathlib.Path(tmp) / "gov-chat.sqlite", None, FakeGovBots(),
        )

    def test_post_creates_thread_and_message_is_honest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            posted = service.post_explanation(
                "",
                text="**一句话**：命中 OSV 已知漏洞依赖。",
                model="deepseek-flash",
                tokens=1522,
                cost_microusd=473,
                title="AI 解读 · evox",
                finding_id="F-POC-0002",
                audit_id="evox.app-2026-11-18-739ba7",
            )
            thread = service.get_thread(posted["thread_id"])
            self.assertEqual(len(thread["messages"]), 1)
            message = thread["messages"][0]
            self.assertEqual(message["role"], "assistant")
            # 正文干净（无前缀/边界行），上下文在 explain 元数据里
            self.assertEqual(
                message["text"], "**一句话**：命中 OSV 已知漏洞依赖。")
            self.assertNotIn("【AI 解读", message["text"])
            self.assertEqual(message["explain"]["finding_id"], "F-POC-0002")
            self.assertEqual(
                message["explain"]["audit_id"], "evox.app-2026-11-18-739ba7")
            self.assertEqual(message["model"], "deepseek-flash")
            self.assertEqual(message["usage"]["total_tokens"], 1522)
            # 没有收据就不得声称已签：渲染端按 receipt_ref 判定
            self.assertEqual(message["receipt_ref"], "")

    def test_post_joins_existing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            first = service.post_explanation(
                "", text="**结论**：第一条解读", model="m", tokens=1, cost_microusd=1)
            second = service.post_explanation(
                first["thread_id"], text="**结论**：第二条解读", model="m",
                tokens=2, cost_microusd=2)
            self.assertEqual(second["thread_id"], first["thread_id"])
            thread = service.get_thread(second["thread_id"])
            self.assertEqual(
                [m["text"] for m in thread["messages"]],
                ["**结论**：第一条解读", "**结论**：第二条解读"],
            )

    def test_post_rejects_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            with self.assertRaises(Exception) as ctx:
                service.post_explanation("", text="  ", model="m")
            self.assertIn("解读文本为空", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
