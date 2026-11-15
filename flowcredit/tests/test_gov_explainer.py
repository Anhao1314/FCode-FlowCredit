"""治理解释层：模型解释确定性证据，硬边界由测试钉死。

边界即契约：
1. prompt 只含 bundle 的有界事实 + "不可信数据"框架，findings 有 40 条上限；
2. 解释产物进 governance.sqlite（bot_explanations），审计包逐字节不变；
3. 模型缺位/未完成运行/无审计包全部 fail-closed，治理功能不受影响。
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from gov_bots import GovBotService  # noqa: E402
from gov_explainer import (  # noqa: E402
    GovExplainer,
    ProviderUnavailable,
    explain_context,
)


class FakeProvider:
    """记录 prompt 的假补全 provider：解释层的最小可执行替身。"""

    model = "fake-explainer"

    def __init__(self, text="发现 F-1 依据 fp-1 与宿主 oracle，风险在权限记忆。"):
        self.text = text
        self.calls: list[dict] = []

    def complete(self, system, user, *, max_tokens=None, reasoning_effort=None,
                 json_mode=None):
        self.calls.append({
            "system": system, "user": user, "max_tokens": max_tokens,
        })
        meta = {
            "model": self.model,
            "usage_total_tokens": 321,
            "usage_prompt_tokens": 300,
            "usage_completion_tokens": 21,
            "cost_microusd": 45,
        }
        return self.text, meta


def mini_bundle(findings: int = 2) -> dict:
    items = []
    for index in range(findings):
        items.append({
            "finding_id": f"F-{index}",
            "fingerprint": f"fp-{index}",
            "category": "AGI-02",
            "severity": "high",
            "root_cause_group": "approval-memory",
            "poc_refs": ["POC-0001"],
        })
    return {
        "audit_id": "demo-1",
        "target": {"identity": "/repo/fixtures/demo-target", "version": ""},
        "toolchain": {"ruleset_version": "v10", "analyst_model": "deterministic"},
        "chain_verification": {"head_hash": "ab" * 16},
        "findings": items,
        "poc_evidence_refs": [{"poc_id": "POC-0001"}],
        "rea_evidence_refs": [],
        "uncovered_surfaces": [{"surface": "dynamic probing"}],
    }


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = GovBotService(
            audit=None,
            db_path=pathlib.Path(self._tmp.name) / "governance.sqlite",
            start_scheduler=False,
        )
        self.addCleanup(self.service._db.close)
        self.run_id = "bot-demo-1"
        with self.service._db as db:
            db.execute(
                "INSERT INTO bot_runs (run_id, bot_id, target, status, audit_id,"
                " started_at, finished_at) VALUES (?,?,?,?,?,?,?)",
                (self.run_id, "auditor", "/repo/fixtures/demo-target", "done",
                 "demo-1", "2026-11-12T10:00:00", "2026-11-12T10:05:00"),
            )
        self.bundle = mini_bundle()
        self.calls: list[str] = []

        def loader(audit_id):
            self.calls.append(audit_id)
            return copy.deepcopy(self.bundle)

        self.bundle_loader = loader

    def explainer(self, provider=None, bundle_loader=None):
        return GovExplainer(
            service=self.service,
            bundle_loader=bundle_loader or self.bundle_loader,
            provider_factory=lambda key: provider,
            default_model="fake-explainer",
        )

    def seed(self, status: str, audit_id: str = "demo-1"):
        run_id = "bot-" + uuid.uuid4().hex[:8]
        with self.service._db as db:
            db.execute(
                "INSERT INTO bot_runs (run_id, bot_id, target, status, audit_id,"
                " started_at) VALUES (?,?,?,?,?,?)",
                (run_id, "auditor", "/repo/t", status, audit_id,
                 "2026-11-12T11:00:00"),
            )
        return run_id


class ExplainBoundaryTests(Harness):
    def test_explain_stores_record_and_grounds_prompt(self) -> None:
        provider = FakeProvider()
        record = self.explainer(provider).explain(self.run_id)
        self.assertEqual(record["run_id"], self.run_id)
        self.assertEqual(record["model"], "fake-explainer")
        self.assertEqual(record["tokens"], 321)
        self.assertEqual(record["cost_microusd"], 45)
        stored = self.service.explanation(self.run_id)
        self.assertEqual(stored["text"], provider.text)
        self.assertTrue(stored["prompt_sha256"])

        call = provider.calls[0]
        # 边界框架与证据都在 prompt 里
        self.assertIn("不做裁决", call["system"])
        self.assertIn("不可信数据", call["system"])
        self.assertIn("fp-1", call["user"])
        self.assertIn('"findings_total": 2', call["user"])
        self.assertIn("ruleset_version", call["user"])
        self.assertLessEqual(call["max_tokens"], 4096)

    def test_findings_are_capped_at_forty(self) -> None:
        provider = FakeProvider()
        run_id = self.seed("done")
        big = mini_bundle(findings=45)
        self.explainer(provider, bundle_loader=lambda a: copy.deepcopy(big)).explain(run_id)
        user = provider.calls[0]["user"]
        self.assertIn('"findings_total": 45', user)
        self.assertEqual(user.count('"fingerprint"'), 40, "上下文只带 40 条发现")

    def test_bundle_is_never_mutated(self) -> None:
        provider = FakeProvider()
        before = copy.deepcopy(self.bundle)
        self.explainer(provider).explain(self.run_id)
        self.assertEqual(self.bundle, before, "解释层不得改动审计包")

    def test_reexplain_replaces_the_previous_record(self) -> None:
        provider = FakeProvider("第一版解读")
        self.explainer(provider).explain(self.run_id)
        provider2 = FakeProvider("第二版解读")
        self.explainer(provider2).explain(self.run_id, question="回归风险？")
        rows = self.service.explanations_map()
        self.assertEqual(list(rows), [self.run_id])
        self.assertIn("第二版", rows[self.run_id]["text"])
        self.assertEqual(rows[self.run_id]["question"], "回归风险？")

    def test_fail_closed_paths(self) -> None:
        # 未完成运行
        pending = self.seed("pending")
        with self.assertRaises(ValueError):
            self.explainer(FakeProvider()).explain(pending)
        # 没有审计包
        no_bundle = self.seed("done", audit_id="")
        with self.assertRaises(ValueError):
            self.explainer(FakeProvider()).explain(no_bundle)
        # provider 缺位（mock / 目录不可用）
        with self.assertRaises(ProviderUnavailable):
            self.explainer(provider=None).explain(self.run_id)
        # 空文本（补全预算被推理占满）
        with self.assertRaises(ValueError):
            self.explainer(FakeProvider(text="")).explain(self.run_id)

    def test_tuple_loader_and_old_shape_findings(self) -> None:
        # 服务器 bundle() 返回 (payload, code) 元组；旧 bundle 没有 fingerprint
        provider = FakeProvider()
        old = mini_bundle()
        old["findings"] = [{
            "finding_id": "F-POC-0001", "category": "AGI-02",
            "severity": "high", "root_cause_group": "approval-memory",
        }]
        self.explainer(
            provider,
            bundle_loader=lambda a: copy.deepcopy(old),
        ).explain(self.run_id)
        user = provider.calls[0]["user"]
        self.assertIn("F-POC-0001", user)


if __name__ == "__main__":
    unittest.main()