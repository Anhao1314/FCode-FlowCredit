"""CLI for the offline Runtime benchmark."""

from __future__ import annotations

import argparse
import json

from .runner import DEFAULT_OUTPUT, run_benchmark
from .tasks import taskset, taskset_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real Runtime tasks with scripted offline proposals")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--task", action="append", dest="tasks", help="run only this task id (repeatable)")
    parser.add_argument("--list", action="store_true", help="list the versioned task set without running")
    args = parser.parse_args()
    if args.list:
        print(json.dumps({
            "sha256": taskset_sha256(),
            "tasks": [{"task_id": item["task_id"], "category": item["category"]} for item in taskset()],
        }, ensure_ascii=False, indent=2))
        return 0
    completed = run_benchmark(args.output, task_ids=args.tasks)
    print(f"artifacts: {completed['output']}")
    print(f"tasks: {len(completed['results'])}; case expectations met: {completed['all_case_expectations_met']}")
    print("execution class: OFFLINE_SCRIPTED; ONLINE_MODEL: NOT_RUN")
    return completed["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
