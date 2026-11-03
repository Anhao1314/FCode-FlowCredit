"""P1-1.5 第一步:大载荷基准(96KB 级),判断增量缓存是否真有收益。

判读规则(先测后改,不拍脑袋):
- 若大载荷下冷缓存均值比无缓存低 30% 以上 → 收益存在,进入阈值门控;
- 若收益不显著或为负 → 停下,不做阈值门控,讨论换介质或回滚 P1-1;
- 若命中率低于 90% → 先查 key 设计,不进入第二步。

载荷形态贴近真实工具:
- fetch_source 的大输出:嵌套 {"content": {"text": 长文本}};
- read_slice 的大输出:{"lines": [{line, text} ...]} 的 dict 列表。
合成一个 96KB 级结果,由真实的 "detailed" 策略压缩(与线上分发一致)。

用法:python3 scripts/bench_large_payload.py
报告:work/cache-benchmark/large-payload-report.json(不进仓库)
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import WorkingMemory, detailed_summarize  # noqa: E402
from flowagent.memory.summary_cache import SummaryCache  # noqa: E402

ROUNDS = 50
BUDGET = 24_000
LONG_TEXT = "revenue reconciliation paragraph; " * 2_000       # ~60KB 长文本
SNIPPETS = [
    {"doc_id": f"evidence:{i:08x}", "path": f"reports/segment_{i}.md",
     "source": "annual-report", "line": 100 + i,
     "snippet": ("adjusted margin bridge note " + str(i) + " ") * 16}  # ~500 字符/条
    for i in range(40)
]


def payload() -> dict:
    """96KB 级:嵌套长文本 + 40 条 dict 列表(贴近 fetch_source/read_slice)。"""
    return {
        "source": "annual-report-web", "url": "https://example.com/fy2025",
        "status": 200, "content_type": "text/html",
        "sha256": "ab" * 32, "bytes": len(LONG_TEXT),
        "content": {"text": LONG_TEXT},
        "hits": SNIPPETS,
    }


def observation(round_no: int) -> object:
    return type("O", (), {
        "tool": "fetch_source",  # summary_policy="detailed" 的真实分发
        "arguments": {"source": "annual-report-web"},
        "result": payload(),
        "result_hash": f"{round_no:064d}",
    })()


def run_group(with_cache: bool) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = TaskStore(pathlib.Path(tmp.name) / "bench.sqlite")
    cache = SummaryCache(store) if with_cache else None
    memory = WorkingMemory(
        objective="bench", tool_catalog=[], observation_budget=BUDGET,
        summary_cache=cache,
        summarizer_config={"fetch_source": ("detailed", 1)},
    )
    rows = []
    for round_no in range(1, ROUNDS + 1):
        memory.record(round_no, observation(round_no))
        started = time.monotonic()
        context = memory.build(round_no, {"calls": f"{round_no}/60"})
        elapsed = (time.monotonic() - started) * 1000
        inline = context["observations"]
        rows.append({
            "round": round_no, "build_ms": round(elapsed, 3),
            "inline": len(inline),
            "index": len(context.get("observation_index") or []),
        })
    tmp.cleanup()
    times = [row["build_ms"] for row in rows]
    return {
        "rows": rows,
        "avg_ms": round(statistics.mean(times), 3),
        "max_ms": round(max(times), 3),
        "median_ms": round(statistics.median(times), 3),
        "hit_rate": (f"{cache.hits / (cache.hits + cache.misses):.1%}"
                     if cache and (cache.hits + cache.misses) else "n/a"),
    }


def unit_costs() -> dict:
    """单次 summarize vs 单次缓存点查(命中)的平均耗时,µs。"""
    result = payload()
    started = time.monotonic()
    for _ in range(200):
        detailed_summarize(result)
    summarize_us = (time.monotonic() - started) * 1e6 / 200

    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(pathlib.Path(tmp) / "unit.sqlite")
        cache = SummaryCache(store)
        key = ("fetch_source", "a" * 64, "b" * 64, "detailed", 1)
        cache.put(key, detailed_summarize(result))
        started = time.monotonic()
        for _ in range(200):
            cache.get(key)
        cache_get_us = (time.monotonic() - started) * 1e6 / 200
    return {"summarize_us": round(summarize_us, 2),
            "cache_get_hit_us": round(cache_get_us, 2)}


def main() -> int:
    size = len(json.dumps(payload(), ensure_ascii=False).encode("utf-8"))
    print(f"载荷大小: {size / 1024:.1f} KB(goal ≈96KB)\n")

    no_cache = run_group(with_cache=False)
    cold_cache = run_group(with_cache=True)
    units = unit_costs()

    def curve(group: dict) -> str:
        return ", ".join(f"R{row['round']}:{row['build_ms']}" for row in group["rows"])

    print("== 无缓存 ==")
    print(f"均值 {no_cache['avg_ms']} ms | 峰值 {no_cache['max_ms']} ms | "
          f"中位 {no_cache['median_ms']} ms")
    print("曲线:", curve(no_cache), "\n")

    print("== 冷缓存 ==")
    print(f"均值 {cold_cache['avg_ms']} ms | 峰值 {cold_cache['max_ms']} ms | "
          f"中位 {cold_cache['median_ms']} ms | 命中率 {cold_cache['hit_rate']}")
    print("曲线:", curve(cold_cache), "\n")

    print("== 单元成本 ==")
    print(f"单次 detailed summarize: {units['summarize_us']} µs | "
          f"单次缓存命中点查: {units['cache_get_hit_us']} µs")

    delta = no_cache["avg_ms"] - cold_cache["avg_ms"]
    ratio = delta / no_cache["avg_ms"] if no_cache["avg_ms"] else 0.0
    verdict = ("收益存在(≥30%)" if ratio >= 0.30
               else "收益不显著/为负 → 停下,不做阈值门控")
    print(f"\n判读: 缓存相对无缓存 {'快' if delta > 0 else '慢'} "
          f"{abs(ratio):.0%} → {verdict}")

    out = ROOT / "work" / "cache-benchmark"
    out.mkdir(parents=True, exist_ok=True)
    (out / "large-payload-report.json").write_text(json.dumps({
        "payload_bytes": size,
        "no_cache": no_cache, "cold_cache": cold_cache,
        "unit_costs": units,
        "verdict": verdict, "delta_ratio": round(ratio, 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
