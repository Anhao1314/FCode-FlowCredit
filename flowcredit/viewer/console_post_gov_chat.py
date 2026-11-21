"""HTTP handlers for the normal Governance chat.

The model may converse and propose; only this host-side confirmation endpoint
turns a proposal into a team run. No chat message can execute an audit by itself.
"""

from __future__ import annotations

__all__ = ["post_gov_chat_send", "post_gov_chat_confirm"]

_INVALID_MODEL = object()


def _gov_chat(handler):
    chat = getattr(handler, "gov_chat", None)
    if chat is None:
        handler._json({"error": "governance chat is unavailable"}, 503)
    return chat


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


def post_gov_chat_send(handler):
    chat = _gov_chat(handler)
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
    events = chat.send(
        thread_id=thread_id,
        text=text,
        attachments=body.get("attachments"),
        model=model,
    )
    return handler._stream_chat(events)


def post_gov_chat_confirm(handler, thread_id: str):
    chat = _gov_chat(handler)
    if chat is None:
        return None
    bots = getattr(handler, "gov_bots", None)
    if bots is None:
        return handler._json({"error": "governance bots are unavailable"}, 503)
    body = handler._read_body()
    message_id = str(body.get("messageId") or "").strip()
    if not message_id:
        return handler._json({"error": "messageId is required"}, 400)
    try:
        thread = chat.get_thread(thread_id)
    except Exception as error:  # noqa: BLE001 — no such thread is a 404
        return handler._json({"error": str(error)}, 404)
    message = next((item for item in thread["messages"] if item["id"] == message_id), None)
    if message is None:
        return handler._json({"error": "no such message"}, 404)
    if message.get("run"):
        return handler._json(
            {"error": "proposal already started",
             "runId": (message["run"] or {}).get("id")},
            409,
        )
    action = message.get("action") or {}
    if action.get("kind") != "gov":
        return handler._json({"error": "this message carries no governance proposal"}, 400)
    bot_id = str(action.get("bot_id") or "")
    target = str(action.get("target") or "")
    try:
        run = bots.start_run(
            bot_id,
            target,
            trigger="manual",
            source="team",
            target_md=action.get("target_md"),
            osv_snapshot=action.get("osv_snapshot"),
        )
    except Exception as error:  # noqa: BLE001 — validation/budget failures are 400s
        return handler._json(
            {"error": str(error), "code": getattr(error, "code", "GOV_RUN_INVALID")},
            400,
        )
    goal = str(action.get("reason") or f"{bot_id} · {action.get('action')} · {target}")[:600]
    try:
        chat.link_run(
            thread_id, message_id, str(run.get("run_id") or ""),
            goal=goal, mode=str(action.get("action") or ""),
        )
    except Exception as error:  # noqa: BLE001 — the run exists; report anchor failure
        return handler._json(
            {"error": f"run {run.get('run_id')} started but the thread anchor failed: {error}"},
            500,
        )
    return handler._json({"run": run, "thread_id": thread_id}, 202)
