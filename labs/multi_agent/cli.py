"""CLI for the deterministic multi-agent lab."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from labs.multi_agent.fixtures import ABLATION_TASKS, get_task  # noqa: E402
from labs.multi_agent.orchestrator import LabRunResult, Orchestrator  # noqa: E402

__all__ = ["main"]


def _run(task_key: str, mode: str, root: Path) -> LabRunResult:
    run_id = f"cli-{task_key}-{mode}-{uuid.uuid4().hex[:8]}"
    return Orchestrator(root).run(get_task(task_key), mode=mode, run_id=run_id)


def _ablate(root: Path, repeat: int = 3) -> list[dict[str, Any]]:
    rows = []
    for key in ABLATION_TASKS:
        for mode in ("single", "multi"):
            runs = [_run(key, mode, root) for _ in range(max(1, repeat))]
            item = runs[0].to_dict()
            item["wall_ms"] = int(median(run.wall_ms for run in runs))
            item["runs"] = len(runs)
            rows.append(item)
    return rows


def _markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| task | mode | status | success | attempts | calls | reads | cost µUSD | tokens | wall ms | replans | rejects | dup | verified |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        budget = row.get("budget") or {}
        settled = budget.get("settled") or budget
        lines.append(
            f"| {row['task']} | {row['mode']} | {row['status']} | "
            f"{'yes' if row['success'] else 'no'} | {row['attempts']} | "
            f"{settled.get('calls', 0)} | {settled.get('reads', 0)} | "
            f"{settled.get('cost_microusd', 0)} | {settled.get('tokens', 0)} | "
            f"{row['wall_ms']} | {row['replans']} | {row['rejects']} | "
            f"{row['duplicate_count']} | {'yes' if row['verified'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fcode-multi-agent-lab")
    parser.add_argument("--task", default="full")
    parser.add_argument("--mode", choices=("single", "multi"), default="multi")
    parser.add_argument("--ablate", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent / ".runs"))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    if args.ablate:
        rows = _ablate(root, repeat=args.repeat)
        out = Path(args.out) if args.out else root / "ablation.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        markdown = _markdown(rows)
        md_path = out.with_suffix(".md")
        md_path.write_text(markdown, encoding="utf-8")
        print(markdown)
        print(json.dumps({"rows": len(rows), "out": str(out), "markdown": str(md_path)}))
        return 0

    result = _run(args.task, args.mode, root)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.success and result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
