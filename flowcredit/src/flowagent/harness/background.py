"""Background tasks (S4): detached shells, incremental output, kill.

Ported shape from the dissected harness: a long-running command can be started
without blocking the loop; its stdout/stderr stream into a log file; the model
polls incremental slices with ``task_output`` and can ``kill_shell`` it.

The manager is deliberately process-local: logs survive a restart, live
process handles do not. Tool factories at the bottom expose the three
operations to a ``HarnessCore`` registry.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .tools import ToolCallContext, ToolResult, ToolSpec

__all__ = [
    "BackgroundLimitError",
    "BackgroundTask",
    "BackgroundTasks",
    "background_tools",
    "kill_shell_tool",
    "run_background_tool",
    "task_output_tool",
]

Clock = Callable[[], float]
IdFactory = Callable[[int], str]


class BackgroundLimitError(RuntimeError):
    code = "TASK_LIMIT"


@dataclass
class BackgroundTask:
    task_id: str
    command: str
    status: str  # running | completed | failed | killed
    started_at_ms: int
    output_path: Path
    pid: int
    label: str = ""
    ended_at_ms: int | None = None
    returncode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "command": self.command,
            "status": self.status,
            "pid": int(self.pid),
            "label": self.label,
            "started_at_ms": int(self.started_at_ms),
            "ended_at_ms": (int(self.ended_at_ms)
                            if self.ended_at_ms is not None else None),
            "exit_code": (int(self.returncode)
                          if self.returncode is not None else None),
            "output_path": str(self.output_path),
        }


class BackgroundTasks:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_running: int = 8,
        clock: Clock = time.time,
        id_factory: IdFactory | None = None,
    ) -> None:
        self.root = Path(root) / "background"
        self.max_running = max(1, int(max_running))
        self._clock = clock
        self._id_factory = id_factory or (lambda number: f"bg{number}")
        self._lock = threading.Lock()
        self._tasks: dict[str, BackgroundTask] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._counter = 0

    # ------------------------------------------------------------------- start

    def start(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        label: str = "",
    ) -> BackgroundTask:
        text = str(command or "").strip()
        if not text:
            raise ValueError("background command must not be empty")
        with self._lock:
            for task in self._tasks.values():
                self._refresh(task)
            running = sum(1 for task in self._tasks.values()
                          if task.status == "running")
            if running >= self.max_running:
                raise BackgroundLimitError(
                    f"{running} background tasks already running")
            self._counter += 1
            task_id = self._id_factory(self._counter)
            suffix = 2
            while task_id in self._tasks:
                task_id = f"{self._id_factory(self._counter)}-{suffix}"
                suffix += 1
            self.root.mkdir(parents=True, exist_ok=True)
            log_path = self.root / f"{task_id}.log"
            handle = log_path.open("ab")
            try:
                process = subprocess.Popen(
                    text,
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(cwd) if cwd is not None else None,
                    env=dict(env) if env is not None else None,
                    start_new_session=True,
                )
            finally:
                handle.close()
            task = BackgroundTask(
                task_id=task_id,
                command=text,
                status="running",
                started_at_ms=int(self._clock() * 1000),
                output_path=log_path,
                pid=int(process.pid or 0),
                label=str(label or ""),
            )
            self._tasks[task_id] = task
            self._processes[task_id] = process
            return task

    # ------------------------------------------------------------------ status

    def status(self, task_id: str) -> BackgroundTask:
        with self._lock:
            task = self._task(task_id)
            self._refresh(task)
            return task

    def list(self) -> list[BackgroundTask]:
        with self._lock:
            for task in self._tasks.values():
                self._refresh(task)
            return list(self._tasks.values())

    def output(
        self,
        task_id: str,
        *,
        offset: int = 0,
        limit: int = 20_000,
        block: bool = False,
        timeout_ms: int = 0,
    ) -> dict[str, Any]:
        start = max(0, int(offset))
        width = max(1, int(limit))
        wait_ms = int(timeout_ms) if int(timeout_ms) > 0 else 30_000
        deadline = time.monotonic() + wait_ms / 1000 if block else None
        while True:
            with self._lock:
                task = self._task(task_id)
                self._refresh(task)
                status = task.status
            data = self._read_slice(task, start, width)
            remaining = 0
            try:
                remaining = max(0, task.output_path.stat().st_size - start - len(data))
            except OSError:
                remaining = 0
            eof = status != "running" and remaining == 0
            view = task.to_dict()
            view.update({
                "output": data.decode("utf-8", "replace"),
                "next_offset": start + len(data),
                "eof": bool(eof),
                "remaining_bytes": int(remaining),
            })
            if not block or eof or len(data) >= width:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                return view
            time.sleep(0.05)

    def kill(self, task_id: str, *, timeout_ms: int = 3_000) -> dict[str, Any]:
        with self._lock:
            task = self._task(task_id)
            self._refresh(task)
            if task.status != "running":
                return task.to_dict()
            process = self._processes.get(task_id)
            if process is None:
                task.status = "killed"
                task.ended_at_ms = int(self._clock() * 1000)
                return task.to_dict()
            process.terminate()
            try:
                process.wait(timeout=max(0.1, timeout_ms / 1000))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            task.status = "killed"
            task.ended_at_ms = int(self._clock() * 1000)
            task.returncode = process.poll()
            return task.to_dict()

    # ----------------------------------------------------------------- private

    def _task(self, task_id: str) -> BackgroundTask:
        key = str(task_id)
        task = self._tasks.get(key)
        if task is None:
            raise KeyError(f"unknown background task {key!r}")
        return task

    def _refresh(self, task: BackgroundTask) -> None:
        if task.status != "running":
            return
        process = self._processes.get(task.task_id)
        if process is None:
            return
        code = process.poll()
        if code is None:
            return
        task.returncode = int(code)
        task.ended_at_ms = int(self._clock() * 1000)
        task.status = "completed" if code == 0 else "failed"

    @staticmethod
    def _read_slice(task: BackgroundTask, offset: int, limit: int) -> bytes:
        try:
            with task.output_path.open("rb") as handle:
                handle.seek(offset)
                return handle.read(limit)
        except FileNotFoundError:
            return b""


# ----------------------------------------------------------------------- tools


def run_background_tool(tasks: BackgroundTasks) -> ToolSpec:
    def handler(args: Mapping[str, Any], context: ToolCallContext) -> ToolResult:
        try:
            task = tasks.start(
                str(args.get("command") or ""),
                cwd=str(args["cwd"]) if args.get("cwd") else None,
                label=str(args.get("label") or ""),
            )
        except BackgroundLimitError as error:
            return ToolResult.error(error.code, str(error))
        except ValueError as error:
            return ToolResult.error("TASK_INPUT_INVALID", str(error))
        except OSError as error:
            return ToolResult.error(
                "TASK_START_FAILED", f"{type(error).__name__}: {error}")
        _emit(context, "background.start", task_id=task.task_id,
              pid=task.pid, command_chars=len(task.command))
        return ToolResult.ok(
            f"started {task.task_id} (pid {task.pid})", data=task.to_dict())

    return ToolSpec(
        name="run_background",
        description=(
            "Start a shell command without blocking the loop. Poll it with "
            "task_output; stop it with kill_shell."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["command"],
        },
        handler=handler,
    )


def task_output_tool(tasks: BackgroundTasks) -> ToolSpec:
    def handler(args: Mapping[str, Any], context: ToolCallContext) -> ToolResult:
        try:
            view = tasks.output(
                str(args.get("task_id") or ""),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 20_000),
                block=bool(args.get("block") or False),
                timeout_ms=int(args.get("timeout_ms") or 0),
            )
        except KeyError as error:
            return ToolResult.error("TASK_UNKNOWN", str(error))
        _emit(context, "background.output", task_id=view["task_id"],
              status=view["status"], bytes=len(view["output"]))
        return ToolResult.ok(str(view["output"]), data=view)

    return ToolSpec(
        name="task_output",
        description=(
            "Read incremental stdout/stderr of a background task. Use "
            "next_offset to resume; set block=true to wait briefly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "block": {"type": "boolean"},
                "timeout_ms": {"type": "integer"},
            },
            "required": ["task_id"],
        },
        handler=handler,
    )


def kill_shell_tool(tasks: BackgroundTasks) -> ToolSpec:
    def handler(args: Mapping[str, Any], context: ToolCallContext) -> ToolResult:
        try:
            view = tasks.kill(str(args.get("task_id") or ""))
        except KeyError as error:
            return ToolResult.error("TASK_UNKNOWN", str(error))
        _emit(context, "background.kill", task_id=view["task_id"],
              status=view["status"])
        return ToolResult.ok(f"{view['task_id']}: {view['status']}", data=view)

    return ToolSpec(
        name="kill_shell",
        description="Terminate a background task and return its final status.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        handler=handler,
    )


def background_tools(tasks: BackgroundTasks) -> list[ToolSpec]:
    return [
        run_background_tool(tasks),
        task_output_tool(tasks),
        kill_shell_tool(tasks),
    ]


def _emit(context: ToolCallContext, event_type: str, **payload: Any) -> None:
    bus = getattr(context, "events", None)
    if bus is None or not hasattr(bus, "emit"):
        return
    bus.emit(event_type, **payload)
