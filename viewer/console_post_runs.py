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
    spec = body.get("spec")
    if spec is not None:
        if not isinstance(spec, dict):
            return handler._json({"error": "spec must be an object"}, 400)
        if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > 4096:
            return handler._json({"error": "spec is too large"}, 400)
        try:
            mgr.validate_delegation_spec(spec, scenario)
        except ValueError as exc:
            return handler._json({"error": str(exc)}, 400)
    model_field = body.get("model")
    effort_field = body.get("effort")
    try:
        model_key, effort = mgr.resolve_model(
            model_field if isinstance(model_field, str) else None,
            effort_field if isinstance(effort_field, str) else None,
        )
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    run = mgr.create(objective, template, approval, spec, model_key=model_key, effort=effort)
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
    result = control.mutate_message(
        str(body.get("messageId") or ""), str(body.get("op") or ""), body.get("text"),
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
    except (KeyError, TypeError, ValueError):
        return handler._json(
            {"error": "maxCalls, maxReads and maxCostMicrousd are required integers"}, 400,
        )
    current = control.current_limits()
    if (
        max_calls < current["max_calls"]
        or max_reads < current["max_reads"]
        or max_cost < current["max_cost_microusd"]
    ):
        return handler._json({"error": "limits can only be raised"}, 400)
    control.queue({
        "kind": "budget", "maxCalls": max_calls, "maxReads": max_reads, "maxCostMicrousd": max_cost,
    })
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
    except (KeyError, TypeError, ValueError):
        return handler._json(
            {"error": "maxCalls, maxReads and maxCostMicrousd are required integers"}, 400,
        )
    if min(max_calls, max_reads, max_cost) < 1 or not 0 <= extra_rounds <= 50:
        return handler._json(
            {"error": "limits must be positive and extraRounds between 0 and 50"}, 400,
        )
    try:
        handler.manager.resume(
            run,
            limits={"maxCalls": max_calls, "maxReads": max_reads, "maxCostMicrousd": max_cost},
            extra_rounds=extra_rounds,
        )
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
