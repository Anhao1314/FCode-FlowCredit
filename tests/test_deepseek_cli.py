"""DeepSeek unified CLI behavior without network access or live credentials."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.demo import api as api_lab  # noqa: E402
from flowagent.demo import deepseek as deepseek_lab  # noqa: E402
from flowagent.demo import packet as packet_lab  # noqa: E402


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "DEEPSEEK_API_KEY": "sk-test-not-real",
        "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK": "270000",
        "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK": "1100000",
        "DEEPSEEK_PRICE_AS_OF": "2026-10-24",
    }
    env.update(overrides)
    return env


class DeepSeekCLITests(unittest.TestCase):
    def test_preflight_does_not_call_the_network_or_leak_the_key(self) -> None:
        output = io.StringIO()

        def no_network(request, timeout):  # noqa: ARG001
            raise AssertionError("preflight must not open a socket")

        with mock.patch.dict(os.environ, _env()), \
                mock.patch(
                    "flowagent.providers.deepseek.urllib.request.urlopen",
                    no_network,
                ), contextlib.redirect_stdout(output):
            code = deepseek_lab.main([
                "--workdir", "/tmp/fcode-ds-cli-preflight",
                "--preflight-only",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["networkCalled"])
        self.assertEqual(payload["config"]["apiKey"], "env:DEEPSEEK_API_KEY")
        self.assertNotIn("sk-test-not-real", output.getvalue())
        self.assertTrue(payload["scenarioBudget"]["reservationWithinBudget"])

    def test_missing_price_fails_before_any_request(self) -> None:
        env = _env()
        del env["DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK"]
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            deepseek_lab.main([
                "--workdir", "/tmp/fcode-ds-cli-missing-price",
                "--preflight-only",
            ])
        self.assertIn("DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK", stderr.getvalue())
        self.assertNotIn("sk-test-not-real", stderr.getvalue())

    def test_packet_open_ended_expected_failure_exits_zero(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {
                "status": "failed",
                "failure_code": "INSUFFICIENT_EVIDENCE",
                "verified": True,
            }

        output = io.StringIO()
        with mock.patch.dict(os.environ, _env()), \
                mock.patch.object(deepseek_lab, "run_packet", fake_run), \
                contextlib.redirect_stdout(output):
            code = deepseek_lab.main([
                "--workdir", "/tmp/fcode-ds-cli-open",
                "--mode", "packet",
                "--scenario", "open-ended",
                "--fresh",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured["policy_name"], "deepseek")
        self.assertEqual(captured["model"], "deepseek-chat")
        self.assertEqual(captured["json_mode"], True)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["outcome_ok"])
        self.assertEqual(payload["expected_status"], "failed")
        self.assertNotIn("sk-test-not-real", output.getvalue())

    def test_proposal_mode_builds_deepseek_provider(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "verified": True}

        output = io.StringIO()
        with mock.patch.dict(os.environ, _env()), \
                mock.patch.object(deepseek_lab, "run_proposal", fake_run), \
                contextlib.redirect_stdout(output):
            code = deepseek_lab.main([
                "--workdir", "/tmp/fcode-ds-cli-proposal",
                "--mode", "proposal",
                "--scenario", "full",
                "--fresh",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured["provider"].name, "deepseek")
        self.assertEqual(captured["provider"].model, "deepseek-chat")
        self.assertEqual(captured["provider"].max_tokens, 256)

    def test_legacy_packet_cli_uses_expected_terminal_status(self) -> None:
        def fake_run(**kwargs):  # noqa: ARG001
            return {
                "status": "failed",
                "failure_code": "INSUFFICIENT_EVIDENCE",
                "verified": True,
            }

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(packet_lab, "run", fake_run), \
                contextlib.redirect_stdout(output):
            code = packet_lab.main([
                "--workdir", tmp,
                "--scenario", "open-ended",
                "--policy", "mock",
                "--fresh",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["expected_status"], "failed")
        self.assertTrue(payload["outcome_ok"])

    def test_legacy_api_cli_reads_prices_from_environment(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "verified": True, "outcome_ok": True}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, _env()), \
                mock.patch.object(api_lab, "run", fake_run), \
                contextlib.redirect_stdout(output):
            code = api_lab.main([
                "--workdir", tmp,
                "--scenario", "full",
                "--fresh",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured["provider"].name, "deepseek")
        self.assertEqual(captured["provider"].price.input_microusd_per_mtok, 270000)
        self.assertEqual(captured["provider"].price.output_microusd_per_mtok, 1100000)
        self.assertEqual(captured["provider"].max_tokens, 256)


if __name__ == "__main__":
    unittest.main()
