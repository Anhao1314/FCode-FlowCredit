"""S2-11: the blind set must stay blind, and CI enforces it.

Blind fixtures FCODE-04/07 exist to measure generalization: the audit
engine must find defects it was never shown. That measurement dies the
moment their details leak into rules, prompts, examples or product code.
This test walks every rule/prompt/product surface and refuses the
identifying strings. The blind set itself (fixtures/) and the process
record (design-reviews/) are the two allowed homes.
"""

from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "audit-rules"
PRODUCT_SRC = ROOT / "src" / "flowaudit"

#: Strings that identify the blind defects. If any of these appear in a
#: rule, a scenario, a prompt or product code, the blind measurement is
#: contaminated and this test fails.
FORBIDDEN = (
    "FCODE-04",
    "FCODE-07",
    "receipt_key",
)

#: Where the forbidden strings are ALLOWED to live (with reasons).
ALLOWED_HOMES = (
    ROOT / "fixtures",           # the ground truth itself
    ROOT / "docs" / "design-reviews",  # the process record quotes the review
)


class BlindSetIsolationTests(unittest.TestCase):
    def test_rules_and_examples_never_mention_the_blind_set(self) -> None:
        self._assert_clean(RULES_DIR)

    def test_product_source_never_hardcodes_blind_details(self) -> None:
        self._assert_clean(PRODUCT_SRC)

    def _assert_clean(self, base: pathlib.Path) -> None:
        offenders: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in (".json", ".py", ".md", ".txt"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)}: contains {needle!r}")
        self.assertEqual(offenders, [], "blind-set details leaked into rule/prompt surfaces")


if __name__ == "__main__":
    unittest.main()
