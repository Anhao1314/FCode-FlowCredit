"""子代理编排的纯逻辑层（console 侧）。

治理不变量在这里落地，RunDriver 的 `_spawn_channel` 只做接线：
- 深度硬限 1：子代理不能再 spawn（parent_run_id 已存在的运行拒绝）；
- 数量硬顶 4：乘数是审批卡上明示的承诺；
- 目标是一句话而不是一篇文章（≤600 字），空列表显式拒绝；
- 触发超时把未完成的子运行如实标为 stopped(timeout)，不伪造成功。

等待/收集循环与墙钟解耦（sleep/now 可注入），单元测试用假时钟覆盖。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

MAX_CHILDREN = 4
MAX_OBJECTIVE_CHARS = 600
#: 阻塞 join 的总上限。超过即请求停止未完成的子运行并如实汇报。
JOIN_TIMEOUT_SECONDS = 600.0
#: 触发超时后，给子运行响应 stop 请求的宽限。
STOP_GRACE_SECONDS = 30.0
POLL_SECONDS = 2.0

TERMINAL_STATUSES = ("succeeded", "failed", "denied", "stopped", "error")


def normalize_spawn_request(
    objectives: list[Any], parent_run_id: str | None
) -> list[str]:
    """校验并规整一次 spawn 请求；违规抛 ValueError（HTTP 层转 400）。"""
    if parent_run_id:
        raise ValueError(
            "子代理不能再派生（深度硬限 1）——递归编排是 v1 明确不做的"
        )
    cleaned: list[str] = []
    for raw in objectives or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if len(text) > MAX_OBJECTIVE_CHARS:
            raise ValueError(f"子任务目标最长 {MAX_OBJECTIVE_CHARS} 字")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ValueError("至少需要一个子任务目标")
    if len(cleaned) > MAX_CHILDREN:
        raise ValueError(f"一次最多派生 {MAX_CHILDREN} 个子代理（审批卡上的乘数承诺）")
    return cleaned


def wait_for_children(
    children: list[dict[str, Any]],
    *,
    get_state: Callable[[str], dict[str, Any]],
    deadline_monotonic: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    request_stop: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """阻塞至全部子运行终态或超时；返回 {timed_out, states}。

    ``get_state(run_id)`` 返回 {finished: bool, status: str, rounds: int}；
    超时路径先对未完成的子运行发起 stop（宽限期内再等），然后如实记账。
    """
    states: dict[str, dict[str, Any]] = {}
    timed_out = False
    while True:
        all_done = True
        for child in children:
            run_id = child["run_id"]
            state = states.get(run_id)
            if state and state.get("finished"):
                continue
            state = get_state(run_id)
            states[run_id] = state
            if not state.get("finished"):
                all_done = False
        if all_done:
            return {"timed_out": False, "states": states}
        if now() >= deadline_monotonic:
            timed_out = True
            break
        sleep(POLL_SECONDS)
    if timed_out:
        grace_end = now() + STOP_GRACE_SECONDS
        for child in children:
            state = states.get(child["run_id"]) or {}
            if not state.get("finished") and request_stop is not None:
                try:
                    request_stop(child["run_id"])
                except Exception:  # noqa: BLE001 — stop 失败如实记账即可
                    pass
        while now() < grace_end:
            pending = False
            for child in children:
                state = get_state(child["run_id"])
                states[child["run_id"]] = state
                if not state.get("finished"):
                    pending = True
            if not pending:
                break
            sleep(min(0.5, max(0.1, grace_end - now())))
        for child in children:
            state = states.setdefault(child["run_id"], {})
            if not state.get("finished"):
                state["status"] = "stopped"
                state["note"] = "join 超时，已请求停止"
    return {"timed_out": timed_out, "states": states}


def build_child_digest(
    *,
    objective: str,
    run_id: str,
    state: dict[str, Any],
    files: list[str],
    summary: str,
) -> dict[str, Any]:
    """单个子代理的回传摘要：结构固定，长度有顶。"""
    status = state.get("status") or "unknown"
    return {
        "run_id": run_id,
        "objective": objective[:MAX_OBJECTIVE_CHARS],
        "status": status,
        "rounds": state.get("rounds"),
        "files": [str(f) for f in files][:20],
        "summary": str(summary or "").strip()[:600] or "（子代理未产出可读摘要）",
    }


#: 子代理每个子运行的预算硬顶——spawn 审批卡上的乘数承诺以这里封底。
SPAWN_BUDGET_CEILINGS = {
    "maxCalls": 24,
    "maxReads": 64,
    "maxCostMicrousd": 1_000_000,
    "maxTokens": 200_000,
}


def validate_spawn_budget(budget: Any) -> dict[str, int] | None:
    """校验 spawn 请求的每子预算覆盖；违规抛 ValueError，None 原样通过。"""
    if budget is None:
        return None
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    cleaned: dict[str, int] = {}
    key_map = {"maxCalls": "maxCalls", "maxReads": "maxReads",
               "maxCostMicrousd": "maxCostMicrousd", "maxTokens": "maxTokens"}
    for key, ceiling in SPAWN_BUDGET_CEILINGS.items():
        value = budget.get(key)
        if value is None:
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"budget.{key} must be an integer")
        if value <= 0 or value > ceiling:
            raise ValueError(f"budget.{key} 必须在 1–{ceiling} 之间")
        cleaned[key_map[key]] = value
    if not cleaned:
        return None
    return cleaned


class SubagentChannel:
    """一个父运行的编排通道：spawn / status / read。

    治理在这里落地：深度硬限、数量硬顶、子运行经 mgr.create 走与手创
    运行相同的流水线；read 狱到「本运行的子代理」的工作区——父运行
    读不到兄弟或陌生运行的产品。
    """

    def __init__(self, manager, run) -> None:
        self.manager = manager
        self.run = run

    # ---------------------------------------------------------------- spawn

    def spawn(self, arguments: dict[str, Any]) -> dict[str, Any]:
        import time as _time

        objectives = normalize_spawn_request(
            list(arguments.get("objectives") or []),
            (self.run.spec or {}).get("parent_run_id"),
        )
        budget = validate_spawn_budget(arguments.get("budget"))
        started = _time.monotonic()
        children = []
        for objective in objectives:
            child_spec: dict[str, Any] = {"mode": "subagent", "parent_run_id": self.run.id}
            if budget:
                child_spec["budget"] = budget
            child = self.manager.create(
                objective=objective,
                template="general-subagent",
                # 内部角色:操作者已通过 spawn 审批 consciously 批准了扇出;
                # 子运行的写入被物理限制在自己的沙箱(runs/<child>/workspace),
                # 不碰父工作区——审批挂起只会把 join 拖过超时,无治理增益。
                approval="off",
                spec=child_spec,
                model_key=self.run.model,
                effort=self.run.effort,
                source="subagent",
            )
            children.append({"run_id": child.id, "objective": objective})

        join = wait_for_children(
            children,
            get_state=self._child_state,
            deadline_monotonic=started + JOIN_TIMEOUT_SECONDS,
            request_stop=self._request_stop,
        )
        digests = [self._digest(child, join["states"]) for child in children]
        return {"children": digests, "timed_out": bool(join.get("timed_out"))}

    # --------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        children = [
            run for run in self.manager.runs.values()
            if (run.spec or {}).get("parent_run_id") == self.run.id
        ]
        return {
            "children": [
                {"run_id": c.id, "objective": (c.objective or "")[:200],
                 "status": c.status, "rounds": c.rounds}
                for c in children
            ],
            "count": len(children),
        }

    # ----------------------------------------------------------------- read

    def read(self, run_id: str, path: str) -> dict[str, Any]:
        child = self.manager.runs.get(str(run_id or ""))
        if child is None or (child.spec or {}).get("parent_run_id") != self.run.id:
            raise ValueError("只能读取本运行派生的子代理工作区")
        from flowagent.tools.general.sandbox import WorkspaceSandbox

        child_sandbox = WorkspaceSandbox(self.manager.runs_dir / child.id / "workspace")
        record = child_sandbox.read(str(path or ""))
        record["child_run_id"] = child.id
        return record

    # -------------------------------------------------------------- helpers

    def _child_state(self, run_id: str) -> dict[str, Any]:
        run = self.manager.runs.get(run_id)
        if run is None:
            return {"finished": True, "status": "error", "rounds": 0}
        return {"finished": bool(run.finished), "status": run.status, "rounds": run.rounds}

    def _request_stop(self, run_id: str) -> None:
        run = self.manager.runs.get(run_id)
        if run is not None:
            with run.lock:
                run.stop_requested = True

    def _digest(self, child: dict[str, Any], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        state = states.get(child["run_id"]) or {}
        run = self.manager.runs.get(child["run_id"])
        files: list[str] = []
        summary = ""
        if run is not None and run.trace_path:
            import json as _json

            try:
                trace = _json.loads(Path(run.trace_path).read_text(encoding="utf-8"))
                terminal = trace.get("terminal") or {}
                if terminal.get("failure"):
                    summary = f"{terminal['failure'].get('code', '')}: {terminal['failure'].get('message', '')}"
                else:
                    result = terminal.get("result") or {}
                    summary = str(result.get("summary") or result.get("result_truth") or "")
            except Exception:  # noqa: BLE001 — 摘要失败不给父模型编故事
                summary = ""
            workspace = self.manager.runs_dir / child["run_id"] / "workspace"
            if workspace.is_dir():
                files = sorted(
                    p.relative_to(workspace).as_posix()
                    for p in workspace.rglob("*")
                    if p.is_file() and ".git" not in p.parts
                )[:20]
        return build_child_digest(
            objective=child["objective"],
            run_id=child["run_id"],
            state=state,
            files=files,
            summary=summary,
        )
