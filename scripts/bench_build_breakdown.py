"""E 段:分段计时 build 的三段成本(行构造 / summarizer / 缓存点查)。

只读基准:通过运行时 monkeypatch 计时,不修改产品代码。目的:为 P1-2
行构造增量化提供依据(哪一段占每轮耗时的大头)。

三段定义:
- summarise_ms:_summarise 总耗时(含内部调用 summarizer 与字典运算);
- canonical_ms:build 中 canonical_json 的耗时(行尺寸计算/预算求和);
- cache_get_ms:SummaryCache.get 的耗时(SQLite 变体;是 summarise 的子集)。

用法:python3 scripts/bench_build_breakdown.py
输出:work/cache-benchmark/build-breakdown-report.json(不进仓库)
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import flowagent.memory.context as ctx_mod  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import WorkingMemory  # noqa: E402
from flowagent.memory.summary_cache import SummaryCache  # noqa: E402
from flowaudit.memory import make_audit_pin_policy  # noqa: E402

ROUNDS = 50
BUDGET = 24_000

_segments = {"summarise": 0.0, "canonical": 0.0, "cache_get": 0.0}

_original_summarise = WorkingMemory._summarise
_original_canonical = ctx_mod.canonical_json
_original_get = SummaryCache.get


def _timed_summarise(self, record):
    started = time.perf_counter()
    try:
        return _original_summarise(self, record)
    finally:
        _segments["summarise"] += time.perf_counter() - started


def _timed_canonical(value):
    started = time.perf_counter()
    try:
        return _original_canonical(value)
    finally:
        _segments["canonical"] += time.perf_counter() - started


def _timed_get(self, key):
    started = time.perf_counter()
    try:
        return _original_get(self, key)
    finally:
        _segments["cache_get"] += time.perf_counter() - started


WorkingMemory._summarise = _timed_summarise
ctx_mod.canonical_json = _timed_canonical
SummaryCache.get = _timed_get


def pinned(memory, r):
    memory.record(r, type("O", (), {
        "tool": "read_target_slice", "arguments": {"path": f"src/PIN-{r}.py"},
        "result": {"path": f"src/PIN-{r}.py", "evidence": f"PIN-{r}" * 30},
        "result_hash": f"PIN-{r}".ljust(64, "0")[:64]})(), event_kind="tool_succeeded")


def noise(memory, r):
    memory.record(r, type("O", (), {
        "tool": "generic", "arguments": {"k": r},
        "result": {"note": "n" * 400, "round": r},
        "result_hash": f"{r:064d}"})(), event_kind="tool_succeeded")


def run(with_cache: bool) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = TaskStore(pathlib.Path(tmp.name) / "b.sqlite")
    memory = WorkingMemory(
        objective="t", tool_catalog=[], observation_budget=BUDGET,
        pin_policy=make_audit_pin_policy(),
        summary_cache=SummaryCache(store) if with_cache else None,
        summarizer_config={"read_target_slice": ("detailed", 1),
                           "generic": ("default", 1)})
    rows = []
    for r in range(1, ROUNDS + 1):
        pinned(memory, r) if r % 10 == 1 else noise(memory, r)
        for key in _segments:
            _segments[key] = 0.0
        started = time.perf_counter()
        memory.build(r, {"calls": f"{r}/60"})
        total = time.perf_counter() - started
        rows.append({
            "round": r,
            "total_ms": round(total * 1e3, 3),
            "summarise_ms": round(_segments["summarise"] * 1e3, 3),
            "canonical_ms": round(_segments["canonical"] * 1e3, 3),
            "cache_get_ms": round(_segments["cache_get"] * 1e3, 3),
        })
    tmp.cleanup()
    return {"variant": "sqlite-cache" if with_cache else "no-cache", "rows": rows}


def shares(rows: list[dict], r: int) -> dict:
    row = rows[r - 1]
    total = row["total_ms"] or 1e-9
    return {
        "round": r,
        "total_ms": row["total_ms"],
        "summarise_share": f"{row['summarise_ms'] / total:.0%}",
        "canonical_share": f"{row['canonical_ms'] / total:.0%}",
        "cache_get_share": f"{row['cache_get_ms'] / total:.0%}",
    }


def main() -> int:
    no_cache = run(with_cache=False)
    sqlite = run(with_cache=True)
    print("== no-cache ==")
    for r in (10, 30, 50):
        print(shares(no_cache["rows"], r))
    print("== sqlite-cache ==")
    for r in (10, 30, 50):
        print(shares(sqlite["rows"], r))
    out = ROOT / "work" / "cache-benchmark"
    out.mkdir(parents=True, exist_ok=True)
    (out / "build-breakdown-report.json").write_text(
        json.dumps({"no_cache": no_cache, "sqlite_cache": sqlite},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
