"""Clarifying intake: message in, reply + choices out, never a fixed quiz."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.intake import clarify  # noqa: E402


class IntakeTests(unittest.TestCase):
    def test_empty_message_does_not_dump_a_questionnaire(self) -> None:
        turn = clarify()
        self.assertFalse(turn["ready"])
        self.assertEqual(turn["choices"], [])
        self.assertIn("先说说", turn["reply"])

    def test_vague_message_gets_intent_choices(self) -> None:
        turn = clarify(message="帮我看看这份财报")
        self.assertFalse(turn["ready"])
        self.assertEqual([choice["field"] for choice in turn["choices"]], ["intent"] * 3)
        self.assertIn("没确定", turn["reply"])

    def test_clear_message_skips_the_question_it_already_answered(self) -> None:
        turn = clarify(message="请重点看重述和口径变化")
        self.assertEqual(turn["spec"]["intent"], "restatement")
        self.assertEqual(turn["choices"][0]["field"], "output")
        self.assertIn("重述与口径", turn["thought"])

    def test_choice_chain_reaches_a_task_card_and_keeps_the_original(self) -> None:
        first = clarify(message="帮我判断两期毛利率是否可比")
        self.assertEqual(first["choices"][0]["field"], "output")
        second = clarify(
            message="",
            spec=first["spec"],
            choice={"field": "output", "value": "memo"},
        )
        self.assertEqual(second["choices"][0]["field"], "approval")
        third = clarify(
            message="",
            spec=second["spec"],
            choice={"field": "approval", "value": "consequential"},
        )
        self.assertTrue(third["ready"])
        self.assertEqual(third["task"]["template"], "full")
        self.assertIn("帮我判断两期毛利率是否可比", third["task"]["objective"])
        self.assertEqual(third["task"]["spec"]["intent"], "compare")
        self.assertEqual(third["task"]["approval"], "consequential")

    def test_missing_evidence_path_uses_open_ended(self) -> None:
        first = clarify(message="资料可能不全，先看能不能下结论")
        self.assertEqual(first["spec"]["intent"], "missing")
        second = clarify(message="", spec=first["spec"], choice={"field": "output", "value": "trace"})
        third = clarify(message="", spec=second["spec"], choice={"field": "approval", "value": "off"})
        self.assertTrue(third["ready"])
        self.assertEqual(third["task"]["template"], "open-ended")

    def test_long_message_is_truncated_to_the_run_limit(self) -> None:
        first = clarify(message="请判断两期毛利率是否可比" + "很长的补充" * 400)
        second = clarify(message="", spec=first["spec"], choice={"field": "output", "value": "memo"})
        third = clarify(message="", spec=second["spec"], choice={"field": "approval", "value": "off"})
        self.assertTrue(third["ready"])
        self.assertLessEqual(len(third["task"]["objective"]), 600)

    def test_general_delegation_takes_the_operators_autonomy_tier(self) -> None:
        """The composer's approval chip is a real decision: a general task used
        to ignore it and always pause on every consequential tool."""
        for tier in ("off", "all", "consequential"):
            with self.subTest(tier=tier):
                turn = clarify(
                    message="帮我整理一下这周的行程",
                    template="general-research",
                    spec={"approval": tier},
                )
                self.assertTrue(turn["ready"])
                self.assertEqual(turn["task"]["approval"], tier)

    def test_general_delegation_defaults_to_asking(self) -> None:
        turn = clarify(message="帮我整理一下这周的行程", template="general-research")
        self.assertEqual(turn["task"]["approval"], "consequential")
        self.assertIn("关键审批", turn["reply"])

    def test_an_unknown_tier_falls_back_to_the_default(self) -> None:
        turn = clarify(
            message="帮我整理一下这周的行程",
            template="general-research",
            spec={"approval": "yolo"},
        )
        self.assertEqual(turn["task"]["approval"], "consequential")

    def test_invalid_choice_does_not_advance(self) -> None:
        turn = clarify(message="帮我看看", choice={"field": "intent", "value": "not-a-real-choice"})
        self.assertEqual(turn["spec"].get("intent"), None)
        self.assertEqual(turn["choices"][0]["field"], "intent")


if __name__ == "__main__":
    unittest.main()
