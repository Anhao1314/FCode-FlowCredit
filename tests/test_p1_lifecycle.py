"""P1 regression tests: every launched process and owned pipe is reaped."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.commands import CommandRunner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.mcp import client as mcp_client_module  # noqa: E402
from flowagent.tools.mcp.client import McpStdioClient  # noqa: E402
from flowagent.tools.mcp.config import McpServerConfig  # noqa: E402

FAKE_SERVER = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_fake_server.py"


def _config(args: tuple[str, ...] = (str(FAKE_SERVER),), **overrides) -> McpServerConfig:
    values = {
        "name": "lifecycle",
        "command": sys.executable,
        "args": args,
        "timeout": 0.2,
    }
    values.update(overrides)
    return McpServerConfig(**values)


class CommandLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "workspace")
        self.runner = CommandRunner(self.sandbox)

    def _capture_process(self):
        import flowagent.tools.general.commands as commands_module

        real_popen = commands_module.subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        return processes, mock.patch.object(commands_module.subprocess, "Popen", side_effect=capture)

    def test_success_closes_the_merged_output_pipe(self) -> None:
        self.sandbox.write("ok.py", "print('ok')\n")
        processes, patcher = self._capture_process()
        with patcher:
            result = self.runner.run("python3", ["ok.py"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_timeout_kills_reaps_and_closes_the_process(self) -> None:
        self.sandbox.write("sleep.py", "import time\ntime.sleep(30)\n")
        processes, patcher = self._capture_process()
        with patcher, self.assertRaises(AgentError) as ctx:
            self.runner.run("python3", ["sleep.py"], timeout_seconds=1)
        self.assertEqual(ctx.exception.code, "COMMAND_TIMEOUT")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)


class McpLifecycleTests(unittest.TestCase):
    def assert_reaped(self, client: McpStdioClient, process, reader, drain) -> None:
        self.assertFalse(client.alive())
        self.assertIsNotNone(process.poll(), "the child must be waited for, not left as a zombie")
        for pipe in (process.stdin, process.stdout, process.stderr):
            self.assertIsNotNone(pipe)
            self.assertTrue(pipe.closed)
        reader.join(timeout=1)
        drain.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertFalse(drain.is_alive())

    def test_normal_stop_closes_all_pipes_joins_threads_and_is_idempotent(self) -> None:
        client = McpStdioClient(_config())
        client.start()
        process, reader, drain = client._process, client._reader, client._drain
        client.stop()
        self.assert_reaped(client, process, reader, drain)
        client.stop()

    def test_stop_after_child_death_still_reaps_pipes_and_threads(self) -> None:
        client = McpStdioClient(_config())
        client.start()
        process, reader, drain = client._process, client._reader, client._drain
        process.kill()
        process.wait(timeout=5)
        client.stop()
        self.assert_reaped(client, process, reader, drain)

    def test_failed_handshake_cleans_up_the_partially_started_client(self) -> None:
        real_popen = mcp_client_module.subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        # Reads requests but deliberately never answers initialize.
        silent = "import sys\nfor line in sys.stdin:\n    pass\n"
        client = McpStdioClient(_config(args=("-c", silent)))
        with mock.patch.object(mcp_client_module.subprocess, "Popen", side_effect=capture):
            with self.assertRaises(AgentError) as ctx:
                client.start()
        self.assertEqual(ctx.exception.code, "MCP_TIMEOUT")
        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertIsNotNone(process.poll())
        for pipe in (process.stdin, process.stdout, process.stderr):
            self.assertTrue(pipe.closed)
        self.assertFalse(client.alive())
        self.assertIsNone(client._reader)
        self.assertIsNone(client._drain)


if __name__ == "__main__":
    unittest.main()
