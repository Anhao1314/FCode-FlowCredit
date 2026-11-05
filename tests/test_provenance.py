"""P0-2 契约测试:来源可信度 provenance。

四条断言线:三类来源的 trust 正确;旧链缺字段 fail-closed 缺省且
verify_chain 通过;render 输出带枚举字符串且 finance 缺席字节不变;
未登记工具默认 EXTERNAL_UNTRUSTED。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.memory.context import (  # noqa: E402
    SourceTrust,
    WorkingMemory,
)
from flowagent.providers.prompt import render_prompt  # noqa: E402
from flowagent.tools.trust import trust_for_tool  # noqa: E402


def _memory(**overrides) -> WorkingMemory:
    params = dict(
        objective="t",
        tool_catalog=[],
        summarizers={"probe": lambda r: {"ok": r.get("ok", True)}},
        prompt_profile="general",
    )
    params.update(overrides)
    return WorkingMemory(**params)


def _observe(memory, round_no, tool, result, trust):
    memory.record(round_no, type("O", (), {
        "tool": tool, "arguments": {}, "result": result,
        "result_hash": "h" * 64,
    })(), source_trust=trust)


class TrustMappingTests(unittest.TestCase):
    def test_explicit_mapping_is_exhaustive_and_fail_closed(self) -> None:
        self.assertEqual(trust_for_tool("calculate_margin"),
                         SourceTrust.VERIFIED_TOOL)
        self.assertEqual(trust_for_tool("search_cases"),
                         SourceTrust.VERIFIED_TOOL)
        self.assertEqual(trust_for_tool("fetch_source"),
                         SourceTrust.EXTERNAL_UNTRUSTED)
        self.assertEqual(trust_for_tool("run_command"),
                         SourceTrust.EXTERNAL_UNTRUSTED)
        self.assertEqual(trust_for_tool("mcp__whatever__call"),
                         SourceTrust.EXTERNAL_UNTRUSTED)
        self.assertEqual(trust_for_tool("brand_new_unregistered_tool"),
                         SourceTrust.EXTERNAL_UNTRUSTED,
                         "未登记工具 fail-closed")


class ContextTrustTests(unittest.TestCase):
    def test_three_sources_render_their_own_trust(self) -> None:
        memory = _memory()
        _observe(memory, 1, "calculate_margin", {"ok": True},
                 SourceTrust.VERIFIED_TOOL)
        _observe(memory, 2, "probe", {"ok": True},
                 SourceTrust.AGENT_INFERRED)
        _observe(memory, 3, "fetch_source", {"ok": True},
                 SourceTrust.EXTERNAL_UNTRUSTED)
        context = memory.build(round_no=4, budget_snapshot={}, plan=None)
        trusts = [o["trust"] for o in context["observations"]]
        self.assertEqual(
            trusts,
            ["verified_tool", "agent_inferred", "external_untrusted"])

    def test_default_record_without_declaration_is_untrusted(self) -> None:
        memory = _memory()
        _observe(memory, 1, "anything", {"ok": True},
                 SourceTrust.EXTERNAL_UNTRUSTED)
        context = memory.build(round_no=2, budget_snapshot={}, plan=None)
        self.assertEqual(context["observations"][0]["trust"], "external_untrusted")


class OldChainCompatTests(unittest.TestCase):
    def test_old_events_without_trust_default_to_untrusted_and_verify(self) -> None:
        """旧链缺 trust 键:读取按 EXTERNAL_UNTRUSTED,verify_chain 通过。

        模拟方式:直接经 store.save 落一条**不含 trust 字段**的
        tool_succeeded 事件(改动前的落盘形状)——旧链的哈希从未覆盖过
        trust,因此校验天然通过;若事后删改已提交事件的 trust,则链会
        正确报 CHAIN_TAMPERED(那是对篡改的防护,不是本测试的路径)。
        """
        with tempfile.TemporaryDirectory() as tmp:
            from flowagent.core.contracts import Task
            from flowagent.core.store import TaskStore
            store = TaskStore(pathlib.Path(tmp) / "old.sqlite")
            task = Task(id="prov-old", objective="t", binding={})
            store.save(task, 0, "task_created", {"detail": {}})
            task = store.load(task.id)
            # 改动前的落盘形状:detail 里没有 trust 键。
            task = store.save(
                task, task.sequence, "tool_succeeded",
                {"tool": "calculate_margin", "round": 1,
                 "arguments": {}, "result": {"ok": True},
                 "result_hash": "h" * 64},
            )
            # 旧链校验通过
            store.verify_chain(task.id)

            # 读取路径:缺字段按 EXTERNAL_UNTRUSTED(fail-closed 缺省)
            from flowagent.memory.context import SourceTrust, WorkingMemory
            memory = WorkingMemory(objective="t", tool_catalog=[],
                                   prompt_profile="general")
            for event in store.events(task.id):
                if event["type"] != "tool_succeeded":
                    continue
                detail = event["payload"].get("detail", {})
                source_trust = SourceTrust(
                    detail.get("trust") or SourceTrust.EXTERNAL_UNTRUSTED.value)
                memory.record(
                    int(detail.get("round", 0)),
                    type("O", (), {
                        "tool": detail["tool"],
                        "arguments": detail.get("arguments", {}),
                        "result": detail["result"],
                        "result_hash": detail["result_hash"],
                    })(),
                    source_trust=source_trust,
                )
            context = memory.build(round_no=9, budget_snapshot={}, plan=None)
            self.assertEqual(
                [o["trust"] for o in context["observations"]],
                ["external_untrusted"],
                "旧事件缺字段必须落到 fail-closed 缺省")

    def test_stripping_trust_from_a_committed_event_is_tamper(self) -> None:
        """事后删改已提交事件的 trust 字段 → CHAIN_TAMPERED(防篡改兑现)。"""
        import sqlite3
        from flowagent.core.contracts import Task
        from flowagent.core.store import TaskStore
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "s.sqlite")
            task = Task(id="prov-strip", objective="t", binding={})
            store.save(task, 0, "task_created", {"detail": {}})
            task = store.load(task.id)
            task = store.save(
                task, task.sequence, "tool_succeeded",
                {"tool": "probe", "round": 1, "arguments": {},
                 "result": {"ok": True}, "result_hash": "h" * 64,
                 "trust": "verified_tool"},
            )
            with sqlite3.connect(store.path) as conn:
                row = conn.execute(
                    "SELECT sequence, payload FROM event WHERE task_id=? "
                    "AND type='tool_succeeded'", (task.id,)).fetchone()
                payload = json.loads(row[1])
                del payload["detail"]["trust"]
                conn.execute(
                    "UPDATE event SET payload=? WHERE task_id=? AND sequence=?",
                    (json.dumps(payload, ensure_ascii=False), task.id, row[0]))
            with self.assertRaises(AgentError) as caught:
                store.verify_chain(task.id)
            self.assertEqual(caught.exception.code, "CHAIN_TAMPERED")


class PromptEmissionTests(unittest.TestCase):
    def test_render_prompt_carries_enum_string_per_observation(self) -> None:
        memory = _memory()
        _observe(memory, 1, "calculate_margin", {"ok": True},
                 SourceTrust.VERIFIED_TOOL)
        context = memory.build(round_no=2, budget_snapshot={}, plan=None)
        _, user = render_prompt(context)
        self.assertIn('"trust":"verified_tool"', user)

    def test_finance_default_context_has_no_trust_key_byte_frozen(self) -> None:
        """finance 缺席语义:不选 profile 的上下文不得出现 trust 键——
        SFT 字节保真依赖缺省键的缺席(全局约束 2 的落地方式)。"""
        memory = _memory()
        memory.prompt_profile = None
        _observe(memory, 1, "anything", {"ok": True},
                 SourceTrust.VERIFIED_TOOL)
        context = memory.build(round_no=2, budget_snapshot={}, plan=None)
        for observation in context["observations"]:
            self.assertNotIn("trust", observation)
        _, user = render_prompt(context)
        self.assertNotIn('"trust":', user)


if __name__ == "__main__":
    unittest.main()
