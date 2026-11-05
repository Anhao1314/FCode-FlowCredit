"""Local-model provider: Qwen3 behind an OpenAI-compatible endpoint.

Works with Ollama (``/v1``), vLLM, llama.cpp server and MLX — anything that
speaks ``POST /chat/completions``. Zero third-party dependencies: the HTTP call
is stdlib ``urllib``.

Design boundaries, deliberately inherited from the runtime's philosophy:

* The model renders through :func:`flowagent.providers.prompt.render_prompt`,
  the same function that produced the SFT dataset. Training/inference drift is
  a contract violation, not a tuning issue.
* A reply that cannot be parsed is an **unknown outcome** — ``AgentError`` is
  raised and the harness records ``model_unknown``. It is never silently
  retried and never falls back to the mock.
* Cost is zero and honest: the receipt keeps ``external_billing``
  unverified-equivalent semantics via metadata; local inference is not billed.
* This module is a skeleton: the console does not wire it in yet. Enabling a
  local model is a host decision, gated like every other provider change.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..core.contracts import AgentError, PLAN_VERSION
from .base import Proposal
from .prompt import (
    build_proposal_plan,
    parse_proposal_text,
    prompt_profile_for,
    render_prompt,
)

__all__ = ["LocalProvider"]

# Wire-format schema for one proposal. Passing this to an endpoint that
# supports structured outputs prevents a missing quote from turning into a
# syntactically valid but semantically wrong argument.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"enum": ["flowagent.plan/v1"]},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["id", "tool", "arguments"],
            },
        },
        "todo_id": {"type": "string"},
    },
    "required": ["version", "steps"],
}


class LocalProvider:
    """Proposal source backed by a local OpenAI-compatible chat endpoint."""

    name = "local-qwen3"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
        model: str = "qwen3-4b-fcode",
        api_key: str = "local",
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 120.0,
        json_mode: bool = False,
        structured: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.json_mode = json_mode
        self.structured = structured

    # ------------------------------------------------------------------ call

    def _chat(self, system: str, user: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.structured:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "flowagent_plan", "schema": PLAN_SCHEMA},
            }
        elif self.json_mode:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # Transport failure is an unknown outcome, never a silent retry.
            raise AgentError("MODEL_CALL_FAILED", f"local endpoint: {error}") from error
        except ValueError as error:
            raise AgentError("MODEL_CALL_FAILED", f"endpoint returned non-JSON: {error}") from error

    # ------------------------------------------------------------- interface

    def propose(self, context: dict[str, Any]) -> Proposal:
        started = time.monotonic()
        profile = prompt_profile_for(context)
        system, user = render_prompt(context)
        payload = self._chat(system, user)
        latency_ms = int((time.monotonic() - started) * 1000)

        choices = payload.get("choices") or []
        if not choices:
            raise AgentError("MODEL_CALL_FAILED", "endpoint returned no choices")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        try:
            data = parse_proposal_text(content)
        except ValueError as error:
            raise AgentError("PROPOSAL_MALFORMED", f"{error}: {content[:120]!r}") from error

        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
        return Proposal(
            plan=build_proposal_plan(data),
            provider=self.name,
            model=self.model,
            tokens=tokens,
            cost_microusd=0,
            latency_ms=latency_ms,
            todo_id=data.get("todo_id"),
            metadata={"prompt_version": profile.version, "local": True, "billing": "not-billed"},
        )
