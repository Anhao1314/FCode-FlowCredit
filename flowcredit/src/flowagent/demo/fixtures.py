"""Scenario definitions: one story, four ways for the runtime to say no.

   身份声明(2026-11-02 降级):金融参考域是 **Runtime 自检负载**,不是产品域。
   产品是审计域(flowaudit);金融场景保留有两个理由——8 个治理场景用它
   注入故障,24 个测试文件用它当测试床。物理搬迁到 examples/ 的闸门:
   出现一个中立的测试夹具域,且 24 个测试完成迁移。在此之前,任何
   "顺手删金融"都是砸自己测试套件的行为。

Each scenario is the same kernel with a different host policy or a different
failure injected. None of them changes runtime code — that is the claim the demo
is making, so the scenarios are data, not branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime.budget import BudgetLimits

__all__ = ["Scenario", "SCENARIOS", "AVAILABLE", "get_scenario"]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    objective: str
    story: str
    allowed_tools: tuple[str, ...]
    grant: dict[str, Any]
    limits: BudgetLimits
    max_rounds: int
    #: Which domain module assembles this scenario's world, tools and verifier.
    #: Explicit on every scenario: a silent default would bind a new scenario
    #: to the reference workload without anyone deciding it.
    module: str
    policy: str = "research"
    fail_at_call: int | None = None
    reconcile: bool = False
    crash_at: str | None = None
    crash_at_count: int = 1
    partial_corpus: bool = False
    expected_status: str = "succeeded"
    available: bool = True
    #: Tools whose consequences are large enough that the console asks a human
    #: before executing them. Data, not runtime policy: a different domain can
    #: name a different set without touching the loop.
    approval_tools: tuple[str, ...] = ("reconcile_basis", "report_blocked", "working_paper_append")


_ALL_TOOLS = (
    "list_documents",
    "read_document",
    "normalize_units",
    "reconcile_basis",
    "check_restatement",
    "calculate_margin",
    "report_blocked",
    "ingest_file",
    "search_corpus",
    "read_slice",
    "working_paper_append",
    "search_cases",
    "read_case",
    "query_facts",
    "collect_source",
    "fetch_source",
)

_OBJECTIVE = "判断恒星智算 2024 与 2025 两期合并毛利率是否可比，并在可比时给出变化"
_STORY = (
    "两年年报放在一起并不能直接相减：2024 年用万元、2025 年用百万元；"
    "2025 年还发布了一份调整后口径的补充材料；2024 年的毛利后来被一份重述说明改过。"
)


def _grant(max_calls: int = 12, max_reads: int = 20) -> dict[str, Any]:
    return {
        "grant_id": "demo-host-grant",
        "revision": 1,
        "allowed_tools": list(_ALL_TOOLS),
        "max_calls": max_calls,
        "max_reads": max_reads,
        "max_cost_microusd": 20_000,
        "max_tokens": 20_000,
    }


SCENARIOS: dict[str, Scenario] = {
    "full": Scenario(
        key="full",
        title="完整研究：两期毛利率是否可比",
        objective=_OBJECTIVE,
        story=_STORY + "正确的结论必须同时穿过这三个坑。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(),
        limits=BudgetLimits(12, 20, 20_000, 20_000),
        max_rounds=12,
        module="finance-research",
    ),
    "tool-denied": Scenario(
        key="tool-denied",
        title="越权工具：主机拒绝，模型与工具都未执行",
        objective=_OBJECTIVE,
        story="模型一上来就提出一个从未注册的工具 write_document。名字合法、schema 合法，但主机不认。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(),
        limits=BudgetLimits(12, 20, 20_000, 20_000),
        # A real provider may decline the injected off-tool more than once
        # before the host-gate evidence is conclusive. Six rounds leaves room
        # for that without pretending a round-limit refusal is a gate denial.
        max_rounds=6,
        module="finance-research",
        policy="escalate",
        expected_status="denied",
    ),
    "budget": Scenario(
        key="budget",
        title="预算耗尽：发送前被拒绝，已占用不退",
        objective=_OBJECTIVE,
        story="模型反复请求同一份资料，第 5 次调用在发出之前就被预算拦住。前 4 次的额度真实占用。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(max_calls=4),
        limits=BudgetLimits(4, 20, 20_000, 20_000),
        max_rounds=10,
        module="finance-research",
        policy="loop",
        expected_status="failed",
    ),
    "budget-resume": Scenario(
        key="budget-resume",
        title="预算追加：失败后续跑，已付的读取不重发",
        objective=_OBJECTIVE,
        story="前 6 次调用用完了额度，任务停在第七轮之前。追加预算后从事件日志续跑，已读的资料不会重发。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(max_calls=6),
        limits=BudgetLimits(6, 20, 20_000, 20_000),
        max_rounds=12,
        module="finance-research",
        expected_status="failed",
    ),
    "crash": Scenario(
        key="crash",
        title="崩溃恢复：进程被杀，续跑不重复",
        objective=_OBJECTIVE,
        story="第三轮工具执行落盘后进程被 SIGKILL。重启后从事件日志重建记忆，第四轮继续，前三次调用不重发。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(),
        limits=BudgetLimits(12, 20, 20_000, 20_000),
        max_rounds=12,
        module="finance-research",
        crash_at="tool_succeeded",
        crash_at_count=3,
    ),
    "unknown": Scenario(
        key="unknown",
        title="未知结果：不重发，凭签名收据对账",
        objective=_OBJECTIVE,
        story="第三轮调用发出后供应商超时。Runtime 记 unknown，不重发、不退费；只有匹配的签名收据能把它拉回来。",
        allowed_tools=_ALL_TOOLS,
        grant=_grant(),
        limits=BudgetLimits(12, 20, 20_000, 20_000),
        max_rounds=12,
        module="finance-research",
        fail_at_call=3,
        reconcile=True,
    ),
    "open-ended": Scenario(
        key="open-ended",
        title="目标无法完成：计划被卡住，诚实停止",
        objective=_OBJECTIVE,
        story=(
            "这家公司同样公布了调整后口径的补充材料，但没有公布桥接说明。"
            "调整后的毛利无法还原成 IFRS，两期口径就此对不上。"
            "Agent 必须停在这里报告，而不是拿一个算不动的数字出来。"
        ),
        allowed_tools=_ALL_TOOLS,
        grant=_grant(),
        limits=BudgetLimits(12, 20, 20_000, 20_000),
        max_rounds=12,
        module="finance-research",
        partial_corpus=True,
        expected_status="failed",
    ),
    "general-research": Scenario(
        key="general-research",
        title="通用委托：抓取公开来源，写入 research.md",
        objective="调研指定的公开主题，把来源摘录与结论写成 research.md 放进工作区",
        story=(
            "通用能力的参考实现：模型提议 fetch_url 抓取操作者圈定的公开来源，"
            "再提议 write_file 把研究底稿写进沙箱。写入默认过人工审批（真实 diff），"
            "网页内容如实标注 not-verified；runtime 复核的是文件哈希、预算与链。"
        ),
        allowed_tools=(
            "ls_workspace",
            "read_file",
            "write_file",
            "edit_file",
            "run_command",
            "fetch_url",
            # Asking the operator is a host-granted capability like any other:
            # a general delegation with no way to ask has to guess.
            "ask_operator",
            # The honest-stop declaration. Without it in scope *and* grant the
            # gate refuses the very action the shared prompt tells the model to
            # take when the evidence runs out, and the run ends `denied`
            # instead of stopping honestly.
            "report_blocked",
        ),
        grant={
            "grant_id": "general-host-grant",
            "revision": 1,
            "allowed_tools": [
                "ls_workspace",
                "read_file",
                "write_file",
                "edit_file",
                "run_command",
                "fetch_url",
                "ask_operator",
                "report_blocked",
            ],
            # The host names the effect classes this delegation may use. A
            # finance grant omits the key and keeps the read-only promise.
            "allowed_effects": ["read", "write", "network"],
            "max_calls": 12,
            "max_reads": 20,
            # Kimi K2.6 reserves 6,000 prompt + 8,192 completion tokens per
            # call. The default must fit at least one real paid reservation;
            # explicit operator overrides can still tighten these ceilings.
            "max_cost_microusd": 500_000,
            "max_tokens": 120_000,
        },
        limits=BudgetLimits(12, 20, 500_000, 120_000),
        max_rounds=12,
        approval_tools=("write_file", "edit_file", "run_command"),
        module="general-work",
    ),
}

AVAILABLE = tuple(key for key, scenario in SCENARIOS.items() if scenario.available)


def get_scenario(key: str) -> Scenario:
    """Return a scenario by key, raising ``KeyError`` for unknown keys."""
    scenario = SCENARIOS.get(key)
    if scenario is None:
        raise KeyError(key)
    return scenario
