"""POST route table. Authentication and mutation guards stay in server.py."""

from __future__ import annotations

from console_post_chat import (
    post_intake_classify,
    post_chat_confirm,
    post_chat_tool_decision,
    post_chat_edit,
    post_chat_regenerate,
    post_chat_send,
    post_intake,
    post_chat_config,
    post_chat_config_test,
    post_chat_memory,
    post_chat_memory_delete,
)
from console_post_gov_chat import post_gov_chat_confirm, post_gov_chat_send
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
    post_run_recover,
    post_run_resume,
    post_run_review,
    post_run_steer,
    post_run_stop,
    post_schedules_upsert,
    post_schedules_delete,
    post_schedules_toggle,
)


def post_gov_run_explain(handler, run_id: str):
    """治理解释层：基于已完成运行的确定性审计包生成模型解读（非裁决）。

    fail-closed：gov/explainer 不可用、运行未完成、provider 不支持
    都直接拒绝——解释层缺位时，治理功能一个不少。"""
    explainer = getattr(handler, "gov_explainer", None)
    if explainer is None or getattr(handler, "gov_bots", None) is None:
        return handler._json({"error": "governance is unavailable"}, 503)
    body = handler._read_body()
    question = body.get("question")
    model = body.get("model")
    try:
        record = explainer.explain(
            run_id,
            question=str(question) if isinstance(question, str) else "",
            model_key=model if isinstance(model, str) else None,
        )
    except ValueError as error:
        message = str(error)
        code = 503 if "provider" in message or "模型不支持" in message else 400
        return handler._json({"error": message}, code)
    return handler._json(record)


def post_intel_osv_sync(handler):
    """情报知识库 v0：操作员显式同步一个审计包的 Go 依赖情报。

    审计保持离线（network: none）；同步是独立的治理动作：读包内 REA facts
    的 Go 依赖 → 查 OSV → 带出处快照落盘 work/intel/osv/。之后审计与探员
    离线读快照（--osv-snapshot 路径不变）。"""
    audit = getattr(handler, "audit", None)
    if audit is None or audit.repo is None:
        return handler._json({"error": "no repo; intel sync unavailable"}, 503)
    body = handler._read_body()
    audit_dir = str(body.get("audit_dir") or "").strip()
    if not audit_dir:
        return handler._json({"error": "audit_dir is required"}, 400)
    bundle = audit.bundle(audit_dir)
    if isinstance(bundle, tuple):
        return handler._json({"error": str(bundle[0].get("error") or "not found")}, 404)
    facts_path = str(((bundle.get("rea_facts_ref") or {}).get("path") or "")).strip()
    if not facts_path:
        return handler._json(
            {"error": "bundle has no REA facts; nothing to collect dependencies from"}, 400)
    facts = audit.rea(audit_dir, facts_path.rsplit("/", 1)[-1])
    if isinstance(facts, tuple):
        return handler._json({"error": str(facts[0].get("error") or "facts unreadable")}, 404)
    from intel_osv import go_dependencies_from_facts, sync_osv_snapshot

    deps = go_dependencies_from_facts(facts)
    if not deps:
        return handler._json(
            {"error": "facts carry no Go dependencies"}, 400)
    out_path = audit.repo / "work" / "intel" / "osv" / f"osv-{audit_dir}.json"
    try:
        result = sync_osv_snapshot(deps, out_path=out_path)
    except Exception as error:  # noqa: BLE001 — 网络失败是操作员可见的 502
        return handler._json({"error": f"OSV sync failed: {error}"}, 502)
    return handler._json({
        "snapshot_path": result["path"],
        "queries": result["queries"],
        "hits": result["hits"],
        "fetched_at": (result["snapshot"] or {}).get("fetched_at"),
    })


