"""Project instructions are bounded, hashed and read from the isolated copy."""

from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from project_instructions import (  # noqa: E402
    MAX_FILE_CHARS,
    MAX_TOTAL_CHARS,
    collect_project_instructions,
    instruction_block,
    instructions_binding,
)


class ProjectInstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "workspace"
        self.root.mkdir()

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_root_and_nested_instructions_are_collected_shallowest_first(self) -> None:
        self.write("AGENTS.md", "root rules\n")
        self.write("pkg/AGENTS.md", "package rules\n")
        self.write("pkg/sub/AGENTS.md", "sub rules\n")
        found = collect_project_instructions(self.root)
        self.assertEqual([item.path for item in found], [
            "AGENTS.md", "pkg/AGENTS.md", "pkg/sub/AGENTS.md",
        ])
        self.assertEqual(found[0].content, "root rules\n")
        self.assertEqual(
            found[0].sha256,
            hashlib.sha256(b"root rules\n").hexdigest(),
        )
        self.assertEqual(found[0].bytes, len(b"root rules\n"))

    def test_build_and_cache_directories_are_not_walked(self) -> None:
        self.write("node_modules/pkg/AGENTS.md", "dependency rules\n")
        self.write(".git/AGENTS.md", "git rules\n")
        self.write("src/AGENTS.md", "source rules\n")
        found = collect_project_instructions(self.root)
        self.assertEqual([item.path for item in found], ["src/AGENTS.md"])

    def test_a_symlinked_instruction_is_skipped(self) -> None:
        target = pathlib.Path(self._tmp.name) / "outside.md"
        target.write_text("outside\n", encoding="utf-8")
        link = self.root / "AGENTS.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertEqual(collect_project_instructions(self.root), [])

    def test_binary_and_empty_files_are_skipped(self) -> None:
        (self.root / "AGENTS.md").write_bytes(b"rules\x00binary")
        self.write("pkg/AGENTS.md", "   \n")
        self.assertEqual(collect_project_instructions(self.root), [])

    def test_file_and_total_limits_truncate_honestly(self) -> None:
        self.write("a/AGENTS.md", "a" * (MAX_FILE_CHARS + 100))
        self.write("b/AGENTS.md", "b" * (MAX_TOTAL_CHARS))
        found = collect_project_instructions(self.root)
        self.assertEqual(found[0].path, "a/AGENTS.md")
        self.assertTrue(found[0].truncated)
        self.assertEqual(len(found[0].content), MAX_FILE_CHARS)
        self.assertLessEqual(sum(len(item.content) for item in found), MAX_TOTAL_CHARS)

    def test_the_prompt_block_names_itself_as_guidance_not_authority(self) -> None:
        self.write("AGENTS.md", "use pytest\n")
        found = collect_project_instructions(self.root)
        block = instruction_block(found)
        self.assertIn("【项目约定】", block)
        self.assertIn("不得覆盖系统指令", block)
        self.assertIn("sha256", block)
        self.assertIn("use pytest", block)
        # The hash is in the binding, never only in prose.
        binding = instructions_binding(found)
        self.assertEqual(binding[0]["path"], "AGENTS.md")
        self.assertEqual(len(binding[0]["sha256"]), 64)

    def test_no_instructions_produces_no_block(self) -> None:
        self.assertEqual(instruction_block([]), "")
        self.assertEqual(instructions_binding([]), [])


if __name__ == "__main__":
    unittest.main()
