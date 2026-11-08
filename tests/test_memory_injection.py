"""M2: 委托运行时操作员记忆注入 system 上下文 + 验证目标注入。

RunDriver._build_instruction 组合三样东西：
1. 模块协议指令（领域固定的协议文本）
2. 验证目标（spec.verification 里的命令——模型需要知道什么算完成）
3. 操作员长期记忆（chat 服务的 MemoryStore）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from console_run_driver import RunDriver  # noqa: E402
from flowagent.chat_memory import MemoryStore  # noqa: E402


def _make_driver(*, store=None, verification=None):
    """Create a minimal driver-like object for testing _build_instruction."""
    chat = SimpleNamespace(memory=store) if store is not None else None
    run = SimpleNamespace(spec={"verification": verification} if verification else {})
    return SimpleNamespace(
        manager=SimpleNamespace(chat=chat),
        module=SimpleNamespace(instruction="基础协议指令"),
        run=run,
    )


def _instruction(*, store=None, verification=None) -> str:
    driver = _make_driver(store=store, verification=verification)
    return RunDriver._build_instruction.__get__(driver)()


class MemoryInjectionTest(unittest.TestCase):
    def test_no_chat_returns_raw_instruction(self) -> None:
        self.assertEqual(_instruction(), "基础协议指令")

    def test_empty_store_returns_raw_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            self.assertEqual(_instruction(store=store), "基础协议指令",
                             "空库不注入空段落")

    def test_memory_appends_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            store.remember("口径基准是 CNY")
            result = _instruction(store=store)
            self.assertIn("基础协议指令", result)
            self.assertIn("口径基准是 CNY", result)
            self.assertIn("操作员长期记忆", result)

    def test_forget_removes_from_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            entry = store.remember("临时记忆")
            self.assertIn("临时记忆", _instruction(store=store))
            store.forget(entry["id"])
            self.assertNotIn("临时记忆", _instruction(store=store))

    def test_exception_in_memory_silent_fallback(self) -> None:
        """记忆库异常被静默回退——运行不因记忆问题中断。"""
        class BrokenMemory:
            def context_block(self):
                raise RuntimeError("boom")
        result = _instruction(store=BrokenMemory())
        self.assertEqual(result, "基础协议指令")


class VerificationTargetTest(unittest.TestCase):
    def test_verification_command_in_instruction(self) -> None:
        verification = {"command": "python3", "args": ["test_report.py"]}
        result = _instruction(verification=verification)
        self.assertIn("验证目标", result)
        self.assertIn("python3 test_report.py", result)

    def test_no_verification_no_block(self) -> None:
        result = _instruction()
        self.assertNotIn("验证目标", result)

    def test_memory_and_verification_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "mem.sqlite")
            store.remember("口径基准是 CNY")
            verification = {"command": "python3", "args": ["test.py"]}
            result = _instruction(store=store, verification=verification)
            self.assertIn("口径基准是 CNY", result)
            self.assertIn("验证目标", result)
            self.assertIn("python3 test.py", result)


if __name__ == "__main__":
    unittest.main()
