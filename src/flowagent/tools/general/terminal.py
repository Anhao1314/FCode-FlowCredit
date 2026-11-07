"""持久终端：一个跨工具调用存活的 bash 会话。

与 ``run_command`` 的区别：``run_command`` 每次调用创建一个新进程、
无 shell、参数逐个校验——适合单步操作。``terminal`` 维护一个持久的
bash 进程，``cd`` / ``export`` / 管道 / venv 激活跨调用存活——适合
多步工程循环（venv → install → test → fix → re-test）。

治理模型：terminal 是 WRITE effect + 进审批集——操作者批准一次，
会话即可在预算内自由使用。每次调用扣一次 call + 一次 read。
会话生命周期 = 运行生命周期；run 终态后 bash 进程随服务退出。

输出协议：往 stdin 写命令 + 唯一标记，从 stdout 读到标记即知道
命令结束与退出码——不用 PTY 的简化方案，够用于非交互命令。
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError

__all__ = ["PersistentShell", "TERMINAL_TIMEOUT_SECONDS", "TERMINAL_OUTPUT_CAP"]

#: 每条命令的超时。挂起的命令不应挂起运行。
TERMINAL_TIMEOUT_SECONDS = 60.0
#: 单次返回的输出字符上限。完整输出仍在终端会话里，可 tail 查看。
TERMINAL_OUTPUT_CAP = 8_000

_MARKER_FMT = "__FCODE_DONE_{n}_"


class PersistentShell:
    """跨工具调用存活的 bash 会话，绑定一个工作目录。

    线程安全：``execute`` 由运行线程串行调用（loop 不并行工具调用），
    但加锁以防未来的并行执行或 stop 请求竞争。
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._call_count = 0
        self._proc: subprocess.Popen | None = None

    def _ensure(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is not None:
            # 进程已退出（模型发了 exit 或 crash）——诚实重置
            self._proc = None
        if self._proc is None:
            self._proc = subprocess.Popen(
                ["/bin/bash", "--noprofile", "--norc"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(self.cwd),
                text=True,
            )
        return self._proc

    def execute(self, command: str, *, timeout: float = TERMINAL_TIMEOUT_SECONDS) -> dict[str, Any]:
        """执行一条命令，返回 {output, exit_code, call_count}。"""
        command = str(command or "").strip()
        if not command:
            raise AgentError("TERMINAL_CMD_EMPTY", "command must not be empty")
        if len(command) > 4_000:
            raise AgentError("TERMINAL_CMD_TOO_LONG", "command must be at most 4000 chars")

        with self._lock:
            proc = self._ensure()
            self._call_count += 1
            marker = _MARKER_FMT.format(n=self._call_count)

            try:
                proc.stdin.write(f"{command}\necho {marker}_$?\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as err:
                self._proc = None
                raise AgentError("TERMINAL_DEAD", f"terminal session died: {err}") from err

            output_lines: list[str] = []
            exit_code: int | None = None
            import time as _time

            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    # EOF:进程死了
                    self._proc = None
                    raise AgentError("TERMINAL_DEAD", "terminal session closed unexpectedly")
                stripped = line.rstrip("\n")
                if stripped.startswith(marker):
                    suffix = stripped[len(marker):]
                    try:
                        exit_code = int(suffix.lstrip("_"))
                    except ValueError:
                        exit_code = 1
                    break
                output_lines.append(stripped)
            else:
                raise AgentError(
                    "TERMINAL_TIMEOUT",
                    f"command did not complete within {timeout}s",
                )

            if exit_code is None:
                exit_code = 1

            output = "\n".join(output_lines)
            truncated = False
            if len(output) > TERMINAL_OUTPUT_CAP:
                output = output[:TERMINAL_OUTPUT_CAP]
                truncated = True

            return {
                "output": output,
                "exit_code": exit_code,
                "call_count": self._call_count,
                "truncated": truncated,
            }

    def close(self) -> None:
        """终止 bash 进程。幂等。"""
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:  # noqa: BLE001 — 进程可能已退出
                    pass
                self._proc = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
