"""Desktop provider configuration must not inherit its parent agent's session."""

from __future__ import annotations

import unittest

from desktop.app import isolate_provider_environment


class ProviderEnvironmentTests(unittest.TestCase):
    def test_saved_desktop_settings_are_isolated_from_host_providers(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "KIMI_API_KEY": "host-key",
            "KIMI_BASE_URL": "https://agent-gw.invalid/coding/",
            "KIMI_MODEL": "host-model",
            "DEEPSEEK_API_KEY": "host-deepseek-key",
            "FCODE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1",
        }

        isolate_provider_environment(env)

        self.assertEqual(env, {
            "PATH": "/usr/bin",
            "FCODE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1",
        })

    def test_explicit_opt_in_preserves_host_provider_environment(self) -> None:
        env = {
            "FCODE_USE_HOST_PROVIDER_ENV": "1",
            "KIMI_API_KEY": "intentional-key",
            "KIMI_BASE_URL": "https://compatible.example/v1",
        }

        isolate_provider_environment(env)

        self.assertEqual(env["KIMI_API_KEY"], "intentional-key")
        self.assertEqual(env["KIMI_BASE_URL"], "https://compatible.example/v1")


if __name__ == "__main__":
    unittest.main()
