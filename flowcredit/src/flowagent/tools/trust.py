"""P0-2 来源可信度映射表:显式声明,不靠启发式。

判定依据(集中于此,单一事实源):
- 注册表内、宿主确定性执行的**计算型**工具 → VERIFIED_TOOL
  (结果完整性由 Runtime 校验,输出不携带外部内容);
- 网络抓取、外部命令执行、MCP、目标文件读取 → EXTERNAL_UNTRUSTED
  (输出携带不可信内容,进 prompt 前必须隔离);
- 未在此表登记的工具 → EXTERNAL_UNTRUSTED(fail-closed:
  新工具忘记登记时,宁可过度隔离,不可泄漏)。

注意:VERIFIED_TOOL 指"结果由宿主确定性计算",不是"内容可信任"——
语义记忆的写入纪律(L3 只收重放过的)在别处另行约束。
"""

from __future__ import annotations

from ..core.observation import SourceTrust

_VERIFIED_TOOLS: frozenset[str] = frozenset({
    # finance 参考域:宿主确定性计算(输出为派生数值,无外部内容)
    "list_documents", "read_document", "normalize_units", "reconcile_basis",
    "check_restatement", "calculate_margin",
    # 内核:案例/事实/产物检索(本地 SQLite,宿主实现)
    "search_cases", "read_case", "query_facts",
    "working_paper_append", "recall_observation",
})


def trust_for_tool(tool: str) -> SourceTrust:
    """查表;未登记一律 EXTERNAL_UNTRUSTED(fail-closed)。"""
    if tool in _VERIFIED_TOOLS:
        return SourceTrust.VERIFIED_TOOL
    return SourceTrust.EXTERNAL_UNTRUSTED
