"""Chat protocol: version tag and the strict fcode_action parser.

Extracted from flowagent.chat so flowagent.chat_answer can reuse it
without forming an import cycle (chat imports chat_answer lazily).
"""

from __future__ import annotations

import json
import re
from typing import Any


CHAT_VERSION = "flowagent.chat/v1"


_ACTION_BLOCK = re.compile(r"```fcode_action\s*\n(.*?)\n```", re.DOTALL)

#: Governance chat uses a separate proposal block. It never executes an
#: audit directly: the host validates it and the operator confirms it.
_GOV_ACTION_BLOCK = re.compile(r"```gov_action\s*\n(.*?)\n```", re.DOTALL)
_GOV_ACTION_BY_BOT = {
    "auditor": "audit",
    "verifier": "verify",
    "analyst": "analyze",
}
_GOV_ACTION_MAX_BYTES = 2_000


ACTION_MODES = ("script", "research")


_ACTION_MAX_BYTES = 2_000


_ACTION_MAX_SOURCES = 5


def _read_action(payload_text: str) -> dict[str, Any] | None:
    """One candidate block to a proposal, or None if it is not one."""
    if len(payload_text.encode("utf-8")) > _ACTION_MAX_BYTES:
        return None
    try:
        payload = json.loads(payload_text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    goal = payload.get("goal")
    if not isinstance(goal, str) or not 5 <= len(goal.strip()) <= 600:
        return None
    mode = str(payload.get("mode") or "script").strip().lower()
    if mode not in ACTION_MODES:
        return None
    action: dict[str, Any] = {"goal": goal.strip(), "mode": mode}
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        action["reason"] = reason.strip()[:200]
    sources = payload.get("sources")
    if isinstance(sources, list):
        clean: list[str] = []
        for item in sources:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in clean:
                clean.append(text)
        if clean:
            action["sources"] = clean[:_ACTION_MAX_SOURCES]
    return action


def _read_gov_action(payload_text: str) -> dict[str, Any] | None:
    """Validate one governance proposal without executing it."""
    if len(payload_text.encode("utf-8")) > _GOV_ACTION_MAX_BYTES:
        return None
    try:
        payload = json.loads(payload_text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    bot_id = str(payload.get("bot_id") or "").strip().lower()
    if bot_id not in _GOV_ACTION_BY_BOT:
        return None
    action = str(payload.get("action") or _GOV_ACTION_BY_BOT[bot_id]).strip().lower()
    if action != _GOV_ACTION_BY_BOT[bot_id]:
        return None
    target = str(payload.get("target") or "").strip()
    if len(target) > 500:
        return None
    # An auditor needs a concrete target. Verifier/analyst may leave it empty;
    # the server resolves the latest completed audit at confirmation time.
    if bot_id == "auditor" and not target:
        return None
    item: dict[str, Any] = {
        "kind": "gov",
        "bot_id": bot_id,
        "action": action,
        "target": target,
    }
    for key in ("target_md", "osv_snapshot"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            item[key] = value.strip()[:500]
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        item["reason"] = reason.strip()[:200]
    return item


def parse_gov_action_block(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split one governance answer into (visible_text, gov_action | None)."""
    raw = str(text or "")
    action: dict[str, Any] | None = None
    found = False
    for candidate in _GOV_ACTION_BLOCK.finditer(raw):
        found = True
        valid = _read_gov_action(candidate.group(1).strip())
        if valid is not None:
            action = valid
    if not found or action is None:
        return raw, None
    return _GOV_ACTION_BLOCK.sub("", raw).strip(), action


def parse_action_block(
    text: str, *, block: str = "fcode_action"
) -> tuple[str, dict[str, Any] | None]:
    """Split one finished answer into (visible_text, action | None).

    The last block that reads as a whole proposal wins: a model may quote the
    format while explaining it, and a malformed tail (or an earlier draft) must
    not void a proposal it really made. Every candidate is validated by the
    same rules — invalid JSON, an out-of-range goal, an unknown mode or an
    oversized payload simply are not proposals. With no valid block the answer
    is returned exactly as written, a malformed one included: no silent repair.
    """
    if block == "gov_action":
        return parse_gov_action_block(text)
    raw = str(text or "")
    action: dict[str, Any] | None = None
    found = False
    for candidate in _ACTION_BLOCK.finditer(raw):
        found = True
        valid = _read_action(candidate.group(1).strip())
        if valid is not None:
            action = valid
    if not found:
        return raw, None
    if action is None:
        return raw, None
    # The machinery leaves the bubble: every block goes, whichever one won.
    visible = _ACTION_BLOCK.sub("", raw).strip()
    return visible, action
