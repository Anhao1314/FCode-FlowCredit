"""Multi-round driver: follow a plan, one action per round.

Three properties are implemented here rather than in the harness.

**The plan is derived, not trusted.** On every run the plan is rebuilt by
replaying the committed observations through the domain planner. The snapshot in
the task body is for display and for resuming quickly; the log is what decides.
A crash between "observation committed" and "plan updated" therefore costs
nothing — the replay catches up on its own.

**Resumption does not repeat work.** If the last committed event is a
reconciliation, the loop executes the already-approved proposal instead of asking
the model again. Asking again would be a second paid call for the same round.

**Termination is the plan's, then the verifier's.** The loop stops when nothing
is open. It only reports success when the runtime recomputes the answer and
agrees, and only reports failure when the plan says a todo is genuinely blocked.
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import PLAN_VERSION, AgentError, Task
from ..core.plan import Plan
from ..memory.context import SourceTrust, WorkingMemory
from ..tools.trust import trust_for_tool
from ..providers.base import Proposal
from ..tools.base import Observation
from .policy import PacketProvider, compile_packet
from .control import Control, Denial
from .harness import Harness, TraceObserver
from .plan_extension import validate_additions

#: Denials the model can fix by reading the error: the tool exists and is
#: allowed, the proposal simply did not fit its schema. Everything else —
#: an unregistered tool, a scope or effect violation — keeps today's semantics
#: (a governance event, and the run stops on it).
REPAIRABLE_DENIALS = ("TOOL_ARGUMENTS_INVALID",)
#: Bounded on purpose: two extra model calls, each charged and recorded.
MAX_PROPOSAL_REPAIRS = 2

__all__ = ["Loop", "NullObserver"]


class NullObserver:
    """A do-nothing observer, so the loop can run headless."""

    def round_start(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None: ...
    def proposal(self, round_no: int, proposal: Any) -> None: ...
    def gate(self, round_no: int, decision: Any) -> None: ...
    def observation(self, round_no: int, observation: Observation) -> None: ...
    def round_end(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None: ...
    def terminal(self, task: Task, result: dict[str, Any] | None) -> None: ...


class Loop:
    """Runs rounds until the plan is settled or the runtime must stop."""

    def __init__(
        self,
        harness: Harness,
        *,
        planner: Any = None,
        max_rounds: int = 12,
        observer: TraceObserver | None = None,
        control: Control | None = None,
        policy: Any = None,
        compiler: Any = None,
        result_tools: tuple[str, ...],
        memory_summarizers: Any = None,
        memory_state_fn: Any = None,
        memory_instruction: str | None = None,
        memory_prompt_profile: str | None = None,
        plan_extension: bool | None = None,
    ) -> None:
        self.harness = harness
        self.planner = planner
        self.max_rounds = max_rounds
        self.observer: TraceObserver = observer or NullObserver()
        self.control = control
        self.policy = policy
        self.compiler = compiler or compile_packet
        #: Tools whose verified observations are the domain's answer. Required:
        #: the kernel must not carry a domain's tool name as a default. Callers
        #: with no result tool pass ``()`` and cannot silently succeed.
        self.result_tools = tuple(result_tools)
        #: Memory profile from the domain: per-tool observation compressors and
        #: the state view. The memory layer stays tool-name-blind; the assembly
        #: hands these in from the ToolSpecs and the DomainModule.
        #: P0-1:按每个 ToolSpec 的 summary_policy 分发压缩策略(custom/default/
        #: raw),而非只收集自定义压缩器——未配置工具不再退化为盲摘要。
        if memory_summarizers is not None:
            self.memory_summarizers = dict(memory_summarizers)
        else:
            from ..memory.context import summarizer_for
            self.memory_summarizers = {
                name: summarizer_for(harness.registry.get(name))
                for name in harness.registry.names
            }
        self.memory_state_fn = (
            memory_state_fn if memory_state_fn is not None else harness.state_fn
        )
        # What the planner is told every round. The general domain writes files
        # and runs commands, so it brings its own wording through the harness;
        # the default stays the read-only kernel text the finance workload is
        # frozen against.
        self.memory_instruction = (
            memory_instruction if memory_instruction is not None else harness.instruction
        )
        #: Which system-prompt profile this run renders against. Threaded to
        #: working memory the same way the instruction is, so the profile is a
        #: property of the *domain the run belongs to* — never of the text the
        #: user typed. ``None`` keeps the default profile and emits no selector.
        self.memory_prompt_profile = (
            memory_prompt_profile if memory_prompt_profile is not None
            else harness.prompt_profile
        )
        #: Whether the model may propose additional plan todos. Comes from the
        #: domain module through the harness; see the loop's plan-addition path.
        self.plan_extension = (
            plan_extension if plan_extension is not None else harness.plan_extension
        )

    def run(self, task_id: str) -> Task:
        task = self.harness.store.load(task_id)
        memory = self.restore(task)
        plan = self.rebuild_plan(task, memory)
        world = getattr(self.harness, "world", None)
        recall_holder = getattr(world, "recall_holder", None) if world is not None else None
        if recall_holder is not None:
            recall_holder["memory"] = memory
        pending = self.pending_step(task)
        repairs = 0

        while True:
            if pending is None and self.control is not None:
                controlled = self.control.before_round(
                    round_no=self._decisions(task) + 1,
                    task=task,
                    plan=plan,
                )
                task, plan = controlled.task, controlled.plan

            if pending is None:
                if plan.is_blocked():
                    return self._stop(task, plan, "PLAN_BLOCKED")
                if plan.is_complete() or plan.next_open() is None:
                    # A plan the domain opened to the model is not finished just
                    # because the steps it currently holds are: the model may
                    # still propose the next one in this same round. It ends by
                    # proposing report_blocked, which is a gated action like any
                    # other. A domain that kept its plan closed has nothing left
                    # to add, so it stops here exactly as before.
                    if not self.plan_extension:
                        return self._stop(task, plan, "PLAN_EXHAUSTED")
                if self._decisions(task) >= self.max_rounds:
                    return self._abort(task_id, AgentError("ROUND_LIMIT", f"no verified result after {self.max_rounds} rounds"))

            # Operator notes (插话) are chain events, so they are refreshed
            # every round: a note that arrived during the previous round is in
            # this round's prompt. Absent notes emit no context key at all.
            memory.operator_messages = self.operator_notes(task)

            budget_snapshot = self.harness.budget(task).snapshot()

            if pending is not None:
                # A reconciled round: the proposal is already approved and paid
                # for. Execute it; do not ask the model again.
                step = pending
                pending = None
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
                        return self._abort(task_id, AgentError(
                            "POLICY_NO_ACTIONS", "the action compiler produced no legal action",
                        ))
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
                            return self._abort(task_id, error)
                        if proposal is None:
                            self.observer.terminal(task, None)
                            return task
                        task = self.harness.record(task, "policy_decided", {
                            "round": round_no,
                            "candidate_id": proposal.metadata.get("candidate_id", ""),
                            "packet_hash": packet.packet_hash,
                            "repaired": bool(proposal.metadata.get("repaired")),
                            "tokens": proposal.tokens,
                            "latency_ms": proposal.latency_ms,
                        })
                    self.observer.proposal(round_no, proposal)
                    step = proposal.plan["steps"][0]
                else:
                    context = memory.build(round_no, budget_snapshot, plan)
                    try:
                        task, proposal = self.harness.propose(task, context)
                    except AgentError as error:
                        return self._abort(task_id, error)
                    if proposal is None:
                        self.observer.terminal(task, None)
                        return task
                    self.observer.proposal(round_no, proposal)
                    # A proposal may ask to extend the plan. The host judges it
                    # before the step runs, and the plan only changes once the
                    # accepted todos are on the chain — so this round's action
                    # and every later round both see the same durable plan.
                    task, plan = self._extend_plan(task, plan, round_no, memory)
                    step = proposal.plan["steps"][0]

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

            decision = self.harness.gate(task, step)
            self.observer.gate(round_no, decision)
            if not decision.allowed:
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
                    continue
                return self._deny(task, decision, plan)

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
                        return task
                    return self._deny(
                        task,
                        Denial(
                            tool=step["tool"],
                            code=approval.code or "OPERATOR_DENIED",
                            reason=approval.reason or "operator denied the call",
                        ),
                        plan,
                    )

            try:
                task, observation = self.harness.execute(task, step, round_no=round_no)
            except AgentError as error:
                return self._abort(task_id, error)

            memory.record(round_no, observation,
                          source_trust=trust_for_tool(observation.tool))
            plan = self.planner.advance(plan, observation) if self.planner else plan
            task = self._persist_plan(task, plan, round_no)

            self.observer.observation(round_no, observation)
            self.observer.round_end(round_no, self.harness.budget(task).snapshot(), plan)

            if observation.tool == "report_blocked":
                return self._reported_blocked(task, plan, observation)
            if observation.tool in self.result_tools and plan.is_complete():
                try:
                    verified = self._verify_result_set(task.id, plan)
                    sources = self._check_evidence(task, verified, round_no)
                    task, obligation = self._verify_obligation(task, verified, plan, round_no)
                except AgentError as error:
                    return self._abort(task.id, error)
                return self._succeed(task, verified, plan, obligation, sources)

    # ----------------------------------------------------------- plan extension

    def _extend_plan(
        self,
        task: Task,
        plan: Plan,
        round_no: int,
        memory: WorkingMemory,
    ) -> tuple[Task, Plan]:
        """Judge a proposal's plan-extension request, and apply it if accepted.

        The host decides — registration, task scope, host grant, effect class and
        the tool's argument schema, all through the same ``Permissions`` gate the
        step itself will face. A refusal is an *event, not a failure*: the action
        proposed alongside is still gated and executed on its own merits, and the
        reason reaches the next round's context (``rejected_proposals``) so the
        model can correct the request rather than repeat it.

        Accepted todos are recorded through the existing ``plan_steered`` path
        *before* they can drive anything, which is also what makes
        :meth:`rebuild_plan` reconstruct them on resume: the replay already takes
        the latest ``plan_steered`` payload as its base. A refusal is recorded as
        ``control_rejected`` with ``by="model"`` — the same event the operator's
        refused plan commands use, because it is the same fact: a plan
        instruction the host declined, with a reason.
        """
        additions = (task.model.proposal or {}).get("additions") or []
        extension = validate_additions(
            additions,
            plan=plan,
            registry=self.harness.registry,
            permissions=self.harness.permissions(task),
            enabled=self.plan_extension,
            result_tools=self.result_tools,
        )
        if not extension.present:
            return task, plan

        if not extension.ok:
            task = self.harness.record(task, "control_rejected", {
                "round": round_no,
                "kind": "plan_extension",
                "by": "model",
                "reason": extension.reason[:200],
                "rejected": [dict(item) for item in extension.rejected],
                "plan_revision": plan.revision,
            })
            memory.rejected_proposals = list(memory.rejected_proposals or []) + [
                {
                    "tool": str(item.get("tool") or "additions"),
                    "code": str(item.get("code") or "PLAN_ADDITION_INVALID"),
                    "message": str(item.get("message") or extension.reason),
                }
                for item in extension.rejected
            ][-3:]
            return task, plan

        # Addition only: nothing is dropped here, so ``revise``'s ``reason``
        # (which explains an abandonment) has nothing to describe.
        revised = plan.revise(add=tuple(extension.accepted))
        task.plan_state = revised.to_dict()
        task = self.harness.record(task, "plan_steered", {
            "round": round_no,
            "action": "extend",
            "by": "model",
            "todo_id": extension.accepted[0].id,
            "added": extension.added_ids(),
            "note": "模型提议新增步骤",
            "plan": revised.to_dict(),
        })
        return task, revised

    # ------------------------------------------------------- result obligations

    def _check_evidence(
        self,
        task: Task,
        verified: dict[str, Any],
        round_no: int,
    ) -> dict[str, Any]:
        """Resolve the artifact's declared source references against the chain.

        What this establishes, precisely: each cited reading **exists** as a
        durable observation, for that tool, of that path, with that content
        digest. What it does not, and cannot: that the claim it is attached to
        actually follows from the source. That is why the verdict is reported as
        ``semantic_truth: "not-verified"`` rather than dressed up — matching a
        hash to a citation is not reading comprehension, and pretending
        otherwise would just move the false confidence one level down.

        A citation that cannot be resolved is different from a claim with no
        citation. The first asserts support that is not there and fails the run;
        the second is merely unverified, and is labelled as such.
        """
        references = list((task.model.proposal or {}).get("evidence") or [])
        if not references:
            return {
                "status": "not-declared",
                "references": [],
                "semantic_truth": "not-verified",
                "note": (
                    "the artifact's contents were hashed, not read for truth: no source "
                    "references were declared, so no claim is corroborated"
                ),
            }
        chain = self.harness.store.events(task.id)
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for reference in references:
            source = self._resolve_reference(chain, reference)
            if source is None:
                unresolved.append({
                    "claim": reference["claim"],
                    "tool": reference["tool"],
                    "path": reference["path"],
                    "sha256": reference["sha256"],
                })
            else:
                resolved.append({
                    "claim": reference["claim"],
                    "tool": reference["tool"],
                    "path": reference["path"],
                    "sha256": reference["sha256"],
                    "observed_round": source.get("round"),
                    "observed_at": source.get("at"),
                })
        task = self.harness.record(task, "evidence_checked", {
            "round": round_no,
            "artifact": self._artifact_state(verified) or {},
            "resolved": len(resolved),
            "unresolved": unresolved,
            "semantic_truth": "not-verified",
        })
        if unresolved:
            raise AgentError(
                "EVIDENCE_REFERENCE_UNRESOLVED",
                "the artifact cites sources that are not in the durable history: "
                + "; ".join(f"{item['path']} ({item['tool']})" for item in unresolved),
            )
        return {
            "status": "resolved",
            "references": resolved,
            "semantic_truth": "not-verified",
            "note": (
                "every cited reading exists on the chain with the recordable digest; "
                "whether each claim follows from its source is not something hashes decide"
            ),
        }

    @staticmethod
    def _resolve_reference(chain: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any] | None:
        """Find the committed observation a citation points at, if it exists.

        Matches on tool, path and the content digest the observation recorded.
        The digest is accepted from ``sha256`` (a read) or ``sha256_after`` (a
        write or an edit), because both are "the content digest the runtime
        recorded for that path at that moment" — the only difference is which
        side of the change it describes.
        """
        for event in chain:
            if event["type"] != "tool_succeeded":
                continue
            detail = event["payload"].get("detail") or {}
            if detail.get("tool") != reference["tool"]:
                continue
            result = detail.get("result") or {}
            if str(result.get("path")) != reference["path"]:
                continue
            digests = {str(result.get("sha256") or ""), str(result.get("sha256_after") or "")}
            if reference["sha256"] not in digests:
                continue
            return {"round": detail.get("round"), "at": event.get("at", "")}
        return None

    def _verify_obligation(
        self,
        task: Task,
        verified: dict[str, Any],
        plan: Plan,
        round_no: int,
    ) -> tuple[Task, dict[str, Any] | None]:
        """Re-run the declared result obligation, and judge what comes back.

        This is the difference between a truthful log and a correct result. The
        command that decides success comes from the *delegation* — validated
        when the run was created and frozen in the task binding — never from a
        proposal, so naming an easier command is not available to the model. The
        runtime then dispatches it through the ordinary gate, budget and event
        path, which is what makes authorization, pre-charging, durability and
        unknown-result semantics apply to verification exactly as they do to
        work.

        The recorded result of the same command during task execution is
        deliberately *not* what is checked: only this fresh run is. A task that
        declares nothing gets ``None`` back, and the terminal result then says
        plainly that only file state was verified.
        """
        scope = self.harness.verification
        if scope is None:
            return task, None
        obligation = scope.obligation

        artifact = self._artifact_state(verified)
        try:
            snapshot = scope.prepare(artifact)
        except AgentError:
            task = self._record_verification_rejected(
                task, obligation, round_no, "the workspace could not be frozen",
            )
            raise
        try:
            task = self.harness.record(task, "verification_obligation", {
                "round": round_no,
                "name": obligation.name,
                "tool": obligation.tool,
                "arguments": dict(obligation.arguments),
                "expect": dict(obligation.expect),
                # Where the criterion came from. The whole point of the field is
                # that a reader can see it was the host's, not the model's.
                "source": obligation.source,
                "artifact": dict(artifact or {}),
                "snapshot": dict(snapshot),
                "plan_revision": plan.revision,
            })

            step = obligation.step()
            decision = self.harness.gate(task, step)
            self.observer.gate(round_no, decision)
            if not decision.allowed:
                # Refused *before* execution: no dispatch, no read charged, and
                # the record names the gate's own code.
                task = self._record_verification_rejected(
                    task, obligation, round_no, decision.code,
                )
                raise AgentError(
                    "VERIFICATION_NOT_AUTHORIZED",
                    f"the declared obligation {obligation.name!r} is not authorized: {decision.reason}",
                )
            # A timeout or an unknown outcome fails the task closed on the
            # ordinary path before it raises, so the chain already carries
            # `tool_unknown` and `task_failed` with the cause — and nothing is
            # ever re-dispatched. A second refusal record here would only
            # restate it, against a sequence that path has already advanced.
            task, observation = self.harness.execute(task, step, round_no=round_no)
            try:
                item = self.harness.verify(observation)
            except AgentError as error:
                # The domain refused the result: the declared check did not
                # pass. Recorded before the run dies, because a refusal that
                # leaves no trace is indistinguishable from a pass.
                task = self._record_verification_rejected(
                    task, obligation, round_no, error.message,
                )
                raise
            if not item.get("verified"):  # pragma: no cover - verifiers raise instead
                task = self._record_verification_rejected(
                    task, obligation, round_no, "the verifier returned no verdict",
                )
                raise AgentError(
                    "RESULT_VERIFICATION_FAILED",
                    f"the declared obligation {obligation.name!r} was not verified",
                )
        finally:
            scope.release()

        outcome = dict(item["result"])
        outcome["snapshot_sha256"] = snapshot.get("sha256")
        outcome["artifact_matched"] = (snapshot.get("artifact") or {}).get("matched")
        task = self.harness.record(task, "verification_result", {
            "round": round_no,
            "name": obligation.name,
            "verified": True,
            "verification": item.get("verification"),
            "exit_code": outcome.get("exit_code"),
            "expect_exit_code": outcome.get("expect_exit_code"),
            "result_hash": outcome.get("result_hash"),
            "snapshot_sha256": snapshot.get("sha256"),
        })
        return task, outcome

    def _record_verification_rejected(
        self,
        task: Task,
        obligation: Any,
        round_no: int,
        reason: str,
    ) -> Task:
        """Write down a verification that did not happen, and why.

        A refusal that leaves no trace is indistinguishable from a check that
        passed, which is the failure mode this whole phase exists to remove.
        """
        return self.harness.record(task, "verification_result", {
            "round": round_no,
            "name": obligation.name,
            "verified": False,
            "reason": str(reason)[:200],
            "expect": dict(obligation.expect),
            "source": obligation.source,
        })

    @staticmethod
    def _artifact_state(verified: dict[str, Any]) -> dict[str, Any] | None:
        """The artifact the verifier just accepted, as (path, sha256).

        The single-result shape carries its fields under ``result``; the
        multi-result shape is already a list of those inner dicts.
        """
        if "results" in verified:
            for item in verified["results"]:
                if item.get("path"):
                    return {"path": item.get("path"), "sha256": item.get("sha256")}
            return None
        item = verified.get("result") or {}
        if item.get("path"):
            return {"path": item.get("path"), "sha256": item.get("sha256")}
        return None

    # -------------------------------------------------------------- resumption

    def _decisions(self, task: Task) -> int:
        """Model calls plus host auto-actions: the true round count."""
        auto = sum(
            1 for event in self.harness.store.events(task.id)
            if event["type"] == "auto_action"
        )
        return task.model.attempts + auto

    #: Operator notes are steering input, not documents: the prompt keeps the
    #: most recent few, and the store keeps every one of them.
    OPERATOR_NOTES = 8
    OPERATOR_NOTE_CHARS = 600

    def operator_notes(self, task: Task) -> list[dict[str, Any]]:
        """The operator's mid-run notes, read back out of the chain.

        They are events like everything else, so the prompt gets them from the
        same log the rest of the run comes from, and a note queued at a round
        boundary is in that round's prompt. The console used to promise
        "message will reach the agent next round" while the text only reached
        the chain; this is the promise, in code.
        """
        notes: list[dict[str, Any]] = []
        for event in self.harness.store.events(task.id):
            if event["type"] != "operator_message":
                continue
            detail = event["payload"].get("detail") or {}
            text = str(detail.get("text") or "").strip()
            if not text:
                continue
            note: dict[str, Any] = {
                "round": int(detail.get("round") or 0),
                "text": text[: self.OPERATOR_NOTE_CHARS],
            }
            if detail.get("steered"):
                note["steered"] = True
            notes.append(note)
        return notes[-self.OPERATOR_NOTES :]

    def restore(self, task: Task) -> WorkingMemory:
        """Rebuild working memory from committed observations."""
        prior_cases: list[dict[str, Any]] = []
        # Chat → work bridge: the conversation this delegation was handed off
        # from, frozen in the task binding. Sanitised by the console; filtered
        # again here so a tampered binding cannot smuggle arbitrary roles.
        spec = (task.binding or {}).get("spec") or {}
        origin = spec.get("origin_thread") if isinstance(spec, dict) else None
        conversation = None
        if isinstance(origin, dict):
            raw_messages = origin.get("messages")
            if isinstance(raw_messages, list):
                conversation = [
                    {"role": item.get("role"), "text": item.get("text")}
                    for item in raw_messages
                    if isinstance(item, dict)
                    and item.get("role") in ("user", "assistant")
                    and isinstance(item.get("text"), str)
                    and item.get("text")
                ]
        memory = WorkingMemory(
            objective=task.objective,
            tool_catalog=self.harness.registry.catalog(),
            summarizers=dict(self.memory_summarizers or {}),
            state_fn=self.memory_state_fn,
            conversation=conversation,
            instruction=self.memory_instruction,
            prompt_profile=self.memory_prompt_profile,
        )
        for event in self.harness.store.events(task.id):
            if event["type"] == "memory_recalled":
                detail = event["payload"].get("detail") or {}
                prior_cases = list(detail.get("hits") or [])
                continue
            if event["type"] != "tool_succeeded":
                continue
            detail = event["payload"].get("detail", {})
            # P0-2:旧事件无 trust 键 → EXTERNAL_UNTRUSTED(fail-closed 缺省)。
            source_trust = SourceTrust(
                detail.get("trust") or SourceTrust.EXTERNAL_UNTRUSTED.value)
            memory.record(
                int(detail.get("round", 0)),
                Observation(
                    tool=detail["tool"],
                    arguments=detail.get("arguments", {}),
                    result=detail["result"],
                    result_hash=detail["result_hash"],
                ),
                source_trust=source_trust,
            )
        memory.prior_cases = prior_cases
        return memory

    def rebuild_plan(self, task: Task, memory: WorkingMemory) -> Plan:
        """Derive the current plan from the log, including operator revisions.

        The stored snapshot is not trusted here. If a process died between
        committing an observation and updating the plan, the snapshot is one
        step behind; the replay is always right. When an operator revised the
        plan, the latest revision becomes the base and only observations after
        it are replayed — otherwise a restart would silently undo the decision.
        """
        if self.planner is None:
            return Plan(goal=task.objective)
        events = self.harness.store.events(task.id)
        base = Plan.from_dict(task.plan_state) if task.plan_state else self.planner.decompose(task.objective)
        revised_after: int | None = None
        for event in events:
            if event["type"] != "plan_steered":
                continue
            detail = event["payload"].get("detail") or {}
            if detail.get("plan"):
                base = Plan.from_dict(detail["plan"])
                revised_after = int(event["sequence"])
        for event in events:
            if revised_after is not None and int(event["sequence"]) <= revised_after:
                continue
            if event["type"] != "tool_succeeded":
                continue
            detail = event["payload"].get("detail", {})
            base = self.planner.advance(
                base,
                Observation(
                    tool=detail["tool"],
                    arguments=detail.get("arguments", {}),
                    result=detail["result"],
                    result_hash=detail["result_hash"],
                ),
            )
        return base

    def pending_step(self, task: Task) -> dict[str, Any] | None:
        """Return an approved-but-unexecuted step, if reconciliation left one."""
        if task.model.status != "validated" or not task.model.proposal:
            return None
        events = self.harness.store.events(task.id)
        if not events or events[-1]["type"] != "model_reconciled":
            return None
        return task.model.proposal["steps"][0]

    # --------------------------------------------------------------- terminals

    def _persist_plan(self, task: Task, plan: Plan, round_no: int) -> Task:
        task.plan_state = plan.to_dict()
        return self.harness.record(task, "plan_updated", {"round": round_no, "plan": plan.to_dict()})

    def _verify_result_set(self, task_id: str, plan: Plan) -> dict[str, Any]:
        """Verify every declared result observation, not just the last one.

        Required and optional are todo-level properties. A required result todo
        must be ``done`` and its observation must compute and independently
        verify. An optional result todo may be ``done``, ``abandoned`` or
        ``blocked``; a ``computed: false`` optional observation is a reported
        gap, not a verification failure. Any observation that *does* compute is
        verified, optional or not — optional never means unchecked.

        A plan can be ``is_complete()`` while a required result-bearing todo was
        ``abandoned``; that is "finished", not "evidence complete". The todo
        check below turns that distinction into a stable failure instead of a
        partial answer labelled succeeded.
        """
        result_todos = [todo for todo in plan.todos if todo.tool in self.result_tools]
        required = [todo for todo in result_todos if todo.required]
        optional = [todo for todo in result_todos if not todo.required]

        incomplete = [todo.id for todo in required if todo.status != "done"]
        if incomplete:
            raise AgentError(
                "RESULT_INCOMPLETE",
                f"required result todos are not done: {', '.join(incomplete)}",
            )
        invalid_optional = [
            todo.id
            for todo in optional
            if todo.status not in ("done", "abandoned", "blocked")
        ]
        if invalid_optional:
            raise AgentError(
                "RESULT_INCOMPLETE",
                f"optional result todos are still open: {', '.join(invalid_optional)}",
            )

        observations = self._result_observations(task_id)
        for todo in required:
            if not any(self._same_result_step(todo, observation) for observation in observations):
                raise AgentError(
                    "RESULT_INCOMPLETE",
                    f"no observation for required result todo {todo.id}",
                )

        verified: list[dict[str, Any]] = []
        for observation in observations:
            todo = self._result_todo_for_observation(result_todos, observation)
            computed = observation.result.get("computed") is not False
            if not computed:
                if todo is not None and not todo.required:
                    continue
                label = todo.id if todo is not None else "unplanned result"
                raise AgentError(
                    "RESULT_INCOMPLETE",
                    f"required result {label} was not computed",
                )
            item = self.harness.verify(observation)
            if not item.get("verified"):
                raise AgentError(
                    "RESULT_VERIFICATION_FAILED",
                    f"the domain verifier rejected {observation.tool!r}",
                )
            if not isinstance(item.get("result"), dict):
                raise AgentError(
                    "RESULT_INCOMPLETE",
                    f"the domain verifier returned no result for {observation.tool!r}",
                )
            verified.append(item)

        if not verified:
            raise AgentError("RESULT_INCOMPLETE", "no result observations to verify")

        # One verified observation keeps the historical single-result shape
        # (a single-result domain's field set). Multiple observations use a
        # deterministic list ordered by the committed event sequence.
        if len(verified) == 1:
            return verified[0]
        return {
            "verified": True,
            "verification": verified[0].get("verification", "runtime-recomputed"),
            "results": [dict(item["result"]) for item in verified],
        }

    def _same_result_step(self, todo: Any, observation: Observation) -> bool:
        """True when an observation is the step a result todo names.

        Binding is by *identity*: the tool, and the arguments that name the
        target (a path, a doc id, a URL). Arguments the proposer authors — file
        content, edit anchors — are skipped, because a model that writes a
        *better* artifact than the plan composed must not fail the run as
        RESULT_INCOMPLETE: the file exists, and its bytes are verified by hash
        against the receipt. Tools that declare no identity arguments keep the
        strict comparison.
        """
        if todo.tool != observation.tool:
            return False
        spec = None
        try:
            spec = self.harness.registry.get(todo.tool)
        except AgentError:
            spec = None
        if spec is None:
            return all(
                observation.arguments.get(key) == value
                for key, value in todo.arguments.items()
            )
        authored = {param.name for param in spec.params if not param.identity}
        declared = {param.name for param in spec.params}
        for key, value in todo.arguments.items():
            if key in declared and key in authored:
                continue
            if observation.arguments.get(key) != value:
                return False
        return True

    def _result_todo_for_observation(
        self,
        result_todos: list[Any],
        observation: Observation,
    ) -> Any | None:
        for todo in result_todos:
            if self._same_result_step(todo, observation):
                return todo
        return None

    def _result_observations(self, task_id: str) -> list[Observation]:
        """Read every committed observation produced by a declared result tool."""
        observations: list[Observation] = []
        for event in self.harness.store.events(task_id):
            if event["type"] != "tool_succeeded":
                continue
            detail = event["payload"].get("detail", {})
            if detail.get("tool") not in self.result_tools:
                continue
            observations.append(
                Observation(
                    tool=detail["tool"],
                    arguments=dict(detail.get("arguments") or {}),
                    result=dict(detail.get("result") or {}),
                    result_hash=detail.get("result_hash", ""),
                    tool_version=detail.get("tool_version", ""),
                )
            )
        return observations

    def _succeed(
        self,
        task: Task,
        verified: dict[str, Any],
        plan: Plan,
        obligation: dict[str, Any] | None = None,
        sources: dict[str, Any] | None = None,
    ) -> Task:
        if "results" in verified:
            result = {
                "verified": True,
                "verification": verified.get("verification", "runtime-recomputed"),
                "results": [dict(item) for item in verified["results"]],
            }
        else:
            result = dict(verified["result"])
            result["verification"] = verified["verification"]
        result["plan"] = plan.progress()
        # What was independent here, and what was not, kept separate so a reader
        # never has to infer it. ``file_state`` is the artifact recompute; the
        # declared obligation (when there is one) is a re-executed *result*;
        # and no amount of hashing establishes that prose is true.
        result["file_state"] = verified.get("verification", "runtime-recomputed")
        result["obligations"] = [dict(obligation)] if obligation else []
        # Three separate questions, kept separate, because conflating them is
        # exactly how "the file was written" became "the work was correct".
        result["source_links"] = (sources or {}).get("status", "not-declared")
        result["sources"] = list((sources or {}).get("references") or [])
        result["semantic_truth"] = (sources or {}).get("semantic_truth", "not-verified")
        if sources and sources.get("note"):
            result["truth_note"] = sources["note"]
        if obligation:
            result["result_truth"] = "runtime-recomputed-command-result"
        else:
            result["result_truth"] = "no-result-obligation-declared"
        task = self.harness.finalize(task, "verifying", result=None, event="verification_started")
        task = self.harness.finalize(task, "succeeded", result=result, event="task_succeeded")
        self.observer.terminal(task, result)
        return task

    def _deny(self, task: Task, decision: Any, plan: Plan) -> Task:
        task = self.harness.finalize(
            task,
            "denied",
            failure={"code": decision.code, "message": decision.reason, "retryable": False},
            event="tool_denied",
            detail={"tool": decision.tool, "code": decision.code, "plan": plan.progress()},
        )
        self.observer.terminal(task, None)
        return task

    def _reported_blocked(self, task: Task, plan: Plan, observation: Observation) -> Task:
        """The model says the evidence is insufficient; stop, do not re-ask.

        The system prompt allows this exit, and the tool is host-gated like any
        other. Continuing to pull unrelated todos after the model has declared
        the goal unreachable wastes budget and makes the declared stop a lie.
        The failure is still a failure: the verifier never runs, and the plan's
        own blocked reasons are preferred over the model's enum.
        """
        blocked = [todo.to_dict() for todo in plan.todos if todo.status == "blocked"]
        model_reason = str(observation.result.get("reason") or "insufficient_evidence")
        domain_reasons = "; ".join(item["reason"] for item in blocked if item.get("reason"))
        if domain_reasons:
            code, message = "INSUFFICIENT_EVIDENCE", domain_reasons
        elif model_reason == "budget_insufficient":
            code, message = "BUDGET_EXHAUSTED", "the remaining budget cannot cover the plan"
        else:
            code, message = "INSUFFICIENT_EVIDENCE", model_reason
        task = self.harness.finalize(
            task,
            "failed",
            failure={
                "code": code,
                "message": message,
                "retryable": False,
            },
            event="task_stopped",
            detail={
                "code": "MODEL_REPORTED_BLOCKED",
                "model_reason": model_reason,
                "plan": plan.progress(),
                "blocked": blocked,
            },
        )
        self.observer.terminal(task, None)
        return task

    def _stop(self, task: Task, plan: Plan, code: str) -> Task:
        blocked = [todo.to_dict() for todo in plan.todos if todo.status == "blocked"]
        task = self.harness.finalize(
            task,
            "failed",
            failure={
                "code": "INSUFFICIENT_EVIDENCE" if code == "PLAN_BLOCKED" else "PLAN_INCOMPLETE",
                "message": "; ".join(item["reason"] for item in blocked if item["reason"]) or "the plan has no remaining action",
                "retryable": False,
            },
            event="task_stopped",
            detail={"code": code, "plan": plan.progress(), "blocked": blocked},
        )
        self.observer.terminal(task, None)
        return task

    def _abort(self, task_id: str, error: AgentError) -> Task:
        task = self.harness.store.load(task_id)
        if task.status in ("succeeded", "denied", "failed", "recovery_required"):
            self.observer.terminal(task, None)
            return task
        task = self.harness.finalize(
            task,
            "failed",
            failure=error.to_dict(),
            event="task_failed",
            detail={"code": error.code},
        )
        self.observer.terminal(task, None)
        return task
