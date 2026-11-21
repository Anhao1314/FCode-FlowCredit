"""Operator memory: conservative extraction, relevance recall, schema migration."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import ChatService  # noqa: E402
from flowagent.chat_memory import MemoryStore  # noqa: E402
from flowagent.memory.extract import extract_memories  # noqa: E402


class ExtractTests(unittest.TestCase):
    def test_durable_preference_is_extracted(self) -> None:
        found = extract_memories("以后都用 pytest，不要再用 unittest。这个 bug 怎么修？")
        self.assertEqual(len(found), 1)
        self.assertIn("pytest", found[0]["text"])
        self.assertEqual(found[0]["confidence"], 0.6)

    def test_explicit_remember_has_high_confidence(self) -> None:
        found = extract_memories("请记住：报告统一用中文")
        self.assertEqual(found[0]["text"], "报告统一用中文")
        self.assertEqual(found[0]["confidence"], 0.95)

    def test_plain_task_instruction_is_not_memory(self) -> None:
        self.assertEqual(extract_memories("不要修改测试或 verify.py"), [])
        self.assertEqual(extract_memories("修好这个 bug，然后跑 verify.py"), [])
        self.assertEqual(extract_memories("这个函数的返回值是什么？"), [])

    def test_project_convention_is_extracted(self) -> None:
        found = extract_memories("我们项目统一用 ruff 格式化")
        self.assertEqual(len(found), 1)
        self.assertIn("ruff", found[0]["text"])

    def test_at_most_three_per_message(self) -> None:
        text = "以后都用 pytest。记住报告用中文。我们项目统一用 ruff。始终不要用 unittest。"
        self.assertEqual(len(extract_memories(text)), 3)


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(pathlib.Path(self._tmp.name) / "memory.sqlite")

    def test_ingest_marks_auto_and_source(self) -> None:
        written = self.store.ingest("以后都用 pytest 跑测试", source="chat:t1")
        self.assertEqual(len(written), 1)
        entry = written[0]
        self.assertEqual(entry["auto"], 1)
        self.assertEqual(entry["kind"], "auto")
        self.assertEqual(entry["source"], "chat:t1")

    def test_relevance_beats_recency(self) -> None:
        self.store.ingest("以后都用 pytest 跑测试")
        self.store.ingest("报告统一用中文写")
        self.store.ingest("我们项目统一用 ruff 格式化")
        hits = self.store.recall("pytest 测试怎么跑", limit=2)
        self.assertTrue(hits)
        self.assertIn("pytest", hits[0]["text"])
        self.assertGreater(hits[0]["score"], 0)
        self.assertTrue(all("score" in hit for hit in hits))

    def test_context_block_uses_query_and_falls_back(self) -> None:
        self.store.ingest("以后都用 pytest 跑测试")
        self.store.ingest("报告统一用中文写")
        block = self.store.context_block(limit=1, query="pytest")
        self.assertIn("pytest", block)
        self.assertIn("[auto]", block)
        fallback = self.store.context_block(limit=2, query="完全无关的词汇 zzz")
        self.assertIn("操作员长期记忆", fallback)
        self.assertIn("报告统一用中文写", fallback)

    def test_explicit_reentry_upgrades_auto_to_operator(self) -> None:
        auto = self.store.remember("以后都用 pytest", auto=True, kind="auto", confidence=0.6)
        self.assertEqual(auto["auto"], 1)
        operator = self.store.remember("以后都用 pytest", auto=False, kind="operator")
        self.assertEqual(operator["auto"], 0)
        self.assertEqual(operator["kind"], "operator")

    def test_old_m1_database_is_migrated_in_place(self) -> None:
        path = pathlib.Path(self._tmp.name) / "old.sqlite"
        db = sqlite3.connect(str(path))
        db.executescript(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'operator', "
            "text TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, "
            "last_seen TEXT NOT NULL);"
        )
        db.execute(
            "INSERT INTO memories (id, kind, text, source, created, last_seen) "
            "VALUES ('m1', 'operator', '旧记忆仍在', '', '2026-11-01T00:00:00Z', "
            "'2026-11-01T00:00:00Z')"
        )
        db.commit()
        db.close()
        store = MemoryStore(path)
        entry = store.list()[0]
        self.assertEqual(entry["text"], "旧记忆仍在")
        self.assertEqual(entry["auto"], 0)
        self.assertEqual(entry["scope"], "global")
        self.assertEqual(entry["confidence"], 1.0)
        store.remember("新记忆也能写入")
        self.assertEqual(store.count(), 2)


class ChatAutoMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ChatService(pathlib.Path(self._tmp.name) / "chat.sqlite")

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_send_extracts_durable_preference(self) -> None:
        for _ in self.service.send(thread_id=None, text="以后都用 pytest 跑测试"):
            pass
        entries = self.service.memory.list()
        self.assertTrue(any(entry["auto"] == 1 and "pytest" in entry["text"] for entry in entries))

    def test_plain_message_does_not_create_memory(self) -> None:
        for _ in self.service.send(thread_id=None, text="你好"):
            pass
        self.assertEqual(self.service.memory.count(), 0)


if __name__ == "__main__":
    unittest.main()