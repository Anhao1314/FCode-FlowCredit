"""LangGraph 编排引擎：把 :class:`~flowagent.runtime.loop.Loop` 的 while 控制流
搬到 StateGraph 上，治理层（gate/预算/回执/哈希链/计划结算）原样保留为节点内卫兵。

设计文档：fcode-langgraph-lab/DESIGN.md。本版为第二步：
- **逐步执行**：每轮的 step 一个一个过图（dispatch → approve → execute），
  interrupt() 的"节点重放"因此永远只落在无副作用的审批节点上——不会把
  已执行的 step 1..k-1 再执行一遍（at-least-once 问题的解法）。
- **interrupt() 审批**：RunControl 拆出 prepare/apply 两段（无等待），
  审批挂起 = 图暂停（`__interrupt__`），操作员决策 = Command(resume)。
  同步 wait 语义保留在原生引擎的 before_tool 里，事件逐字节对齐。
- **状态 = 控制流原语**（done/step_index/flags）。Task/Plan/WorkingMemory
  留在实例属性上——持久事实在 TaskStore，图只回答"下一节点从哪继续"；
  MemorySaver 检查点因此只序列化原始类型（跨进程 SqliteSaver 留第三步，
  它需要先解决"陈旧对象 vs 事件链重建"的协调问题）。

选型：``FCODE_ENGINE=langgraph`` 时由 runtime.engine.build_loop 构造本类；
默认仍是原生 Loop（零依赖保证不变）。
"""

from __future__ import annotations

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
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    _HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - 无 langgraph 时本模块不可用
    _HAS_LANGGRAPH = False


class _GraphState(TypedDict, total=False):
    """控制流状态：只放原始类型。详见模块 docstring。"""

    done: bool
    rejected: bool
    batch_recover: bool
    batch_done: bool
    completed_without_result: bool
    round_no: int
    repairs: int
    step_index: int
    steps: list
    decisions: list
    approval_payload: Optional[dict]
    operator_response: Optional[dict]


