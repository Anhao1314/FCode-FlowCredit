"""Run the real-model pelican evaluation through the Work capability path.

The output directory must be outside viewer/runs. Credentials come from the
host's saved provider configuration and are never included in the report.
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.capabilities.motion import build_motion_capability  # noqa: E402
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.demo.provider_factory import build_provider  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.evaluation.prompt_eval import provider_env  # noqa: E402
from flowagent.runtime.capability import HarnessCapabilityBridge  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from motion_frames import MotionProbe  # noqa: E402


def verify_html(text: str) -> list[str]:
    """Return capability-motion contract violations in an HTML artifact."""
    errors: list[str] = []
    for animation in ("wheel-spin", "pedal", "scenery-scroll"):
        if not re.search(rf"@keyframes\s+{re.escape(animation)}\b", text):
            errors.append(f"@keyframes {animation}")

    classes = [
        match.group(2).split()
        for match in re.finditer(r"\bclass\s*=\s*([\"'])(.*?)\1", text, re.DOTALL)
    ]
    class_names = {name for group in classes for name in group}
    for hook in ("wheel", "leg", "foot", "hip"):
        if hook not in class_names:
            errors.append(f"class .{hook}")
    if not class_names.intersection({"pedal", "deck", "support"}):
        errors.append("class .pedal, .deck, or .support")
    if not class_names.intersection({"scenery", "background"}):
        errors.append("class .scenery or .background")

    if not re.search(r"<svg\b[^>]*\bviewBox\s*=", text, re.IGNORECASE | re.DOTALL):
        errors.append("<svg> with viewBox")

    visible_text = re.sub(r"<[^>]*>", " ", text)
    visible_text += " " + " ".join(name for group in classes for name in group)
    for identity in ("pelican", "bicycle"):
        if not re.search(rf"\b{identity}\b", visible_text, re.IGNORECASE):
            errors.append(f"text or class {identity}")

    if re.search(r"<\s*script\b", text, re.IGNORECASE):
        errors.append("<script> is forbidden")
    if re.search(r"\brequestAnimationFrame\b", text, re.IGNORECASE):
        errors.append("requestAnimationFrame is forbidden")
    return errors


def _build_verify_script() -> str:
    """Embed the same verifier in the isolated workspace's verify.py."""
    return "\n".join((
        "from pathlib import Path",
        "import re",
        inspect.getsource(verify_html).rstrip(),
        "",
        'text = Path("index.html").read_text(encoding="utf-8")',
        "errors = verify_html(text)",
        "if errors:",
        '    raise SystemExit("invalid pelican artifact: " + ", ".join(errors))',
        'print("pelican artifact structure verified")',
        "",
    ))


VERIFY = _build_verify_script()


