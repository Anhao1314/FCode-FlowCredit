"""CLI and console share provider config, precedence and redacted status."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat_config import CHAT_CONFIG_VERSION, save_chat_config  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.demo.provider_factory import build_provider  # noqa: E402
from flowagent.providers.catalog import resolved_provider_env  # noqa: E402


class ProviderCliConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "chat-config.json"
        save_chat_config(self.path, {
            "version": CHAT_CONFIG_VERSION,
            "providers": {"kimi": {
                "api_key": "sk-user-config-secret",
                "model": "kimi-from-config",
                "input_price_microusd_per_mtok": "100",
                "output_price_microusd_per_mtok": "200",
                "price_as_of": "2026-10-26",
                "price_source": "test-price-snapshot",
            }},
        })

    def test_user_config_is_the_cli_fallback(self) -> None:
        env = resolved_provider_env(config_path=self.path, env={})
        self.assertEqual(env["KIMI_API_KEY"], "sk-user-config-secret")
        self.assertEqual(env["KIMI_MODEL"], "kimi-from-config")

    def test_environment_is_authoritative(self) -> None:
        env = resolved_provider_env(
            config_path=self.path,
            env={"KIMI_API_KEY": "sk-environment-override"},
        )
        self.assertEqual(env["KIMI_API_KEY"], "sk-environment-override")
        self.assertEqual(env["KIMI_MODEL"], "kimi-from-config")

    def test_default_cli_construction_reads_shared_config(self) -> None:
        store = TaskStore(self.root / "task.sqlite")
        self.addCleanup(store.close)
        with mock.patch.dict(os.environ, {"FCODE_CONFIG_DIR": str(self.root)}, clear=True):
            provider = build_provider(
                "kimi",
                scenario=get_scenario("full"),
                receipts=ReceiptStore(store),
            )
        self.assertEqual(provider.model, "kimi-from-config")
        self.assertEqual(provider.price.as_of, "2026-10-26")

    def test_status_reports_provenance_without_secrets(self) -> None:
        from flowagent.demo.__main__ import main

        output = io.StringIO()
        with mock.patch.dict(os.environ, {"FCODE_CONFIG_DIR": str(self.root)}, clear=True), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["--provider-status"]), 0)
        text = output.getvalue()
        self.assertIn("price_as_of=2026-10-26", text)
        self.assertIn("price_source=test-price-snapshot", text)
        self.assertIn(str(self.path), text)
        self.assertNotIn("sk-user-config-secret", text)
        self.assertNotIn("api_key", text)


if __name__ == "__main__":
    unittest.main()