class GraphLoop(Loop):
    """Loop 的 LangGraph 编排变体。构造参数与 Loop 完全一致；
    治理行为（事件序列、预算、审批、恢复）与原生实现逐字节对齐。
    审批挂起：run()/resume() 返回 paused 任务并把载荷留在
    ``suspended_payload``；操作员决策经 :meth:`resume` 以 Command(resume) 续跑。"""

    def __init__(self, harness, **kwargs) -> None:
        super().__init__(harness, **kwargs)
        self._compiled = None
        self._config: dict = {}
        self._checkpointer = MemorySaver() if _HAS_LANGGRAPH else None
        # 控制流活对象（同进程内持久；跨进程恢复走事件链重建，见第三步）
        self._task: Optional[Task] = None
        self._plan: Optional[Plan] = None
        self._memory: Any = None
        self._pending: Optional[dict] = None
        self._repairs = 0
        self._round_no = 0
        self._steps: list = []
        self._decision_gates: list = []
        self._rejected = False
        self._batch_recover = False
        self._completed_without_result = False
        self._step_index = 0
        self._current_step: Optional[dict] = None
        self._current_decision: Any = None
        self._approval_payload: Optional[dict] = None
        #: 审批挂起载荷（非 None = 图暂停等操作员）；driver 据此发布 run.pending
        self.suspended_payload: Optional[dict] = None
        self._terminal_task: Optional[Task] = None

    # ------------------------------------------------------------------ 图构造

    def build_graph(self):
        graph = StateGraph(_GraphState)
        graph.add_node("round_head", self._node_round_head)
        graph.add_node("preflight", self._node_preflight)
        graph.add_node("step_dispatch", self._node_step_dispatch)
        graph.add_node("approve_prepare", self._node_approve_prepare)
        graph.add_node("approve_wait", self._node_approve_wait)
        graph.add_node("approve_apply", self._node_approve_apply)
        graph.add_node("execute_step", self._node_execute_step)
        graph.add_edge(START, "round_head")
        graph.add_conditional_edges(
            "round_head",
            lambda s: END if s.get("done") else "preflight",
            {"preflight": "preflight", END: END},
        )
        graph.add_conditional_edges(
            "preflight",
            lambda s: "round_head" if s.get("rejected") else (
                END if s.get("done") else "step_dispatch"),
            {"round_head": "round_head", "step_dispatch": "step_dispatch", END: END},
        )
        graph.add_conditional_edges(
            "step_dispatch",
            lambda s: END if s.get("done") else (
                "round_head" if s.get("batch_done") else "approve_prepare"),
            {"round_head": "round_head", "approve_prepare": "approve_prepare", END: END},
        )
        graph.add_conditional_edges(
            "approve_prepare",
            lambda s: END if s.get("done") else (
                "approve_wait" if s.get("approval_payload") else "execute_step"),
            {"approve_wait": "approve_wait", "execute_step": "execute_step", END: END},
        )
        graph.add_edge("approve_wait", "approve_apply")
        graph.add_conditional_edges(
            "approve_apply",
            lambda s: END if s.get("done") else "execute_step",
            {"execute_step": "execute_step", END: END},
        )
        graph.add_conditional_edges(
            "execute_step",
            lambda s: END if s.get("done") else "step_dispatch",
            {"step_dispatch": "step_dispatch", END: END},
        )
        return graph.compile(checkpointer=self._checkpointer)

    # -------------------------------------------------------------------- run

    def run(self, task_id: str) -> Task:
        if not _HAS_LANGGRAPH:  # pragma: no cover
            raise AgentError("ENGINE_UNAVAILABLE", "langgraph is not installed")
        self._task = self.harness.store.load(task_id)
        self._memory = self.restore(self._task)
        self._plan = self.rebuild_plan(self._task, self._memory)
        world = getattr(self.harness, "world", None)
        recall_holder = getattr(world, "recall_holder", None) if world is not None else None
        if recall_holder is not None:
            recall_holder["memory"] = self._memory
        self._pending = self.pending_step(self._task)
        self._repairs = 0
        self._rejected = False
        self._batch_recover = False
        self._completed_without_result = False
        self._step_index = 0
        self.suspended_payload = None
        self._terminal_task = None

        if self._compiled is None:
            self._compiled = self.build_graph()
        self._config = {
            "recursion_limit": max(400, self.max_rounds * 30 + 100),
            "configurable": {"thread_id": task_id},
        }
        result = self._compiled.invoke({"done": False}, config=self._config)
        return self._after_invoke(result)

    def resume(self, response: dict) -> Task:
        """操作员决策后续跑：interrupt() 返回决策值，图从挂起点继续。"""
        result = self._compiled.invoke(Command(resume=response), config=self._config)
        return self._after_invoke(result)

    def _after_invoke(self, result: Any) -> Task:
        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        if interrupts:
            self.suspended_payload = interrupts[0].value
        else:
            self.suspended_payload = None
        return self._terminal_task if self._terminal_task is not None else self._task

    # ------------------------------------------------------------------ 节点

    def _node_round_head(self, state: _GraphState) -> _GraphState:
        """原 while 体段 A：轮首控制 → 终止检查。终止路径置 done/terminal。"""
        task_id = self._task.id
        task, plan = self._task, self._plan

        if self._pending is None and self.control is not None:
            controlled = self.control.before_round(
                round_no=self._decisions(task) + 1,
                task=task,
                plan=plan,
            )
            self._task, self._plan = controlled.task, controlled.plan
            task, plan = self._task, self._plan

        if self._pending is None:
            if plan.is_blocked():
                self._terminal_task = self._stop(task, plan, "PLAN_BLOCKED")
                return {"done": True}
            settled = self._settle_advisory_plan(
                task, plan, self._decisions(task) + 1)
            if settled is not None:
                task, plan = settled
                self._task, self._plan = task, plan
                round_no = self._decisions(task) + 1
                try:
                    verified = self._verify_result_set(task.id, plan)
                    sources = self._check_evidence(task, verified, round_no)
                    task, obligation = self._verify_obligation(
                        task, verified, plan, round_no)
                except AgentError as error:
                    self._terminal_task = self._abort(task.id, error)
                    return {"done": True}
                self._task = task
                self._terminal_task = self._succeed(
                    task, verified, plan, obligation, sources)
                return {"done": True}
            if plan.is_complete() or plan.next_open() is None:
                # 原 while 语义：域开放给模型的计划，模型仍可在本轮继续提议
                # （以 report_blocked 收尾）；封闭计划没有可做的就停。
                if not self.plan_extension:
                    self._terminal_task = self._stop(task, plan, "PLAN_EXHAUSTED")
                    return {"done": True}
            if self._decisions(task) >= self.max_rounds:
                self._terminal_task = self._abort(
                    task_id, AgentError(
                        "ROUND_LIMIT",
                        f"no verified result after {self.max_rounds} rounds"))
                return {"done": True}

        # Operator notes（插话）每轮刷新：上一轮到达的插话在本轮 prompt 里。
        self._memory.operator_messages = self.operator_notes(task)
        self._round_no = (
            task.model.attempts if self._pending is not None
            else self._decisions(task) + 1
        )
        return {"done": False}

    def _node_preflight(self, state: _GraphState) -> _GraphState:
        """原 while 体段 B：propose（pending/策略/模型）→ additions → steps →
        compaction 冲刷 → 整批预检。schema 错误在任何派发前回绝。"""
        task_id = self._task.id
        task, plan, memory = self._task, self._plan, self._memory
        round_no = self._round_no

        budget_snapshot = self.harness.budget(task).snapshot()
        if self._pending is not None:
            # A reconciled round: the proposal is already approved and paid
            # for. Execute it; do not ask the model again.
            self.observer.round_start(round_no, budget_snapshot, plan)
            self._steps = [self._pending]
            self._pending = None
        else:
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
                    self._terminal_task = self._abort(task_id, AgentError(
                        "POLICY_NO_ACTIONS", "the action compiler produced no legal action",
                    ))
                    return {"done": True}
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
                            task, {"round": round_no, "packet": packet, "state": packet.state},
                            provider,
                        )
                    except AgentError as error:
                        self._terminal_task = self._abort(task_id, error)
                        return {"done": True}
                    if proposal is None:
                        self.observer.terminal(task, None)
                        self._terminal_task = task
                        return {"done": True}
                    task = self.harness.record(task, "policy_decided", {
                        "round": round_no,
                        "candidate_id": proposal.metadata.get("candidate_id", ""),
                        "packet_hash": packet.packet_hash,
                        "repaired": bool(proposal.metadata.get("repaired")),
                        "tokens": proposal.tokens,
                        "latency_ms": proposal.latency_ms,
                    })
                self.observer.proposal(round_no, proposal)
                self._steps = self._round_steps(proposal)
            else:
                context = memory.build(round_no, budget_snapshot, plan)
                try:
                    task, proposal = self.harness.propose(task, context)
                except AgentError as error:
                    self._terminal_task = self._abort(task_id, error)
                    return {"done": True}
                if proposal is None:
                    self.observer.terminal(task, None)
                    self._terminal_task = task
                    return {"done": True}
                self.observer.proposal(round_no, proposal)
                # A proposal may ask to extend the plan. The host judges it
                # before the step runs; see _extend_plan.
                task, plan = self._extend_plan(task, plan, round_no, memory)
                self._steps = self._round_steps(proposal)

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

        self._task, self._plan = task, plan
        # 新一轮的批从头开始：step 游标与批内标记必须归零，否则上一轮的
        # 游标会让本轮 dispatch 直接判定"批已结束"，模型空转到 ROUND_LIMIT。
        self._step_index = 0
        self._batch_recover = False
        self._completed_without_result = False

        # 整批预检：schema 错误在任何派发前回绝；可修复 → 回 round_head。
        decisions = []
        rejected = False
        for step in self._steps:
            decision = self.harness.gate(task, step)
            self.observer.gate(round_no, decision)
            if decision.allowed:
                decisions.append(decision)
                continue
            if decision.code in REPAIRABLE_DENIALS and self._repairs < MAX_PROPOSAL_REPAIRS:
                # Nothing was dispatched, nothing is unknown: the proposal
                # did not fit the tool's schema. Handing the error back is
                # not a silent retry — it is one visible event and one more
                # model call, bounded, and the run still ends denied if the
                # model cannot fix it.
                self._repairs += 1
                task = self.harness.record(task, "proposal_rejected", {
                    "round": round_no,
                    "tool": step["tool"],
                    "code": decision.code,
                    "message": decision.reason,
                    "arguments": step.get("arguments") or {},
                    "attempt": self._repairs,
                })
                memory.rejected_proposals = list(memory.rejected_proposals or []) + [{
                    "tool": step["tool"], "code": decision.code, "message": decision.reason,
                }]
                rejected = True
                break
            self._task = task
            self._terminal_task = self._deny(task, decision, plan)
            return {"done": True, "rejected": False}
        self._task = task
        self._decision_gates = decisions
        self._rejected = rejected
        return {"done": False, "rejected": rejected}

    def _node_step_dispatch(self, state: _GraphState) -> _GraphState:
        """批内步进：还有 step → 审批段；批结束 → round_end → 回 round_head。"""
        if self._step_index >= len(self._steps):
            if not self._batch_recover:
                self.observer.round_end(
                    self._round_no,
                    self.harness.budget(self._task).snapshot(),
                    self._plan,
                )
            self._batch_recover = False
            self._completed_without_result = False
            return {"batch_done": True}
        self._current_step = self._steps[self._step_index]
        self._current_decision = self._decision_gates[self._step_index]
        self._step_index += 1
        return {"batch_done": False}

    def _node_approve_prepare(self, state: _GraphState) -> _GraphState:
        """审批段前半：需要操作员？→ 提交 approval_requested + paused 并发布
        pending；remembered/not-required → 直接放行。"""
        step = self._current_step
        decision = self._current_decision
        control = self.control
        if control is None:
            return {}
        if not hasattr(control, "prepare_approval"):
            # 同步控制（benchmark ApprovalControl / NullControl）：before_tool
            # 从不阻塞，直接拿结论；不允许 → 终端拒绝。
            result_ctrl = control.before_tool(
                round_no=self._round_no, task=self._task, step=step, decision=decision,
            )
            self._task = result_ctrl.task
            if not result_ctrl.allowed:
                if result_ctrl.terminated:
                    self.observer.terminal(self._task, None)
                    self._terminal_task = self._task
                else:
                    self._terminal_task = self._deny(
                        self._task,
                        Denial(
                            tool=step["tool"],
                            code=result_ctrl.code or "OPERATOR_DENIED",
                            reason=result_ctrl.reason or "operator denied the call",
                        ),
                        self._plan,
                    )
                return {"done": True}
            return {}

        task, approval = control.prepare_approval(
            round_no=self._round_no, task=self._task, step=step,
        )
        self._task = task
        if approval is not None:
            # prepare 提交了 paused 转移；task 已是链上最新（含 paused 转移），
            # 后续事件基于同一序列号推进。
            self._approval_payload = approval
            return {"approval_payload": approval}
        return {}

    def _node_approve_wait(self, state: _GraphState) -> _GraphState:
        """纯节点：interrupt() 挂起等决策；恢复时本节点重跑但无副作用，
        interrupt() 直接返回保存的决策值——重放安全的唯一形态。"""
        response = interrupt(self._approval_payload)
        return {"operator_response": dict(response) if isinstance(response, dict) else {}}

    def _node_approve_apply(self, state: _GraphState) -> _GraphState:
        """审批段后半：提交 granted（回 running）/ denied；操作员停止 →
        aborted + StopRequested（与原生引擎同一终止路径）。"""
        control = self.control
        step = self._current_step
        response = dict(state.get("operator_response") or {})
        if getattr(control.run, "stop_requested", False):
            response["stop"] = True
        task, allowed, flag = control.apply_approval_response(
            round_no=self._round_no, task=self._task, step=step, response=response,
        )
        self._task = task
        if flag is not None and flag.get("stop"):
            # 操作员在审批挂起期间停止：approval_aborted/execution_resumed 已由
            # apply 提交；终态在引擎内落定（flowagent 不反向依赖 viewer 的
            # StopRequested），语义与 console 的 StopRequested 处理一致。
            task = self.harness.finalize(
                task, "failed",
                failure={
                    "code": "STOPPED_BY_OPERATOR",
                    "message": "操作员在审批挂起期间停止了这次运行",
                    "retryable": False,
                },
                event="task_failed", detail={"code": "STOPPED_BY_OPERATOR"},
            )
            self._task = task
            self.observer.terminal(task, None)
            self._terminal_task = task
            return {"done": True}
        if not allowed:
            self._terminal_task = self._deny(
                task,
                Denial(
                    tool=step["tool"],
                    code=flag["code"] if flag else "OPERATOR_DENIED",
                    reason=flag["reason"] if flag else "operator denied the call",
                ),
                self._plan,
            )
            return {"done": True}
        return {}

    def _node_execute_step(self, state: _GraphState) -> _GraphState:
        """单步执行：charge → run → 结算 → 观察 → 终止/完成检查。
        可恢复的参数类错误（fast 批量模式）→ 记 rejected 回 round_head。"""
        task_id = self._task.id
        task, plan, memory = self._task, self._plan, self._memory
        round_no = self._round_no
        step = self._current_step

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
                # execute() committed tool_failed before raising; reload the
                # durable task and clear the transient failure so the next
                # proposal starts from the committed sequence.
                self._task = self.harness.store.load(task_id)
                self._task.failure = None
                self.observer.round_end(
                    round_no, self.harness.budget(self._task).snapshot(), self._plan)
                self._batch_recover = True
                return {}
            self._terminal_task = self._abort(task_id, error)
            return {"done": True}

        memory.record(round_no, observation,
                      source_trust=trust_for_tool(observation.tool))
        self._plan = self.planner.advance(self._plan, observation) if self.planner else self._plan
        plan = self._plan
        self._task = self._persist_plan(task, plan, round_no)
        task = self._task

        self.observer.observation(round_no, observation)

        if observation.tool == "report_blocked":
            self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
            self._terminal_task = self._reported_blocked(task, plan, observation)
            return {"done": True}
        if plan.is_blocked():
            self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)
            self._terminal_task = self._stop(task, plan, "PLAN_BLOCKED")
            return {"done": True}
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
                    self._terminal_task = self._abort(task.id, error)
                    return {"done": True}
                self._task = task
                self.observer.round_end(round_no, self.harness.budget(task).snapshot(), self._plan)
                self._terminal_task = self._succeed(
                    task, verified, self._plan, obligation, sources)
                return {"done": True}
            # No result obligation: finish the batch; the round-head
            # completion check ends the task.
            self._completed_without_result = True
        return {}

