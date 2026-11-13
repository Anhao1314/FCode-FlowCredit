"""One settings page configures both surfaces.

The complaint this closes: the settings card held the Kimi key, the model menu
in Work still said "KIMI_API_KEY is required", because delegation providers
were declared only by the process environment. These tests pin the merge
(environment → user settings → defaults), the guards that survive it, and the two
catalogs agreeing about the same provider.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.providers.catalog import (  # noqa: E402
    compat_settings_for,
    merged_env,
    provider_catalog,
    settings_as_env,
)

VIEWER = ROOT / "viewer"
if str(VIEWER) not in sys.path:
    sys.path.insert(0, str(VIEWER))

KIMI_SETTINGS = {
    "api_key": "sk-settings-1234567890",
    "model": "kimi-k2.6",
    "input_price_microusd_per_mtok": "950000",
    "output_price_microusd_per_mtok": "4000000",
    "price_as_of": "2026-10-25",
    "price_source": "moonshot-cn-published",
    "min_interval_seconds": "22",
}

ENV_CLEARED = {
    name: "" for name in (
        "KIMI_API_KEY", "KIMI_MODEL", "KIMI_BASE_URL",
        "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK", "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK",
        "KIMI_PRICE_AS_OF", "KIMI_PRICE_SOURCE", "KIMI_MIN_CALL_INTERVAL_SECONDS",
    )
}


def find(entries, key):
    return next(entry for entry in entries if entry["key"] == key)


def model_catalog_entries(manager):
    """The models a manager would offer, settings included (no runtime needed)."""
    from console_common import model_catalog

    return model_catalog(manager.local_endpoint, settings_env=manager.settings_env())


class SettingsOverlayTests(unittest.TestCase):
    """``settings_as_env`` is the only translation between the two vocabularies."""

    def test_settings_fields_become_preset_env_names(self) -> None:
        overlay = settings_as_env({"kimi": KIMI_SETTINGS})
        self.assertEqual(overlay["KIMI_API_KEY"], "sk-settings-1234567890")
        self.assertEqual(overlay["KIMI_PRICE_AS_OF"], "2026-10-25")
        self.assertEqual(overlay["KIMI_MIN_CALL_INTERVAL_SECONDS"], "22")

    def test_a_base_url_can_never_come_from_settings(self) -> None:
        """Cloud endpoints stay host-configured: a settings write that could
        redirect the stored key is an exfiltration path."""
        overlay = settings_as_env({
            "kimi": dict(KIMI_SETTINGS, base_url="https://evil.example/v1"),
        })
        self.assertNotIn("KIMI_BASE_URL", overlay)
        entry = find(provider_catalog(settings_env=overlay, env={}), "kimi")
        self.assertTrue(entry["available"])
        self.assertNotIn("evil.example", json.dumps(entry))

    def test_blank_settings_never_erase_the_environment(self) -> None:
        merged = merged_env({"KIMI_API_KEY": "   "}, env={"KIMI_API_KEY": "env-key"})
        self.assertEqual(merged["KIMI_API_KEY"], "env-key")

    def test_environment_beats_settings(self) -> None:
        merged = merged_env({"KIMI_API_KEY": "settings-key"}, env={"KIMI_API_KEY": "env-key"})
        self.assertEqual(merged["KIMI_API_KEY"], "env-key")

    def test_custom_provider_settings_join_the_shared_catalog(self) -> None:
        overlay = settings_as_env({"ollama": {
            "label": "本机 Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3-8b",
            "input_price_microusd_per_mtok": "0",
            "output_price_microusd_per_mtok": "0",
            "price_as_of": "2026-11-14",
            "price_source": "local-not-billed",
        }})
        self.assertIn("OLLAMA", overlay["FCODE_OPENAI_COMPAT_PROVIDERS"].split(","))
        self.assertEqual(overlay["OLLAMA_BASE_URL"], "http://127.0.0.1:11434/v1")
        entry = find(provider_catalog(settings_env=overlay, env={}), "ollama")
        self.assertTrue(entry["available"], entry["reason"])
        self.assertEqual(entry["label"], "本机 Ollama")
        self.assertEqual(entry["price"]["inputMicrousdPerMtok"], 0)

    def test_invalid_custom_provider_keys_are_ignored(self) -> None:
        self.assertEqual(settings_as_env({"bad-key": {"api_key": "x"}}), {})
        self.assertEqual(settings_as_env(None), {})


class DelegationCatalogTests(unittest.TestCase):
    """The model menu a delegation uses must see the settings page."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, ENV_CLEARED, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_kimi_is_available_from_settings_alone(self) -> None:
        overlay = settings_as_env({"kimi": KIMI_SETTINGS})
        entry = find(provider_catalog(settings_env=overlay, env={}), "kimi")
        self.assertTrue(entry["available"], entry["reason"])
        self.assertEqual(entry["model"], "kimi-k2.6")
        self.assertEqual(entry["price"]["inputMicrousdPerMtok"], 950000)

    def test_prices_without_a_date_stay_unavailable_and_say_where(self) -> None:
        overlay = settings_as_env({"kimi": {
            key: value for key, value in KIMI_SETTINGS.items() if key != "price_as_of"
        }})
        entry = find(provider_catalog(settings_env=overlay, env={}), "kimi")
        self.assertFalse(entry["available"])
        self.assertIn("设置页配置不完整", entry["reason"])
        self.assertIn("KIMI_PRICE_AS_OF", entry["reason"])

    def test_an_environment_only_host_keeps_its_own_reason(self) -> None:
        entry = find(provider_catalog(env={}), "kimi")
        self.assertFalse(entry["available"])
        self.assertIn("Host 配置不完整", entry["reason"])

    def test_local_settings_can_construct_the_governance_completion_provider(self) -> None:
        settings = compat_settings_for("local", env={
            "FCODE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1",
            "FCODE_LOCAL_MODEL": "qwen3-local",
        })
        self.assertIsNotNone(settings)
        self.assertEqual(settings.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(settings.model, "qwen3-local")
        self.assertEqual(settings.price.input_microusd_per_mtok, 0)


class DisabledProviderTests(unittest.TestCase):
    """A host can remove a preset without deleting code or its tests."""

    def test_disabled_provider_is_absent_and_unresolvable(self) -> None:
        overlay = settings_as_env({"kimi": KIMI_SETTINGS})
        env = {"FCODE_DISABLED_PROVIDERS": "kimi"}
        keys = [
            entry["key"]
            for entry in provider_catalog(settings_env=overlay, env=env)
        ]
        self.assertNotIn("kimi", keys)
        self.assertIn("deepseek", keys)
        self.assertIsNone(compat_settings_for("kimi", env={**overlay, **env}))

    def test_disable_list_is_case_insensitive_and_comma_separated(self) -> None:
        env = {"FCODE_DISABLED_PROVIDERS": " Kimi , deepseek "}
        keys = [entry["key"] for entry in provider_catalog(env=env)]
        self.assertNotIn("kimi", keys)
        self.assertNotIn("deepseek", keys)


class BothSurfacesAgreeTests(unittest.TestCase):
    """Acceptance for F1c: one provider, one verdict, on both menus."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, ENV_CLEARED, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        from flowagent.chat import ChatService

        service = ChatService(self.root / "chat.sqlite", self.root / "chat-config.json")
        self.addCleanup(service.close)
        return service

    def save(self, service, values):
        return service.update_config(provider="kimi", values={**KIMI_SETTINGS, **values})

    def test_a_complete_settings_page_enables_both_menus(self) -> None:
        service = self.service()
        snapshot = self.save(service, {})
        chat = find(snapshot["models"], "kimi")
        delegation = find(
            provider_catalog(settings_env=settings_as_env(service._config["providers"]), env={}),
            "kimi",
        )
        self.assertTrue(chat["available"], chat["reason"])
        self.assertTrue(delegation["available"], delegation["reason"])

    def test_a_missing_date_disables_both_menus_with_the_same_cause(self) -> None:
        service = self.service()
        snapshot = service.update_config(provider="kimi", values={
            **KIMI_SETTINGS, "price_as_of": "",
        })
        chat = find(snapshot["models"], "kimi")
        delegation = find(
            provider_catalog(settings_env=settings_as_env(service._config["providers"]), env={}),
            "kimi",
        )
        self.assertFalse(chat["available"])
        self.assertFalse(delegation["available"])
        for reason in (chat["reason"], delegation["reason"]):
            self.assertIn("KIMI_PRICE_AS_OF", reason)

    def test_a_custom_endpoint_is_shared_by_chat_and_delegation(self) -> None:
        service = self.service()
        snapshot = service.update_config(provider="ollama", values={
            "label": "本机 Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3-8b",
            "api_key": "custom-secret",
            "input_price_microusd_per_mtok": "0",
            "output_price_microusd_per_mtok": "0",
            "price_as_of": "2026-11-14",
            "price_source": "local-not-billed",
            "supports_effort": "1",
        })
        chat = find(snapshot["models"], "ollama")
        delegation = find(provider_catalog(
            settings_env=settings_as_env(service._config["providers"]), env={},
        ), "ollama")
        self.assertTrue(chat["available"], chat["reason"])
        self.assertTrue(delegation["available"], delegation["reason"])
        self.assertEqual(chat["model"], delegation["model"])
        self.assertNotIn("custom-secret", json.dumps(snapshot["config"]))
        self.assertTrue(snapshot["default"] == "mock")

    def test_deleting_a_custom_provider_needs_no_values_and_falls_back_the_default(self) -> None:
        """The settings page deletes with {provider, delete_provider} only.

        Requiring ``values`` on the delete path made that call impossible:
        the page has nothing to save, and the operation never reads fields.
        Deleting the current default must also fall back to ``mock`` — an
        operator removing an endpoint cannot leave a dangling default.
        """
        service = self.service()
        service.update_config(provider="ollama", values={
            "label": "本机 Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3-8b",
            "input_price_microusd_per_mtok": "0",
            "output_price_microusd_per_mtok": "0",
            "price_as_of": "2026-11-14",
            "price_source": "local-not-billed",
        })
        snapshot = service.update_config(default_model="ollama")
        self.assertEqual(snapshot["default"], "ollama")

        deleted = service.update_config(provider="ollama", delete_provider=True)
        self.assertNotIn("ollama", deleted["config"]["providers"])
        self.assertEqual(deleted["config"]["default_model"], "mock")
        self.assertEqual(deleted["default"], "mock")
        self.assertNotIn("ollama", [entry["key"] for entry in deleted["models"]])

    def test_the_snapshot_reports_where_the_configuration_came_from(self) -> None:
        service = self.service()
        self.save(service, {})
        source = service.config_source()
        self.assertEqual(source["path"], str(self.root / "chat-config.json"))
        self.assertGreater(source["fileMtime"], 0)
        self.assertFalse(source["stale"])

    def test_a_newer_file_on_disk_is_reloaded_by_chat(self) -> None:
        """Every surface observes the same saved default without a restart."""
        from flowagent.chat_config import CHAT_CONFIG_VERSION, save_chat_config

        service = self.service()
        config_path = self.root / "chat-config.json"
        service.update_config(default_model="mock")
        save_chat_config(config_path, {
            "version": CHAT_CONFIG_VERSION, "providers": {}, "default_model": "kimi",
        })
        os.utime(config_path, (service.config_source()["fileMtime"] + 30,) * 2)
        self.assertEqual(service.default_model(), "kimi")
        self.assertFalse(service.config_source()["stale"])


class DelegationConstructionTests(unittest.TestCase):
    """Construction reads the same merged view — no second resolution rule."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.store = TaskStore(self.root / "task.sqlite")
        self.addCleanup(self.store.close)
        patcher = mock.patch.dict(os.environ, ENV_CLEARED, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **kwargs):
        from flowagent.demo.provider_factory import build_provider

        return build_provider(
            "kimi",
            scenario=get_scenario("full"),
            receipts=ReceiptStore(self.store),
            **kwargs,
        )

    def test_a_settings_configured_provider_constructs(self) -> None:
        overlay = settings_as_env({"kimi": KIMI_SETTINGS})
        provider = self.build(env=merged_env(overlay, env={}))
        self.assertEqual(provider.name, "kimi")
        self.assertEqual(provider.model, "kimi-k2.6")
        self.assertEqual(provider.price.input_microusd_per_mtok, 950000)
        self.assertEqual(provider.price.output_microusd_per_mtok, 4000000)
        self.assertEqual(provider.min_call_interval_seconds, 22.0)
        # The cloud endpoint still comes from the preset, never from settings.
        self.assertEqual(provider.base_url, "https://api.moonshot.cn/v1")

    def test_construction_still_fails_closed_without_a_date(self) -> None:
        overlay = settings_as_env({"kimi": {
            key: value for key, value in KIMI_SETTINGS.items() if key != "price_as_of"
        }})
        with self.assertRaises(ValueError):
            self.build(env=merged_env(overlay, env={}))

    def test_deepseek_gets_reasoning_headroom_and_env_override(self) -> None:
        """回归：硬编码 256 会被 reasoning 吃光，content 为空整轮判死。"""
        from flowagent.demo.provider_factory import build_provider

        base = {
            "DEEPSEEK_API_KEY": "sk-test-deepseek",
            "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK": "300000",
            "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK": "1200000",
            "DEEPSEEK_PRICE_AS_OF": "2026-11-09",
        }
        provider = build_provider(
            "deepseek", scenario=get_scenario("full"),
            receipts=ReceiptStore(self.store), env=base,
        )
        self.assertEqual(provider.max_completion_tokens, 8192)
        tuned = build_provider(
            "deepseek", scenario=get_scenario("full"),
            receipts=ReceiptStore(self.store),
            env={**base, "DEEPSEEK_MAX_TOKENS": "12000"},
        )
        self.assertEqual(tuned.max_completion_tokens, 12000)


class ConsoleWiringTests(unittest.TestCase):
    """The console's own resolution path uses the settings overlay."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        cls.viewer = VIEWER
        spec = importlib.util.spec_from_file_location(
            "fcode_console_manager_settings", VIEWER / "console_manager.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.manager_module = module

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = pathlib.Path(self._tmp.name) / "runs"
        self.runs.mkdir()

    def test_resolve_model_accepts_a_settings_configured_provider(self) -> None:
        manager = self.manager_module.RunManager(None, "", 0.0, runs_dir=self.runs)
        manager._settings_env = settings_as_env({"kimi": KIMI_SETTINGS})
        key, effort = manager.resolve_model("kimi", "medium")
        self.assertEqual((key, effort), ("kimi", "medium"))

    def test_default_model_is_shared_and_environment_remains_authoritative(self) -> None:
        manager = self.manager_module.RunManager(None, "", 0.0, runs_dir=self.runs)
        manager.chat = type("Chat", (), {"default_model": lambda self: "kimi"})()
        self.assertEqual(manager.default_model(), "kimi")
        with mock.patch.dict(os.environ, {"FCODE_DEFAULT_RUN_MODEL": "deepseek"}):
            self.assertEqual(manager.default_model(), "deepseek")

    def test_resolve_model_refuses_an_incomplete_provider_with_its_reason(self) -> None:
        manager = self.manager_module.RunManager(None, "", 0.0, runs_dir=self.runs)
        manager._settings_env = settings_as_env({"kimi": {
            key: value for key, value in KIMI_SETTINGS.items() if key != "price_as_of"
        }})
        with self.assertRaises(ValueError) as caught:
            manager.resolve_model("kimi", None)
        self.assertIn("KIMI_PRICE_AS_OF", str(caught.exception))

    def test_the_budget_capacity_follows_the_settings_prices(self) -> None:
        """Real prices reduce capacity without raising the operator's ceiling."""
        from flowagent.runtime.budget import BudgetLimits

        manager = self.manager_module.RunManager(None, "", 0.0, runs_dir=self.runs)
        manager._settings_env = settings_as_env({"kimi": KIMI_SETTINGS})
        entry = find(model_catalog_entries(manager), "kimi")
        self.assertTrue(entry["available"], entry["reason"])
        limits = BudgetLimits(12, 8, 100_000, 100_000)
        budget = manager._budget_for_model(entry, limits)
        self.assertEqual(budget["max_cost_microusd"], limits.max_cost_microusd)
        self.assertGreater(budget["max_calls"], 0)
        self.assertLess(budget["max_calls"], limits.max_calls)


if __name__ == "__main__":
    unittest.main()