"""External scanner adapters: second evidence surface, never a verdict.

The ruleset is the first-party detector; tools such as retire.js, gitleaks and
Lighthouse are independent *signals*. Their output is pinned next to the bundle,
hashed, and recorded in coverage, but it never becomes a finding by itself:
promotion still requires a first-party POC and host oracle.

Fail-closed semantics:

* missing tool      -> ``skipped`` with a reason;
* tool crash/timeout -> ``error`` with a reason;
* successful run    -> ``executed`` plus a bounded, redacted summary and a
  SHA-256-pinned raw file.

No shell, no network by default. Lighthouse needs an explicitly supplied live
URL (``FCODE_LIGHTHOUSE_URL``); an offline snapshot is recorded as skipped, not
silently treated as a clean run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["EXTERNAL_TOOLS", "availability", "run_external_tools"]

EXTERNAL_TOOLS = ("retire", "gitleaks", "lighthouse")
MAX_FINDINGS = 40
TIMEOUT_SECONDS = 120


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _version(name: str, path: str) -> str:
    commands = {
        "retire": [path, "--version"],
        "gitleaks": [path, "version"],
        "lighthouse": [path, "--version"],
    }
    try:
        completed = subprocess.run(
            commands[name], capture_output=True, text=True, timeout=15,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    return text[0][:80] if text else ""


def availability() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in EXTERNAL_TOOLS:
        path = _which(name)
        out[name] = {
            "available": bool(path),
            "path": path,
            "version": _version(name, path) if path else "",
            "reason": "" if path else "not installed",
        }
    return out


def _raw_ref(out_dir: Path, tool: str, raw_path: Path) -> dict[str, str]:
    try:
        raw = raw_path.read_bytes()
    except OSError:
        return {}
    return {
        "tool": tool,
        "path": str(raw_path.relative_to(out_dir)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _run_retire(snapshot: Path, out_dir: Path) -> dict[str, Any]:
    raw_path = out_dir / "external-tools" / "retire.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            _which("retire"), "--js", "--path", str(snapshot),
            "--outputformat", "json", "--outputpath", str(raw_path),
        ],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode not in (0, 1):
        return {
            "status": "error",
            "reason": f"retire exited {completed.returncode}: "
                      f"{(completed.stderr or '').strip()[:160]}",
            "findings": [],
        }
    findings: list[dict[str, Any]] = []
    if raw_path.is_file():
        data = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
        for item in data if isinstance(data, list) else []:
            for match in item.get("matches", []) or []:
                for vuln in match.get("vulnerabilities", []) or []:
                    if len(findings) >= MAX_FINDINGS:
                        break
                    findings.append({
                        "file": str(item.get("file") or "").replace(str(snapshot) + "/", ""),
                        "library": str(match.get("component") or ""),
                        "version": str(match.get("version") or ""),
                        "severity": str(vuln.get("severity") or "").lower() or "unknown",
                        "identifiers": [
                            str(c.get("CVE") or c.get("identifier") or "")
                            for c in (vuln.get("identifiers") or []) if c
                        ][:8],
                        "info": str((vuln.get("info") or [""])[0])[:160],
                    })
    return {"status": "executed", "reason": "", "findings": findings}


def _run_gitleaks(snapshot: Path, out_dir: Path) -> dict[str, Any]:
    raw_path = out_dir / "external-tools" / "gitleaks.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            _which("gitleaks"), "dir", str(snapshot),
            "--report-format", "json", "--report-path", str(raw_path),
            "--redact", "--no-banner",
        ],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env={**os.environ, "NO_COLOR": "1"},
    )
    # gitleaks: 0 = clean, 1 = findings; anything else is a tool failure.
    if completed.returncode not in (0, 1):
        return {
            "status": "error",
            "reason": f"gitleaks exited {completed.returncode}: "
                      f"{(completed.stderr or '').strip()[:160]}",
            "findings": [],
        }
    findings: list[dict[str, Any]] = []
    if raw_path.is_file():
        data = json.loads(raw_path.read_text(encoding="utf-8", errors="replace") or "[]")
        for item in data[:MAX_FINDINGS]:
            findings.append({
                "file": str(item.get("File") or "").replace(str(snapshot) + "/", ""),
                "rule": str(item.get("RuleID") or item.get("Description") or "")[:60],
                "line": int(item.get("StartLine") or 0),
                "secret_redacted": str(item.get("Secret") or "")[:8] + "…",
            })
    return {"status": "executed", "reason": "", "findings": findings}


def _run_lighthouse(snapshot: Path, out_dir: Path) -> dict[str, Any]:
    url = os.environ.get("FCODE_LIGHTHOUSE_URL", "").strip()
    if not url:
        return {
            "status": "skipped",
            "reason": "offline snapshot has no served URL; set FCODE_LIGHTHOUSE_URL to opt in",
            "findings": [],
        }
    raw_path = out_dir / "external-tools" / "lighthouse.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            _which("lighthouse"), url, "--quiet",
            "--output=json", f"--output-path={raw_path}",
            "--chrome-flags=--headless=new --no-sandbox",
        ],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode != 0 or not raw_path.is_file():
        return {
            "status": "error",
            "reason": f"lighthouse exited {completed.returncode}: "
                      f"{(completed.stderr or '').strip()[:160]}",
            "findings": [],
        }
    data = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
    findings: list[dict[str, Any]] = []
    audits = data.get("audits") if isinstance(data, dict) else {}
    for audit_id, audit in (audits or {}).items():
        if not isinstance(audit, dict):
            continue
        score = audit.get("score")
        if score is None or score >= 1:
            continue
        if str(audit.get("scoreDisplayMode") or "") not in ("binary", "numeric", "metricSavings"):
            continue
        findings.append({
            "audit": str(audit_id)[:80],
            "title": str(audit.get("title") or "")[:160],
            "score": int(round(float(score) * 100)),
        })
        if len(findings) >= MAX_FINDINGS:
            break
    return {"status": "executed", "reason": "", "findings": findings}


def run_external_tools(snapshot_dir: Path | str, out_dir: Path | str, *,
                       input_sha256: str) -> dict[str, Any]:
    """Run available external tools; never raise, never promote findings."""
    snapshot = Path(snapshot_dir)
    out = Path(out_dir)
    available = availability()
    tools: list[dict[str, Any]] = []
    refs: list[dict[str, str]] = []
    for name in EXTERNAL_TOOLS:
        meta = available[name]
        if not meta["available"]:
            tools.append({
                "tool": name, "status": "skipped", "version": "",
                "findings_count": 0, "reason": meta["reason"],
            })
            continue
        try:
            if name == "retire":
                result = _run_retire(snapshot, out)
            elif name == "gitleaks":
                result = _run_gitleaks(snapshot, out)
            else:
                result = _run_lighthouse(snapshot, out)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            tools.append({
                "tool": name, "status": "error", "version": meta["version"],
                "findings_count": 0,
                "reason": f"{type(error).__name__}: {error}"[:200],
            })
            continue
        findings = result.get("findings") or []
        entry = {
            "tool": name,
            "status": result["status"],
            "version": meta["version"],
            "findings_count": len(findings),
            "findings": findings,
            "reason": result.get("reason", ""),
            "input_sha256": input_sha256,
        }
        raw_path = out / "external-tools" / f"{name}.json"
        ref = _raw_ref(out, name, raw_path)
        if ref:
            entry.update({"raw_path": ref["path"], "raw_sha256": ref["sha256"]})
            refs.append(ref)
        tools.append(entry)
    return {
        "schema_version": "fcode.external-tools/v1",
        "snapshot": str(snapshot),
        "input_sha256": input_sha256,
        "tools": tools,
        "refs": refs,
    }
