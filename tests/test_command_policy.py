"""The host command policy must accept the commands real projects already use.

The original whitelist covered Python scripts and pytest/unittest. A project
that verifies with `npm test`, `cargo test`, `go test ./...`, `make test` or
`uv run pytest` could be imported but never independently verified, which made
"real project Work" narrower than it claimed. These tests pin the expanded
defaults while keeping the argv-only, no-shell contract intact.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.tools.general.commands import CommandPolicy  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402


COMMON_CHECKS = (
    ("npm", ["test"]),
    ("pnpm", ["test"]),
    ("yarn", ["test"]),
    ("npx", ["vitest", "run"]),
    ("bun", ["test"]),
    ("deno", ["test"]),
    ("uv", ["run", "pytest", "-q"]),
    ("poetry", ["run", "pytest", "-q"]),
    ("pip", ["--version"]),
    ("ruff", ["check", "."]),
    ("mypy", ["src"]),
    ("tox", ["-q"]),
    ("cargo", ["test"]),
    ("go", ["test", "./..."]),
    ("make", ["test"]),
    ("cmake", ["--build", "build"]),
    ("ctest", ["--output-on-failure"]),
    ("ninja", ["-C", "build"]),
    ("mvn", ["-q", "test"]),
    ("gradle", ["test"]),
    ("dotnet", ["test"]),
    ("bundle", ["exec", "rake", "test"]),
    ("composer", ["test"]),
    ("swift", ["test"]),
)


class ExpandedCommandPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "workspace")

    def test_common_project_test_commands_are_whitelisted(self) -> None:
        policy = CommandPolicy()
        for program, args in COMMON_CHECKS:
            with self.subTest(program=program):
                rule, argv = policy.resolve(program, args, self.sandbox)
                self.assertFalse(rule.readonly, f"{program} can write and must stay consequential")
                self.assertEqual(argv, [program, *args])

    def test_the_default_is_still_argv_only(self) -> None:
        policy = CommandPolicy()
        with self.assertRaises(AgentError) as ctx:
            policy.resolve("npm", ["test; rm -rf ."], self.sandbox)
        self.assertEqual(ctx.exception.code, "COMMAND_SHELL_SYNTAX")

    def test_a_shell_is_still_not_a_verification_command(self) -> None:
        policy = CommandPolicy()
        with self.assertRaises(AgentError) as ctx:
            policy.resolve("sh", ["-c", "exit 0"], self.sandbox)
        self.assertEqual(ctx.exception.code, "COMMAND_NOT_WHITELISTED")

    def test_an_npm_verification_survives_assembly(self) -> None:
        """The assembly check uses the same policy the dispatcher will use."""
        spec = {
            "verification": {
                "name": "npm test",
                "command": "npm",
                "args": ["test"],
                "expectExitCode": 0,
                "timeoutSeconds": 120,
            },
        }
        world = GENERAL_DOMAIN.create_world(
            spec=spec,
            sandbox=self.sandbox,
        )
        tools = GENERAL_DOMAIN.build_tools(world)
        self.assertTrue(any(tool.name == "run_command" for tool in tools))


if __name__ == "__main__":
    unittest.main()