def post_gov_audit_hunt(handler, audit_dir: str):
    """探员候选（shadow）：语义候选生成 → 落审计链 → 输出进治理对话。

    候选以 candidate_proposed 落 session 链（actor=model-shadow）——没有
    replay receipt，晋升双闸门天然拒绝；shadow 零晋升、不进 bundle。
    输出按「输出进对话框」纪律追加进治理线程。"""
    audit = getattr(handler, "audit", None)
    if audit is None or audit.root is None:
        return handler._json({"error": "no repo; hunt unavailable"}, 503)
    body = handler._read_body()
    thread_id = str(body.get("threadId") or "").strip()
    bundle = audit.bundle(audit_dir)
    if isinstance(bundle, tuple):
        return handler._json({"error": str(bundle[0].get("error") or "not found")}, 404)
    audit_id = str(bundle.get("audit_id") or audit_dir)
    facts_path = str(((bundle.get("rea_facts_ref") or {}).get("path") or "")).strip()
    if not facts_path:
        return handler._json({"error": "bundle has no REA facts"}, 400)
    facts = audit.rea(audit_dir, facts_path.rsplit("/", 1)[-1])
    if isinstance(facts, tuple):
        return handler._json({"error": "facts unreadable"}, 404)
    desktop = None
    for ref in bundle.get("rea_evidence_refs") or []:
        if str(ref.get("operation") or "") == "inspect_desktop_application":
            filename = str(ref.get("path") or "").rsplit("/", 1)[-1]
            loaded = audit.rea(audit_dir, filename)
            desktop = None if isinstance(loaded, tuple) else loaded
            break
    intel_path = audit.repo / "work" / "intel" / "osv" / f"osv-{audit_dir}.json"
    intel_snapshot = None
    if intel_path.exists():
        try:
            intel_snapshot = json.loads(intel_path.read_text(encoding="utf-8"))
        except ValueError:
            intel_snapshot = None
    from gov_hunter import (
        build_context,
        default_provider,
        hunt_candidates,
    )
    from intel_osv import go_dependencies_from_facts

    provider = default_provider()
    if provider is None:
        return handler._json(
            {"error": "探员不可用（provider 缺失或为 mock）", "code": "HUNTER_PROVIDER_UNAVAILABLE"},
            503,
        )
    context = build_context(
        facts=facts,
        go_deps=go_dependencies_from_facts(facts),
        intel_snapshot=intel_snapshot,
        desktop_evidence=desktop,
    )
    try:
        result = hunt_candidates(provider, context)
    except ValueError as error:
        return handler._json({"error": str(error)}, 502)
    except Exception as error:  # noqa: BLE001 — provider 失败按 502 汇报
        return handler._json({"error": f"hunt failed: {error}"}, 502)

    try:
        candidate_ids = audit.record_model_candidates(
            audit_dir, result["candidates"])
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    lines = [f"**结论**：探员产出 {len(candidate_ids)} 条候选（shadow 落链，未晋升、未进 bundle）。"]
    for index, item in enumerate(result["candidates"][:8], 1):
        lines.append(
            f"{index}. [{item['severity_guess'] or '?'}] {item['surface']} — "
            f"{item['hypothesis'][:120]}")
    if len(result["candidates"]) > 8:
        lines.append(f"…其余 {len(result['candidates']) - 8} 条见审计链。")
    meta = result["meta"] or {}
    chat = getattr(handler, "gov_chat", None)
    thread_result = None
    if chat is not None:
        try:
            thread_result = chat.post_explanation(
                thread_id,
                text="\n\n".join(lines),
                model=str(meta.get("model") or ""),
                tokens=int(meta.get("tokens") or 0),
                cost_microusd=int(meta.get("cost_microusd") or 0),
                title=f"探员候选 · {audit_dir}",
                finding_id="",
                audit_id=audit_dir,
                kind="hunt",
                candidate_count=len(candidate_ids),
            )
        except Exception as error:  # noqa: BLE001 — 候选已落链，线程失败不回滚
            return handler._json({
                "candidates": result["candidates"],
                "candidate_ids": candidate_ids,
                "meta": result["meta"],
                "thread_error": str(error),
            })
    return handler._json({
        "candidates": result["candidates"],
        "candidate_ids": candidate_ids,
        "meta": result["meta"],
        "thread_id": (thread_result or {}).get("thread_id"),
        "message_id": (thread_result or {}).get("message_id"),
    })


