"""LangGraph 编排引擎：把 :class:`~flowagent.runtime.loop.Loop` 的 while 控制流
搬到 StateGraph 上，治理层（gate/预算/回执/哈希链/计划结算）原样保留为节点内卫兵。

设计文档：fcode-langgraph-lab/DESIGN.md。要点：
- **状态 = 控制流变量**（task/plan/memory/steps/flags 的活对象）。持久状态仍在
  TaskStore（哈希链）；图状态只负责"下一节点拿什么"。checkpoint 集成是第二步。
- **事件顺序逐字节对齐**：每个节点对应原 while 体的一段，节点内代码从原实现移植，
  复用 Loop 的全部 helper（_extend_plan/_verify_result_set/_succeed/_deny/...）。
- 路由 = 原 while 的 continue/return：repairable 拒绝、batch_recover、
  completed_without_result → 回 round_head；终端路径置 terminal_task → END。

选型：``FCODE_ENGINE=langgraph`` 时由 runtime.engine.build_loop 构造本类；
默认仍是原生 Loop（零依赖保证不变）。
"""

from __future__ import annotations

import os
from typing import Any, Optional, TypedDict

from ..core.contracts import PLAN_VERSION, AgentError, Task
from ..core.plan import Plan
from ..providers.base import Proposal
from .control import Denial
from .loop import (
    MAX_PROPOSAL_REPAIRS,
    RECOVERABLE_TOOL_ERRORS,
    REPAIRABLE_DENIALS,
    Loop,
    trust_for_tool,
)
from .policy import PacketProvider

try:  # LangGraph 是可选依赖：编排引擎按需启用，仓库本体保持零第三方依赖
    from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: F401

    _HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - 无 langgraph 时本模块不可用
    _HAS_LANGGRAPH = False


class _GraphState(TypedDict, total=False):
    """控制流状态。携带活对象（Task/Plan/WorkingMemory）——持久事实在 TaskStore，
    图状态只回答"下一节点从哪继续"。"""

    task_id: str
    task: Task
    memory: Any            # WorkingMemory
    plan: Plan
    pending: Optional[dict]
    repairs: int
    round_no: int
    budget_snapshot: dict
    steps: list
    decisions: list
    rejected: bool
    batch_recover: bool
    completed_without_result: bool
    terminal_task: Optional[Task]
    done: bool


