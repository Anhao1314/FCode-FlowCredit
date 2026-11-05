"""Finance research: the deterministic reference workload module.

The general module is the product's default entry; this one stays registered
because it gives the reliability scenarios a world whose answer can be
independently recomputed. The seven finance tools, the blueprint planner and
the margin recomputation all live here. The shared workspace tools (ingestion,
artifacts, connectors) are composed by the caller, not owned by this module.
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import AgentError
# The domain is a composition point: it may name the memory implementations
# that the capability layer only sees as ports.
from ..memory.cases import CaseIndex
from ..memory.facts import FactStore
from ..tools.base import Observation, ToolSpec
from ..tools.finance.fixtures import FinanceWorld
from ..tools.finance.planner import FinancePlanner
from ..tools.finance.summaries import summarize_state as finance_summarize_state
from ..tools.finance.tools import build_finance_tools
from .base import DomainModule

__all__ = ["FINANCE_DOMAIN", "verify_observation", "create_world", "build_tools", "make_planner"]

APPROVAL_TOOLS = ("reconcile_basis", "report_blocked", "working_paper_append")


def create_world(partial: bool = False, spec: dict | None = None, **kwargs: Any) -> FinanceWorld:
    # ``spec``/``sandbox`` are interface parity with the general module; the
    # finance world is fixture-built and delegation-spec independent.
    return FinanceWorld(partial=partial)


def build_tools(
    world: FinanceWorld,
    *,
    evidence: Any = None,
    artifacts: Any = None,
    connectors: Any = None,
    cases: Any = None,
    facts: Any = None,
) -> list[ToolSpec]:
    return build_finance_tools(
        world, evidence, artifacts, connectors,
        cases if cases is not None else CaseIndex(),
        facts if facts is not None else FactStore(),
    )


def make_planner(world: Any = None, **kwargs: Any) -> FinancePlanner:
    return FinancePlanner()


def verify_observation(world: FinanceWorld, observation: Observation) -> dict[str, Any]:
    """Recompute the answer from the world and compare it with the model's.

    The model never produced these numbers; it only asked for them. This
    function is what turns "the tool returned something" into "the runtime
    independently agrees", and it is the only path to ``succeeded``.
    """
    if observation.tool != "calculate_margin" or not observation.result.get("computed"):
        raise AgentError("RESULT_INCOMPLETE", "no computed margin to verify")
    recomputed = world.calculate_margin()
    if not recomputed.get("computed"):
        raise AgentError("RESULT_INCOMPLETE", "the world refuses to recompute the margin")
    for key in ("margin_bp_2024", "margin_bp_2025", "change_bp"):
        if recomputed[key] != observation.result[key]:
            raise AgentError(
                "RESULT_VERIFICATION_FAILED",
                f"{key} differs: recorded {observation.result[key]} vs recomputed {recomputed[key]}",
            )
    return {
        "verified": True,
        "verification": "runtime-recomputed",
        "result": recomputed,
    }


FINANCE_DOMAIN = DomainModule(
    key="finance-research",
    title="金融研究",
    create_world=create_world,
    build_tools=build_tools,
    make_planner=make_planner,
    verify=verify_observation,
    result_tools=("calculate_margin",),
    approval_tools=APPROVAL_TOOLS,
    report_template="research-note",
    summarize_state=finance_summarize_state,
)