def post_gov_audit_explain(handler, audit_dir: str):
    """审计包视角的解读层：人话输出进治理对话线程。

    finding_id 缺省 = 包级摘要；带 finding_id = 该发现的一句话解读。
    生成走解释层（缓存按键替换、不进审计包、fail-closed）；带 threadId
    或允许新建线程时，宿主把解读追加进对话流——输出出现在对话框中，
    账本保持纯证据面。"""
    explainer = getattr(handler, "gov_explainer", None)
    if explainer is None or getattr(handler, "gov_bots", None) is None:
        return handler._json({"error": "governance is unavailable"}, 503)
    body = handler._read_body()
    finding_id = str(body.get("finding_id") or "").strip()
    model = body.get("model")
    thread_id = str(body.get("threadId") or "").strip()
    try:
        record = explainer.explain_audit(
            audit_dir,
            finding_id=finding_id,
            model_key=model if isinstance(model, str) else None,
        )
    except ValueError as error:
        message = str(error)
        code = 503 if "provider" in message or "模型不支持" in message else 400
        return handler._json({"error": message}, code)
    chat = getattr(handler, "gov_chat", None)
    if chat is None:
        return handler._json({"error": "governance chat is unavailable"}, 503)
    # 正文只带解读本身；上下文（finding/audit）进 explain 元数据，前端渲染
    # 成结构化卡片（徽章 + 目标 chip + 边界标注），对话流里不堆前缀文本。
    try:
        posted = chat.post_explanation(
            thread_id,
            text=str(record.get("text") or ""),
            model=str(record.get("model") or ""),
            tokens=int(record.get("tokens") or 0),
            cost_microusd=int(record.get("cost_microusd") or 0),
            title=f"AI 解读 · {audit_dir}",
            finding_id=finding_id,
            audit_id=audit_dir,
        )
    except Exception as error:  # noqa: BLE001 — 线程缺失/链错误按 4xx 处理
        return handler._json(
            {"error": str(error), "code": getattr(error, "code", "CHAT_THREAD_INVALID")},
            400,
        )
    return handler._json({
        "record": record,
        "thread_id": posted["thread_id"],
        "message_id": posted["message_id"],
    })


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


def post_gov_run_create(handler):
    """统一运行入口：对话 `/挖洞` 与团队派活都走这里。

    - 不带 bot_id / source=console：手动 run，source=console，不占预算、不走审批；
    - 带 bot_id：团队 run，source=team，按 Bot 预算与 routine 审批语义执行。
    目标校验在审计服务里同步完成，非法目标直接 400。
    """
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    target = str(body.get("target") or "").strip()
    bot_id = str(body.get("bot_id") or "").strip()
    source = str(body.get("source") or "").strip()
    target_md = str(body.get("target_md") or "").strip() or None
    osv_snapshot = str(body.get("osv_snapshot") or "").strip() or None
    try:
        if bot_id:
            if source == "console":
                raise ValueError("console runs are auditor-only and take no bot_id")
            run = service.start_run(bot_id, target, source="team",
                                    target_md=target_md, osv_snapshot=osv_snapshot)
        else:
            run = service.start_manual_run(target, target_md=target_md,
                                           osv_snapshot=osv_snapshot)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"run": run}, 201)


