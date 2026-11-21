"""S3 compaction: thresholds, 9-section summary, keepEarlierAsks, boundaries."""

from __future__ import annotations

import hashlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    COMPACTION_MARKER,
    Compaction,
    CompactionConfig,
    CompactionError,
    Done,
    EventBus,
    HarnessCore,
    SUMMARY_SECTIONS,
    SessionStore,
    StreamUsage,
    TextDelta,
    ToolRegistry,
    ToolUse,
    build_summary_prompt,
    ensure_earlier_asks,
    estimate_messages_tokens,
    estimate_text_tokens,
    extract_section,
    strip_analysis,
)

SUMMARY = """<analysis>
The user wants a bicycle animation. I made a plan.
</analysis>
<summary>
1. Primary Request and Intent:
Build a pelican bicycle animation.

2. Key Technical Concepts:
SVG keyframes, geometry checks.

3. Files and Code Sections:
- work/pelican.html

4. Errors and fixes:
Foot-pedal distance failed; moved the foot.

5. Problem Solving:
Rig scaffold proposed.

6. All user messages:
- user: build the pelican
- user: keep it single-file

7. Pending Tasks:
Re-run the motion gate.

8. Current Work:
Editing the leg coordinates.

9. Optional Next Step:
Run motion_check.
</summary>"""


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


class FailOnceProvider:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.failed = False

    def stream(self, request):
        if not self.failed:
            self.failed = True
            raise RuntimeError("summarizer offline")
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


def config(**overrides) -> CompactionConfig:
    base = dict(window_tokens=200_000, threshold=0.95, reserve_tokens=60_000,
                min_trigger_tokens=100_000, deadline_ms=5_000,
                summary_max_tokens=256)
    base.update(overrides)
    return CompactionConfig(**base)


def conversation() -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "build it"}]},
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t1", "name": "edit_file",
            "input": {"path": "a.html", "content": "x"}}]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "t1",
            "content": "ok", "is_error": False}]},
    ]


class EstimateTests(unittest.TestCase):
    def test_ascii_and_cjk(self) -> None:
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertEqual(estimate_text_tokens("x" * 400), 100)
        self.assertGreaterEqual(estimate_text_tokens("中" * 10), 10)
        self.assertGreater(
            estimate_messages_tokens(conversation()),
            estimate_text_tokens("build it"))


class TriggerTests(unittest.TestCase):
    def test_effective_trigger(self) -> None:
        self.assertEqual(config().trigger_tokens, 140_000)
        self.assertEqual(config(window_tokens=128_000).trigger_tokens, 100_000)
        self.assertIsNone(config(window_tokens=100_000).trigger_tokens)
        self.assertEqual(
            config(window_tokens=1_000_000).trigger_tokens, 940_000)

    def test_should_compact_on_tokens_and_idle(self) -> None:
        compaction = Compaction(ScriptedProvider(), config=config())
        small = [{"role": "user", "content": "x" * 40}]
        self.assertFalse(compaction.should_compact(small))
        big = [{"role": "user", "content": "x" * 600_000}]
        self.assertTrue(compaction.should_compact(big))
        meaningful = [{"role": "user", "content": "x" * 4_000}]
        self.assertTrue(compaction.should_compact(meaningful, idle_seconds=3600))
        self.assertFalse(compaction.should_compact(small, idle_seconds=3600))
        self.assertFalse(compaction.should_compact([], idle_seconds=3600))


class PromptTests(unittest.TestCase):
    def test_nine_sections_and_analysis(self) -> None:
        prompt = build_summary_prompt("CONVERSATION TEXT")
        for index, title in enumerate(SUMMARY_SECTIONS, start=1):
            self.assertIn(f"{index}. {title}", prompt)
        self.assertEqual(len(SUMMARY_SECTIONS), 9)
        self.assertIn("<analysis>", prompt)
        self.assertIn("CONVERSATION TEXT", prompt)

    def test_earlier_summary_asks_are_injected(self) -> None:
        earlier = "6. All user messages:\n- user: keep feature A\n"
        prompt = build_summary_prompt("NEW", earlier_summary=earlier)
        self.assertIn("verbatim", prompt)
        self.assertIn("keep feature A", prompt)
        self.assertIn("<earlier_summary>", prompt)


class SummaryTextTests(unittest.TestCase):
    def test_strip_analysis_and_extract(self) -> None:
        cleaned = strip_analysis(SUMMARY)
        self.assertNotIn("<analysis>", cleaned)
        self.assertIn("Primary Request", cleaned)
        self.assertEqual(
            extract_section(cleaned, "Pending Tasks"), "Re-run the motion gate.")

    def test_carry_over_user_asks(self) -> None:
        earlier = ("6. All user messages:\n- user: keep feature A\n\n"
                   "7. Pending Tasks:\nold\n")
        fresh = ("1. Primary Request and Intent:\nx\n\n"
                 "6. All user messages:\n- user: new request B\n\n"
                 "7. Pending Tasks:\nnew\n")
        merged = ensure_earlier_asks(fresh, earlier)
        users = extract_section(merged, "All user messages")
        self.assertIn("keep feature A", users)
        self.assertIn("new request B", users)
        self.assertEqual(extract_section(merged, "Pending Tasks"), "new")
        # idempotent when the model already carried it
        self.assertEqual(ensure_earlier_asks(merged, earlier), merged)


