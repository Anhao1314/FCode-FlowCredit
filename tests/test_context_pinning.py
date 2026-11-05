"""P0-3 契约测试:pin 集 + 三级折叠 + fail-open 溢出事件。

规格测试全覆盖:①pinned 全量存活、噪声按级降级;②pinned 自身超预算 →
context_pin_overflow 事件且 pinned 不被截断;③折叠顺序先摘要后索引,
不得跳级;④无 pinned 时索引行与改动前语义等价;⑤50 轮脚本化基准
(pinned 存活率 100%,每轮装配耗时与估算 token 落盘 work/ 临时路径)。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.memory.context import (  # noqa: E402
    PinPolicy,
    RAW_MAX_CHARS,
    WorkingMemory,
)
from flowaudit.memory import make_audit_pin_policy  # noqa: E402


def _noise_observation(memory, round_no, note_chars=300):
    memory.record(round_no, type("O", (), {
        "tool": "generic",
        "arguments": {"k": round_no},
        "result": {"note": "n" * note_chars, "round": round_no},
        "result_hash": f"{round_no:064d}",
    })(), event_kind="tool_succeeded")


def _pinned_observation(memory, round_no, marker, chars=120):
    memory.record(round_no, type("O", (), {
        "tool": "read_target_slice",
        "arguments": {"path": f"src/{marker}.py"},
        "result": {"path": f"src/{marker}.py", "evidence": marker * max(1, chars // len(marker))},
        "result_hash": marker[:8] + f"{round_no:056d}",
    })(), event_kind="tool_succeeded")


class PinSurvivalTests(unittest.TestCase):
    def test_pinned_stays_full_while_noise_degrades(self) -> None:
        memory = WorkingMemory(
            objective="t", tool_catalog=[],
            observation_budget=1_500,
            pin_policy=make_audit_pin_policy(),
        )
        _pinned_observation(memory, 1, "CRITICAL-EVIDENCE", chars=200)
        for i in range(2, 22):
            _noise_observation(memory, i)
        context = memory.build(22, {})

        inline = context["observations"]
        pinned_rows = [o for o in inline if o["tool"] == "read_target_slice"]
        self.assertEqual(len(pinned_rows), 1)
        # 全量存活:证据标记原样在 inline 里,未被摘要/折叠
        self.assertIn("CRITICAL-EVIDENCE", json_dumps(pinned_rows))
        self.assertEqual(pinned_rows[0]["result"]["evidence"].count("CRITICAL-EVIDENCE"),
                         200 // len("CRITICAL-EVIDENCE"))
        # 噪声按级降级:最老的进索引,较新的呈摘要
        self.assertIn("observation_index", context)
        index_rounds = [r["round"] for r in context["observation_index"]]
        self.assertEqual(index_rounds, list(range(2, 2 + len(index_rounds))),
                         "最老优先,顺序 FIFO(仅非 pinned)")
        self.assertFalse(any(r == 1 for r in index_rounds), "pinned 永不进索引")

    def test_levels_do_not_skip_summary_stage(self) -> None:
        memory = WorkingMemory(
            objective="t", tool_catalog=[],
            observation_budget=1_500,
            pin_policy=make_audit_pin_policy(),
        )
        _pinned_observation(memory, 1, "EVIDENCE", chars=100)
        for i in range(2, 22):
            _noise_observation(memory, i)
        context = memory.build(22, {})
        inline = context["observations"]
        index = context["observation_index"]
        # 折叠顺序:被折叠的最老轮次的下一轮必须仍在 inline 且呈摘要形态
        # (有 summary 字段)——证明先经过第 2 级,没有直接跳到索引。
        first_inline_round = min(o["round"] for o in inline
                                 if o["tool"] == "generic")
        self.assertGreater(first_inline_round, index[-1]["round"])
        summary_rows = [o for o in inline if o["tool"] == "generic"
                        and o["round"] == first_inline_round]
        self.assertIn("summary", summary_rows[0])


class PinOverflowTests(unittest.TestCase):
    def test_pinned_over_budget_records_overflow_and_keeps_full(self) -> None:
        memory = WorkingMemory(
            objective="t", tool_catalog=[],
            observation_budget=500,
            pin_policy=make_audit_pin_policy(),
        )
        _pinned_observation(memory, 1, "HUGE-EVIDENCE", chars=900)
        context = memory.build(2, {})
        self.assertIsNotNone(memory.pending_pin_overflow,
                             "pinned 自身超预算必须置位溢出标记")
        self.assertEqual(memory.pending_pin_overflow["pinned_count"], 1)
        self.assertGreater(memory.pending_pin_overflow["pinned_chars"], 500)
        inline = context["observations"]
        self.assertEqual(len(inline), 1)
        self.assertEqual(
            inline[0]["result"]["evidence"], "HUGE-EVIDENCE" * (900 // 13),
            "pinned 原文未被截断(900 字符按标记重复,无衰减)")
        self.assertGreater(
            len(json_dumps(inline)), 500,
            "fail-open:允许超预算继续,证据不丢")


class NoPinEquivalenceTests(unittest.TestCase):
    def test_no_pin_policy_index_rows_match_legacy_shape(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[],
                               observation_budget=1_500)
        for i in range(1, 11):
            memory.record(i, type("O", (), {
                "tool": "generic", "arguments": {"k": i},
                "result": {"k": i, "note": "x" * 300},
                "result_hash": "r" * 64,
            })(), event_kind="tool_succeeded")
        context = memory.build(11, {})
        index = context.get("observation_index") or []
        self.assertTrue(index, "400 字符级记录必须触发折叠")
        for row in index:
            self.assertEqual(sorted(row.keys()),
                             ["arguments_head", "result_hash", "round", "tool"],
                             "索引行形状与改动前一致")
            self.assertEqual(row["result_hash"], "r" * 12)


class FiftyRoundBenchmarkTests(unittest.TestCase):
    """长会话基准:脚本化 50 轮(不调真实模型)。报告落盘 work/ 临时路径,
    不进仓库。"""

    def test_fifty_round_scripted_benchmark(self) -> None:
        memory = WorkingMemory(
            objective="t", tool_catalog=[],
            observation_budget=24_000,
            pin_policy=make_audit_pin_policy(),
        )
        rounds = 50
        per_round_ms = []
        report_rows = []
        for round_no in range(1, rounds + 1):
            if round_no % 10 == 1:
                _pinned_observation(memory, round_no,
                                    f"PIN-{round_no}", chars=150)
            else:
                _noise_observation(memory, round_no, note_chars=400)
            started = time.monotonic()
            context = memory.build(round_no, {"calls": f"{round_no}/60"})
            elapsed_ms = (time.monotonic() - started) * 1000
            per_round_ms.append(elapsed_ms)
            inline = context["observations"]
            index = context.get("observation_index") or []
            pinned_inline = [o for o in inline if o["tool"] == "read_target_slice"]
            estimated_tokens = sum(len(json_dumps(o)) for o in inline) // 4
            report_rows.append({
                "round": round_no,
                "build_ms": round(elapsed_ms, 3),
                "inline": len(inline),
                "index": len(index),
                "estimated_tokens": estimated_tokens,
            })
        # pinned 存活率 100%
        pinned_inline = [o for o in context["observations"]
                         if o["tool"] == "read_target_slice"]
        pinned_total = 1 + len(range(11, rounds + 1, 10))
        self.assertEqual(len(pinned_inline), pinned_total,
                         "pinned 证据存活率必须 100%")
        overflow = memory.pending_pin_overflow is not None
        self.assertFalse(overflow, "本基准预算下不应触发 pinned 溢出")

        out_dir = ROOT / "work" / "pin-benchmark"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "rounds": rounds,
            "pinned_total": pinned_total,
            "pinned_survived": len(pinned_inline),
            "pinned_survival_rate": f"{len(pinned_inline) / pinned_total:.0%}",
            "pin_overflow": overflow,
            "build_ms_total": round(sum(per_round_ms), 3),
            "build_ms_avg": round(sum(per_round_ms) / len(per_round_ms), 3),
            "build_ms_max": round(max(per_round_ms), 3),
            "estimated_tokens_last_round": report_rows[-1]["estimated_tokens"],
            "rows": report_rows,
        }
        (out_dir / "pin-benchmark-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertLess(max(per_round_ms), 250,
                        "50 轮下单轮装配不得出现灾难性耗时")


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
