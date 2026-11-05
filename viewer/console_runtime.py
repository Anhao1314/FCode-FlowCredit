"""Console runtime host: the flowagent runtime, a run, and its observers.

This module knows how to import the runtime from the checkout, how one run is
represented, how live events are observed, and how a crashed run is inspected.
It does not know about HTTP or the run history index.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from console_common import DOMAIN_KEY

class StopRequested(Exception):
    """Raised inside an observer callback when the operator stops the run."""


class ScopedPlanner:
    """A domain planner narrowed to the operator's document scope.

    The runtime contract is unchanged: decompose() still returns a Plan and
    advance() still delegates settlement to the domain. Only the initial todos
    are filtered, so a scoped delegation never asks the model for documents it
    is not allowed to read. The world is filtered to the same ids, which is what
    enforces the scope for any provider that proposes outside the plan.
    """

    def __init__(self, runtime: Runtime, inner, scope: set[str]) -> None:
        self.runtime = runtime
        self.inner = inner
        self.scope = scope

    def decompose(self, goal: str):
        plan = self.inner.decompose(goal)
        todos = [
            todo for todo in plan.todos
            if not getattr(todo, "arguments", {}).get("doc_id") or todo.arguments["doc_id"] in self.scope
        ]
        return self.runtime.Plan(goal=plan.goal, todos=todos, revision=plan.revision)

    def advance(self, plan, observation):
        return self.inner.advance(plan, observation)


# --------------------------------------------------------------------- runtime


class Runtime:
    """A lazily imported flowagent checkout, exposing just what the console needs."""

    def __init__(self, repo: Path) -> None:
        src = None
        for candidate in (repo / "flowcredit" / "src", repo / "src", repo):
            if (candidate / "flowagent" / "__init__.py").exists():
                src = candidate.resolve()
                break
        if src is None:
            raise RuntimeError(f"no flowagent package found under {repo}")
        self.repo = repo.resolve()
        self.src = src
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))

        def imp(name: str):
            return importlib.import_module(name)

        m = imp("flowagent.core.contracts")
        self.AgentError = m.AgentError
        m = imp("flowagent.core.receipts")
        self.ReceiptStore = m.ReceiptStore
        m = imp("flowagent.core.store")
        self.TaskStore = m.TaskStore
        m = imp("flowagent.core.plan")
        self.Plan = m.Plan
        m = imp("flowagent.memory.cases")
        self.CaseIndex = m.CaseIndex
        m = imp("flowagent.memory.artifacts")
        self.ArtifactBook = m.ArtifactBook
        self.project_artifacts = m.project_artifacts
        self.default_artifact_db_path = m.default_artifact_db_path
        m = imp("flowagent.memory.facts")
        self.FactStore = m.FactStore
        m = imp("flowagent.tools.finance.facts")
        self.project_facts = m.project_facts
        m = imp("flowagent.intake")
        self.clarify = m.clarify
        m = imp("flowagent.chat")
        self.ChatService = m.ChatService
        self.merge_action = m.merge_action
        m = imp("flowagent.domains")
        self.get_domain = m.get_domain
        self.list_domains = m.list_domains
        m = imp("flowagent.demo.fixtures")
        self.SCENARIOS = m.SCENARIOS
        self.AVAILABLE = m.AVAILABLE
        self.get_scenario = m.get_scenario
        m = imp("flowagent.providers.mock")
        self.MockProvider = m.MockProvider
        m = imp("flowagent.runtime.budget")
        self.BudgetLimits = m.BudgetLimits
        m = imp("flowagent.runtime.control")
        self.RoundControl = m.RoundControl
        self.ToolControl = m.ToolControl
        m = imp("flowagent.runtime.harness")
        self.Harness = m.Harness
        m = imp("flowagent.runtime.loop")
        self.Loop = m.Loop
        m = imp("flowagent.runtime.verification")
        # The console validates a declared verification obligation when a run is
        # created, so an operator typo is refused then rather than discovered
        # after the work is already done.
        self.build_obligation = m.build_obligation
        m = imp("flowagent.tools.finance.fixtures")
        self.FinanceWorld = m.FinanceWorld
        m = imp("flowagent.tools.finance.planner")
        self.FinancePlanner = m.FinancePlanner
        m = imp("flowagent.tools.finance.tools")
        self.build_finance_tools = m.build_finance_tools
        m = imp("flowagent.tools.registry")
        self.ToolRegistry = m.ToolRegistry
        m = imp("flowagent.trajectory.exporter")
        self.export_trace = m.export_trace
        self.verify_trace = m.verify_trace
        m = imp("flowagent.trajectory.recorder")
        self.TraceRecorder = m.TraceRecorder
        m = imp("flowagent.review")
        self.record_review = m.record_review
        self.REVIEW_DECISIONS = m.DECISIONS
        self.MAX_REASON = m.MAX_REASON
        self.MAX_REVIEWER = m.MAX_REVIEWER
        m = imp("flowagent.review_profile")
        self.build_profile = m.build_profile
        m = imp("flowagent.tools.general.sandbox")
        self.WorkspaceSandbox = m.WorkspaceSandbox
        m = imp("flowagent.tools.general.project")
        self.import_project = m.import_project
        self.project_delivery = m.project_delivery
        self.validate_project_spec = m.validate_project_spec
        self.PROJECT_REPORT_PATH = m.PROJECT_REPORT_PATH
        m = imp("flowagent.providers.catalog")
        self.provider_catalog = m.provider_catalog

    def _module_for(self, template_key: str | None):
        """The domain module a template runs on.

        The general module is the product's default entry. The finance module
        stays registered as the reference workload: when no template is named
        (the corpus browser) or an unknown key arrives, the reference world is
        the only one with a canned corpus to show.
        """
        key = DOMAIN_KEY
        if template_key:
            try:
                key = self.get_scenario(template_key).module
            except Exception:  # noqa: BLE001 — unknown template falls back
                key = DOMAIN_KEY
        return self.get_domain(key)

    def tool_contracts(self) -> list[dict]:
        """The registered contracts (name, effect, version, schemas)."""
        module = self.get_domain(DOMAIN_KEY)
        world = module.create_world()
        registry = self.ToolRegistry(module.build_tools(world))
        return [registry.get(tool["name"]).contract() for tool in registry.catalog()]

    def corpus(self, template_key: str | None = None) -> list[dict]:
        """The document corpus a template will run against (operator-facing)."""
        module = self._module_for(template_key)
        if module.key != DOMAIN_KEY:
            return []  # general delegations select sources, not corpus documents
        partial = False
        if template_key:
            partial = self.get_scenario(template_key).partial_corpus
        world = module.create_world(partial=partial)
        return world.list_documents()["documents"]

    def template_tools(self, template_key: str) -> list[dict]:
        """This template's closed tool registry, in registration order."""
        scenario = self.get_scenario(template_key)
        module = self._module_for(template_key)
        world = module.create_world(partial=scenario.partial_corpus)
        registry = self.ToolRegistry(module.build_tools(world))
        allowed = set(scenario.allowed_tools)
        return [registry.get(name).contract() for name in registry.names if name in allowed]

    def modules(self) -> list[dict]:
        """The domain modules this runtime can host, for the console."""
        return [
            {
                "key": module.key,
                "title": module.title,
                "reportTemplate": module.report_template,
                "approvalTools": list(module.approval_tools),
            }
            for module in self.list_domains()
        ]

    def templates(self) -> list[dict]:
        # The general module is the product's default entry, so its scenarios
        # lead the picker even though the reference workload is declared first
        # in the scenario table. The sort is stable within each group.
        keys = sorted(
            self.AVAILABLE,
            key=lambda key: 0 if self.SCENARIOS[key].module == "general-work" else 1,
        )
        out = []
        for key in keys:
            scenario = self.SCENARIOS[key]
            limits = scenario.limits
            out.append(
                {
                    "key": scenario.key,
                    "title": scenario.title,
                    "objective": scenario.objective,
                    "story": scenario.story,
                    "expected": scenario.expected_status,
                    "max_rounds": scenario.max_rounds,
                    "limits": limits.to_dict(),
                    "module": scenario.module,
                }
            )
        return out


