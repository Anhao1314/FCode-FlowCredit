"""Start the real console server for a test class, and keep its output.

Why this exists: two test classes boot ``viewer/server.py`` as a subprocess and
wait for ``/api/health``. On GitHub's macOS runners that wait kept expiring with
``RuntimeError: server did not become healthy`` while every local run passed —
and the reason was invisible, because the child's stdout/stderr went to
``subprocess.DEVNULL``. A crashed or wedged server left no trace at all in the CI
log, which is exactly how a red build survives twenty pushes without a diagnosis.

So the harness redirects both streams into temporary files (files, not pipes:
nothing can deadlock on a full pipe buffer) and, when the deadline passes, raises
with the process state — exit code if it died, "still running" if it is alive —
plus the tail of both files. The next failure names its own cause.

Knobs, for slow or unusual runners:
  FCODE_TEST_SERVER_TIMEOUT   seconds to wait for /api/health (default 30)
  FCODE_TEST_SERVER_VERBOSE=1 also print the server log on a successful boot
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "viewer" / "server.py"


def free_port() -> int:
    """A port nobody holds right now.

    Bound on the same interface the server will use (127.0.0.1) so the bind
    succeeds for the same reasons. The window between closing this socket and the
    child's bind is a race in principle; the health poll below reports a lost
    race as ``Address already in use`` instead of an unexplained timeout.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, *, method: str = "GET", body: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: float = 10.0):
    """The tiny JSON client the server tests share (kept here so both use one)."""
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, method=method, data=body, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class ConsoleServer:
    """A console server on a loopback port, with its output captured."""

    def __init__(
        self,
        *,
        runs_dir: pathlib.Path | str,
        config_dir: pathlib.Path | str,
        timeout: float | None = None,
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.port = free_port()
        self.timeout = float(
            timeout if timeout is not None
            else os.environ.get("FCODE_TEST_SERVER_TIMEOUT", "30")
        )
        self._logs = tempfile.TemporaryDirectory(prefix="fcode-server-logs-")
        self._out_path = pathlib.Path(self._logs.name) / "stdout.txt"
        self._err_path = pathlib.Path(self._logs.name) / "stderr.txt"
        env = dict(os.environ)
        env.update({
            "FCODE_RUNS_DIR": str(runs_dir),
            "FCODE_CONFIG_DIR": str(config_dir),
            "FCODE_SKIP_LEGACY_CONFIG_MIGRATION": "1",
            # The desktop bundle keeps its records elsewhere; a test server that
            # inherits an operator's FCODE_RUNS_DIR would test the wrong tree.
            "PYTHONUNBUFFERED": "1",
        })
        env.update(extra_env or {})
        self._env = env
        self._args = [sys.executable, str(SERVER), "--repo", str(ROOT), "--port", str(self.port)]
        self._args.extend(extra_args or [])
        self.process: subprocess.Popen | None = None
        self._out = None
        self._err = None

    # --------------------------------------------------------------- lifecycle
    def start(self) -> "ConsoleServer":
        self._out = open(self._out_path, "w", encoding="utf-8")
        self._err = open(self._err_path, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            self._args, cwd=str(ROOT), env=self._env, stdout=self._out, stderr=self._err,
        )
        deadline = time.time() + self.timeout
        last = ""
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    "server exited before becoming healthy"
                    f" (exit {self.process.returncode})\n{self.diagnostics()}"
                )
            try:
                status, _ = request(f"{self.base}/api/health", timeout=2.0)
                if status == 200:
                    if os.environ.get("FCODE_TEST_SERVER_VERBOSE") == "1":
                        sys.stderr.write(self.diagnostics())
                    return self
                last = f"health answered {status}"
            except Exception as error:  # noqa: BLE001 — still booting
                last = f"{type(error).__name__}: {error}"
            time.sleep(0.2)
        raise RuntimeError(
            "server did not become healthy within"
            f" {self.timeout:g}s (last attempt: {last})\n{self.diagnostics()}"
        )

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover — best effort
                self.process.kill()
        for handle in (self._out, self._err):
            if handle is not None and not handle.closed:
                handle.close()
        self._logs.cleanup()

    # ------------------------------------------------------------------ output
    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, suffix: str) -> str:
        return f"{self.base}{suffix}"

    def diagnostics(self, lines: int = 40) -> str:
        """Process state + the tail of both streams — the whole point."""
        state = "not started"
        if self.process is not None:
            state = (
                f"exit {self.process.returncode}" if self.process.poll() is not None
                else "still running"
            )
        parts = [f"server on 127.0.0.1:{self.port}: {state}"]
        for label, path in (("stdout", self._out_path), ("stderr", self._err_path)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            except OSError:  # pragma: no cover — a log that vanished is not a failure
                text = ""
            tail = "\n".join(text.strip().splitlines()[-lines:])
            parts.append(f"--- server {label} (last {lines} lines) ---\n{tail or '(empty)'}")
        return "\n".join(parts)