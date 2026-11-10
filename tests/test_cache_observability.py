"""Cache observability and cache-friendly prompt ordering."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.memory.context import (  # noqa: E402
    WorkingMemory, _cache_ordered_context,
)
from flowagent.providers.openai_compat import (  # noqa: E402
    CompatSettings,
    OpenAICompatProvider,
    _prompt_prefix_stats,
)
from flowagent.providers.prompt import (  # noqa: E402
    FINANCE_PROMPT_PROFILE,
    GENERAL_PROMPT_PROFILE,
    render_prompt,
)

REPLY = json.dumps({
    "version": "flowagent.plan/v1",
    "steps": [{"id": "s1", "tool": "read_file", "arguments": {"path": "x.py"}}],
    "todo_id": None,
})


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _price():
    from flowagent.providers.deepseek import PriceSnapshot

    return PriceSnapshot(
        input_microusd_per_mtok=1_000_000,
        output_microusd_per_mtok=2_000_000,
        as_of="2026-11-11",
    )


class CacheFriendlyPromptTests(unittest.TestCase):
    def test_general_context_puts_stable_fields_before_round(self) -> None:
        context = WorkingMemory(
            objective="fix pricing",
            tool_catalog=[{"name": "read_file"}],
            prompt_profile=GENERAL_PROMPT_PROFILE.key,
            instruction="general instruction",
        ).build(7, {"max_calls": 3})
        ordered = _cache_ordered_context(context)
        keys = list(ordered)
        self.assertLess(keys.index("prompt_profile"), keys.index("round"))
        self.assertLess(keys.index("instruction"), keys.index("round"))
        self.assertLess(keys.index("tools"), keys.index("round"))
        self.assertLess(keys.index("observations"), keys.index("budget"))
        self.assertLess(keys.index("budget"), keys.index("round"))

    def test_general_render_uses_cache_friendly_order_and_same_content(self) -> None:
        context = {
            "objective": "fix pricing",
            "round": 7,
            "tools": [{"name": "read_file"}],
            "plan": [],
            "state": {"tests": "failing"},
            "observations": [{"round": 1, "tool": "read_file"}],
            "budget": {"max_calls": 3},
            "instruction": "general instruction",
            "prompt_profile": GENERAL_PROMPT_PROFILE.key,
        }
        _, user = render_prompt(context)
        self.assertLess(user.index('"prompt_profile"'), user.index('"round"'))
        self.assertLess(user.index('"instruction"'), user.index('"budget"'))
        self.assertIn('"round":7', user)
        self.assertIn('"prompt_version": "fcode.prompt/general-v11"', user)

    def test_finance_prompt_keeps_frozen_sorted_rendering(self) -> None:
        context = {
            "objective": "finance",
            "round": 7,
            "tools": [],
            "plan": [],
            "state": {},
            "observations": [],
            "budget": {},
            "instruction": "read only",
        }
        _, user = render_prompt(context)
        # Canonical sorted-key rendering: budget remains before round, and the
        # version is still the frozen v10 string.
        self.assertLess(user.index('"budget"'), user.index('"round"'))
        self.assertIn('"prompt_version": "fcode.prompt/v10"', user)
        self.assertEqual(render_prompt(context)[0], FINANCE_PROMPT_PROFILE.system)


class PromptPrefixStatsTests(unittest.TestCase):
    def test_prefix_stats_measure_common_prefix_over_current_request(self) -> None:
        stats = _prompt_prefix_stats("abcdef", "abcxyz")
        self.assertEqual(stats["prompt_prefix_chars"], 3)
        self.assertEqual(stats["prompt_prefix_ratio"], 0.5)

    def test_first_request_has_no_previous_prefix(self) -> None:
        self.assertEqual(_prompt_prefix_stats(None, "abc")["prompt_prefix_chars"], 0)
        self.assertEqual(_prompt_prefix_stats("", "abc")["prompt_prefix_ratio"], 0.0)


class ProviderCacheMetadataTests(unittest.TestCase):
    def _provider(self) -> OpenAICompatProvider:
        settings = CompatSettings(
            label="compat", api_key_env="X", api_key="sk-test",
            base_url="https://endpoint.example/v1", model="m", price=_price(),
            receipt_detail="compact",
        )
        return OpenAICompatProvider.from_settings(settings)

    def _propose(self, provider, round_no, cached_tokens=0):
        payload = {
            "id": f"chatcmpl-{round_no}",
            "choices": [{"message": {"content": REPLY}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "total_tokens": 1010,
                "prompt_cache_hit_tokens": cached_tokens,
            },
        }
        seen: list[dict] = []

        def fake(request, timeout=None):  # noqa: ARG001
            seen.append(json.loads(request.data.decode("utf-8")))
            return _Response(payload)

        with mock.patch("urllib.request.urlopen", fake):
            proposal = provider.propose({
                "objective": "fix pricing",
                "round": round_no,
                "tools": [],
                "plan": [],
                "state": {},
                "observations": [],
                "budget": {},
                "instruction": "general instruction",
                "prompt_profile": GENERAL_PROMPT_PROFILE.key,
            })
        return proposal, seen

    def test_compact_receipt_still_reports_provider_cache(self) -> None:
        provider = self._provider()
        first, _ = self._propose(provider, 1, cached_tokens=0)
        self.assertEqual(first.metadata["cached_tokens"], 0)
        self.assertEqual(first.metadata["prompt_prefix_ratio"], 0.0)
        self.assertNotIn("raw_content", first.metadata, "compact receipt stays compact")

        second, seen = self._propose(provider, 2, cached_tokens=700)
        self.assertEqual(second.metadata["cached_tokens"], 700)
        self.assertEqual(second.metadata["cache_hit_rate"], 0.7)
        self.assertGreater(second.metadata["prompt_prefix_chars"], 0)
        self.assertGreater(second.metadata["prompt_prefix_ratio"], 0)
        # Stable contract fields must precede the per-round field in the user
        # message, otherwise the observed prefix similarity stays artificial.
        user = next(item["messages"][1]["content"] for item in seen)
        self.assertLess(user.index('"prompt_profile"'), user.index('"round"'))
        self.assertLess(user.index('"instruction"'), user.index('"budget"'))