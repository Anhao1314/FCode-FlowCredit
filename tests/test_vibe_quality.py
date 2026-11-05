"""VIBE-02 / VIBE-05 rules: consistency defects and AI fingerprints.

The scanner emits bounded fact groups; the rule engine turns them into
candidates; the host oracle confirms by re-reading the pinned facts. These
tests cover all three layers plus the negative-control projection.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowaudit import cli  # noqa: E402
from flowaudit.rea.vibe import scan_code_quality  # noqa: E402
from flowaudit.vulns import engine  # noqa: E402

DUP_FUNC = '''def duplicated_helper(a, b):
    total = a + b
    if total > 1000:
        total = 1000
    if total < -999:
        total = -999
    factor = 100
    total = total * factor // 100
    return total
'''

FILE_A = '''MAX_RETRIES = 3
LIMIT = 100

def schedule():
    # 250 ms 轮询间隔，反复出现
    return [250, 250, 250, 250, 250]

def process(items):
    # 希望这对你有帮助：处理所有条目
    return [item for item in items]

def not_implemented_yet():
    pass

''' + DUP_FUNC

FILE_B = '''MAX_RETRIES = 5
LIMIT = 100

''' + DUP_FUNC


class VibeScannerTests(unittest.TestCase):
    def _scan(self, files: dict[str, str]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return scan_code_quality(root)

    def test_scanner_emits_all_vibe02_05_fact_groups(self) -> None:
        facts = self._scan({"src/a.py": FILE_A, "src/b.py": FILE_B})
        conflicts = facts["constant_conflicts"]
        self.assertEqual([c["name"] for c in conflicts], ["MAX_RETRIES"])
        self.assertEqual(
            sorted(d["value"] for d in conflicts[0]["definitions"]), ["3", "5"])

        self.assertTrue(
            any(row["value"] == "250" and row["count"] >= 5
                for row in facts["literal_sprawl"]),
            "repeated magic literal must surface as literal_sprawl")

        dups = facts["duplicate_functions"]
        self.assertEqual([d["name"] for d in dups], ["duplicated_helper"])
        self.assertEqual({f["path"] for f in dups[0]["files"]},
                         {"src/a.py", "src/b.py"})

        stubs = facts["placeholder_stubs"]
        self.assertEqual([s["name"] for s in stubs], ["not_implemented_yet"])

        residue = facts["ai_residue"]
        self.assertEqual(len(residue), 1)
        self.assertEqual(residue[0]["phrase"], "希望这对你有帮助")

    def test_ai_residue_ignores_string_constants(self) -> None:
        """短语表本身是字符串常量——扫字符串会让扫描器自我命中。"""
        facts = self._scan({
            "src/table.py": 'PHRASES = ("as an ai", "hope this helps")\n',
        })
        self.assertEqual(facts["ai_residue"], [])

    def test_private_constants_do_not_conflict(self) -> None:
        """_DEFAULT_* 这类私有默认值跨模块不同是正常的。"""
        facts = self._scan({
            "src/a.py": "_DEFAULT_TIMEOUT = 10\n",
            "src/b.py": "_DEFAULT_TIMEOUT = 30\n",
        })
        self.assertEqual(facts["constant_conflicts"], [])


class VibeRuleTests(unittest.TestCase):
    def test_new_rules_match_scanner_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text(FILE_A, encoding="utf-8")
            (root / "b.py").write_text(FILE_B, encoding="utf-8")
            facts = scan_code_quality(root)
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        candidates = engine.rea_scan({"host": {"vibe_scan": facts}}, ruleset)
        ids = {c.rule_id for c in candidates}
        for expected in (
            "RULE-VIBE02-001", "RULE-VIBE02-002",
            "RULE-VIBE05-001", "RULE-VIBE05-002", "RULE-VIBE05-003",
        ):
            self.assertIn(expected, ids)

    def test_rules_do_not_fire_on_clean_facts(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        clean = {
            "host": {"vibe_scan": {
                "god_files": [], "todo_hotspots": [], "issue_files": [],
                "literal_sprawl": [], "constant_conflicts": [],
                "duplicate_functions": [], "placeholder_stubs": [],
                "ai_residue": [],
            }},
        }
        candidates = engine.rea_scan(clean, ruleset)
        new_ids = {c.rule_id for c in candidates if c.rule.category in ("VIBE-02", "VIBE-05")}
        self.assertEqual(new_ids, set(), "clean facts must not produce VIBE-02/05 candidates")


class VibeAuditIntegrationTests(unittest.TestCase):
    def test_audit_finds_and_controls_all_new_rules(self) -> None:
        """端到端：5 条新规则全部出 finding，负对照全部 as_expected。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "target"
            (target / "src").mkdir(parents=True)
            (target / "src" / "a.py").write_text(FILE_A, encoding="utf-8")
            (target / "src" / "b.py").write_text(FILE_B, encoding="utf-8")
            out = pathlib.Path(tmp) / "bundle"
            rc = cli.main(["audit", str(target), "--out", str(out),
                           "--operator", "ci-test"])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            vibe = [f for f in bundle["findings"] if f["category"] in ("VIBE-02", "VIBE-05")]
            groups = {f["root_cause_group"] for f in vibe}
            self.assertEqual(groups, {
                "constant-conflict", "magic-value-sprawl", "ai-chat-residue",
                "duplicate-implementation", "placeholder-stub",
            })
            for finding in vibe:
                record = json.loads(
                    (out / f"{finding['poc_refs'][0]}.json").read_text(encoding="utf-8"))
                self.assertEqual(record["replay"]["outcome"], "finding")
                self.assertEqual(record["negative_control"]["result"], "as_expected",
                                 f"{record['rule_id']} 的负对照必须 as_expected")
                self.assertTrue(record.get("notes"), "证据摘要必须点名具体行")


if __name__ == "__main__":
    unittest.main()