class GraphLoop(Loop):
    """Loop 的 LangGraph 编排变体。构造参数与 Loop 完全一致；
    治理行为（事件序列、预算、审批、恢复）与原生实现逐字节对齐。"""

    def build_graph(self):
        graph = StateGraph(_GraphState)
        graph.add_node("round_head", self._node_round_head)
        graph.add_node("preflight", self._node_preflight)
        graph.add_node("execute_batch", self._node_execute_batch)
        graph.add_edge(START, "round_head")
        graph.add_conditional_edges(
            "round_head",
            lambda s: END if s.get("done") else "preflight",
            {"preflight": "preflight", END: END},
        )
        graph.add_conditional_edges(
            "preflight",
            lambda s: "round_head" if s.get("rejected") else (END if s.get("done") else "execute_batch"),
            {"round_head": "round_head", "execute_batch": "execute_batch", END: END},
        )
        graph.add_conditional_edges(
            "execute_batch",
            lambda s: END if s.get("done") else "round_head",
            {"round_head": "round_head", END: END},
        )
        return graph.compile()

    def run(self, task_id: str) -> Task:
        if not _HAS_LANGGRAPH:  # pragma: no cover
            raise AgentError("ENGINE_UNAVAILABLE", "langgraph is not installed")
        task = self.harness.store.load(task_id)
        memory = self.restore(task)
        plan = self.rebuild_plan(task, memory)
        world = getattr(self.harness, "world", None)
        recall_holder = getattr(world, "recall_holder", None) if world is not None else None
        if recall_holder is not None:
            recall_holder["memory"] = memory

        app = self.build_graph()
        state: _GraphState = {
            "task_id": task_id,
            "task": task,
            "memory": memory,
            "plan": plan,
            "pending": self.pending_step(task),
            "repairs": 0,
            "done": False,
            "terminal_task": None,
        }
        # 每轮最多 3 个节点 + 余量；递归上限只限制图步数，
        # 轮数语义仍由 ROUND_LIMIT（_decisions >= max_rounds）承担。
        config = {
            "recursion_limit": max(120, self.max_rounds * 6 + 50),
            "configurable": {"thread_id": task_id},
        }
        final = app.invoke(state, config=config)
        return final.get("terminal_task") or final["task"]

    # ------------------------------------------------------------------ 节点

    def _node_round_head(self, state: _GraphState) -> _GraphState:
        """原 while 体的段 A+B：轮首控制 → 终止检查 → 观察/预算 → propose →
        additions → steps → compaction 冲刷。终止路径置 done/terminal_task。"""
        task_id = state["task_id"]
        task: Task = state["task"]
        memory = state["memory"]
        plan: Plan = state["plan"]
        pending = state.get("pending")
        updates: _GraphState = {}

        if pending is None and self.control is not None:
            controlled = self.control.before_round(
                round_no=self._decisions(task) + 1,
                task=task,
                plan=plan,
            )
            task, plan = controlled.task, controlled.plan

        if pending is None:
            if plan.is_blocked():
                updates["terminal_task"] = self._stop(task, plan, "PLAN_BLOCKED")
                updates["done"] = True
                return {**state, **updates}
            if plan.is_complete() or plan.next_open() is None:
                # 原 while 语义：域开放给模型的计划，模型仍可在本轮继续提议
                # （以 report_blocked 收尾）；封闭计划没有可做的就停。
                if not self.plan_extension:
                    updates["terminal_task"] = self._stop(task, plan, "PLAN_EXHAUSTED")
                    updates["done"] = True
                    return {**state, **updates}
            if self._decisions(task) >= self.max_rounds:
                updates["terminal_task"] = self._abort(
                    task_id, AgentError("ROUND_LIMIT", f"no verified result after {self.max_rounds} rounds"))
                updates["done"] = True
                return {**state, **updates}

        # Operator notes（插话）每轮刷新：上一轮到达的插话在本轮 prompt 里。
        memory.operator_messages = self.operator_notes(task)
        budget_snapshot = self.harness.budget(task).snapshot()
        updates["memory"] = memory
        updates["task"] = task
        updates["plan"] = plan

        if pending is not None:
            # A reconciled round: the proposal is already approved and paid
            # for. Execute it; do not ask the model again.
            steps = [pending]
            updates["pending"] = None
            round_no = task.model.attempts
            self.observer.round_start(round_no, budget_snapshot, plan)
        else:
            round_no = self._decisions(task) + 1
            self.observer.round_start(round_no, budget_snapshot, plan)
            if self.policy is not None:
                packet = self.compiler(
                    objective=task.objective,
                    round_no=round_no,
                    plan=plan,
                    memory=memory,
                    registry=self.harness.registry,
                    budget=budget_snapshot,
                )
                task = self.harness.record(task, "policy_requested", {
                    "round": round_no,
                    "packet": packet.to_dict(),
                    "packet_hash": packet.packet_hash,
                    "packet_bytes": packet.size_bytes,
                })
                if not packet.candidates:
                    updates["terminal_task"] = self._abort(task_id, AgentError(
                        "POLICY_NO_ACTIONS", "the action compiler produced no legal action",
                    ))
                    updates["done"] = True
                    return {**state, **updates}
                if len(packet.candidates) == 1:
                    candidate = packet.candidates[0]
                    step = {"tool": candidate.tool, "arguments": dict(candidate.arguments)}
                    task = self.harness.auto_step(
                        task, step, round_no=round_no,
                        candidate_id=candidate.id, packet_hash=packet.packet_hash,
                    )
                    proposal = Proposal(
                        plan={"version": PLAN_VERSION, "steps": [{
                            "id": candidate.todo_id or "stop",
                            "tool": candidate.tool,
                            "arguments": dict(candidate.arguments),
                        }]},
                        provider="host-auto",
                        model="deterministic-auto",
                        tokens=0,
                        cost_microusd=0,
                        todo_id=candidate.todo_id or None,
                        metadata={"packet_hash": packet.packet_hash, "candidate_id": candidate.id},
                    )
                else:
                    provider = PacketProvider(packet, self.policy)
                    try:
                        task, proposal = self.harness.propose_with(
                            task, {"round": round_no, "packet": packet, "state": packet.state}, provider,
                        )
                    except AgentError as error:
                        updates["terminal_task"] = self._abort(task_id, error)
                        updates["done"] = True
                        return {**state, **updates}
                    if proposal is None:
                        self.observer.terminal(task, None)
                        updates["terminal_task"] = task
                        updates["done"] = True
                        return {**state, **updates}
                    task = self.harness.record(task, "policy_decided", {
                        "round": round_no,
                        "candidate_id": proposal.metadata.get("candidate_id", ""),
                        "packet_hash": packet.packet_hash,
                        "repaired": bool(proposal.metadata.get("repaired")),
                        "tokens": proposal.tokens,
                        "latency_ms": proposal.latency_ms,
                    })
                self.observer.proposal(round_no, proposal)
                steps = self._round_steps(proposal)
            else:
                context = memory.build(round_no, budget_snapshot, plan)
                try:
                    task, proposal = self.harness.propose(task, context)
                except AgentError as error:
                    updates["terminal_task"] = self._abort(task_id, error)
                    updates["done"] = True
                    return {**state, **updates}
                if proposal is None:
                    self.observer.terminal(task, None)
                    updates["terminal_task"] = task
                    updates["done"] = True
                    return {**state, **updates}
                self.observer.proposal(round_no, proposal)
                # A proposal may ask to extend the plan. The host judges it
                # before the step runs; see _extend_plan.
                task, plan = self._extend_plan(task, plan, round_no, memory)
                steps = self._round_steps(proposal)

        if memory.pending_compaction is not None:
            task = self.harness.record(
                task, "context_compacted", dict(memory.pending_compaction)
            )
            memory.pending_compaction = None
        # P0-3 fail-open 兑现:pinned 全量自身超预算时,证据完整性优先于
        # 预算——事件如实记录溢出,不丢 pinned、不静默。
        if memory.pending_pin_overflow is not None:
            task = self.harness.record(
                task, "context_pin_overflow", dict(memory.pending_pin_overflow)
            )
            memory.pending_pin_overflow = None

        updates.update({
            "task": task, "plan": plan, "round_no": round_no,
            "steps": steps, "rejected": False, "batch_recover": False,
            "completed_without_result": False,
        })
        return {**state, **updates}

    def _node_preflight(self, state: _GraphState) -> _GraphState:
        """原"整批预检"段：schema 错误在任何派发前回绝；可修复 → 回 round_head。"""
        task: Task = state["task"]
        plan: Plan = state["plan"]
        round_no = state["round_no"]
        steps = state["steps"]
        repairs = state.get("repairs", 0)
        memory = state["memory"]

        decisions = []
        rejected = False
        terminal = None
        for step in steps:
            decision = self.harness.gate(task, step)
            self.observer.gate(round_no, decision)
            if decision.allowed:
                decisions.append(decision)
                continue
            if decision.code in REPAIRABLE_DENIALS and repairs < MAX_PROPOSAL_REPAIRS:
                # Nothing was dispatched, nothing is unknown: the proposal
                # did not fit the tool's schema. Handing the error back is
                # not a silent retry — it is one visible event and one more
                # model call, bounded, and the run still ends denied if the
                # model cannot fix it.
                repairs += 1
                task = self.harness.record(task, "proposal_rejected", {
                    "round": round_no,
                    "tool": step["tool"],
                    "code": decision.code,
                    "message": decision.reason,
                    "arguments": step.get("arguments") or {},
                    "attempt": repairs,
                })
                memory.rejected_proposals = list(memory.rejected_proposals or []) + [{
                    "tool": step["tool"], "code": decision.code, "message": decision.reason,
                }]
                rejected = True
                break
            terminal = self._deny(task, decision, plan)
            break

        updates: _GraphState = {
            "task": task, "memory": memory, "decisions": decisions,
            "rejected": rejected, "repairs": repairs,
        }
        if terminal is not None:
            updates["terminal_task"] = terminal
            updates["done"] = True
        return {**state, **updates}

    def _node_execute_batch(self, state: _GraphState) -> _GraphState:
        """原"批量执行"段：审批 → 执行 → 结算 → 观察 → 终止/完成检查。"""
        task_id = state["task_id"]
        task: Task = state["task"]
        plan: Plan = state["plan"]
        memory = state["memory"]
        round_no = state["round_no"]
        steps = state["steps"]
        decisions = state["decisions"]

        terminal = None
        completed_without_result = False
        batch_recover = False
        for step, decision in zip(steps, decisions):
            if self.control is not None:
                approval = self.control.before_tool(
                    round_no=round_no,
                    task=task,
                    step=step,
                    decision=decision,
                )
                task = approval.task
                if not approval.allowed:
                    if approval.terminated:
                        self.observer.terminal(task, None)
                        terminal = task
                        break
                    terminal = self._deny(
                        task,
                        Denial(
                            tool=step["tool"],
                            code=approval.code or "OPERATOR_DENIED",
                            reason=approval.reason or "operator denied the call",
                        ),
                        plan,
                    )
                    break

            try:
                task, observation = self.harness.execute(task, step, round_no=round_no)
            except AgentError as error:
                if self.harness.max_steps > 1 and error.code in RECOVERABLE_TOOL_ERRORS:
                    # The tool call itself was malformed (a 600-char spawn
                    # objective, a bad regex/range). The harness already
                    # committed tool_failed; hand the error back in context
                    # and let the model fix it next round instead of killing
                    # the whole run. Fast mode only: the one-action contract
                    # keeps terminal-on-tool-failure.
                    memory.rejected_proposals = list(memory.rejected_proposals or []) + [{
                        "tool": step["tool"], "code": error.code,
                        "message": error.message,
                    }]
                    # execute() committed tool_failed before raising; the
                    # loop still holds the pre-execution snapshot. Reload the
                    # durable task and clear the transient failure so the
                    # next proposal starts from the committed sequence.
                    task = self.harness.store.load(task_id)
                    task.failure = None
                    self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
                    batch_recover = True
                    break
                terminal = self._abort(task_id, error)
                break

            memory.record(round_no, observation,
                          source_trust=trust_for_tool(observation.tool))
            plan = self.planner.advance(plan, observation) if self.planner else plan
            task = self._persist_plan(task, plan, round_no)

            self.observer.observation(round_no, observation)

            if observation.tool == "report_blocked":
                self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
                terminal = self._reported_blocked(task, plan, observation)
                break
            if plan.is_blocked():
                self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
                terminal = self._stop(task, plan, "PLAN_BLOCKED")
                break
            if plan.is_complete():
                # The batch may complete the plan on a step that is not
                # itself a result tool. Verify whenever the plan *contains*
                # a result obligation, not only when the last action is one.
                if any(todo.tool in self.result_tools for todo in plan.todos):
                    try:
                        verified = self._verify_result_set(task.id, plan)
                        sources = self._check_evidence(task, verified, round_no)
                        task, obligation = self._verify_obligation(task, verified, plan, round_no)
                    except AgentError as error:
                        terminal = self._abort(task.id, error)
                        break
                    self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
                    terminal = self._succeed(task, verified, plan, obligation, sources)
                    break
                # No result obligation: stop consuming the rest of the batch
                # and let the round-head completion check end the task.
                completed_without_result = True
                break

        updates: _GraphState = {
            "task": task, "plan": plan, "memory": memory,
            "batch_recover": batch_recover,
            "completed_without_result": completed_without_result,
        }
        if terminal is not None:
            updates["terminal_task"] = terminal
            updates["done"] = True
        else:
            # continue 语义（batch_recover）已在 break 前调用过 round_end；
            # 正常批结束在这里收 round_end。
            self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
        return {**state, **updates}