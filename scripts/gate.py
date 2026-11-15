#!/usr/bin/env python3
"""FCode 本地门禁：把"敢改代码"需要的全部检查收敛成一条命令。

五道门（顺序执行，一票否决）：
  1. backend   Python 单测（unittest discover）
  2. frontend  前端单测（node --test viewer/tests）
  3. benchmark 离线 agent 回归（flowagent.benchmark，19 个任务）+ 指标阈值断言
  4. audit     审计自检（规则集扫本仓库，verify 必须通过且无 new 发现）
  5. e2e       Playwright 浏览器端到端（离线确定性 mock，12 例）

报告落 work/gate/report.json 与 work/gate/report.md；任何一道失败退出码非 0。
用法：python3 scripts/gate.py [--only backend,e2e] [--skip e2e] [--list]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "work" / "gate"

# benchmark 指标阈值：治理主张里"必须恒为 1"的部分。回归时这里先红。
METRIC_THRESHOLDS = {
    "pipeline_completion_rate": 1.0,
    "approval_gate_accuracy": 1.0,
    "budget_enforcement_accuracy": 1.0,
    "crash_recovery_rate": 1.0,
    "hash_chain_tamper_detection_rate": 1.0,
    "replay_equivalence_rate": 1.0,
    "policy_block_accuracy": 1.0,
}

CHECKS = ["backend", "frontend", "benchmark", "audit", "e2e"]


def run(name: str, argv: list[str], cwd: Path = PROJECT_ROOT, timeout: int = 900) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={"PATH": __import__("os").environ["PATH"], "PYTHONPATH": "src", "HOME": __import__("os").environ.get("HOME", "")},
        )
        ok = proc.returncode == 0
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-(40 if ok else 140):])
        return ok, tail, time.monotonic() - started
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s", time.monotonic() - started
    except FileNotFoundError as error:
        return False, f"command not found: {error}", time.monotonic() - started


def check_backend() -> tuple[bool, dict]:
    ok, tail, seconds = run("backend", [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    return ok, {"tail": tail, "seconds": round(seconds, 1)}


def check_frontend() -> tuple[bool, dict]:
    ok, tail, seconds = run("frontend", ["node", "--test"] + [str(p) for p in sorted((PROJECT_ROOT / "viewer" / "tests").glob("*.test.js"))])
    return ok, {"tail": tail, "seconds": round(seconds, 1)}


def check_benchmark() -> tuple[bool, dict]:
    out = OUT_DIR / "benchmark"
    ok, tail, seconds = run(
        "benchmark",
        [sys.executable, "-m", "flowagent.benchmark", "--output", str(out)],
        timeout=1200,
    )
    detail: dict = {"tail": tail, "seconds": round(seconds, 1)}
    if not ok:
        detail["metrics"] = "benchmark 运行失败，不做阈值断言"
        return False, detail

    summary_path = out / "summary.json"
    if not summary_path.exists():
        detail["metrics"] = f"缺少 {summary_path}"
        return False, detail
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    metrics = summary.get("metrics", {})

    breaches = []
    if not summary.get("all_case_expectations_met"):
        breaches.append("all_case_expectations_met is false")
    for key, expected in METRIC_THRESHOLDS.items():
        metric = metrics.get(key) or {}
        value = metric.get("value")
        if value is None:
            breaches.append(f"{key}: 无值")
        elif value < expected:
            missed = (metric.get("task_ids") or [])[:6]
            breaches.append(f"{key}: {value:.4f} < {expected}（如 {', '.join(missed)}）")
    unauth = (metrics.get("unauthorized_side_effect_count") or {}).get("count")
    if unauth != 0:
        breaches.append(f"unauthorized_side_effect_count={unauth}（必须为 0）")

    detail["metrics"] = {
        key: (metrics.get(key) or {}).get("value") for key in METRIC_THRESHOLDS
    } | {"unauthorized_side_effect_count": unauth, "all_case_expectations_met": summary.get("all_case_expectations_met")}
    detail["breaches"] = breaches
    return (not breaches), detail


def check_audit() -> tuple[bool, dict]:
    out = OUT_DIR / "audit-self"
    ok, tail, seconds = run(
        "audit",
        [sys.executable, "-m", "flowaudit.cli", "audit", ".", "--out", str(out), "--operator", "gate"],
        timeout=600,
    )
    detail: dict = {"tail": tail, "seconds": round(seconds, 1)}
    if not ok:
        return False, detail
    bundle_path = out / "audit-bundle.json"
    if not bundle_path.exists():
        detail["audit"] = f"缺少 {bundle_path}"
        return False, detail
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    regression = bundle.get("regression") or {}
    new_findings = regression.get("new", 0)
    detail["audit"] = {
        "audit_id": bundle.get("audit_id"),
        "ruleset_version": (bundle.get("toolchain") or {}).get("ruleset_version"),
        "findings": len(bundle.get("findings") or []),
        "new": new_findings,
        "fixed": regression.get("fixed", 0),
    }
    if new_findings:
        detail["audit"]["breach"] = f"出现 {new_findings} 个 new 发现——先修或先入账（known），门禁不允许无声回归"
        return False, detail
    return True, detail


def check_e2e() -> tuple[bool, dict]:
    # playwright 的 [WebServer] 日志与结果行交叠,直接捕获会把结果淹没在
    # 请求日志里——落盘后按结果行提取失败摘要。
    started = time.monotonic()
    out_path = OUT_DIR / "e2e-output.txt"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

    # Playwright keeps one transient output tree per run. After a few runs it
    # can hold thousands of trace artifacts, and its startup cleanup becomes a
    # single bulk delete that sandboxed hosts refuse. Move the stale tree aside
    # with one atomic rename (no bulk deletion) before Node starts, so every
    # run begins from an empty output directory. This is transient test output,
    # never run evidence; the archive lives in the OS temp dir, not the repo.
    stale_output = PROJECT_ROOT / "viewer" / "test-results"
    if stale_output.exists():
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        archived = Path(tempfile.gettempdir()) / f"fcode-e2e-stale-{int(started)}-{os.getpid()}"
        try:
            os.replace(stale_output, archived)
        except OSError:
            # Different filesystem: fall back to a same-volume scratch path.
            try:
                os.replace(stale_output, OUT_DIR / f"e2e-stale-{int(started)}-{os.getpid()}")
            except OSError:
                pass

    env = dict(os.environ)
    try:
        with out_path.open("w", encoding="utf-8") as sink:
            proc = subprocess.run(
                ["npm", "exec", "--", "playwright", "test"],
                cwd=PROJECT_ROOT / "viewer", stdout=sink, stderr=subprocess.STDOUT,
                timeout=1200, env=env,
            )
        ok = proc.returncode == 0
        text = out_path.read_text(encoding="utf-8", errors="replace")
        if ok:
            summary = next((l for l in text.splitlines() if " passed" in l), "")
            return True, {"tail": summary, "seconds": round(time.monotonic() - started, 1)}
        # 失败:取 ✘ 行与其后首个 Error 摘要
        lines = text.splitlines()
        keep = []
        for i, line in enumerate(lines):
            if "✘" in line or line.strip().startswith("Error:"):
                keep.extend(lines[i:i + 6])
                if len(keep) > 60:
                    break
        return False, {"tail": "\n".join(keep[:60]) or text[-2000:], "seconds": round(time.monotonic() - started, 1)}
    except subprocess.TimeoutExpired:
        return False, {"tail": "TIMEOUT after 1200s", "seconds": round(time.monotonic() - started, 1)}
    except FileNotFoundError as error:
        return False, {"tail": f"command not found: {error}", "seconds": round(time.monotonic() - started, 1)}


CHECK_FUNCS = {
    "backend": check_backend,
    "frontend": check_frontend,
    "benchmark": check_benchmark,
    "audit": check_audit,
    "e2e": check_e2e,
}


def write_report(results: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "passed": all(r["ok"] for r in results.values()),
        "results": results,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# FCode 门禁报告", "", f"结论：{'✅ 全部通过' if report['passed'] else '❌ 存在失败'}", ""]
    for name in CHECKS:
        if name not in results:
            continue
        r = results[name]
        lines.append(f"## {'✅' if r['ok'] else '❌'} {name}（{r['seconds']}s）")
        body = r.get("metrics") or r.get("audit")
        if isinstance(body, dict):
            lines.append("```json")
            lines.append(json.dumps(body, ensure_ascii=False, indent=2))
            lines.append("```")
        lines.append("")
    (OUT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="FCode 本地门禁")
    parser.add_argument("--only", help="只跑这些检查（逗号分隔）")
    parser.add_argument("--skip", help="跳过这些检查（逗号分隔）")
    parser.add_argument("--list", action="store_true", help="列出检查项")
    args = parser.parse_args()
    if args.list:
        print("\n".join(CHECKS))
        return 0
    selected = CHECKS
    if args.only:
        selected = [c for c in CHECKS if c in args.only.split(",")]
    if args.skip:
        selected = [c for c in selected if c not in args.skip.split(",")]

    results: dict = {}
    failed = False
    for name in selected:
        print(f"[gate] {name} …", flush=True)
        ok, detail = CHECK_FUNCS[name]()
        results[name] = {"ok": ok, "seconds": detail.get("seconds"), **detail}
        print(f"[gate] {name}: {'✅' if ok else '❌'}（{detail.get('seconds')}s）", flush=True)
        if not ok:
            failed = True
            tail = detail.get("tail", "")
            if tail:
                print("\n".join("       " + line for line in tail.splitlines()[-25:]), flush=True)

    write_report(results)
    total = sum(r.get("seconds") or 0 for r in results.values())
    print(f"[gate] 结论：{'✅ 全部通过' if not failed else '❌ 存在失败'} · 报告 {OUT_DIR/'report.md'} · 总耗时 {total:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
