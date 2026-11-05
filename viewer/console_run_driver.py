"""One live run's assembly, execution and publication phases.

RunManager owns history and the public lifecycle API. This driver owns only the
work that used to be packed into its 350-line ``_run_thread`` method.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from typing import Any

from console_common import DOMAIN_KEY, TERMINAL_STATUS, _scaled_budget_for_prices
from console_run_budget import budget_summary
from console_runtime import LiveObserver, RunControl, ScopedPlanner, StopRequested, _run_crash, _watch


class RunDriver:
    def __init__(self, manager, run, *, resume_limits=None, extra_rounds: int = 6) -> None:
        self.manager = manager
        self.run = run
        self.rt = manager.runtime
        self.resume_limits = resume_limits
        self.extra_rounds = extra_rounds
        self.store = None
        self.watcher = None
        self.sandbox = None
        self.ask_holder: dict[str, Any] = {}
        self.issued: dict[str, str] = {}

    def execute(self) -> None:
        assert self.rt is not None
        try:
            self._assemble()
            task = self._prepare_task()
            status = self._execute_task(task)
            self._publish(task, status)
        except Exception as exc:  # noqa: BLE001 — setup failures belong on the run
            traceback.print_exc()
            with self.run.lock:
                self.run.status = "error"
                self.run.error = f"{type(exc).__name__}: {exc}"
                self.run.finished = True
                self.run.bump()
        finally:
            self._cleanup()

    # --------------------------------------------------------------- assembly

    def _assemble(self) -> None:
        rt = self.rt
        self.scenario = rt.get_scenario(self.run.template)
        self.run_dir = self.manager.runs_dir / self.run.id
        self.db = self.run_dir / "task.sqlite"
        self.store = rt.TaskStore(self.db)
        self.module = rt.get_domain(self.scenario.module)
        self.general = self.module.key != DOMAIN_KEY
        self.world = self._build_world()
        self.scope = list(((self.run.spec or {}).get("scope") or []))
        if self.scope and hasattr(self.world, "documents"):
            selected = set(self.scope)
            unknown = selected - set(self.world.documents)
            if unknown:
                raise ValueError(f"scope contains unknown documents: {', '.join(sorted(unknown))}")
            self.world.documents = {
                doc_id: self.world.documents[doc_id] for doc_id in selected
            }
        native_tools = self.module.build_tools(
            self.world, cases=self.manager.case_index, facts=self.manager.fact_store,
        )
        self.mcp_bridge = self.manager.mcp.get("bridge") if self.general else None
        mcp_specs = self.mcp_bridge.specs() if self.mcp_bridge else []
        self.extra_allowed = [spec.name for spec in mcp_specs]
        registry = rt.ToolRegistry(native_tools + mcp_specs)
        self.receipts = rt.ReceiptStore(self.store)
        limits = self._limits()
        provider = self.manager._build_provider(
            rt, self.run, self.scenario, self.receipts, self._issue,
        )
        if self.run.model != "mock":
            price = getattr(provider, "price", None)
            max_completion = int(getattr(provider, "max_completion_tokens", 512) or 512)
            if price is not None:
                limits = rt.BudgetLimits(**_scaled_budget_for_prices(limits, price, max_completion))
        self.limits = limits
        self.harness = rt.Harness(
            store=self.store,
            registry=registry,
            provider=provider,
            world=self.world,
            limits=limits,
            receipts=self.receipts,
            verifier=lambda observation: self.module.verify(self.world, observation),
            state_fn=self.module.summarize_state,
            instruction=self.module.instruction,
            prompt_profile=self.module.prompt_profile,
            plan_extension=self.module.plan_extension,
            verification=(
                self.module.make_verification(self.world)
                if self.module.make_verification is not None else None
            ),
        )
        self.recorder = rt.TraceRecorder(
            self.store, provider_name=provider.name, model_name=provider.model,
        )
        if self.resume_limits:
            previous = self.run_dir / "trace.json"
            if previous.exists():
                try:
                    self.recorder.seed(json.loads(previous.read_text(encoding="utf-8")).get("rounds") or [])
                except Exception:  # noqa: BLE001 — a damaged old trace must not block resume
                    pass
        approval_tools = tuple(self.scenario.approval_tools)
        if self.mcp_bridge is not None:
            approval_tools += tuple(self.mcp_bridge.approval_names())
        self.control = RunControl(
            self.run, self.harness, rt, approval_tools, self.manager.approval_timeout,
            preview=self._approval_preview if self.sandbox is not None else None,
            classify_consequential=self._classify_consequential,
        )
        self.run.controller = self.control
        if self.general:
            self.ask_holder["control"] = self.control

    def _build_world(self):
        kwargs = {}
        if self.general:
            self.sandbox = self.rt.WorkspaceSandbox(self.run_dir / "workspace")
            project_spec = (self.run.spec or {}).get("project")
            if project_spec:
                project_path = self.run_dir / "project.json"
                if project_path.is_file():
                    self.project = json.loads(project_path.read_text(encoding="utf-8"))
                else:
                    self.project = self.rt.import_project(project_spec["path"], self.sandbox)
                    project_path.write_text(
                        json.dumps(self.project, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
            kwargs = {
                "sandbox": self.sandbox,
                "command_config": os.environ.get("FCODE_COMMANDS_CONFIG")
                or str(self.rt.repo / "commands.json"),
                "ask_operator": self._ask_channel,
            }
        return self.module.create_world(
            partial=self.scenario.partial_corpus, spec=self.run.spec or {}, **kwargs,
        )

    def _ask_channel(self, **kwargs: Any) -> dict[str, Any]:
        control = self.ask_holder.get("control")
        if control is None:
            return {"answered": False, "reason": "this deployment has no operator channel"}
        return control.ask(**kwargs)

    def _issue(self, **kwargs):
        ref = self.receipts.issue(**kwargs)
        self.issued["ref"] = ref
        return ref

    def _limits(self):
        scenario = self.scenario
        limits = scenario.limits
        spec = self.run.spec or {}
        if spec.get("project") and not self.resume_limits:
            # More room to inspect and repair a real repository; the operator's
            # hard cost ceiling and the provider price scaling still apply.
            limits = self.rt.BudgetLimits(
                max_calls=max(24, scenario.limits.max_calls),
                max_reads=max(64, scenario.limits.max_reads),
                max_cost_microusd=max(500_000, scenario.limits.max_cost_microusd),
                max_tokens=max(120_000, scenario.limits.max_tokens),
            )
        override = spec.get("budget") if not self.resume_limits else None
        if override:
            limits = self.rt.BudgetLimits(
                max_calls=int(override["maxCalls"]),
                max_reads=int(override["maxReads"]),
                max_cost_microusd=int(override["maxCostMicrousd"]),
                max_tokens=limits.max_tokens,
            )
        if self.resume_limits:
            limits = self.rt.BudgetLimits(
                max_calls=int(self.resume_limits["maxCalls"]),
                max_reads=int(self.resume_limits["maxReads"]),
                max_cost_microusd=int(self.resume_limits["maxCostMicrousd"]),
                max_tokens=int(self.resume_limits.get("maxTokens") or scenario.limits.max_tokens),
            )
        return limits

    def _approval_preview(self, tool: str, arguments: dict) -> dict | None:
        if self.sandbox is None:
            return None
        if tool == "write_file":
            path = str(arguments.get("path") or "")
            content = str(arguments.get("content") or "")
            if not path or not content:
                return None
            try:
                return self.sandbox.diff_preview(path, content)
            except Exception as error:  # noqa: BLE001 — preview refusals must be visible
                return {"path": path, "created": True, "additions": 0, "removals": 0,
                        "truncated": False, "diff": "",
                        "error": f"{getattr(error, 'code', type(error).__name__)}: {error}"}
        if tool == "edit_file":
            path = str(arguments.get("path") or "")
            old_text = str(arguments.get("old_text") or "")
            if not path or not old_text:
                return None
            try:
                return self.sandbox.edit_preview(path, old_text, str(arguments.get("new_text") or ""))
            except Exception as error:  # noqa: BLE001
                return {"path": path, "created": False, "additions": 0, "removals": 0,
                        "truncated": False, "diff": "",
                        "error": f"{getattr(error, 'code', type(error).__name__)}: {error}"}
        if tool == "run_command":
            command = str(arguments.get("command") or "")
            if not command:
                return None
            argv = [command, *(arguments.get("args") or [])]
            return {
                "command": " ".join(argv),
                "timeout": arguments.get("timeout_seconds") or 30,
                "note": "白名单内命令；无 shell、参数逐个校验；超时记 unknown 不重跑",
            }
        return None

    def _classify_consequential(self, tool: str, arguments: dict) -> bool:
        if tool != "run_command":
            return True
        runner = getattr(self.world, "command_runner", None)
        if runner is None:
            return True
        verdict = runner.policy.classify(
            str(arguments.get("command") or ""), list(arguments.get("args") or []), runner.sandbox,
        )
        return True if verdict is None else verdict

    # --------------------------------------------------------------- execution

    def _prepare_task(self):
        if self.resume_limits:
            task = self.store.load(self.run.task_id)
            task = self.harness.resume(
                task,
                detail={"by": "operator", "reason": "budget topped up", "limits": self.limits.to_dict()},
            )
            self.max_rounds = max(self.scenario.max_rounds, task.model.attempts + self.extra_rounds)
        else:
            binding = {
                "scenario": self.scenario.key,
                "module": self.module.key,
                "source": "viewer-live",
                **({"spec": self.run.spec} if self.run.spec else {}),
            }
            if self.general:
                binding["workspace"] = "workspace"
            if self.extra_allowed:
                binding["mcp_imports"] = self.extra_allowed
            grant = dict(self.scenario.grant)
            if self.extra_allowed:
                grant["allowed_tools"] = list(grant.get("allowed_tools") or []) + self.extra_allowed
            task = self.harness.create(
                task_id=f"task-{self.run.id}",
                objective=self.run.objective,
                principal="console-operator",
                binding=binding,
                allowed_tools=list(self.scenario.allowed_tools) + self.extra_allowed,
                grant=grant,
            )
            if self.manager.case_index is not None:
                try:
                    hits = self.manager.case_index.search(self.run.objective, limit=3)
                    task = self.harness.record_recall(task, hits)
                except Exception:  # noqa: BLE001 — memory must never kill a run
                    traceback.print_exc()
            self.max_rounds = max(24, self.scenario.max_rounds) if (self.run.spec or {}).get("project") else self.scenario.max_rounds
        self.run.task_id = task.id
        self.observer = LiveObserver(self.run, self.recorder, self.store, task.id)
        self.watcher = threading.Thread(
            target=_watch,
            args=(self.run, self.recorder, self.store, self.db, task.id),
            name=f"watch:{self.run.id}",
            daemon=True,
        )
        self.watcher.start()
        base_planner = self.module.make_planner(self.world)
        self.planner = ScopedPlanner(self.rt, base_planner, set(self.scope)) if self.scope else base_planner
        self.loop = self._new_loop()
        if self.resume_limits:
            self.world.apply_state(self.loop.restore(task).state())
        return task

    def _new_loop(self):
        return self.rt.Loop(
            self.harness,
            planner=self.planner,
            max_rounds=self.max_rounds,
            observer=self.observer,
            control=self.control,
            result_tools=self.module.result_tools,
        )

    def _execute_task(self, task) -> str:
        try:
            if self.scenario.crash_at:
                task = _run_crash(
                    self.rt, self.harness, self.store, self.world, self.scenario, task, self.observer, self.db,
                )
            else:
                task = self.loop.run(task.id)
            if self.scenario.reconcile and task.status == "recovery_required":
                try:
                    self.harness.reconcile(task, "0" * 64)
                except self.rt.AgentError:
                    pass
                task = self.harness.reconcile(task, self.issued.get("ref", ""))
                task = self._new_loop().run(task.id)
            return task.status
        except StopRequested:
            task = self.store.load(task.id)
            if task.status not in TERMINAL_STATUS:
                task = self.harness.finalize(
                    task, "failed",
                    failure={"code": "STOPPED_BY_OPERATOR", "message": "操作员在轮次边界停止了这次运行", "retryable": False},
                    event="task_failed", detail={"code": "STOPPED_BY_OPERATOR"},
                )
            self.observer.terminal(task, None)
            return "stopped"
        except Exception as exc:  # noqa: BLE001 — surface, never swallow
            traceback.print_exc()
            self.run.error = f"{type(exc).__name__}: {exc}"
            task = self.store.load(task.id)
            if task.status not in TERMINAL_STATUS:
                # A failure before planning cannot use created -> failed. Keep
                # the task store's state machine intact and report the run as
                # error instead of masking the original error with a second
                # ILLEGAL_STATE_TRANSITION exception.
                target = "denied" if task.status == "created" else "failed"
                task = self.harness.finalize(
                    task, target,
                    failure={"code": "VIEWER_RUNNER_ERROR", "message": str(exc)[:300], "retryable": False},
                    event="tool_denied" if target == "denied" else "task_failed",
                    detail={"code": "VIEWER_RUNNER_ERROR"},
                )
            self.observer.terminal(task, None)
            return "error"

    # -------------------------------------------------------------- publication

    def _publish(self, task, status: str) -> None:
        final = self.recorder.build(self.store.load(task.id))
        if getattr(self, "project", None):
            delivery = self.rt.project_delivery(self.sandbox, self.project)
            patch = delivery.pop("patch")
            (self.run_dir / "changes.patch").write_text(patch, encoding="utf-8")
            (self.run_dir / "project.json").write_text(
                json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            self.project = delivery
            final["project"] = delivery
        self.manager._ingest_memory(self.rt, self.run, final)
        trace_path = self.run_dir / "trace.json"
        self.rt.export_trace(final, trace_path)
        try:
            verification = self.rt.verify_trace(final)
            verified = bool(verification.get("ok", verification.get("valid", True)))
        except Exception:  # verification must never kill a finished run
            verified = False
        with self.run.lock:
            self.run.snapshot = final
            self.run.status = status
            self.run.rounds = len(final["rounds"])
            self.run.event_count = len(final["events"])
            self.run.trace_path = f"runs/{self.run.id}/trace.json"
            self.run.trace_verified = verified
            self.run.cost = budget_summary(final)
            self.run.finished = True
            self.run.bump()

    def _cleanup(self) -> None:
        with self.run.lock:
            if self.run.pending is not None:
                self.run.pending = None
                self.run.bump()
            self.run.controller = None
        self.manager._write_meta(self.run)
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
