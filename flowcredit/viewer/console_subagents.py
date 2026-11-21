"""子代理编排的纯逻辑层（console 侧）。

治理不变量在这里落地，RunDriver 的 `_spawn_channel` 只做接线：
- 深度硬限 1：子代理不能再 spawn（parent_run_id 已存在的运行拒绝）；
- 数量硬顶 4：乘数是审批卡上明示的承诺；
- 目标是一句话而不是一篇文章（≤600 字），空列表显式拒绝；
- 触发超时把未完成的子运行如实标为 stopped(timeout)，不伪造成功。

S2b 写入子代理（隔离拷贝 + 显式应用）：
- 写子代理拿父工作区的**拷贝**（零共享），改完的文件对父不可见；
- 播种时写基线哈希快照；父要用 `apply_child_changes` 显式应用（write
  effect → 审批），逐文件做冲突检测——父在等待期间改过同一文件即拒绝；
- 删除不自动应用（v1 保守），只报告。

等待/收集循环与墙钟解耦（sleep/now 可注入），单元测试用假时钟覆盖。
"""

from __future__ import annotations

import hashlib
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


#: 写扇出的数量硬顶——写入比只读贵，乘数承诺更严。
MAX_WRITE_CHILDREN = 3
#: 应用改动的封底。
MAX_APPLY_FILES = 20
MAX_APPLY_BYTES = 2_000_000
#: 子工作区拷贝的文件数上限（防止拷进巨型目录）。
MAX_SEED_FILES = 2_000


def snapshot_hashes(root: Path) -> dict[str, str]:
    """{相对路径: sha256}；跳过 .git 与隐藏元数据。超限直接拒绝。"""
    root = Path(root)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/") or "/.git/" in rel or path.name == ".DS_Store":
            continue
        if len(out) >= MAX_SEED_FILES:
            raise ValueError(f"工作区文件超过 {MAX_SEED_FILES} 个，拒绝拷贝")
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def snapshot_hashes_single(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def diff_snapshot(baseline: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    """基线 vs 当前：modified / added / deleted。"""
    modified = [rel for rel, digest in current.items()
                if rel in baseline and baseline[rel] != digest]
    added = [rel for rel in current if rel not in baseline]
    deleted = [rel for rel in baseline if rel not in current]
    return {"modified": sorted(modified), "added": sorted(added), "deleted": sorted(deleted)}


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
            # 超长降级为截断（2026-11-21）：模型给子代理写实施简报是好的
            # 委托习惯，拒绝整次请求只会杀死 run；细节由子代理自己读工作区。
            text = text[:MAX_OBJECTIVE_CHARS - 1].rstrip() + "…"
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

        write_access = bool(arguments.get("write_access"))
        objectives = normalize_spawn_request(
            list(arguments.get("objectives") or []),
            (self.run.spec or {}).get("parent_run_id"),
        )
        if write_access and len(objectives) > MAX_WRITE_CHILDREN:
            raise ValueError(
                f"写子代理一次最多 {MAX_WRITE_CHILDREN} 个（写入乘数比只读更贵）"
            )
        budget = validate_spawn_budget(arguments.get("budget"))
        parent_workspace = self.manager.runs_dir / self.run.id / "workspace"
        started = _time.monotonic()
        children = []
        for objective in objectives:
            child_spec: dict[str, Any] = {"mode": "subagent", "parent_run_id": self.run.id}
            if budget:
                child_spec["budget"] = budget
            template = "general-subagent"
            if write_access:
                # 隔离拷贝:子代理拿父工作区的副本改,改完由父显式 apply。
                template = "general-subagent-writer"
                child_spec["workspace_copy_from"] = str(parent_workspace)
            child = self.manager.create(
                objective=objective,
                template=template,
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

    # --------------------------------------------------------------- apply

    def apply_changes(self, run_id: str, paths: list[Any] | None = None) -> dict[str, Any]:
        """把子代理的写入**显式**应用到父工作区（write effect，过审批）。

        逐文件冲突检测：父在子代理运行期间改过同一文件（当前哈希 ≠ 播种
        基线）即拒绝该文件并报告冲突——绝不用旧的子版本覆盖父的新改动。
        应用 = 文本写回父沙箱（越狱与大小由 WorkspaceSandbox.write 把关）。
        """
        child = self.manager.runs.get(str(run_id or ""))
        if child is None or (child.spec or {}).get("parent_run_id") != self.run.id:
            raise ValueError("只能应用本运行派生的子代理改动")
        if not child.finished:
            raise ValueError("子代理还在运行——等它终态后再应用")
        child_dir = self.manager.runs_dir / child.id
        baseline_path = child_dir / "workspace_baseline.json"
        if not baseline_path.is_file():
            raise ValueError("该子代理没有隔离拷贝基线（不是写子代理？）")
        import json as _json

        baseline = _json.loads(baseline_path.read_text(encoding="utf-8"))
        child_ws = child_dir / "workspace"
        parent_ws = self.manager.runs_dir / self.run.id / "workspace"
        if not child_ws.is_dir() or not parent_ws.is_dir():
            raise ValueError("工作区缺失，无法应用")

        current_child = snapshot_hashes(child_ws)
        changes = diff_snapshot(baseline, current_child)
        wanted = [str(p).strip() for p in (paths or []) if str(p).strip()]
        candidates = wanted if wanted else sorted(changes["modified"] + changes["added"])
        if len(candidates) > MAX_APPLY_FILES:
            raise ValueError(
                f"一次最多应用 {MAX_APPLY_FILES} 个文件——请用 paths 分批应用"
            )
        # 删除只报告不应用(v1 保守):子代理删的文件不会从父工作区消失。
        deleted_reported = list(changes["deleted"])
        candidates = [rel for rel in candidates if rel not in changes["deleted"]]

        from flowagent.tools.general.sandbox import WorkspaceSandbox

        parent = WorkspaceSandbox(parent_ws)
        applied: list[str] = []
        conflicts: list[dict[str, str]] = []
        unchanged: list[str] = []
        refused: list[dict[str, str]] = []
        total_bytes = 0
        for rel in candidates:
            child_file = child_ws / rel
            if not child_file.is_file():
                refused.append({"path": rel, "reason": "子代理没有这个文件"})
                continue
            child_hash = current_child.get(rel)
            if rel in baseline and baseline[rel] == child_hash:
                unchanged.append(rel)
                continue
            parent_file = parent_ws / rel
            parent_hash = None
            if parent_file.is_file():
                parent_hash = snapshot_hashes_single(parent_file)
            if parent_hash != baseline.get(rel):
                conflicts.append({
                    "path": rel,
                    "reason": "父工作区在子代理运行期间改过该文件（或状态不一致）",
                })
                continue
            data = child_file.read_bytes()
            total_bytes += len(data)
            if total_bytes > MAX_APPLY_BYTES:
                raise ValueError(f"累计应用超过 {MAX_APPLY_BYTES} 字节——请用 paths 分批")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                refused.append({"path": rel, "reason": "非 UTF-8 文本，v1 不应用二进制"})
                continue
            parent.write(rel, text)
            applied.append(rel)
        return {
            "applied": applied,
            "conflicts": conflicts,
            "unchanged": unchanged,
            "refused": refused,
            "deleted_reported": deleted_reported,
            "child_run_id": child.id,
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
