"""Capability baseline: edit_file, run_command, and the playwright import.

All offline. run_command executes real subprocesses (python3/git/…) inside a
temporary workspace — that is the honest way to test a terminal — but nothing
touches a network, and the timeout case uses a script that sleeps far past a
0.5-second deadline.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError, PLAN_VERSION, Task  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.base import validate_arguments  # noqa: E402
from flowagent.tools.general.commands import (  # noqa: E402
    CommandPolicy,
    CommandRunner,
    load_command_config,
)
from flowagent.tools.general.planner import GeneralPlanner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import GENERAL_TOOLS, build_general_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402

PLAYWRIGHT_MOCK = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_playwright_mock.py"
HAVE_PY3 = shutil.which("python3") is not None
HAVE_GIT = shutil.which("git") is not None


def _sandbox(tmp: str) -> WorkspaceSandbox:
    return WorkspaceSandbox(pathlib.Path(tmp) / "ws")


class EditFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = _sandbox(self._tmp.name)
        self.sandbox.write("app.py", "def one():\n    return 1\n\n\ndef two():\n    return 2\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unique_anchor_edit_commits_and_receipts_hashes(self) -> None:
        record = self.sandbox.edit("app.py", "    return 2", "    return 22")
        self.assertEqual(record["lines_added"], 1)
        self.assertEqual(record["lines_removed"], 1)
        self.assertTrue(record["commit"], "an edit is a change and must commit")
        self.assertNotEqual(record["sha256_before"], record["sha256_after"])
        self.assertIn("return 22", self.sandbox.read("app.py")["content"])

    def test_ambiguous_anchor_is_refused_with_candidate_lines(self) -> None:
        self.sandbox.write("dup.txt", "same\nsame\nsame\n")
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.edit("dup.txt", "same", "other")
        self.assertEqual(ctx.exception.code, "SANDBOX_EDIT_ANCHOR_AMBIGUOUS")
        self.assertIn("lines [1, 2, 3]", ctx.exception.message)

    def test_missing_anchor_is_refused_with_line_hint(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.edit("app.py", "def one():\n    return 999", "x")
        self.assertEqual(ctx.exception.code, "SANDBOX_EDIT_ANCHOR_NOT_FOUND")
        self.assertIn("near line 1", ctx.exception.message)

    def test_missing_file_is_refused_edits_never_create(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.edit("ghost.py", "a", "b")
        self.assertEqual(ctx.exception.code, "SANDBOX_EDIT_TARGET_MISSING")

    def test_edit_path_jail_is_the_sandbox_jail(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.edit("../outside.txt", "a", "b")
        self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")

    def test_oversized_edit_is_refused_with_a_split_hint(self) -> None:
        body = "".join(f"line {i}\n" for i in range(1, 400))
        self.sandbox.write("big.txt", body)
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.edit("big.txt", body, "")
        self.assertEqual(ctx.exception.code, "SANDBOX_EDIT_TOO_LARGE")
        self.assertIn("split", ctx.exception.message)

    def test_preview_shows_the_diff_without_writing(self) -> None:
        before = self.sandbox.read("app.py")["sha256"]
        preview = self.sandbox.edit_preview("app.py", "    return 1", "    return 111")
        self.assertEqual(preview["additions"], 1)
        self.assertEqual(preview["removals"], 1)
        self.assertIn("+    return 111", preview["diff"])
        self.assertEqual(self.sandbox.read("app.py")["sha256"], before, "preview must not write")
        refused = self.sandbox.edit_preview("app.py", "    return 99", "x")
        self.assertIn("SANDBOX_EDIT_ANCHOR_NOT_FOUND", refused.get("error", ""))

    def test_deletion_via_empty_new_text(self) -> None:
        self.sandbox.write("note.md", "keep\nremove me\nalso keep\n")
        record = self.sandbox.edit("note.md", "remove me\n", "")
        self.assertEqual(record["lines_removed"], 1)
        self.assertEqual(self.sandbox.read("note.md")["content"], "keep\nalso keep\n")


class RunCommandPolicyTests(unittest.TestCase):
    """The argument policy: refusals happen before any process exists."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = _sandbox(self._tmp.name)
        self.sandbox.ensure()
        self.runner = CommandRunner(self.sandbox)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _denied(self, command: str, args: list, code: str) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.runner.run(command, args)
        self.assertEqual(ctx.exception.code, code)

    def test_off_whitelist_program_is_refused(self) -> None:
        self._denied("curl", ["https://example.com"], "COMMAND_NOT_WHITELISTED")
        self._denied("rm", ["-rf", "."], "COMMAND_NOT_WHITELISTED")
        # a path-shaped program is refused before the whitelist lookup
        self._denied("/bin/sh", ["-c", "echo"], "COMMAND_DENIED")

    def test_python_inline_code_is_refused(self) -> None:
        self._denied("python3", ["-c", "print(1)"], "COMMAND_INLINE_REFUSED")
        self._denied("node", ["-e", "console.log(1)"], "COMMAND_INLINE_REFUSED")
        self._denied("python3", ["-m", "http.server"], "COMMAND_INLINE_REFUSED")

    def test_python_script_must_live_in_the_workspace(self) -> None:
        self.sandbox.write("ok.py", "print('hi')")
        self._denied("python3", ["../outside.py"], "SANDBOX_PATH_DENIED")
        self._denied("python3", ["/tmp/spy.py"], "SANDBOX_PATH_DENIED")
        # a non-script file is refused on shape before the jail check
        self._denied("python3", ["/etc/passwd"], "COMMAND_DENIED")
        self._denied("python3", [], "COMMAND_DENIED")

    def test_shell_syntax_is_refused_in_any_argument(self) -> None:
        self._denied("cat", ["a;rm", "-rf"], "COMMAND_SHELL_SYNTAX")
        self._denied("cat", ["a && b"], "COMMAND_SHELL_SYNTAX")
        self._denied("cat", ["a | b"], "COMMAND_SHELL_SYNTAX")
        self._denied("cat", ["a > /etc/passwd"], "COMMAND_SHELL_SYNTAX")
        self._denied("grep", ["$(whoami)", "file"], "COMMAND_SHELL_SYNTAX")

    def test_git_network_and_rewrite_subcommands_are_refused(self) -> None:
        self._denied("git", ["push", "origin", "main"], "COMMAND_GIT_SUBCOMMAND_REFUSED")
        self._denied("git", ["reset", "--hard"], "COMMAND_GIT_SUBCOMMAND_REFUSED")
        self._denied("git", ["-C", "/tmp", "status"], "COMMAND_GIT_FLAG_REFUSED")

    def test_readonly_path_args_are_jailed(self) -> None:
        self._denied("cat", ["../../../etc/passwd"], "SANDBOX_PATH_DENIED")
        self._denied("find", [".."], "SANDBOX_PATH_DENIED")
        # grep: 第一个非 flag 参数是 pattern，不按路径校验；目标文件按路径校验
        self._denied("grep", ["pattern", "../../secret"], "SANDBOX_PATH_DENIED")

    def test_classification_readonly_vs_consequential(self) -> None:
        policy = CommandPolicy()
        self.assertFalse(policy.classify("ls", []))
        self.assertFalse(policy.classify("cat", ["x"]))
        self.assertFalse(policy.classify("git", ["status"]))
        self.assertFalse(policy.classify("git", ["log"]))
        self.assertTrue(policy.classify("git", ["add", "x"]))
        self.assertTrue(policy.classify("git", ["commit", "-m", "x"]))
        self.assertTrue(policy.classify("python3", ["x.py"]))
        self.assertIsNone(policy.classify("curl", []))


