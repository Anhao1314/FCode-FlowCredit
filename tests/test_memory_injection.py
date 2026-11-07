"""M2: 委托运行时操作员记忆注入 system 上下文。

RunDriver._build_instruction 把 chat 服务的操作员长期记忆追加到模块
协议指令后面——有记忆就带，没记忆就保持原样，异常静默回退。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from console_run_driver import RunDriver  # noqa: E402
from flowagent.chat_memory import MemoryStore  # noqa: E402


class MemoryInjectionTest(unittest.TestCase):
    def _instruction(self, *, store=None) -> str:
        chat = SimpleNamespace(memory=store) if store is not None else None
        driver = SimpleNamespace(
            manager=SimpleNamespace(chat=chat),
            module=SimpleNamespace(instruction="基础协议指令"),
        )
        return RunDriver._build_instruction.__get__(driver)()

    def test_no_chat_returns_raw_instruction(self) -> None:
        self.assertEqual(self._instruction(), "基础协议指令")

    def test_empty_store_returns_raw_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            self.assertEqual(self._instruction(store=store), "基础协议指令",
                             "空库不注入空段落")

    def test_memory_appends_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            store.remember("口径基准是 CNY")
            result = self._instruction(store=store)
            self.assertIn("基础协议指令", result)
            self.assertIn("口径基准是 CNY", result)
            self.assertIn("操作员长期记忆", result)

    def test_forget_removes_from_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            entry = store.remember("临时记忆")
            self.assertIn("临时记忆", self._instruction(store=store))
            store.forget(entry["id"])
            self.assertNotIn("临时记忆", self._instruction(store=store))

    def test_exception_in_memory_silent_fallback(self) -> None:
        """记忆库异常被静默回退——运行不因记忆问题中断。"""
        class BrokenMemory:
            def context_block(self):
                raise RuntimeError("original boom")
        chat = SimpleNamespace(memory=BrokenMemory())
        result = self._instruction(store=chat.memory)
        self.assertEqual(result, "基础协议指令")


import tempfile  # noqa: E402

if __name__ == "__main__":
    unittest.main()
