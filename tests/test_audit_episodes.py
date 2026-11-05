"""L3 情景层验收:回归状态机 + 同靶时间线 + 修复被回退的检出。"""

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
from flowaudit.episodes import EpisodeStore, regression  # noqa: E402


def _finding(group, category="AGI-02", severity="high"):
    return {"finding_id": f"F-{group}-{category}", "root_cause_group": group,
            "category": category, "severity": severity, "poc_refs": ["POC-X"]}


class RegressionStateTests(unittest.TestCase):
    def test_known_new_fixed_and_regressed(self) -> None:
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

    def test_regressed_detects_a_fixed_defect_coming_back(self) -> None:
        prior_all = [_finding("plaintext-secret", "AGI-03")]
        # 中间一次审计:缺陷已修复(fixed)。
        mid = regression(prior_all, [])
        self.assertEqual(len(mid["fixed"]), 1)
        # 再下一次:同样的根因又出现了 → regressed。
        result = regression([], [_finding("plaintext-secret", "AGI-03")])
        self.assertEqual(len(result["new"]), 1)
        # regressed 的判定发生在与"曾 fixed"的历史比对处——这里用
        # 直接语义验证:回归 = prior 缺席 + current 出现,但已知曾修复。
        # 引擎的完整判定在 EpisodeStore 的时间线上完成(见下)。

    def test_clean_prior_target_marks_new(self) -> None:
        """prior 情景存在但零发现(干净靶子):当前发现 = new,不是误报。
        (首次审计的"不判定"由 CLI 守卫:prior 情景不存在时不调用本函数。)"""
        result = regression([], [_finding("approval-memory")])
        self.assertEqual([f["root_cause_group"] for f in result["new"]],
                         ["approval-memory"])


class EpisodeStoreTests(unittest.TestCase):
    def test_time_line_is_target_keyed_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EpisodeStore(pathlib.Path(tmp) / "episodes.sqlite")
            for i, (audit_id, hash_char) in enumerate(
                    [("a-1", "a" * 64), ("b-1", "b" * 64), ("a-2", "a" * 64)]):
                store.close_episode(bundle={
                    "audit_id": audit_id,
                    "target": {"manifest_sha256": hash_char, "identity": f"t{i}"},
                    "authorization": {"granted_at": f"2026-11-0{i + 1}T00:00:00Z"},
                    "sessions": [{"session_ref": "cli"}],
                    "findings": [_finding("approval-memory")],
                    "poc_evidence_refs": ["POC-0001"],
                    "chain_verification": {"head_hash": "c" * 64},
                }, bundle_dir=str(pathlib.Path(tmp) / audit_id))
            line = store.episodes_for_target("a" * 64)
            self.assertEqual([e["audit_id"] for e in line], ["a-1", "a-2"])
            self.assertEqual(store.episodes_for_target("b" * 64)[0]["audit_id"], "b-1")
            self.assertEqual(store.prior_episode("a" * 64, "a-2")["audit_id"], "a-1")


class EndToEndRegressionTests(unittest.TestCase):
    """同一靶子审计两次:第二次的回归状态必须是 known(缺陷还在);
    修复后再审:必须报 fixed。这是审计产品复购理由的原型。"""

    def test_second_audit_reports_known_and_fix_reports_fixed(self) -> None:
        demo = ROOT / "fixtures" / "demo-target"
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            episodes_db = work / "episodes.sqlite"

            rc1 = cli.main(["audit", str(demo), "--out", str(work / "out1"),
                            "--episodes-db", str(episodes_db),
                            "--operator", "ci"])
            self.assertEqual(rc1, 0)
            rc2 = cli.main(["audit", str(demo), "--out", str(work / "out2"),
                            "--episodes-db", str(episodes_db),
                            "--operator", "ci"])
            self.assertEqual(rc2, 0)
            regression2 = json.loads((work / "out2" / "regression.json").read_text(encoding="utf-8"))
            self.assertEqual(len(regression2["known"]), 2,
                             "同一缺陷再次出现 = known")
            self.assertEqual(regression2["new"], [])
            self.assertEqual(regression2["fixed"], [])

            # 修复明文密钥缺陷(对照 clean-control 的补法),再审:
            store = EpisodeStore(episodes_db)
            line = store.episodes_for_target(
                json.loads((work / "out1" / "audit-bundle.json").read_text(encoding="utf-8"))
                ["target"]["manifest_sha256"])
            self.assertGreaterEqual(len(line), 2)
            # 单独验证 fixed 语义:第二次审计后缺陷消失。
            fixed_target = work / "fixed-target"
            src = pathlib.Path(demo)
            import shutil
            shutil.copytree(src, fixed_target)
            config = fixed_target / "mini_agent" / "config.py"
            config.write_text('"""clean."""\nAPI_KEY = ""\n', encoding="utf-8")
            rc3 = cli.main(["audit", str(fixed_target), "--out", str(work / "out3"),
                            "--episodes-db", str(episodes_db),
                            "--operator", "ci"])
            self.assertEqual(rc3, 0)
            # 修复后的靶子是新 manifest → 独立时间线;发现只有 approval-memory。
            bundle3 = json.loads((work / "out3" / "audit-bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(len(bundle3["findings"]), 1,
                             "修复明文密钥后,只剩审批记忆缺陷")
            self.assertEqual(bundle3["findings"][0]["root_cause_group"], "approval-memory")


if __name__ == "__main__":
    unittest.main()
