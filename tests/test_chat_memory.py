"""操作员长期记忆（chat_memory）：存储语义、上限、注入块与 ChatService 接线。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowagent.chat import ChatService
from flowagent.chat_memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name) / "chat-memory.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_remember_and_list(self) -> None:
        self.store.remember("口径基准是 CNY", source="operator")
        self.store.remember("测试用 pytest 而不是 unittest", source="operator")
        entries = self.store.list()
        self.assertEqual(len(entries), 2)
        # 列表按 last_seen 倒序：后写入的在前
        self.assertEqual(entries[0]["text"], "测试用 pytest 而不是 unittest")

    def test_duplicate_text_updates_last_seen_not_row(self) -> None:
        first = self.store.remember("同一条记忆")
        again = self.store.remember("同一条记忆")
        self.assertEqual(first["id"], again["id"], "同文去重：刷新而非新行")
        self.assertGreaterEqual(again["last_seen"], first["last_seen"])
        self.assertEqual(self.store.count(), 1)

    def test_length_bounds(self) -> None:
        with self.assertRaises(ValueError):
            self.store.remember("短")
        with self.assertRaises(ValueError):
            self.store.remember("长" * 601)
        entry = self.store.remember("刚" * 600)
        self.assertIsNotNone(entry)

    def test_forget(self) -> None:
        entry = self.store.remember("会被删掉的记忆")
        self.assertTrue(self.store.forget(entry["id"]))
        self.assertFalse(self.store.forget(entry["id"]), "重复删除返回 False")
        self.assertEqual(self.store.list(), [])

    def test_context_block_empty_is_empty_string(self) -> None:
        self.assertEqual(self.store.context_block(), "")

    def test_context_block_lists_entries_and_respects_limit(self) -> None:
        for i in range(15):
            self.store.remember(f"记忆条目 {i:02d}")
        block = self.store.context_block(limit=12)
        self.assertIn("操作员长期记忆", block)
        self.assertIn("- 记忆条目 14", block, "注入按最新在前")
        self.assertNotIn("- 记忆条目 02", block, "只注入 limit 条,最旧的被截断")
        full = self.store.context_block(limit=50)
        self.assertIn("- 记忆条目 02", full)


class ChatServiceMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ChatService(Path(self._tmp.name) / "chat.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_memory_store_lives_beside_chat_db(self) -> None:
        side = Path(self._tmp.name) / "chat-memory.sqlite"
        self.assertTrue(side.exists(), "记忆库与 chat.sqlite 并排、独立建库")

    def test_prompt_messages_injects_memory_block(self) -> None:
        self.service.memory.remember("口径基准是 CNY")
        messages = self.service._prompt_messages("no-such-thread")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("口径基准是 CNY", messages[0]["content"])

    def test_prompt_messages_without_memory_has_no_block(self) -> None:
        messages = self.service._prompt_messages("no-such-thread")
        self.assertNotIn("操作员长期记忆", messages[0]["content"], "空库不注入空段落")

    def test_forget_removes_from_prompt(self) -> None:
        entry = self.service.memory.remember("临时记忆")
        self.assertIn("临时记忆", self.service._prompt_messages("x")[0]["content"])
        self.service.memory.forget(entry["id"])
        self.assertNotIn("临时记忆", self.service._prompt_messages("x")[0]["content"])

    def test_memory_survives_service_restart(self) -> None:
        self.service.memory.remember("重启后仍在")
        again = ChatService(Path(self._tmp.name) / "chat.sqlite")
        self.assertEqual(again.memory.list()[0]["text"], "重启后仍在")


if __name__ == "__main__":
    unittest.main()
