"""S3 session tree: JSONL parentUuid chains, rewind/fork, file history."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    FileHistory,
    SessionStore,
    project_slug,
)


class SessionTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def store(self, **kwargs) -> SessionStore:
        return SessionStore(self.root, "/work/project", session_id="s1", **kwargs)

    def test_append_chain_and_reload(self) -> None:
        store = self.store()
        first = store.record_message("user", "hello")
        second = store.record_message("assistant", "hi")
        third = store.record_message("user", "next")
        self.assertIsNone(first["parentUuid"])
        self.assertEqual(second["parentUuid"], first["uuid"])
        self.assertEqual(third["parentUuid"], second["uuid"])
        self.assertTrue(all(isinstance(event["atMs"], int)
                            for event in store.events()))

        reloaded = self.store()
        self.assertEqual([event["uuid"] for event in reloaded.events()],
                         [first["uuid"], second["uuid"], third["uuid"]])
        self.assertEqual(
            [message["content"] for message in reloaded.messages()],
            ["hello", "hi", "next"])
        self.assertEqual(reloaded.chain()[-1]["uuid"], third["uuid"])
        self.assertIn("projects", str(reloaded.path))
        self.assertIn("work-project", str(reloaded.path))

    def test_rewind_is_a_non_destructive_fork(self) -> None:
        store = self.store()
        first = store.record_message("user", "hello")
        second = store.record_message("assistant", "hi")
        store.rewind(first["uuid"])
        forked = store.record_message("user", "take two")
        self.assertEqual(forked["parentUuid"], first["uuid"])
        self.assertEqual([event["uuid"] for event in store.children(first["uuid"])],
                         [second["uuid"], forked["uuid"]])
        self.assertEqual(
            [message["content"] for message in store.messages(forked["uuid"])],
            ["hello", "take two"])
        # the abandoned branch is still readable
        self.assertEqual(
            [message["content"] for message in store.messages(second["uuid"])],
            ["hello", "hi"])
        with self.assertRaises(KeyError):
            store.rewind("missing")

    def test_compact_boundary_is_a_flat_chain_safe_event(self) -> None:
        store = self.store()
        store.record_message("user", "hello")
        event = store.record_boundary(
            before_tokens=140_000, after_tokens=5_000,
            summary_sha256="a" * 64, trigger="auto")
        self.assertEqual(event["type"], "compact_boundary")
        self.assertEqual(event["beforeTokens"], 140_000)
        self.assertEqual(event["afterTokens"], 5_000)
        self.assertEqual(event["summarySha256"], "a" * 64)
        self.assertEqual(event["trigger"], "auto")
        self.assertIsInstance(event["atMs"], int)
        reloaded = self.store()
        self.assertEqual(reloaded.events()[-1]["type"], "compact_boundary")

    def test_corrupt_line_warns_and_the_rest_still_loads(self) -> None:
        store = self.store()
        store.record_message("user", "hello")
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        reloaded = self.store()
        self.assertEqual(len(reloaded.events()), 1)
        self.assertTrue(reloaded.warnings)
        self.assertIn("line 2", reloaded.warnings[0])

    def test_project_slug_is_filesystem_safe(self) -> None:
        slug = project_slug("/work/some project/v1")
        self.assertTrue(slug.endswith("work-some-project-v1"))
        self.assertNotIn("/", slug)
        self.assertNotIn(" ", slug)


class FileHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.history = FileHistory(self.root, session_id="s1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_dedupes_and_restores(self) -> None:
        source = self.root / "work" / "a.html"
        source.parent.mkdir()
        source.write_text("<svg/>", encoding="utf-8")
        first = self.history.snapshot(source, message_uuid="m1")
        second = self.history.snapshot(source, message_uuid="m2")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        blobs = [path for path in (self.history.blob_dir).rglob("*")
                 if path.is_file() and not path.name.endswith(".tmp")]
        self.assertEqual(len(blobs), 1)
        entries = self.history.entries()
        self.assertEqual([entry["messageUuid"] for entry in entries], ["m1", "m2"])
        self.assertTrue(all(entry["atMs"] >= 0 for entry in entries))

        target = self.root / "restored.html"
        self.history.restore(first, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "<svg/>")
        self.assertEqual(self.history.read(first), b"<svg/>")

    def test_missing_file_is_a_tombstone(self) -> None:
        ref = self.history.snapshot(self.root / "gone.txt", message_uuid="m1")
        self.assertEqual(ref, "")
        entry = self.history.entries()[-1]
        self.assertFalse(entry["exists"])
        self.assertEqual(entry["sha256"], "")

    def test_invalid_digest_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.history.read("../../etc/passwd")

    def test_manifest_is_jsonl_and_survives_restart(self) -> None:
        source = self.root / "a.txt"
        source.write_text("one", encoding="utf-8")
        self.history.snapshot(source)
        reloaded = FileHistory(self.root, session_id="s1")
        rows = reloaded.entries()
        self.assertEqual(len(rows), 1)
        line = reloaded.manifest_path.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(json.loads(line)["path"], str(source))


if __name__ == "__main__":
    unittest.main()
