"""HTTP POST handlers for run creation, control, review and metadata."""

from __future__ import annotations

import json
import uuid


def _run(handler, run_id: str):
    run = handler.manager.get(run_id)
    if run is None:
        handler._json({"error": "no such run"}, 404)
    return run


def _live_control(handler, run):
    if run.finished:
        handler._json({"error": "run has finished"}, 409)
        return None
    control = getattr(run, "controller", None)
    if control is None:
        handler._json({"error": "run has no live control"}, 409)
    return control


def post_run_create(handler):
    mgr = handler.manager
    if mgr.runtime is None:
        return handler._json({"error": f"live mode unavailable: {mgr.runtime_error}"}, 503)
    body = handler._read_body()
    template = str(body.get("template") or "full")
    objective = str(body.get("objective") or "").strip()
    try:
        scenario = mgr.runtime.get_scenario(template)
    except Exception:
        return handler._json({"error": f"unknown template {template!r}"}, 400)
    if not objective:
        objective = scenario.objective
    if len(objective) > 600:
        return handler._json({"error": "objective too long"}, 400)
    approval = str(body.get("approval") or "consequential")
    if approval not in ("off", "consequential", "all"):
        return handler._json({"error": "approval must be off, consequential or all"}, 400)
    project_id = str(body.get("projectId") or "").strip()
    spec = body.get("spec")
    if spec is not None and not isinstance(spec, dict):
        return handler._json({"error": "spec must be an object"}, 400)
    if not project_id and isinstance(spec, dict):
        # The composer carries the project identity inside the delegation spec;
        # the top-level field is for direct API callers.
        project_id = str(spec.get("project_id") or "").strip()
    try:
        spec = mgr.resolve_project_spec(spec, project_id, scenario)
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    if spec is not None:
        if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > 4096:
            return handler._json({"error": "spec is too large"}, 400)
        try:
            mgr.validate_delegation_spec(spec, scenario)
        except ValueError as exc:
            error = {"error": str(exc)}
            if getattr(exc, "code", ""):
                error["code"] = exc.code
            return handler._json(error, 400)
    # Fast experiment: the run is deliberately unattended, so per-action
    # approvals would just block forever. Gate, budget and receipts still run.
    if isinstance(spec, dict) and spec.get("fast"):
        approval = "off"
    model_field = body.get("model")
    effort_field = body.get("effort")
    try:
        model_key, effort = mgr.resolve_model(
            model_field if isinstance(model_field, str) else None,
            effort_field if isinstance(effort_field, str) else None,
        )
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    run = mgr.create(
        objective, template, approval, spec, model_key=model_key, effort=effort,
        project_id=project_id,
    )
    return handler._json(run.to_json(detail=True), 202)


def post_run_review(handler, run_id: str):
    mgr = handler.manager
    if mgr.runtime is None:
        return handler._json({"error": f"live mode unavailable: {mgr.runtime_error}"}, 503)
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    decision = body.get("decision")
    if decision not in mgr.runtime.REVIEW_DECISIONS:
        return handler._json(
            {"error": f"decision must be one of {', '.join(mgr.runtime.REVIEW_DECISIONS)}"}, 400,
        )
    reason = body.get("reason")
    if reason is None:
        reason = ""
    if not isinstance(reason, str) or len(reason) > mgr.runtime.MAX_REASON:
        return handler._json(
            {"error": f"reason must be a string of at most {mgr.runtime.MAX_REASON} characters"}, 400,
        )
    reviewer = body.get("reviewer")
    if reviewer is None:
        reviewer = ""
    if not isinstance(reviewer, str) or len(reviewer) > mgr.runtime.MAX_REVIEWER:
        return handler._json(
            {"error": f"reviewer must be a string of at most {mgr.runtime.MAX_REVIEWER} characters"}, 400,
        )
    try:
        payload = mgr.record_review(run.id, decision, reason, reviewer)
    except KeyError:
        return handler._json({"error": "no such run"}, 404)
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 409)
    return handler._json(payload)


