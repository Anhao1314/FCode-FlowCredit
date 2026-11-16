"""One live run's assembly, execution and publication phases.

RunManager owns history and the public lifecycle API. This driver owns only the
work that used to be packed into its 350-line ``_run_thread`` method.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from pathlib import Path
from typing import Any

from console_common import DOMAIN_KEY, TERMINAL_STATUS, _scaled_budget_for_prices
from console_budget_extension import BudgetChannel
from console_subagents import SubagentChannel
from console_run_budget import budget_summary
from console_runtime import LiveObserver, RunControl, ScopedPlanner, StopRequested, _run_crash, _watch
from flowagent.capabilities.motion import build_motion_capability
from flowagent.providers.prompt import GENERAL_FAST_INSTRUCTION, PROMPT_PROFILES
from flowagent.runtime.capability import HarnessCapabilityBridge
from flowagent.tools.general.motion_quality import analyze_motion, merge_quality
from motion_frames import HOST_VERIFY_TIMES_MS, MotionProbe
from project_instructions import (
    collect_project_instructions,
    instruction_block,
    instructions_binding,
)


def filter_toolset(spec: dict | None, allowed_tools: list[str]) -> tuple[list[str], list[str]]:
    """Narrow a run's tools to the task's requested toolset.

    A narrower schema is both a permission boundary and a token saving: every
    tool schema is sent on every model call. An empty/absent request keeps the
    full scenario set, so existing runs are unchanged. A requested name the
    scenario does not grant is an error, never silently dropped.
    """
    requested = [
        str(name).strip()
        for name in ((spec or {}).get("toolset") or [])
        if str(name).strip()
    ]
    if not requested:
        return list(allowed_tools), []
    missing = [name for name in requested if name not in allowed_tools]
    if missing:
        raise ValueError(f"toolset contains tools this run cannot use: {missing}")
    narrowed = [name for name in allowed_tools if name in set(requested)]
    return narrowed, requested


class RunDriver:
    # Some recovery tests construct through __new__ to isolate _prepare_task.
    recover_pending = None

    def __init__(
        self,
        manager,
        run,
        *,
        resume_limits=None,
        extra_rounds: int = 6,
        recover_unknown: bool = False,
        recover_pending=None,
    ) -> None:
        self.manager = manager
        self.run = run
        self.rt = manager.runtime
        self.resume_limits = resume_limits
        self.extra_rounds = extra_rounds
        #: Operator-confirmed recovery of an unknown model outcome. It reuses
        #: the operator's limits (and trace seeding) but must NOT go through
        #: ``harness.resume`` — that entry only accepts ``failed`` tasks.
        self.recover_unknown = recover_unknown
        #: A durable approval/question restored after a process restart. The
        #: original chain has the authority; no model call is repeated here.
        self.recover_pending = dict(recover_pending) if recover_pending else None
        #: Opt-in fast experiment: batch actions per model round, native tool
        #: calling, operator envelope without price-scaled call shrink.
        self.fast = bool((getattr(run, "spec", None) or {}).get("fast")) \
            or os.environ.get("FCODE_FAST_MODE") == "1"
        self.store = None
        self.watcher = None
        self.sandbox = None
        self.ask_holder: dict[str, Any] = {}
        self.issued: dict[str, str] = {}
        self.capability_result = None
        self.capability_quality = None

    def execute(self) -> None:
        assert self.rt is not None
        try:
            self._assemble()
            task = self._prepare_task()
            status = self._execute_recovered_task(task) if self.recover_pending else self._execute_task(task)
            if status == "paused" and getattr(self.loop, "suspended_payload", None):
                # LangGraph 引擎在审批处挂起：run 线程到此结束，会话交给
                # manager；操作员决策后经 resume_from_decision 续跑。
                self._register_suspended()
                return
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

    def _register_suspended(self) -> None:
        """把审批挂起的图会话登记给 manager，并把 pending 发布到 run。"""
        payload = self.loop.suspended_payload
        self.manager.graph_sessions[self.run.id] = {"driver": self, "loop": self.loop}
        with self.run.lock:
            self.run.status = "paused"
            self.run.pending = payload
            self.run.finished = False
            self.run.bump()
        self._suspended = True

    def resume_from_decision(self, response: dict) -> None:
        """操作员审批决策后由 manager 的新线程调用：Command(resume) 续跑图，
        直到下一个挂起点（再次登记）或终态（正常发布）。"""
        try:
            task = self.loop.resume(response)
            if getattr(self.loop, "suspended_payload", None):
                self._register_suspended()
                return
            self.manager.graph_sessions.pop(self.run.id, None)
            self._suspended = False
            self._publish(task, task.status)
        except StopRequested:
            task = self.store.load(self.run.task_id)
            if task.status not in TERMINAL_STATUS:
                task = self.harness.finalize(
                    task, "failed",
                    failure={"code": "STOPPED_BY_OPERATOR", "message": "操作员在轮次边界停止了这次运行", "retryable": False},
                    event="task_failed", detail={"code": "STOPPED_BY_OPERATOR"},
                )
            self.observer.terminal(task, None)
            self.manager.graph_sessions.pop(self.run.id, None)
            self._suspended = False
            self._publish(task, "stopped")
        except Exception as exc:  # noqa: BLE001 — surface, never swallow
            traceback.print_exc()
            self.run.error = f"{type(exc).__name__}: {exc}"
            task = self.store.load(self.run.task_id)
            if task.status not in TERMINAL_STATUS:
                target = "denied" if task.status == "created" else "failed"
                task = self.harness.finalize(
                    task, target,
                    failure={"code": "VIEWER_RUNNER_ERROR", "message": str(exc)[:300], "retryable": False},
                    event="tool_denied" if target == "denied" else "task_failed",
                    detail={"code": "VIEWER_RUNNER_ERROR"},
                )
            self.observer.terminal(task, None)
            self.manager.graph_sessions.pop(self.run.id, None)
            self._suspended = False
            self._publish(task, "error")

    # --------------------------------------------------------------- assembly

    def _assemble(self) -> None:
        rt = self.rt
        self.scenario = rt.get_scenario(self.run.template)
        self.run_dir = self.manager.runs_dir / self.run.id
        self.db = self.run_dir / "task.sqlite"
        self.store = rt.TaskStore(self.db)
        self.module = rt.get_domain(self.scenario.module)
        self.general = self.module.key != DOMAIN_KEY
        # 子代理编排通道(spawn/status/read):治理硬限在 console_subagents;
        # 通道对象持有 manager 与本运行,read 狱到"本运行的子代理"。
        self.subagent_channel = SubagentChannel(self.manager, self.run) if self.general else None
        # 预算延展通道(S2a):必须在 _build_world 之前存在(工具集在 limits
        # 定稿前构建);原始上限由下面的 base_limits 提供。
        self.budget_channel = BudgetChannel(self) if self.general else None
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
        if self.run.model != "mock" and not self.fast:
            price = getattr(provider, "price", None)
            max_completion = int(getattr(provider, "max_completion_tokens", 512) or 512)
            if price is not None:
                limits = rt.BudgetLimits(**_scaled_budget_for_prices(limits, price, max_completion))
        self.limits = limits
        #: 延展封底参照的原始上限(价格缩放后的定稿值)——通道懒解析它。
        self.base_limits = limits
        # Fast runs opt into the batch/native contract; `fastProfile` lets the
        # experiment compare JSON batching (`general-fast`) with native tools
        # (`general-fast-native`, the default).
        fast_profile = str((self.run.spec or {}).get("fastProfile") or "").strip()
        if fast_profile and fast_profile not in PROMPT_PROFILES:
            raise ValueError(f"unknown fastProfile {fast_profile!r}")
        prompt_profile = (
            fast_profile or "general-fast-native"
        ) if (self.fast and self.general) else self.module.prompt_profile
        self.harness = rt.Harness(
            store=self.store,
            registry=registry,
            provider=provider,
            world=self.world,
            limits=limits,
            receipts=self.receipts,
            verifier=lambda observation: self.module.verify(self.world, observation),
            state_fn=self.module.summarize_state,
            instruction=self._build_instruction(),
            prompt_profile=prompt_profile,
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
        if self.budget_channel is not None:
            approval_tools += ("request_budget_extension",)
        if self.subagent_channel is not None:
            approval_tools += ("apply_child_changes",)
        if self.mcp_bridge is not None:
            approval_tools += tuple(self.mcp_bridge.approval_names())
        self.control = RunControl(
            self.run, self.harness, rt, approval_tools, self.manager.approval_timeout,
            preview=self._approval_preview if self.sandbox is not None else None,
            classify_consequential=self._classify_consequential,
            persist=self.manager._write_meta,
        )
        self.run.controller = self.control
        if self.general:
            self.ask_holder["control"] = self.control

    def _seed_workspace_copy(self) -> None:
        """写子代理的隔离拷贝:从父工作区拷入自己的沙箱并写基线哈希。

        源路径必须在 runs 目录之下（父运行工作区），否则拒绝——spec 来自
        API，不能让任意路径被拷进运行沙箱。
        """
        spec = self.run.spec or {}
        source = spec.get("workspace_copy_from")
        if not source:
            return
        import shutil

        from console_subagents import snapshot_hashes

        src = Path(str(source)).resolve()
        runs_root = Path(self.manager.runs_dir).resolve()
        if runs_root not in src.parents:
            raise ValueError("workspace_copy_from 必须位于 runs 目录之下")
        dest = self.run_dir / "workspace"
        if dest.exists() and any(dest.iterdir()):
            return
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        baseline = snapshot_hashes(dest)
        (self.run_dir / "workspace_baseline.json").write_text(
            json.dumps(baseline, ensure_ascii=False), encoding="utf-8",
        )

    def _build_world(self):
        kwargs = {}
        if self.general:
            self._seed_workspace_copy()
            self.sandbox = self.rt.WorkspaceSandbox(self.run_dir / "workspace")
            project_spec = (self.run.spec or {}).get("project")
            if project_spec:
                project_path = self.run_dir / "project.json"
                if project_path.is_file():
                    self.project = json.loads(project_path.read_text(encoding="utf-8"))
                elif self.fast and self._try_worktree(project_spec):
                    project_path.write_text(
                        json.dumps(self.project, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
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
                # 子代理编排通道:深度/数量/预算的硬限在 console_subagents
                # 的纯逻辑里;这里只提供"创建真实子运行并等它们终态"的能力。
                "subagent_channel": self.subagent_channel,
                # 预算延展通道(S2a):长任务在批前申请加预算,审批门后生效
                "budget_channel": self.budget_channel,
            }
            if (self.run.spec or {}).get("fastProfile") == "motion-render":
                # Model-in-the-loop geometry check plus a host-side final
                # verdict. Frames live beside the run, never in the deliverable
                # workspace; only their names/hashes enter project.json.
                self.motion_probe = MotionProbe(
                    self.sandbox.root, self.run_dir / "quality-frames",
                )
                kwargs["motion_probe"] = self.motion_probe
        return self.module.create_world(
            partial=self.scenario.partial_corpus, spec=self.run.spec or {}, **kwargs,
        )

    def _try_worktree(self, project_spec: dict) -> bool:
        """Fast-mode workspace: git worktree instead of a full copy.

        Only a clean git checkout is eligible: a dirty source has no HEAD that
        matches what the operator sees, and applying a patch generated from a
        different baseline would be dishonest. Anything unexpected falls back to
        ``import_project`` (the isolated copy keeps the same patch contract).
        """
        import subprocess

        source = Path(str(project_spec.get("path") or "")).expanduser().resolve()
        if not source.is_dir():
            return False
        try:
            top = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10,
            )
            if top.returncode != 0 or Path(top.stdout.strip()).resolve() != source:
                return False
            dirty = subprocess.run(
                ["git", "-C", str(source), "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
            if dirty.returncode != 0 or dirty.stdout.strip():
                return False
            head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if not head:
                return False
        except (OSError, subprocess.SubprocessError):
            return False
        workspace = self.run_dir / "workspace"
        if workspace.exists() and any(workspace.iterdir()):
            return False
        try:
            added = subprocess.run(
                ["git", "-C", str(source), "worktree", "add", "--detach",
                 str(workspace), "HEAD"],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if added.returncode != 0:
            return False
        self.project = {
            "source": str(source),
            "baselineCommit": head,
            "isolatedCopy": True,
            "worktree": True,
        }
        return True

    def _ask_channel(self, **kwargs: Any) -> dict[str, Any]:
        control = self.ask_holder.get("control")
        if control is None:
            return {"answered": False, "reason": "this deployment has no operator channel"}
        return control.ask(**kwargs)

    def _issue(self, **kwargs):
        ref = self.receipts.issue(**kwargs)
        self.issued["ref"] = ref
        return ref

    def _build_instruction(self) -> str:
        """模块协议指令 + 验证目标 + 操作员长期记忆(M2)。

        验证命令从 spec.verification 提取——模型需要知道"什么算完成"。
        记忆来自 chat 服务的 MemoryStore。两者都是佐料，不是主菜。
        """
        instruction = self.module.instruction
        if getattr(self, "fast", False) and getattr(self, "general", False):
            # The fast toolset has no terminal; the domain instruction that
            # advertises it would send the model straight into a denial.
            instruction = GENERAL_FAST_INSTRUCTION

        # 验证目标：模型需要知道成功的判定标准
        verification = (self.run.spec or {}).get("verification")
        if isinstance(verification, dict) and verification.get("command"):
            cmd = verification["command"]
            args = verification.get("args") or []
            full_cmd = " ".join([cmd] + [str(a) for a in args])
            instruction = (instruction or "") + (
                f"\n\n【验证目标】你的产出将被以下命令独立验证——"
                f"确保它通过：\n  {full_cmd}"
            )

        # 仓库级约定：只读隔离工作区，限长，哈希随 task binding 入链。
        # 失败时宁可不注入，也不能因为一个项目说明文件阻断运行。
        self.project_instructions = []
        sandbox = getattr(self, "sandbox", None)
        if sandbox is not None:
            try:
                self.project_instructions = collect_project_instructions(sandbox.root)
                block = instruction_block(self.project_instructions)
                if block:
                    instruction = (instruction or "") + "\n\n" + block
            except Exception:  # noqa: BLE001 — project instructions are best-effort context
                traceback.print_exc()

        # 操作员长期记忆
        chat = getattr(self.manager, "chat", None)
        if chat is not None and hasattr(chat, "memory"):
            try:
                query = str(getattr(self.run, "objective", "") or "")
                block = chat.memory.context_block(query=query)
                if block:
                    instruction = (instruction or "") + block
            except Exception:  # noqa: BLE001 — 记忆注入失败不阻断运行
                pass
        return instruction

    def _limits(self):
        scenario = self.scenario
        limits = scenario.limits
        spec = self.run.spec or {}
        if spec.get("project") and not self.resume_limits:
            # More room to inspect and repair a real repository; the operator's
            # hard cost ceiling and the provider price scaling still apply.
            # Fast mode is an experiment: it keeps the operator envelope instead
            # of shrinking calls by the worst-case reserve (which is what forced
            # the 21-call ceiling in the observed run).
            if self.fast:
                limits = self.rt.BudgetLimits(
                    max_calls=max(60, scenario.limits.max_calls),
                    max_reads=max(300, scenario.limits.max_reads),
                    max_cost_microusd=max(3_000_000, scenario.limits.max_cost_microusd),
                    max_tokens=max(1_200_000, scenario.limits.max_tokens),
                )
            else:
                limits = self.rt.BudgetLimits(
                    max_calls=max(40, scenario.limits.max_calls),
                    max_reads=max(160, scenario.limits.max_reads),
                    max_cost_microusd=max(1_000_000, scenario.limits.max_cost_microusd),
                    max_tokens=max(300_000, scenario.limits.max_tokens),
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
        if tool == "browser_act":
            url = str(arguments.get("url") or "")
            action = str(arguments.get("action") or "")
            selector = str(arguments.get("selector") or "")
            if not url or not action or not selector:
                return None
            return {
                "url": url,
                "action": action,
                "selector": selector,
                "note": "本机回环浏览器交互；操作前后截图与 DOM 入证据",
            }
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
        restore_state = False
        if self.recover_pending:
            task = self.store.load(self.run.task_id)
            self.max_rounds = (
                max(60 if self.fast else 24, self.scenario.max_rounds)
                if (self.run.spec or {}).get("project")
                else self.scenario.max_rounds
            )
            restore_state = True
        elif self.recover_unknown:
            # Unknown model outcome: the operator explicitly accepted the risk.
            # A matching receipt reconciles in place; otherwise a fresh round is
            # priced. Either way the loop must start from the durable event log,
            # never from ``harness.resume`` (which only accepts failed tasks).
            task = self.store.load(self.run.task_id)
            task = self.harness.recover_unknown(task, operator="console-operator")
            self.max_rounds = task.model.attempts + self.extra_rounds
            restore_state = True
        elif self.resume_limits:
            task = self.store.load(self.run.task_id)
            task = self.harness.resume(
                task,
                detail={"by": "operator", "reason": "budget topped up", "limits": self.limits.to_dict()},
            )
            self.max_rounds = max(self.scenario.max_rounds, task.model.attempts + self.extra_rounds)
            restore_state = True
        else:
            binding = {
                "scenario": self.scenario.key,
                "module": self.module.key,
                "source": "viewer-live",
                **({"spec": self.run.spec} if self.run.spec else {}),
            }
            if self.general:
                binding["workspace"] = "workspace"
            if getattr(self, "project_instructions", None):
                # The exact bytes that entered the prompt, with their hashes.
                # This is the audit trail for "which conventions did the model see".
                binding["project_instructions"] = instructions_binding(self.project_instructions)
            if self.extra_allowed:
                binding["mcp_imports"] = self.extra_allowed
            grant = dict(self.scenario.grant)
            if self.extra_allowed:
                grant["allowed_tools"] = list(grant.get("allowed_tools") or []) + self.extra_allowed
            allowed_tools = list(self.scenario.allowed_tools) + self.extra_allowed
            allowed_tools, requested_tools = filter_toolset(self.run.spec, allowed_tools)
            if requested_tools:
                binding["toolset"] = requested_tools
            task = self.harness.create(
                task_id=f"task-{self.run.id}",
                objective=self.run.objective,
                principal="console-operator",
                binding=binding,
                allowed_tools=allowed_tools,
                grant=grant,
            )
            if self.manager.case_index is not None:
                try:
                    hits = self.manager.case_index.search(self.run.objective, limit=3)
                    task = self.harness.record_recall(task, hits)
                except Exception:  # noqa: BLE001 — memory must never kill a run
                    traceback.print_exc()
            self.max_rounds = (
                max(60 if self.fast else 24, self.scenario.max_rounds)
                if (self.run.spec or {}).get("project")
                else self.scenario.max_rounds
            )
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
        if restore_state:
            self.world.apply_state(self.loop.restore(task).state())
        return task

    def _new_loop(self):
        if self.recover_pending:
            # A restored wait continues from the event-sourced kernel state.
            # The in-memory graph checkpoint did not survive the old process.
            from flowagent.runtime.loop import Loop

            return Loop(
                self.harness,
                planner=self.planner,
                max_rounds=self.max_rounds,
                observer=self.observer,
                control=self.control,
                result_tools=self.module.result_tools,
            )
        from flowagent.runtime.engine import build_loop

        return build_loop(
            self.harness,
            planner=self.planner,
            max_rounds=self.max_rounds,
            observer=self.observer,
            control=self.control,
            result_tools=self.module.result_tools,
        )

    def _execute_recovered_task(self, task) -> str:
        """Resolve one restored human wait, then continue from the chain."""
        pending = dict(self.recover_pending or {})
        self.control.restore_pending(pending)
        if pending.get("kind") == "question":
            events = self.store.events(task.id)
            started = events[-1] if events else {}
            started_payload = (started.get("payload") or {}).get("detail") or {}
            if task.status != "running" or started.get("type") != "tool_started" or started_payload.get("tool") != "ask_operator":
                raise ValueError("restored question does not match the committed tool start")
            step = next(
                (item for item in task.model.proposal["steps"]
                 if item["tool"] == "ask_operator" and item.get("arguments") == started_payload.get("arguments")),
                None,
            )
            if step is None or started_payload.get("arguments", {}).get("question") != pending.get("question"):
                raise ValueError("restored question does not match the validated proposal")
            reply = self.control.wait_restored_question()
            result = {
                "asked": pending["question"],
                "answered": bool(reply.get("answered")),
            }
            if reply.get("answered"):
                result["answer"] = str(reply.get("answer") or "")[:600]
            else:
                result["timed_out"] = bool(reply.get("timed_out"))
                result["reason"] = str(reply.get("reason") or "the operator did not answer")
            observation = self.harness.registry.observe(step["tool"], step.get("arguments") or {}, result)
            return self.loop.resume_operator_question(task, observation).status

        events = self.store.events(task.id)
        requested = events[-1] if events else {}
        request_payload = (requested.get("payload") or {}).get("detail") or {}
        if task.status != "paused" or requested.get("type") != "approval_requested":
            raise ValueError("restored approval does not match the committed pause")
        tool_matches = request_payload.get("tool") == pending.get("tool")
        arguments_match = request_payload.get("arguments") == pending.get("arguments")
        if not tool_matches or not arguments_match:
            raise ValueError(
                "restored approval payload differs from the chain "
                f"(tool={tool_matches}, arguments={arguments_match}, event_keys={sorted(request_payload)})"
            )
        step = next(
            (item for item in task.model.proposal["steps"]
             if item["tool"] == pending.get("tool") and item.get("arguments") == pending.get("arguments")),
            None,
        )
        if step is None:
            raise ValueError("restored approval is absent from the validated proposal")
        gate = self.control.wait_restored_approval(
            round_no=int(pending.get("round") or request_payload.get("round") or 0),
            task=task,
            step=step,
        )
        return self.loop.resume_operator_approval(gate.task, gate, step).status

    def _use_capability_runtime(self) -> bool:
        spec = getattr(self.run, "spec", None) or {}
        selected = spec.get("capabilityRuntime", True) not in (False, "off", "legacy")
        return bool(
            selected
            and os.environ.get("FCODE_CAPABILITY_RUNTIME", "1") != "0"
            and getattr(self, "fast", False)
            and getattr(self, "general", False)
            and getattr(self, "sandbox", None) is not None
            and spec.get("fastProfile") == "motion-render"
            and not self.scenario.crash_at
            and not getattr(self, "recover_pending", None)
        )

    def _capability_constraints(self) -> dict[str, Any]:
        documents: dict[str, str] = {}
        for name in ("README.md", "verify.py"):
            try:
                record = self.sandbox.read(name)
            except Exception:  # noqa: BLE001 — an absent optional document is empty input
                continue
            documents[name] = str(record.get("content") or "")[:20_000]
        return {
            "instruction": self._build_instruction(),
            "documents": documents,
            "verification": dict((self.run.spec or {}).get("verification") or {}),
            "artifact_policy": "index.html is the primary artifact; FCODE_RESULT.md is only a delivery receipt",
        }

    def _execute_capability(self, task):
        """Run motion.render as staged Work, with Harness-owned side effects."""
        if self.general and self.motion_probe is None:
            from motion_frames import MotionProbe

            self.motion_probe = MotionProbe(
                self.sandbox.root,
                out_dir=self.run_dir / "motion_frames",
                node=os.environ.get("FCODE_NODE_BIN"),
            )
        verification_loop = self.rt.Loop(
            self.harness,
            planner=self.planner,
            max_rounds=self.max_rounds,
            observer=self.observer,
            control=self.control,
            result_tools=self.module.result_tools,
        )
        bridge = HarnessCapabilityBridge(
            self.harness,
            task,
            control=self.control,
            observer=self.observer,
            loop=verification_loop,
            motion_probe=self.motion_probe,
            host_verify_times_ms=HOST_VERIFY_TIMES_MS,
        )
        runtime = build_motion_capability(
            self.sandbox,
            bridge,
            probe=self.motion_probe,
            verify=bridge.verify,
            executor=bridge.execute,
            child_run_id=f"cap-{self.run.id}",
        )
        bridge.bind(runtime)
        try:
            result = runtime.run(self.run.objective, constraints=self._capability_constraints())
        except Exception:
            bridge.finish()
            raise
        task = bridge.finish()
        self.capability_result = result
        self.capability_quality = dict(bridge.final_quality or result.quality or {})
        if task.status not in TERMINAL_STATUS:
            payload = result.to_dict()
            task = self.harness.record(
                task,
                "capability_succeeded" if result.succeeded else "capability_failed",
                payload,
            )
            target = "succeeded" if result.succeeded else "failed"
            task = self.harness.finalize(
                task,
                target,
                failure=(
                    None if result.succeeded else {
                        "code": result.reason or "CAPABILITY_FAILED",
                        "message": f"capability stopped in {result.stage}: {result.reason}",
                        "retryable": False,
                    }
                ),
                event="task_succeeded" if result.succeeded else "task_failed",
                detail={"code": "CAPABILITY_PASSED" if result.succeeded else result.reason,
                        "stage": result.stage, "child_run_id": result.child_run_id},
            )
        self.observer.terminal(task, result.to_dict())
        return task

    def _execute_task(self, task) -> str:
        try:
            if self.scenario.crash_at:
                task = _run_crash(
                    self.rt, self.harness, self.store, self.world, self.scenario, task, self.observer, self.db,
                )
            elif self._use_capability_runtime():
                task = self._execute_capability(task)
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
            if self.capability_result is not None:
                # The capability reached its terminal state only after this
                # artifact hash passed final host quality and declared verify.
                # Publish that same fact instead of taking a contradictory
                # post-terminal sample.
                delivery["capability"] = self.capability_result.to_dict()
                if self.capability_quality:
                    delivery["quality"] = self.capability_quality
            elif (self.run.spec or {}).get("fastProfile") == "motion-render" and self.sandbox is not None:
                # Legacy rollback path: quality remains a best-effort publication
                # observation and cannot repair the already settled task.
                try:
                    record = self.sandbox.read("index.html")
                    motion = analyze_motion(str(record.get("content") or ""))
                    probe = getattr(self, "motion_probe", None)
                    delivery["quality"] = merge_quality(
                        motion,
                        probe.probe("index.html", times=HOST_VERIFY_TIMES_MS)
                        if probe is not None else None,
                    )
                except Exception as error:  # noqa: BLE001 — quality is best-effort, not a run failure
                    delivery["quality"] = {
                        "status": "unknown",
                        "score": 0,
                        "issues": [f"quality check unavailable: {error}"],
                    }
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
        if getattr(self, "_suspended", False):
            # 审批挂起：pending 是给操作员 UI 的活跃状态，会话仍活着；
            # controller/检测器都要保留，resume 终态时再统一清理。
            return
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
