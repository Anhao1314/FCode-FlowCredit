"""S2-12/13: negative controls and structural variants, end to end.

Three targets derived from the real checkout:

- **clean control** — the FCODE-02 flaw is patched to key the remembered
  approval on (tool, arguments digest), plus a decoy file that still
  trips the static trigger. The honest pipeline must produce a
  candidate, clear it in replay (``not_reproduced``), and promote
  nothing: zero findings, zero false positives.
- **structural variant** — the remembered-grant block is extracted into
  a helper method (same tokens, different shape). Static detection and
  replay must both survive the restructuring: same root cause found.
- **clean rule loading** — the ruleset loads with both dev rules active.

These run the full CLI pipeline twice each and are the slowest tests in
the suite; they are the reason the suite exists.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowaudit import cli  # noqa: E402
from flowaudit.targets import repo as repo_adapter  # noqa: E402

RUNTIME = pathlib.Path("viewer") / "console_runtime.py"

REMEMBER_CHECK = "        if tool in self._remembered:"
REMEMBER_ADD = "                    self._remembered.add(tool)"
GRANT_BLOCK = """        if tool in self._remembered:
            task = self.harness.record(
                task,
                "approval_granted",
                {"round": round_no, "tool": tool, "by": "operator-rule", "scope": "run"},
            )
            return self.ToolControl(task=task, allowed=True)
"""
TOOL_LINE = '        tool = step["tool"]'


def _copy_and_read(workdir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str]:
    copy = repo_adapter.copy_target(ROOT, workdir)
    runtime = copy / RUNTIME
    return copy, runtime, runtime.read_text(encoding="utf-8")


def _write_decoy(copy: pathlib.Path) -> None:
    """A decoy that trips the static trigger but is not the flaw: the
    honest pipeline must clear it in replay, not promote it."""
    base = copy / "flowcredit" if (copy / "flowcredit").exists() else copy
    decoy = base / "docs" / "decoy_legacy_pattern.py"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(
        "# Style note for reviewers: the legacy pattern\n"
        "#   self._remembered.add(tool)\n"
        "# is forbidden; approvals must be keyed on (tool, arguments).\n",
        encoding="utf-8")


def make_clean_control(workdir: pathlib.Path) -> pathlib.Path:
    """Patch BOTH dev defects so the target is genuinely clean:
    FCODE-02 → remember keyed on (tool, arguments digest);
    FCODE-06 → execute refuses tools outside the task allow list.
    Plus a decoy file that still trips the static trigger."""
    copy, runtime, text = _copy_and_read(workdir)
    assert REMEMBER_CHECK in text and REMEMBER_ADD in text, "anchor strings drifted"
    text = text.replace(
        TOOL_LINE,
        TOOL_LINE + '\n        step_key = json.dumps(step.get("arguments") or {}, sort_keys=True)',
        1,
    )
    text = text.replace(
        REMEMBER_CHECK,
        "        if (tool, step_key) in self._remembered:",
        1,
    )
    text = text.replace(
        REMEMBER_ADD,
        "                    self._remembered.add((tool, step_key))",
        1,
    )
    # FCODE-06 minimal fix: defense inside execute itself (harness.py, not
    # console_runtime.py — different file, different patch).
    harness_rel = (
        pathlib.Path("flowcredit") / "src" / "flowagent" / "runtime" / "harness.py"
    )
    if not (copy / harness_rel).exists():
        harness_rel = pathlib.Path("src") / "flowagent" / "runtime" / "harness.py"
    harness = copy / harness_rel
    htext = harness.read_text(encoding="utf-8")
    planned_block = (
        '        if task.status == "planned":\n'
        '            task = self._commit(task, task.advance("running"), "execution_started", {})\n'
    )
    assert planned_block in htext, "execute anchor drifted"
    htext = htext.replace(
        planned_block,
        planned_block
        + '        if step["tool"] not in task.allowed_tools:\n'
        + '            raise AgentError("TOOL_NOT_ALLOWED",\n'
        + '                f"{step[\'tool\']!r} is not in the task allow list")\n',
        1,
    )
    harness.write_text(htext, encoding="utf-8")
    _write_decoy(copy)
    runtime.write_text(text, encoding="utf-8")
    # The bundled demo target's fake key would light up RULE-AGI03-001 on
    # a "clean" audit; a clean repo fixes its demo target too.
    demo_cfg = copy / "fixtures" / "demo-target" / "mini_agent" / "config.py"
    if demo_cfg.exists():
        demo_cfg.write_text(
            '"""MiniAgent configuration (clean control: reads from env)."""\n'
            'import os\n\nAPI_KEY = os.environ.get("MINIAGENT_API_KEY", "")\n',
            encoding="utf-8")
    return copy


def make_structural_variant(workdir: pathlib.Path) -> pathlib.Path:
    """Extract the remembered-grant block into a helper: same tokens,
    different shape. Detection must survive it."""
    copy, runtime, text = _copy_and_read(workdir)
    assert GRANT_BLOCK in text, "grant block anchor drifted"
    text = text.replace(
        GRANT_BLOCK,
        "        granted = self._grant_from_memory(tool, round_no, task)\n"
        "        if granted is not None:\n"
        "            return granted\n",
        1,
    )
    marker = "    # ------------------------------------------------------------------ private"
    assert marker in text, "helper anchor drifted"
    helper = (
        "    def _grant_from_memory(self, tool: str, round_no: int, task):\n"
        "        if tool in self._remembered:\n"
        "            task = self.harness.record(\n"
        "                task,\n"
        "                \"approval_granted\",\n"
        "                {\"round\": round_no, \"tool\": tool, \"by\": \"operator-rule\", \"scope\": \"run\"},\n"
        "            )\n"
        "            return self.ToolControl(task=task, allowed=True)\n"
        "        return None\n\n"
    )
    text = text.replace(marker, helper + marker, 1)
    runtime.write_text(text, encoding="utf-8")
    return copy


class NegativeControlTests(unittest.TestCase):
    def test_clean_control_produces_zero_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            control = make_clean_control(workdir / "control-src")
            out = workdir / "bundle"
            rc = cli.main(["audit", str(control), "--out", str(out),
                           "--operator", "ci-test"])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            agi_findings = [f for f in bundle["findings"]
                            if f["category"].startswith("AGI")]
            self.assertEqual(
                agi_findings, [],
                "a clean target must not produce AGI findings — false positives kill trust")
            self.assertTrue(bundle["poc_evidence_refs"],
                            "the decoy candidate must still be replayed, not skipped")
            for ref in bundle["poc_evidence_refs"]:
                record = json.loads((out / f"{ref}.json").read_text(encoding="utf-8"))
                if record["category"].startswith("VIBE"):
                    continue  # VIBE findings are quality observations, not security false positives
                self.assertEqual(record["replay"]["outcome"], "not_reproduced",
                                 "the decoy must be cleared by the host oracle")


class VariantTests(unittest.TestCase):
    def test_structural_variant_is_still_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            variant = make_structural_variant(workdir / "variant-src")
            out = workdir / "bundle"
            rc = cli.main(["audit", str(variant), "--out", str(out),
                           "--operator", "ci-test"])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            groups = {f["root_cause_group"] for f in bundle["findings"]}
            self.assertIn("approval-memory", groups,
                          "restructured same-root-cause code must still be found")


if __name__ == "__main__":
    unittest.main()
