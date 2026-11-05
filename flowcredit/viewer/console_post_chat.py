"""HTTP POST handlers for intake, chat configuration and chat branches."""

from __future__ import annotations

import json


def _chat(handler):
    chat = handler.manager.chat
    if chat is None:
        handler._json({"error": "chat is unavailable"}, 503)
    return chat


_INVALID_MODEL = object()


def _model_or_error(handler, chat, body):
    model = body.get("model") if isinstance(body.get("model"), str) else None
    try:
        chat.check_model(model)
    except Exception as error:  # noqa: BLE001 — exact config failures are client errors
        handler._json(
            {"error": str(error), "code": getattr(error, "code", "CHAT_PROVIDER_UNAVAILABLE")},
            400,
        )
        return _INVALID_MODEL
    return model


def post_intake(handler):
    mgr = handler.manager
    if mgr.runtime is None:
        return handler._json({"error": f"live mode unavailable: {mgr.runtime_error}"}, 503)
    body = handler._read_body()
    message = str(body.get("message") or "")[:2000]
    spec = body.get("spec") if isinstance(body.get("spec"), dict) else {}
    choice = body.get("choice") if isinstance(body.get("choice"), dict) else None
    history = body.get("history") if isinstance(body.get("history"), list) else []
    if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > 4096:
        return handler._json({"error": "spec is too large"}, 400)
    try:
        turn = mgr.runtime.clarify(
            message=message,
            history=history[-20:],
            spec=spec,
            choice=choice,
            template=str(body.get("template") or ""),
        )
    except Exception as error:  # noqa: BLE001
        return handler._json({"error": f"intake failed: {error}"}, 400)
    return handler._json(turn)


def post_chat_config(handler):
    chat = _chat(handler)
    if chat is None:
        return None
    body = handler._read_body()
    provider = body.get("provider")
    if provider is not None and not isinstance(provider, str):
        return handler._json({"error": "provider must be a string"}, 400)
    default_model = body.get("default_model")
    if default_model is not None and not isinstance(default_model, str):
        return handler._json({"error": "default_model must be a string"}, 400)
    values = body.get("values")
    if values is not None and not isinstance(values, dict):
        return handler._json({"error": "values must be an object"}, 400)
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 8192:
        return handler._json({"error": "config payload is too large"}, 400)
    try:
        snapshot = chat.update_config(provider=provider, values=values, default_model=default_model)
    except Exception as error:  # noqa: BLE001 — validation errors are 400s
        return handler._json(
            {"error": str(error), "code": getattr(error, "code", "CHAT_CONFIG_INVALID")}, 400,
        )
    return handler._json(snapshot)


def post_chat_send(handler):
    chat = _chat(handler)
    if chat is None:
        return None
    body = handler._read_body()
    text = body.get("text")
    if not isinstance(text, str):
        return handler._json({"error": "text must be a string"}, 400)
    thread_id = body.get("threadId")
    if thread_id is not None and not isinstance(thread_id, str):
        return handler._json({"error": "threadId must be a string"}, 400)
    model = _model_or_error(handler, chat, body)
    if model is _INVALID_MODEL:
        return None
    approval = body.get("approval")
    if approval is not None and approval not in ("off", "consequential", "all"):
        return handler._json({"error": "approval must be off/consequential/all"}, 400)
    events = chat.send(
        thread_id=thread_id, text=text, attachments=body.get("attachments"), model=model,
        approval=approval,
    )
    return handler._stream_chat(events)


def post_chat_tool_decision(handler, thread_id: str):
    """写工具的逐调用审批：允许/拒绝（可记住本回合）。"""
    chat = _chat(handler)
    if chat is None:
        return None
    body = handler._read_body()
    approval_id = body.get("approvalId")
    if not isinstance(approval_id, str) or not approval_id:
        return handler._json({"error": "approvalId is required"}, 400)
    allow = bool(body.get("allow"))
    remember = bool(body.get("remember"))
    decision = chat.decide_tool(thread_id, approval_id, allow, remember)
    if decision is None:
        return handler._json({"error": "approval not found"}, 404)
    return handler._json({"ok": True, "decision": decision})



