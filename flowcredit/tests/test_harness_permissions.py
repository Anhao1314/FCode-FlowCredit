"""S2 permissions: rule syntax, matching, precedence, modes, pipeline.

Behavioral contract ported from the dissected harness; no network, no model.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    CheckDecision,
    Done,
    EventBus,
    HarnessCore,
    HookRunner,
    HookSpec,
    PermissionPipeline,
    PermissionPolicy,
    PermissionRule,
    RuleSyntaxError,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
    default_content,
)


def edit_tool(calls: list[dict] | None = None) -> ToolSpec:
    def handler(args, _context):
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok(f"wrote {args.get('path')}: {args.get('content', '')}")

    return ToolSpec(
        name="edit_file",
        description="edit a file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path"],
        },
        handler=handler,
    )


class RuleSyntaxTests(unittest.TestCase):
    def test_parse_tool_and_pattern(self) -> None:
        rule = PermissionRule.parse("Bash(git:*)", behavior="allow")
        self.assertEqual((rule.tool, rule.pattern, rule.behavior),
                         ("Bash", "git:*", "allow"))
        bare = PermissionRule.parse("Edit", behavior="deny", source="managed")
        self.assertEqual((bare.tool, bare.pattern, bare.source),
                         ("Edit", "", "managed"))

    def test_invalid_rules_are_typed(self) -> None:
        with self.assertRaises(RuleSyntaxError):
            PermissionRule.parse("Bash(git:*", behavior="allow")
        with self.assertRaises(RuleSyntaxError):
            PermissionRule.parse("", behavior="allow")
        with self.assertRaises(RuleSyntaxError):
            PermissionRule(behavior="maybe", tool="Bash")
        with self.assertRaises(RuleSyntaxError):
            PermissionPolicy(mode="wild")

    def test_prefix_patterns_match_commands_not_strings(self) -> None:
        rule = PermissionRule.parse("Bash(git:*)", behavior="allow")
        self.assertTrue(rule.matches("Bash", "git status"))
        self.assertTrue(rule.matches("Bash", "git"))
        self.assertFalse(rule.matches("Bash", "gitk"))
        self.assertFalse(rule.matches("Edit", "git status"))
        glob = PermissionRule.parse("WebFetch(domain:*.github.com)", behavior="allow")
        self.assertTrue(glob.matches("WebFetch", "api.github.com"))

    def test_content_resolution(self) -> None:
        self.assertEqual(default_content("Bash", {"command": "ls -la"}), "ls -la")
        self.assertEqual(default_content("edit_file", {"path": "a.py"}), "a.py")
        self.assertEqual(
            default_content("WebFetch", {"url": "https://example.com/x"}),
            "example.com")


class PolicyTests(unittest.TestCase):
    def rule(self, text: str, behavior: str, source: str = "user") -> PermissionRule:
        return PermissionRule.parse(text, behavior=behavior, source=source)

    def test_deny_beats_allow_and_managed_beats_local(self) -> None:
        policy = PermissionPolicy(
            allow=[self.rule("Bash(git:*)", "allow", "local")],
            deny=[self.rule("Bash(git:*)", "deny", "managed")],
        )
        decision = policy.evaluate("Bash", {"command": "git push --force"})
        self.assertEqual(decision.behavior, "deny")
        self.assertEqual(decision.rule.source, "managed")

    def test_ask_beats_allow(self) -> None:
        policy = PermissionPolicy(
            allow=[self.rule("Bash(git:*)", "allow")],
            ask=[self.rule("Bash(git:*)", "ask")],
        )
        self.assertEqual(
            policy.evaluate("Bash", {"command": "git push"}).behavior, "ask")

    def test_default_mode_asks_then_denies_without_approver(self) -> None:
        policy = PermissionPolicy()
        decision = policy.resolve("Bash", {"command": "ls"})
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("no approver", decision.reason)

    def test_approver_only_sees_ask(self) -> None:
        seen: list[str] = []

        def approver(tool, args, reason):
            seen.append(tool)
            return True

        policy = PermissionPolicy(
            allow=[self.rule("Edit", "allow")], approver=approver)
        self.assertEqual(policy.resolve("Edit", {"path": "a"}), 
                         policy.evaluate("Edit", {"path": "a"}))
        self.assertEqual(seen, [])
        self.assertTrue(policy.resolve("Bash", {"command": "ls"}).allowed)
        self.assertEqual(seen, ["Bash"])

    def test_rejected_approval_is_a_deny(self) -> None:
        policy = PermissionPolicy(approver=lambda *_: False)
        decision = policy.resolve("Bash", {"command": "rm -rf /"})
        self.assertEqual(decision.behavior, "deny")
        self.assertEqual(decision.reason, "operator denied")

    def test_accept_edits_mode_covers_write_tools_only(self) -> None:
        policy = PermissionPolicy(mode="acceptEdits")
        self.assertTrue(policy.evaluate("edit_file", {"path": "a"}).allowed)
        self.assertTrue(policy.evaluate("run_command", {"command": "make"}).allowed)
        self.assertEqual(policy.evaluate("Bash", {"command": "ls"}).behavior, "ask")

    def test_bypass_permissions_is_gated(self) -> None:
        open_policy = PermissionPolicy(mode="bypassPermissions")
        self.assertTrue(open_policy.evaluate("Bash", {"command": "ls"}).allowed)
        gated = PermissionPolicy(mode="bypassPermissions", bypass_available=False)
        self.assertEqual(gated.evaluate("Bash", {"command": "ls"}).behavior, "ask")

    def test_plan_mode_requires_approval(self) -> None:
        policy = PermissionPolicy(mode="plan")
        self.assertEqual(policy.evaluate("edit_file", {"path": "a"}).behavior, "ask")

    def test_shed_keeps_managed_rules(self) -> None:
        policy = PermissionPolicy(allow=[
            self.rule("Bash(git:*)", "allow", "managed"),
            self.rule("Bash(make:*)", "allow", "user"),
            self.rule("Bash(ls)", "allow", "local"),
        ])
        self.assertEqual(policy.shed_allow_rules(1), 2)
        self.assertTrue(policy.evaluate("Bash", {"command": "git pull"}).allowed)
        self.assertEqual(policy.evaluate("Bash", {"command": "make"}).behavior, "ask")
        self.assertEqual(policy.shed_allow_rules(5), 0)


class PipelineTests(unittest.TestCase):
    def test_pre_tool_hook_block_prevents_execution(self) -> None:
        calls: list[dict] = []
        hooks = HookRunner(
            [HookSpec(event="PreToolUse", matcher="edit_file",
                      command="guard")],
            execute=lambda *_a: (2, "", "denied by org hook"),
        )
        pipeline = PermissionPipeline(
            PermissionPolicy(mode="bypassPermissions"), hooks)
        decision = pipeline.check("edit_file", {"path": "a"})
        self.assertFalse(decision.allow)
        self.assertIn("org hook", decision.reason)
        self.assertEqual(calls, [])

    def test_non_matching_hook_is_skipped(self) -> None:
        hooks = HookRunner(
            [HookSpec(event="PreToolUse", matcher="Bash", command="guard")],
            execute=lambda *_a: (2, "", "nope"),
        )
        pipeline = PermissionPipeline(
            PermissionPolicy(mode="bypassPermissions"), hooks)
        self.assertTrue(pipeline.check("edit_file", {"path": "a"}).allow)

    def test_hook_rewrite_reaches_the_handler(self) -> None:
        calls: list[dict] = []
        hooks = HookRunner(
            [HookSpec(event="PreToolUse", matcher="edit_file", command="redact")],
            execute=lambda *_a: (
                0, '{"updatedInput": {"path": "safe.py", "content": "ok"}}', ""),
        )
        pipeline = PermissionPipeline(PermissionPolicy(mode="bypassPermissions"), hooks)
        bus = EventBus()
        core = HarnessCore(
            ScriptedProvider(
                [ToolUse("t1", "edit_file", {"path": "evil.py"}),
                 Done(stop_reason="tool_use")],
                [TextDelta("done"), Done()],
            ),
            ToolRegistry([edit_tool(calls)]),
            checker=pipeline.check,
            bus=bus,
        )
        result = core.run([{"role": "user", "content": []}], system="s")
        self.assertTrue(result.completed)
        self.assertEqual(calls, [{"path": "safe.py", "content": "ok"}])
        check = next(e for e in bus.history if e.type == "tool.check")
        self.assertTrue(check.payload["updated"])
        self.assertIn("wrote safe.py", result.messages[2]["content"][0]["content"])

    def test_deny_rule_stops_the_tool(self) -> None:
        calls: list[dict] = []
        policy = PermissionPolicy(deny=[PermissionRule.parse(
            "edit_file(*.py)", behavior="deny", source="managed")])
        core = HarnessCore(
            ScriptedProvider(
                [ToolUse("t1", "edit_file", {"path": "secret.py"}),
                 Done(stop_reason="tool_use")],
                [TextDelta("ok"), Done()],
            ),
            ToolRegistry([edit_tool(calls)]),
            checker=PermissionPipeline(policy).check,
        )
        result = core.run([{"role": "user", "content": []}], system="s")
        tool_result = result.messages[2]["content"][0]
        self.assertEqual(tool_result["code"], "TOOL_DENIED")
        self.assertEqual(calls, [])


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)

    def stream(self, _request):
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


if __name__ == "__main__":
    unittest.main()