@unittest.skipUnless(HAVE_PY3, "python3 is required to run commands")
class RunCommandExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = _sandbox(self._tmp.name)
        self.runner = CommandRunner(self.sandbox)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_workspace_script_and_receipt(self) -> None:
        self.sandbox.write("hello.py", "import sys\nprint('out', 'ok')\nprint('err-ish', file=sys.stderr)\n")
        result = self.runner.run("python3", ["hello.py"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("out ok", result["output"])
        self.assertIn("err-ish", result["output"], "stderr merges into the captured output")
        self.assertIn("python3 hello.py", result["command"])
        self.assertFalse(result["readonly"])
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_nonzero_exit_is_a_confirmed_result_not_a_failure(self) -> None:
        self.sandbox.write("fail.py", "raise SystemExit(3)\n")
        result = self.runner.run("python3", ["fail.py"])
        self.assertEqual(result["exit_code"], 3)

    def test_timeout_is_an_unknown_outcome_and_kills_the_group(self) -> None:
        self.sandbox.write("sleep.py", "import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        with self.assertRaises(AgentError) as ctx:
            self.runner.run("python3", ["sleep.py"], timeout_seconds=1)
        self.assertEqual(ctx.exception.code, "COMMAND_TIMEOUT")
        self.assertEqual(ctx.exception.details.get("outcome"), "unknown")
        # The process group is dead: no sleeper survives the refusal.
        import subprocess as sp

        listing = sp.run(["pgrep", "-f", "sleep.py"], capture_output=True, text=True)
        self.assertEqual(listing.stdout.strip(), "", "the timed-out process must be killed")

    def test_output_is_capped_and_marked_truncated(self) -> None:
        self.sandbox.write("chatty.py", "for i in range(40000):\n    print('x' * 8)\n")
        result = self.runner.run("python3", ["chatty.py"], timeout_seconds=60)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["bytes"], 64 * 1024 + 8192)

    def test_timeout_parameter_bounds(self) -> None:
        self.sandbox.write("noop.py", "pass\n")
        with self.assertRaises(AgentError) as ctx:
            self.runner.run("python3", ["noop.py"], timeout_seconds=121)
        self.assertEqual(ctx.exception.code, "COMMAND_TIMEOUT_INVALID")

    @unittest.skipUnless(HAVE_GIT, "git is required")
    def test_git_commit_through_the_terminal_lands_in_the_sandbox_repo(self) -> None:
        # An out-of-band change (not via write_file, which auto-commits):
        # the terminal sees it with git status and can commit it.
        self.sandbox.ensure()
        (self.sandbox.root / "tracked.txt").write_text("content\n", encoding="utf-8")
        status = self.runner.run("git", ["status"])
        self.assertIn("tracked.txt", status["output"])
        self.runner.run("git", ["add", "tracked.txt"])
        result = self.runner.run("git", ["commit", "-m", "via terminal"])
        self.assertEqual(result["exit_code"], 0)
        status = self.runner.run("git", ["status"])
        self.assertIn("nothing to commit", status["output"])
        self.assertFalse(status["readonly"], "git status is a read; classify() says so")
        self.assertTrue(self.runner.policy.classify("git", ["commit", "-m", "x"]))


class RunCommandToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = _sandbox(self._tmp.name)
        from flowagent.tools.general.fetch import UrlFetcher

        self.registry = ToolRegistry(build_general_tools(self.sandbox, UrlFetcher()))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_registration_order_pins_the_baseline_three(self) -> None:
        self.assertEqual(tuple(self.registry.names), GENERAL_TOOLS)
        effects = {name: self.registry.get(name).effect for name in self.registry.names}
        self.assertEqual(effects["edit_file"], "write")
        self.assertEqual(effects["run_command"], "write")
        self.assertEqual(effects["fetch_url"], "network")

    def test_gate_validates_run_command_arguments(self) -> None:
        spec = self.registry.get("run_command")
        validate_arguments(spec, {"command": "python3", "args": ["x.py"], "timeout_seconds": 30})
        with self.assertRaises(AgentError):
            validate_arguments(spec, {"command": "python3", "args": ["x.py"], "timeout_seconds": 300})
        with self.assertRaises(AgentError):
            validate_arguments(spec, {"command": "python3", "args": ["ok", 5]})

    def test_edit_file_new_text_is_optional_so_deletion_is_expressible(self) -> None:
        spec = self.registry.get("edit_file")
        validate_arguments(spec, {"path": "a.txt", "old_text": "x"})
        with self.assertRaises(AgentError):
            validate_arguments(spec, {"path": "a.txt", "old_text": "x", "new_text": 5})




class AuthoringProvider:
    """Stands in for a real model: proposes the plan's step, authors its own
    content. The milestone fixture must never appear in what it writes."""

    name = "authoring-test"
    model = "authoring-test-v1"

    DRAFT = "\n".join([
        "def greet():",
        "    print('hello')",
        "    print('world')  # first draft: forgets the third line",
        "",
        "if __name__ == '__main__':",
        "    greet()",
        "",
    ])
    FIXED = "\n".join([
        "def greet():",
        "    print('hello')",
        "    print('world')",
        "    print('again')  # fixed: three lines",
        "",
        "if __name__ == '__main__':",
        "    greet()",
        "",
    ])

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def propose(self, context):
        self.contexts.append(context)
        todo = next(
            (t for t in (context.get("plan") or []) if t.get("status") in ("pending", "active")),
            None,
        )
        if todo is None:
            raise AgentError("POLICY_NO_ACTIONS", "plan exhausted")
        arguments = dict(todo.get("arguments") or {})
        if todo["tool"] == "write_file" and not str(arguments.get("content") or "").strip():
            arguments["content"] = self.DRAFT
        if todo["tool"] == "edit_file" and not str(arguments.get("old_text") or "").strip():
            arguments["old_text"] = "    print('world')  # first draft: forgets the third line"
            arguments["new_text"] = "    print('world')\n    print('again')  # fixed: three lines"
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [{
                "id": todo["id"], "tool": todo["tool"], "arguments": arguments,
            }]},
            provider=self.name,
            model=self.model,
            tokens=10,
            cost_microusd=0,
            todo_id=todo["id"],
        )


