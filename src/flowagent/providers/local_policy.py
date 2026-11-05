"""Local policy head: a small model chooses one candidate id.

The transport is the same OpenAI-compatible chat endpoint as LocalProvider, but
the contract is different: the model no longer generates tool calls. It reads a
compact DecisionPacket and returns ``{"choice": "a1"}``.

Invalid output gets exactly one repair attempt. That is a host policy, not a
silent retry loop: the second failure raises ``POLICY_INVALID_CHOICE`` and never
falls back to a made-up action.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..core.contracts import AgentError
from ..core.packets import ActionChoice, DecisionPacket
from .packet_prompt import render_policy_prompt

__all__ = ["LocalPolicy"]


class LocalPolicy:
    name = "local-qwen3-policy"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
        model: str = "qwen3-4b-fcode",
        api_key: str = "local",
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float = 120.0,
        json_mode: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.json_mode = json_mode

    # ------------------------------------------------------------------ call

    def _chat(self, messages: list[dict[str, str]]) -> tuple[str, int]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AgentError("MODEL_CALL_FAILED", f"local policy endpoint: {error}") from error
        except ValueError as error:
            raise AgentError("MODEL_CALL_FAILED", f"local policy returned non-JSON: {error}") from error
        choices = payload.get("choices") or []
        if not choices:
            raise AgentError("MODEL_CALL_FAILED", "local policy returned no choices")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
        return str(content), tokens

    # ------------------------------------------------------------- interface

    def choose(self, packet: DecisionPacket) -> ActionChoice:
        started = time.monotonic()
        system, user = render_policy_prompt(packet)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        content, tokens = self._chat(messages)
        total_tokens = tokens
        try:
            choice = self._validate(packet, content)
            return ActionChoice(
                choice,
                tokens=total_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except ValueError as error:
            repair = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": content[:500]},
                {"role": "user", "content": (
                    f"上一次输出不合法：{error}。只返回一个 JSON 对象 "
                    '{"choice": "<candidate id>"}，且 id 必须来自 candidates。'
                )},
            ]
            repaired_content, repaired_tokens = self._chat(repair)
            total_tokens += repaired_tokens
            try:
                choice = self._validate(packet, repaired_content)
            except ValueError as final_error:
                raise AgentError(
                    "POLICY_INVALID_CHOICE",
                    f"policy output invalid after one repair: {final_error}",
                ) from final_error
            return ActionChoice(
                choice,
                tokens=total_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                repaired=True,
            )

    @staticmethod
    def _validate(packet: DecisionPacket, content: str) -> str:
        from .packet_prompt import parse_choice_text

        choice = parse_choice_text(content)
        if packet.candidate(choice) is None:
            raise ValueError(f"{choice!r} is not a candidate id")
        return choice
