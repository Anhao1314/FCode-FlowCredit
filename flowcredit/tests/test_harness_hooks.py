"""S2 hooks: events, matchers, ordering, blocking, rewrites, loop wiring."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    Done,
    EventBus,
    HarnessCore,
    HOOK_EVENTS,
    HookRunner,
    HookSpec,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
)


def echo_tool(calls: list[dict] | None = None) -> ToolSpec:
    def handler(args, _context):
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok(f"echo:{args['text']}")

    return ToolSpec(
        name="echo",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=handler,
    )


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)

    def stream(self, _request):
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


def run_core(provider, *, hooks=None, tools=None):
    bus = EventBus()
    core = HarnessCore(
        provider,
        ToolRegistry(tools if tools is not None else [echo_tool()]),
        bus=bus, hooks=hooks,
    )
    result = core.run([{"role": "user", "content": []}], system="s")
    return result, bus


class HookSpecTests(unittest.TestCase):
    def test_events_are_the_ported_ten(self) -> None:
        self.assertEqual(len(HOOK_EVENTS), 10)
        for expected in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
            self.assertIn(expected, HOOK_EVENTS)

    def test_unknown_event_and_empty_command_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HookSpec(event="AfterEverything", command="x")
        with self.assertRaises(ValueError):
            HookSpec(event="PreToolUse", command="  ")

    def test_matchers(self) -> None:
        self.assertTrue(HookSpec(event="PreToolUse", command="x").matches("Bash"))
        self.assertTrue(HookSpec(event="PreToolUse", command="x", matcher="*").matches(None))
        both = HookSpec(event="PreToolUse", command="x", matcher="Edit|Write")
        self.assertTrue(both.matches("Write"))
        self.assertFalse(both.matches("Bash"))
        self.assertFalse(both.matches(None))


class HookRunnerTests(unittest.TestCase):
    def test_managed_hooks_run_first_and_receive_json_stdin(self) -> None:
        seen: list[tuple[str, dict]] = []

        def execute(command, payload, _timeout):
            seen.append((command, json.loads(payload.decode())))
            return 0, "", ""

        runner = HookRunner([
            HookSpec(event="PreToolUse", command="user-check", source="user"),
            HookSpec(event="PreToolUse", command="org-check", source="managed"),
        ], execute=execute)
        outcome = runner.run("PreToolUse", {"tool_name": "Bash",
                                            "tool_input": {"command": "ls"}},
                             tool="Bash")
        self.assertFalse(outcome.blocked)
        self.assertEqual([name for name, _ in seen], ["org-check", "user-check"])
        self.assertEqual(seen[0][1]["event"], "PreToolUse")
        self.assertEqual(seen[0][1]["tool_input"]["command"], "ls")

    def test_exit_two_blocks_and_stops_later_hooks(self) -> None:
        calls: list[str] = []

        def execute(command, _payload, _timeout):
            calls.append(command)
            if command == "guard":
                return 2, "", "no writes after 18:00"
            return 0, "", ""

        runner = HookRunner([
            HookSpec(event="PreToolUse", command="guard", source="managed"),
            HookSpec(event="PreToolUse", command="later"),
        ], execute=execute)
        outcome = runner.run("PreToolUse", {}, tool="Bash")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.reason, "no writes after 18:00")
        self.assertEqual(calls, ["guard"])

    def test_json_block_and_snake_case_rewrite(self) -> None:
        def execute(_command, _payload, _timeout):
            return 0, json.dumps({
                "decision": "block", "reason": "policy",
                "updatedInput": {"command": "git status"},
            }), ""

        runner = HookRunner(
            [HookSpec(event="PreToolUse", command="guard")], execute=execute)
        outcome = runner.run("PreToolUse", {}, tool="Bash")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.reason, "policy")
        self.assertEqual(outcome.updated_input, {"command": "git status"})

    def test_timeout_is_recorded_not_fatal(self) -> None:
        def execute(_command, _payload, _timeout):
            raise subprocess.TimeoutExpired(cmd="slow", timeout=1)

        runner = HookRunner(
            [HookSpec(event="PreToolUse", command="slow")], execute=execute)
        outcome = runner.run("PreToolUse", {}, tool="Bash")
        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.results[0]["code"], "HOOK_TIMEOUT")

    def test_nonzero_exit_is_an_error_but_not_a_block(self) -> None:
        runner = HookRunner(
            [HookSpec(event="Stop", command="notify")],
            execute=lambda *_a: (1, "", "smtp down"),
        )
        outcome = runner.run("Stop", {})
        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.results[0]["code"], "HOOK_ERROR")
        self.assertIn("smtp down", outcome.results[0]["stderr"])

    def test_real_subprocess_roundtrip(self) -> None:
        runner = HookRunner([HookSpec(
            event="PreToolUse", matcher="Bash",
            command="cat >/dev/null; echo '{\"decision\": \"block\", "
                    "\"reason\": \"real shell\"}'")])
        outcome = runner.run("PreToolUse", {"tool_name": "Bash"}, tool="Bash")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.reason, "real shell")


class CoreHookTests(unittest.TestCase):
    def test_post_tool_hook_sees_the_result_and_can_feed_back(self) -> None:
        seen: list[dict] = []

        def execute(_command, payload, _timeout):
            body = json.loads(payload.decode())
            seen.append(body)
            return 2, "", "trim the output next time"

        hooks = HookRunner(
            [HookSpec(event="PostToolUse", matcher="echo", command="review")],
            execute=execute)
        result, bus = run_core(
            ScriptedProvider(
                [ToolUse("t1", "echo", {"text": "x"}), Done(stop_reason="tool_use")],
                [TextDelta("done"), Done()],
            ), hooks=hooks)
        self.assertTrue(result.completed)
        self.assertEqual(seen[0]["tool_response"]["content"], "echo:x")
        tool_result = result.messages[2]["content"][0]
        self.assertIn("[PostToolUse] trim the output next time", tool_result["content"])
        self.assertFalse(tool_result["is_error"])
        self.assertIn("hook.result", bus.types())

    def test_session_start_and_stop_fire_on_a_clean_run(self) -> None:
        events: list[str] = []

        def execute(command, _payload, _timeout):
            events.append(command)
            return 0, "", ""

        hooks = HookRunner([
            HookSpec(event="SessionStart", command="start"),
            HookSpec(event="Stop", command="stop"),
        ], execute=execute)
        result, _ = run_core(
            ScriptedProvider([TextDelta("hi"), Done()]), hooks=hooks)
        self.assertTrue(result.completed)
        self.assertEqual(events, ["start", "stop"])

    def test_no_hooks_means_no_hook_events(self) -> None:
        _result, bus = run_core(ScriptedProvider([TextDelta("hi"), Done()]))
        self.assertNotIn("hook.result", bus.types())


if __name__ == "__main__":
    unittest.main()
