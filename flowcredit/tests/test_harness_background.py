"""S4 background tasks: detached shells, incremental output, kill, tools."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    BackgroundLimitError,
    BackgroundTasks,
    Done,
    EventBus,
    HarnessCore,
    TextDelta,
    ToolRegistry,
    ToolUse,
    background_tools,
)


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)

    def stream(self, request):
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


class BackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.managers: list[BackgroundTasks] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            for task in manager.list():
                if task.status == "running":
                    manager.kill(task.task_id)
        self.tmp.cleanup()

    def manager(self, **kwargs) -> BackgroundTasks:
        kwargs.setdefault("id_factory", lambda number: f"bg{number}")
        tasks = BackgroundTasks(self.root, **kwargs)
        self.managers.append(tasks)
        return tasks

    def test_incremental_output_and_completion(self) -> None:
        tasks = self.manager()
        task = tasks.start("printf 'one\\n'; sleep 0.15; printf 'two\\n'")
        self.assertEqual(task.status, "running")
        self.assertTrue(task.output_path.exists())
        view = tasks.output(task.task_id, block=True, timeout_ms=3000)
        self.assertEqual(view["output"], "one\ntwo\n")
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["exit_code"], 0)
        self.assertTrue(view["eof"])
        self.assertEqual(view["next_offset"], len("one\ntwo\n"))
        # reading from the returned offset yields nothing new
        tail = tasks.output(task.task_id, offset=view["next_offset"])
        self.assertEqual(tail["output"], "")
        self.assertTrue(tail["eof"])

    def test_small_limit_returns_a_resumable_slice(self) -> None:
        tasks = self.manager()
        task = tasks.start("printf 'abcdef'")
        view = tasks.output(task.task_id, offset=0, limit=3,
                            block=True, timeout_ms=3000)
        self.assertEqual(view["output"], "abc")
        self.assertEqual(view["next_offset"], 3)
        rest = tasks.output(task.task_id, offset=view["next_offset"],
                            block=True, timeout_ms=3000)
        self.assertEqual(rest["output"], "def")
        self.assertTrue(rest["eof"])

    def test_failure_exit_code_is_visible(self) -> None:
        tasks = self.manager()
        task = tasks.start("exit 3")
        view = tasks.output(task.task_id, block=True, timeout_ms=3000)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["exit_code"], 3)
        self.assertTrue(view["eof"])

    def test_kill_terminates_a_running_task(self) -> None:
        tasks = self.manager()
        task = tasks.start("sleep 5")
        self.assertEqual(tasks.status(task.task_id).status, "running")
        killed = tasks.kill(task.task_id)
        self.assertEqual(killed["status"], "killed")
        view = tasks.output(task.task_id, block=True, timeout_ms=1000)
        self.assertTrue(view["eof"])
        self.assertEqual(view["status"], "killed")
        # killing an already-finished task is a no-op, not an error
        self.assertEqual(tasks.kill(task.task_id)["status"], "killed")

    def test_max_running_is_enforced(self) -> None:
        tasks = self.manager(max_running=1)
        first = tasks.start("sleep 5")
        with self.assertRaises(BackgroundLimitError):
            tasks.start("sleep 5")
        tasks.kill(first.task_id)
        second = tasks.start("printf ok")
        self.assertNotEqual(second.task_id, first.task_id)

    def test_unknown_task_is_a_key_error(self) -> None:
        tasks = self.manager()
        with self.assertRaises(KeyError):
            tasks.status("nope")
        with self.assertRaises(KeyError):
            tasks.output("nope")
        with self.assertRaises(KeyError):
            tasks.kill("nope")

    def test_empty_command_is_rejected(self) -> None:
        tasks = self.manager()
        with self.assertRaises(ValueError):
            tasks.start("   ")


class BackgroundToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = BackgroundTasks(
            pathlib.Path(self.tmp.name), id_factory=lambda number: f"bg{number}")

    def tearDown(self) -> None:
        for task in self.tasks.list():
            if task.status == "running":
                self.tasks.kill(task.task_id)
        self.tmp.cleanup()

    def test_tools_round_trip_through_the_loop(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "run_background", {"command": "printf 'hello'"}),
             Done(stop_reason="tool_use")],
            [ToolUse("t2", "task_output", {
                "task_id": "bg1", "block": True, "timeout_ms": 3000}),
             Done(stop_reason="tool_use")],
            [ToolUse("t3", "kill_shell", {"task_id": "bg1"}),
             Done(stop_reason="tool_use")],
            [TextDelta("done"), Done()],
        )
        bus = EventBus()
        core = HarnessCore(
            provider, ToolRegistry(background_tools(self.tasks)), bus=bus)
        result = core.run([{"role": "user", "content": []}], system="s")
        self.assertTrue(result.completed)
        contents = [
            block["content"]
            for message in result.messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        self.assertTrue(contents[0].startswith("started bg1"))
        self.assertEqual(contents[1], "hello")
        self.assertEqual(contents[2], "bg1: completed")
        types = bus.types()
        self.assertIn("background.start", types)
        self.assertIn("background.output", types)
        self.assertIn("background.kill", types)

    def test_unknown_task_becomes_a_tool_error(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "task_output", {"task_id": "missing"}),
             Done(stop_reason="tool_use")],
            [TextDelta("recovered"), Done()],
        )
        core = HarnessCore(
            provider, ToolRegistry(background_tools(self.tasks)))
        result = core.run([{"role": "user", "content": []}], system="s")
        tool_result = result.messages[2]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["code"], "TASK_UNKNOWN")

    def test_limit_becomes_a_tool_error(self) -> None:
        blocked = BackgroundTasks(
            pathlib.Path(self.tmp.name), max_running=1,
            id_factory=lambda number: f"bg{number}")
        first = blocked.start("sleep 5")
        try:
            provider = ScriptedProvider(
                [ToolUse("t1", "run_background", {"command": "sleep 5"}),
                 Done(stop_reason="tool_use")],
                [TextDelta("recovered"), Done()],
            )
            core = HarnessCore(
                provider, ToolRegistry(background_tools(blocked)))
            result = core.run([{"role": "user", "content": []}], system="s")
            tool_result = result.messages[2]["content"][0]
            self.assertEqual(tool_result["code"], "TASK_LIMIT")
        finally:
            blocked.kill(first.task_id)


if __name__ == "__main__":
    unittest.main()
