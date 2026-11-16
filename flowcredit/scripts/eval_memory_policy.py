#!/usr/bin/env python3
"""Ground-truth evaluation for the memory policy head.

The end-to-end shadow report needs a full agent run; this script isolates the
decision itself. Each scenario is a realistic observation log plus a declared
ground truth: which observations the *remaining* steps will actually need.
The real provider answers through ``OpenAICompatProvider.complete``, and the
script scores:

- ``needed_kept``   needed observations kept fully (pin/keep);
- ``needed_dropped`` needed observations demoted (the dangerous error);
- ``unneeded_dropped`` irrelevant observations demoted (the saving);
- ``unneeded_kept`` irrelevant observations kept (conservative).

It also compares against the deterministic heuristic for the same log (small
logs are all-inline), and records cost/latency from the provider metadata.

Usage:
    python3 scripts/eval_memory_policy.py --provider kimi
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.memory.policy import (  # noqa: E402
    PolicyConfig,
    build_policy_messages,
    compare_heuristic,
    parse_policy_reply,
)


def _row(obs_id: str, round_no: int, tool: str, args: str, summary: str) -> dict:
    return {
        "id": obs_id,
        "round": round_no,
        "tool": tool,
        "arguments_head": args,
        "summary_head": summary[:240],
    }


SCENARIOS = [
    {
        "name": "fix-pricing-test",
        "objective": "修复 pricing 折扣取整的失败测试，改完后跑测试并写报告",
        "todos": [
            {"id": "t1", "text": "修正 pricing.py 的取整逻辑", "status": "active"},
            {"id": "t2", "text": "同步修正 utils.py 的共享 helper", "status": "pending"},
            {"id": "t3", "text": "运行测试确认通过", "status": "pending"},
            {"id": "t4", "text": "写 report.md", "status": "pending"},
        ],
        "rows": [
            _row("a1b2c3d4e5f1", 1, "read_file", "path=src/pricing.py",
                 '{"path":"src/pricing.py","head":"def apply_discount(price, rate): return round(price * rate)"}'),
            _row("a1b2c3d4e5f2", 2, "read_file", "path=tests/test_pricing.py",
                 '{"path":"tests/test_pricing.py","head":"assert apply_discount(100, 0.15) == 15"}'),
            _row("a1b2c3d4e5f3", 3, "grep_workspace", "pattern=discount",
                 '{"matches":12,"files":["pricing.py","utils.py","docs/pricing.md"]}'),
            _row("a1b2c3d4e5f4", 4, "run_command", "command=pytest -q",
                 '{"exit":1,"tail":"FAILED tests/test_pricing.py::test_discount - AssertionError: 14.0 != 15"}'),
            _row("a1b2c3d4e5f5", 5, "read_file", "path=README.md",
                 '{"path":"README.md","head":"# Pricing service. Run pytest to verify."}'),
            _row("a1b2c3d4e5f6", 6, "read_file", "path=src/utils.py",
                 '{"path":"src/utils.py","head":"def round_half_up(value): return int(value + 0.5)"}'),
            _row("a1b2c3d4e5f7", 7, "read_file", "path=src/pricing.py",
                 '{"path":"src/pricing.py","head":"def apply_discount(price, rate): return round_half_up(price * rate)"}'),
            _row("a1b2c3d4e5f8", 8, "run_command", "command=pytest -q",
                 '{"exit":0,"tail":"3 passed in 0.42s"}'),
        ],
        "needed": {"a1b2c3d4e5f1", "a1b2c3d4e5f2", "a1b2c3d4e5f4",
                   "a1b2c3d4e5f6", "a1b2c3d4e5f7", "a1b2c3d4e5f8"},
        # Historical state that a later observation supersedes. Dropping these
        # is correct policy, not a miss: the current state is still available.
        "superseded": {"a1b2c3d4e5f1": "a1b2c3d4e5f7",
                       "a1b2c3d4e5f4": "a1b2c3d4e5f8"},
    },
    {
        "name": "research-sources",
        "objective": "根据操作者圈定的来源写 research.md，并核对引用",
        "todos": [
            {"id": "t1", "text": "提取来源中的关键结论", "status": "active"},
            {"id": "t2", "text": "写 research.md", "status": "pending"},
            {"id": "t3", "text": "核对引用与来源哈希", "status": "pending"},
        ],
        "rows": [
            _row("b1b2c3d4e5f1", 1, "fetch_url", "url=https://docs.example/a",
                 '{"status":200,"title":"Protocol A","excerpt":"latency budget 200ms"}'),
            _row("b1b2c3d4e5f2", 2, "fetch_url", "url=https://docs.example/b",
                 '{"status":200,"title":"Protocol B","excerpt":"retries capped at 3"}'),
            _row("b1b2c3d4e5f3", 3, "fetch_url", "url=https://blog.example/unrelated",
                 '{"status":200,"title":"10 productivity tips","excerpt":"inbox zero"}'),
            _row("b1b2c3d4e5f4", 4, "write_file", "path=research.md",
                 '{"path":"research.md","bytes":1840,"sha256":"9f2c"}'),
            _row("b1b2c3d4e5f5", 5, "read_file", "path=research.md",
                 '{"path":"research.md","head":"# Research: latency and retries"}'),
            _row("b1b2c3d4e5f6", 6, "fetch_url", "url=https://docs.example/a",
                 '{"status":200,"title":"Protocol A","excerpt":"latency budget 200ms"}'),
        ],
        "needed": {"b1b2c3d4e5f1", "b1b2c3d4e5f2", "b1b2c3d4e5f4", "b1b2c3d4e5f5"},
        "superseded": {"b1b2c3d4e5f4": "b1b2c3d4e5f5"},
    },
    {
        "name": "multi-file-fix",
        "objective": "修复 a.py 的 import 环并跑测试，然后写变更说明",
        "todos": [
            {"id": "t1", "text": "修正 a.py 的 import", "status": "active"},
            {"id": "t2", "text": "运行测试", "status": "pending"},
            {"id": "t3", "text": "写 CHANGELOG 条目", "status": "pending"},
        ],
        "rows": [
            _row("c1b2c3d4e5f1", 1, "list_files", "path=src",
                 '{"files":["a.py","b.py","c.py","tests/test_a.py"]}'),
            _row("c1b2c3d4e5f2", 2, "read_file", "path=src/a.py",
                 '{"path":"src/a.py","head":"import b; from b import helper"}'),
            _row("c1b2c3d4e5f3", 3, "read_file", "path=src/b.py",
                 '{"path":"src/b.py","head":"import a  # cycle"}'),
            _row("c1b2c3d4e5f4", 4, "grep_workspace", "pattern=from b import",
                 '{"matches":3,"files":["a.py","c.py","tests/test_a.py"]}'),
            _row("c1b2c3d4e5f5", 5, "read_file", "path=src/a.py",
                 '{"path":"src/a.py","head":"from b_helpers import helper"}'),
            _row("c1b2c3d4e5f6", 6, "run_command", "command=pytest -q",
                 '{"exit":0,"tail":"5 passed"}'),
            _row("c1b2c3d4e5f7", 7, "read_file", "path=CHANGELOG.md",
                 '{"path":"CHANGELOG.md","head":"# 1.4.2 ... # 1.4.1"}'),
        ],
        "needed": {"c1b2c3d4e5f2", "c1b2c3d4e5f4", "c1b2c3d4e5f5", "c1b2c3d4e5f6"},
        "superseded": {"c1b2c3d4e5f2": "c1b2c3d4e5f5",
                       "c1b2c3d4e5f4": "c1b2c3d4e5f5"},
    },
]


def _provider(key: str):
    from console_common import merged_provider_env, settings_provider_env
    from flowagent.providers.catalog import compat_settings_for
    from flowagent.providers.openai_compat import OpenAICompatProvider

    env = merged_provider_env(settings_provider_env())
    settings = compat_settings_for(key, env=env)
    if settings is None:
        raise SystemExit(f"provider {key!r} is not configured")
    return OpenAICompatProvider.from_settings(settings)


def evaluate(provider, scenario: dict, config: PolicyConfig) -> dict:
    rows = scenario["rows"]
    budget = {
        "records": len(rows),
        "observation_chars": 19_200,
        "budget_chars": 24_000,
        "folded_through": 0,
        "pinned": 0,
        "non_pinned": len(rows),
    }
    system, user = build_policy_messages(
        objective=scenario["objective"],
        round_no=max(row["round"] for row in rows) + 1,
        rows=rows,
        open_todos=scenario["todos"],
        budget=budget,
        max_entries=config.max_entries,
    )
    text, meta = provider.complete(
        system, user, max_tokens=config.max_tokens,
        reasoning_effort=(config.effort if getattr(provider, "reasoning_effort", "") else None),
    )
    decision = parse_policy_reply(
        text, {row["id"] for row in rows}, max_entries=config.max_entries)

    needed = scenario["needed"]
    superseded = scenario.get("superseded") or {}
    live_needed = needed - set(superseded)
    actions = {item.id: item.action for item in decision.observations}
    details = {
        item.id: {"action": item.action, "reason": item.reason,
                  "confidence_bp": int(round(item.confidence * 10_000))}
        for item in decision.observations
    }
    kept = {obs_id for obs_id, action in actions.items() if action in ("pin", "keep")}
    dropped = {obs_id for obs_id, action in actions.items() if action == "drop"}
    all_ids = {row["id"] for row in rows}

    result = {
        "scenario": scenario["name"],
        "ok": decision.ok,
        "error": decision.error,
        "decided": len(actions),
        "needed_total": len(needed),
        "needed_kept": len(needed & kept),
        "needed_dropped": len(needed & dropped),
        "needed_undecided": len(needed - set(actions)),
        "live_needed_total": len(live_needed),
        "live_needed_kept": len(live_needed & kept),
        "live_needed_dropped": len(live_needed & dropped),
        "superseded_total": len(superseded),
        "superseded_dropped": len(set(superseded) & dropped),
        "unneeded_total": len(all_ids - needed),
        "unneeded_dropped": len((all_ids - needed) & dropped),
        "unneeded_kept": len((all_ids - needed) & kept),
        "unneeded_undecided": len((all_ids - needed) - set(actions)),
        "agreement": compare_heuristic(
            decision, {obs_id: "inline" for obs_id in all_ids}),
        "cost": meta,
        "raw_actions": details,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="kimi")
    parser.add_argument("--pace", type=float, default=5.0)
    parser.add_argument("--out", default=str(
        ROOT / "work" / "cache-benchmark" / "memory-policy-eval.json"))
    args = parser.parse_args()

    provider = _provider(args.provider)
    config = PolicyConfig(mode="shadow", max_tokens=8192, max_entries=40, effort="none")
    results = []
    for index, scenario in enumerate(SCENARIOS):
        if index and args.pace > 0:
            time.sleep(args.pace)
        try:
            results.append(evaluate(provider, scenario, config))
        except Exception as error:  # noqa: BLE001 — report, keep other scenarios
            results.append({
                "scenario": scenario["name"], "ok": False,
                "error": f"{type(error).__name__}: {error}",
            })

    totals = {
        "needed_total": sum(r.get("needed_total", 0) for r in results),
        "needed_kept": sum(r.get("needed_kept", 0) for r in results),
        "needed_dropped": sum(r.get("needed_dropped", 0) for r in results),
        "live_needed_total": sum(r.get("live_needed_total", 0) for r in results),
        "live_needed_kept": sum(r.get("live_needed_kept", 0) for r in results),
        "live_needed_dropped": sum(r.get("live_needed_dropped", 0) for r in results),
        "superseded_total": sum(r.get("superseded_total", 0) for r in results),
        "superseded_dropped": sum(r.get("superseded_dropped", 0) for r in results),
        "unneeded_total": sum(r.get("unneeded_total", 0) for r in results),
        "unneeded_dropped": sum(r.get("unneeded_dropped", 0) for r in results),
        "unneeded_kept": sum(r.get("unneeded_kept", 0) for r in results),
        "cost_microusd": sum(
            int((r.get("cost") or {}).get("cost_microusd") or 0) for r in results),
        "tokens": sum(
            int((r.get("cost") or {}).get("usage_total_tokens") or 0)
            for r in results),
    }
    report = {
        "provider": args.provider,
        "config": {
            "max_tokens": config.max_tokens,
            "max_entries": config.max_entries,
        },
        "totals": totals,
        "results": results,
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"provider: {args.provider}")
    for result in results:
        if not result.get("ok"):
            print(f"- {result['scenario']}: ERROR {result.get('error')}")
            continue
        print(
            f"- {result['scenario']}: needed kept "
            f"{result['needed_kept']}/{result['needed_total']}, "
            f"live needed kept {result['live_needed_kept']}/{result['live_needed_total']}, "
            f"superseded dropped {result['superseded_dropped']}/"
            f"{result['superseded_total']}, "
            f"unneeded dropped {result['unneeded_dropped']}/"
            f"{result['unneeded_total']}, "
            f"decided {result['decided']}, "
            f"cost {result['cost']['cost_microusd']}µUSD"
        )
    print(
        f"totals: needed kept {totals['needed_kept']}/{totals['needed_total']}, "
        f"live needed kept {totals['live_needed_kept']}/{totals['live_needed_total']}, "
        f"superseded dropped {totals['superseded_dropped']}/{totals['superseded_total']}, "
        f"unneeded dropped {totals['unneeded_dropped']}/{totals['unneeded_total']}, "
        f"cost {totals['cost_microusd']}µUSD / {totals['tokens']} tokens"
    )
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())