def post_chat_confirm(handler, thread_id: str):
    mgr = handler.manager
    chat = _chat(handler)
    if chat is None:
        return None
    if mgr.runtime is None:
        return handler._json({"error": f"live mode unavailable: {mgr.runtime_error}"}, 503)
    body = handler._read_body()
    message_id = str(body.get("messageId") or "").strip()
    if not message_id:
        return handler._json({"error": "messageId is required"}, 400)
    try:
        thread = chat.get_thread(thread_id)
    except Exception as error:  # noqa: BLE001 — no such thread is a 404, not a 500
        return handler._json({"error": str(error)}, 404)
    message = next((item for item in thread["messages"] if item["id"] == message_id), None)
    if message is None:
        return handler._json({"error": "no such message"}, 404)
    action = message.get("action") or {}
    if not action:
        return handler._json({"error": "this message carries no execution proposal"}, 400)
    if message.get("run"):
        return handler._json(
            {"error": "proposal already started", "runId": (message["run"] or {}).get("id")}, 409,
        )
    try:
        action = mgr.merge_chat_action(
            action,
            goal=body.get("goal") if isinstance(body.get("goal"), str) else None,
            mode=body.get("mode") if isinstance(body.get("mode"), str) else None,
            sources=body.get("sources") if isinstance(body.get("sources"), list) else None,
        )
    except ValueError as exc:
        return handler._json({"error": str(exc)}, 400)
    template = "general-research"
    try:
        scenario = mgr.runtime.get_scenario(template)
    except Exception:
        return handler._json({"error": f"unknown template {template!r}"}, 400)
    spec = {
        "version": "fcode.intake/v1",
        "task": template,
        "mode": str(action.get("mode") or "script"),
        "source": "chat-action",
        "origin_thread": {
            "threadId": thread_id,
            "threadTitle": thread.get("title", ""),
            "messages": [
                {"role": item["role"], "text": str(item["text"])[:600]}
                for item in thread["messages"]
                if item.get("role") in ("user", "assistant")
                and item.get("text") and item.get("status") != "error"
            ][-24:],
        },
    }
    if action.get("sources"):
        spec["sources"] = list(action["sources"])
    approval = str(body.get("approval") or "consequential")
    if approval not in ("off", "consequential", "all"):
        return handler._json({"error": "approval must be off, consequential or all"}, 400)
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
    objective = str(action["goal"]).strip()[:600]
    run = mgr.create(objective, template, approval, spec, model_key=model_key, effort=effort)
    try:
        chat.link_run(thread_id, message_id, run.id, goal=objective, mode=str(action["mode"]))
    except Exception as error:  # noqa: BLE001 — the run exists; report the anchor failure
        return handler._json(
            {"error": f"run {run.id} started but the thread anchor failed: {error}"}, 500,
        )
    return handler._json({"run": run.to_json(detail=True)}, 202)


def post_chat_edit(handler, thread_id: str):
    chat = _chat(handler)
    if chat is None:
        return None
    body = handler._read_body()
    text = body.get("text")
    message_id = body.get("messageId")
    if not isinstance(text, str) or not isinstance(message_id, str):
        return handler._json({"error": "text and messageId are required strings"}, 400)
    model = _model_or_error(handler, chat, body)
    if model is _INVALID_MODEL:
        return None
    events = chat.edit(
        thread_id=thread_id,
        message_id=message_id,
        text=text,
        attachments=body.get("attachments"),
        model=model,
    )
    return handler._stream_chat(events)


def post_chat_regenerate(handler, thread_id: str):
    chat = _chat(handler)
    if chat is None:
        return None
    body = handler._read_body()
    message_id = body.get("messageId")
    if not isinstance(message_id, str):
        return handler._json({"error": "messageId must be a string"}, 400)
    model = _model_or_error(handler, chat, body)
    if model is _INVALID_MODEL:
        return None
    events = chat.regenerate(thread_id=thread_id, message_id=message_id, model=model)
    return handler._stream_chat(events)
