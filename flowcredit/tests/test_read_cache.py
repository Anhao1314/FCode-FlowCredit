"""Repeated reads are folded without turning an unchanged file into a lie."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


class ReadCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "workspace")
        self.sandbox.write("note.txt", "one\ntwo\nthree\n")
        self.tools = {spec.name: spec for spec in build_general_tools(self.sandbox, None)}

    def test_second_full_read_is_compact_and_force_restores_the_body(self) -> None:
        first = self.tools["read_file_raw"].handler({"path": "note.txt"})
        second = self.tools["read_file_raw"].handler({"path": "note.txt"})
        self.assertEqual(first["content"], "one\ntwo\nthree\n")
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["content"], "")
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertIn("force=true", second["note"])
        summary = self.tools["read_file_raw"].summarize(second)
        self.assertTrue(summary["unchanged"])
        self.assertIn("force=true", summary["note"])

        forced = self.tools["read_file_raw"].handler({"path": "note.txt", "force": True})
        self.assertEqual(forced["content"], "one\ntwo\nthree\n")
        self.assertNotIn("unchanged", forced)

    def test_changed_content_is_returned_in_full_again(self) -> None:
        self.tools["read_file_raw"].handler({"path": "note.txt"})
        self.sandbox.write("note.txt", "changed\n")
        result = self.tools["read_file_raw"].handler({"path": "note.txt"})
        self.assertEqual(result["content"], "changed\n")
        self.assertNotIn("unchanged", result)

    def test_range_reads_are_not_folded_into_the_full_read_cache(self) -> None:
        first = self.tools["read_file"].handler({"path": "note.txt", "offset": 1, "limit": 1})
        second = self.tools["read_file"].handler({"path": "note.txt", "offset": 1, "limit": 1})
        self.assertEqual(first["content"], "one")
        self.assertEqual(second["content"], "one")
        self.assertNotIn("unchanged", second)

    def test_the_two_read_tools_share_the_run_cache(self) -> None:
        self.tools["read_file_raw"].handler({"path": "note.txt"})
        result = self.tools["read_file"].handler({"path": "note.txt"})
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["content"], "")


if __name__ == "__main__":
    unittest.main()
