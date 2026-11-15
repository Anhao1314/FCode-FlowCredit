"""宿主预置 SECTION 骨架：播种规则与写入门禁。全离线。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.motion_scaffold import (  # noqa: E402
    MOTION_SCAFFOLD_HTML,
    MOTION_SCAFFOLD_MARKERS,
    ensure_motion_scaffold,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


class _Fetcher:
    def fetch(self, url: str) -> dict:  # pragma: no cover - 本测试不触网
        raise AssertionError("fetch must not be called")


class ScaffoldSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_index_html_is_seeded(self) -> None:
        gated = ensure_motion_scaffold(self.sandbox)
        self.assertEqual(gated, ("index.html",))
        content = self.sandbox.read("index.html")["content"]
        for marker in MOTION_SCAFFOLD_MARKERS:
            self.assertIn(marker, content)
        self.assertIn('<svg id="scene" viewBox=', content)

    def test_placeholder_without_anchors_is_replaced(self) -> None:
        self.sandbox.write("index.html", "<!doctype html>\n<html><body>\n"
                                         "<!-- Implement here. -->\n</body></html>\n")
        gated = ensure_motion_scaffold(self.sandbox)
        self.assertEqual(gated, ("index.html",))
        self.assertEqual(
            self.sandbox.read("index.html")["content"], MOTION_SCAFFOLD_HTML,
        )

    def test_real_implementation_is_never_overwritten(self) -> None:
        artifact = "<html><style>@keyframes pedal{}</style><svg viewBox='0 0 1 1'></svg></html>"
        self.sandbox.write("index.html", artifact)
        gated = ensure_motion_scaffold(self.sandbox)
        self.assertEqual(gated, (), "真实实现不播种、也不门禁")
        self.assertEqual(self.sandbox.read("index.html")["content"], artifact)

    def test_already_seeded_file_is_left_alone(self) -> None:
        ensure_motion_scaffold(self.sandbox)
        before = self.sandbox.read("index.html")["sha256"]
        gated = ensure_motion_scaffold(self.sandbox)
        self.assertEqual(gated, ("index.html",))
        self.assertEqual(self.sandbox.read("index.html")["sha256"], before)


class ScaffoldGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self.tmp.name))
        gated = ensure_motion_scaffold(self.sandbox)
        specs = build_general_tools(
            self.sandbox, _Fetcher(), repair_policy=True, scaffolded_paths=gated,
        )
        self.tools = {spec.name: spec for spec in specs}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_whole_file_write_on_scaffold_is_refused_in_every_mode(self) -> None:
        for arguments in (
            {"path": "index.html", "content": "one"},
            {"path": "index.html", "content": "one", "rewrite": True},
            {"path": "index.html", "content": "one", "append": True},
        ):
            with self.assertRaises(AgentError) as raised:
                self.tools["write_file"].handler(arguments)
            self.assertEqual(raised.exception.code, "WRITE_SCAFFOLDED_USE_EDIT")

    def test_other_paths_keep_write_file(self) -> None:
        record = self.tools["write_file"].handler(
            {"path": "FCODE_RESULT.md", "content": "done\n"},
        )
        self.assertIn("sha256_after", record)

    def test_anchor_fill_by_edit_file(self) -> None:
        self.tools["edit_file"].handler({
            "path": "index.html",
            "old_text": "/* SECTION: styles */",
            "new_text": ".wheel{animation:wheel-spin 1s linear infinite}",
        })
        self.tools["edit_file"].handler({
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": '<circle class="wheel" cx="100" cy="400" r="40"/>',
        })
        content = self.sandbox.read("index.html")["content"]
        self.assertIn(".wheel{animation:wheel-spin", content)
        self.assertIn('class="wheel"', content)
        self.assertNotIn("SECTION:", content, "锚点已被实现替换")

    def test_oversized_edit_is_refused_with_chunk_guidance(self) -> None:
        with self.assertRaises(AgentError) as raised:
            self.tools["edit_file"].handler({
                "path": "index.html",
                "old_text": "/* SECTION: styles */",
                "new_text": "x" * 3_001,
            })
        self.assertEqual(raised.exception.code, "EDIT_TOO_LARGE")
        content = self.sandbox.read("index.html")["content"]
        self.assertIn("/* SECTION: styles */", content, "拒绝的 edit 不得落盘")

    def test_chained_fill_readds_anchor(self) -> None:
        self.tools["edit_file"].handler({
            "path": "index.html",
            "old_text": "/* SECTION: styles */",
            "new_text": ".a{fill:red}\n/* SECTION: styles */",
        })
        self.tools["edit_file"].handler({
            "path": "index.html",
            "old_text": "/* SECTION: styles */",
            "new_text": ".b{fill:blue}",
        })
        content = self.sandbox.read("index.html")["content"]
        self.assertIn(".a{fill:red}", content)
        self.assertIn(".b{fill:blue}", content)


if __name__ == "__main__":
    unittest.main()
