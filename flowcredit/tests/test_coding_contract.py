"""W2 Coding Task Contract: explicit deliverables, forbidden surface, gates.

The contract turns "please fix the bug" into a declared, enforced agreement:

* ``normalize_coding_contract`` is fail-closed — unknown fields, absolute
  paths and unenforceable shapes are refused, and defaults are explicit;
* ``write_file``/``edit_file`` deny test, declared-forbidden and frozen
  dependency paths directly;
* the loop may only finalize ``succeeded`` through the world's completion
  gate, which re-hashes the forbidden surface against its build-time
  baseline — so a terminal command that bypassed the file tools is caught;
* runs without a contract keep exactly the old behaviour.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# Console modules import each other by bare name; the path stays for the whole
# process the way test_console_queue and server.py set it up.
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.domains.general import GeneralWorld  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.coding_contract import (  # noqa: E402
    coding_contract_binding,
    coding_contract_instruction,
    coding_contract_violations,
    coding_path_violation,
    contract_path_state,
    missing_coding_deliverables,
    normalize_coding_contract,
)
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402

VIEWER = ROOT / "viewer"


def _load_console_manager():
    spec = importlib.util.spec_from_file_location(
        "fcode_console_manager_contract", VIEWER / "console_manager.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _StubHarness:
    """Records finalize calls; the loop never touches a real kernel here.

    Only the attributes ``Loop.__init__`` reads are present: an empty registry
    keeps the memory summarizer table empty, the rest are inert defaults.
    """

    def __init__(self, world) -> None:
        self.world = world
        self.registry = mock.Mock()
        self.registry.names = []
        self.store = mock.Mock()
        self.state_fn = lambda: {}
        self.instruction = ""
        self.prompt_profile = None
        self.plan_extension = False
        self.calls: list[tuple[str, dict]] = []

    def finalize(self, task, status, **kwargs):
        self.calls.append((status, kwargs))
        task.status = status
        return task


class _Task:
    def __init__(self) -> None:
        self.status = "running"


class NormalizeTests(unittest.TestCase):
    def test_defaults_are_explicit_and_fail_closed(self) -> None:
        contract = normalize_coding_contract({"deliverables": ["REPORT.md"]})
        self.assertEqual(contract["deliverables"], ["REPORT.md"])
        self.assertFalse(contract["allow_test_edits"])
        self.assertFalse(contract["no_new_dependencies"])
        self.assertTrue(contract["require_verification"])
        # Test surface is forbidden even when the operator declared none.
        self.assertIn("tests/**", contract["forbidden_paths"])
        self.assertIn("test_*/**", contract["forbidden_paths"])
        self.assertIn("**/test_*", contract["forbidden_paths"])

    def test_declared_forbidden_and_dependency_freeze_extend(self) -> None:
        contract = normalize_coding_contract({
            "deliverables": ["out/app.py"],
            "forbiddenPaths": ["src/legacy/**", "src/legacy/**"],
            "noNewDependencies": True,
        })
        self.assertEqual(contract["declared_forbidden_paths"], ["src/legacy/**"])
        self.assertIn("src/legacy/**", contract["forbidden_paths"])
        self.assertIn("package.json", contract["forbidden_paths"])
        self.assertIn("pyproject.toml", contract["forbidden_paths"])

    def test_allow_test_edits_removes_default_test_ban(self) -> None:
        contract = normalize_coding_contract({
            "deliverables": ["x.py"], "allowTestEdits": True,
        })
        self.assertNotIn("tests/**", contract["forbidden_paths"])

    def test_require_verification_false_needs_no_deliverables(self) -> None:
        contract = normalize_coding_contract({"requireVerification": False})
        self.assertEqual(contract["deliverables"], [])
        self.assertEqual(contract["definition_of_done"], [])

    def test_rejects_unknown_fields_and_bad_shapes(self) -> None:
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": ["a"], "honor": True})
        with self.assertRaises(ValueError):
            normalize_coding_contract("fix the login bug")
        with self.assertRaises(ValueError):
            normalize_coding_contract({})
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": ["/etc/passwd"]})
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": ["../escape"]})
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": [42]})
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": None, "noNewDependencies": "yes"})
        with self.assertRaises(ValueError):
            normalize_coding_contract({"deliverables": [f"f{i}.txt" for i in range(41)]})

    def test_none_passes_through(self) -> None:
        self.assertIsNone(normalize_coding_contract(None))


class PathViolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = normalize_coding_contract({
            "deliverables": ["REPORT.md"],
            "forbiddenPaths": ["src/legacy/**"],
            "noNewDependencies": True,
        })
        self.loose = normalize_coding_contract({
            "deliverables": ["REPORT.md"], "allowTestEdits": True,
        })

    def test_test_paths_denied_by_default(self) -> None:
        for path in ("tests/test_app.py", "tests/app.py",
                     "src/test_helper.py", "app_test.py", "src/app.spec.ts"):
            reason = coding_path_violation(self.contract, path)
            self.assertIsNotNone(reason, path)
            self.assertIn("test", reason)

    def test_declared_and_dependency_patterns_denied(self) -> None:
        self.assertIn("forbidden pattern",
                      coding_path_violation(self.contract, "src/legacy/old.py"))
        self.assertIn("frozen",
                      coding_path_violation(self.contract, "package.json"))
        self.assertIn("frozen",
                      coding_path_violation(self.contract, "sub/requirements.txt"))

    def test_deliverable_and_normal_paths_allowed(self) -> None:
        for path in ("REPORT.md", "src/main.py", "docs/notes.md"):
            self.assertIsNone(coding_path_violation(self.contract, path), path)

    def test_allow_test_edits_lifts_only_the_test_ban(self) -> None:
        self.assertIsNone(coding_path_violation(self.loose, "tests/test_app.py"))
        self.assertIn("forbidden pattern",
                      coding_path_violation(self.contract, "src/legacy/old.py"))

    def test_no_contract_never_denies(self) -> None:
        self.assertIsNone(coding_path_violation(None, "tests/test_app.py"))


class ToolDenialTests(unittest.TestCase):
    """The file tools themselves refuse contract-forbidden paths."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")
        self.sandbox.ensure()
        self.sandbox.write("src/main.py", "print('hi')\n")
        self.sandbox.write("tests/test_main.py", "def test_ok():\n    assert True\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tools(self, contract):
        contract = normalize_coding_contract(contract) if contract else None
        specs = build_general_tools(
            self.sandbox, UrlFetcher(), coding_contract=contract)
        return {spec.name: spec for spec in specs}

    def test_write_to_tests_is_denied(self) -> None:
        tools = self._tools({"deliverables": ["REPORT.md"]})
        with self.assertRaises(AgentError) as ctx:
            tools["write_file"].handler({
                "path": "tests/test_main.py", "content": "pass\n"})
        self.assertEqual(ctx.exception.code, "CODING_CONTRACT_PATH_DENIED")

    def test_edit_to_tests_is_denied(self) -> None:
        tools = self._tools({"deliverables": ["REPORT.md"]})
        with self.assertRaises(AgentError) as ctx:
            tools["edit_file"].handler({
                "path": "tests/test_main.py",
                "old_text": "assert True", "new_text": "assert False",
            })
        self.assertEqual(ctx.exception.code, "CODING_CONTRACT_PATH_DENIED")

    def test_deliverable_write_succeeds(self) -> None:
        tools = self._tools({"deliverables": ["REPORT.md"]})
        record = tools["write_file"].handler({
            "path": "REPORT.md", "content": "# done\n"})
        self.assertTrue(record.get("computed"))

    def test_dependency_manifest_write_is_denied_when_frozen(self) -> None:
        tools = self._tools({
            "deliverables": ["REPORT.md"], "noNewDependencies": True})
        with self.assertRaises(AgentError) as ctx:
            tools["write_file"].handler({
                "path": "requirements.txt", "content": "flask==3.0\n"})
        self.assertEqual(ctx.exception.code, "CODING_CONTRACT_PATH_DENIED")

    def test_no_contract_keeps_tests_writable(self) -> None:
        tools = self._tools(None)
        record = tools["edit_file"].handler({
            "path": "tests/test_main.py",
            "old_text": "assert True", "new_text": "assert 1 == 1",
        })
        self.assertTrue(record.get("computed"))


class CompletionGateTests(unittest.TestCase):
    """Baseline + gate: deliverables and forbidden-surface drift at the end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")
        self.sandbox.ensure()
        self.sandbox.write("src/main.py", "print('v1')\n")
        self.sandbox.write("tests/test_main.py", "def test_ok():\n    assert True\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _world(self, contract) -> GeneralWorld:
        return GeneralWorld(
            {"mode": "open"}, sandbox=self.sandbox, coding_contract=contract)

    def test_missing_deliverable_is_incomplete(self) -> None:
        world = self._world({"deliverables": ["REPORT.md"]})
        self.assertEqual(
            missing_coding_deliverables(self.sandbox.root, world.coding_contract),
            ["REPORT.md"],
        )
        violations = world.completion_gate()
        self.assertEqual(violations, ["missing deliverable: REPORT.md"])
        self.sandbox.write("REPORT.md", "# done\n")
        self.assertEqual(world.completion_gate(), [])

    def test_terminal_bypass_is_caught_by_baseline(self) -> None:
        world = self._world({"deliverables": ["REPORT.md"]})
        self.sandbox.write("REPORT.md", "# done\n")
        self.assertEqual(world.completion_gate(), [])
        # A terminal command rewrote a test after the world was built.
        self.sandbox.write("tests/test_main.py", "def test_ok():\n    assert False\n")
        violations = world.completion_gate()
        self.assertEqual(
            violations, ["forbidden path changed: tests/test_main.py"])

    def test_created_forbidden_path_is_caught(self) -> None:
        world = self._world({"deliverables": ["REPORT.md"], "noNewDependencies": True})
        self.sandbox.write("REPORT.md", "# done\n")
        self.sandbox.write("package.json", '{"name": "sneak"}\n')
        self.assertIn(
            "forbidden path created: package.json", world.completion_gate())

    def test_declared_forbidden_baseline_covers_seeded_files(self) -> None:
        world = self._world({
            "deliverables": ["REPORT.md"], "forbiddenPaths": ["src/**"]})
        self.sandbox.write("REPORT.md", "# done\n")
        self.assertEqual(world.completion_gate(), [])
        self.sandbox.write("src/main.py", "print('v2')\n")
        self.assertIn(
            "forbidden path changed: src/main.py", world.completion_gate())

    def test_world_without_contract_gates_nothing(self) -> None:
        world = self._world(None)
        self.assertEqual(world.contract_baseline, {})
        self.assertEqual(world.completion_gate(), [])
        self.sandbox.write("tests/test_main.py", "# rewritten\n")
        self.assertEqual(world.completion_gate(), [])

    def test_violations_helper_merges_missing_and_drift(self) -> None:
        contract = normalize_coding_contract({
            "deliverables": ["REPORT.md", "OTHER.md"]})
        baseline = contract_path_state(self.sandbox.root, contract)
        self.assertIn("tests/test_main.py", baseline)
        self.sandbox.write("REPORT.md", "# done\n")
        self.sandbox.write("tests/test_main.py", "# tampered\n")
        violations = coding_contract_violations(
            self.sandbox.root, contract, baseline=baseline)
        self.assertIn("missing deliverable: OTHER.md", violations)
        self.assertIn("forbidden path changed: tests/test_main.py", violations)
        self.assertNotIn("REPORT.md", " ".join(violations))


class LoopGateTests(unittest.TestCase):
    """The loop may only finalize ``succeeded`` through the world's gate."""

    def _loop(self, world) -> tuple[Loop, _StubHarness]:
        harness = _StubHarness(world)
        return (
            Loop(harness, result_tools=("write_file",), memory_summarizers={}),
            harness,
        )

    def _succeed(self, loop, plan=None):
        return loop._succeed(
            _Task(),
            {"result": {"ok": True}, "verification": "runtime-recomputed"},
            plan or mock.Mock(progress=mock.Mock(return_value={})),
        )

    def test_missing_deliverable_fails_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            sandbox.ensure()
            world = GeneralWorld(
                {"mode": "open"}, sandbox=sandbox,
                coding_contract={"deliverables": ["REPORT.md"]})
            loop, harness = self._loop(world)
            task = self._succeed(loop)
            self.assertEqual(task.status, "failed")
            status, kwargs = harness.calls[-1]
            self.assertEqual(status, "failed")
            self.assertEqual(kwargs["failure"]["code"], "CODING_CONTRACT_INCOMPLETE")
            self.assertIn(
                "missing deliverable: REPORT.md", kwargs["failure"]["message"])
            self.assertEqual(kwargs["detail"]["violations"],
                             ["missing deliverable: REPORT.md"])

    def test_forbidden_drift_fails_violated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            sandbox.ensure()
            sandbox.write("tests/test_a.py", "x = 1\n")
            world = GeneralWorld(
                {"mode": "open"}, sandbox=sandbox,
                coding_contract={"deliverables": ["REPORT.md"]})
            sandbox.write("REPORT.md", "# done\n")
            sandbox.write("tests/test_a.py", "x = 2\n")
            loop, harness = self._loop(world)
            task = self._succeed(loop)
            self.assertEqual(task.status, "failed")
            self.assertEqual(
                harness.calls[-1][1]["failure"]["code"], "CODING_CONTRACT_VIOLATED")

    def test_no_contract_succeeds_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            sandbox.ensure()
            world = GeneralWorld({"mode": "open"}, sandbox=sandbox)
            loop, harness = self._loop(world)
            task = self._succeed(loop)
            self.assertEqual(task.status, "succeeded")
            statuses = [status for status, _ in harness.calls]
            self.assertEqual(statuses, ["verifying", "succeeded"])


class PromptAndBindingTests(unittest.TestCase):
    def test_instruction_states_the_enforced_policy(self) -> None:
        text = coding_contract_instruction(normalize_coding_contract({
            "deliverables": ["REPORT.md"],
            "forbiddenPaths": ["src/legacy/**"],
            "noNewDependencies": True,
            "definitionOfDone": ["所有用例通过"],
        }))
        self.assertIn("REPORT.md", text)
        self.assertIn("src/legacy/**", text)
        self.assertIn("禁止修改测试", text)
        self.assertIn("禁止引入或更新依赖清单", text)
        self.assertIn("CODING_CONTRACT_VIOLATED", text)
        self.assertEqual(coding_contract_instruction(None), "")

    def test_binding_is_chain_safe_projection(self) -> None:
        binding = coding_contract_binding(normalize_coding_contract({
            "deliverables": ["REPORT.md"], "allowTestEdits": True,
        }))
        self.assertEqual(binding["deliverables"], ["REPORT.md"])
        self.assertTrue(binding["allow_test_edits"])
        self.assertFalse(binding["no_new_dependencies"])
        self.assertNotIn("tests/**", binding["forbidden_paths"])
        for value in binding.values():
            self.assertNotIsInstance(value, float)
        self.assertIsNone(coding_contract_binding(None))


class ConsoleValidationTests(unittest.TestCase):
    """Creation-time refusal: an invalid contract is a 400, not a failed run."""

    @classmethod
    def setUpClass(cls) -> None:
        module = _load_console_manager()
        cls.manager = module.RunManager(None, "", 0.0)
        from flowagent.demo.fixtures import get_scenario

        cls.general = get_scenario("general-research")
        cls.finance = get_scenario("full")

    def validate(self, spec, scenario=None):
        return self.manager.validate_delegation_spec(
            spec, scenario or self.general)

    def test_valid_contract_passes(self) -> None:
        spec = {"mode": "script",
                "codingContract": {"deliverables": ["REPORT.md"],
                                   "requireVerification": False}}
        clean = self.validate(dict(spec))
        self.assertEqual(clean["codingContract"]["deliverables"], ["REPORT.md"])

    def test_invalid_shape_is_refused_at_creation(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.validate({"mode": "script", "codingContract": {"delivers": ["x"]}})
        self.assertIn("codingContract", str(ctx.exception))
        with self.assertRaises(ValueError):
            self.validate({"mode": "script", "codingContract": {}})

    def test_require_verification_needs_a_declared_command(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.validate({"mode": "script",
                           "codingContract": {"deliverables": ["REPORT.md"]}})
        self.assertIn("spec.verification", str(ctx.exception))
        # With the command declared the same contract is accepted (the spec
        # keeps the raw shape; the world normalizes it at build time).
        clean = self.validate({
            "mode": "script",
            "verification": {"command": "python3", "args": ["-c", "pass"]},
            "codingContract": {"deliverables": ["REPORT.md"]},
        })
        self.assertEqual(clean["codingContract"]["deliverables"], ["REPORT.md"])

    def test_motion_capability_runtime_rejects_contracts(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.validate({
                "mode": "open", "fastProfile": "motion-render",
                "project": {"path": "/tmp/nowhere"},
                "codingContract": {"deliverables": ["index.html"],
                                   "requireVerification": False},
            })
        self.assertIn("motion", str(ctx.exception))

    def test_non_general_modules_reject_contracts(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.validate({"codingContract": {"requireVerification": False}},
                          scenario=self.finance)
        self.assertIn("general-work", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