class BlueprintHonestyTests(unittest.TestCase):
    """The blueprint states the shape of the work; the proposer authors content.

    Regression: the plan used to pre-fill the first draft and the edit anchors
    with this checkout's fibonacci fixture, so a *real* delegation executed the
    fixture instead of the operator's task (observed on a real Kimi run whose
    objective was "print 1..5" and whose file was the demo draft).
    """

    def test_the_blueprint_carries_intent_not_authored_content(self) -> None:
        plan = GeneralPlanner(["https://example.com"], mode="script").decompose("打印 1..5")
        by_id = {todo.id: todo for todo in plan.todos}
        self.assertNotIn("content", by_id["t1"].arguments)
        self.assertEqual(set(by_id["t3"].arguments), {"path"})
        # …and no fixture symbol survives in the planner module at all.
        planner_module = sys.modules[GeneralPlanner.__module__]
        source = pathlib.Path(planner_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SCRIPT_BUG", source)
        self.assertNotIn("_first_draft", source)
        self.assertNotIn("range(n - 1)", source)

    def test_the_proposers_own_content_is_what_lands_on_disk(self) -> None:
        provider = AuthoringProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = _sandbox(tmp)
            from flowagent.tools.general.fetch import UrlFetcher

            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "script", "scriptPath": "greet.py"},
                sandbox=sandbox,
                fetcher=UrlFetcher(),
                command_runner=CommandRunner(sandbox),
            )
            scenario = get_scenario("general-research")
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            harness = Harness(
                store=store,
                registry=ToolRegistry(world.build_tools()),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: world.verify_observation(observation),
            )
            task = harness.create(
                task_id="task-authoring",
                objective="写一个脚本打印三行问候语并运行，把输出写进 results.md",
                principal="test",
                binding={"scenario": scenario.key, "mode": "script"},
                allowed_tools=list(scenario.allowed_tools),
                grant=scenario.grant,
            )
            task = Loop(
                harness,
                # the product path: the world carries mode/paths, the planner reads them
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=scenario.max_rounds,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)

            self.assertEqual(task.status, "succeeded", task.failure)
            script = sandbox.read("greet.py")["content"]
            self.assertIn("print('hello')", script)
            self.assertIn("print('again')", script)   # the proposer's own fix applied
            self.assertNotIn("fib", script)
            self.assertNotIn("range(n - 1)", script)
            results = sandbox.read("results.md")["content"]
            self.assertIn("print('again')", results)
            self.assertNotIn("fib(10)", results, "the report files what ran, not a fixture conclusion")


