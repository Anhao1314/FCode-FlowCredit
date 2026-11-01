"""POST route table. Authentication and mutation guards stay in server.py."""

from __future__ import annotations

from console_post_chat import (
    post_chat_confirm,
    post_chat_edit,
    post_chat_regenerate,
    post_chat_send,
    post_intake,
    post_chat_config,
)
from console_post_runs import (
    post_run_answer,
    post_run_budget,
    post_run_create,
    post_run_decision,
    post_run_message,
    post_run_meta,
    post_run_plan,
    post_run_queue,
    post_run_resume,
    post_run_review,
    post_run_steer,
    post_run_stop,
)
from audit_jobs import start_audit_run


def post_audit_run(handler):
    """`/audit <target>` 斜杠命令的落点:离线审计管线跑成后台作业。"""
    repo = type(handler).repo
    if repo is None:
        return handler._json({"error": "no repo; audit unavailable"}, 503)
    body = handler._read_body()
    target = str(body.get("target") or "").strip()
    if not target:
        return handler._json({"error": "target is required"}, 400)
    try:
        job_id = start_audit_run(repo, target)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"job_id": job_id, "status": "running"})


_STATIC = {
    "/api/intake": post_intake,
    "/api/settings/chat-config": post_chat_config,
    "/api/chat/send": post_chat_send,
    "/api/runs": post_run_create,
    "/api/audit/run": post_audit_run,
}
_CHAT_ROUTES = {
    "/confirm": post_chat_confirm,
    "/edit": post_chat_edit,
    "/regenerate": post_chat_regenerate,
}
_RUN_ROUTES = {
    "/review": post_run_review,
    "/stop": post_run_stop,
    "/decision": post_run_decision,
    "/answer": post_run_answer,
    "/messages": post_run_message,
    "/steer": post_run_steer,
    "/queue": post_run_queue,
    "/plan": post_run_plan,
    "/budget": post_run_budget,
    "/resume": post_run_resume,
    "/meta": post_run_meta,
}


def _suffix(path: str, prefix: str, routes: dict) -> tuple[str, object] | None:
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix):]
    for suffix, handler in routes.items():
        if remainder.endswith(suffix):
            entity_id = remainder[:-len(suffix)]
            if entity_id and "/" not in entity_id:
                return entity_id, handler
    return None


def dispatch_post(handler, path: str):
    route = _STATIC.get(path)
    if route is not None:
        return route(handler)
    match = _suffix(path, "/api/chat/threads/", _CHAT_ROUTES)
    if match is not None:
        thread_id, route = match
        return route(handler, thread_id)
    match = _suffix(path, "/api/runs/", _RUN_ROUTES)
    if match is not None:
        run_id, route = match
        return route(handler, run_id)
    return handler._json({"error": "unknown endpoint"}, 404)
