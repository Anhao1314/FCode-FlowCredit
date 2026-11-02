"""P0-1 收尾验收:workspace 族与 MCP 动态工具显式声明 detailed 策略。

背景:两者均为默认策略且无自定义压缩器;default 会把 dict 列表压成
count、嵌套压成 keys、长文截 240 字头——证据对模型不可见。本测试在
实现前先落地断言(红),改动后转绿。

不触碰:finance 工具(有 custom summarize,分发优先级=有实现先用实现)。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.memory.context import detailed_summarize, summarizer_for  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import default_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.mcp.bridge import McpBridge  # noqa: E402
from flowagent.tools.mcp.config import McpServerConfig  # noqa: E402
from flowagent.tools.workspace import build_workspace_tools  # noqa: E402

WORKSPACE_TOOL_NAMES = (
    "ingest_file", "search_corpus", "read_slice", "working_paper_append",
    "search_cases", "read_case", "query_facts", "collect_source",
    "fetch_source",
)


def _workspace_specs():
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    specs = build_workspace_tools(EvidenceStore(root), ArtifactStore(root / "artifacts"),
                                  default_registry())
    return tmp, specs


class WorkspacePolicyTests(unittest.TestCase):
    def test_all_workspace_tools_declare_detailed(self) -> None:
        tmp, specs = _workspace_specs()
        with tmp:
            by_name = {spec.name: spec for spec in specs}
            self.assertEqual(tuple(by_name), WORKSPACE_TOOL_NAMES)
            for name, spec in by_name.items():
                self.assertEqual(
                    spec.summary_policy, "detailed",
                    f"{name} 必须显式声明 detailed,否则 default 会压成 count/keys/240 字头")
                self.assertIsNone(spec.summarize,
                                  "本次不给 workspace 工具写 custom summarizer")

    def test_dispatch_resolves_to_detailed_summarize(self) -> None:
        tmp, specs = _workspace_specs()
        with tmp:
            for spec in specs:
                self.assertIs(summarizer_for(spec), detailed_summarize,
                              f"{spec.name} 的分发必须落到 detailed_summarize")


class McpPolicyTests(unittest.TestCase):
    def test_imported_mcp_tools_declare_detailed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 不启动服务器、不解析配置文件:只验证 spec 构造的策略字段。
            bridge = McpBridge.__new__(McpBridge)
            bridge._imported, bridge._clients, bridge._cwd = {}, {}, tmp
            bridge._configs, bridge._status, bridge._refused = {}, {}, {}
            config = McpServerConfig(name="fake", command="true")
            tools = [{
                "name": "echo",
                "description": "echo tool",
                "inputSchema": {"type": "object", "properties": {}},
            }]
            imported, refused = bridge._import_server(config, None, tools)
            self.assertEqual(imported, ["mcp__fake__echo"])
            self.assertEqual(refused, [])
            spec = bridge._imported["mcp__fake__echo"].spec
            self.assertEqual(spec.summary_policy, "detailed",
                             "MCP 动态工具必须有损压缩降级为 detailed")
            self.assertIs(summarizer_for(spec), detailed_summarize)


if __name__ == "__main__":
    unittest.main()
