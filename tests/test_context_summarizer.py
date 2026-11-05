"""P0-1 契约测试:summarizer 策略分发与改进版压缩。

策略语义(含设计冲突的裁决,见提交说明):
- "default"  = 旧形状压缩,**字节冻结**(finance 参考负载依赖);
- "detailed" = P0-1 改进版(列表首项全量、嵌套一层、长文头尾);
- "raw"      = 仅截断保护;
- "custom"   = 必须提供 summarize,缺失即注册错误(fail-closed)。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.memory.context import (  # noqa: E402
    DEFAULT_SUMMARY_HEAD,
    DEFAULT_SUMMARY_TAIL,
    default_summarize,
    detailed_summarize,
    raw_summarize,
    summarizer_for,
)
from flowagent.tools.base import ToolSpec  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _spec(name="t", *, policy="default", summarize=None, handler=None):
    return ToolSpec(
        name=name, summary="t", effect="read", params=(),
        handler=handler or (lambda args: {"ok": True}),
        summarize=summarize, summary_policy=policy,
    )


class DetailedSummarizeTests(unittest.TestCase):
    def test_dict_list_keeps_first_item_full_and_count_for_rest(self) -> None:
        result = {"matches": [
            {"a": 1, "b": {"c": 2}}, {"a": 3}, {"a": 4}, {"a": 5},
        ]}
        out = detailed_summarize(result)["matches"]
        self.assertEqual(out[0], {"a": 1, "b": {"c": 2}}, "首项全量可见")
        self.assertEqual(out[1], {"count": 4}, "count 为总数,配合首项可推出其余 3 项")

    def test_short_list_keeps_every_item(self) -> None:
        out = detailed_summarize({"items": [{"a": 1}, {"a": 2}, {"a": 3}]})["items"]
        self.assertEqual(out, [{"a": 1}, {"a": 2}, {"a": 3}])

    def test_long_string_keeps_head_tail_and_marker(self) -> None:
        text = "A" * 500 + "SECRET-IN-MIDDLE" + "B" * 500
        out = detailed_summarize({"content": text})["content"]
        self.assertTrue(out.startswith("A" * 10), "头 200 保留")
        self.assertTrue(out.endswith("B" * 10), "尾 200 保留")
        omitted = len(text) - DEFAULT_SUMMARY_HEAD - DEFAULT_SUMMARY_TAIL
        self.assertIn(f"...[truncated {omitted} chars]...", out, "显式截断标记")
        self.assertNotIn("SECRET-IN-MIDDLE", out, "中段被截掉")
        self.assertEqual(len(out), DEFAULT_SUMMARY_HEAD + DEFAULT_SUMMARY_TAIL
                         + len(f"...[truncated {omitted} chars]..."))

    def test_string_within_head_plus_tail_is_untouched(self) -> None:
        text = "x" * (DEFAULT_SUMMARY_HEAD + DEFAULT_SUMMARY_TAIL)
        self.assertEqual(detailed_summarize({"content": text})["content"], text)


class RawPolicyTests(unittest.TestCase):
    def test_raw_only_truncates_never_structures(self) -> None:
        result = {"blob": "Z" * 5000,
                  "matches": [{"a": i} for i in range(10)]}
        out = raw_summarize(result)
        self.assertEqual(out["blob"]["chars"], 5000)
        self.assertEqual(len(out["blob"]["head"]), 4000)
        # 列表原样保留(未被结构化压缩)——raw 的语义就是不做压缩决策。
        self.assertEqual(out["matches"], result["matches"])


class DefaultPolicyBackwardCompatTests(unittest.TestCase):
    def test_default_policy_keeps_legacy_shape_for_short_content(self) -> None:
        result = {"text": "short", "score": 3, "flag": True}
        self.assertEqual(default_summarize(result),
                         {"text": "short", "score": 3, "flag": True})

    def test_default_policy_is_unchanged_for_dict_lists(self) -> None:
        # 字节冻结依赖:default 对 dict 列表的行为与改动前一致(只有 count)。
        result = {"matches": [{"a": 1}, {"a": 2}]}
        self.assertEqual(default_summarize(result)["matches"], {"count": 2})

    def test_dispatch_default_uses_tool_impl_when_present(self) -> None:
        # 优先级裁决:有实现用实现——finance 字节冻结依赖此条。
        custom = lambda result: {"custom": True}  # noqa: E731
        fn = summarizer_for(_spec(policy="default", summarize=custom))
        self.assertEqual(fn({}), {"custom": True})

    def test_dispatch_detailed_and_raw(self) -> None:
        self.assertIs(summarizer_for(_spec(policy="detailed")), detailed_summarize)
        self.assertIs(summarizer_for(_spec(policy="raw")), raw_summarize)


class RegistrationFailClosedTests(unittest.TestCase):
    def test_custom_policy_without_summarize_is_refused_at_registration(self) -> None:
        with self.assertRaises(AgentError) as caught:
            ToolRegistry([_spec(name="broken", policy="custom", summarize=None)])
        self.assertEqual(caught.exception.code, "TOOL_SUMMARY_POLICY_INVALID")

    def test_unknown_policy_is_refused_at_registration(self) -> None:
        with self.assertRaises(AgentError):
            ToolRegistry([_spec(name="odd", policy="aggressive")])

    def test_valid_policies_register_cleanly(self) -> None:
        registry = ToolRegistry([
            _spec(name="d", policy="default"),
            _spec(name="dt", policy="detailed"),
            _spec(name="r", policy="raw"),
            _spec(name="c", policy="custom", summarize=lambda r: {}),
        ])
        self.assertEqual(len(registry.names), 4)


if __name__ == "__main__":
    unittest.main()