def post_gov_swarm_create(handler):
    """目标级 fan-out：一个蜂群把多个目标排入队列，宿主按并发调度。"""
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    targets = body.get("targets")
    if not isinstance(targets, list):
        return handler._json({"error": "targets must be an array"}, 400)
    try:
        swarm = service.create_swarm(
            targets,
            name=str(body.get("name") or ""),
            max_concurrency=body.get("max_concurrency", 2),
            target_md=str(body.get("target_md") or "").strip() or None,
            osv_snapshot=str(body.get("osv_snapshot") or "").strip() or None,
        )
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"swarm": swarm}, 201)


def post_gov_swarm_cancel(handler, swarm_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        swarm = service.cancel_swarm(swarm_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"swarm": swarm})


def post_gov_swarm_retry(handler, swarm_id):
    service = getattr(handler, "gov_bots", None)
    if service is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    try:
        swarm = service.retry_swarm(swarm_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"swarm": swarm})


def post_gov_fix_create(handler):
    """Fixer：在隔离副本验证补丁，不写原目录；批准后才应用并复扫。"""
    service = getattr(handler, "gov_fixes", None)
    if service is None:
        return handler._json({"error": "fixer is unavailable"}, 503)
    body = handler._read_body()
    audit_id = str(body.get("audit_id") or "").strip()
    finding_id = str(body.get("finding_id") or "").strip()
    patch = body.get("patch")
    verification = str(body.get("verification") or "").strip()
    model_key = str(body.get("model") or "").strip()
    if not audit_id or not finding_id:
        return handler._json({"error": "audit_id and finding_id are required"}, 400)
    try:
        if isinstance(patch, str) and patch.strip():
            fix = service.create(audit_id, finding_id, patch)
        elif verification:
            fix = service.delegate_fix(
                audit_id, finding_id, verification, model_key=model_key)
        else:
            return handler._json(
                {"error": "patch or verification command is required"}, 400)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    return handler._json({"fix": fix}, 201)


def post_gov_fix_approve(handler, fix_id):
    service = getattr(handler, "gov_fixes", None)
    if service is None:
        return handler._json({"error": "fixer is unavailable"}, 503)
    try:
        fix = service.approve(fix_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    except RuntimeError as error:
        return handler._json({"error": str(error)}, 500)
    return handler._json({"fix": fix})


def post_gov_fix_reject(handler, fix_id):
    service = getattr(handler, "gov_fixes", None)
    if service is None:
        return handler._json({"error": "fixer is unavailable"}, 503)
    try:
        fix = service.reject(fix_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"fix": fix})


def post_gov_plan_create(handler):
    """主代理规划：objective →（模型或手动 steps）→ proposed 计划。

    规划失败是 400 + 结构化错误（含规划器原因），不是 500：这是治理面的
    可见事实，不是服务器故障。
    """
    service = getattr(handler, "gov_plans", None)
    if service is None:
        return handler._json({"error": "governance plans are unavailable"}, 503)
    body = handler._read_body()
    objective = str(body.get("objective") or "").strip()
    if not objective:
        return handler._json({"error": "objective is required"}, 400)
    target = str(body.get("target") or "").strip()
    steps = body.get("steps")
    if steps is not None and not isinstance(steps, list):
        return handler._json({"error": "steps must be a list"}, 400)
    try:
        result = service.propose(objective, target=target, steps=steps)
    except ValueError as error:
        return handler._json({"error": str(error)}, 400)
    if not result.get("ok"):
        return handler._json(
            {"error": result.get("error") or "planner failed"}, 400)
    return handler._json({"plan": {k: v for k, v in result.items() if k != "ok"}},
                         201)


def post_gov_plan_approve(handler, plan_id):
    service = getattr(handler, "gov_plans", None)
    if service is None:
        return handler._json({"error": "governance plans are unavailable"}, 503)
    try:
        plan = service.approve(plan_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"plan": plan})


