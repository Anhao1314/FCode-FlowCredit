"""The analyst role: the first real model-in-the-loop step of the auditor.

Discipline of this slice (v1):

- ONE call, full-proposal mode, per audit run: the analyst receives the
  audit's outcome summary and returns a prioritized follow-up plan.
- The provider layer is the target's own — flowagent.providers — so
  pricing is host-reviewed (PriceSnapshot from env), pacing applies, and
  failures do not retry.
- The analyst proposes; the host interprets. Its plan steps are recorded
  verbatim as follow-up recommendations, never executed without the
  same gating any other action goes through.
- Wallet guard: authorize before dispatch (upper-bound reservation from
  the provider's own no-network estimate), record realized cost after.
"""

from __future__ import annotations

from pathlib import Path

from flowagent.core.contracts import AgentError
from flowagent.providers.deepseek_config import load_deepseek_settings

from . import online

PROVIDER = "deepseek"


def _env() -> dict:
    import os
    return dict(os.environ)


def followup_priorities(bundle: dict, *, env: dict | None = None,
                        policy_path: Path | None = None) -> dict:
    """Ask deepseek-flash for a prioritized follow-up plan; wallet-guarded."""
    settings = load_deepseek_settings(env=_env() if env is None else env)
    provider = settings.provider()
    _, estimated = settings.reservation("proposal")
    policy = online.authorize(PROVIDER, int(estimated), policy_path=policy_path)

    target = bundle.get("target") or {}
    findings = bundle.get("findings") or []
    uncovered = bundle.get("uncovered_surfaces") or []
    context = {
        "objective": (
            "你是审计跟进分析师。依据已完成的审计结果,给出下一步人工复核或"
            "自动化探针的优先级计划。只依据给定事实,不要编造未给出的信息。"
        ),
        "round": 1,
        "task": {
            "id": bundle.get("audit_id", "audit"),
            "objective": f"跟进审计:{target.get('identity', 'unknown')}",
        },
        "audit_result": {
            "findings": findings,
            "uncovered_surfaces": uncovered,
            "evidence_count": len(bundle.get("poc_evidence_refs") or []),
        },
    }
    captured: dict = {}
    provider.raw_sink = lambda record: captured.update(record)
    proposal = provider.propose(context)

    # 真实用量从 raw_sink 拿(每次调用必然回调一次);metadata 的 usage 只在
    # receipt_detail=full 时存在,不能当唯一来源。计费是事实,不许为 0。
    usage = captured.get("usage") or {}
    cost = provider.price.cost({
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    })
    total = online.record_spend(
        PROVIDER, int(cost), policy_path=policy_path,
        note=f"followup priorities for {bundle.get('audit_id', '')}")

    plan = proposal.plan or {}
    followups = [
        {
            "id": step.get("id"),
            "action": step.get("tool"),
            "detail": step.get("arguments") or {},
        }
        for step in (plan.get("steps") or [])
    ]
    return {
        "model": provider.model,
        "provider": PROVIDER,
        "cost_microusd": int(cost),
        "wallet_spent_microusd": total,
        "wallet_cap_microusd": (policy.get("providers") or {}).get(
            PROVIDER, {}).get("wallet_cap_microusd"),
        "plan_version": plan.get("version"),
        "followups": followups,
        "raw_tokens": {"prompt": usage.get("prompt_tokens", 0),
                       "completion": usage.get("completion_tokens", 0)},
    }
