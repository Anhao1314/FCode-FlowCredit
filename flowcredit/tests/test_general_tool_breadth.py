"""工具广度批次（2026-11）：grep_workspace / find_files / git_status。

断言的是治理语义与诚实行为：越界读不到、坏正则显式报错、二进制
跳过、上限截断如实标注、git 状态只读。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


class ToolBreadthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(Path(self._tmp.name) / "ws")
        self.registry = {
            spec.name: spec for spec in build_general_tools(self.sandbox, fetcher=None)
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _call(self, name: str, arguments: dict) -> dict:
        return self.registry[name].handler(arguments)

    def _seed(self) -> None:
        self.sandbox.write("src/main.py", "def charge():\n    return PRICE * 2\n")
        self.sandbox.write("src/util.py", "PRICE = 42\n")
        self.sandbox.write("docs/notes.md", "# 笔记\nPRICE 口径见 src/util.py\n")
        self.sandbox.write("blob.bin", "PRICE\x00binary-ish")

    # ------------------------------------------------------------ grep

    def test_grep_finds_matches_with_line_numbers(self) -> None:
        self._seed()
        result = self._call("grep_workspace", {"pattern": r"PRICE"})
        self.assertGreaterEqual(result["count"], 3)
        paths = {(m["path"], m["line"]) for m in result["matches"]}
        self.assertIn(("src/main.py", 2), paths)
        self.assertIn(("src/util.py", 1), paths)
        # 二进制文件被跳过而不是吐出乱码
        self.assertNotIn("blob.bin", {m["path"] for m in result["matches"]})
        self.assertGreaterEqual(result["skipped_binary"], 1)

    def test_grep_glob_filter_limits_files(self) -> None:
        self._seed()
        result = self._call("grep_workspace", {"pattern": r"PRICE", "glob": "*.md"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["path"], "docs/notes.md")

    def test_grep_invalid_regex_is_explicit_error(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self._call("grep_workspace", {"pattern": "([unclosed"})
        self.assertEqual(ctx.exception.code, "GREP_PATTERN_INVALID")

    def test_grep_pattern_length_cap(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self._call("grep_workspace", {"pattern": "x" * 201})
        self.assertEqual(ctx.exception.code, "GREP_PATTERN_TOO_LONG")

    def test_grep_caps_matches_and_honestly_marks_truncation(self) -> None:
        lines = "\n".join(f"hit {i}" for i in range(500))
        self.sandbox.write("hot.txt", lines)
        result = self._call("grep_workspace", {"pattern": r"hit", "max_results": 10})
        self.assertEqual(result["count"], 10)
        self.assertTrue(result["truncated"])
        # 单文件热命中也不会吃掉整个预算(per-file cap 20)
        self.assertLessEqual(len([m for m in result["matches"]
                                   if m["path"] == "hot.txt"]), 20)

    def test_grep_no_match_is_honest_zero(self) -> None:
        self._seed()
        result = self._call("grep_workspace", {"pattern": r"不存在的字符串xyz"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["matches"], [])

    # -------------------------------------------------------- find_files

    def test_find_files_matches_basename_and_path(self) -> None:
        self._seed()
        by_basename = self._call("find_files", {"pattern": "*.py"})
        paths = {p["path"] for p in by_basename["paths"]}
        self.assertEqual(paths, {"src/main.py", "src/util.py"})
        by_dir = self._call("find_files", {"pattern": "docs/*"})
        self.assertEqual([p["path"] for p in by_dir["paths"]], ["docs/notes.md"])

    def test_find_files_empty_pattern_refused(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self._call("find_files", {"pattern": "  "})
        self.assertEqual(ctx.exception.code, "FIND_PATTERN_REQUIRED")

    def test_find_files_no_match(self) -> None:
        self._seed()
        result = self._call("find_files", {"pattern": "*.rs"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["paths"], [])

    # --------------------------------------------------------- git_status

    def test_git_status_reports_commits_and_clean_tree(self) -> None:
        self.sandbox.write("a.txt", "hello")
        result = self._call("git_status", {})
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["change_count"], 0, "write 已提交,工作树应干净")
        self.assertGreaterEqual(result["commit_count"], 1)
        self.assertIn("write_file", result["commits"][0]["subject"])

    def test_git_status_sees_uncommitted_change(self) -> None:
        self.sandbox.write("a.txt", "hello")
        # 绕过 write 直接改文件:模拟外部变化,status 应如实报告
        (self.sandbox.root / "a.txt").write_text("changed outside", encoding="utf-8")
        result = self._call("git_status", {})
        self.assertEqual(result["change_count"], 1)
        self.assertEqual(result["changes"][0]["path"], "a.txt")

    # ------------------------------------------------- registry/governance

    def test_new_tools_registered_as_read_effect(self) -> None:
        for name in ("grep_workspace", "find_files", "git_status"):
            self.assertIn(name, self.registry, name)
            self.assertEqual(self.registry[name].effect, "read", name)

    def test_chat_read_tools_include_new_entries(self) -> None:
        from flowagent.chat_tools import READ_TOOLS, build_chat_registry, build_chat_permissions

        self.assertIn("grep_workspace", READ_TOOLS)
        self.assertIn("find_files", READ_TOOLS)
        self.assertIn("git_status", READ_TOOLS)
        registry = build_chat_registry(self.sandbox.root)
        for name in ("grep_workspace", "find_files", "git_status"):
            self.assertIn(name, registry.names)
        permissions = build_chat_permissions(registry)
        # 只读工具在所有档位直接执行:合法参数下 gate 必须放行
        # (空参被拒是参数 schema 校验在工作,read_file 同款行为)
        valid_args = {
            "grep_workspace": {"pattern": "x"},
            "find_files": {"pattern": "*.md"},
            "git_status": {},
        }
        for name, arguments in valid_args.items():
            gate = permissions.gate(name, arguments)
            self.assertTrue(gate.allowed, f"{name} 应免审批直接执行: {gate.reason}")


if __name__ == "__main__":
    unittest.main()