# ------------------------------------------------------------------ run records


@dataclass
class Run:
    id: str
    template: str
    objective: str
    created: str
    pace: float
    status: str = "running"  # running | succeeded | failed | denied | stopped | error
    version: int = 0
    snapshot: dict | None = None
    trace_path: str = ""
    rounds: int = 0
    event_count: int = 0
    trace_verified: bool = False
    task_id: str = ""
    error: str = ""
    stop_requested: bool = False
    finished: bool = False
    name: str = ""
    pinned: bool = False
    approval: str = "consequential"  # off | consequential | all
    model: str = "mock"              # operator-selected provider key for this run
    effort: str = ""                 # reasoning effort (kimi only); mock ignores it
    pending: dict | None = None
    queue: list | None = None
    cost: dict | None = None
    spec: dict | None = None
    controller: Any = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    cond: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.cond = threading.Condition(self.lock)

    def bump(self) -> None:
        """Caller must hold the lock."""
        self.version += 1
        self.cond.notify_all()

    def to_json(self, *, detail: bool = False) -> dict:
        out = {
            "id": self.id,
            "template": self.template,
            "objective": self.objective,
            "created": self.created,
            "status": self.status,
            "rounds": self.rounds,
            "events": self.event_count,
            "tracePath": self.trace_path,
            "verified": self.trace_verified,
            "name": self.name,
            "pinned": self.pinned,
            "approval": self.approval,
            "taskId": self.task_id,
            "cost": self.cost,
            "spec": self.spec,
        }
        if detail:
            out["error"] = self.error
            out["pending"] = self.pending
            out["queue"] = self.queue or []
            out["model"] = self.model
            out["effort"] = self.effort
        return out

