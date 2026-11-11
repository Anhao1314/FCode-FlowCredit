"""POST route table. Authentication and mutation guards stay in server.py."""

from __future__ import annotations

from console_post_chat import (
    post_chat_confirm,
    post_chat_tool_decision,
    post_chat_edit,
    post_chat_regenerate,
    post_chat_send,
    post_intake,
    post_chat_config,
    post_chat_memory,
    post_chat_memory_delete,
)
from console_post_runs import (
    post_project_archive,
    post_project_create,
    post_project_update,
    post_run_answer,
    post_run_budget,
    post_run_create,
    post_run_decision,
    post_run_message,
    post_run_meta,
    post_run_plan,
    post_run_project,
    post_run_queue,
    post_run_resume,
    post_run_review,
    post_run_steer,
    post_run_stop,
    post_schedules_upsert,
    post_schedules_delete,
    post_schedules_toggle,
)


def post_audit_run(handler):
    """`/挖洞 <target>` 与 `/audit <target>` 的落点:离线审计管线跑成后台作业。
    adapter 可显式指定；缺省按目标位置解析（仓库内=repo,放行根内=rea）。"""
    audit = getattr(handler, "audit", None)
    if audit is None or audit.repo is None:
        return handler._json({"error": "no repo; audit unavailable"}, 503)
    body = handler._read_body()
    target = str(body.get("target") or "").strip()
    if not target:
        return handler._json({"error": "target is required"}, 400)
    adapter = str(body.get("adapter") or "").strip() or None
    try:
        job_id = audit.start_run(target, adapter)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"job_id": job_id, "status": "running"})


def post_plugin_install(handler):
    """Install one curated MCP plugin by writing the host-reviewed mcp.json."""
    market = getattr(handler, "plugins", None)
    if market is None:
        return handler._json({"error": "plugin market is unavailable"}, 503)
    body = handler._read_body()
    try:
        entry = market.install(str(body.get("id") or ""))
    except Exception as error:  # noqa: BLE001 — market errors are user-facing
        code = 404 if "unknown plugin" in str(error) else 400
        return handler._json({"error": str(error)}, code)
    handler.manager.reload_mcp()
    return handler._json({"plugin": entry}, 202)


def post_plugin_uninstall(handler):
    market = getattr(handler, "plugins", None)
    if market is None:
        return handler._json({"error": "plugin market is unavailable"}, 503)
    body = handler._read_body()
    try:
        entry = market.uninstall(str(body.get("id") or ""))
    except Exception as error:  # noqa: BLE001 — market errors are user-facing
        code = 404 if "unknown plugin" in str(error) else 400
        return handler._json({"error": str(error)}, code)
    handler.manager.reload_mcp()
    return handler._json({"plugin": entry}, 202)


def post_gov_bot_run(handler, bot_id):
    """Dispatch one manual run to a governance bot (auditor / verifier)."""
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    try:
        run = service.start_run(bot_id, str(body.get("target") or "").strip())
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"run": run}, 202)


def post_gov_bot_settings(handler, bot_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    try:
        settings = service.set_settings(bot_id, body)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"settings": settings})


def post_gov_run_approve(handler, run_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        run = service.approve_run(run_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"run": run})


def post_gov_run_reject(handler, run_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        run = service.reject_run(run_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"run": run})


def post_gov_skill(handler):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    try:
        skill = service.create_skill(
            str(body.get("run_id") or ""), str(body.get("name") or ""),
        )
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"skill": skill}, 201)


def post_gov_skill_delete(handler, skill_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        service.delete_skill(skill_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 404)
    return handler._json({"deleted": skill_id})


def post_gov_routine(handler):
    """Create a standing routine for one bot (cron-like manual form)."""
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    try:
        routine = service.create_routine(str(body.get("bot_id") or ""), body)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"routine": routine}, 201)