@unittest.skipUnless(HAVE_PY3, "python3 executes the script blueprint")
class ScriptDelegationTests(unittest.TestCase):
    """The milestone delegation, offline: write → run → fix → re-run → file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = _sandbox(self._tmp.name)
        from flowagent.tools.general.fetch import UrlFetcher

        self.world = GENERAL_DOMAIN.create_world(
            spec={"mode": "script", "scriptPath": "fib.py"},
            sandbox=self.sandbox,
            fetcher=UrlFetcher(),
            command_runner=CommandRunner(self.sandbox),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _harness(self) -> Harness:
        scenario = get_scenario("general-research")
        store = TaskStore(pathlib.Path(self._tmp.name) / "t.sqlite")
        harness = Harness(
            store=store,
            registry=ToolRegistry(self.world.build_tools()),
            provider=MockProvider(policy="research"),
            world=self.world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: self.world.verify_observation(observation),
        )
        return harness, scenario

    def test_full_script_delegation_succeeds_with_verified_file_states(self) -> None:
        harness, scenario = self._harness()
        recorder = TraceRecorder(harness.store, provider_name="offline-mock", model_name="m")
        task = harness.create(
            task_id="task-script",
            objective="在 workspace 写一个 fibonacci 脚本并运行，把输出写进 results.md",
            principal="test",
            binding={"scenario": scenario.key, "mode": "script"},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        planner = GENERAL_DOMAIN.make_planner(self.world)
        task = Loop(
            harness, planner=planner, max_rounds=scenario.max_rounds,
            observer=recorder, result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)

        self.assertEqual(task.status, "succeeded", task.failure)
        # The bug was really there and really fixed: first run short, second run right.
        fib = self.sandbox.read("fib.py")["content"]
        self.assertIn("range(n)", fib)
        self.assertNotIn("range(n - 1)", fib)
        results = self.sandbox.read("results.md")["content"]
        self.assertIn("fib(10) = 34", results, "the first (buggy) run output is filed honestly")
        self.assertIn("fib(10) = 55", results, "the fixed run output is filed")
        self.assertIn("range(n - 1)", results, "the fix is documented")
        # The verified results: one entry per write observation. results.md
        # verifies against current disk; fib.py (edited later) verifies
        # against the git state its receipt names.
        entries = task.result.get("results") or [task.result]
        by_path = {entry["path"]: entry for entry in entries}
        self.assertEqual(by_path["results.md"]["sha256"], self.sandbox.read("results.md")["sha256"])
        self.assertIn(task.result["verification"], ("file-state-recomputed", "git-history-recomputed"))
        # Git history: script write, edit fix, results write — one commit each.
        import subprocess as sp

        log = sp.run(["git", "log", "--oneline"], cwd=str(self.sandbox.root),
                     capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 3, log)
        trace = recorder.build(task)
        verify_trace(trace)
        events = [event["type"] for event in harness.store.events(task.id)]
        self.assertEqual(events.count("tool_succeeded"), 5)
        harness.store.close()

    def test_timeout_in_the_blueprint_ends_unknown_and_never_reruns(self) -> None:
        # The same delegation, but the runner's every command times out.
        class ExplodingRunner(CommandRunner):
            def run(self, command, args, *, timeout_seconds=None):
                raise AgentError(
                    "COMMAND_TIMEOUT",
                    "dispatched and never answered",
                    details={"outcome": "unknown"},
                )

        self.world.command_runner = ExplodingRunner(self.sandbox)
        harness, scenario = self._harness()
        task = harness.create(
            task_id="task-script-unknown",
            objective="脚本委托（超时路径）",
            principal="test",
            binding={"scenario": scenario.key, "mode": "script"},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        planner = GENERAL_DOMAIN.make_planner(self.world)
        task = Loop(
            harness, planner=planner, max_rounds=6,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.failure["code"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(task.failure["cause"], "COMMAND_TIMEOUT")
        events = harness.store.events(task.id)
        self.assertIn("tool_unknown", [event["type"] for event in events])
        run_starts = [
            event for event in events
            if event["type"] == "tool_started"
            and (event["payload"].get("detail") or {}).get("tool") == "run_command"
        ]
        self.assertEqual(len(run_starts), 1, "dispatched once, never re-sent")
        harness.store.close()


class CommandConfigTests(unittest.TestCase):
    def test_missing_file_is_the_default_policy(self) -> None:
        policy = load_command_config("/nonexistent/commands.json")
        self.assertFalse(policy.classify("ls", []))

    def test_extension_is_add_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "commands.json"
            path.write_text(json.dumps({
                "commands": [
                    {"program": "ruff", "readonly": True, "argPolicy": "free"},
                ]
            }), encoding="utf-8")
            policy = load_command_config(path)
        self.assertFalse(policy.classify("ruff", []))
        self.assertTrue(policy.classify("python3", ["x.py"]), "defaults survive")

            # a config may not weaken a built-in rule
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "commands.json"
            path.write_text(json.dumps({
                "commands": [
                    {"program": "git", "readonly": True, "argPolicy": "free"},
                ]
            }), encoding="utf-8")
            with self.assertRaises(AgentError) as ctx:
                load_command_config(path)
            self.assertEqual(ctx.exception.code, "COMMAND_CONFIG_INVALID")

    def test_bad_entries_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "commands.json"
            path.write_text(json.dumps({"commands": [{"program": "../evil"}]}), encoding="utf-8")
            with self.assertRaises(AgentError):
                load_command_config(path)


class PlaywrightBridgeSmokeTests(unittest.TestCase):
    """The browser piece: the bridge consumes playwright-mcp over stdio.

    Runs the mock server as a real subprocess — the same wire protocol the
    real ``npx @playwright/mcp --stdio`` speaks. What is proven here is the
    *import and classification* contract; browser automation itself is not
    executed (no browser exists in the mock).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = pathlib.Path(self._tmp.name) / "mcp.json"
        self.config_path.write_text(json.dumps({"servers": {
            "playwright": {
                "command": sys.executable,
                "args": [str(PLAYWRIGHT_MOCK)],
                "timeout": 5.0,
            }
        }}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bridge(self):
        from flowagent.tools.mcp.bridge import McpBridge

        bridge = McpBridge(self.config_path)
        bridge.connect()
        self.addCleanup(bridge.disconnect)
        return bridge

    def test_playwright_tools_import_namespaced_and_consequential(self) -> None:
        bridge = self._bridge()
        statuses = bridge.statuses()
        self.assertTrue(statuses[0]["connected"], statuses[0].get("reason"))
        expected = {
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_snapshot",
        }
        self.assertEqual(set(statuses[0]["tools"]), expected)
        # The mock's snapshot self-describes as a read; the host never
        # trusted that, and no readonlyTools were configured — so ALL of the
        # browser tools are consequential (approval-gated, write effect).
        self.assertEqual(set(bridge.approval_names()), expected)
        effects = {name: spec.effect for name, spec in bridge.specs_map().items()}
        self.assertEqual(set(effects.values()), {"write"})

    def test_url_and_selector_params_are_gate_validated(self) -> None:
        bridge = self._bridge()
        navigate = bridge.specs_map()["mcp__playwright__browser_navigate"]
        validate_arguments(navigate, {"url": "https://example.com/"})
        with self.assertRaises(AgentError):
            validate_arguments(navigate, {"url": 5})
        with self.assertRaises(AgentError):
            validate_arguments(navigate, {"url": "https://example.com/", "extra": 1})

    def test_browser_call_round_trips_through_the_bridge(self) -> None:
        bridge = self._bridge()
        result = bridge.call("playwright", "browser_navigate", {"url": "https://example.com/"})
        self.assertIn("navigated to https://example.com/", result["text"])
        self.assertEqual(result["server"], "playwright")
        self.assertFalse(result["is_error"])

    def test_host_can_mark_a_browser_tool_readonly(self) -> None:
        self.config_path.write_text(json.dumps({"servers": {
            "playwright": {
                "command": sys.executable,
                "args": [str(PLAYWRIGHT_MOCK)],
                "timeout": 5.0,
                "readonlyTools": ["browser_snapshot"],
            }
        }}), encoding="utf-8")
        bridge = self._bridge()
        snapshot = bridge.specs_map()["mcp__playwright__browser_snapshot"]
        self.assertEqual(snapshot.effect, "read")
        self.assertNotIn("mcp__playwright__browser_snapshot", bridge.approval_names())
        navigate = bridge.specs_map()["mcp__playwright__browser_navigate"]
        self.assertEqual(navigate.effect, "write")


if __name__ == "__main__":
    unittest.main()
