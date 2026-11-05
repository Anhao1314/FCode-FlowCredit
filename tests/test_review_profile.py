"""Reviewer profile: a read-only projection of signed review decisions.

The profile must never become a second source of truth. It is derived from the
``review_decided`` events already committed to exported traces, so these tests
pin the projection's edges: unknown scope, invalid decisions, newest-first
order, majority with a deterministic tie-break, and the on-demand trace scan.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.review_profile import (  # noqa: E402
    BASIS,
    DECISIONS,
    RECENT_LIMIT,
    build_profile,
    review_records,
    scope_of,
)

VIEWER = ROOT / "viewer"


def review_event(
    decision: str,
    *,
    sequence: int,
    at: str,
    reason: str = "",
    reviewer: str = "reviewer",
    event_type: str = "review_decided",
    detail: dict | None = None,
) -> dict:
    payload = {
        "detail": detail
        if detail is not None
        else {"decision": decision, "reason": reason, "reviewer": reviewer}
    }
    return {"sequence": sequence, "type": event_type, "payload": payload, "at": at}


def make_trace(
    *,
    module: str = "finance-research",
    template: str = "full",
    outcome: str = "succeeded",
    events: list[dict] | None = None,
) -> dict:
    return {
        "task": {"binding": {"module": module, "scenario": template}},
        "terminal": {"status": outcome},
        "events": list(events or []),
    }


class ScopeTests(unittest.TestCase):
    def test_scope_uses_binding_and_terminal(self) -> None:
        self.assertEqual(
            scope_of(make_trace()),
            {"module": "finance-research", "template": "full", "outcome": "succeeded"},
        )

    def test_missing_values_are_explicit_unknown(self) -> None:
        traces = (
            {},
            {"task": {}, "terminal": None},
            {"task": {"binding": {"module": "", "scenario": None}}, "terminal": {"status": ""}},
        )
        for trace in traces:
            with self.subTest(trace=trace):
                self.assertEqual(
                    scope_of(trace),
                    {"module": "unknown", "template": "unknown", "outcome": "unknown"},
                )


class ReviewRecordTests(unittest.TestCase):
    def test_extracts_valid_reviews_in_chain_order(self) -> None:
        trace = make_trace(
            events=[
                review_event(
                    "accept",
                    sequence=2,
                    at="2026-01-01T00:00:02Z",
                    reason="证据充分",
                    reviewer="林岚",
                ),
                review_event("approved", sequence=3, at="2026-01-01T00:00:03Z"),
                {
                    "sequence": 4,
                    "type": "tool_succeeded",
                    "payload": {"detail": {"decision": "accept"}},
                    "at": "2026-01-01T00:00:04Z",
                },
                {
                    "sequence": 5,
                    "type": "review_decided",
                    "payload": {},
                    "at": "2026-01-01T00:00:05Z",
                },
                review_event(
                    "redo",
                    sequence=6,
                    at="2026-01-01T00:00:06Z",
                    reason=None,  # type: ignore[arg-type]
                    reviewer=None,  # type: ignore[arg-type]
                ),
            ]
        )
        records = review_records(trace, "run-a")
        self.assertEqual([record["decision"] for record in records], ["accept", "redo"])
        self.assertEqual(
            records[0],
            {
                "run_id": "run-a",
                "decision": "accept",
                "reason": "证据充分",
                "reviewer": "林岚",
                "sequence": 2,
                "at": "2026-01-01T00:00:02Z",
            },
        )
        self.assertEqual(records[1]["reason"], "")
        self.assertEqual(records[1]["reviewer"], "")

    def test_missing_or_malformed_events_are_empty(self) -> None:
        self.assertEqual(review_records({}, "run-a"), [])
        self.assertEqual(review_records({"events": "not-a-list"}, "run-a"), [])
        self.assertEqual(review_records({"events": [None, 3]}, "run-a"), [])


class BuildProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.traces = [
            (
                "run-a",
                make_trace(
                    events=[
                        review_event(
                            "accept", sequence=3, at="2026-01-01T00:00:01Z", reason="a"
                        ),
                        review_event(
                            "qualified", sequence=4, at="2026-01-01T00:00:02Z", reason="q"
                        ),
                    ]
                ),
            ),
            (
                "run-b",
                make_trace(
                    events=[
                        review_event(
                            "qualified", sequence=3, at="2026-01-01T00:00:03Z", reason="q2"
                        )
                    ]
                ),
            ),
            (
                "run-c",
                make_trace(
                    template="blocked",
                    outcome="failed",
                    events=[
                        review_event(
                            "redo", sequence=3, at="2026-01-01T00:00:04Z", reason="r"
                        )
                    ],
                ),
            ),
            (
                "run-d",
                make_trace(
                    module="other-module",
                    events=[
                        review_event(
                            "accept", sequence=3, at="2026-01-01T00:00:05Z", reason="a2"
                        )
                    ],
                ),
            ),
        ]

    def test_counts_every_review(self) -> None:
        profile = build_profile(self.traces)
        self.assertEqual(profile["total"], 5)
        self.assertEqual(
            profile["counts"],
            {"accept": 2, "needs_evidence": 0, "qualified": 2, "redo": 1},
        )
        self.assertEqual(profile["scope"], {"module": "", "template": "", "outcome": ""})

    def test_scope_filters_are_exact_and_conjunctive(self) -> None:
        self.assertEqual(build_profile(self.traces, module="finance-research")["total"], 4)
        self.assertEqual(build_profile(self.traces, template="full")["total"], 4)
        self.assertEqual(build_profile(self.traces, outcome="succeeded")["total"], 4)

        profile = build_profile(
            self.traces,
            module="finance-research",
            template="full",
            outcome="succeeded",
        )
        self.assertEqual(profile["total"], 3)
        self.assertEqual(
            profile["counts"],
            {"accept": 1, "needs_evidence": 0, "qualified": 2, "redo": 0},
        )
        self.assertEqual(
            profile["scope"],
            {"module": "finance-research", "template": "full", "outcome": "succeeded"},
        )

        empty = build_profile(
            self.traces, module="other-module", template="full", outcome="failed"
        )
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["counts"], {"accept": 0, "needs_evidence": 0, "qualified": 0, "redo": 0})
        self.assertIsNone(empty["suggested"])
        self.assertEqual(empty["recent"], [])

    def test_recent_is_newest_first_and_capped(self) -> None:
        events = [
            review_event(
                "accept",
                sequence=index,
                at=f"2026-01-01T00:00:0{index}Z",
                reason=f"r{index}",
            )
            for index in range(1, 8)
        ]
        profile = build_profile([("run-a", make_trace(events=events))])
        self.assertEqual(len(profile["recent"]), RECENT_LIMIT)
        self.assertEqual([record["sequence"] for record in profile["recent"]], [7, 6, 5, 4, 3])
        self.assertTrue(all(record["run_id"] == "run-a" for record in profile["recent"]))

    def test_recent_ties_use_sequence(self) -> None:
        older = make_trace(
            events=[review_event("accept", sequence=2, at="2026-01-01T00:00:00Z")]
        )
        newer = make_trace(
            events=[review_event("redo", sequence=9, at="2026-01-01T00:00:00Z")]
        )
        profile = build_profile([("run-a", older), ("run-b", newer)])
        self.assertEqual([record["decision"] for record in profile["recent"]], ["redo", "accept"])

    def test_suggestion_is_majority_with_the_latest_reason(self) -> None:
        trace = make_trace(
            events=[
                review_event(
                    "qualified", sequence=3, at="2026-01-01T00:00:01Z", reason="保留意见"
                ),
                review_event(
                    "qualified", sequence=4, at="2026-01-01T00:00:02Z", reason="仍需说明口径"
                ),
                review_event(
                    "accept", sequence=5, at="2026-01-01T00:00:03Z", reason="这次证据够了"
                ),
            ]
        )
        profile = build_profile([("run-a", trace)])
        self.assertEqual(
            profile["suggested"],
            {
                "decision": "qualified",
                "support": 2,
                "total": 3,
                "basis": BASIS,
                "last_reason": "仍需说明口径",
            },
        )
        self.assertEqual(BASIS, "同类历史多数")

    def test_tie_uses_the_most_recent_decision(self) -> None:
        two_way = make_trace(
            events=[
                review_event("accept", sequence=3, at="2026-01-01T00:00:01Z", reason="old"),
                review_event("redo", sequence=4, at="2026-01-01T00:00:02Z", reason="new"),
            ]
        )
        profile = build_profile([("run-a", two_way)])
        self.assertEqual(profile["suggested"]["decision"], "redo")
        self.assertEqual(profile["suggested"]["support"], 1)
        self.assertEqual(profile["suggested"]["total"], 2)
        self.assertEqual(profile["suggested"]["last_reason"], "new")

        four_way = make_trace(
            events=[
                review_event("accept", sequence=3, at="2026-01-01T00:00:01Z"),
                review_event("qualified", sequence=4, at="2026-01-01T00:00:02Z"),
                review_event("redo", sequence=5, at="2026-01-01T00:00:03Z"),
                review_event("needs_evidence", sequence=6, at="2026-01-01T00:00:04Z"),
            ]
        )
        profile = build_profile([("run-b", four_way)])
        self.assertEqual(profile["suggested"]["decision"], "needs_evidence")
        self.assertEqual(profile["suggested"]["support"], 1)
        self.assertEqual(profile["suggested"]["total"], 4)

    def test_suggestion_reason_matches_the_suggested_decision(self) -> None:
        trace = make_trace(events=[
            review_event("accept", sequence=3, at="2026-01-01T00:00:01Z", reason="old accept"),
            review_event("qualified", sequence=4, at="2026-01-01T00:00:02Z", reason="new qualified"),
            review_event("accept", sequence=5, at="2026-01-01T00:00:03Z", reason="new accept"),
        ])
        profile = build_profile([("run-a", trace)])
        self.assertEqual(profile["suggested"]["decision"], "accept")
        self.assertEqual(profile["suggested"]["last_reason"], "new accept")

    def test_single_review_has_no_suggestion(self) -> None:
        trace = make_trace(
            events=[review_event("accept", sequence=3, at="2026-01-01T00:00:01Z")]
        )
        profile = build_profile([("run-a", trace)])
        self.assertEqual(profile["total"], 1)
        self.assertEqual(profile["counts"]["accept"], 1)
        self.assertIsNone(profile["suggested"])

    def test_counts_follow_the_known_decisions(self) -> None:
        self.assertEqual(DECISIONS, ("accept", "needs_evidence", "qualified", "redo"))
        counts = build_profile([])["counts"]
        self.assertEqual(tuple(counts), DECISIONS)
        self.assertEqual(set(counts.values()), {0})

    def test_empty_profile_is_explicit(self) -> None:
        self.assertEqual(
            build_profile([]),
            {
                "scope": {"module": "", "template": "", "outcome": ""},
                "total": 0,
                "counts": {"accept": 0, "needs_evidence": 0, "qualified": 0, "redo": 0},
                "suggested": None,
                "recent": [],
            },
        )
        scoped = build_profile(
            [], module="finance-research", template="full", outcome="succeeded"
        )
        self.assertEqual(
            scoped["scope"],
            {"module": "finance-research", "template": "full", "outcome": "succeeded"},
        )
        self.assertEqual(scoped["total"], 0)

    def test_traces_without_reviews_are_ignored(self) -> None:
        traces = self.traces + [("run-empty", make_trace())]
        self.assertEqual(build_profile(traces)["total"], 5)


def load_server():
    """Import the console server by path without starting it."""
    path = VIEWER / "server.py"
    spec = importlib.util.spec_from_file_location("fcode_viewer_server_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load viewer/server.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so register
    # the module before executing it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunManagerScanTests(unittest.TestCase):
    def test_scan_reads_exported_traces_and_skips_broken_files(self) -> None:
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            runs = pathlib.Path(tmp) / "runs"
            exported = (
                (
                    "run-1",
                    make_trace(
                        events=[
                            review_event(
                                "accept", sequence=3, at="2026-01-01T00:00:01Z", reason="ok"
                            )
                        ]
                    ),
                ),
                (
                    "run-2",
                    make_trace(
                        events=[
                            review_event(
                                "qualified",
                                sequence=3,
                                at="2026-01-01T00:00:02Z",
                                reason="reserve",
                            )
                        ]
                    ),
                ),
            )
            for run_id, trace in exported:
                directory = runs / run_id
                directory.mkdir(parents=True)
                (directory / "trace.json").write_text(
                    json.dumps(trace, ensure_ascii=False), encoding="utf-8"
                )
            broken = runs / "run-3"
            broken.mkdir()
            (broken / "trace.json").write_text("{not json", encoding="utf-8")

            original = server.RUNS_DIR
            server.RUNS_DIR = runs
            try:
                manager = server.RunManager(None, "", 0.0)
                profile = manager.review_profile(
                    module="finance-research", template="full", outcome="succeeded"
                )
            finally:
                server.RUNS_DIR = original

            self.assertEqual(profile["total"], 2)
            self.assertEqual(
                profile["counts"],
                {"accept": 1, "needs_evidence": 0, "qualified": 1, "redo": 0},
            )
            self.assertEqual(profile["suggested"]["decision"], "qualified")
            self.assertEqual(
                [record["run_id"] for record in profile["recent"]], ["run-2", "run-1"]
            )


if __name__ == "__main__":
    unittest.main()
