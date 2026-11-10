#!/usr/bin/env python3
"""Run prompt-level evals with a real provider.

Examples:
    python3 scripts/prompt_eval.py --provider deepseek --profile general-fast-native \
        --tasks readme-summary,injection-readme --limit 2
    python3 scripts/prompt_eval.py --provider deepseek --profile general \
        --system-prompt-file /tmp/candidate-v10.txt --variant-label general-v10-candidate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flowagent.evaluation.prompt_eval import (  # noqa: E402
    TASKS,
    provider_env,
    run_suite,
)
from flowagent.providers.prompt import PROMPT_PROFILES  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prompt-level evaluation (real model).")
    parser.add_argument("--provider", default="deepseek",
                        choices=("deepseek", "kimi", "local", "mock"))
    parser.add_argument("--profile", default="general",
                        choices=("general", "general-fast", "general-fast-native"))
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="",
                        help="provider base URL (or set DEEPSEEK_BASE_URL / KIMI_BASE_URL)")
    parser.add_argument("--config", default="",
                        help="chat-config.json path; defaults to the desktop config")
    parser.add_argument("--tasks", default="",
                        help="comma-separated task ids; default all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", default="")
    parser.add_argument("--system-prompt-file", default="",
                        help="candidate system prompt; runs as a variant of --profile")
    parser.add_argument("--variant-label", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for task in TASKS:
            print(f"{task.id:20} {task.category:10} {task.objective}")
        return 0

    task_ids = [item.strip() for item in args.tasks.split(",") if item.strip()]
    env = None
    if args.provider not in ("mock", "local"):
        env = provider_env(
            provider=args.provider,
            base_url=args.base_url or None,
            config_path=Path(args.config) if args.config else None,
        )
    report = run_suite(
        profile=args.profile,
        provider_name=args.provider,
        task_ids=task_ids or None,
        limit=args.limit or None,
        output_dir=Path(args.output) if args.output else None,
        env=env,
        model=args.model or None,
        system_prompt_file=args.system_prompt_file or None,
        variant_label=args.variant_label or None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["markdown"])
        print(f"output: {report['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())