"""Motion repairs are edits, not repeated full-file regeneration."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


class RepairPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "workspace")
        self.sandbox.write("index.html", "stub\n")

    def _tools(self, repair_policy: bool):
        return {
            spec.name: spec for spec in build_general_tools(
                self.sandbox, None, repair_policy=repair_policy,
            )
        }

    def test_motion_tool_schema_advertises_the_chunk_limit(self) -> None:
        content = next(
            param for param in self._tools(True)["write_file"].params
            if param.name == "content"
        )
        self.assertEqual(content.max_length, 3_000)

    def test_a_second_full_write_is_refused_until_rewrite_is_explicit(self) -> None:
        tools = self._tools(True)
        first = tools["write_file"].handler({
            "path": "index.html", "content": "<html>one</html>\n",
        })
        self.assertIn("sha256_after", first)
        with self.assertRaises(AgentError) as raised:
            tools["write_file"].handler({
                "path": "index.html", "content": "<html>two</html>\n",
            })
        self.assertEqual(raised.exception.code, "FILE_EXISTS_USE_EDIT")
        forced = tools["write_file"].handler({
            "path": "index.html", "content": "<html>two</html>\n", "rewrite": True,
        })
        self.assertIn("sha256_after", forced)
        with self.assertRaises(AgentError) as again:
            tools["write_file"].handler({
                "path": "index.html", "content": "<html>three</html>\n", "rewrite": True,
            })
        self.assertEqual(again.exception.code, "FILE_EXISTS_USE_EDIT")

    def test_append_is_allowed_for_large_motion_artifacts(self) -> None:
        tools = self._tools(True)
        tools["write_file"].handler({
            "path": "index.html", "content": "<html>skeleton\n",
        })
        appended = tools["write_file"].handler({
            "path": "index.html", "content": "<body>chunk</body></html>\n",
            "append": True,
        })
        self.assertIn("sha256_after", appended)
        self.assertEqual(
            self.sandbox.read("index.html")["content"],
            "<html>skeleton\n<body>chunk</body></html>\n",
        )

    def test_large_single_motion_write_is_refused_with_append_guidance(self) -> None:
        tools = self._tools(True)
        with self.assertRaises(AgentError) as raised:
            tools["write_file"].handler({
                "path": "index.html", "content": "x" * 3_001,
            })
        self.assertEqual(raised.exception.code, "WRITE_TOO_LARGE_USE_APPEND")
        tools["write_file"].handler({"path": "index.html", "content": "skeleton"})
        tools["write_file"].handler({
            "path": "index.html", "content": "x" * 3_001, "append": True,
        })
        self.assertEqual(len(self.sandbox.read("index.html")["content"]), 3_009)

    def test_edit_file_remains_the_normal_repair_path(self) -> None:
        tools = self._tools(True)
        tools["write_file"].handler({
            "path": "index.html", "content": "<html>one</html>\n",
        })
        edited = tools["edit_file"].handler({
            "path": "index.html", "old_text": "one", "new_text": "fixed",
        })
        self.assertEqual(edited["sha256_before"] != edited["sha256_after"], True)
        self.assertEqual(self.sandbox.read("index.html")["content"], "<html>fixed</html>\n")

    def test_the_motion_profile_opts_into_the_policy_via_its_spec(self) -> None:
        world = GENERAL_DOMAIN.create_world(
            spec={"mode": "open", "fastProfile": "motion-render"},
            sandbox=self.sandbox,
        )
        tools = {spec.name: spec for spec in GENERAL_DOMAIN.build_tools(world)}
        # 宿主预置 SECTION 骨架；骨架文件禁止整文件 write_file。
        scaffold = self.sandbox.read("index.html")["content"]
        self.assertIn("/* SECTION: styles */", scaffold)
        self.assertIn("<!-- SECTION: scene -->", scaffold)
        with self.assertRaises(AgentError) as raised:
            tools["write_file"].handler({"path": "index.html", "content": "one\n"})
        self.assertEqual(raised.exception.code, "WRITE_SCAFFOLDED_USE_EDIT")
        # 非骨架路径保持修复策略：首次写入放行，第二次整文件写拒绝。
        tools["write_file"].handler({"path": "FCODE_RESULT.md", "content": "one\n"})
        with self.assertRaises(AgentError) as raised:
            tools["write_file"].handler({"path": "FCODE_RESULT.md", "content": "two\n"})
        self.assertEqual(raised.exception.code, "FILE_EXISTS_USE_EDIT")

    def test_other_domains_keep_their_old_write_semantics(self) -> None:
        tools = self._tools(False)
        tools["write_file"].handler({"path": "index.html", "content": "one\n"})
        second = tools["write_file"].handler({"path": "index.html", "content": "two\n"})
        self.assertIn("sha256_after", second)
        self.assertEqual(self.sandbox.read("index.html")["content"], "two\n")


if __name__ == "__main__":
    unittest.main()
