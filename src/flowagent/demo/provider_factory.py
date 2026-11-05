"""Host-owned provider selection for the demo CLI.

The default remains the offline mock. Every other provider is an explicit host
decision conveyed by ``--provider``; this factory only constructs the proposal
source. Gating, budgets, receipts, execution and independent verification stay
in the runtime and are unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ..core.receipts import ReceiptStore
from ..providers.base import Provider, Proposal
from ..providers.catalog import resolved_provider_env
from ..providers.deepseek_config import load_deepseek_settings
from ..providers.kimi import KimiProvider, load_kimi_settings
from ..providers.local import LocalProvider
from ..providers.mock import MockProvider
from ..providers.openai_compat import (
    GENERIC_COMPAT_DEFAULTS,
    OpenAICompatProvider,
    declared_compat_prefixes,
    load_compat_settings,
)
from ..providers.oracle import OracleFileProvider
from .fixtures import Scenario

__all__ = [
    "PROVIDER_NAMES",
    "FAULT_DISCLOSURE",
    "FaultInjectionProvider",
    "build_provider",
    "provider_start_line",
]

PROVIDER_NAMES = ("mock", "local", "kimi", "deepseek", "oracle")
FAULT_DISCLOSURE = (
    "scenario deliberately includes an unregistered tool in the plan; "
    "test target is host gating, not model spontaneity"
)
RawSink = Callable[[dict[str, Any]], None]


def provider_start_line(provider: Provider) -> str:
    return f"provider={provider.name}，所有外发调用由主机门控与审计"


class FaultInjectionProvider:
    """Publicly disclose that a scenario's plan deliberately contains an off-tool.

    This wrapper never edits the system prompt, parser or host gate. It changes
    only the model-visible plan for the ``tool-denied`` experiment, then marks
    every resulting proposal with an explicit disclosure. The test target is
    whether the host refuses an unregistered tool, not whether a model invents
    one spontaneously.
    """

    def __init__(self, wrapped: Provider) -> None:
        self.wrapped = wrapped
        self.name = wrapped.name
        self.model = wrapped.model

    def _context(self, context: dict[str, Any]) -> dict[str, Any]:
        plan = [dict(item) for item in context.get("plan") or []]
        injected = {
            "id": "t0",
            "text": "尝试用 write_document 写入研究底稿",
            "tool": "write_document",
            "arguments": {},
            "status": "pending",
        }
        index = next(
            (
                position
                for position, item in enumerate(plan)
                if item.get("status") in ("pending", "active")
            ),
            0,
        )
        plan[index] = injected
        return {**context, "plan": plan}

    def reserve(self, context: dict[str, Any]) -> tuple[int, int]:
        reserve = getattr(self.wrapped, "reserve", None)
        if callable(reserve):
            return reserve(self._context(context))
        return (
            int(getattr(self.wrapped, "reserve_tokens", 1_000)),
            int(getattr(self.wrapped, "reserve_cost_microusd", 500)),
        )

    def propose(self, context: dict[str, Any]) -> Proposal:
        proposal = self.wrapped.propose(self._context(context))
        metadata = dict(proposal.metadata)
        metadata["fault_injection"] = True
        metadata["disclosure"] = FAULT_DISCLOSURE
        metadata["injected_plan_todo"] = {
            "id": "t0",
            "tool": "write_document",
            "arguments": {},
        }
        return replace(proposal, metadata=metadata)


def build_provider(
    provider_name: str,
    *,
    scenario: Scenario,
    receipts: ReceiptStore,
    model: str | None = None,
    endpoint: str | None = None,
    timeout: float | None = None,
    json_mode: bool = False,
    structured: bool = False,
    reasoning_effort: str | None = None,
    calibration_path: Path | None = None,
    raw_sink: RawSink | None = None,
    env: Mapping[str, str] | None = None,
) -> Provider:
    """Construct one explicitly selected provider; never fall back to mock.

    ``env`` is the resolved configuration view (environment plus whatever the
    settings page saved); presets read their ``{PREFIX}_*`` names from it. It
    is never a way around validation: prices and their date are still required,
    and cloud endpoints still refuse a non-https base URL.
    """
    env = resolved_provider_env() if env is None else dict(env)
    if provider_name == "mock":
        return MockProvider(
            policy=scenario.policy,
            fail_at_call=scenario.fail_at_call,
        )
    if provider_name == "local":
        return LocalProvider(
            base_url=endpoint or "http://127.0.0.1:11434/v1",
            model=model or "fcode-qwen3-4b",
            timeout=timeout if timeout is not None else 900.0,
            json_mode=json_mode,
            structured=structured,
        )
    if provider_name == "oracle":
        exchange = Path(endpoint) if endpoint else Path("work/oracle-exchange")
        return OracleFileProvider(exchange, timeout=timeout or 900.0)

    def issue(**usage: Any) -> str:
        return receipts.issue(**usage)

    if provider_name == "kimi":
        settings = load_kimi_settings(
            env=env,
            model=model,
            base_url=endpoint,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
        )
        provider = KimiProvider.from_settings(
            settings,
            calibration_path=calibration_path,
            issue_receipt=issue,
            raw_sink=raw_sink,
        )
        return FaultInjectionProvider(provider) if scenario.key == "tool-denied" else provider
    if provider_name == "deepseek":
        settings = load_deepseek_settings(
            env=env,
            model=model,
            base_url=endpoint,
            timeout=timeout,
            max_tokens=256,
        )
        provider = settings.provider()
        return FaultInjectionProvider(provider) if scenario.key == "tool-denied" else provider

    declared = {prefix.lower(): prefix for prefix in declared_compat_prefixes(env=env)}
    if provider_name in declared:
        # A host-declared OpenAI-compatible endpoint (docs/openai-compat.md):
        # the same engine, transport discipline and receipts as the presets.
        prefix = declared[provider_name]
        settings = load_compat_settings(
            prefix,
            env=env,
            defaults=GENERIC_COMPAT_DEFAULTS,
            overrides={"reasoning_effort": reasoning_effort} if reasoning_effort else None,
        )
        calibration = calibration_path
        if calibration is None:
            path_env = env.get(f"{prefix}_CALIBRATION_PATH", "").strip()
            if path_env:
                calibration = Path(path_env)
        provider = OpenAICompatProvider.from_settings(
            settings,
            calibration_path=calibration,
            issue_receipt=issue,
            raw_sink=raw_sink,
        )
        return FaultInjectionProvider(provider) if scenario.key == "tool-denied" else provider

    known = sorted({*PROVIDER_NAMES, *declared})
    raise ValueError(f"unknown provider {provider_name!r}; known: {known}")