def run(
    output: Path,
    *,
    max_tokens: int = 32_768,
    reasoning_effort: str = "low",
    max_calls: int = 10,
    max_total_tokens: int = 300_000,
    max_cost_microusd: int = 200_000,
) -> dict:
    output = output.resolve()
    if ROOT / "viewer" / "runs" in output.parents:
        raise ValueError("evaluation output must not overwrite viewer/runs")
    output.mkdir(parents=True, exist_ok=True)
    scene = json.loads((ROOT / "config" / "scenes" / "pelican_bicycle.json").read_text())
    scenario = get_scenario("general-research")
    sandbox = WorkspaceSandbox(output / "workspace")
    sandbox.ensure()
    sandbox.write("verify.py", VERIFY)
    probe = MotionProbe(sandbox.root, output / "frames")
    spec = {
        "mode": "open",
        "fastProfile": "motion-render",
        "verification": {"command": "python3", "args": ["verify.py"]},
    }
    world = GENERAL_DOMAIN.create_world(spec=spec, sandbox=sandbox, motion_probe=probe)
    tools = ToolRegistry(GENERAL_DOMAIN.build_tools(world))
    store = TaskStore(output / "task.sqlite")
    receipts = ReceiptStore(store)
    env = provider_env(provider="deepseek")
    env["DEEPSEEK_MAX_TOKENS"] = str(max_tokens)
    env["DEEPSEEK_REASONING_EFFORT"] = reasoning_effort
    provider = build_provider(
        "deepseek", scenario=scenario, receipts=receipts, env=env,
    )
    limits = replace(
        scenario.limits,
        max_calls=max_calls,
        max_reads=24,
        max_cost_microusd=max_cost_microusd,
        max_tokens=max_total_tokens,
    )
    harness = Harness(
        store=store,
        registry=tools,
        provider=provider,
        world=world,
        limits=limits,
        receipts=receipts,
        verifier=lambda observation: GENERAL_DOMAIN.verify(world, observation),
        state_fn=GENERAL_DOMAIN.summarize_state,
        instruction=GENERAL_DOMAIN.instruction,
        prompt_profile="capability-motion",
        plan_extension=False,
        verification=GENERAL_DOMAIN.make_verification(world),
    )
    task = harness.create(
        task_id=f"pelican-{int(time.time())}",
        objective=(
            "Create a polished animated index.html of a pelican riding a bicycle. "
            "Both feet must remain on the pedals and both legs must stay attached "
            "to the pelican throughout the animation."
        ),
        principal="work-pelican-eval",
        binding={"scenario": scenario.key, "scene": scene["id"], "evaluation": True},
        allowed_tools=list(scenario.allowed_tools),
        grant=dict(scenario.grant),
    )
    verification_loop = Loop(harness, max_rounds=4, result_tools=("write_file",))
    bridge = HarnessCapabilityBridge(
        harness,
        task,
        loop=verification_loop,
        motion_probe=probe,
    )
    runtime = build_motion_capability(
        sandbox,
        bridge,
        probe=probe,
        verify=bridge.verify,
        executor=bridge.execute,
        child_run_id=f"cap-{task.id}",
    )
    bridge.bind(runtime)
    started = time.monotonic()
    error = None
    try:
        result = runtime.run(task.objective)
    except AgentError as exc:
        error = exc.to_dict()
        raise
    finally:
        elapsed = time.monotonic() - started
    task = bridge.finish()
    task = harness.record(
        task,
        "capability_succeeded" if result.succeeded else "capability_failed",
        result.to_dict(),
    )
    task = harness.finalize(
        task,
        "succeeded" if result.succeeded else "failed",
        failure=None if result.succeeded else {
            "code": result.reason or "CAPABILITY_FAILED",
            "message": f"capability stopped in {result.stage}: {result.reason}",
            "retryable": False,
        },
        event="task_succeeded" if result.succeeded else "task_failed",
        detail={"evaluation": "pelican-bicycle", "stage": result.stage},
    )
    events = store.events(task.id)
    tool_counts = Counter(
        event["payload"]["detail"].get("tool")
        for event in events
        if event["type"] == "tool_succeeded"
    )
    report = {
        "schema_version": "fcode.work-pelican-eval-v1",
        "mode": "online-real-model+real-playwright+independent-verifier",
        "provider": provider.name,
        "model": provider.model,
        "prompt_profile": "capability-motion",
        "completion": {"max_tokens": max_tokens, "reasoning_effort": reasoning_effort},
        "scene": scene["id"],
        "task_status": task.status,
        "elapsed_seconds": round(elapsed, 3),
        "result": result.to_dict(),
        "budget": harness.budget(task).snapshot(),
        "event_counts": dict(Counter(event["type"] for event in events)),
        "successful_tools": dict(tool_counts),
        "verification_events": [
            {"type": event["type"], "detail": event["payload"]["detail"]}
            for event in events
            if event["type"] in {"verification_obligation", "verification_result"}
        ],
        "artifact": {
            "path": str(sandbox.root / "index.html"),
            "bytes": (sandbox.root / "index.html").stat().st_size,
            "frames": str(output / "frames"),
        },
        "error": error,
        "disclosure": (
            "One online sample, not a statistically significant model comparison. "
            "Historical v5/v7 runs used different prompts and execution paths."
        ),
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    store.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=32_768)
    parser.add_argument("--max-calls", type=int, default=10)
    parser.add_argument("--max-total-tokens", type=int, default=300_000)
    parser.add_argument("--max-cost-microusd", type=int, default=200_000)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    args = parser.parse_args()
    report = run(
        args.output,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        max_calls=args.max_calls,
        max_total_tokens=args.max_total_tokens,
        max_cost_microusd=args.max_cost_microusd,
    )
    print(json.dumps({
        "task_status": report["task_status"],
        "stage": report["result"]["stage"],
        "failure": report["result"].get("reason") or None,
        "budget": report["budget"],
        "quality": (report["result"].get("quality") or {}).get("status"),
        "report": str(args.output.resolve() / "report.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if report["task_status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
