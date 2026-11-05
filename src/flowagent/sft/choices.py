"""Choice-format SFT: DecisionPacket in, one candidate id out.

The legacy exporter trains the model to write a plan JSON from the full context.
This exporter trains the policy head on the much smaller packet contract:
``policy_requested`` carries the packet, ``policy_decided`` carries the label.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any

from ..providers.packet_prompt import POLICY_PROMPT_VERSION, render_policy_prompt
from ..providers.prompt import canonical

__all__ = ["samples_from_trace", "samples_to_sharegpt", "main"]


def samples_from_trace(trace: dict[str, Any], source_id: str) -> list[dict[str, Any]]:
    """Pair packets with choices from one trace. Auto actions have no label."""
    packets: dict[int, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    for event in trace.get("events") or []:
        if event.get("type") == "policy_requested":
            detail = event.get("payload", {}).get("detail") or {}
            round_no = int(detail.get("round") or 0)
            packet = detail.get("packet")
            if isinstance(packet, dict):
                packets[round_no] = packet
        elif event.get("type") == "policy_decided":
            detail = event.get("payload", {}).get("detail") or {}
            round_no = int(detail.get("round") or 0)
            choice = str(detail.get("candidate_id") or "")
            packet = packets.get(round_no)
            if not packet or not choice:
                continue
            candidate_ids = {
                item.get("id") for item in packet.get("candidates") or []
                if isinstance(item, dict)
            }
            if choice not in candidate_ids:
                continue
            samples.append({
                "packet": packet,
                "choice": choice,
                "source": f"{source_id}#r{round_no}",
            })
    return samples


def samples_to_sharegpt(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for sample in samples:
        system, user = render_policy_prompt(sample["packet"])
        signature = (user, sample["choice"])
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        assistant = canonical({"choice": sample["choice"]})
        records.append({
            "conversations": [
                {"from": "system", "value": system},
                {"from": "user", "value": user},
                {"from": "assistant", "value": assistant},
            ],
            "source": sample["source"],
        })
    return records, {
        "unique": len(records),
        "duplicates_skipped": duplicates,
        "prompt_version": POLICY_PROMPT_VERSION,
    }


def _load(paths: list[str]) -> list[tuple[str, dict[str, Any]]]:
    traces: list[tuple[str, dict[str, Any]]] = []
    for pattern in paths:
        for name in sorted(glob.glob(pattern)):
            try:
                trace = json.loads(Path(name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            trace_id = (trace.get("task") or {}).get("id") or Path(name).stem
            traces.append((str(trace_id), trace))
    return traces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-sft-choices")
    parser.add_argument("--out", required=True)
    parser.add_argument("--traces", nargs="*", default=[])
    parser.add_argument("--stats-out", default="")
    args = parser.parse_args(argv)

    samples: list[dict[str, Any]] = []
    for source_id, trace in _load(args.traces):
        samples.extend(samples_from_trace(trace, source_id))
    records, stats = samples_to_sharegpt(samples)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} choice samples -> {out} ({stats})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
