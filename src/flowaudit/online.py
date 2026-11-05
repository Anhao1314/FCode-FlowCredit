"""Online-call authorization and wallet guard for the audit domain.

The operator's authorization is a committed policy file
(config/online-policy.json) with hard wallet caps per provider. Every
online call must pass :func:`authorize` before dispatch and report its
realized cost through :func:`record_spend` after; the running ledger
lives beside the audit artifacts (work/audit/online-spend.json) and is
evidence like everything else.

Nothing here trusts the model: the guard bounds *spend*, not truth.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from flowagent.core.contracts import AgentError  # noqa: F401  (re-exported contract)


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "online-policy.json"
SPEND_FILENAME = "online-spend.json"


def load_policy(policy_path: Path | None = None) -> dict:
    path = Path(policy_path or DEFAULT_POLICY_PATH)
    if not path.exists():
        raise AgentError(
            "ONLINE_NOT_AUTHORIZED",
            f"no online policy at {path}; online calls stay disabled",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise AgentError("ONLINE_POLICY_INVALID", f"{path}: {error}") from error


def _spend_path(policy_path: Path | None) -> Path:
    path = Path(policy_path or DEFAULT_POLICY_PATH)
    return path.parent.parent / "work" / "audit" / SPEND_FILENAME


def _load_spend(spend_path: Path) -> dict:
    if spend_path.exists():
        try:
            return json.loads(spend_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"version": 1, "providers": {}}


def spent_microusd(provider: str, policy_path: Path | None = None) -> int:
    spend = _load_spend(_spend_path(policy_path))
    entry = spend["providers"].get(provider, {})
    return int(entry.get("spent_microusd", 0))


def authorize(provider: str, estimated_microusd: int,
              policy_path: Path | None = None) -> dict:
    """Raise unless one more estimated call fits inside the wallet cap.

    Returns the policy on success so the caller can cite the
    authorization it acted under.
    """
    policy = load_policy(policy_path)
    entry = (policy.get("providers") or {}).get(provider)
    if not entry or not entry.get("enabled"):
        raise AgentError(
            "ONLINE_NOT_AUTHORIZED",
            f"provider {provider!r} is not enabled in the online policy",
        )
    cap = int(entry.get("wallet_cap_microusd", 0))
    already = spent_microusd(provider, policy_path)
    if already + int(estimated_microusd) > cap:
        raise AgentError(
            "ONLINE_BUDGET_EXCEEDED",
            f"{provider}: spent {already} + estimated {estimated_microusd} "
            f"> wallet cap {cap} µUSD",
        )
    session_cap = int(policy.get("per_session_cap_microusd", 0))
    if session_cap and int(estimated_microusd) > session_cap:
        raise AgentError(
            "ONLINE_BUDGET_EXCEEDED",
            f"estimated {estimated_microusd} > per-session cap {session_cap} µUSD",
        )
    return policy


def record_spend(provider: str, microusd: int,
                 policy_path: Path | None = None,
                 note: str = "") -> int:
    """Persist realized cost and return the new cumulative total."""
    spend_path = _spend_path(policy_path)
    spend = _load_spend(spend_path)
    entry = spend["providers"].setdefault(provider, {"spent_microusd": 0, "calls": []})
    entry["spent_microusd"] = int(entry.get("spent_microusd", 0)) + int(microusd)
    entry.setdefault("calls", []).append({
        "microusd": int(microusd), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **({"note": note} if note else {}),
    })
    spend_path.parent.mkdir(parents=True, exist_ok=True)
    spend_path.write_text(json.dumps(spend, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry["spent_microusd"]
