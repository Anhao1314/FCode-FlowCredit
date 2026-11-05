"""Terminal rendering: ANSI only, no dependencies, no build step.

The renderer is an observer too. It prints what the runtime did; it never decides
anything. Colours are disabled automatically when stdout is not a TTY or when
``NO_COLOR`` is set, so piping the demo into a file produces clean text.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from ..core.contracts import Task
from ..providers.base import Proposal
from ..runtime.permissions import GateDecision
from ..tools.base import Observation

__all__ = ["Renderer"]


class Renderer:
    """Human-readable narration of one run."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    def __init__(self, *, fast: bool = False, stream: Any = None, quiet: bool = False) -> None:
        self.fast = fast
        self.quiet = quiet
        self.stream = stream or sys.stdout
        self.use_color = self._detect_color()

    @staticmethod
    def _detect_color() -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        return bool(getattr(sys.stdout, "isatty", lambda: False)())

    # ------------------------------------------------------------------ output

    def _p(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{self.RESET}" if self.use_color else text

    def pause(self, seconds: float = 0.35) -> None:
        """Slow the demo down so a human can follow it, unless --fast."""
        if self.fast:
            return
        import time

        time.sleep(seconds)

    # ------------------------------------------------------------------ frames

    def header(
        self,
        scenario_title: str,
        objective: str,
        story: str,
        model: str,
        budget: dict[str, Any],
        *,
        provider_note: str = "(离线确定性 Mock，无网络、无密钥、无付费调用)",
    ) -> None:
        if self.quiet:
            return
        line = "─" * 72
        self._p()
        self._p(self._c(self.BOLD, line))
        self._p(self._c(self.BOLD, f" {scenario_title}"))
        self._p(self._c(self.DIM, line))
        self._p(f" 目标   {objective}")
        self._p(f" 背景   {story}")
        self._p(f" 模型   {self._c(self.YELLOW, model)}  {self._c(self.DIM, provider_note)}")
        self._p(f" 预算   {self._budget_text(budget)}")
        self._p(self._c(self.DIM, line))

    def round_start(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        if self.quiet:
            return
        self._p()
        self._p(self._c(self.CYAN, f"第 {round_no} 轮") + self._c(self.DIM, f"   预算 {self._budget_text(budget)}"))

    def proposal(self, round_no: int, proposal: Proposal) -> None:
        if self.quiet:
            return
        step = proposal.plan["steps"][0]
        arguments = step.get("arguments") or {}
        args = ", ".join(f"{key}={value}" for key, value in arguments.items()) or "无参数"
        self._p(f"  模型提议  {self._c(self.BOLD, step['tool'])}  {self._c(self.DIM, args)}")

    def gate(self, round_no: int, decision: GateDecision) -> None:
        if self.quiet:
            return
        if decision.allowed:
            self._p(f"  主机门控  {self._c(self.GREEN, 'PASS')}  {self._c(self.DIM, decision.reason)}")
        else:
            self._p(f"  主机门控  {self._c(self.RED, 'DENIED')}  {decision.code}  {self._c(self.DIM, decision.reason)}")

    def observation(self, round_no: int, observation: Observation) -> None:
        if self.quiet:
            return
        for line in self._describe(observation):
            self._p(f"  工具返回  {line}")

    def round_end(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        if self.quiet:
            return
        self._p(self._c(self.DIM, f"  预算扣减  {self._budget_text(budget)}"))

    def event(self, tag: str, message: str, *, colour: str | None = None) -> None:
        """Print a security-relevant event on its own highlighted line."""
        if self.quiet:
            return
        code = colour or self.YELLOW
        self._p(f"  {self._c(code + self.BOLD, tag):<24} {message}")

    def terminal(self, task: Task, result: dict[str, Any] | None) -> None:
        if self.quiet:
            return
        line = "─" * 72
        self._p()
        self._p(self._c(self.BOLD, line))
        if task.status == "succeeded" and result:
            self._p(self._c(self.GREEN + self.BOLD, " 终态  SUCCEEDED（Runtime 独立重算通过）"))
            if "margin_bp_2024" in result:
                self._p(f"  2024 毛利率  {self._pct(result['margin_bp_2024'])}")
                self._p(f"  2025 毛利率  {self._pct(result['margin_bp_2025'])}")
                change = result["change_bp"]
                colour = self.RED if change < 0 else self.GREEN
                self._p(f"  变化        {self._c(colour, self._pp(change))}")
                self._p(f"  口径        {result['basis']} / {result['consolidation']} / 已应用 2024 重述")
                self._p("  引用来源")
                for citation in result.get("citations", []):
                    self._p(
                        f"    - {citation['period']}  {citation['doc_id']}  毛利 {citation['value_cny']:,} 元"
                    )
            else:
                # The result shape belongs to the domain module, not the
                # renderer: print the verified entries unchanged.
                self._p(f"  复核方式    {result.get('verification', 'runtime-recomputed')}")
                entries = result.get("results")
                if not isinstance(entries, list):
                    entries = [result]
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    label = str(entry.get("path") or entry.get("tool") or "结果")
                    details = " · ".join(
                        f"{key}={value}"
                        for key, value in entry.items()
                        if key not in ("path", "content_truth", "verification", "verified")
                        and isinstance(value, (str, int, bool))
                    )
                    self._p(f"  {label:<16} {details}")
                    if entry.get("content_truth"):
                        self._p(f"  {'':<16} {entry['content_truth']}")
        elif task.status == "denied":
            failure = task.failure or {}
            self._p(self._c(self.RED + self.BOLD, " 终态  DENIED（主机拒绝，工具未执行）"))
            self._p(f"  原因  {failure.get('code')}  {failure.get('message', '')}")
        else:
            failure = task.failure or {}
            self._p(self._c(self.YELLOW + self.BOLD, f" 终态  {task.status.upper()}"))
            self._p(f"  原因  {failure.get('code')}  {failure.get('message', '')}")
        self._p(self._c(self.BOLD, line))

    def footer(self, trace_path: str, budget: dict[str, Any], rounds: int) -> None:
        if self.quiet:
            return
        self._p(f" 轮次 {rounds}   预算 {self._budget_text(budget)}")
        self._p(f" Trace {trace_path}")
        self._p(self._c(self.DIM, " 可视化：python demo.py <scenario> --open（本地起服务并打开），或 python demo.py --serve 后把该 JSON 拖进页面"))
        self._p()

    # ------------------------------------------------------------- formatting

    def _budget_text(self, budget: dict[str, Any]) -> str:
        limits = budget.get("limits", {})
        return (
            f"calls {budget.get('calls_used', 0)}/{limits.get('max_calls', '?')}  "
            f"reads {budget.get('reads_used', 0)}/{limits.get('max_reads', '?')}  "
            f"cost {budget.get('cost_used_microusd', 0)}/{limits.get('max_cost_microusd', '?')} µUSD"
        )

    @staticmethod
    def _pct(basis_points: int) -> str:
        return f"{basis_points / 100:.2f}%"

    @staticmethod
    def _pp(basis_points: int) -> str:
        return f"{basis_points / 100:+.2f} pp"

    def _describe(self, observation: Observation) -> list[str]:
        """Turn one tool result into a line a non-specialist can read."""
        tool, result = observation.tool, observation.result
        if tool == "list_documents":
            docs = result.get("documents", [])
            return [f"共 {len(docs)} 份资料：" + "、".join(f"{d['id']}({d['unit_label']}/{d['basis']})" for d in docs)]
        if tool == "read_document":
            return [
                f"{result['id']}  {result['period']}  {result['unit_label']}  口径 {result['basis']}",
                f"{self._c(self.DIM, result['body'][:56] + '…')}",
            ]
        if tool == "normalize_units":
            return [f"{result['id']}  {result['source_unit']} → CNY  ×{result['factor']:,}  毛利 {result['gross_profit_cny']:,} 元"]
        if tool == "reconcile_basis":
            if result.get("reconciled") is False:
                return [f"{result['period']}  无法桥接：{result.get('reason', '没有公布的桥接说明')}"]
            return [
                f"{result['period']}  {result['from_basis']} → {result['to_basis']}  差异 {result['delta']:+d} 百万元",
                f"{self._c(self.DIM, result['reason'])}",
            ]
        if tool == "check_restatement":
            if result.get("restated"):
                return [
                    f"{result['period']} 已重述：{result['original_gross_profit']} → {result['restated_gross_profit']} 万元"
                    f"（{result['delta']:+d}）"
                ]
            return [f"{result['period']} 无重述"]
        if tool == "calculate_margin":
            if result.get("computed"):
                return [f"已计算：2024 {self._pct(result['margin_bp_2024'])}，2025 {self._pct(result['margin_bp_2025'])}"]
            return ["无法计算：" + "；".join(result.get("blockers", []))]
        if tool == "report_blocked":
            return [f"声明受阻：{result.get('reason')}"]
        return [str(result)]
