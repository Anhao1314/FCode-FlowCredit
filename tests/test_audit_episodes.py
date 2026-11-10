"""L3 情景层验收:稳定指纹 + 同靶时间线 + 修复被回退的检出。

时间线键是 target identity(路径),不是 manifest 哈希——修复会改哈希,
而"修复后重挖"正是回归判断唯一有价值的时刻。发现匹配先用实例指纹
(根因 + 证据路径集合,行号会漂移),旧情景无指纹时回退根因粗键。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowaudit import cli  # noqa: E402
from flowaudit.episodes import (  # noqa: E402
    EpisodeStore,
    fingerprint_for,
    regression,
)


def _finding(group, category="AGI-02", severity="high", fingerprint=None):
    finding = {"finding_id": f"F-{group}-{category}", "root_cause_group": group,
               "category": category, "severity": severity, "poc_refs": ["POC-X"]}
    if fingerprint:
        finding["fingerprint"] = fingerprint
    return finding


class RegressionStateTests(unittest.TestCase):
    def test_known_new_fixed_by_root_cause(self) -> None:
        prior = [_finding("approval-memory"), _finding("plaintext-secret", "AGI-03")]
        current = [
            _finding("approval-memory"),                          # known
            _finding("ungated-execute"),                          # new
            # plaintext-secret 缺席 → fixed
        ]
        result = regression(prior, current)
        self.assertEqual([f["root_cause_group"] for f in result["known"]],
                         ["approval-memory"])
        self.assertEqual([f["root_cause_group"] for f in result["new"]],
                         ["ungated-execute"])
        self.assertEqual([f["root_cause_group"] for f in result["fixed"]],
                         ["plaintext-secret"])

    def test_same_root_cause_different_sites_are_distinct(self) -> None:
        """同规则同根因、不同文件的两条发现:一条修复不能把另一条也报 fixed。"""
        prior = [_finding("plaintext-secret", fingerprint="a" * 16),
                 _finding("plaintext-secret", fingerprint="b" * 16)]
        current = [_finding("plaintext-secret", fingerprint="a" * 16)]
        result = regression(prior, current)
        self.assertEqual([f["fingerprint"] for f in result["known"]], ["a" * 16])
        self.assertEqual([f["fingerprint"] for f in result["fixed"]], ["b" * 16])
        self.assertEqual(result["new"], [])

    def test_legacy_prior_matches_fingerprinted_current(self) -> None:
        """升级后的第一次审计:旧情景没有指纹,按根因身份仍然认得出 known。"""
        result = regression([_finding("approval-memory")],
                            [_finding("approval-memory", fingerprint="c" * 16)])
        self.assertEqual(len(result["known"]), 1)
        self.assertEqual(result["new"], [])
        self.assertEqual(result["fixed"], [])

    def test_regressed_detects_return_after_fix(self) -> None:
        fixed_before = [_finding("plaintext-secret", "AGI-03", fingerprint="d" * 16)]
        # 上情景为空(缺陷已修复),但更早情景里出现过 → regressed,不是 new。
        result = regression([], fixed_before, earlier=[fixed_before])
        self.assertEqual(len(result["regressed"]), 1)
        self.assertEqual(result["new"], [])

    def test_reappearance_never_seen_before_is_new(self) -> None:
        current = [_finding("approval-memory", fingerprint="e" * 16)]
        result = regression([], current, earlier=[[]])
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["regressed"], [])

    def test_clean_prior_target_marks_new(self) -> None:
        """prior 情景存在但零发现(干净靶子):当前发现 = new,不是误报。
        (首次审计的"不判定"由 CLI 守卫:prior 情景不存在时不调用本函数。)"""
        result = regression([], [_finding("approval-memory")])
        self.assertEqual([f["root_cause_group"] for f in result["new"]],
                         ["approval-memory"])


class FingerprintTests(unittest.TestCase):
    def test_line_numbers_and_snippets_do_not_change_identity(self) -> None:
        one = fingerprint_for("AGI-03", "plaintext-secret", [
            {"path": "a.py", "line": 10, "snippet": "SECRET = 'x'"}])
        two = fingerprint_for("AGI-03", "plaintext-secret", [
            {"path": "a.py", "line": 99, "snippet": "SECRET = 'y'"}])
        self.assertEqual(one, two)

    def test_different_sites_do_not_collide(self) -> None:
        one = fingerprint_for("AGI-03", "plaintext-secret", [{"path": "a.py"}])
        two = fingerprint_for("AGI-03", "plaintext-secret", [{"path": "b.py"}])
        self.assertNotEqual(one, two)


class EpisodeStoreTests(unittest.TestCase):
    def test_timeline_is_identity_keyed_across_content_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EpisodeStore(pathlib.Path(tmp) / "episodes.sqlite")
            rows = [
                ("a-1", "a" * 64, "target-a", "2026-11-01T00:00:00Z"),
                ("b-1", "b" * 64, "target-b", "2026-11-02T00:00:00Z"),
                ("a-2", "c" * 64, "target-a", "2026-11-03T00:00:00Z"),
            ]
            for audit_id, target_hash, identity, created in rows:
                store.close_episode(bundle={
                    "audit_id": audit_id,
                    "target": {"manifest_sha256": target_hash, "identity": identity},
                    "authorization": {"granted_at": created},
                    "sessions": [{"session_ref": "cli"}],
                    "findings": [_finding("approval-memory")],
                    "poc_evidence_refs": ["POC-0001"],
                    "chain_verification": {"head_hash": "c" * 64},
                }, bundle_dir=str(pathlib.Path(tmp) / audit_id))
            line = store.episodes_for_identity("target-a")
            self.assertEqual([e["audit_id"] for e in line], ["a-1", "a-2"])
            self.assertEqual(store.episodes_for_identity("target-b")[0]["audit_id"], "b-1")
            # 内容版本查询仍可用:哈希 a 只对应第一次审计。
            self.assertEqual([e["audit_id"] for e in store.episodes_for_target("a" * 64)],
                             ["a-1"])
            # 改内容后(a-2 哈希 c),prior 仍是同一路径上的 a-1。
            self.assertEqual(store.prior_episode("target-a", "a-2")["audit_id"], "a-1")


class EndToEndRegressionTests(unittest.TestCase):
    """同一路径审计四次:行号漂移仍 known;修复报 fixed;回退报 regressed。

    这是审计产品复购理由的原型,也是 manifest 哈希不能当时间线键的证明:
    每次修复都改哈希,但基线必须还在。
    """

    def test_in_place_fix_and_reintroduction(self) -> None:
        demo = ROOT / "fixtures" / "demo-target"
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            live = work / "live-target"
            shutil.copytree(demo, live)
            episodes_db = work / "episodes.sqlite"

            def audit(tag: str) -> tuple[dict, dict]:
                rc = cli.main(["audit", str(live), "--out", str(work / tag),
                               "--episodes-db", str(episodes_db),
                               "--operator", "ci"])
                self.assertEqual(rc, 0)
                bundle = json.loads(
                    (work / tag / "audit-bundle.json").read_text(encoding="utf-8"))
                regression_doc = json.loads(
                    (work / tag / "regression.json").read_text(encoding="utf-8"))
                return bundle, regression_doc

            bundle1, regression1 = audit("out1")
            self.assertEqual(len(bundle1["findings"]), 3)
            self.assertNotIn("regression", bundle1,
                             "首次审计没有基线,不伪造回归判断")
            self.assertIsNone(regression1["prior_audit_id"])

            # 只插入一行注释:行号整体漂移,缺陷原样还在 → 必须仍是 known。
            agent = live / "mini_agent" / "agent.py"
            agent.write_text("# line shift only\n" + agent.read_text(encoding="utf-8"),
                             encoding="utf-8")
            bundle2, regression2 = audit("out2")
            self.assertNotEqual(bundle2["target"]["manifest_sha256"],
                                bundle1["target"]["manifest_sha256"])
            self.assertEqual(len(regression2["known"]), 3)
            self.assertEqual(regression2["new"], [])
            self.assertEqual(regression2["fixed"], [])
            self.assertEqual(bundle2["regression"]["known"], 3)
            self.assertEqual(bundle2["regression"]["prior_audit_id"],
                             bundle1["audit_id"])

            # 修复明文密钥:两条密钥发现 fixed,审批记忆缺陷仍 known。
            config = live / "mini_agent" / "config.py"
            original_config = config.read_text(encoding="utf-8")
            config.write_text('"""clean."""\nAPI_KEY = ""\n', encoding="utf-8")
            bundle3, regression3 = audit("out3")
            self.assertEqual([f["root_cause_group"] for f in bundle3["findings"]],
                             ["approval-memory"])
            self.assertEqual(len(regression3["known"]), 1)
            self.assertEqual(len(regression3["fixed"]), 2)
            self.assertEqual(regression3["new"], [])

            # 回退修复:上情景缺席、更早情景出现过 → regressed,不是 new。
            config.write_text(original_config, encoding="utf-8")
            bundle4, regression4 = audit("out4")
            self.assertEqual(len(regression4["known"]), 1)
            self.assertEqual(regression4["new"], [])
            self.assertEqual(len(regression4["regressed"]), 2)
            self.assertEqual(
                {f["fingerprint"] for f in regression4["regressed"]},
                {f["fingerprint"] for f in bundle1["findings"]
                 if f["root_cause_group"] in ("plaintext-secret", "hardcoded-secret")})
            self.assertEqual(bundle4["regression"]["regressed"], 2)

    def test_different_target_path_starts_a_new_timeline(self) -> None:
        """复制到新路径 = 新靶子身份;即使内容相同也不继承别人的基线。"""
        demo = ROOT / "fixtures" / "demo-target"
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            episodes_db = work / "episodes.sqlite"
            clone = work / "clone-target"
            shutil.copytree(demo, clone)
            for index, target in enumerate((demo, clone), start=1):
                rc = cli.main(["audit", str(target), "--out", str(work / f"out{index}"),
                               "--episodes-db", str(episodes_db), "--operator", "ci"])
                self.assertEqual(rc, 0)
            regression2 = json.loads(
                (work / "out2" / "regression.json").read_text(encoding="utf-8"))
            self.assertIsNone(regression2["prior_audit_id"])
            self.assertEqual(regression2["known"], [])


if __name__ == "__main__":
    unittest.main()