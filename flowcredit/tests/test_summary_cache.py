"""P1-1 验收:增量 summary 缓存(不改变任何语义)。

T1 二次 build 命中缓存(summarizer 只调 1 次);T2 summary_version bump 后
未命中;T3 summary_policy 变更后未命中;T4 缓存损坏时 fail-open;T5 长路径
指针在 2000 上限下保留 >1 条(T 与 P0-1.5 T4 互补)。
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

from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import WorkingMemory  # noqa: E402
from flowagent.memory.summary_cache import SummaryCache  # noqa: E402
from flowagent.tools.workspace import summarize_search_corpus  # noqa: E402


def _observation(result=None, args=None):
    return type("O", (), {
        "tool": "probe", "arguments": args or {"k": 1},
        "result": result or {"value": "payload", "n": 7},
        "result_hash": "r" * 64,
    })()


def _memory(cache, *, config, summarizer):
    memory = WorkingMemory(
        objective="t", tool_catalog=[],
        summarizers={"probe": summarizer},
        summarizer_config=config,
        summary_cache=cache,
    )
    memory.record(1, _observation())
    return memory


class CacheHitTests(unittest.TestCase):
    def test_t1_second_summarize_hits_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            calls = {"n": 0}

            def counting(result):
                calls["n"] += 1
                return {"compressed": result.get("value")}

            cache = SummaryCache(store)
            memory = _memory(cache, config={"probe": ("custom", 1)},
                             summarizer=counting)
            first = memory.build(1, {}, None)["observations"]
            second = memory.build(2, {}, None)["observations"]
            self.assertEqual(calls["n"], 1, "第二次必须命中缓存,不重跑 summarizer")
            self.assertEqual(first, second, "产物必须一致")
            self.assertEqual(cache.hits, 1, "原缓存实例应记录一次命中")

    def test_t2_version_bump_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            calls = {"n": 0}

            def versioned(result):
                calls["n"] += 1
                return {"v": calls["n"], "value": result.get("value")}

            cache = SummaryCache(store)
            m1 = _memory(cache, config={"probe": ("custom", 1)}, summarizer=versioned)
            m1.build(1, {}, None)
            m2 = _memory(cache, config={"probe": ("custom", 2)}, summarizer=versioned)
            out = m2.build(1, {}, None)["observations"][0]["summary"]
            self.assertEqual(calls["n"], 2, "版本变更后必须重新 summarize")
            self.assertEqual(out["v"], 2)

    def test_t3_policy_change_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            calls = {"n": 0}

            def counting(result):
                calls["n"] += 1
                return {"value": result.get("value")}

            cache = SummaryCache(store)
            m1 = _memory(cache, config={"probe": ("detailed", 1)}, summarizer=counting)
            m1.build(1, {}, None)
            m2 = _memory(cache, config={"probe": ("custom", 1)}, summarizer=counting)
            m2.build(1, {}, None)
            self.assertEqual(calls["n"], 2, "策略变更后 key 变化,必须未命中")


class FailOpenTests(unittest.TestCase):
    def test_t4_broken_cache_still_returns_correct_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            cache = SummaryCache(store)
            store._conn.close()  # 损坏缓存:任何查询都会抛异常
            memory = _memory(cache, config={"probe": ("custom", 1)},
                             summarizer=lambda r: {"ok": r.get("value")})
            context = memory.build(2, {}, None)  # 不得抛异常
            self.assertEqual(context["observations"][0]["summary"],
                             {"ok": "payload"})


class LongPathRegressionTests(unittest.TestCase):
    def test_t5_long_pointer_rows_keep_more_than_one(self) -> None:
        long_path = "reports/" + "nested-segment-" * 11 + "doc.md"  # ~170 字符
        self.assertGreater(len(long_path), 150)
        hits = [{"doc_id": f"evidence:{i:08x}", "path": long_path,
                 "source": "fixture", "line": i,
                 "snippet": "s" * 200} for i in range(1, 6)]
        out = summarize_search_corpus({"query": "q", "hits": hits,
                                       "scanned": 1, "truncated": False})
        from flowagent.core.hashing import canonical_json
        self.assertGreater(len(out["hits"]), 1, "长路径不得退化为 1 条")
        self.assertEqual([row["doc_id"] for row in out["hits"]],
                         [h["doc_id"] for h in hits[:len(out["hits"])]])
        self.assertLessEqual(len(canonical_json(out)), 2000)


if __name__ == "__main__":
    unittest.main()