def post_gov_plan_reject(handler, plan_id):
    service = getattr(handler, "gov_plans", None)
    if service is None:
        return handler._json({"error": "governance plans are unavailable"}, 503)
    try:
        plan = service.reject(plan_id)
    except ValueError as error:
        return handler._json({"error": str(error)}, 409)
    return handler._json({"plan": plan})


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
    "/api/settings/model-config": post_chat_config,
    "/api/settings/chat-config": post_chat_config,
    "/api/settings/model-config/test": post_chat_config_test,
    "/api/chat/send": post_chat_send,
    "/api/gov/chat/send": post_gov_chat_send,
    "/api/intake/classify": post_intake_classify,
    "/api/chat/memory": post_chat_memory,
    "/api/schedules": post_schedules_upsert,
    "/api/schedules/delete": post_schedules_delete,
    "/api/schedules/toggle": post_schedules_toggle,
    "/api/chat/memory/delete": post_chat_memory_delete,
    "/api/runs": post_run_create,
    "/api/audit/run": post_audit_run,
    "/api/intel/osv/sync": post_intel_osv_sync,
    "/api/gov/runs": post_gov_run_create,
    "/api/gov/plans": post_gov_plan_create,
    "/api/gov/swarms": post_gov_swarm_create,
    "/api/gov/fixes": post_gov_fix_create,
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
    import os
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

    # 项目可能是父 git 仓库的子目录。`git apply` 在子目录里对 new-file
    # 补丁会静默 "Skipped patch"（rc=0 但什么都没写）——真模型 Work run 实测：
    # 422 字节的补丁被跳过，receipt 却写 applied。子目录项目必须从仓库根
    # 加 --directory 前缀应用；非 git 源目录仍按原样在 source 内应用。
    apply_cwd = source
    extra: tuple[str, ...] = ()
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source, capture_output=True, text=True, timeout=30,
    )
    if probe.returncode == 0 and (probe.stdout or "").strip():
        toplevel = _Path(probe.stdout.strip()).resolve()
        if toplevel != _Path(source).resolve():
            rel = os.path.relpath(_Path(source).resolve(), toplevel)
            apply_cwd = str(toplevel)
            extra = (f"--directory={rel}",)

    def _apply(*args: str):
        return subprocess.run(
            ["git", "apply", "--whitespace=nowarn", *extra, *args, "-"],
            input=patch, cwd=apply_cwd, capture_output=True, text=True, timeout=120,
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
    # rc=0 不证明落地：路径解析不到时 git 只打印 "Skipped patch"。
    if "Skipped patch" in (applied.stdout or ""):
        return handler._json(
            {"error": "git apply skipped the patch (paths did not resolve); nothing was written"},
            500,
        )
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
    "/recover": post_run_recover,
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
    match = _suffix(path, "/api/gov/chat/threads/", {
        "/confirm": post_gov_chat_confirm,
    })
    if match is not None:
        thread_id, route = match
        return route(handler, thread_id)
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
        "/explain": post_gov_run_explain,
    })
    if match is not None:
        run_id, route = match
        return route(handler, run_id)
    match = _suffix(path, "/api/gov/plans/", {
        "/approve": post_gov_plan_approve,
        "/reject": post_gov_plan_reject,
    })
    if match is not None:
        plan_id, route = match
        return route(handler, plan_id)
    match = _suffix(path, "/api/gov/audit/", {
        "/explain": post_gov_audit_explain,
        "/hunt": post_gov_audit_hunt,
    })
    if match is not None:
        audit_dir, route = match
        return route(handler, audit_dir)
    match = _suffix(path, "/api/gov/swarms/", {
        "/cancel": post_gov_swarm_cancel,
        "/retry": post_gov_swarm_retry,
    })
    if match is not None:
        swarm_id, route = match
        return route(handler, swarm_id)
    match = _suffix(path, "/api/gov/fixes/", {
        "/approve": post_gov_fix_approve,
        "/reject": post_gov_fix_reject,
    })
    if match is not None:
        fix_id, route = match
        return route(handler, fix_id)
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