class CompactTests(unittest.TestCase):
    def test_compact_builds_boundary_and_continuation(self) -> None:
        usage = StreamUsage(input_tokens=100, output_tokens=50,
                            cost_microusd=7, cached_tokens=3)
        provider = ScriptedProvider([TextDelta(SUMMARY), Done(usage=usage)])
        compaction = Compaction(provider, config=config())
        result = compaction.compact(conversation())
        self.assertNotIn("<analysis>", result.summary)
        self.assertEqual(result.boundary["type"], "compact_boundary")
        self.assertEqual(result.boundary["before_tokens"], result.before_tokens)
        self.assertEqual(result.boundary["after_tokens"], result.after_tokens)
        self.assertEqual(result.boundary["sections"], 9)
        self.assertEqual(len(result.boundary["summary_sha256"]), 64)
        self.assertEqual(result.usage.output_tokens, 50)
        text = result.messages[0]["content"][0]["text"]
        self.assertTrue(text.startswith(COMPACTION_MARKER))
        self.assertIn("<summary>", text)
        self.assertEqual(provider.requests[0].tools, [])
        self.assertEqual(provider.requests[0].max_tokens, 256)
        self.assertIn("CONVERSATION", provider.requests[0].messages[0]["content"][0]["text"].upper())
        # deterministic hash of the exact summary text
        self.assertEqual(
            result.boundary["summary_sha256"],
            hashlib.sha256(result.summary.encode()).hexdigest())

    def test_recompaction_keeps_earlier_asks(self) -> None:
        earlier = ("6. All user messages:\n- user: keep feature A\n\n"
                   "7. Pending Tasks:\nold\n")
        previous = [{"role": "user", "content": [{
            "type": "text",
            "text": f"{COMPACTION_MARKER}\n\n<summary>\n{earlier}\n</summary>",
        }]}]
        provider = ScriptedProvider([TextDelta(SUMMARY), Done()])
        compaction = Compaction(provider, config=config())
        result = compaction.compact(previous)
        self.assertIn("keep feature A",
                      extract_section(result.summary, "All user messages"))
        self.assertIn("keep feature A",
                      provider.requests[0].messages[0]["content"][0]["text"])

    def test_empty_summary_is_typed(self) -> None:
        compaction = Compaction(ScriptedProvider([Done()]), config=config())
        with self.assertRaises(CompactionError):
            compaction.compact(conversation())

    def test_summarizer_tool_call_is_rejected(self) -> None:
        provider = ScriptedProvider([
            ToolUse("t", "edit_file", {"path": "x"}), Done()])
        compaction = Compaction(provider, config=config())
        with self.assertRaises(CompactionError):
            compaction.compact(conversation())


class CoreIntegrationTests(unittest.TestCase):
    def small_config(self) -> CompactionConfig:
        return CompactionConfig(
            window_tokens=1_000, threshold=0.5, reserve_tokens=100,
            min_trigger_tokens=1, deadline_ms=5_000, summary_max_tokens=256)

    def test_core_compacts_before_the_turn_and_records_the_tree(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider(
                [TextDelta(SUMMARY), Done(usage=StreamUsage(
                    input_tokens=10, output_tokens=5, cost_microusd=3))],
                [TextDelta("final"), Done(usage=StreamUsage(
                    input_tokens=1, output_tokens=1, cost_microusd=1))],
            )
            compaction = Compaction(provider, config=self.small_config())
            session = SessionStore(tmp, "/work/project", session_id="s1")
            bus = EventBus()
            core = HarnessCore(
                provider, ToolRegistry([]), bus=bus,
                compaction=compaction, session=session,
            )
            result = core.run(
                [{"role": "user", "content": [{"type": "text",
                                               "text": "x" * 2_400}]}],
                system="sys",
            )
        self.assertTrue(result.completed)
        self.assertEqual(result.compactions, 1)
        self.assertEqual(result.turns, 1)
        # history is now the continuation summary + the final answer
        self.assertEqual(len(result.messages), 2)
        self.assertTrue(result.messages[0]["content"][0]["text"].startswith(
            COMPACTION_MARKER))
        self.assertEqual(result.messages[-1]["content"][0]["text"], "final")
        self.assertIn("compact_boundary", bus.types())
        # compaction usage is counted
        self.assertEqual(result.usage.input_tokens, 11)
        self.assertEqual(result.usage.output_tokens, 6)
        self.assertEqual(result.usage.cost_microusd, 4)

        events = session.events()
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds, [
            "session_start", "message", "compact_boundary",
            "message", "message",
        ])
        boundary = events[2]
        self.assertGreater(boundary["beforeTokens"], boundary["afterTokens"])
        self.assertEqual(len(boundary["summarySha256"]), 64)
        self.assertEqual(len(session.messages()), 3)

    def test_compaction_failure_is_reported_and_run_continues(self) -> None:
        provider = FailOnceProvider([TextDelta("final"), Done()])
        compaction = Compaction(provider, config=self.small_config())
        bus = EventBus()
        core = HarnessCore(
            provider, ToolRegistry([]), bus=bus, compaction=compaction)
        result = core.run(
            [{"role": "user", "content": [{"type": "text", "text": "x" * 2_400}]}],
            system="sys",
        )
        self.assertTrue(result.completed)
        self.assertEqual(result.compactions, 0)
        self.assertIn("compact.error", bus.types())
        self.assertEqual(result.messages[-1]["content"][0]["text"], "final")


if __name__ == "__main__":
    unittest.main()
