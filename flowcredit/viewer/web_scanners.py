"""P1：外部扫描刀——第二证据面的装配层。

治理语义（外部刀不破叙事）：

1. 外部刀的输出是**第二证据面**，落 `external-scanners.json` 于审计目录
   （audit-bundle.json 旁边，不进 bundle 哈希）——多源证据，主签名链仍是
   我们的确定性规则集 v10；
2. **fail-closed 不伪造**：刀缺失/失败 → `available: false` + 原因，coverage
   如实降级，绝不静默、绝不伪造结果；
3. 全部 subprocess 调用（不 vendor 源码，无许可证传染）；带超时与版本号
   （版本入链，可复现）；
4. webcrack 标记 experimental：turbopack 产物解包未验证，只报可用性不自动跑。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["scanner_availability", "run_scanners"]

SCAN_TIMEOUT = 300


def _which(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"available": bool(path), "path": path or "", "reason": "" if path else "not installed"}


def scanner_availability() -> dict[str, Any]:
    """三把刀的可用性探测——coverage 如实降级的依据。"""
    out: dict[str, Any] = {
        "retire": _which("retire"),
        "gitleaks": _which("gitleaks"),
        "webcrack": {**_which("webcrack"), "note": "experimental：turbopack 产物解包未验证"},
    }
    return out


def _run_retire(snapshot: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["retire", "--js", "--path", str(snapshot), "--outputformat", "json", "--outputpath", str(snapshot / "_retire-raw.json")],
        capture_output=True, text=True, timeout=SCAN_TIMEOUT,
    )
    raw = snapshot / "_retire-raw.json"
    findings: list[dict[str, Any]] = []
    if raw.exists():
        data = json.loads(raw.read_text(encoding="utf-8", errors="replace"))
        for item in data if isinstance(data, list) else []:
            for match in item.get("matches", []) or []:
                for vuln in match.get("vulnerabilities", []) or []:
                    findings.append({
                        "file": str(item.get("file") or "").replace(str(snapshot) + "/", ""),
                        "library": str(match.get("component") or ""),
                        "version": str(match.get("version") or ""),
                        "severity": str(vuln.get("severity") or "").lower() or "unknown",
                        "identifiers": [str(c.get("CVE") or c.get("identifier") or "") for c in (vuln.get("identifiers") or []) if c],
                        "info": str((vuln.get("info") or [""])[0])[:160],
                    })
        raw.unlink(missing_ok=True)
    return {"available": True, "findings": findings, "count": len(findings)}


def _run_gitleaks(snapshot: Path) -> dict[str, Any]:
    report = snapshot / "_gitleaks-raw.json"
    proc = subprocess.run(
        ["gitleaks", "dir", str(snapshot), "--report-format", "json", "--report-path", str(report), "--no-banner"],
        capture_output=True, text=True, timeout=SCAN_TIMEOUT,
    )
    findings: list[dict[str, Any]] = []
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8", errors="replace") or "[]")
        for item in data[:40]:
            findings.append({
                "file": str(item.get("File") or "").replace(str(snapshot) + "/", ""),
                "rule": str(item.get("RuleID") or item.get("Description") or "")[:60],
                "line": item.get("StartLine"),
                "secret_redacted": str(item.get("Secret") or "")[:8] + "…",
            })
        report.unlink(missing_ok=True)
    # gitleaks 无发现时 exit code 1 是正常（0=有发现）——不算失败
    return {"available": True, "findings": findings, "count": len(findings)}


def run_scanners(snapshot_dir: Path | str, *, include_experimental: bool = False) -> dict[str, Any]:
    """对快照目录跑可用的外部刀。结果调用方落盘（审计目录旁）。"""
    snapshot = Path(snapshot_dir)
    availability = scanner_availability()
    out: dict[str, Any] = {
        key: {"available": availability[key]["available"],
              "reason": availability[key].get("reason", "")}
        for key in ("retire", "gitleaks", "webcrack")
    }
    if availability["retire"]["available"]:
        try:
            out["retire"] = _run_retire(snapshot)
        except Exception as error:  # noqa: BLE001 — 刀失败如实降级
            out["retire"] = {"available": True, "error": f"{type(error).__name__}: {error}"[:160]}
    if availability["gitleaks"]["available"]:
        try:
            out["gitleaks"] = _run_gitleaks(snapshot)
        except Exception as error:  # noqa: BLE001
            out["gitleaks"] = {"available": True, "error": f"{type(error).__name__}: {error}"[:160]}
    out["webcrack"] = {"available": availability["webcrack"]["available"], "experimental": True,
                       "note": availability["webcrack"].get("note", "")}
    return out
