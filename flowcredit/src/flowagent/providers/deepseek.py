"""DeepSeek preset: the OpenAI-compatible engine pinned to DeepSeek's API.

DeepSeek follows FCode's external-call discipline — price snapshot from the
host, budget reserved before dispatch, transport failure as an unknown
outcome, no retry, no mock fallback — with the preset's own wire details: the
older ``max_tokens`` field name, ``temperature: 0`` always sent, JSON mode on
by default, and cache-split pricing (reported cache hits at the cheaper
price). The transport, reservation and receipts live in the shared engine;
this module pins the preset and keeps the choice-head (``DeepSeekPolicy``)
for Executive–Policy packets.
"""

from __future__ import annotations

import time  # noqa: F401 — patch anchor: tests intercept flowagent.providers.deepseek.time
import urllib.request  # noqa: F401 — patch anchor: tests intercept flowagent.providers.deepseek.urllib.request.urlopen
from dataclasses import dataclass
from typing import Any

from ..core.contracts import AgentError
from ..core.packets import ActionChoice, DecisionPacket
from .base import Proposal
from .openai_compat import (
    CompatClient,
    OpenAICompatProvider,
    PriceSnapshot as _EnginePriceSnapshot,
    _micro,
    _usage_tokens,
)
from .packet_prompt import parse_choice_text, render_policy_prompt
from .prompt import parse_proposal_text, render_prompt  # noqa: F401 — preset re-export surface

__all__ = ["PriceSnapshot", "DeepSeekProvider", "DeepSeekPolicy"]

_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class PriceSnapshot(_EnginePriceSnapshot):
    """Host-reviewed DeepSeek prices; reported cache hits bill at the
    ``cache_hit`` price when the host configures one."""

    billing: str = "cache-split"


class DeepSeekProvider(OpenAICompatProvider):
    """Full-proposal provider. The host still validates and verifies the plan."""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        price: PriceSnapshot,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_tokens: int = 256,
        max_prompt_tokens: int = 4_096,
        json_mode: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            price=price,
            model=model,
            base_url=base_url,
            label="deepseek",
            timeout=timeout,
            max_completion_tokens=max_tokens,
            reasoning_effort=None,
            temperature=None,
            min_call_interval_seconds=0.0,
            completion_field="max_tokens",
            temperature_mode="pin-zero",
            json_mode=json_mode,
            require_https=True,
            receipt_detail="compact",
            max_prompt_tokens=max_prompt_tokens,
        )
        # Historical attribute names the preset's callers rely on.
        self.max_tokens = max_tokens


class DeepSeekPolicy:
    """Choice-only policy head. One call; no repair, no hidden second charge."""

    name = "deepseek-policy"

    def __init__(
        self,
        *,
        api_key: str,
        price: PriceSnapshot,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_tokens: int = 64,
        max_packet_tokens: int = 4_096,
        json_mode: bool = True,
    ) -> None:
        self.client = CompatClient(
            label="deepseek",
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_completion_tokens=max_tokens,
            completion_field="max_tokens",
            temperature_mode="pin-zero",
            temperature=None,
            reasoning_effort=None,
            json_mode=json_mode,
            require_https=True,
        )
        self.model = model
        self.price = price
        self.max_tokens = max_tokens
        self.max_packet_tokens = max_packet_tokens
        self.reserve_tokens = max_packet_tokens + max_tokens
        self.reserve_cost_microusd = _micro(
            max_packet_tokens, price.input_microusd_per_mtok
        ) + _micro(max_tokens, price.output_microusd_per_mtok)

    def reserve(self, packet: DecisionPacket) -> tuple[int, int]:
        system, user = render_policy_prompt(packet)
        prompt_tokens = max(1, (len(system) + len(user) + 2) // 3) + 64
        return (
            prompt_tokens + self.max_tokens,
            _micro(prompt_tokens, self.price.input_microusd_per_mtok)
            + _micro(self.max_tokens, self.price.output_microusd_per_mtok),
        )

    def choose(self, packet: DecisionPacket) -> ActionChoice:
        system, user = render_policy_prompt(packet)
        started = time.monotonic()
        content, usage, request_id = self.client.chat(system, user)
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            choice = parse_choice_text(content)
        except ValueError as error:
            raise AgentError("POLICY_INVALID_CHOICE", f"{error}: {content[:120]!r}") from error
        if packet.candidate(choice) is None:
            raise AgentError("POLICY_INVALID_CHOICE", f"{choice!r} is not a candidate id")
        _, _, total_tokens = _usage_tokens(usage)
        metadata = {
            "billing": "external",
            "price_as_of": self.price.as_of,
            "price_source": self.price.source,
        }
        if not usage:
            metadata["usage_missing"] = True
        return ActionChoice(
            choice,
            tokens=total_tokens,
            latency_ms=latency_ms,
            cost_microusd=self.price.cost(usage),
            request_id=request_id,
            metadata=metadata,
        )
