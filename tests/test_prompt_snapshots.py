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
        "fcode.prompt/general-v10", 3466,
        "7fc5d4d070c4b387af501ca90bc721c6226588dd9f2b74416d5f5e0d25861b11",
    ),
    "general-fast": (
        "fcode.prompt/general-fast-v2", 3905,
        "db591d38c6725814a9c067f9c59745478cab71ca53a860283748b256e46efc55",
    ),
    "general-fast-native": (
        "fcode.prompt/general-fast-native-v2", 3846,
        "a93ba5b28f43456c89af83ae22b7bd9ef1aa1e97d3f51667d150ce58db598def",
    ),
}

CHAT_SHA256 = "bac45dbfd2af50025aa7e71ea2a461fc11b084f0d3e0141307205eae19a362f8"
CHAT_BYTES = 1223


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