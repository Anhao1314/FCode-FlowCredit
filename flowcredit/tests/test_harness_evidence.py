"""M1 证据运行时：每次派发必然产出链式回执，任何结果都带证据链头。

契约钉死的是护城河的运行时不变量：
1. 四种派发结果（执行/拒绝/参数非法/未知工具）各产一条回执——
   "执行了但没有回执"在这一层不可表示；
2. 回执链 prev 绑定 + canonical sha256，篡改任意一条即可被
   verify_evidence_chain 证伪；
3. world 文件事实只来自 ToolResult.data["evidence"]（工具自己声明），
   内核不发明它没观测到的世界状态；
4. 结果对象带 evidence_head；子代理与 proposal 元数据自动继承。

全离线：脚本化 provider，无网络、无模型。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    CheckDecision,
    Done,
    EvidenceChain,
    HarnessCore,
    StreamUsage,
    SubagentRunner,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
    verify_evidence_chain,
)


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)

    def stream(self, _request):
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


def echo_tool(**overrides) -> ToolSpec:
    def handler(args, _context):
        data = {"evidence": args.get("evidence")} if args.get("evidence") else None
        return ToolResult.ok(f"echo:{args.get('text', '')}", data=data)

    return ToolSpec(
        name="echo",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "evidence": {"type": "object"},
            },
        },
        handler=handler,
        **overrides,
    )


def run_core(provider, *, tools=None, checker=None):
    core = HarnessCore(provider, ToolRegistry(tools or [echo_tool()]),
                       checker=checker)
    result = core.run([{"role": "user", "content": []}], system="s")
    return result, core


def one_tool_call(*, args=None, name="echo"):
    return [
        ToolUse("t1", name, args if args is not None else {"text": "x"}),
        Done(stop_reason="tool_use"),
    ]


class ReceiptEmissionTests(unittest.TestCase):
    def test_executed_call_emits_one_receipt(self) -> None:
        result, core = run_core(ScriptedProvider(one_tool_call()))
        self.assertEqual(len(result.evidence_receipts), 1)
        receipt = result.evidence_receipts[0]
        self.assertEqual(receipt["tool"], "echo")
        self.assertEqual(receipt["seq"], 1)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["code"], "")
        self.assertIsInstance(receipt["duration_ms"], int)
        self.assertEqual(receipt["prev_sha256"], "0" * 64)
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertEqual(result.evidence_head, receipt["sha256"])
        # world 默认为空：内核不发明它没观测到的状态
        self.assertEqual(receipt["world"], [])

    def test_every_outcome_gets_a_receipt(self) -> None:
        # 拒绝
        result, _ = run_core(
            ScriptedProvider(one_tool_call()),
            checker=lambda *_: CheckDecision(allow=False, reason="no"),
        )
        self.assertEqual(len(result.evidence_receipts), 1)
        self.assertFalse(result.evidence_receipts[0]["ok"])
        self.assertEqual(result.evidence_receipts[0]["code"], "TOOL_DENIED")

        # 参数非法
        result, _ = run_core(ScriptedProvider(one_tool_call(args={"text": 1})))
        self.assertEqual(result.evidence_receipts[0]["code"], "TOOL_INPUT_INVALID")

        # 未知工具
        result, _ = run_core(ScriptedProvider(one_tool_call(name="nope")))
        self.assertEqual(result.evidence_receipts[0]["code"], "TOOL_UNKNOWN")

        # 执行中异常也是回执，不是丢失
        def broken(args, _context):
            raise RuntimeError("boom")

        tool = ToolSpec(name="echo", description="x",
                        input_schema={"type": "object", "properties": {}},
                        handler=broken)
        result, _ = run_core(ScriptedProvider(one_tool_call(args={})), tools=[tool])
        self.assertEqual(result.evidence_receipts[0]["code"], "TOOL_FAILED")

    def test_receipts_are_chain_linked_across_calls(self) -> None:
        provider = ScriptedProvider(
            one_tool_call(args={"text": "a"}), one_tool_call(args={"text": "b"}),
            [TextDelta("done"), Done()],
        )
        result, core = run_core(provider)
        receipts = result.evidence_receipts
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[1]["prev_sha256"], receipts[0]["sha256"])
        self.assertEqual(result.evidence_head, receipts[1]["sha256"])
        ok, reason = verify_evidence_chain(receipts)
        self.assertTrue(ok, reason)

    def test_tool_declared_world_evidence_is_bound(self) -> None:
        evidence = {"work/index.html": "a" * 64}
        result, _ = run_core(
            ScriptedProvider(one_tool_call(args={"text": "x", "evidence": evidence})))
        receipt = result.evidence_receipts[0]
        self.assertEqual(receipt["world"],
                         [{"path": "work/index.html", "sha256": "a" * 64}])

    def test_tampered_receipt_is_proven_broken(self) -> None:
        result, _ = run_core(ScriptedProvider(one_tool_call()))
        receipts = [dict(r) for r in result.evidence_receipts]
        receipts[0]["code"] = "TAMPERED"
        ok, reason = verify_evidence_chain(receipts)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", reason)

    def test_verify_rejects_seq_and_prev_breaks(self) -> None:
        result, _ = run_core(
            ScriptedProvider(one_tool_call(), one_tool_call(),
                             [TextDelta("d"), Done()]))
        receipts = result.evidence_receipts
        # seq 断裂
        dropped = [dict(receipts[1])]
        dropped[0]["seq"] = 2
        ok, reason = verify_evidence_chain(dropped)
        self.assertFalse(ok)
        self.assertIn("seq mismatch", reason)
        # prev 断裂
        edited = [dict(receipts[0]), dict(receipts[1])]
        edited[1]["prev_sha256"] = "f" * 64
        ok, reason = verify_evidence_chain(edited)
        self.assertFalse(ok)
        self.assertIn("prev link broken", reason)

    def test_receipts_are_chain_safe_json(self) -> None:
        result, _ = run_core(ScriptedProvider(one_tool_call()))
        encoded = json.dumps(result.evidence_receipts)
        self.assertIn("echo", encoded)
        for receipt in result.evidence_receipts:
            self.assertIsInstance(receipt["duration_ms"], int)


class InheritanceTests(unittest.TestCase):
    def test_subagent_carries_evidence_head(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "x"}), Done(stop_reason="tool_use")],
            [TextDelta("child done"), Done(usage=StreamUsage(2, 1))],
        )
        runner = SubagentRunner(
            provider, ToolRegistry([echo_tool()]),
            type("R", (), {"max_depth": 1})(),
        )
        # registry.get 需要返回 definition；用最小桩
        class Defn:
            agent_type = "worker"
            tools = ("echo",)
            model = ""
            max_turns = 4
            system = ""

        class Registry:
            max_depth = 1
            known = staticmethod(lambda t: True)

            def get(self, agent_type):
                return Defn() if agent_type == "worker" else None

            def names(self):
                return ["worker"]

        runner.registry = Registry()
        result = runner.spawn("worker", "do it")
        self.assertTrue(result.completed)
        self.assertNotEqual(result.evidence_head, "")
        self.assertNotEqual(result.evidence_head, "0" * 64)
        self.assertIn("evidence_head", result.to_dict())

    def test_text_only_run_has_zero_head(self) -> None:
        result, _ = run_core(ScriptedProvider([TextDelta("hi"), Done()]))
        self.assertEqual(result.evidence_head, "0" * 64)
        self.assertEqual(result.evidence_receipts, [])


if __name__ == "__main__":
    unittest.main()
