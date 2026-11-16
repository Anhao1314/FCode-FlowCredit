"""Curated plugin market: catalog discovery and host-reviewed mcp.json writes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "viewer"))

from plugin_market import MarketError, PluginMarket  # noqa: E402


class PluginMarketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "mcp.json"
        self.market = PluginMarket(config_path=self.config, repo=Path(self.tmp.name) / "repo")
        self.market.workspace = self.market.repo / "workspace-general"

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_lists_builtin_and_mcp_plugins(self):
        entries = self.market.entries()
        by_id = {entry["id"]: entry for entry in entries}
        self.assertIn("code-search", by_id)
        self.assertIn("filesystem", by_id)
        self.assertTrue(by_id["code-search"]["kind"] == "builtin")
        self.assertFalse(by_id["filesystem"]["installed"])
        self.assertEqual(by_id["filesystem"]["status"], "not_installed")
        self.assertIn(str(self.market.workspace), by_id["filesystem"]["command"])

    def test_builtin_plugin_availability_comes_from_tool_facts(self):
        entries = self.market.entries(tool_names={"search_codebase", "grep_workspace"})
        by_id = {entry["id"]: entry for entry in entries}
        self.assertTrue(by_id["code-search"]["installed"])
        self.assertEqual(by_id["code-search"]["status"], "connected")
        self.assertEqual(by_id["code-search"]["missing_tools"], ["repo_map", "find_files"])

    def test_install_writes_reviewed_mcp_entry(self):
        entry = self.market.install("fetch")
        self.assertTrue(entry["installed"])
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["servers"]["fetch"]["command"], "uvx")
        self.assertEqual(data["servers"]["fetch"]["readonlyTools"], ["fetch"])
        # Refuse duplicate install and unknown ids.
        with self.assertRaises(MarketError):
            self.market.install("fetch")
        with self.assertRaises(MarketError):
            self.market.install("nope")

    def test_uninstall_removes_entry_and_builtin_is_refused(self):
        self.market.install("git")
        entry = self.market.uninstall("git")
        self.assertFalse(entry["installed"])
        self.assertEqual(self.market.configured(), {})
        with self.assertRaises(MarketError):
            self.market.uninstall("git")
        with self.assertRaises(MarketError):
            self.market.uninstall("browser")

    def test_installed_status_tracks_bridge_facts(self):
        self.market.install("fetch")
        entries = self.market.entries(mcp_servers=[{
            "server": "fetch", "connected": True, "tools": ["fetch"],
        }])
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(by_id["fetch"]["status"], "connected")
        self.assertEqual(by_id["fetch"]["tools"], ["fetch"])

        entries = self.market.entries(mcp_servers=[{
            "server": "fetch", "connected": False, "tools": [], "reason": "spawn failed",
        }])
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(by_id["fetch"]["status"], "failed")
        self.assertEqual(by_id["fetch"]["reason"], "spawn failed")

    def test_malformed_mcp_json_fails_loudly(self):
        self.config.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(MarketError):
            self.market.configured()


if __name__ == "__main__":
    unittest.main()