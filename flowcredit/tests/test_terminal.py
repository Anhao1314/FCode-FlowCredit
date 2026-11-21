"""持久终端：状态持久化、治理语义、输出截断、异常路径。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.terminal import PersistentShell  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


class PersistentShellTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.shell = PersistentShell(Path(self._tmp.name) / "ws")

    def tearDown(self) -> None:
        self.shell.close()
        self._tmp.cleanup()

    def test_basic_execution(self) -> None:
        result = self.shell.execute("echo hello")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "hello")
        self.assertFalse(result["truncated"])

    def test_state_persists_cd(self) -> None:
        self.shell.execute("mkdir -p sub")
        self.shell.execute("cd sub")
        result = self.shell.execute("pwd")
        self.assertIn("sub", result["output"])

    def test_state_persists_env_var(self) -> None:
        self.shell.execute("MY_VAR=hello_world; export MY_VAR")
        result = self.shell.execute("echo $MY_VAR")
        self.assertEqual(result["output"], "hello_world")

    def test_pipes_work(self) -> None:
        self.shell.execute("echo -e 'b\\na\\nc' > /tmp/fcode_test_sort.txt")
        result = self.shell.execute("sort /tmp/fcode_test_sort.txt")
        self.assertEqual(result["output"], "a\nb\nc")

    def test_nonzero_exit_code_captured(self) -> None:
        result = self.shell.execute("false")
        self.assertEqual(result["exit_code"], 1)

    def test_empty_command_refused(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.shell.execute("  ")
        self.assertEqual(ctx.exception.code, "TERMINAL_CMD_EMPTY")

    def test_command_length_cap(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.shell.execute("x" * 4001)
        self.assertEqual(ctx.exception.code, "TERMINAL_CMD_TOO_LONG")

    def test_output_truncation(self) -> None:
        self.shell.execute("yes 'line' | head -500")
        result = self.shell.execute("yes 'output line with some padding' | head -1000")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"]), 8000)

    def test_dead_session_detected(self) -> None:
        # exit 杀掉 bash 进程:本身触发 TERMINAL_DEAD(诚实——会话已终结)
        with self.assertRaises(AgentError) as ctx:
            self.shell.execute("exit 0")
        self.assertEqual(ctx.exception.code, "TERMINAL_DEAD")
        # 重建后可继续使用
        self.shell.execute("echo rebuilt")
        result = self.shell.execute("echo still works")
        self.assertEqual(result["output"], "still works")


class TerminalRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(Path(self._tmp.name) / "ws")
        self.specs = {spec.name: spec for spec in build_general_tools(self.sandbox, fetcher=None)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_terminal_registered_as_write_effect(self) -> None:
        self.assertIn("terminal", self.specs)
        self.assertEqual(self.specs["terminal"].effect, "write")

    def test_terminal_in_chat_write_tools(self) -> None:
        from flowagent.chat_tools import WRITE_TOOLS, APPROVAL_TOOLS
        self.assertIn("terminal", WRITE_TOOLS)
        self.assertIn("terminal", APPROVAL_TOOLS)

    def test_terminal_in_pinned_general_tools(self) -> None:
        from flowagent.tools.general.tools import GENERAL_TOOLS
        self.assertIn("terminal", GENERAL_TOOLS)

    def test_terminal_in_general_research_whitelist(self) -> None:
        from flowagent.demo.fixtures import SCENARIOS
        scenario = SCENARIOS["general-research"]
        self.assertIn("terminal", scenario.allowed_tools)
        self.assertIn("terminal", scenario.grant["allowed_tools"])


from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402

if __name__ == "__main__":
    unittest.main()
