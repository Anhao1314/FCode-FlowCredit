"""Single-action mechanics: propose, gate, execute, verify.

This is the smallest unit of the runtime. It knows how to ask a model for one
proposal, how to refuse it, how to run one read, and how to re-derive the final
answer independently. It deliberately knows nothing about *how many* rounds
happen — that is :mod:`flowagent.runtime.loop`.

The split matters for an interview question like "what stops the model from
looping forever?" The answer is not in this file; it is in the loop's round
budget and in the verifier's refusal to accept an unproven result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Protocol

from ..core.contracts import AgentError, Task, validate_plan
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..providers.base import Proposal, Provider
from ..tools.base import EFFECT_READ, Observation
from ..tools.registry import ToolRegistry
from ..tools.trust import trust_for_tool
from .budget import Budget, BudgetLimits
from .permissions import GateDecision, Permissions

__all__ = ["Harness", "TraceObserver"]


class TraceObserver(Protocol):
    """Receives structured signals so the demo can render and record one run."""

    def round_start(self, round_no: int, budget: dict[str, Any]) -> None: ...
    def proposal(self, round_no: int, proposal: Proposal) -> None: ...
    def gate(self, round_no: int, decision: GateDecision) -> None: ...
    def observation(self, round_no: int, observation: Observation) -> None: ...
    def round_end(self, round_no: int, budget: dict[str, Any]) -> None: ...
    def terminal(self, task: Task, result: dict[str, Any] | None) -> None: ...


class Harness:
    """Executes exactly one proposal at a time against a durable task."""

    #: Conservative reservation charged before a model call is dispatched.
    RESERVED_TOKENS = 1_000
    RESERVED_COST_MICROUSD = 500

    def __init__(
        self,
        *,
        store: TaskStore,
        registry: ToolRegistry,
        provider: Provider,
        world: Any,
        limits: BudgetLimits,
        receipts: ReceiptStore,
        verifier: Callable[[Observation], dict[str, Any]] | None = None,
        state_fn: Callable[[Any], dict[str, Any]] | None = None,
        instruction: str | None = None,
        prompt_profile: str | None = None,
        plan_extension: bool = False,
        verification: Any = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.provider = provider
        self.world = world
        self.limits = limits
        self.receipts = receipts
        self.verifier = verifier
        #: The domain's memory state view (see DomainModule.summarize_state);
        #: working memory calls it instead of knowing tool names.
        self.state_fn = state_fn
        #: The domain's protocol wording for the planner (see
        #: DomainModule.instruction). ``None`` keeps the read-only default the
        #: finance reference workload is frozen against.
        self.instruction = instruction
        #: The system-prompt profile this run is bound by (see
        #: :mod:`flowagent.providers.prompt`). ``None`` selects the default and,
        #: more importantly, emits no selector into the context — which is what
        #: keeps finance contexts byte-identical to the frozen SFT set.
        self.prompt_profile = prompt_profile
        #: Whether the model may propose additional plan todos (see
        #: DomainModule.plan_extension). Carried on the harness so every caller
        #: that builds one — demo, console, tests — gets the domain's own
        #: answer instead of having to repeat it.
        self.plan_extension = plan_extension
        #: The declared result obligation this task must satisfy, or ``None``.
        #: A :class:`~flowagent.runtime.verification.VerificationScope`: the
        #: loop dispatches its obligation through the ordinary gate/budget/event
        #: path and the domain judges the observation. ``None`` means the task
        #: declared no engineering-result obligation, and then the terminal
        #: result must say that only file state was verified.
        self.verification = verification

    # ------------------------------------------------------------------ setup

    def create(
        self,
        *,
        task_id: str,
        objective: str,
        principal: str,
        binding: dict[str, Any],
        allowed_tools: list[str],
        grant: dict[str, Any],
    ) -> Task:
        task = Task(
            id=task_id,
            principal=principal,
            objective=objective,
            binding=binding,
            allowed_tools=list(allowed_tools),
            model_grant=dict(grant),
        )
        return self.store.create(task)

    def record_recall(self, task: Task, hits: list[dict[str, Any]]) -> Task:
        """Freeze the old-case lookup into the chain before the first proposal.

        Memory is a read, but an invisible one would break replay: a resumed
        run would query a different index and see a different prompt. Writing
        the hits as an event makes the recall itself auditable and replayable.
        """
        if not hits:
            return task
        return self.store.save(
            task,
            expected_sequence=task.sequence,
            event_type="memory_recalled",
            detail={"query": task.objective, "hits": list(hits), "count": len(hits)},
        )

    def budget(self, task: Task) -> Budget:
        return Budget(self.store, f"task:{task.id}", self.limits)

    def permissions(self, task: Task) -> Permissions:
        """The gate for this task: effects come from the grant, not the tool.

        A grant without ``allowed_effects`` keeps the Phase 1 read-only promise;
        naming wider classes there is a host decision recorded in the task
        binding and visible in every trace.
        """
        effects = task.model_grant.get("allowed_effects") or (EFFECT_READ,)
        return Permissions(self.registry, task.allowed_tools, task.model_grant, effects)

    # -------------------------------------------------------------- mechanics

    def propose(self, task: Task, context: dict[str, Any]) -> tuple[Task, Proposal | None]:
        """Ask the configured provider for one proposal."""
        return self._propose(task, context, self.provider)

    def propose_with(
        self,
        task: Task,
        context: dict[str, Any],
        provider: Provider,
    ) -> tuple[Task, Proposal | None]:
        """Ask a specific policy adapter for one proposal.

        Packet mode needs a provider constructed per round (it carries the
        candidate menu). The state-machine, budget and receipt semantics are
        identical to :meth:`propose`.
        """
        return self._propose(task, context, provider)

    def _propose(
        self,
        task: Task,
        context: dict[str, Any],
        provider: Provider,
    ) -> tuple[Task, Proposal | None]:
        """Ask the model for one proposal and commit it as a plan-less round.

        The plan is *not* written into ``task.plan``: in loop mode each round is
        a fresh proposal, and the task-level plan is reserved for the single-shot
        path. Keeping them separate is what lets the store keep enforcing "a
        validated plan is immutable" without blocking round two.

        Returns ``(task, None)`` when the round ended without a usable proposal.
        The caller must treat the returned task as terminal in that case.
        """
        first = task.status == "created"
        if first:
            task = self._commit(
                task,
                task.advance("planning").with_model(status="started", attempts=task.model.attempts + 1),
                "model_started",
                {"round": context["round"]},
            )
        else:
            # Every round is a fresh call: validated -> started. Keeping the old
            # "validated" state would make a timeout in round two illegal.
            task = self._commit(
                task,
                task.with_model(status="started", attempts=task.model.attempts + 1),
                "model_started",
                {"round": context["round"]},
            )

        intent = self._intent(task, context)
        call_context = {**context, "intent": intent}

        # Charge before dispatch. A provider that never answers still costs.
        # Providers may declare a context-aware upper bound; local providers
        # fall back to the historical constants.
        reserve = getattr(provider, "reserve", None)
        if callable(reserve):
            reserve_tokens, reserve_cost = reserve(call_context)
        else:
            reserve_tokens = int(getattr(provider, "reserve_tokens", self.RESERVED_TOKENS))
            reserve_cost = int(getattr(provider, "reserve_cost_microusd", self.RESERVED_COST_MICROUSD))
        reserve_tokens = int(reserve_tokens)
        reserve_cost = int(reserve_cost)
        self.budget(task).charge_call(
            tokens=reserve_tokens,
            cost_microusd=reserve_cost,
        )

        try:
            proposal = provider.propose(call_context)
        except AgentError as error:
            return self._unknown(task, intent, error), None

        try:
            self.budget(task).settle_call(
                reserved_tokens=reserve_tokens,
                reserved_cost_microusd=reserve_cost,
                actual_tokens=proposal.tokens,
                actual_cost_microusd=proposal.cost_microusd,
            )
            plan = validate_plan(proposal.plan)
        except AgentError as error:
            # A response arrived and it was illegal. That is a confirmed failure,
            # not an unknown one: we know exactly what the model said.
            failed = task.with_model(status="failed", receipt=proposal.receipt())
            failed.failure = {"code": error.code, "message": error.message, "retryable": False}
            return self._commit(task, failed.advance("failed"), "model_invalid", {"code": error.code}), None

        updated = task.with_model(
            status="validated",
            proposal=plan.to_dict(),
            receipt=proposal.receipt(),
        )
        if first:
            updated = updated.advance("planned")
        try:
            return self._commit(task, updated, "plan_validated", {"plan": plan.to_dict(), "round": context["round"]}), proposal
        except TypeError as error:
            # The integrity layer rejected the model-authored body (e.g. a
            # float where the runtime requires int micro-units or basis
            # points). The model said it, so this is a confirmed failure,
            # not an unknown outcome.
            failed = task.with_model(status="failed", receipt=proposal.receipt())
            failed.failure = {
                "code": "PROPOSAL_MALFORMED",
                "message": f"plan body rejected by integrity layer: {error}",
                "retryable": False,
            }
            return (
                self._commit(
                    task,
                    failed.advance("failed"),
                    "model_invalid",
                    {"code": "PROPOSAL_MALFORMED"},
                ),
                None,
            )

    def auto_step(
        self,
        task: Task,
        step: dict[str, Any],
        *,
        round_no: int,
        candidate_id: str = "",
        packet_hash: str = "",
    ) -> Task:
        """Commit a host-selected action without a model call.

        Only legal when the Action Compiler found exactly one candidate. It
        still advances the task state machine and writes an ``auto_action``
        event, so replay and audit see why no model was asked.
        """
        detail = {
            "round": round_no,
            "candidate_id": candidate_id,
            "packet_hash": packet_hash,
            "step": dict(step),
        }
        if task.status == "created":
            task = self._commit(task, task.advance("planning"), "auto_planning", detail)
        if task.status == "planning":
            return self._commit(task, task.advance("planned"), "auto_action", detail)
        if task.status in ("planned", "running"):
            return self._commit(task, task, "auto_action", detail)
        raise AgentError("AUTO_STEP_BLOCKED", f"cannot auto-step from {task.status}")

    def reconcile(self, task: Task, receipt_ref: str) -> Task:
        """Resolve an unknown outcome with a signed, matching receipt.

        This is the only exit from ``recovery_required``. It does not re-send the
        call, does not refund the reservation, and does not accept a receipt for
        a different round.
        """
        if task.status != "recovery_required":
            raise AgentError("RECONCILIATION_BLOCKED", f"task is {task.status}, not recovery_required")
        payload = self.receipts.load(receipt_ref)
        intent = payload.get("intent") or {}
        if intent.get("task_id") != task.id:
            raise AgentError("MODEL_RECEIPT_MISMATCH", "receipt belongs to a different task")
        if intent.get("round") != task.model.attempts:
            raise AgentError(
                "MODEL_RECEIPT_MISMATCH",
                f"receipt is for round {intent.get('round')}, task is waiting on round {task.model.attempts}",
            )
        plan = validate_plan(payload["plan"])
        updated = task.with_model(
            status="validated",
            proposal=plan.to_dict(),
            receipt={"provider": payload.get("usage", {}).get("provider", "reconciled"), "external_billing": "not-verified"},
            receipt_ref=receipt_ref,
        )
        updated.failure = None
        return self._commit(task, updated.advance("running"), "model_reconciled", {"receipt_ref": receipt_ref})

    def transition(self, task: Task, status: str, event: str, detail: dict[str, Any] | None = None) -> Task:
        """Move the task to a non-terminal status and record why.

        Used by operator controls for ``paused`` around an approval. The
        transition table still decides what is legal; this method only owns the
        event and the sequence bookkeeping.
        """
        return self._commit(task, task.advance(status), event, detail or {})

    def resume(self, task: Task, *, detail: dict[str, Any] | None = None) -> Task:
        """Return a failed task to running so a new loop can continue it.

        Budget exhaustion is the motivating case: the call counters live in the
        store, so raising the limits and resuming continues from the same round
        instead of replaying the reads that were already paid for.
        """
        if task.status != "failed":
            raise AgentError("RESUME_BLOCKED", f"task is {task.status}, not failed")
        cleared = replace(task, failure=None)
        return self._commit(task, cleared.advance("running"), "task_resumed", detail or {})

    def _intent(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Describe one call precisely enough that a receipt can only match it."""
        from ..core.hashing import digest

        return {
            "task_id": task.id,
            "round": task.model.attempts,
            "objective": task.objective,
            "state_hash": digest(context.get("state", {})),
        }

    def _unknown(self, task: Task, intent: dict[str, Any], error: AgentError) -> Task:
        """Record an unknown outcome without retrying or refunding."""
        updated = task.with_model(status="unknown")
        updated.failure = {
            "code": "MODEL_OUTCOME_UNKNOWN",
            "cause": error.code,
            "retryable": False,
            "intent_round": intent["round"],
        }
        return self._commit(
            task,
            updated.advance("recovery_required"),
            "model_unknown",
            {"cause": error.code, "message": error.message},
        )

    def gate(self, task: Task, step: dict[str, Any]) -> GateDecision:
        """Ask the host gate about one step. Never raises for a denial."""
        return self.permissions(task).gate(step["tool"], step.get("arguments") or {})

    def execute(self, task: Task, step: dict[str, Any], *, round_no: int = 0) -> tuple[Task, Observation]:
        """Run one gated read and commit the observation.

        The full result is written into the event payload, not just its hash.
        Without that, a process that dies mid-task could not rebuild what it had
        already learned, and resuming would repeat every read.

        A tool that raises with ``details={"outcome": "unknown"}`` was
        dispatched and never answered. That is not a confirmed failure: the
        same discipline as a model-side unknown applies — the read stays
        charged, the step is never re-sent, and the task ends in a state whose
        code says *unknown*, so no downstream reader mistakes it for a clean
        refusal. Reconciliation for tool-side unknowns (a signed out-of-band
        result, as model receipts do) is deliberately not invented here.
        """
        if task.status == "planned":
            task = self._commit(task, task.advance("running"), "execution_started", {})

        self.budget(task).charge_read()
        task = self._commit(
            task,
            task,
            "tool_started",
            {"tool": step["tool"], "arguments": step.get("arguments") or {}, "round": round_no},
        )
        try:
            observation = self.registry.execute(step["tool"], step.get("arguments") or {})
        except AgentError as error:
            if error.details.get("outcome") == "unknown":
                task = self._commit(task, task, "tool_unknown", {
                    "tool": step["tool"],
                    "code": error.code,
                    "message": error.message,
                    "round": round_no,
                })
                failed = task
                failed.failure = {
                    "code": "TOOL_OUTCOME_UNKNOWN",
                    "cause": error.code,
                    "retryable": False,
                    "intent_round": round_no,
                }
                self._commit(task, failed.advance("failed"), "task_failed", {
                    "code": "TOOL_OUTCOME_UNKNOWN",
                    "cause": error.code,
                })
                raise
            failed = task
            failed.failure = error.to_dict()
            failed = self._commit(failed, failed, "tool_failed", {
                "code": error.code,
                "tool": step["tool"],
                "arguments": step.get("arguments") or {},
                "round": round_no,
            })
            raise
        detail = {
            "tool": observation.tool,
            "arguments": observation.arguments,
            "result": observation.result,
            "result_hash": observation.result_hash,
            "tool_version": observation.tool_version,
            "round": round_no,
            # P0-2 增量字段:旧链无此键,读取按 EXTERNAL_UNTRUSTED 缺省,
            # 哈希校验不受影响(新事件才携带)。
            "trust": trust_for_tool(observation.tool).value,
        }
        return self._commit(task, task, "tool_succeeded", detail), observation

    def verify(self, observation: Observation) -> dict[str, Any]:
        """Delegate independent recomputation to the domain module.

        The kernel does not know how to check a number, a file state or an
        external fact; it only knows that a result is not accepted on the
        model's word. The module supplies the recomputation.
        """
        if self.verifier is None:
            raise AgentError("RESULT_INCOMPLETE", "no verifier is configured for this harness")
        return self.verifier(observation)

    # ------------------------------------------------------------------ helper

    def record(self, task: Task, event: str, detail: dict[str, Any] | None = None) -> Task:
        """Persist a non-terminal change (a plan revision, say) as its own event.

        The plan is state the agent acts on, so it belongs in the chain rather
        than in a side table: a revision that is not recorded cannot be audited.
        """
        return self._commit(task, task, event, detail or {})

    def finalize(
        self,
        task: Task,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        event: str = "task_finished",
        detail: dict[str, Any] | None = None,
    ) -> Task:
        """Move a task to a terminal state and commit it.

        Kept public because the loop owns termination policy while the harness
        owns the mechanics of writing it down.
        """
        updated = task.advance(status)
        if result is not None:
            updated.result = result
        if failure is not None:
            updated.failure = failure
        return self._commit(task, updated, event, detail or {})

    def _commit(self, previous: Task, updated: Task, event: str, detail: dict[str, Any]) -> Task:
        """Advance the sequence and write one event plus the new body."""
        expected = previous.sequence
        updated.sequence = expected + 1
        return self.store.save(updated, expected, event, detail)
