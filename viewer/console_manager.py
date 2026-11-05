"""Run history, creation, control and persistence.

The manager owns the runs/ directory, resumes and reviews. It uses the runtime
host for execution and the shared catalog for model/budget facts, and exposes a
small surface the HTTP handler calls.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from console_common import (
    CHAT_CONFIG_PATH,
    DOMAIN_KEY,
    EFFORT_LEVELS,
    FINISHED_STATUS,
    REVIEWABLE_STATUS,
    RUNS_DIR,
    TERMINAL_STATUS,
    VIEWER_DIR,
    _scaled_budget_for_prices,
    merged_provider_env,
    model_catalog,
    settings_provider_env,
)
from console_run_budget import budget_summary as _budget_summary
from console_runtime import Run, Runtime


class RunManager:
    def __init__(
        self,
        runtime: Runtime | None,
        runtime_error: str,
        pace: float,
        approval_timeout: float = 300.0,
        local_endpoint: str | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self.runtime = runtime
        # Injected so a caller (the HTTP server, tests) can point the manager at
        # a temporary history without mutating a process-wide global.
        self.runs_dir = Path(runs_dir) if runs_dir is not None else RUNS_DIR
        self.runtime_error = runtime_error
        self.pace = pace
        self.approval_timeout = approval_timeout
        self.local_endpoint = local_endpoint
        #: Test/embedding seam: provider values the settings page supplied.
        #: ``None`` means "read the settings file on demand".
        self._settings_env: dict[str, str] | None = None
        self.runs: dict[str, Run] = {}
        self.order: list[str] = []
        self.case_index = runtime.CaseIndex() if runtime is not None else None
        self.fact_store = runtime.FactStore() if runtime is not None else None
        # The artifact book is the general domain's cross-task fact layer: which
        # file exists, produced by which run and round, with which hash. It sits
        # beside the run history (Application Support in the desktop bundle) and
        # is a projection of signed traces — rebuildable, never the only copy.
        self.artifact_book = None
        if runtime is not None:
            try:
                self.artifact_book = runtime.ArtifactBook(
                    self.runs_dir.parent / "artifacts.sqlite"
                )
            except Exception:  # noqa: BLE001 — memory must never take the console down
                traceback.print_exc()
        self.chat = None
        if runtime is not None:
            try:
                # Chat lives next to the run history (Application Support in the
                # desktop bundle), so audit records survive app upgrades. The
                # settings file sits beside it and holds provider overrides —
                # including API keys — outside the repository.
                self.chat = runtime.ChatService(
                    self.runs_dir.parent / "chat.sqlite",
                    CHAT_CONFIG_PATH,
                    # Agentic chat 的只读工具沙箱：以仓库根为界（越狱由沙箱拒绝）。
                    workspace_root=Path(runtime.repo or self.runs_dir.parent),
                )
                try:
                    retention = int(os.environ.get("FCODE_CHAT_RETENTION_DAYS", "0") or 0)
                except ValueError:
                    retention = 0
                if retention > 0:
                    archived = self.chat.archive_stale(retention)
                    if archived:
                        print(f"[console] archived {archived} chat thread(s) older than {retention} days")
            except Exception:  # noqa: BLE001 — chat must not take the console down
                traceback.print_exc()
        self._lock = threading.Lock()
        self._seq = 0
        self.runs_dir.mkdir(exist_ok=True)
        self._load_history()
        # The MCP bridge attaches whatever the host reviewed into mcp.json.
        # Connection happens off-thread: a hanging server must not delay boot,
        # and /api/mcp reports the honest per-server status meanwhile.
        self.mcp: dict[str, Any] = {"ready": False, "bridge": None}
        if runtime is not None:
            config_path = os.environ.get("FCODE_MCP_CONFIG") or str(runtime.repo / "mcp.json")
            thread = threading.Thread(
                target=self._connect_mcp, args=(config_path,), name="mcp:bridge", daemon=True,
            )
            thread.start()

    def _connect_mcp(self, config_path: str) -> None:
        try:
            from flowagent.tools.mcp.bridge import McpBridge

            bridge = McpBridge(config_path, cwd=self.runtime.repo if self.runtime else None)
            bridge.connect()
            self.mcp = {"ready": True, "bridge": bridge}
        except Exception as error:  # noqa: BLE001 — the bridge must never kill the console
            traceback.print_exc()
            self.mcp = {"ready": True, "bridge": None, "error": f"{type(error).__name__}: {error}"}

    def mcp_tools(self) -> list[dict]:
        """Honest MCP availability for the console (server-by-server)."""
        bridge = self.mcp.get("bridge")
        if bridge is None:
            error = self.mcp.get("error")
            return [{
                "server": "mcp",
                "connected": False,
                "tools": [],
                "refused": [],
                "reason": error or "no mcp.json configured",
            }]
        return bridge.statuses()

    def settings_env(self) -> dict[str, str]:
        """Provider values from the settings page (read on demand)."""
        if self._settings_env is not None:
            return dict(self._settings_env)
        return settings_provider_env()

    def resolve_model(self, model_key: str | None, effort: str | None) -> tuple[str, str]:
        """Fail-closed model resolution for run creation.

        An unavailable or unknown selection raises with the same reason the
        catalog shows — creation never silently falls back to the mock.
        """
        if model_key in (None, "", "mock"):
            if effort not in (None, ""):
                raise ValueError("mock 模型不支持思考强度选择")
            return "mock", ""
        # The same catalog the menu shows: settings-page values included, so a
        # provider the operator configured in the app resolves here too.
        catalog = model_catalog(self.local_endpoint, settings_env=self.settings_env())
        entry = next((m for m in catalog if m["key"] == model_key), None)
        if entry is None:
            raise ValueError(f"未知模型 {model_key!r}")
        if not entry["available"]:
            raise ValueError(entry["reason"] or f"模型 {model_key} 当前不可用")
        if not entry["supportsEffort"]:
            if effort not in (None, ""):
                raise ValueError(f"模型 {model_key} 不支持思考强度选择")
            return model_key, ""
        if effort not in (None, "", *EFFORT_LEVELS):
            raise ValueError("思考强度必须是 low、medium 或 high")
        # No choice means the provider's own default applies (Kimi's preset
        # pins "low"); forcing "medium" here silently raised both the cost and
        # the reasoning length, and a longer reasoning channel is what emptied
        # a real run's completion budget.
        return model_key, effort or ""

    def merge_chat_action(
        self,
        action: dict | None,
        *,
        goal: str | None = None,
        mode: str | None = None,
        sources: list | None = None,
    ) -> dict:
        """Operator edits on a chat proposal, under the model's own rules.

        The confirmation card lets the goal be rewritten and the mode switched;
        ``flowagent.chat.merge_action`` re-applies the parser's fail-closed
        checks, so an edited proposal can never become one the parser would
        have refused.
        """
        if self.runtime is None:
            raise ValueError("live mode is unavailable")
        return self.runtime.merge_action(action, goal=goal, mode=mode, sources=sources)

    def validate_delegation_spec(self, spec: dict | None, scenario) -> dict | None:
        """Validate operator delegation overrides before a run exists.

        Finance delegations carry ``scope`` (which corpus documents this run may
        read) and ``budget``. General delegations carry a mode: ``research``
        needs ``sources`` (the URLs fetch_url may visit); ``script`` needs
        neither (the plan writes and runs a script in the sandbox). ``budget``
        applies to both. All of it is fact in the task binding, consumed by
        the run thread — not decorative UI state.
        """
        if spec is None:
            return None
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        general = scenario.module != DOMAIN_KEY
        if general:
            mode = str(spec.get("mode") or "research")
            if mode not in ("research", "script", "open"):
                # ``open`` is the delegation whose plan the model extends; the
                # other two are blueprints and stay closed.
                raise ValueError("mode must be research, script or open")
            script_path = spec.get("scriptPath")
            if script_path is not None and (
                not isinstance(script_path, str)
                or not script_path.strip()
                or "/" in script_path
                or script_path.startswith(".")
            ):
                raise ValueError("scriptPath must be a bare workspace filename")
            # The declared engineering-result obligation, if any. Validated here as
            # well as at world construction so an operator typo is refused when
            # the run is created rather than after the work is done.
            if self.runtime is not None:
                try:
                    self.runtime.build_obligation(spec.get("verification"))
                except self.runtime.AgentError as error:
                    raise ValueError(f"{error.code}: {error.message}") from error
            project = spec.get("project")
            if project is not None:
                if mode != "open":
                    raise ValueError("a project delegation must use open mode")
                if self.runtime is None:
                    raise ValueError("live mode is unavailable")
                try:
                    spec["project"] = self.runtime.validate_project_spec(project)
                except ValueError as error:
                    raise ValueError(str(error)) from error
                if spec.get("verification") is None:
                    raise ValueError("a project delegation needs an independent verification command")
                spec.setdefault("reportPath", self.runtime.PROJECT_REPORT_PATH)
            sources = spec.get("sources")
            if sources is not None:
                if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                    raise ValueError("sources must be a list of URLs")
                if not 1 <= len(sources) <= 5:
                    raise ValueError("sources must list 1 to 5 URLs")
                if len(set(sources)) != len(sources):
                    raise ValueError("sources must not repeat a URL")
                for url in sources:
                    parsed = urlparse(url)
                    if parsed.scheme not in ("http", "https") or not parsed.hostname:
                        raise ValueError(f"sources contains a non-http(s) URL: {url[:120]!r}")
            elif mode == "research" and "scope" not in spec:
                # A research delegation without sources cannot plan a single
                # fetch; refuse at creation instead of failing mid-run. A
                # script delegation plans no fetch at all.
                raise ValueError("a research delegation needs spec.sources (1 to 5 URLs)")
            # Chat-to-work bridge: the conversation a delegation came from, so
            # "周三/当天/到了后" resolve against what the user actually said.
            # Capped hard — context travels, but cannot smuggle a payload.
            origin = spec.get("origin_thread")
            if origin is not None:
                if not isinstance(origin, dict):
                    raise ValueError("origin_thread must be an object")
                if origin.get("threadId") is not None and not isinstance(origin["threadId"], str):
                    raise ValueError("origin_thread.threadId must be a string")
                for field in ("threadTitle",):
                    if origin.get(field) is not None and not isinstance(origin[field], str):
                        raise ValueError(f"origin_thread.{field} must be a string")
                messages = origin.get("messages")
                if messages is None:
                    messages = []
                if not isinstance(messages, list) or not all(
                    isinstance(item, dict) and item.get("role") in ("user", "assistant")
                    and isinstance(item.get("text"), str)
                    for item in messages
                ):
                    raise ValueError("origin_thread.messages must be user/assistant objects")
                if len(messages) > 24:
                    # The tail, not the head: "周三 / 当天 / 到了之后" resolve
                    # against the most recent turns.
                    messages = messages[-24:]
                origin["messages"] = [
                    {"role": item["role"], "text": item["text"][:600]} for item in messages
                ]
        else:
            if spec.get("project") is not None:
                raise ValueError("a project delegation requires a general-work template")
            scope = spec.get("scope")
            if scope is not None:
                if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
                    raise ValueError("scope must be a list of document ids")
                if not scope:
                    raise ValueError("scope cannot be empty; omit it to use the full corpus")
                if len(set(scope)) != len(scope):
                    raise ValueError("scope cannot contain duplicate documents")
                known = {doc["id"] for doc in self.runtime.corpus(scenario.key)}
                unknown = [doc for doc in scope if doc not in known]
                if unknown:
                    raise ValueError(f"scope contains unknown documents: {', '.join(unknown)}")
        budget = spec.get("budget")
        if budget is not None:
            if not isinstance(budget, dict):
                raise ValueError("budget must be an object")
            required = ("maxCalls", "maxReads", "maxCostMicrousd")
            missing = [name for name in required if name not in budget]
            if missing:
                raise ValueError(f"budget is missing {', '.join(missing)}")
            for name in required:
                value = budget[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"budget.{name} must be a positive integer")
        return spec

    def model_catalog_with_budget(self, template_key: str | None) -> list[dict]:
        """模型清单 + 该模板下各自真实生效的预算顶（草稿预览的单一事实源）。"""
        models = model_catalog(self.local_endpoint, settings_env=self.settings_env())
        limits = None
        if template_key and self.runtime is not None:
            try:
                limits = self.runtime.get_scenario(template_key).limits
            except Exception:  # noqa: BLE001 — unknown template: budget stays None
                limits = None
        if limits is None:
            return models
        out = []
        for m in models:
            entry = dict(m)
            entry["budget"] = self._budget_for_model(m, limits)
            out.append(entry)
        return out

    def _budget_for_model(self, m: dict, limits) -> dict | None:
        if m["key"] in ("mock", "local"):
            # 不产生外部计费，预算顶即模板草案
            return {
                "max_calls": limits.max_calls,
                "max_reads": limits.max_reads,
                "max_cost_microusd": limits.max_cost_microusd,
                "max_tokens": limits.max_tokens,
            }
        price = m.get("price")
        if not m.get("available") or not price:
            return None
        # 价格只收紧调用容量，不抬高操作者的成本 / token 硬上限。
        # 菜单与创建、恢复共用这条路径；无法容纳一次预留时如实展示原因。
        from types import SimpleNamespace

        snapshot = SimpleNamespace(
            input_microusd_per_mtok=price["inputMicrousdPerMtok"],
            output_microusd_per_mtok=price["outputMicrousdPerMtok"],
        )
        try:
            return _scaled_budget_for_prices(
                limits, snapshot, int(m.get("maxCompletionTokens") or 512)
            )
        except ValueError as error:
            return {
                "max_calls": 0,
                "max_reads": limits.max_reads,
                "max_cost_microusd": limits.max_cost_microusd,
                "max_tokens": limits.max_tokens,
                "error": str(error),
            }

    # ------------------------------------------------------------- persistence

    #: What a run interrupted by a restart is labelled with. The trace stops
    #: where the process did; calling it ``running`` would promise work nobody
    #: is doing, and calling it ``failed`` would contradict the kernel's
    #: unknown-outcome rule.
    INTERRUPTED_NOTE = "进程重启时这次运行还在进行；轨迹停在进程消失的那一刻"

    def _load_history(self) -> None:
        """Make runs from earlier processes visible again, read-only.

        The index, the trace and the task database all outlive the process, so
        this rebuilds only the in-memory record: no thread, no observer, no
        re-ingest, no snapshot. One damaged meta.json is skipped rather than
        taking the console's boot down with it — a half-written file must not
        hide the rest of the history.
        """
        for meta_path in sorted(self.runs_dir.glob("run-*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict) or not str(meta.get("id") or "").strip():
                    continue
                recorded = str(meta.get("status") or "")
                interrupted = recorded not in FINISHED_STATUS
                run = Run(
                    id=str(meta["id"]),
                    template=str(meta.get("template") or "?"),
                    objective=str(meta.get("objective") or ""),
                    created=str(meta.get("created") or ""),
                    pace=0.0,
                    status="stopped" if interrupted else recorded,
                    rounds=int(meta.get("rounds") or 0),
                    event_count=int(meta.get("events") or 0),
                    trace_path=str(meta.get("tracePath") or ""),
                    trace_verified=bool(meta.get("verified", False)),
                    name=str(meta.get("name") or ""),
                    pinned=bool(meta.get("pinned", False)),
                    approval=str(meta.get("approval") or "consequential"),
                    task_id=str(meta.get("taskId") or ""),
                    model=str(meta.get("model") or "mock"),
                    effort=str(meta.get("effort") or ""),
                    cost=meta.get("cost"),
                    spec=meta.get("spec"),
                    error=self.INTERRUPTED_NOTE if interrupted else str(meta.get("error") or ""),
                    finished=True,
                )
            except (OSError, ValueError, TypeError):
                continue
            self.runs[run.id] = run
            self.order.append(run.id)
        self.order.sort(key=lambda rid: self.runs[rid].created, reverse=True)

    def _ingest_memory(self, rt: Runtime, run: Run, final: dict) -> None:
        """Every projection of one finished trace, in one place.

        Cases answer "what did we do", facts answer the finance reference
        workload, artifacts answer "what exists and who produced it". All three
        are idempotent (a review re-exports the trace and ingests again), and
        none of them may kill a finished run.
        """
        if self.case_index is not None:
            try:
                self.case_index.upsert(final, run_id=run.id)
            except Exception:  # noqa: BLE001 — memory must never kill a run
                traceback.print_exc()
        if self.fact_store is not None:
            try:
                self.fact_store.upsert_run(
                    run_id=run.id,
                    facts=rt.project_facts(final, run_id=run.id),
                )
            except Exception:  # noqa: BLE001 — memory must never kill a run
                traceback.print_exc()
        if self.artifact_book is not None:
            try:
                self.artifact_book.ingest_trace(final, run_id=run.id)
            except Exception:  # noqa: BLE001 — memory must never kill a run
                traceback.print_exc()

    def _write_meta(self, run: Run) -> None:
        meta = run.to_json()
        meta["verified"] = run.trace_verified
        # Model and effort are not in the list payload (the menu does not need
        # them per row) but they must survive a restart: without them a
        # restored run resumed after the fact would silently fall back to the
        # offline mock.
        meta["model"] = run.model
        meta["effort"] = run.effort
        meta["error"] = run.error
        try:
            (self.runs_dir / run.id / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ public

    def list_runs(self) -> list[dict]:
        with self._lock:
            return [self.runs[rid].to_json() for rid in self.order]

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def delete(self, run_id: str) -> tuple[bool, str]:
        """Remove a finished run's record and directory. Running runs are refused."""
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                return False, "no such run"
            if run.status == "running" and not run.finished:
                return False, "run is still running; stop it first"
            self.runs.pop(run_id, None)
            if run_id in self.order:
                self.order.remove(run_id)
        shutil.rmtree(self.runs_dir / run_id, ignore_errors=True)
        if self.case_index is not None:
            try:
                self.case_index.tombstone(run_id)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        if self.artifact_book is not None:
            try:
                self.artifact_book.archive_run(run_id)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        if self.fact_store is not None:
            try:
                self.fact_store.tombstone_run(run_id)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        return True, ""

    def memory_summary(self) -> dict[str, Any]:
        """Read-only counts for the settings page; never includes case contents."""
        cases = self.case_index
        facts = self.fact_store
        return {
            "runs": len(self.runs),
            "cases": {
                "active": cases.count() if cases else 0,
                "total": cases.count(include_archived=True) if cases else 0,
            },
            "facts": {
                "active": facts.count() if facts else 0,
                "total": facts.count(include_archived=True) if facts else 0,
            },
            # The artifact book is the general domain's fact layer: which file
            # exists, produced by which run and round. Counts here; the browser
            # that opens them lands with the artifacts work.
            "artifacts": {
                "active": self.artifact_book.count(archived=False) if self.artifact_book else 0,
                "total": self.artifact_book.count() if self.artifact_book else 0,
            },
        }

    def review_profile(self, module: str = "", template: str = "", outcome: str = "") -> dict[str, Any]:
        """Project past review decisions from exported traces, read-only.

        The scan is on demand so a newly reviewed run is visible immediately and
        there is no second index to keep in sync. Unreadable or half-written
        traces are skipped: one damaged file must not hide the rest of the
        history. The projection itself lives in ``flowagent.review_profile``.
        """
        builder = getattr(self.runtime, "build_profile", None)
        if builder is None:
            # Static mode: the viewer still sits beside its source checkout.
            src = VIEWER_DIR.parent / "src"
            if str(src) not in sys.path and (src / "flowagent" / "__init__.py").exists():
                sys.path.insert(0, str(src))
            from flowagent.review_profile import build_profile

            builder = build_profile

        traces: list[tuple[str, dict]] = []
        for trace_path in sorted(self.runs_dir.glob("run-*/trace.json")):
            try:
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(trace, dict):
                traces.append((trace_path.parent.name, trace))
        return builder(traces, module=module, template=template, outcome=outcome)

    def create(
        self,
        objective: str,
        template: str,
        approval: str = "consequential",
        spec: dict | None = None,
        model_key: str = "mock",
        effort: str = "",
    ) -> Run:
        with self._lock:
            self._seq += 1
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"run-{stamp}-{uuid.uuid4().hex[:4]}"
            run = Run(
                id=run_id,
                template=template,
                objective=objective,
                created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                pace=self.pace,
                approval=approval,
                spec=spec,
                model=model_key,
                effort=effort,
            )
            self.runs[run_id] = run
            self.order.insert(0, run_id)
        (self.runs_dir / run.id).mkdir(parents=True, exist_ok=True)
        self._write_meta(run)
        thread = threading.Thread(target=self._run_thread, args=(run,), name=f"run:{run.id}", daemon=True)
        thread.start()
        return run

    def resume(self, run: Run, *, limits: dict, extra_rounds: int = 6) -> Run:
        """Continue a run that stopped on a resource limit, without re-paying."""
        rt = self.runtime
        if rt is None:
            raise ValueError("live runtime is unavailable")
        if not run.finished:
            raise ValueError("run is still active")
        if not run.task_id:
            raise ValueError("run has no durable task to continue")
        db = self.runs_dir / run.id / "task.sqlite"
        store = rt.TaskStore(db)
        try:
            task = store.load(run.task_id)
        finally:
            store.close()
        code = (task.failure or {}).get("code", "")
        if task.status != "failed" or code not in ("BUDGET_EXCEEDED", "ROUND_LIMIT"):
            raise ValueError(f"run cannot be resumed from {task.status}/{code or 'no failure'}")
        with run.lock:
            run.finished = False
            run.status = "running"
            run.error = ""
            run.stop_requested = False
            run.pending = None
            run.bump()
        thread = threading.Thread(
            target=self._run_thread,
            args=(run,),
            kwargs={"resume_limits": limits, "extra_rounds": extra_rounds},
            name=f"run:{run.id}",
            daemon=True,
        )
        thread.start()
        return run

    def record_review(self, run_id: str, decision: str, reason: str, reviewer: str) -> dict:
        """Append a human decision to a finished run and re-export its trace.

        This is a new event on the existing hash chain, not a status change:
        the run stays terminal, and the trace is rebuilt from the store so the
        reviewer's judgement sits beside the evidence it is judging.
        """
        rt = self.runtime
        if rt is None:
            raise ValueError("live runtime is unavailable")
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        with run.lock:
            status = run.status
            finished = run.finished
            task_id = run.task_id
        if not finished or status == "running":
            raise ValueError("run is still running; a review can only be recorded after it finishes")
        if status not in REVIEWABLE_STATUS:
            raise ValueError(f"run is {status}; only a terminal run can be reviewed")
        if not task_id:
            raise ValueError("run has no durable task to review")
        run_dir = self.runs_dir / run.id
        trace_path = run_dir / "trace.json"
        db = run_dir / "task.sqlite"
        if not trace_path.exists():
            raise ValueError("run has no trace.json; a review needs the exported trace")
        if not db.exists():
            raise ValueError("run has no task.sqlite; a review needs the durable event chain")
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read trace.json: {exc}") from exc

        store = rt.TaskStore(db)
        try:
            try:
                final, event = rt.record_review(
                    store,
                    task_id,
                    trace,
                    decision=decision,
                    reason=reason,
                    reviewer=reviewer,
                )
            except rt.AgentError as exc:
                raise ValueError(f"cannot record review: {exc}") from exc
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 — a damaged trace is a 409, not a crash
                raise ValueError(f"cannot record review: {type(exc).__name__}: {exc}") from exc
            rt.export_trace(final, trace_path)
            try:
                verification = rt.verify_trace(final)
                verified = bool(verification.get("ok", verification.get("valid", True)))
            except Exception:  # verification must never lose the recorded decision
                verified = False
            with run.lock:
                run.snapshot = final
                run.rounds = len(final["rounds"])
                run.event_count = len(final["events"])
                run.trace_path = f"runs/{run.id}/trace.json"
                run.trace_verified = verified
                run.cost = _budget_summary(final)
                run.bump()
            self._write_meta(run)
            self._ingest_memory(rt, run, final)
            return {
                "ok": True,
                "decision": decision,
                "sequence": event["sequence"],
                "event": {
                    "sequence": event["sequence"],
                    "type": event["type"],
                    "hash": event["hash"],
                },
                "tracePath": run.trace_path,
            }
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- run driver

    def _build_provider(self, rt: Runtime, run: Run, scenario, receipts, issue):
        """Construct this run's proposal source from the operator's selection.

        mock keeps the console's own wiring (scenario policy + fault-injection
        receipts); every other provider goes through the demo factory so env
        validation, price snapshots, pacing and fault disclosure stay identical
        to the CLI path. Gating, budgets and auditing live in the runtime and
        are untouched by this choice.
        """
        if (run.model or "mock") == "mock":
            return rt.MockProvider(
                policy=scenario.policy,
                fail_at_call=scenario.fail_at_call,
                issue_receipt=issue if scenario.fail_at_call else None,
            )
        from flowagent.demo.provider_factory import build_provider

        return build_provider(
            run.model,
            scenario=scenario,
            receipts=receipts,
            endpoint=self.local_endpoint if run.model == "local" else None,
            # 思考强度对任何 supportsEffort 的 provider 生效（创建时已校验）
            reasoning_effort=(run.effort or None) if run.model != "mock" else None,
            # 设置页保存的 provider 值：委托与对话走同一份配置
            env=merged_provider_env(self.settings_env()),
        )

    def _run_thread(self, run: Run, *, resume_limits: dict | None = None, extra_rounds: int = 6) -> None:
        from console_run_driver import RunDriver

        RunDriver(self, run, resume_limits=resume_limits, extra_rounds=extra_rounds).execute()


# -------------------------------------------------------------------- observer
