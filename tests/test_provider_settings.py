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

    def test_unknown_providers_are_ignored(self) -> None:
        self.assertEqual(settings_as_env({"ollama": {"api_key": "x"}}), {})
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

    def test_the_snapshot_reports_where_the_configuration_came_from(self) -> None:
        service = self.service()
        self.save(service, {})
        source = service.config_source()
        self.assertEqual(source["path"], str(self.root / "chat-config.json"))
        self.assertGreater(source["fileMtime"], 0)
        self.assertFalse(source["stale"])

    def test_a_newer_file_on_disk_is_reported_as_stale(self) -> None:
        """The running process holding an older copy is exactly the confusion
        that made a configured Kimi look unconfigured."""
        service = self.service()
        config_path = self.root / "chat-config.json"
        service.update_config(provider="kimi", values=KIMI_SETTINGS)
        # Another process writes the file after this one loaded it.
        os.utime(config_path, (service.config_source()["fileMtime"] + 30,) * 2)
        self.assertTrue(service.config_source()["stale"])


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