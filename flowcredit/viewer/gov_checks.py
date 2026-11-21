"""M2：治理计划步骤的宿主检查注册表（verifier-first 的类型化）。

不变量：**计划步骤的"完成"由宿主执行的声明检查判定，模型/bot 说完成不算。**

* 检查是确定性的、证据绑定的：只读 audit 服务（同一个 path jail），
  判定的是"产物是否存在/链是否完整/分析是否落库"这类宿主可观测事实，
  不评价模型输出的质量——质量裁决仍属操作员。
* 每个 step 必须声明 ≥1 个已知检查（propose 时校验，未知检查名直接拒绝）；
  未声明时按角色取默认集——但"零检查"不可表示。
* step 终态 = bot run 到达 done **且** 全部声明检查通过；任一检查失败 =
  step 失败，检查明细（name/ok/detail）进 result，可回放。
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["GOV_CHECKS", "GOV_CHECK_DEFAULTS", "run_gov_checks"]

GOV_CHECK_DEFAULTS = {
    "auditor": ("bundle.exists", "chain.intact"),
    "verifier": ("bundle.exists", "chain.intact"),
    "analyst": ("triage.recorded",),
}


def _audit_id_of(ctx: dict[str, Any]) -> str:
    return str((ctx.get("result") or {}).get("audit_id") or "")


def _check_bundle_exists(ctx: dict[str, Any]) -> tuple[bool, str]:
    audit_id = _audit_id_of(ctx)
    if not audit_id:
        return False, "step result has no audit_id"
    bundle = ctx["audit"].bundle(audit_id)
    if isinstance(bundle, tuple):
        payload, status = bundle
        return False, f"bundle unavailable ({status}): {payload}"
    return True, f"bundle {bundle.get('audit_id') or audit_id} loaded"


def _check_chain_intact(ctx: dict[str, Any]) -> tuple[bool, str]:
    audit_id = _audit_id_of(ctx)
    if not audit_id:
        return False, "step result has no audit_id"
    chain = ctx["audit"].chain(audit_id)
    if isinstance(chain, tuple):
        payload, status = chain
        return False, f"chain unavailable ({status}): {payload}"
    intact = bool(chain.get("chain_intact"))
    detail = (f"chain_intact={intact} events={chain.get('count') or len(chain.get('events') or [])}")
    return intact, detail


def _check_triage_recorded(ctx: dict[str, Any]) -> tuple[bool, str]:
    run = ctx.get("run") or {}
    # 两种真实形状：bot_runs 行是字符串 triage_json；GovBotService._row
    # 投影会 pop 掉它、给出解析后的 dict triage。检查按事实判定，两种都收。
    triage = run.get("triage_json") or run.get("triage") or ""
    if isinstance(triage, str):
        size = len(triage)
        unit = "bytes"
    elif isinstance(triage, (dict, list)):
        size = len(triage)
        unit = "entries"
    else:
        size, unit = 0, ""
    if not size:
        return False, "no triage output recorded on the bot run"
    return True, f"triage recorded ({size} {unit})"


#: name → (callable(ctx) -> (ok, detail), 人类描述)。ctx = {result, run, audit, bots}
GOV_CHECKS: dict[str, tuple[Callable[[dict[str, Any]], tuple[bool, str]], str]] = {
    "bundle.exists": (_check_bundle_exists, "step 产出的 audit_id 能加载 bundle"),
    "chain.intact": (_check_chain_intact, "audit 签名链完整"),
    "triage.recorded": (_check_triage_recorded, "分析产物已落 bot_runs"),
}


def run_gov_checks(
    names: list[str] | tuple[str, ...],
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """宿主执行声明检查。返回 [{name, ok, detail}]；顺序 = 声明顺序。"""
    results: list[dict[str, Any]] = []
    for name in names:
        entry = GOV_CHECKS.get(name)
        if entry is None:
            results.append({"name": name, "ok": False,
                            "detail": "unknown check (host registry)"})
            continue
        fn, _doc = entry
        try:
            ok, detail = fn(ctx)
        except Exception as error:  # noqa: BLE001 — 检查失败是事实，不是崩溃
            ok, detail = False, f"{type(error).__name__}: {error}"
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
    return results