def post_gov_routine_toggle(handler, routine_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    try:
        routine = service.set_routine_enabled(routine_id, bool(body.get("enabled")))
    except ValueError as error:
        return handler._json({"error": str(error)}, 404)
    return handler._json({"routine": routine})


def post_gov_routine_delete(handler, routine_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        service.delete_routine(routine_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 404)
    return handler._json({"deleted": routine_id})


_STATIC = {
    "/api/intake": post_intake,
    "/api/projects": post_project_create,
    "/api/projects/update": post_project_update,
    "/api/projects/archive": post_project_archive,
    "/api/settings/chat-config": post_chat_config,
    "/api/chat/send": post_chat_send,
    "/api/chat/memory": post_chat_memory,
    "/api/schedules": post_schedules_upsert,
    "/api/schedules/delete": post_schedules_delete,
    "/api/schedules/toggle": post_schedules_toggle,
    "/api/chat/memory/delete": post_chat_memory_delete,
    "/api/runs": post_run_create,
    "/api/audit/run": post_audit_run,
    "/api/gov/routines": post_gov_routine,
    "/api/gov/skills": post_gov_skill,
    "/api/plugins/install": post_plugin_install,
    "/api/plugins/uninstall": post_plugin_uninstall,
}
_CHAT_ROUTES = {
    "/confirm": post_chat_confirm,
    "/tool-decision": post_chat_tool_decision,
    "/edit": post_chat_edit,
    "/regenerate": post_chat_regenerate,
}
def post_run_apply_patch(handler, run_id):
    """把运行产出的 changes.patch 应用到原项目目录（operator 显式动作）。

    治理边界：
    - 只对终态运行；项目源目录必须是服务端记录的 project.json.source，
      不接受客户端传路径；
    - `git apply --check` 先做冲突检测——原目录在运行期间改过就拒绝，
      绝不用旧补丁覆盖新改动；check 通过才 apply（git apply 本身原子，
      全成或全不动）；
    - 回执写 runs/<id>/apply.json（补丁 sha256 + 文件清单 + 时间），
      应用过的证据留在运行目录，不依赖会话内存。
    """
    import hashlib
    import json as _json
    import subprocess
    import time as _time
    from pathlib import Path as _Path

    mgr = type(handler).manager
    run = mgr.get(str(run_id or ""))
    if run is None:
        return handler._json({"error": "no such run"}, 404)
    if not getattr(run, "finished", False):
        return handler._json({"error": "run is still in progress; wait for its terminal state"}, 409)
    run_dir = _Path(mgr.runs_dir) / run.id
    patch_path = run_dir / "changes.patch"
    project_path = run_dir / "project.json"
    if not patch_path.is_file():
        return handler._json({"error": "this run has no changes.patch (no project was imported)"}, 404)
    try:
        project = _json.loads(project_path.read_text(encoding="utf-8")) if project_path.is_file() else {}
    except (OSError, ValueError):
        return handler._json({"error": "project delivery record is unreadable"}, 500)
    source = str(project.get("source") or "").strip()
    if not source or not _Path(source).is_dir():
        return handler._json({"error": f"project source is unavailable: {source or '(none)'}"}, 400)
    patch = patch_path.read_text(encoding="utf-8")
    if not patch.strip():
        return handler._json({"error": "the patch is empty — nothing to apply"}, 400)

    def _apply(*args: str):
        return subprocess.run(
            ["git", "apply", "--whitespace=nowarn", *args, "-"],
            input=patch, cwd=source, capture_output=True, text=True, timeout=120,
        )

    check = _apply("--check")
    if check.returncode != 0:
        lines = (check.stderr or check.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else "patch does not apply"
        return handler._json({"error": f"patch does not apply cleanly: {detail[:300]}"}, 409)
    applied = _apply()
    if applied.returncode != 0:
        lines = (applied.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else "git apply failed"
        return handler._json({"error": f"git apply failed: {detail[:300]}"}, 500)
    receipt = {
        "applied_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "source": source,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "files": project.get("filesChanged") or [],
    }
    (run_dir / "apply.json").write_text(
        _json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return handler._json({"applied": True, "source": source, "receipt": "apply.json",
                          "files": receipt["files"]})


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
    "/apply-patch": post_run_apply_patch,
    "/meta": post_run_meta,
    "/project": post_run_project,
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
    match = _suffix(path, "/api/gov/bots/", {
        "/run": post_gov_bot_run,
        "/settings": post_gov_bot_settings,
    })
    if match is not None:
        bot_id, route = match
        return route(handler, bot_id)
    match = _suffix(path, "/api/gov/runs/", {
        "/approve": post_gov_run_approve,
        "/reject": post_gov_run_reject,
    })
    if match is not None:
        run_id, route = match
        return route(handler, run_id)
    match = _suffix(path, "/api/gov/routines/", {
        "/toggle": post_gov_routine_toggle,
        "/delete": post_gov_routine_delete,
    })
    if match is not None:
        routine_id, route = match
        return route(handler, routine_id)
    match = _suffix(path, "/api/gov/skills/", {"/delete": post_gov_skill_delete})
    if match is not None:
        skill_id, route = match
        return route(handler, skill_id)
    match = _suffix(path, "/api/chat/threads/", _CHAT_ROUTES)
    if match is not None:
        thread_id, route = match
        return route(handler, thread_id)
    match = _suffix(path, "/api/runs/", _RUN_ROUTES)
    if match is not None:
        run_id, route = match
        return route(handler, run_id)
    return handler._json({"error": "unknown endpoint"}, 404)