class LiveObserver:
    """The demo Renderer's job, minus the ANSI — and with an operator stop.

    Timing is simulated: the offline mock answers instantly, so without pacing a
    nine-round run would be one blob. Each pause doubles as a stop check, which
    is why stopping lands on a round boundary instead of mid-transaction.
    """

    def __init__(self, run: Run, recorder, store, task_id: str) -> None:
        self.run = run
        self.recorder = recorder
        self.store = store
        self.task_id = task_id
        self.pace = run.pace

    # ---------------------------------------------------------------- plumbing

    def _pace(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            if self.run.stop_requested:
                raise StopRequested()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _enter(self, seconds: float):
        self._pace(seconds)
        if self.run.stop_requested:
            raise StopRequested()

    # ------------------------------------------------------------ observer API

    def round_start(self, round_no: int, budget, plan=None) -> None:
        self._enter(self.pace * 0.5)
        with self.run.lock:
            self.recorder.round_start(round_no, budget, plan)

    def proposal(self, round_no: int, proposal) -> None:
        self._enter(self.pace * 0.25)
        with self.run.lock:
            self.recorder.proposal(round_no, proposal)

    def gate(self, round_no: int, decision) -> None:
        with self.run.lock:
            self.recorder.gate(round_no, decision)

    def observation(self, round_no: int, observation) -> None:
        self._enter(self.pace * 0.25)
        with self.run.lock:
            self.recorder.observation(round_no, observation)

    def round_end(self, round_no: int, budget, plan=None) -> None:
        with self.run.lock:
            self.recorder.round_end(round_no, budget, plan)

    def terminal(self, task, result) -> None:
        with self.run.lock:
            self.recorder.terminal(task, result)


class RunControl:
    """Console-side control: approvals block, plan and budget commands queue.

    Exactly one thread writes the event chain: the run thread. HTTP threads only
    set a decision or enqueue a command, and the run thread commits it at the
    next legal boundary. That keeps the compare-and-swap discipline intact and
    makes "下一轮生效" a fact rather than a label.
    """

    def __init__(
        self,
        run: Run,
        harness,
        runtime: Runtime,
        approval_tools,
        timeout: float,
        preview=None,
        classify_consequential=None,
    ) -> None:
        self.run = run
        self.harness = harness
        self.Plan = runtime.Plan
        self.BudgetLimits = runtime.BudgetLimits
        self.RoundControl = runtime.RoundControl
        self.ToolControl = runtime.ToolControl
        self.approval_tools = set(approval_tools)
        #: Optional host hook: (tool, arguments) -> a preview payload the
        #: approval dialog renders (write_file/edit_file's real diff,
        #: run_command's full command line). Host-side by construction;
        #: nothing executes before the decision.
        self.preview = preview
        #: Optional host hook: (tool, arguments) -> True when this specific
        #: call is consequential. The command registry uses it to classify
        #: run_command per program (readonly utilities skip the approval);
        #: every other tool falls back to the approval set.
        self.classify_consequential = classify_consequential
        self.timeout = max(5.0, float(timeout))
        self._lock = threading.Lock()
        self._commands: deque[dict] = deque()
        self._approval: dict | None = None
        self._decision: dict | None = None
        self._wake = threading.Event()
        self._remembered: set[str] = set()

    # ---------------------------------------------------------------- HTTP side

    def queue(self, command: dict) -> None:
        with self._lock:
            self._commands.append(command)
        if command.get("kind") == "message":
            # 发布权威队列：任何客户端入队后，所有客户端的排队面板立即同步
            self._publish_queue()

    def queue_snapshot(self) -> list[dict]:
        """The authoritative queued-message list (server order is truth)."""
        with self._lock:
            return [
                {"id": c.get("id"), "text": c.get("text", ""), "steered": bool(c.get("steered"))}
                for c in self._commands
                if c.get("kind") == "message"
            ]

    def _publish_queue(self) -> None:
        """Mirror the queue onto the run and wake SSE subscribers."""
        snapshot = self.queue_snapshot()
        with self.run.lock:
            self.run.queue = snapshot
            self.run.bump()

    def steer_message(self, message_id: str) -> dict:
        """Promote a queued operator message to the front, flagged ``steered``.

        Steer never bypasses the chain: the message is still applied by the run
        thread at the next round boundary, only earlier in the queue and with a
        visible ``steered`` mark on its ``operator_message`` event.
        """
        return self.mutate_message(message_id, "steer")

    def mutate_message(self, message_id: str, op: str, new_text: str | None = None) -> dict:
        """Edit / remove / reorder / steer a queued operator message by id."""
        with self._lock:
            idx = next(
                (
                    i
                    for i, c in enumerate(self._commands)
                    if c.get("kind") == "message" and c.get("id") == message_id
                ),
                None,
            )
            if idx is None:
                return {"ok": False, "error": "no queued message with that id"}
            if op == "remove":
                del self._commands[idx]
            elif op == "moveUp":
                prev = next(
                    (
                        j
                        for j in range(idx - 1, -1, -1)
                        if self._commands[j].get("kind") == "message"
                    ),
                    None,
                )
                if prev is not None:
                    self._commands[idx], self._commands[prev] = (
                        self._commands[prev],
                        self._commands[idx],
                    )
            elif op == "edit":
                text = str(new_text or "").strip()
                if not text:
                    return {"ok": False, "error": "text is required"}
                self._commands[idx]["text"] = text[:600]
            elif op == "steer":
                command = self._commands[idx]
                del self._commands[idx]
                command["steered"] = True
                self._commands.appendleft(command)
            else:
                return {"ok": False, "error": "op must be edit, remove, moveUp or steer"}
        self._publish_queue()
        return {"ok": True, "queue": self.queue_snapshot()}

    def decide(self, approval_id: str, *, allow: bool, remember: bool = False) -> bool:
        with self._lock:
            if self._approval is None or self._approval.get("id") != approval_id:
                return False
            # The two pending kinds share the slot; an approval decision must not
            # resolve a question (it would read as "not answered").
            if self._approval.get("kind") == "question":
                return False
            if self._decision is not None:
                return False
            self._decision = {"allow": allow, "remember": remember}
            self._wake.set()
            return True

    def pending(self) -> dict | None:
        with self._lock:
            return dict(self._approval) if self._approval else None

    # ------------------------------------------------------------------ asking
    #
    # ``ask_operator`` is a tool, so the model asks *inside* a tool call: the
    # run thread parks here exactly like the approval gate does, and the answer
    # comes back as that tool's observation. The console owns the human
    # channel, the kernel owns the accounting.

    def ask(self, *, question: str, why: str = "") -> dict:
        """Publish one question, wait for the operator, return the answer.

        The audit trail is the tool call itself, not a side event: the harness
        has already written ``tool_started`` with the question in its arguments,
        and it writes ``tool_succeeded`` with the answer in its result. Adding
        events from inside a tool body would break the store's optimistic
        sequencing (the loop holds a task snapshot) — and it would be less
        honest, because the tool call *is* the unit an auditor reads.

        A timeout is reported as a timeout — the model is told it is on its own
        rather than handed an invented answer. A stop request ends the question
        the same way it ends an approval.
        """
        text = str(question or "").strip()[:400]
        if not text:
            raise AgentError("ASK_QUESTION_EMPTY", "question must not be empty")
        with self._lock:
            if self._approval is not None:
                # One human decision at a time: the gate and the question share
                # the pending slot and the UI renders whichever is live.
                return {"answered": False, "reason": "another question or approval is already pending"}
            round_no = max(0, int(self.run.rounds or 0)) + 1
            ask_id = f"ask-{round_no}-{uuid.uuid4().hex[:6]}"
            self._approval = {
                "kind": "question",
                "id": ask_id,
                "round": round_no,
                "question": text,
                "why": str(why or "")[:200],
                "expiresIn": int(self.timeout),
                "requestedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._decision = None
            self._wake.clear()
        self._publish()

        deadline = time.monotonic() + self.timeout
        stopped = False
        try:
            while True:
                if self.run.stop_requested:
                    stopped = True
                    break
                if self._wake.wait(0.1):
                    break
                if time.monotonic() >= deadline:
                    with self._lock:
                        self._decision = {"answered": False, "timeout": True}
                    break
        finally:
            with self._lock:
                answer = self._decision
                self._approval = None
            self._publish()

        if stopped:
            raise StopRequested()
        answered = bool(answer and answer.get("answered"))
        reply = str((answer or {}).get("answer") or "")[:600]
        if answered:
            return {"answered": True, "answer": reply}
        return {
            "answered": False,
            "timed_out": bool((answer or {}).get("timeout")),
            "reason": "the operator did not answer in time",
        }

    def answer(self, question_id: str, text: str) -> bool:
        """Record the operator's answer to the pending question."""
        with self._lock:
            pending = self._approval
            if pending is None or pending.get("kind") != "question" or pending.get("id") != question_id:
                return False
            if self._decision is not None:
                return False
            self._decision = {"answered": True, "answer": str(text or "").strip()[:600]}
            self._wake.set()
            return True

    def skip_question(self, question_id: str) -> bool:
        """The operator declines to answer: the run continues without one."""
        with self._lock:
            pending = self._approval
            if pending is None or pending.get("kind") != "question" or pending.get("id") != question_id:
                return False
            if self._decision is not None:
                return False
            self._decision = {"answered": False, "declined": True}
            self._wake.set()
            return True

    def current_limits(self) -> dict:
        return self.harness.limits.to_dict()

    # ------------------------------------------------------------- runtime side

    def before_round(self, *, round_no: int, task, plan):
        with self._lock:
            commands, self._commands = list(self._commands), deque()
        for command in commands:
            if command["kind"] == "plan":
                task, plan = self._apply_plan(task, plan, round_no, command)
            elif command["kind"] == "budget":
                task = self._apply_budget(task, round_no, command)
            elif command["kind"] == "message":
                task = self._apply_message(task, round_no, command)
        if any(c["kind"] == "message" for c in commands):
            # 队列在本边界被消费：把权威队列的新状态推给所有订阅者
            self._publish_queue()
        return self.RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task, step: dict, decision):
        mode = self.run.approval
        tool = step["tool"]
        required = mode == "all" or (mode == "consequential" and tool in self.approval_tools)
        if required and mode == "consequential" and self.classify_consequential is not None:
            # 命令级豁免：主机注册表判定为纯读的调用（如 run_command 的
            # ls/cat/git status）与读工具同等待遇，不过审批。
            try:
                if self.classify_consequential(tool, step.get("arguments") or {}) is False:
                    required = False
            except Exception:  # noqa: BLE001 — 分类失败按需要审批处理
                required = True
        if not required:
            return self.ToolControl(task=task, allowed=True)
        if tool in self._remembered:
            task = self.harness.record(
                task,
                "approval_granted",
                {"round": round_no, "tool": tool, "by": "operator-rule", "scope": "run"},
            )
            return self.ToolControl(task=task, allowed=True)

        if task.status == "planned":
            task = self.harness.transition(task, "running", "execution_started", {})
        approval = {
            "id": f"apv-{round_no}-{uuid.uuid4().hex[:6]}",
            "round": round_no,
            "tool": tool,
            "arguments": step.get("arguments") or {},
            "policy": "every-step" if mode == "all" else "consequential",
            "expiresIn": int(self.timeout),
            "requestedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if self.preview is not None:
            try:
                payload = self.preview(tool, step.get("arguments") or {})
            except Exception:  # noqa: BLE001 — a preview must never block the gate
                payload = None
            if payload is not None:
                approval["preview"] = payload
        task = self.harness.transition(task, "paused", "approval_requested", approval)
        with self._lock:
            self._approval = approval
            self._decision = None
            self._wake.clear()
        self._publish()

        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if self.run.stop_requested:
                    with self._lock:
                        self._approval = None
                    self._publish()
                    task = self.harness.record(
                        task,
                        "approval_aborted",
                        {"round": round_no, "tool": tool, "by": "operator", "code": "STOPPED_BY_OPERATOR"},
                    )
                    task = self.harness.transition(
                        task, "running", "execution_resumed", {"round": round_no}
                    )
                    raise StopRequested()
                if self._wake.wait(0.1):
                    break
                if time.monotonic() >= deadline:
                    with self._lock:
                        self._decision = {"allow": False, "timeout": True}
                    break
        except BaseException:
            with self._lock:
                self._approval = None
            self._publish()
            if task.status == "paused":
                task = self.harness.transition(
                    task, "running", "execution_resumed", {"round": round_no, "reason": "controller interrupted"}
                )
            raise

        with self._lock:
            result = self._decision or {"allow": False, "timeout": True}
            self._approval = None
        self._publish()
        if result["allow"]:
            task = self.harness.transition(
                task,
                "running",
                "approval_granted",
                {
                    "round": round_no,
                    "tool": tool,
                    "by": "operator",
                    "remember": bool(result.get("remember")),
                },
            )
            if result.get("remember"):
                with self._lock:
                    self._remembered.add(tool)
            return self.ToolControl(task=task, allowed=True)

        code = "APPROVAL_TIMEOUT" if result.get("timeout") else "OPERATOR_DENIED"
        reason = (
            "no decision arrived before the approval deadline"
            if result.get("timeout")
            else "the operator denied this call"
        )
        task = self.harness.record(
            task,
            "approval_denied",
            {"round": round_no, "tool": tool, "by": "operator", "code": code},
        )
        return self.ToolControl(task=task, allowed=False, code=code, reason=reason)

    # ------------------------------------------------------------------ private

    def _publish(self) -> None:
        pending = self.pending()
        with self.run.lock:
            self.run.pending = pending
            self.run.bump()

    def _reject(self, task, round_no: int, kind: str, reason: str):
        return self.harness.record(
            task,
            "control_rejected",
            {"round": round_no, "kind": kind, "reason": reason[:200], "by": "operator"},
        )

    def _apply_plan(self, task, plan, round_no: int, command: dict):
        action = command.get("action")
        todo_id = command.get("todo_id")
        note = str(command.get("note") or "")[:200]
        try:
            if action == "skip":
                marked = plan.mark(todo_id, "abandoned", reason=note or "operator skipped this step")
                updated = self.Plan(goal=marked.goal, todos=marked.todos, revision=plan.revision + 1)
            elif action == "promote":
                todos = list(plan.todos)
                index = next((i for i, todo in enumerate(todos) if todo.id == todo_id), None)
                if index is None:
                    raise ValueError(f"no todo {todo_id!r}")
                target = todos.pop(index)
                if target.status not in ("pending", "active"):
                    raise ValueError(f"todo {todo_id!r} is {target.status}")
                insert = next(
                    (i for i, todo in enumerate(todos) if todo.status in ("pending", "active")),
                    len(todos),
                )
                updated = self.Plan(
                    goal=plan.goal,
                    todos=todos[:insert] + [target] + todos[insert:],
                    revision=plan.revision + 1,
                )
            else:
                raise ValueError(f"unknown plan action {action!r}")
        except Exception as exc:  # noqa: BLE001 — a stale command must not kill the run
            return self._reject(task, round_no, "plan", str(exc)), plan
        task.plan_state = updated.to_dict()
        task = self.harness.record(
            task,
            "plan_steered",
            {
                "round": round_no,
                "action": action,
                "todo_id": todo_id,
                "note": note,
                "plan": updated.to_dict(),
                "by": "operator",
            },
        )
        return task, updated

    def _apply_budget(self, task, round_no: int, command: dict):
        before = self.harness.limits
        try:
            new = self.BudgetLimits(
                max_calls=int(command["maxCalls"]),
                max_reads=int(command["maxReads"]),
                max_cost_microusd=int(command["maxCostMicrousd"]),
                max_tokens=int(command.get("maxTokens") or before.max_tokens),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._reject(task, round_no, "budget", f"invalid limits: {exc}")
        if (
            new.max_calls < before.max_calls
            or new.max_reads < before.max_reads
            or new.max_cost_microusd < before.max_cost_microusd
            or new.max_tokens < before.max_tokens
        ):
            return self._reject(task, round_no, "budget", "limits can only be raised")
        used = self.harness.budget(task).snapshot()
        if (
            used["calls_used"] > new.max_calls
            or used["reads_used"] > new.max_reads
            or used["cost_used_microusd"] > new.max_cost_microusd
        ):
            return self._reject(task, round_no, "budget", "new limits are below current usage")
        self.harness.limits = new
        return self.harness.record(
            task,
            "budget_adjusted",
            {
                "round": round_no,
                "before": before.to_dict(),
                "after": new.to_dict(),
                "by": "operator",
            },
        )

    def _apply_message(self, task, round_no: int, command: dict):
        # A mid-run operator note. The deterministic mock cannot read free
        # text, so this does not steer the planner yet — but it is committed
        # to the same hash chain as every other human decision, and a real
        # provider consumes it from the event log like any other input.
        # A steered message keeps its queue priority visible on the event:
        # ``steered`` is an annotation, never a bypass of the round boundary.
        text = str(command.get("text") or "").strip()
        if not text:
            return task
        detail: dict = {"round": round_no, "text": text[:600], "by": "operator"}
        if command.get("id"):
            detail["message_id"] = str(command["id"])
        if command.get("steered"):
            detail["steered"] = True
        return self.harness.record(task, "operator_message", detail)


class _EventsShim:
    """Satisfies TraceRecorder.build()'s only store call, from a read connection.

    The watcher must read committed rows through its own SQLite connection
    (WAL gives it a stable snapshot), not through the run's connection where a
    read could land inside an open write transaction and see torn state.
    """

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def events(self, task_id: str) -> list[dict]:
        return self._events


def _watch(run: Run, recorder, store, db: Path, task_id: str) -> None:
    """Tail the committed event chain and publish trace snapshots."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    last_events = -1
    try:
        while True:
            time.sleep(0.12)
            with run.lock:
                if run.finished:
                    return
            try:
                rows = conn.execute(
                    "SELECT sequence, type, payload, prev_hash, hash, at FROM event"
                    " WHERE task_id=? ORDER BY sequence ASC",
                    (task_id,),
                ).fetchall()
                if len(rows) == last_events:
                    continue
                body_row = conn.execute("SELECT body FROM task WHERE id=?", (task_id,)).fetchone()
                if body_row is None:
                    continue
                body = json.loads(body_row["body"])
                events = [
                    {
                        "sequence": row["sequence"],
                        "type": row["type"],
                        "payload": json.loads(row["payload"]),
                        "prev_hash": row["prev_hash"],
                        "hash": row["hash"],
                        "at": row["at"],
                    }
                    for row in rows
                ]
                task_view = types.SimpleNamespace(
                    id=body["id"],
                    objective=body["objective"],
                    principal=body["principal"],
                    binding=body["binding"],
                    allowed_tools=body["allowed_tools"],
                    model_grant=body["model_grant"],
                    status=body["status"],
                    sequence=body["sequence"],
                    failure=body.get("failure"),
                )
                with run.lock:
                    if run.finished:
                        return
                    real_store = recorder._store  # noqa: SLF001 — swap is guarded by run.lock
                    recorder._store = _EventsShim(events)
                    try:
                        snapshot = recorder.build(task_view)
                    finally:
                        recorder._store = real_store
                    run.snapshot = snapshot
                    run.event_count = len(events)
                    run.rounds = len(snapshot["rounds"])
                    run.bump()
                last_events = len(rows)
            except sqlite3.Error:
                continue  # database being created/renamed under us; retry next tick
    finally:
        conn.close()


def _run_crash(rt: Runtime, harness, store, world, scenario, task, observer, db: Path):
    """Mirrors demo/runner.py::_run_crash, against this run's database.

    A real SIGKILL needs a real process, so the worker subprocess kills itself
    mid-run and the parent resumes from the same event log.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(rt.src),
        "FLOWAGENT_DB": str(db),
        "FLOWAGENT_TASK_ID": task.id,
        "FLOWAGENT_CRASH_AT": scenario.crash_at or "tool_succeeded",
        "FLOWAGENT_CRASH_AT_COUNT": str(scenario.crash_at_count),
        "FLOWAGENT_MAX_ROUNDS": str(scenario.max_rounds),
    }
    subprocess.run(
        [sys.executable, "-m", "flowagent.demo.crash_worker"],
        cwd=str(rt.src.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    resumed = store.load(task.id)
    module = rt.get_domain(scenario.module)
    memory = rt.Loop(
        harness, planner=module.make_planner(world), result_tools=module.result_tools,
    ).restore(resumed)
    world.apply_state(memory.state())
    return rt.Loop(
        harness, planner=module.make_planner(world), max_rounds=scenario.max_rounds, observer=observer,
        result_tools=module.result_tools,
    ).run(resumed.id)


# ---------------------------------------------------------------- http surface
