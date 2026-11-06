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
