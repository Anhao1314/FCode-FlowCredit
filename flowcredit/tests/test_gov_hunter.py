"""探员（语义候选生成器）：上下文有界、盲考纪律、输出解析纪律。

边界即契约：
1. findings 永远不进上下文（盲考：探员不抄答案）；
2. 文件树摘要有界（notable ≤ 90、顶层目录聚合）；
3. 候选解析裁剪字段、丢弃空假设、上限 12 条；
4. 非法 JSON 显式报错（端点按 502 汇报），不静默吞掉。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from gov_hunter import (  # noqa: E402
    MAX_CANDIDATES,
    build_context,
    hunt_candidates,
)


def facts_fixture() -> dict:
    return {
        "target": {"identity": "/Applications/Demo.app", "version": "1.0"},
        "host": {
            "declared_features": ["audio-input"],
            "entitlements": ["com.apple.security.device.audio-input",
                             "com.apple.security.cs.disable-library-validation"],
            "library_validation": {"entitlement_present": True},
            "runtime_installers": [{"command": "npm install -g %s"}],
            "signature": {"status": "valid"},
            "vibe_scan": {"total_files": 100, "total_lines": 2000,
                          "god_files": [{"path": "big.ts", "lines": 2137}]},
        },
        "sidecars": [{"runtime_bundled": True}],
        "operations": {"inspect_go_binary": {"facts": {
            "go_version": "go1.26.10",
            "module": {"path": "github.com/demo/app"},
            "settings": [{"key": "-tags", "value": "desktop"}],
        }}},
        # 探员的输入永远不该带 findings——即便上游误传，build_context 也不搬
        "findings": [{"finding_id": "F-SECRET"}],
    }


def desktop_fixture(count: int = 3000) -> dict:
    files = [{"path": f"Contents/Resources/app/file{i}.js", "kind": "file",
              "bytes": 10} for i in range(count)]
    files.append({"path": "Contents/MacOS/demo", "kind": "file", "bytes": 99})
    return {"files": files}


class FakeProvider:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, system, user, *, max_tokens=None, reasoning_effort=None,
                 json_mode=None):
        self.calls.append({"system": system, "user": user,
                           "max_tokens": max_tokens, "json_mode": json_mode})
        return self.payload, {"model": "fake-hunter", "usage_total_tokens": 100,
                              "cost_microusd": 7}


class BuildContextTests(unittest.TestCase):
    def test_findings_never_enter_the_context(self) -> None:
        context = build_context(
            facts=facts_fixture(),
            go_deps=[{"name": "github.com/wailsapp/wails/v2", "version": "v2.12.0"}],
            intel_snapshot=None,
            desktop_evidence=desktop_fixture(10),
        )
        self.assertNotIn("F-SECRET", context)
        self.assertNotIn('"findings"', context)
        self.assertIn("disable-library-validation", context)
        self.assertIn("wails/v2", context)

    def test_file_tree_is_bounded(self) -> None:
        context = build_context(
            facts=facts_fixture(), go_deps=[],
            intel_snapshot=None, desktop_evidence=desktop_fixture(3000),
        )
        parsed = json.loads(context)
        tree = parsed["app_file_tree"]
        self.assertLessEqual(len(tree["notable_files"]), 90, "notable 有界")
        self.assertIn("Contents/Resources/app", tree["top_dirs"], "顶层目录聚合")
        self.assertNotIn('"count"', json.dumps(tree), "不搬全量文件清单")

    def test_intel_is_compacted_with_provenance(self) -> None:
        context = build_context(
            facts=facts_fixture(), go_deps=[],
            intel_snapshot={
                "source": "api.osv.dev", "fetched_at": "2026-11-19T00:00:00Z",
                "vulns": [{
                    "id": "GHSA-47hv-j4px-h3c9",
                    "summary": "Wails: Origin bypass via suffix match",
                    "aliases": ["GO-2026-6862"],
                    "affected": [{
                        "package": {"ecosystem": "Go",
                                    "name": "github.com/wailsapp/wails/v2"},
                        "ranges": [{"type": "SEMVER", "events": [
                            {"introduced": "0"}, {"fixed": "2.13.0"}]}],
                    }],
                }],
            },
            desktop_evidence=None,
        )
        parsed = json.loads(context)
        intel = parsed["intel_known_vulnerabilities"]
        self.assertEqual(intel["source"], "api.osv.dev")
        self.assertEqual(intel["fetched_at"], "2026-11-19T00:00:00Z")
        self.assertEqual(intel["records"][0]["id"], "GHSA-47hv-j4px-h3c9")
        self.assertEqual(
            intel["records"][0]["affected_packages"][0]["fixed_versions"],
            ["2.13.0"])


class HuntCandidatesTests(unittest.TestCase):
    def test_parses_trims_and_caps_candidates(self) -> None:
        items = [{
            "surface": f"surface-{i}", "hypothesis": f"hyp-{i}",
            "evidence_refs": ["host.entitlements"], "attack_scenario": "a",
            "severity_guess": "high", "verification_plan": "v",
            "knowledge_based": i % 2 == 0,
        } for i in range(20)]
        items.append({"surface": "empty", "hypothesis": "  "})  # 空假设被丢
        provider = FakeProvider(json.dumps({
            "candidates": items, "uncertainty_notes": ["n1"]}))
        result = hunt_candidates(provider, "{}")
        self.assertEqual(len(result["candidates"]), MAX_CANDIDATES)
        self.assertEqual(result["candidates"][0]["surface"], "surface-0")
        self.assertTrue(result["candidates"][0]["knowledge_based"])
        self.assertEqual(result["meta"]["model"], "fake-hunter")
        self.assertEqual(result["meta"]["tokens"], 100)
        call = provider.calls[0]
        self.assertIn("不可信数据", call["system"])
        self.assertTrue(call["json_mode"])

    def test_invalid_json_fails_loudly(self) -> None:
        provider = FakeProvider('{"candidates": [')
        with self.assertRaises(ValueError):
            hunt_candidates(provider, "{}")

    def test_missing_provider_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            hunt_candidates(None, "{}")


if __name__ == "__main__":
    unittest.main()
