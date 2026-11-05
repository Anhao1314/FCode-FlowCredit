"""A worker whose only job is to die at the worst possible moment.

It runs the normal loop, but patches the store so that immediately *after* a
chosen event is committed the process SIGKILLs itself. That reproduces the case
the runtime is built for: work has been done and recorded, and the process is gone
before it can act on the result.

Nothing here is imported by the runtime. It exists only so the crash scenario can
be a real crash rather than a simulated exception.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..providers.mock import MockProvider
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.loop import Loop
from ..tools.finance.summaries import summarize_state as _finance_state
from ..tools.finance.fixtures import FinanceWorld
from ..tools.finance.planner import FinancePlanner
from ..tools.finance.tools import build_finance_tools
from ..tools.registry import ToolRegistry
from ..domains.finance import FINANCE_DOMAIN, verify_observation as verify_finance
from .fixtures import get_scenario
from .provider_factory import build_provider


def _optional_float(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def main() -> int:
    db = os.environ["FLOWAGENT_DB"]
    task_id = os.environ["FLOWAGENT_TASK_ID"]
    crash_at = os.environ.get("FLOWAGENT_CRASH_AT", "tool_succeeded")
    crash_count = int(os.environ.get("FLOWAGENT_CRASH_AT_COUNT", "1"))
    max_rounds = int(os.environ.get("FLOWAGENT_MAX_ROUNDS", "12"))
    provider_name = os.environ.get("FLOWAGENT_PROVIDER", "mock")
    model = os.environ.get("FLOWAGENT_PROVIDER_MODEL", "").strip() or None
    endpoint = os.environ.get("FLOWAGENT_PROVIDER_ENDPOINT", "").strip() or None
    reasoning_effort = os.environ.get("FLOWAGENT_PROVIDER_REASONING_EFFORT", "").strip() or None
    calibration = os.environ.get("FLOWAGENT_KIMI_CALIBRATION", "").strip()
    scenario = get_scenario(os.environ.get("FLOWAGENT_SCENARIO", "crash"))

    # Patch save(), not _write(): _write runs *inside* the transaction, so killing
    # there would roll the event back and the crash would test nothing. save()
    # returns only after COMMIT, which is the moment a real crash becomes
    # interesting — the work is durable and the process is gone.
    original_save = TaskStore.save
    seen = {"n": 0}

    def patched_save(self, task, expected_sequence, event_type, detail=None):  # type: ignore[no-untyped-def]
        result = original_save(self, task, expected_sequence, event_type, detail)
        if event_type == crash_at:
            seen["n"] += 1
            if seen["n"] == crash_count:
                sys.stdout.write(f"stoppedAt={event_type}#{seen['n']}\n")
                sys.stdout.flush()
                os.kill(os.getpid(), signal.SIGKILL)
        return result

    TaskStore.save = patched_save  # type: ignore[method-assign]

    store = TaskStore(Path(db))
    world = FinanceWorld(partial=scenario.partial_corpus)
    registry = ToolRegistry(build_finance_tools(world))
    receipts = ReceiptStore(store)
    if provider_name == "mock":
        provider = MockProvider()
    else:
        provider = build_provider(
            provider_name,
            scenario=scenario,
            receipts=receipts,
            model=model,
            endpoint=endpoint,
            timeout=_optional_float("FLOWAGENT_PROVIDER_TIMEOUT"),
            json_mode=bool(os.environ.get("FLOWAGENT_PROVIDER_JSON_MODE")),
            structured=bool(os.environ.get("FLOWAGENT_PROVIDER_STRUCTURED")),
            reasoning_effort=reasoning_effort,
            calibration_path=Path(calibration) if calibration else None,
        )
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=BudgetLimits(
            int(os.environ.get("FLOWAGENT_BUDGET_MAX_CALLS", "12")),
            int(os.environ.get("FLOWAGENT_BUDGET_MAX_READS", "20")),
            int(os.environ.get("FLOWAGENT_BUDGET_MAX_COST_MICROUSD", "20_000").replace("_", "")),
            int(os.environ.get("FLOWAGENT_BUDGET_MAX_TOKENS", "20_000").replace("_", "")),
        ),
        receipts=receipts,
        verifier=lambda observation: verify_finance(world, observation),
        state_fn=_finance_state,
    )
    Loop(
        harness,
        planner=FinancePlanner(),
        max_rounds=max_rounds,
        result_tools=FINANCE_DOMAIN.result_tools,
    ).run(task_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
