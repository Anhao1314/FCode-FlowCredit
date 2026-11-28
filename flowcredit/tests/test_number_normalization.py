"""Float boundaries: receipt metadata and model-authored numbers.

The runtime hashes canonical JSON and canonical JSON rejects floats. Two
boundaries produced floats before this fix:

1. provider receipt metadata (cache ratios) — committed with the task body;
2. model-authored plan arguments (``30.0``) — committed with ``plan_validated``.

Metadata now travels in basis points; model numbers are normalized at the
proposal boundary. Both are covered here, including the exact chain-safety
assertion that would have caught the original crash.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION, AgentError, validate_plan  # noqa: E402
from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.core.numbers import (  # noqa: E402
    NumberNormalizationError,
    normalize_json_numbers,
)
from flowagent.providers.openai_compat import (  # noqa: E402
    CompatSettings,
    OpenAICompatProvider,
)


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


def _provider(reasoning_effort: str | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider.from_settings(CompatSettings(
        label="compat", api_key_env="X", api_key="sk-test",
        base_url="https://endpoint.example/v1", model="m", price=_price(),
        receipt_detail="compact", reasoning_effort=reasoning_effort,
    ))


def _chat_payload(content: str) -> dict:
    return {
        "id": "chatcmpl-number",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_cache_hit_tokens": 25,
        },
    }


class NormalizeNumbersTests(unittest.TestCase):
    def test_integral_float_becomes_int(self) -> None:
        value, count = normalize_json_numbers({"timeout": 30.0})
        self.assertEqual(value, {"timeout": 30})
        self.assertIsInstance(value["timeout"], int)
        self.assertEqual(count, 1)

    def test_nested_lists_and_dicts_are_walked(self) -> None:
        value, count = normalize_json_numbers({
            "items": [1.0, {"ratio": 2.0}, "keep"],
        })
        self.assertEqual(value, {"items": [1, {"ratio": 2}, "keep"]})
        self.assertEqual(count, 2)

    def test_bool_is_not_an_int(self) -> None:
        value, count = normalize_json_numbers({"flag": True})
        self.assertIs(value["flag"], True)
        self.assertEqual(count, 0)

    def test_fractional_float_is_rejected_with_a_path(self) -> None:
        with self.assertRaises(NumberNormalizationError) as caught:
            normalize_json_numbers({"steps": [{"arguments": {"ratio": 0.5}}]})
        self.assertIn("$.steps[0].arguments.ratio", str(caught.exception))


class ProposalNumberBoundaryTests(unittest.TestCase):
    def _propose(self, provider, content: str):
        seen: list[dict] = []

        def fake(request, timeout=None):  # noqa: ARG001
            seen.append(json.loads(request.data.decode("utf-8")))
            return _Response(_chat_payload(content))

        with mock.patch("urllib.request.urlopen", fake):
            return provider.propose({
                "objective": "fix pricing",
                "round": 1,
                "tools": [],
                "plan": [],
                "state": {},
                "observations": [],
                "budget": {},
                "instruction": "general instruction",
                "prompt_profile": "general",
            })

    def test_text_proposal_normalizes_integral_float(self) -> None:
        provider = _provider()
        proposal = self._propose(provider, json.dumps({
            "version": PLAN_VERSION,
            "steps": [{
                "id": "s1", "tool": "read_file",
                "arguments": {"path": "x.py", "timeout": 30.0},
            }],
            "todo_id": None,
        }))
        self.assertEqual(proposal.plan["steps"][0]["arguments"]["timeout"], 30)
        self.assertIsInstance(
            proposal.plan["steps"][0]["arguments"]["timeout"], int)
        self.assertEqual(proposal.metadata["normalized_numbers"], 1)

        # The exact crash path: receipt metadata is committed with the body,
        # and the validated plan is hashed into plan_validated.
        canonical_json(proposal.receipt())
        plan = validate_plan(proposal.plan)
        canonical_json(plan.to_dict())

    def test_text_proposal_rejects_fractional_float(self) -> None:
        provider = _provider()
        with self.assertRaises(AgentError) as caught:
            self._propose(provider, json.dumps({
                "version": PLAN_VERSION,
                "steps": [{
                    "id": "s1", "tool": "read_file",
                    "arguments": {"path": "x.py", "ratio": 0.5},
                }],
                "todo_id": None,
            }))
        self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")
        self.assertIn("non-integral number", caught.exception.message)

    def test_native_tool_call_arguments_are_normalized(self) -> None:
        provider = _provider()
        steps, additions, dropped, normalized = provider._steps_from_tool_calls(
            [{
                "id": "call-1",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "x.py", "timeout": 30.0}',
                },
            }],
            max_steps=4,
            request_id="req-1",
        )
        self.assertEqual(steps[0]["arguments"]["timeout"], 30)
        self.assertIsInstance(steps[0]["arguments"]["timeout"], int)
        self.assertEqual((len(steps), additions, dropped, normalized), (1, [], 0, 1))

    def test_native_explore_only_reply_needs_no_submit_plan(self) -> None:
        """C1: a reply of only read calls maps to steps with no additions and
        no error — exploration before commitment is legal at the provider
        layer, so the round-one plan tax lives nowhere in the runtime."""
        provider = _provider()
        steps, additions, dropped, normalized = provider._steps_from_tool_calls(
            [
                {"id": "call-1",
                 "function": {"name": "ls_workspace", "arguments": "{}"}},
                {"id": "call-2", "function": {
                    "name": "read_file",
                    "arguments": '{"path": "stats.py"}'}},
            ],
            max_steps=4,
            request_id="req-1",
        )
        self.assertEqual(
            [item["tool"] for item in steps], ["ls_workspace", "read_file"])
        self.assertEqual((additions, dropped, normalized), ([], 0, 0))

    def test_native_submit_plan_arguments_are_normalized(self) -> None:
        provider = _provider()
        steps, additions, dropped, normalized = provider._steps_from_tool_calls(
            [{
                "id": "call-1",
                "function": {
                    "name": "submit_plan",
                    "arguments": json.dumps({"todos": [{
                        "id": "m1", "text": "read", "tool": "read_file",
                        "arguments": {"timeout": 5.0},
                    }]}),
                },
            }],
            max_steps=4,
            request_id="req-1",
        )
        self.assertEqual(steps, [])
        self.assertEqual(additions[0]["arguments"]["timeout"], 5)
        self.assertEqual(dropped, 0)
        self.assertEqual(normalized, 1)

    def test_native_fractional_number_is_a_retryable_proposal_error(self) -> None:
        provider = _provider()
        with self.assertRaises(AgentError) as caught:
            provider._steps_from_tool_calls(
                [{
                    "id": "call-1",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "x.py", "ratio": 0.5}',
                    },
                }],
                max_steps=4,
                request_id="req-1",
            )
        self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")


class ReceiptMetadataTests(unittest.TestCase):
    def test_proposal_metadata_has_no_float(self) -> None:
        provider = _provider()
        seen: list[dict] = []

        def fake(request, timeout=None):  # noqa: ARG001
            seen.append(json.loads(request.data.decode("utf-8")))
            return _Response(_chat_payload(json.dumps({
                "version": PLAN_VERSION,
                "steps": [{"id": "s1", "tool": "read_file", "arguments": {}}],
                "todo_id": None,
            })))

        with mock.patch("urllib.request.urlopen", fake):
            first = provider.propose({
                "objective": "t", "round": 1, "tools": [], "plan": [],
                "state": {}, "observations": [], "budget": {},
                "instruction": "i", "prompt_profile": "general",
            })
            second = provider.propose({
                "objective": "t", "round": 2, "tools": [], "plan": [],
                "state": {}, "observations": [], "budget": {},
                "instruction": "i", "prompt_profile": "general",
            })

        for proposal in (first, second):
            canonical_json(proposal.metadata)
            canonical_json(proposal.receipt())
        self.assertEqual(second.metadata["cache_hit_bp"], 2500)
        self.assertGreater(second.metadata["prompt_prefix_bp"], 0)


if __name__ == "__main__":
    unittest.main()