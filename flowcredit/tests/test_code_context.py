"""W1 coding context pack: bounded, deterministic, hash-bound and fail-closed."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.hashing import digest  # noqa: E402
from flowagent.tools.general.code_context import build_code_context  # noqa: E402

APP_PY = """def apply_discount(amount, customer_type):
    if customer_type == "student":
        return amount * 0.9
    return amount


def invoice_total(amount, customer_type):
    tax = 0.13
    return round(apply_discount(amount, customer_type) * (1 + tax), 2)
"""

TEST_PY = """import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import invoice_total

assert invoice_total(100, "student") == 103.5
assert invoice_total(100, "staff") == 113.0
print("public ok")
"""

APP_JS = """class Counter {
  constructor({ max = 3 } = {}) {
    this.value = 0;
    this.max = max;
  }

  increment() {
    this.value += 1;
    return this.value;
  }
}

module.exports = { Counter };
"""

TEST_JS = """const { Counter } = require('../src/counter');
const counter = new Counter({ max: 1 });
assert(counter.increment() === 1);
console.log('public ok');
"""


class CodeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "app.py").write_text(APP_PY, encoding="utf-8")
        (self.root / "tests" / "test_app.py").write_text(TEST_PY, encoding="utf-8")
        (self.root / "src" / "counter.js").write_text(APP_JS, encoding="utf-8")
        (self.root / "tests" / "counter.test.js").write_text(TEST_JS, encoding="utf-8")

    def test_python_context_has_candidates_tests_symbols_and_hash(self) -> None:
        context = build_code_context(
            self.root,
            "修复 student invoice_total 的折扣计算",
            verification={"command": "python3", "args": ["tests/test_app.py"]},
        )
        self.assertIsNotNone(context)
        paths = [item["path"] for item in context["candidate_files"]]
        self.assertIn("src/app.py", paths)
        test_paths = [item["path"] for item in context["related_tests"]]
        self.assertIn("tests/test_app.py", test_paths)
        self.assertTrue(any(
            item["path"] == "src/app.py" and any(sym.startswith("apply_discount:") for sym in item["symbols"])
            for item in context["symbol_edges"]
        ))
        self.assertEqual(context["verification"]["argv"],
                         ["python3", "tests/test_app.py"])
        self.assertEqual(context["chars"], len(context["block"]))
        self.assertEqual(context["block_sha256"], digest(context["block"]))
        self.assertIn("验证命令", context["block"])
        for item in context["candidate_files"] + context["related_tests"]:
            self.assertIsInstance(item["sha256"], str)
            self.assertEqual(len(item["sha256"]), 64)

    def test_javascript_context_extracts_symbols_and_matching_test(self) -> None:
        context = build_code_context(self.root, "fix the Counter increment max behavior")
        self.assertIsNotNone(context)
        paths = [item["path"] for item in context["candidate_files"]]
        self.assertIn("src/counter.js", paths)
        self.assertTrue(any(
            item["path"] == "src/counter.js"
            and any(symbol.startswith("Counter:") for symbol in item["symbols"])
            for item in context["symbol_edges"]
        ))
        self.assertIn("tests/counter.test.js",
                      [item["path"] for item in context["related_tests"]])

    def test_context_is_deterministic_and_chain_safe(self) -> None:
        query = "修复 student invoice_total 的折扣计算"
        first = build_code_context(self.root, query,
                                   verification={"command": "python3", "args": ["tests/test_app.py"]})
        second = build_code_context(self.root, query,
                                    verification={"command": "python3", "args": ["tests/test_app.py"]})
        self.assertEqual(first["block_sha256"], second["block_sha256"])
        # The pack itself enters the hash chain; no floats may leak through.
        self.assertIsInstance(digest({
            "candidates": first["candidate_files"],
            "tests": first["related_tests"],
            "edges": first["symbol_edges"],
        }), str)

    def test_block_is_bounded_and_truncation_is_honest(self) -> None:
        context = build_code_context(
            self.root, "修复 student invoice_total 的折扣计算", max_block_chars=300,
        )
        self.assertIsNotNone(context)
        self.assertLessEqual(context["chars"], 300)
        self.assertTrue(context["truncated"])
        self.assertTrue(context["block"].endswith("…"))

    def test_no_terms_or_no_hits_fail_closed(self) -> None:
        self.assertIsNone(build_code_context(self.root, "   "))
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertIsNone(build_code_context(empty, "anything useful"))

    def test_sensitive_files_are_never_candidates(self) -> None:
        secret = self.root / "credentials.py"
        secret.write_text("def apply_discount(amount):\n    return amount\n", encoding="utf-8")
        context = build_code_context(self.root, "apply_discount customer invoice")
        self.assertIsNotNone(context)
        all_paths = [
            item["path"] for item in context["candidate_files"] + context["related_tests"]
        ]
        self.assertNotIn("credentials.py", all_paths)


if __name__ == "__main__":
    unittest.main()