def post_run_stop(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    run.stop_requested = True
    return handler._json({"stop": True, "id": run.id})


def post_run_decision(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    mgr = handler.manager
    # LangGraph 引擎：审批挂起时 run 线程已结束（无 live control），
    # 决策以 Command(resume) 送回挂起的图——必须在 live-control 回退之前分流。
    if mgr.graph_approval_pending(run):
        result = mgr.resume_graph_decision(
            run_id,
            str(body.get("approvalId") or ""),
            allow=bool(body.get("allow")),
            remember=bool(body.get("remember")),
        )
        if not result.get("ok"):
            return handler._json({"error": result.get("error", "resume failed")}, 409)
        return handler._json({"ok": True})
    control = getattr(run, "controller", None)
    if control is None:
        return handler._json({"error": "run is not waiting for a decision"}, 409)
    if not control.decide(
        str(body.get("approvalId") or ""),
        allow=bool(body.get("allow")),
        remember=bool(body.get("remember")),
    ):
        return handler._json({"error": "no pending approval with that id"}, 409)
    return handler._json({"ok": True})


def post_run_answer(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    question_id = str(body.get("questionId") or "").strip()
    if not question_id:
        return handler._json({"error": "questionId is required"}, 400)
    text = body.get("text")
    skip = bool(body.get("skip"))
    control = getattr(run, "controller", None)
    if control is None:
        return handler._json({"error": "this run is not accepting answers"}, 409)
    if skip:
        ok = control.skip_question(question_id)
    else:
        if not isinstance(text, str) or not text.strip():
            return handler._json({"error": "text is required unless skip is set"}, 400)
        if len(text) > 600:
            return handler._json({"error": "text must be at most 600 characters"}, 400)
        ok = control.answer(question_id, text)
    if not ok:
        return handler._json({"error": "that question is not pending"}, 409)
    return handler._json({"ok": True, "questionId": question_id, "skipped": skip})


def post_run_message(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    control = _live_control(handler, run)
    if control is None:
        return None
    body = handler._read_body()
    text = str(body.get("text") or "").strip()
    if not text:
        return handler._json({"error": "text is required"}, 400)
    if len(text) > 600:
        return handler._json({"error": "message too long"}, 400)
    message_id = uuid.uuid4().hex[:10]
    control.queue({"kind": "message", "text": text, "id": message_id})
    return handler._json({"queued": True, "id": run.id, "messageId": message_id}, 202)


def post_run_steer(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    control = _live_control(handler, run)
    if control is None:
        return None
    result = control.steer_message(str(handler._read_body().get("messageId") or ""))
    if not result.get("ok"):
        return handler._json({"error": result.get("error", "steer failed")}, 409)
    return handler._json(result)


def post_run_queue(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    control = _live_control(handler, run)
    if control is None:
        return None
    body = handler._read_body()
    raw_index = body.get("toIndex")
    to_index = None
    if raw_index is not None:
        try:
            to_index = int(raw_index)
        except (TypeError, ValueError):
            to_index = None
    result = control.mutate_message(
        str(body.get("messageId") or ""), str(body.get("op") or ""), body.get("text"),
        to_index=to_index,
    )
    if not result.get("ok"):
        return handler._json({"error": result.get("error", "queue op failed")}, 409)
    return handler._json(result)


def post_run_plan(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    control = _live_control(handler, run)
    if control is None:
        return None
    body = handler._read_body()
    action = str(body.get("action") or "")
    todo_id = str(body.get("todoId") or "")
    if action not in ("skip", "promote") or not todo_id:
        return handler._json({"error": "action must be skip or promote, with a todoId"}, 400)
    control.queue({
        "kind": "plan",
        "action": action,
        "todo_id": todo_id,
        "note": str(body.get("note") or "")[:200],
    })
    return handler._json({"queued": True, "applies": "next-round"})


def post_run_budget(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    control = _live_control(handler, run)
    if control is None:
        return None
    body = handler._read_body()
    try:
        max_calls = int(body["maxCalls"])
        max_reads = int(body["maxReads"])
        max_cost = int(body["maxCostMicrousd"])
        max_tokens = int(body["maxTokens"]) if body.get("maxTokens") is not None else None
        request_id = str(body.get("requestId") or "").strip()[:120] or None
    except (KeyError, TypeError, ValueError):
        return handler._json(
            {"error": "maxCalls, maxReads and maxCostMicrousd are required integers"}, 400,
        )
    current = control.current_limits()
    if (
        max_calls < current["max_calls"]
        or max_reads < current["max_reads"]
        or max_cost < current["max_cost_microusd"]
        or (max_tokens is not None and max_tokens < current["max_tokens"])
    ):
        return handler._json({"error": "limits can only be raised"}, 400)
    command = {
        "kind": "budget", "maxCalls": max_calls, "maxReads": max_reads,
        "maxCostMicrousd": max_cost,
    }
    if max_tokens is not None:
        command["maxTokens"] = max_tokens
    if request_id is not None:
        command["requestId"] = request_id
    control.queue(command)
    return handler._json({"queued": True, "applies": "next-round"})


def post_run_resume(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    try:
        max_calls = int(body["maxCalls"])
        max_reads = int(body["maxReads"])
        max_cost = int(body["maxCostMicrousd"])
        extra_rounds = int(body.get("extraRounds") or 6)
        # 可选:token 顶。价格缩放按 tokens 换算可负担调用数,长任务需要它。
        max_tokens = int(body["maxTokens"]) if body.get("maxTokens") is not None else None
        request_id = str(body.get("requestId") or "").strip()[:120] or None
    except (KeyError, TypeError, ValueError):
        return handler._json(
            {"error": "maxCalls, maxReads and maxCostMicrousd are required integers"}, 400,
        )
    if min(max_calls, max_reads, max_cost) < 1 or not 0 <= extra_rounds <= 50:
        return handler._json(
            {"error": "limits must be positive and extraRounds between 0 and 50"}, 400,
        )
    limits = {"maxCalls": max_calls, "maxReads": max_reads, "maxCostMicrousd": max_cost}
    if max_tokens is not None:
        if max_tokens < 1:
            return handler._json({"error": "maxTokens must be positive"}, 400)
        limits["maxTokens"] = max_tokens
    try:
        handler.manager.resume(
            run, limits=limits, extra_rounds=extra_rounds, request_id=request_id,
        )
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 409)
    return handler._json(run.to_json(detail=True), 202)


#: Recovery is a fresh round, not a top-up of the disputed one (which stays
#: charged and unknown). These are the operator's default hard ceilings for it.
RECOVER_DEFAULT_LIMITS = {
    "maxCalls": 40,
    "maxReads": 160,
    "maxCostMicrousd": 1_000_000,
    "maxTokens": 300_000,
    "extraRounds": 40,
}


def post_run_recover(handler, run_id: str):
    """Explicit, one-shot recovery of an unknown model outcome (202/409).

    The body is optional: an operator clicking 「确认恢复」 gets the documented
    defaults. A run in any other state is a 409 — recovery is never a generic
    "continue" button.
    """
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body() or {}
    defaults = RECOVER_DEFAULT_LIMITS
    try:
        max_calls = int(body.get("maxCalls", defaults["maxCalls"]))
        max_reads = int(body.get("maxReads", defaults["maxReads"]))
        max_cost = int(body.get("maxCostMicrousd", defaults["maxCostMicrousd"]))
        max_tokens = int(body.get("maxTokens", defaults["maxTokens"]))
        extra_rounds = int(body.get("extraRounds", defaults["extraRounds"]))
    except (TypeError, ValueError):
        return handler._json({"error": "recovery limits must be integers"}, 400)
    if min(max_calls, max_reads, max_cost, max_tokens) < 1 or not 0 <= extra_rounds <= 50:
        return handler._json(
            {"error": "limits must be positive and extraRounds between 0 and 50"}, 400,
        )
    limits = {
        "maxCalls": max_calls,
        "maxReads": max_reads,
        "maxCostMicrousd": max_cost,
        "maxTokens": max_tokens,
    }
    try:
        handler.manager.recover_unknown(run, limits=limits, extra_rounds=extra_rounds)
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 409)
    return handler._json(run.to_json(detail=True), 202)


def post_run_meta(handler, run_id: str):
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    if "name" in body:
        run.name = str(body.get("name") or "").strip()[:60]
    if "pinned" in body:
        run.pinned = bool(body.get("pinned"))
    handler.manager._write_meta(run)
    return handler._json(run.to_json())


def post_schedules_upsert(handler):
    """定时任务：创建/更新排程（调度器到点自动创建运行）。"""
    import datetime

    from console_scheduler import validate_schedule_payload

    mgr = handler.manager
    if getattr(mgr, "schedules", None) is None:
        return handler._json({"error": "scheduler is unavailable"}, 503)
    body = handler._read_body()
    try:
        fields = validate_schedule_payload(body)
        if fields.get("model") is not None:
            model, effort = mgr.resolve_model(fields["model"], fields.get("effort"))
            fields["model"], fields["effort"] = model, effort
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    entry = mgr.schedules.upsert(fields)
    now = datetime.datetime.now()
    entry = dict(entry)
    entry["next"] = describe_next_safe(entry, now)
    return handler._json({"schedule": entry})


def describe_next_safe(entry, now):
    try:
        from console_scheduler import describe_next

        return describe_next(entry, now)
    except Exception:  # noqa: BLE001 — 展示字段失败不影响保存
        return ""


def post_schedules_delete(handler):
    mgr = handler.manager
    if getattr(mgr, "schedules", None) is None:
        return handler._json({"error": "scheduler is unavailable"}, 503)
    body = handler._read_body()
    entry_id = body.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        return handler._json({"error": "id must be a string"}, 400)
    if not mgr.schedules.delete(entry_id):
        return handler._json({"error": "schedule not found"}, 404)
    return handler._json({"ok": True})


def post_schedules_toggle(handler):
    mgr = handler.manager
    if getattr(mgr, "schedules", None) is None:
        return handler._json({"error": "scheduler is unavailable"}, 503)
    body = handler._read_body()
    entry_id = body.get("id")
    enabled = body.get("enabled")
    if not isinstance(entry_id, str) or not entry_id or not isinstance(enabled, bool):
        return handler._json({"error": "id and enabled are required"}, 400)
    if not mgr.schedules.set_enabled(entry_id, enabled):
        return handler._json({"error": "schedule not found"}, 404)
    return handler._json({"ok": True})


# ------------------------------------------------------------------ projects


def _projects(handler):
    mgr = handler.manager
    if getattr(mgr, "projects", None) is None:
        handler._json({"error": "project store is unavailable"}, 503)
        return None
    return mgr.projects


def post_project_create(handler):
    mgr = handler.manager
    store = _projects(handler)
    if store is None:
        return None
    body = handler._read_body()
    try:
        project = store.create(
            body.get("name"),
            body.get("path"),
            body.get("verification") or "",
        )
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    return handler._json(project, 201)


def post_project_update(handler):
    mgr = handler.manager
    store = _projects(handler)
    if store is None:
        return None
    body = handler._read_body()
    project_id = body.get("id")
    if not isinstance(project_id, str) or not project_id:
        return handler._json({"error": "id must be a string"}, 400)
    try:
        project = store.update(
            project_id,
            name=body.get("name"),
            verification=body.get("verification"),
            archived=body.get("archived"),
        )
    except KeyError:
        return handler._json({"error": "project not found"}, 404)
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    return handler._json(project)


def post_project_archive(handler):
    mgr = handler.manager
    store = _projects(handler)
    if store is None:
        return None
    body = handler._read_body()
    project_id = body.get("id")
    if not isinstance(project_id, str) or not project_id:
        return handler._json({"error": "id must be a string"}, 400)
    try:
        project = store.archive(project_id)
    except KeyError:
        return handler._json({"error": "project not found"}, 404)
    return handler._json(project)


def post_run_project(handler, run_id: str):
    """Move a run into (or out of) a project after the fact."""
    mgr = handler.manager
    run = _run(handler, run_id)
    if run is None:
        return None
    body = handler._read_body()
    project_id = body.get("projectId")
    if project_id is not None and not isinstance(project_id, str):
        return handler._json({"error": "projectId must be a string"}, 400)
    try:
        run = mgr.assign_run_project(run_id, project_id or "")
    except KeyError:
        return handler._json({"error": "no such run"}, 404)
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    return handler._json(run.to_json())
