"""Prompt assets are versioned, byte-snapshotted and indexed.

A prompt change is a behaviour change: it must bump the profile version, update
this file's golden constants in the same commit, and be reflected in
``docs/prompt-catalog.md``. This is the non-finance counterpart of the finance
SFT golden hash in ``test_domain_prompt.py``.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import CHAT_SYSTEM_PROMPT  # noqa: E402
from flowagent.providers.prompt import PROMPT_PROFILES  # noqa: E402

#: key -> (version, utf-8 byte length, sha256)
EXPECTED = {
    "finance": (
        "fcode.prompt/v10", 726,
        "ec0ba6d412b34fbb60682a0bd07bb8d4d233e3b0b728863a1f1cdc5b69693408",
    ),
    "general": (
        "fcode.prompt/general-v9", 3279,
        "6fd75597ab479f22998f22a6a904227572973d2bed2518b0701f271bac516545",
    ),
    "general-fast": (
        "fcode.prompt/general-fast-v1", 3757,
        "365e5dd67cfb6018e18ecf70fbe15688cb415c05c3e9a7949a038efddc71ea0f",
    ),
    "general-fast-native": (
        "fcode.prompt/general-fast-native-v1", 3698,
        "73618bc762a4eb4590d49736022d03ee4c31d0575130ea7cd591c9fbd41551f2",
    ),
}

CHAT_SHA256 = "8a6aab9860f33e35a63c80dc55ca804d74a99d27a0e171c297a8f41aae1f709b"
CHAT_BYTES = 1145


class PromptSnapshotTests(unittest.TestCase):
    def test_profile_versions_and_bytes_are_pinned(self) -> None:
        self.assertEqual(set(PROMPT_PROFILES), set(EXPECTED))
        for key, (version, size, digest) in EXPECTED.items():
            profile = PROMPT_PROFILES[key]
            data = profile.system.encode("utf-8")
            self.assertEqual(profile.version, version, key)
            self.assertEqual(len(data), size, key)
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, key)

    def test_chat_prompt_bytes_are_pinned(self) -> None:
        data = CHAT_SYSTEM_PROMPT.encode("utf-8")
        self.assertEqual(len(data), CHAT_BYTES)
        self.assertEqual(hashlib.sha256(data).hexdigest(), CHAT_SHA256)

    def test_catalog_lists_every_profile(self) -> None:
        catalog = (ROOT / "docs" / "prompt-catalog.md").read_text(encoding="utf-8")
        for key, (version, _, _) in EXPECTED.items():
            self.assertIn(f"`{key}`", catalog)
            self.assertIn(version, catalog)

    def test_general_contract_no_longer_calls_additions_fatal(self) -> None:
        system = PROMPT_PROFILES["general"].system
        self.assertNotIn("不要使用 additions", system)
        self.assertIn("部分接受", system)
        self.assertIn("additions", system)

    def test_chat_contract_matches_approval_tools(self) -> None:
        self.assertNotIn("不能写文件", CHAT_SYSTEM_PROMPT)
        self.assertIn("审批", CHAT_SYSTEM_PROMPT)
        self.assertIn("写文件", CHAT_SYSTEM_PROMPT)
        self.assertIn("执行提案", CHAT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()