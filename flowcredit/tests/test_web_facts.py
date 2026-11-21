"""P2 web 规则 v11：web 面事实提取（确定性投影）与规则加载。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowaudit.rea.web import scan_web_surface  # noqa: E402
from flowaudit.vulns import engine  # noqa: E402

PAGE = """<!doctype html><html><head>
<script src="https://cdn.example.com/analytics.js"></script>
<script src="https://cdn.example.com/lib.js" integrity="sha384-abc" crossorigin></script>
<script src="/_next/app.js"></script>
</head><body onload="init()"><button onclick="go()">x</button></body></html>"""
JS = "function a(){return eval('1+1')}\n//# sourceMappingURL=app.js.map\n"


class WebFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "snap"
        (self.root / "_next").mkdir(parents=True)
        (self.root / "index.html").write_text(PAGE)
        (self.root / "_next" / "app.js").write_text(JS)
        (self.root / "runtime-evidence.json").write_text(json.dumps({
            "status": "ok",
            "requests": [
                {"method": "GET", "origin": "https://site.example.com", "resource": "document"},
                {"method": "GET", "origin": "https://cdn.example.com", "resource": "script"},
                {"method": "POST", "origin": "https://api.tracker.example", "resource": "xhr"},
            ],
            "console_errors": ["boom"],
        }))
        (self.root / "headers.json").write_text(json.dumps({
            "source_url": "https://site.example.com/zh",
            "landing_headers": {"strict-transport-security": "max-age=1"},
            "assets": [],
        }))

    def test_facts_cover_web_surface(self) -> None:
        facts = scan_web_surface(self.root)
        self.assertEqual(facts["pages"], 1)
        origins = [row["origin"] for row in facts["third_party_scripts"]]
        self.assertEqual(origins, ["cdn.example.com", "cdn.example.com"])
        # 有 SRI 的那条不进 missing_sri
        self.assertEqual(len(facts["missing_sri"]), 1)
        self.assertEqual(facts["missing_sri"][0]["src"], "https://cdn.example.com/analytics.js")
        handlers = [row["snippet"] for row in facts["inline_event_handlers"]]
        self.assertTrue(any("onload" in h for h in handlers))
        self.assertTrue(any("onclick" in h for h in handlers))
        self.assertEqual(len(facts["eval_usage"]), 1)
        self.assertEqual(facts["eval_usage"][0]["file"], "_next/app.js")
        self.assertEqual(len(facts["sourcemap_refs"]), 1)
        self.assertEqual(facts["sourcemap_refs"][0]["target"], "app.js.map")
        runtime_origins = {row["origin"] for row in facts["runtime_third_party_origins"]}
        self.assertEqual(runtime_origins, {"https://cdn.example.com", "https://api.tracker.example"})
        self.assertEqual(len(facts["runtime_console_errors"]), 1)
        missing = {row["name"] for row in facts["missing_security_headers"]}
        self.assertIn("content-security-policy", missing)
        self.assertNotIn("strict-transport-security", missing)
        self.assertEqual([row["name"] for row in facts["present_security_headers"]],
                         ["strict-transport-security"])

    def test_ruleset_v11_loads_web_rules_active(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        self.assertEqual(ruleset["version"], 11)
        web_rules = [r for r in ruleset["rules"].values() if r.id.startswith("RULE-WEB")]
        self.assertEqual(len(web_rules), 7)
        self.assertTrue(all(r.active for r in web_rules))
        # 触发器都指向 web_scan 选择器
        for rule in web_rules:
            self.assertTrue(rule.trigger["selector"].startswith("web_scan."), rule.id)

    def test_rea_scan_matches_web_facts_and_control_stays_clean(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        facts = scan_web_surface(self.root)
        probe_doc = {"host": {"web_scan": facts}}
        candidates = engine.rea_scan(probe_doc, ruleset)
        web_hits = {c.rule.id for c in candidates if c.rule.id.startswith("RULE-WEB")}
        self.assertEqual(web_hits, {
            "RULE-WEB01-001", "RULE-WEB02-001", "RULE-WEB03-001",
            "RULE-WEB04-001", "RULE-WEB05-001", "RULE-WEB06-001",
            "RULE-WEB07-001",
        })
        # 控制投影：web 行清零后不得命中
        control = json.loads(json.dumps(facts))
        for key in ("third_party_scripts", "missing_sri", "inline_event_handlers",
                    "eval_usage", "sourcemap_refs", "missing_security_headers"):
            control[key] = []
        for key in ("runtime_third_party_origins", "runtime_console_errors"):
            control[key] = []
        control_hits = [c for c in engine.rea_scan({"host": {"web_scan": control}}, ruleset)
                        if c.rule.id.startswith("RULE-WEB")]
        self.assertEqual(control_hits, [])


if __name__ == "__main__":
    unittest.main()
