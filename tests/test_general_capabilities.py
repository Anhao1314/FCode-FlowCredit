"""Governed general capabilities: sandbox, fetch_url, and the gate that holds.

Phase 1 of the general-capabilities batch. Every test is offline: the fetcher
runs against injected resolvers and transports, so the SSRF matrix never
touches a real network, and the "unknown outcome" path is provoked with a
transport that raises exactly what a dead connection raises.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError, Task  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.permissions import Permissions  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402
from flowagent.tools.general.planner import GeneralPlanner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import GENERAL_TOOLS, build_general_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402


def _html(body: str = "<html><head><title>T</title></head><body><p>hello world</p></body></html>") -> bytes:
    return body.encode("utf-8")


class FakeResolver:
    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.seen: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.seen.append(host)
        return list(self.addresses)


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]] | None = None,
                 error: Exception | None = None) -> None:
        self.responses = responses or []
        self.error = error
        self.calls: list[tuple[str, str, str, int, str]] = []

    def __call__(self, scheme: str, host: str, ip: str, port: int, target: str):
        self.calls.append((scheme, host, ip, port, target))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("no canned response left")
        return self.responses.pop(0)


def _fetcher(resolver: FakeResolver, transport: FakeTransport, **kwargs) -> UrlFetcher:
    return UrlFetcher(resolver=resolver, transport=transport, **kwargs)


class SandboxJailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _denied(self, path: str) -> str:
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.resolve(path)
        self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")
        return ctx.exception.message

    def test_absolute_paths_are_refused(self) -> None:
        self._denied("/etc/passwd")
        self._denied("/tmp/escape")

    def test_dotdot_components_are_refused(self) -> None:
        self._denied("../outside.txt")
        self._denied("reports/../../outside.txt")
        self._denied("a/..")

    def test_empty_and_shape_odd_paths_are_refused(self) -> None:
        self._denied("")
        self._denied("reports//file.txt")
        self._denied("back\\slash.txt")
        self._denied("nul\x00byte.txt")

    def test_git_metadata_is_not_addressable(self) -> None:
        self._denied(".git/config")

    def test_symlink_escape_is_refused(self) -> None:
        self.sandbox.ensure()
        outside = pathlib.Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = pathlib.Path(self._tmp.name) / "ws" / "link"
        link.symlink_to(outside)
        with self.assertRaises(AgentError) as ctx:
            self.sandbox.read("link")
        self.assertEqual(ctx.exception.code, "SANDBOX_PATH_DENIED")

    def test_write_records_before_and_after_hashes_and_commits(self) -> None:
        record = self.sandbox.write("notes/a.md", "first")
        self.assertIsNone(record["sha256_before"])
        self.assertTrue(record["changed"])
        self.assertTrue(record["commit"], "a new file is a change and must be committed")
        second = self.sandbox.write("notes/a.md", "second")
        self.assertNotEqual(second["sha256_before"], second["sha256_after"])
        self.assertTrue(second["commit"], "a changed write must produce a git commit")
        self.assertIn("-first", second["diff"])
        self.assertIn("+second", second["diff"])
        self.assertFalse(second["truncated"])
        reread = self.sandbox.read("notes/a.md")
        self.assertEqual(reread["sha256"], second["sha256_after"])

    def test_identical_rewrite_is_visible_but_not_committed(self) -> None:
        self.sandbox.write("same.txt", "stable")
        again = self.sandbox.write("same.txt", "stable")
        self.assertFalse(again["changed"])
        self.assertEqual(again["commit"], "")
        self.assertEqual(again["sha256_before"], again["sha256_after"])
        self.assertEqual(again["diff"], "")
        self.assertFalse(again["truncated"])

    def test_diff_preview_shows_a_real_unified_diff(self) -> None:
        self.sandbox.write("report.md", "line one\nline two\n")
        preview = self.sandbox.diff_preview("report.md", "line one\nline two changed\n")
        self.assertFalse(preview["created"])
        self.assertEqual(preview["removals"], 1)
        self.assertEqual(preview["additions"], 1)
        self.assertIn("-line two", preview["diff"])
        self.assertIn("+line two changed", preview["diff"])
        fresh = self.sandbox.diff_preview("new.md", "brand new\n")
        self.assertTrue(fresh["created"])
        self.assertEqual(fresh["additions"], 1)

    def test_ls_hides_git_metadata(self) -> None:
        self.sandbox.write("a.txt", "a")
        entries = {item["path"] for item in self.sandbox.ls()}
        self.assertEqual(entries, {"a.txt"})


class FetchUrlTests(unittest.TestCase):
    def _ok(self, **kwargs):
        return FakeResolver(["93.184.216.34"]), FakeTransport([
            (200, {"Content-Type": "text/html; charset=utf-8"}, _html()),
        ]), kwargs

    def test_private_ranges_are_refused_before_any_connection(self) -> None:
        for address in (
            "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
            "192.168.1.1", "169.254.169.254", "::1", "fd12:3456:789a::1",
        ):
            transport = FakeTransport()
            fetcher = _fetcher(FakeResolver([address]), transport)
            with self.assertRaises(AgentError) as ctx:
                fetcher.fetch("https://internal.example.com/x")
            self.assertEqual(ctx.exception.code, "FETCH_HOST_DENIED", address)
            self.assertEqual(transport.calls, [], f"{address} must never be connected")

    def test_dns_rebinding_is_refused_when_any_address_is_private(self) -> None:
        transport = FakeTransport()
        fetcher = _fetcher(FakeResolver(["93.184.216.34", "127.0.0.1"]), transport)
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://rebind.example.com/x")
        self.assertEqual(ctx.exception.code, "FETCH_HOST_DENIED")
        self.assertEqual(transport.calls, [])

    def test_allowlist_admits_a_host_with_private_addresses(self) -> None:
        resolver, transport, _ = self._ok()
        fetcher = _fetcher(FakeResolver(["127.0.0.1"]), transport, allow_hosts=("fixture.local",))
        result = fetcher.fetch("https://fixture.local/doc")
        self.assertEqual(result["status"], 200)
        self.assertEqual(transport.calls[0][2], "127.0.0.1", "must pin the validated address")

    def test_connection_pins_the_validated_ip_not_the_hostname(self) -> None:
        resolver, transport, _ = self._ok()
        fetcher = _fetcher(resolver, transport)
        fetcher.fetch("https://example.com/x")
        scheme, host, ip, port, target = transport.calls[0]
        self.assertEqual(ip, "93.184.216.34")
        self.assertEqual(target, "/x")

    def test_timeout_is_an_unknown_outcome(self) -> None:
        fetcher = _fetcher(FakeResolver(["93.184.216.34"]), FakeTransport(error=TimeoutError("late")))
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://slow.example.com/x")
        self.assertEqual(ctx.exception.code, "FETCH_TIMEOUT")
        self.assertEqual(ctx.exception.details.get("outcome"), "unknown")

    def test_midflight_transport_death_is_unknown(self) -> None:
        import socket

        fetcher = _fetcher(FakeResolver(["93.184.216.34"]), FakeTransport(error=OSError("reset")))
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://flaky.example.com/x")
        self.assertEqual(ctx.exception.code, "FETCH_UNKNOWN")
        self.assertEqual(ctx.exception.details.get("outcome"), "unknown")

    def test_pre_dispatch_failures_are_confirmed_not_unknown(self) -> None:
        fetcher = UrlFetcher(resolver=FakeResolver([]), transport=FakeTransport())
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("ftp://example.com/f")
        self.assertEqual(ctx.exception.code, "FETCH_URL_DENIED")
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://no-address.invalid/x")
        self.assertEqual(ctx.exception.code, "FETCH_UNRESOLVED")
        self.assertNotIn("outcome", ctx.exception.details or {})

    def test_body_cap_is_enforced(self) -> None:
        fetcher = _fetcher(FakeResolver(["93.184.216.34"]), FakeTransport([
            (200, {"Content-Type": "text/plain"}, b"x" * (1024 * 1024 + 1)),
        ]), max_bytes=1024 * 1024)
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://big.example.com/x")
        self.assertEqual(ctx.exception.code, "FETCH_TOO_LARGE")

    def test_redirects_are_revalidated(self) -> None:
        transport = FakeTransport([
            (302, {"Location": "https://private.target/inner"}, b""),
        ])
        fetcher = _fetcher(FakeResolver(["93.184.216.34"]), transport)
        # The redirect target resolves private: refused even though the hop
        # itself was public.
        resolver = {"public": ["93.184.216.34"]}
        calls: list[str] = []

        def resolve(host: str, port: int) -> list[str]:
            calls.append(host)
            return resolver.get(host, ["127.0.0.1"])

        fetcher = UrlFetcher(resolver=resolve, transport=transport)
        with self.assertRaises(AgentError) as ctx:
            fetcher.fetch("https://public.example/x")
        self.assertEqual(ctx.exception.code, "FETCH_HOST_DENIED")

    def test_result_carries_receipt_fields_and_honesty_label(self) -> None:
        import hashlib

        body = _html()
        fetcher = _fetcher(FakeResolver(["93.184.216.34"]), FakeTransport([
            (200, {"Content-Type": "text/html; charset=utf-8"}, body),
        ]))
        result = fetcher.fetch("https://example.com/x?q=1")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["host"], "example.com")
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["content_truth"], "not-verified")
        self.assertIn("hello world", result["text"])
        self.assertEqual(result["title"], "T")


class GeneralToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")
        self.fetcher = UrlFetcher(resolver=FakeResolver(["93.184.216.34"]), transport=FakeTransport([
            (200, {"Content-Type": "text/plain"}, b"plain text"),
        ]))
        self.registry = ToolRegistry(build_general_tools(self.sandbox, self.fetcher))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_registration_order_is_pinned(self) -> None:
        self.assertEqual(tuple(self.registry.names), GENERAL_TOOLS)
        effects = {spec.name: spec.effect for spec in
                   (self.registry.get(name) for name in self.registry.names)}
        self.assertEqual(effects["write_file"], "write")
        self.assertEqual(effects["fetch_url"], "network")

    def test_write_passes_only_when_the_grant_names_the_effect(self) -> None:
        tools = ["ls_workspace", "read_file", "write_file", "fetch_url"]
        legacy_grant = {"allowed_tools": tools}
        args = {"path": "a.txt", "content": "text"}
        denied = Permissions(self.registry, tools, legacy_grant).gate("write_file", args)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, "TOOL_EFFECT_DENIED")

        wide = dict(legacy_grant, allowed_effects=["read", "write", "network"])
        allowed = Permissions(self.registry, tools, wide).gate("write_file", args)
        self.assertTrue(allowed.allowed)

        fetch_denied = Permissions(self.registry, tools, wide and dict(legacy_grant, allowed_effects=["read", "write"])).gate(
            "fetch_url", {"url": "https://example.com/x"},
        )
        self.assertFalse(fetch_denied.allowed)
        self.assertEqual(fetch_denied.code, "TOOL_EFFECT_DENIED")

    def test_oversized_content_is_refused_at_the_gate(self) -> None:
        tools = ["write_file"]
        grant = {"allowed_tools": tools, "allowed_effects": ["read", "write", "network"]}
        gate = Permissions(self.registry, tools, grant)
        decision = gate.gate("write_file", {"path": "big.txt", "content": "x" * 65_537})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_ARGUMENTS_INVALID")

    def test_grant_cannot_name_an_unknown_effect_class(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            Permissions(self.registry, [], {"allowed_tools": []}, ("read", "side-effect"))
        self.assertEqual(ctx.exception.code, "GRANT_EFFECTS_INVALID")


class GeneralPlannerTests(unittest.TestCase):
    def _observation(self, tool: str, arguments: dict, result: dict) -> Observation:
        from flowagent.core.hashing import digest

        return Observation(tool=tool, arguments=arguments, result=result, result_hash=digest(result))

    def test_plan_fetches_sources_then_writes_the_report(self) -> None:
        planner = GeneralPlanner(["https://a.example/x", "https://b.example/y"])
        plan = planner.decompose("调研主题")
        self.assertEqual([todo.tool for todo in plan.todos],
                         ["fetch_url", "fetch_url", "write_file"])
        self.assertEqual(plan.todos[-1].arguments["path"], "research.md")

    def test_write_content_is_composed_from_arrived_evidence_only(self) -> None:
        planner = GeneralPlanner(["https://a.example/x"])
        plan = planner.decompose("调研主题")
        plan = planner.advance(plan, self._observation("fetch_url", {"url": "https://a.example/x"}, {
            "url": "https://a.example/x", "title": "A", "text": "evidence body",
            "sha256": "ab" * 32, "bytes": 10, "retrieved_at": "2026-10-25T00:00:00+00:00",
        }))
        write = plan.todos[-1]
        self.assertIn("evidence body", write.arguments["content"])
        self.assertIn("not-verified", write.arguments["content"])
        done = planner.advance(plan, self._observation("write_file",
            {"path": "research.md", "content": write.arguments["content"]},
            {"computed": True, "sha256_after": "cd" * 32, "path": "research.md"}))
        self.assertEqual(done.todos[-1].status, "done")


class GeneralRunTests(unittest.TestCase):
    """An entire general delegation, offline: fetch (faked) then write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _world(self, spec: dict):
        return GENERAL_DOMAIN.create_world(
            spec=spec,
            sandbox=WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws"),
            fetcher=UrlFetcher(resolver=FakeResolver(["93.184.216.34"]), transport=FakeTransport([
                (200, {"Content-Type": "text/plain"}, b"the answer is 42"),
            ])),
        )

    def test_full_delegation_succeeds_with_file_state_verification(self) -> None:
        scenario = get_scenario("general-research")
        world = self._world({"sources": ["https://answer.example/final"]})
        harness = Harness(
            store=TaskStore(pathlib.Path(self._tmp.name) / "t.sqlite"),
            registry=ToolRegistry(world.build_tools()),
            provider=MockProvider(policy="research"),
            world=world,
            limits=scenario.limits,
            receipts=None,  # type: ignore[arg-type] — receipts only gate model calls; not used here
            verifier=lambda observation: world.verify_observation(observation),
        )
        recorder = TraceRecorder(harness.store, provider_name="offline-mock", model_name="m")
        task = harness.create(
            task_id="task-general",
            objective="调研 answer.example，写 research.md",
            principal="test",
            binding={"scenario": scenario.key},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        planner = GENERAL_DOMAIN.make_planner(world)
        task = Loop(
            harness, planner=planner, max_rounds=scenario.max_rounds,
            observer=recorder, result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        self.assertEqual(task.status, "succeeded")
        report = world.sandbox.read("research.md")
        self.assertIn("the answer is 42", report["content"])
        # The verified result carries the recomputed file state.
        self.assertEqual(task.result["sha256"], report["sha256"])
        self.assertEqual(task.result["verification"], "file-state-recomputed")
        trace = recorder.build(task)
        verify_trace(trace)
        harness.store.close()

    def test_tampered_file_fails_verification(self) -> None:
        world = self._world({"sources": ["https://answer.example/final"]})
        observation = Observation(
            tool="write_file",
            arguments={"path": "research.md", "content": "x"},
            result={"computed": True, "path": "research.md",
                    "sha256_after": "00" * 32, "changed": True,
                    "bytes": 1, "sha256_before": None, "commit": "abc"},
        )
        world.sandbox.write("research.md", "x")
        with self.assertRaises(AgentError) as ctx:
            world.verify_observation(observation)
        self.assertEqual(ctx.exception.code, "RESULT_VERIFICATION_FAILED")


class ToolUnknownOutcomeTests(unittest.TestCase):
    def test_fetch_timeout_ends_the_run_as_unknown_never_retried(self) -> None:
        scenario = get_scenario("general-research")
        world = GENERAL_DOMAIN.create_world(
            spec={"sources": ["https://hang.example/x"]},
            sandbox=WorkspaceSandbox(pathlib.Path(tempfile.mkdtemp()) / "ws"),
            fetcher=UrlFetcher(resolver=FakeResolver(["93.184.216.34"]),
                               transport=FakeTransport(error=TimeoutError("late"))),
        )
        store = TaskStore(pathlib.Path(tempfile.mkdtemp()) / "t.sqlite")
        harness = Harness(
            store=store,
            registry=ToolRegistry(world.build_tools()),
            provider=MockProvider(policy="research"),
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: world.verify_observation(observation),
        )
        task = harness.create(
            task_id="task-unknown",
            objective="调研",
            principal="test",
            binding={"scenario": scenario.key},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        planner = GENERAL_DOMAIN.make_planner(world)
        task = Loop(
            harness, planner=planner, max_rounds=6,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.failure["code"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(task.failure["cause"], "FETCH_TIMEOUT")
        events = [event["type"] for event in store.events(task.id)]
        self.assertIn("tool_unknown", events)
        # One dispatch, one charge — never a second attempt.
        self.assertEqual(events.count("tool_started"), 1)
        store.close()


if __name__ == "__main__":
    unittest.main()
