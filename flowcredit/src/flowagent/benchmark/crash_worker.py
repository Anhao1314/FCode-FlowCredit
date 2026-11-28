"""Independent-process SIGKILL injection around the production Runtime loop.

Shared by the runtime benchmark and the general-agent evaluation suite: the
taskset is chosen by ``BENCHMARK_TASKSET`` (``runtime`` default, ``general``),
while the Loop/Harness/gates stay the production ones in both cases.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from ..core.store import TaskStore
from .runner import build_stack
from .tasks import get_task


def _stack_for(run_dir: Path) -> tuple[dict[str, Any], Any]:
    taskset_name = os.environ.get("BENCHMARK_TASKSET", "runtime")
    task_id = os.environ["BENCHMARK_TASK_ID"]
    if taskset_name == "general":
        from ..evaluation.general_eval import build_general_stack
        from ..evaluation.general_tasks import get_general_task

        definition = get_general_task(task_id)
        return definition, build_general_stack(definition, run_dir)
    definition = get_task(task_id)
    return definition, build_stack(definition, run_dir)


def _die(run_dir: Path, counts: dict[str, int], checkpoint: str) -> None:
    (run_dir / "worker-executions.json").write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
    print(f"pid={os.getpid()} checkpoint={checkpoint}", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def main() -> int:
    run_dir = Path(os.environ["BENCHMARK_RUN_DIR"])
    definition, stack = _stack_for(run_dir)
    mode = definition["crash"]["mode"]
    task_id = definition["task_id"]

    if mode == "after_commit":
        original_save = TaskStore.save
        original_save_many = TaskStore.save_many

        def kill_after_commit(self, task, expected_sequence, event_type, detail=None):  # type: ignore[no-untyped-def]
            result = original_save(self, task, expected_sequence, event_type, detail)
            if event_type == "tool_succeeded":
                _die(run_dir, stack.tool_executions, "tool_succeeded_committed")
            return result

        def kill_after_commit_many(self, task, events):  # type: ignore[no-untyped-def]
            # The batch is atomic: tool_succeeded and its audit fact commit
            # together. Kill only after that commit point, exactly like the
            # single-event path above.
            result = original_save_many(self, task, events)
            if any(event_type == "tool_succeeded" for event_type, _ in events):
                _die(run_dir, stack.tool_executions, "tool_succeeded_committed")
            return result

        TaskStore.save = kill_after_commit  # type: ignore[method-assign]
        TaskStore.save_many = kill_after_commit_many  # type: ignore[method-assign]
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
