"""Independent-process SIGKILL injection around the production Runtime loop."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from ..core.store import TaskStore
from .runner import build_stack
from .tasks import get_task


def _die(run_dir: Path, counts: dict[str, int], checkpoint: str) -> None:
    (run_dir / "worker-executions.json").write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
    print(f"pid={os.getpid()} checkpoint={checkpoint}", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def main() -> int:
    run_dir = Path(os.environ["BENCHMARK_RUN_DIR"])
    definition = get_task(os.environ["BENCHMARK_TASK_ID"])
    mode = definition["crash"]["mode"]
    stack = build_stack(definition, run_dir)
    task_id = definition["task_id"]

    if mode == "after_commit":
        original_save = TaskStore.save

        def kill_after_commit(self, task, expected_sequence, event_type, detail=None):  # type: ignore[no-untyped-def]
            result = original_save(self, task, expected_sequence, event_type, detail)
            if event_type == "tool_succeeded":
                _die(run_dir, stack.tool_executions, "tool_succeeded_committed")
            return result

        TaskStore.save = kill_after_commit  # type: ignore[method-assign]
    else:
        original_execute = stack.registry.execute
        seen = {"count": 0}

        def kill_around_tool(name, arguments):  # type: ignore[no-untyped-def]
            seen["count"] += 1
            if seen["count"] == 1 and mode == "before_tool":
                _die(run_dir, stack.tool_executions, "before_registry_execute")
            observation = original_execute(name, arguments)
            if seen["count"] == 1 and mode == "after_side_effect":
                _die(run_dir, stack.tool_executions, "after_handler_before_observation_commit")
            return observation

        stack.registry.execute = kill_around_tool  # type: ignore[method-assign]
    stack.loop.run(task_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
