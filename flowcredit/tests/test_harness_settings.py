"""S2 settings: four-source layering, permissions, hooks, warnings."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    HookSpec,
    load_settings,
    settings_paths,
)


def write(path: pathlib.Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "project"
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.managed = pathlib.Path(self.tmp.name) / "managed.json"
        self.root.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, **kwargs):
        return load_settings(self.root, home=self.home,
                             managed=self.managed, env={}, **kwargs)

    def test_missing_files_are_silently_ignored(self) -> None:
        loaded = self.load()
        self.assertEqual(loaded.sources, [])
        self.assertEqual(loaded.rules["allow"], [])
        self.assertEqual(loaded.warnings, [])
        self.assertEqual(loaded.mode, "default")
        self.assertTrue(loaded.bypass_available)

    def test_all_four_sources_load_in_policy_order(self) -> None:
        write(self.managed, {"permissions": {
            "allow": ["Bash(git:*)"], "deny": ["Bash(rm:*)"],
            "defaultMode": "acceptEdits"}})
        write(self.root / ".fcode" / "settings.json", {"permissions": {
            "allow": ["Edit"], "ask": ["Bash"]}})
        write(self.home / ".fcode" / "settings.json", {"permissions": {
            "allow": ["Read"]}})
        write(self.root / ".fcode" / "settings.local.json", {"permissions": {
            "allow": ["WebFetch(domain:*.github.com)"]}})
        loaded = self.load()
        self.assertEqual(loaded.sources, ["managed", "project", "user", "local"])
        self.assertEqual(
            [(rule.tool, rule.source) for rule in loaded.rules["allow"]],
            [("Bash", "managed"), ("Edit", "project"), ("Read", "user"),
             ("WebFetch", "local")])
        self.assertEqual(loaded.rules["deny"][0].source, "managed")
        self.assertEqual(loaded.mode, "acceptEdits")

        policy = loaded.policy()
        self.assertFalse(policy.evaluate("Bash", {"command": "rm -rf /"}).allowed)
        # project's un-scoped "Bash" ask beats the managed allow rule
        self.assertEqual(
            policy.evaluate("Bash", {"command": "git status"}).behavior, "ask")

    def test_managed_can_disable_bypass(self) -> None:
        write(self.managed, {"permissions": {
            "disableBypassPermissionsMode": "disable"}})
        loaded = self.load()
        self.assertFalse(loaded.bypass_available)
        decision = loaded.policy(mode="bypassPermissions").evaluate(
            "Bash", {"command": "ls"})
        self.assertEqual(decision.behavior, "ask")

    def test_hooks_parse_matcher_groups_and_timeouts(self) -> None:
        write(self.root / ".fcode" / "settings.json", {"hooks": {
            "PreToolUse": [
                {"matcher": "Bash|Edit", "hooks": [
                    {"type": "command", "command": "guard.sh", "timeout": 5},
                ]},
                {"hooks": [{"type": "command", "command": "always.sh"}]},
            ],
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [
                    {"type": "command", "command": "audit.sh", "timeout": 1.5},
                ]},
            ],
        }})
        loaded = self.load()
        self.assertEqual(len(loaded.hooks), 3)
        self.assertEqual(
            [(hook.event, hook.matcher, hook.timeout_ms) for hook in loaded.hooks],
            [("PreToolUse", "Bash|Edit", 5000), ("PreToolUse", "", 60_000),
             ("PostToolUse", "Edit", 1500)])
        runner = loaded.hook_runner(execute=lambda *_a: (0, "", ""))
        edit = runner.run("PreToolUse", {}, tool="Edit")
        self.assertEqual([item["command"] for item in edit.results],
                         ["guard.sh", "always.sh"])
        # the matcher-less group applies to every tool
        read = runner.run("PreToolUse", {}, tool="Read")
        self.assertEqual([item["command"] for item in read.results],
                         ["always.sh"])

    def test_bad_entries_warn_but_do_not_brick_the_run(self) -> None:
        write(self.root / ".fcode" / "settings.json", {
            "permissions": {"allow": ["Bash(git:*", 7], "ask": "Bash"},
            "hooks": {"PreToolUse": [
                {"hooks": [{"type": "webhook", "command": "x"}]},
                {"hooks": [{"command": ""}]},
            ], "Nope": []},
        })
        loaded = self.load()
        self.assertEqual(loaded.sources, ["project"])
        self.assertEqual(len(loaded.rules["ask"]), 1)
        self.assertEqual(loaded.hooks, [])
        summary = " | ".join(loaded.warnings)
        self.assertIn("unbalanced", summary)
        self.assertIn("unsupported hook type", summary)
        self.assertIn("unknown hook event", summary)

    def test_invalid_json_is_a_warning(self) -> None:
        (self.root / ".fcode").mkdir()
        (self.root / ".fcode" / "settings.json").write_text("{", encoding="utf-8")
        loaded = self.load()
        self.assertEqual(loaded.sources, [])
        self.assertIn("invalid JSON", loaded.warnings[0])

    def test_managed_path_can_come_from_env(self) -> None:
        write(self.managed, {"permissions": {"deny": ["Bash"]}})
        paths = settings_paths(self.root, home=self.home,
                               env={"FCODE_MANAGED_SETTINGS": str(self.managed)})
        self.assertEqual(paths["managed"], self.managed)
        loaded = load_settings(self.root, home=self.home,
                               env={"FCODE_MANAGED_SETTINGS": str(self.managed)})
        self.assertEqual(loaded.sources, ["managed"])
        self.assertEqual(loaded.rules["deny"][0].source, "managed")

    def test_policy_override_mode_and_approver(self) -> None:
        loaded = self.load()
        approved = loaded.policy(mode="default", approver=lambda *_: True)
        self.assertTrue(approved.resolve("Bash", {"command": "ls"}).allowed)
        denied = loaded.policy(approver=lambda *_: False)
        self.assertFalse(denied.resolve("Bash", {"command": "ls"}).allowed)


class HookSpecImportTests(unittest.TestCase):
    def test_public_surface_is_exported(self) -> None:
        from flowagent.harness import (  # noqa: F401
            LoadedSettings, PermissionPipeline, PermissionPolicy, RuleSyntaxError,
        )
        self.assertTrue(HookSpec(event="Stop", command="true").matches(None))


if __name__ == "__main__":
    unittest.main()
