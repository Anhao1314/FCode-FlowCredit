"""Replay projection: an assistant message only carries an action it really has.

The old projection wrote ``action: {}`` onto every historical assistant message.
``{}`` is truthy in JavaScript, so the viewer painted an empty execution card
under every plain reply. These tests pin the projection rule from both ends of
the wire: history read-back, live completion and the stopped branch.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import ChatService  # noqa: E402
from flowagent.pacing import reset  # noqa: E402

PROPOSAL = {
    "goal": "查周三上海到杭州的高铁",
    "mode": "research",
    "sources": ["https://example.com/a"],
    "reason": "需要实时车次",
}


class ReplayActionProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"FCODE_CHAT_MOCK_DELAY": "0"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        reset()
        self.service = ChatService(pathlib.Path(self._tmp.name) / "chat.sqlite")
        self.addCleanup(self.service.close)
        self.addCleanup(reset)

    def send(self, text: str, thread_id: str | None = None, **kwargs):
        return list(self.service.send(thread_id=thread_id, text=text, model="mock", **kwargs))

    def append_assistant(self, thread_id: str, message_id: str, action=None, text="补充说明") -> None:
        task = self.service._load_task(thread_id)
        detail = {
            "id": message_id,
            "user_id": "",
            "text": text,
            "reasoning": "",
            "status": "ok",
            "usage": {},
            "receipt_ref": "",
            "provider": "mock",
            "model": "mock",
        }
        if action is not None:
            detail["action"] = action
        self.service._append(task, "chat_assistant", detail)

    def test_plain_answer_has_no_action_field(self) -> None:
        events = self.send("介绍一下 FCode")
        thread_id = events[0]["thread_id"]
        thread = self.service.get_thread(thread_id)
        assistant = thread["messages"][-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertNotIn("action", assistant)
        # The live done frame follows the same rule as the replay.
        done = [event for event in events if event["type"] == "done"][-1]
        self.assertNotIn("action", done["message"])

    def test_empty_stored_action_is_not_projected(self) -> None:
        events = self.send("历史里的空 action")
        thread_id = events[0]["thread_id"]
        # An older writer stored {} for "no proposal"; replay must drop it.
        self.append_assistant(thread_id, "a-empty", action={})
        thread = self.service.get_thread(thread_id)
        empty = next(item for item in thread["messages"] if item["id"] == "a-empty")
        self.assertNotIn("action", empty)
        self.assertEqual(empty["text"], "补充说明")

    def test_legal_action_survives_the_projection_unchanged(self) -> None:
        events = self.send("给我一个提案")
        thread_id = events[0]["thread_id"]
        self.append_assistant(thread_id, "a-proposal", action=PROPOSAL, text="我建议这样查。")
        thread = self.service.get_thread(thread_id)
        proposal = next(item for item in thread["messages"] if item["id"] == "a-proposal")
        self.assertEqual(proposal["action"], PROPOSAL)

    def test_run_anchor_still_projects_alongside_the_action(self) -> None:
        events = self.send("派一个任务")
        thread_id = events[0]["thread_id"]
        self.append_assistant(thread_id, "a-anchor", action=PROPOSAL)
        message = next(
            item for item in self.service.get_thread(thread_id)["messages"]
            if item["id"] == "a-anchor"
        )
        self.service.link_run(
            thread_id=thread_id,
            message_id=message["id"],
            run_id="run-anchor-1",
            goal="整理文件",
            mode="script",
        )
        thread = self.service.get_thread(thread_id)
        anchored = next(item for item in thread["messages"] if item["id"] == "a-anchor")
        self.assertEqual(anchored["run"]["id"], "run-anchor-1")
        self.assertEqual(anchored["run"]["goal"], "整理文件")
        # The proposal is still there: the anchor adds, it does not replace.
        self.assertEqual(anchored["action"], PROPOSAL)

    def test_plain_message_cannot_smuggle_a_run_anchor_without_a_proposal(self) -> None:
        events = self.send("普通回答")
        thread_id = events[0]["thread_id"]
        assistant = self.service.get_thread(thread_id)["messages"][-1]
        with self.assertRaises(Exception) as caught:
            self.service.link_run(
                thread_id=thread_id,
                message_id=assistant["id"],
                run_id="run-anchor-2",
                goal="整理文件",
                mode="script",
            )
        self.assertEqual(getattr(caught.exception, "code", ""), "CHAT_ACTION_MISSING")

    def test_stopped_answer_has_no_action_on_read_back(self) -> None:
        first = self.send("第一问")
        thread_id = first[0]["thread_id"]
        generator = self.service.send(thread_id=thread_id, text="再写一段", model="mock")
        seen = 0
        for event in generator:
            if event["type"] == "delta":
                seen += 1
                if seen >= 3:
                    generator.close()
                    break
        thread = self.service.get_thread(thread_id)
        stopped = thread["messages"][-1]
        self.assertEqual(stopped["status"], "cancelled")
        self.assertNotIn("action", stopped)


if __name__ == "__main__":
    unittest.main()
