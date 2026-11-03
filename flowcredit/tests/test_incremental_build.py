"""P1-2 验收:行构造增量化 + 进程内 dict 介质。

T1 增量化输出与"全量重建"参考实现逐字节一致(跨 R1-R50);
T1b 第二次 build 不重序列化历史行(增量性契约,先红);
T2 折叠级分布/pinned 集合/pending 标记逐轮一致;
T3 dict 介质命中、SQLite 默认关闭时不触达;
T4 历史行内容不被后续轮次修改。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

import flowagent.memory.context as ctx_mod  # noqa: E402
from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import (  # noqa: E402
    WorkingMemory,
    _arguments_head,
    raw_summarize,
)
from flowaudit.memory import make_audit_pin_policy  # noqa: E402


def _pinned(memory, r, marker):
    memory.record(r, type("O", (), {
        "tool": "read_target_slice", "arguments": {"path": f"src/{marker}.py"},
        "result": {"path": f"src/{marker}.py",
                   "evidence": marker * max(1, 150 // len(marker))},
        "result_hash": marker[:8] + f"{r:056d}"})(), event_kind="tool_succeeded")


def _noise(memory, r, chars=400):
    memory.record(r, type("O", (), {
        "tool": "generic", "arguments": {"k": r},
        "result": {"note": "n" * chars, "round": r},
        "result_hash": f"{r:064d}"})(), event_kind="tool_succeeded")


def _memory(budget=24_000):
    return WorkingMemory(objective="t", tool_catalog=[],
                         observation_budget=budget,
                         pin_policy=make_audit_pin_policy())


def _build_reference(memory, round_no, budget_snapshot, plan=None):
    """P1-2 之前的全量重建算法(逐字节等价参考);纯函数,不写 memory 状态。"""
    include_trust = memory.prompt_profile is not None
    rows = []
    for record in memory.records:
        pinned = bool(memory.pin_policy and memory.pin_policy.should_pin(record))
        trust = {"trust": record.source_trust.value} if include_trust else {}
        rows.append({
            "record": record, "pinned": pinned,
            "full": {"round": record.round, "tool": record.tool,
                     "arguments": record.arguments,
                     "result": raw_summarize({"result": record.result})["result"],
                     "result_hash": record.result_hash, **trust},
            "summary_row": {"round": record.round, "tool": record.tool,
                            "arguments": record.arguments,
                            "summary": memory._summarise(record), **trust},
        })
    pinned_full = [row["full"] for row in rows if row["pinned"]]
    index_rows, inline = [], []
    pinned_chars = sum(len(canonical_json(o)) for o in pinned_full)
    pending_pin = pending_compaction = None
    if pinned_chars > memory.observation_budget:
        pending_pin = {"pinned_count": len(pinned_full),
                       "pinned_chars": pinned_chars,
                       "budget": memory.observation_budget}
        inline = pinned_full + [row["summary_row"] for row in rows if not row["pinned"]]
    else:
        non_pinned_summaries = [row["summary_row"] for row in rows if not row["pinned"]]
        budget_left = memory.observation_budget - pinned_chars
        used = sum(len(canonical_json(o)) for o in non_pinned_summaries)
        if used <= budget_left or not non_pinned_summaries:
            inline = [row["full"] for row in rows if row["pinned"]] + non_pinned_summaries
        else:
            keep_from = len(non_pinned_summaries)
            running = used
            for i, row in enumerate(non_pinned_summaries):
                running -= len(canonical_json(row))
                if running <= budget_left:
                    keep_from = i + 1
                    break
            folded = non_pinned_summaries[:keep_from]
            inline = ([row["full"] for row in rows if row["pinned"]]
                      + non_pinned_summaries[keep_from:])
            non_pinned_records = [row["record"] for row in rows if not row["pinned"]]
            index_rows = [
                {"round": row["round"], "tool": row["tool"],
                 "arguments_head": _arguments_head(record.arguments),
                 "result_hash": record.result_hash[:12]}
                for row, record in zip(folded, non_pinned_records[:keep_from])]
            if index_rows:
                pending_compaction = {
                    "from_round": index_rows[0]["round"],
                    "through_round": index_rows[-1]["round"],
                    "inlined_from_round": inline[-1].get("round", round_no),
                    "folded_count": len(index_rows),
                    "pinned_count": len(pinned_full),
                    "levels": {"full": len(pinned_full),
                               "summary": len(inline) - len(pinned_full),
                               "index": len(index_rows)}}
    return {"observations": inline, "observation_index": index_rows,
            "pending_pin_overflow": pending_pin,
            "pending_compaction": pending_compaction}


class IncrementalityEquivalenceTests(unittest.TestCase):
    def test_t1_round_by_round_equivalence_with_rebuild_reference(self) -> None:
        for budget in (24_000, 1_500):  # 覆盖常规与激进折叠两种预算
            incr, ref = _memory(budget), _memory(budget)
            for r in range(1, 51):
                _pinned(incr, r, f"PIN-{r}") if r % 10 == 1 else _noise(incr, r)
                _pinned(ref, r, f"PIN-{r}") if r % 10 == 1 else _noise(ref, r)
                got = incr.build(r, {"calls": f"{r}/60"})
                want = _build_reference(ref, r, {"calls": f"{r}/60"})
                self.assertEqual(
                    canonical_json(got["observations"]),
                    canonical_json(want["observations"]),
                    f"budget={budget} R{r}: observations 必须逐字节一致")
                self.assertEqual(
                    canonical_json(got.get("observation_index") or []),
                    canonical_json(want["observation_index"]),
                    f"budget={budget} R{r}: 索引行必须逐字节一致")
                self.assertEqual(incr.pending_pin_overflow,
                                 want["pending_pin_overflow"],
                                 f"budget={budget} R{r}: pin 溢出标记一致")
                if want["pending_compaction"] is not None:
                    self.assertEqual(incr.pending_compaction,
                                     want["pending_compaction"],
                                     f"budget={budget} R{r}: 折叠标记一致")
                    incr.pending_compaction = None

    def test_t1b_second_build_does_not_reserialize_history(self) -> None:
        memory = _memory()
        for r in range(1, 11):
            _noise(memory, r)
        calls = {"n": 0}
        original = ctx_mod.canonical_json

        def counting(value):
            calls["n"] += 1
            return original(value)

        ctx_mod.canonical_json = counting
        try:
            memory.build(11, {})
            first_calls = calls["n"]
            calls["n"] = 0
            memory.build(12, {})
            second_calls = calls["n"]
        finally:
            ctx_mod.canonical_json = original
        self.assertGreater(first_calls, 0)
        self.assertLessEqual(
            second_calls, 2,
            "第二次 build 不得重序列化历史行(仅允许新记录/预算快照的极少量)")

    def test_t2_fold_levels_and_pending_markers_match(self) -> None:
        incr, ref = _memory(1_500), _memory(1_500)
        seen_levels = set()
        for r in range(1, 31):
            _pinned(incr, r, f"PIN-{r}") if r % 10 == 1 else _noise(incr, r)
            _pinned(ref, r, f"PIN-{r}") if r % 10 == 1 else _noise(ref, r)
            got = incr.build(r, {})
            want = _build_reference(ref, r, {})
            got_pending = incr.pending_compaction
            if want["pending_compaction"] is not None:
                self.assertEqual(got_pending, want["pending_compaction"])
                seen_levels.add(tuple(sorted(
                    want["pending_compaction"]["levels"].items())))
                incr.pending_compaction = None
        self.assertTrue(seen_levels, "至少观察到一次折叠级分布")


class DictMediumTests(unittest.TestCase):
    def test_t3_memo_hit_avoids_recompute_and_sqlite_default_off(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[],
                               summarizers={"probe": None} if False else {
                                   "probe": lambda r: {"v": r.get("x")}})
        record = memory.record(1, type("O", (), {
            "tool": "probe", "arguments": {}, "result": {"x": 1},
            "result_hash": "r" * 64})())
        calls = {"n": 0}
        original = memory.summarizers["probe"]

        def counting(result):
            calls["n"] += 1
            return original(result)

        memory.summarizers["probe"] = counting
        memory._summarise(record)
        memory._summarise(record)
        self.assertEqual(calls["n"], 1, "第二次必须命中进程内 dict 介质")

        import flowagent.runtime.loop as loop_mod
        self.assertFalse(loop_mod.ENABLE_SQLITE_SUMMARY_CACHE,
                         "SQLite 缓存默认必须关闭")


class ImmutabilityTests(unittest.TestCase):
    def test_t4_historical_rows_unchanged_after_cursor_advances(self) -> None:
        memory = _memory()
        _noise(memory, 1)
        first = memory.build(1, {})
        snapshot = canonical_json(first["observations"])
        for r in range(2, 8):
            _noise(memory, r)
            memory.build(r, {})
        context = memory.build(8, {})
        # 第一轮的行若仍在 inline(预算内),内容必须与快照一致;
        # 若已折叠,则索引行必须与该记录对应。
        first_round_row = [o for o in context["observations"] if o["round"] == 1]
        index_row = [i for i in (context.get("observation_index") or [])
                     if i["round"] == 1]
        if first_round_row:
            self.assertEqual(canonical_json(first_round_row),
                             canonical_json(json.loads(snapshot)))
        else:
            self.assertTrue(index_row, "折叠后必须留有索引行")


if __name__ == "__main__":
    unittest.main()
