"""CLI for the general-agent evaluation and the intent-router evaluation.

Offline by default. Online runs require ``--online`` plus an explicit
``--confirm-paid`` acknowledgement; missing key, model name, price snapshot or
confirmation fails closed with ``ONLINE_MODEL=NOT_RUN`` and a non-zero exit —
no silent mock fallback, no silent offline substitution.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .coding_eval import DEFAULT_OUTPUT as DEFAULT_CODING_OUTPUT
from .coding_eval import run_coding_suite
from .coding_tasks import coding_taskset, coding_taskset_sha256
from .general_eval import (
    DEFAULT_OUTPUT,
    check_online_gate,
    redact_text,
    run_general_suite,
)
from .general_tasks import general_taskset, general_taskset_sha256
from .prompt_eval import provider_env
from .router_eval import evaluate_router, summary_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROUTER_OUTPUT = PROJECT_ROOT / "work" / "intent-router-eval" / "latest"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_general(args: argparse.Namespace) -> int:
    if args.list:
        print(json.dumps({
            "sha256": general_taskset_sha256(),
            "tasks": [
                {"task_id": item["task_id"], "category": item["category"],
                 "online": item["applicability"]["ONLINE_MODEL"].startswith("real-model")}
                for item in general_taskset()
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    env = None
    if args.online:
        env = provider_env(provider=args.provider)
        gate = check_online_gate(
            provider_name=args.provider, model=args.model, env=env,
            confirm_paid=args.confirm_paid,
        )
        if not gate["ok"]:
            print(f"ONLINE_MODEL=NOT_RUN — {gate['reason']}")
            print("真实模型通过率 = NOT_MEASURED")
            return 3
    completed = run_general_suite(
        args.output,
        task_ids=args.tasks,
        repeat=args.repeat,
        mode="online" if args.online else "offline",
        provider_name=args.provider if args.online else "mock",
        model=args.model if args.online else None,
        env=env,
    )
    # Operator-supplied credentials never reach the reports.
    secrets = [
        str(value) for key, value in (env or {}).items()
        if key.endswith("_API_KEY") and value
    ]
    if secrets:
        for name in ("summary.md", "summary.json", "run_manifest.json"):
            target = Path(completed["output"]) / name
            if target.exists():
                _write(target, redact_text(target.read_text(encoding="utf-8"), secrets))
    print(f"artifacts: {completed['output']}")
    print(f"runs: {len(completed['results'])}; case expectations met: "
          f"{completed['all_case_expectations_met']}")
    if args.online:
        print(f"execution class: ONLINE_MODEL · provider {args.provider} · model {args.model or '(configured)'}")
    else:
        print("execution class: OFFLINE_SCRIPTED")
        print("ONLINE_MODEL=NOT_RUN")
        print("真实模型通过率 = NOT_MEASURED")
    return completed["exit_code"]


def run_intent(args: argparse.Namespace) -> int:
    report = evaluate_router()
    output_root = Path(args.output) if args.output else ROUTER_OUTPUT
    output_root.mkdir(parents=True, exist_ok=True)
    _write(output_root / "summary.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write(output_root / "summary.md", summary_markdown(report))
    metrics = report["metrics"]
    print(f"artifacts: {output_root}")
    print(f"cases: {len(report['rows'])}; accuracy: {metrics['accuracy']['value']}")
    print(f"chat→run: {metrics['chat_misrouted_to_run']['count']} · "
          f"work→chat: {metrics['work_missed_into_chat']['count']} · "
          f"clarify share: {metrics['clarify_share']['value']} · "
          f"safe fallback: {metrics['safe_fallback_rate']['value']}")
    return 0 if metrics["accuracy"]["value"] == 1 else 1


def run_coding(args: argparse.Namespace) -> int:
    output_root = args.output if args.output else DEFAULT_CODING_OUTPUT
    if args.list:
        print(json.dumps({
            "sha256": coding_taskset_sha256(),
            "tasks": [
                {"task_id": item["task_id"], "category": item["category"],
                 "difficulty": item["difficulty"],
                 "hidden_test_sha256": item["hidden_test"]["content_sha256"]}
                for item in coding_taskset()
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    completed = run_coding_suite(
        output_root,
        task_ids=args.tasks,
        repeat=args.repeat,
    )
    print(f"artifacts: {completed['output']}")
    print(f"runs: {len(completed['results'])}; case expectations met: "
          f"{completed['all_case_expectations_met']}; hidden tests passed: "
          f"{completed['all_hidden_tests_passed']}")
    print("execution class: OFFLINE_SCRIPTED")
    print("ONLINE_MODEL=NOT_RUN")
    print("真实模型通过率 = NOT_MEASURED")
    return completed["exit_code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="General-agent task evaluation (offline default; online requires paid confirmation)")
    parser.add_argument("--suite", choices=("general", "intent", "coding"), default="general")
    parser.add_argument("--output", default=None, help="output directory (default work/<suite>/latest)")
    parser.add_argument("--task", action="append", dest="tasks",
                        help="run only this task id (repeatable)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat every task N times; every raw run is retained")
    parser.add_argument("--list", action="store_true", help="list the task set")
    parser.add_argument("--online", action="store_true",
                        help="run with a real provider (requires --confirm-paid)")
    parser.add_argument("--provider", default="deepseek",
                        help="online provider key (deepseek/kimi/local/declared compat)")
    parser.add_argument("--model", default=None, help="online model override")
    parser.add_argument("--confirm-paid", default=None,
                        help='must contain "authorize paid online evaluation"')
    args = parser.parse_args(argv)
    if args.suite == "intent":
        return run_intent(args)
    if args.suite == "coding":
        return run_coding(args)
    return run_general(args)


if __name__ == "__main__":
    raise SystemExit(main())
