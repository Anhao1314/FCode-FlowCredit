"""P0-1.5 验收:search_corpus 的 custom summarizer(指针型返回值)。

T1 策略与实现存在;T2 全部指针行保留、正文剔除;T3 空命中不抛异常;
T4 上限截断(顺序、>1 条、带 truncated_after/total);T5 分发走 custom。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.memory.context import (  # noqa: E402
    detailed_summarize,
    summarizer_for,
)
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import default_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.workspace import build_workspace_tools  # noqa: E402


def _search_spec():
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    specs = build_workspace_tools(EvidenceStore(root), ArtifactStore(root / "a"),
                                  default_registry())
    return tmp, {spec.name: spec for spec in specs}["search_corpus"]


def _hit(i: int) -> dict:
    return {"doc_id": f"evidence:{i:012x}", "path": f"reports/doc_{i}.md",
            "source": "fixture", "line": 10 + i,
            "snippet": f"SECRET-SNIPPET-BODY-{i} margin reconciliation note"}


def _result(hits: list[dict]) -> dict:
    return {"query": "margin", "hits": hits, "scanned": 12, "truncated": False}


class SearchCorpusCustomTests(unittest.TestCase):
    def test_t1_policy_custom_and_implementation_present(self) -> None:
        tmp, spec = _search_spec()
        with tmp:
            self.assertEqual(spec.summary_policy, "custom")
            self.assertIsNotNone(spec.summarize)

    def test_t2_all_pointers_kept_and_snippets_removed(self) -> None:
        tmp, spec = _search_spec()
        with tmp:
            out = spec.summarize(_result([_hit(i) for i in range(1, 6)]))
            self.assertEqual(out["count"], 5)
            self.assertEqual(len(out["hits"]), 5, "5 条指针必须全部保留")
            for row in out["hits"]:
                self.assertEqual(set(row), {"doc_id", "path", "source", "line"})
                self.assertNotIn("snippet", row)
            self.assertNotIn("SECRET-SNIPPET-BODY", canonical_json(out),
                             "正文子串不得出现在输出中")
            self.assertEqual(out["query"], "margin")
            self.assertEqual(out["scanned"], 12)
            self.assertFalse(out["truncated"])

    def test_t3_empty_hits_is_explicit_and_safe(self) -> None:
        tmp, spec = _search_spec()
        with tmp:
            out = spec.summarize(_result([]))
            self.assertEqual(out["hits"], [])
            self.assertEqual(out["count"], 0)
            self.assertNotIn("truncated_after", out)

    def test_t4_cap_truncates_in_order_and_keeps_more_than_one(self) -> None:
        tmp, spec = _search_spec()
        with tmp:
            hits = [_hit(i) for i in range(1, 201)]
            out = spec.summarize(_result(hits))
            self.assertIn("truncated_after", out)
            self.assertEqual(out["total"], 200)
            kept = out["truncated_after"]
            self.assertGreater(kept, 1, "不得退化为只留第 1 条")
            self.assertLess(kept, 200)
            expected = [h["doc_id"] for h in hits[:kept]]
            self.assertEqual([row["doc_id"] for row in out["hits"]], expected,
                             "保留的必须是前 N 条且顺序不变")
            self.assertLessEqual(len(canonical_json(out)), 2000,
                                 "输出必须落在 2000 字符上限内")

    def test_t5_dispatch_uses_custom_not_detailed(self) -> None:
        tmp, spec = _search_spec()
        with tmp:
            fn = summarizer_for(spec)
            self.assertIs(fn, spec.summarize)
            self.assertIsNot(fn, detailed_summarize)


if __name__ == "__main__":
    unittest.main()
