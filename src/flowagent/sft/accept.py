"""One-command acceptance for the trained local policy.

Run after the GGUF is imported into Ollama. It checks that the endpoint/model
exist, runs the packet evaluation on the frozen scenarios, and prints a
machine-readable pass/fail report. A model that cannot choose a legal candidate
or cannot reproduce the known outcomes does not pass, regardless of loss.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .evaluate import evaluate

__all__ = ["accept", "checks", "main"]


def _models(endpoint: str, model: str, timeout: float = 5.0) -> dict[str, Any]:
    url = endpoint.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"available": False, "model_found": False, "models": [], "error": str(error)}
    names = [
        str(item.get("id") or item.get("name") or "")
        for item in payload.get("data") or payload.get("models") or []
        if isinstance(item, dict)
    ]

    def canonical(name: str) -> str:
        return name[:-7] if name.endswith(":latest") else name

    return {
        "available": True,
        "model_found": any(canonical(name) == canonical(model) for name in names),
        "models": names,
        "error": "",
    }


def checks(report: dict[str, Any], endpoint_check: dict[str, Any] | None = None) -> dict[str, Any]:
    totals = report.get("totals") or {}
    scenarios = report.get("scenarios") or []
    verified = all(item.get("verified", True) for item in scenarios)
    result = {
        "endpoint_available": True if endpoint_check is None else bool(endpoint_check.get("available")),
        "model_found": True if endpoint_check is None else bool(endpoint_check.get("model_found")),
        "no_errors": totals.get("errors", 1) == 0,
        "outcomes_ok": totals.get("outcomes_ok") == totals.get("scenarios"),
        "chain_verified": verified,
        "has_decisions": totals.get("policy_decisions", 0) > 0,
        "repaired": totals.get("repaired", 0),
        "warnings": [],
    }
    if result["repaired"]:
        result["warnings"].append(f"{result['repaired']} choice(s) required one repair")
    if not result["endpoint_available"]:
        result["warnings"].append("endpoint /models was unavailable; chat path may still work")
    if not result["model_found"] and result["endpoint_available"]:
        result["warnings"].append("model name is not listed by /models")
    result["passed"] = all(
        result[key] for key in ("no_errors", "outcomes_ok", "chain_verified", "has_decisions")
    )
    return result


def accept(
    *,
    workdir: Path,
    endpoint: str,
    model: str,
    scenarios: list[str],
    json_mode: bool = False,
    timeout: float = 120.0,
) -> dict[str, Any]:
    endpoint_check = _models(endpoint, model)
    report = evaluate(
        workdir=workdir,
        scenarios=scenarios,
        policy_name="local",
        endpoint=endpoint,
        model=model,
        json_mode=json_mode,
        timeout=timeout,
    )
    verdict = checks(report, endpoint_check)
    return {
        "model": model,
        "endpoint": endpoint,
        "endpoint_check": endpoint_check,
        "verdict": verdict,
        "evaluation": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-accept")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="fcode-qwen3-4b")
    parser.add_argument("--scenarios", default="full,open-ended")
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = accept(
        workdir=Path(args.workdir),
        endpoint=args.endpoint,
        model=args.model,
        scenarios=[item.strip() for item in args.scenarios.split(",") if item.strip()],
        json_mode=args.json_mode,
        timeout=args.timeout,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    verdict = result["verdict"]
    print(
        ("PASS" if verdict["passed"] else "FAIL")
        + f" · outcomes {result['evaluation']['totals']['outcomes_ok']}/"
        + f"{result['evaluation']['totals']['scenarios']}"
        + f" · repaired {verdict['repaired']}"
    )
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
