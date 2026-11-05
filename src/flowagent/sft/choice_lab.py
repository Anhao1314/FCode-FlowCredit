"""Build choice-SFT packets from the frozen full-proposal corpus.

The legacy corpus has one fatal gap for packet mode: no assistant turn ever
chooses ``report_blocked``. This lab reconstructs a DecisionPacket from each
saved context, relabels blocked states as an honest stop, and expands the
candidate menu with permutations and safe hard negatives.

The output is ShareGPT: system/user come from ``render_policy_prompt`` so the
training bytes match LocalPolicy at inference; assistant is one canonical JSON
object.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..providers.packet_prompt import POLICY_PROMPT_VERSION, render_policy_prompt
from ..providers.prompt import canonical
from ..runtime.policy import PACKET_VERSION

__all__ = [
    "LabCandidate",
    "build_packet",
    "expand_packet",
    "main",
]


@dataclass(frozen=True)
class LabCandidate:
    todo_id: str
    tool: str
    arguments: dict[str, Any]
    why: str
    effect: str = ""
    version: str = ""


@dataclass
class PacketSource:
    packet: dict[str, Any]
    menu: list[LabCandidate]
    label: LabCandidate
    blocked: bool
    source: str
    tool_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates_total: int = 0
    skip_reason: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)


def _tool_specs(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("name")): item
        for item in context.get("tools") or []
        if isinstance(item, dict) and item.get("name")
    }


def _candidate_from_step(
    step: dict[str, Any],
    specs: dict[str, dict[str, Any]],
    *,
    why: str = "",
) -> LabCandidate:
    tool = str(step.get("tool") or "")
    spec = specs.get(tool) or {}
    return LabCandidate(
        todo_id=str(step.get("id") or "stop"),
        tool=tool,
        arguments=dict(step.get("arguments") or {}),
        why=why or str(step.get("text") or tool),
        effect=str(spec.get("effect") or ""),
        version=str(spec.get("version") or ""),
    )


def _signature(candidate: LabCandidate) -> tuple[str, str, str]:
    return (candidate.tool, canonical(candidate.arguments), candidate.todo_id)


def _match(menu: Iterable[LabCandidate], step: dict[str, Any]) -> LabCandidate | None:
    step_id = str(step.get("id") or "")
    tool = str(step.get("tool") or "")
    arguments = dict(step.get("arguments") or {})
    for candidate in menu:
        if step_id and candidate.todo_id == step_id and candidate.tool == tool:
            return candidate
    for candidate in menu:
        if candidate.tool == tool and candidate.arguments == arguments:
            return candidate
    return None


def _trim(menu: list[LabCandidate], label: LabCandidate, limit: int = 5) -> list[LabCandidate]:
    """Keep the label and the first ``limit`` candidates without duplicates."""
    out: list[LabCandidate] = []
    seen: set[tuple[str, str, str]] = set()

    def push(candidate: LabCandidate) -> None:
        signature = _signature(candidate)
        if signature in seen:
            return
        seen.add(signature)
        out.append(candidate)

    push(label)
    for candidate in menu:
        if candidate is label:
            continue
        push(candidate)
        if len(out) >= limit:
            break
    return out[:limit]


def build_packet(
    context: dict[str, Any],
    step: dict[str, Any],
    *,
    source: str = "",
) -> PacketSource | None:
    """Rebuild one packet and its teacher label from a legacy context."""
    plan = [item for item in context.get("plan") or [] if isinstance(item, dict)]
    state = dict(context.get("state") or {})
    specs = _tool_specs(context)
    blocked = bool(state.get("blockers")) or any(item.get("status") == "blocked" for item in plan)
    open_steps = [item for item in plan if item.get("status") in ("pending", "active")]
    menu = [
        _candidate_from_step(item, specs)
        for item in open_steps
        if item.get("tool")
    ]

    if blocked:
        label = LabCandidate(
            todo_id="stop",
            tool="report_blocked",
            arguments={"reason": "insufficient_evidence"},
            why="已经出现阻塞；如实停止，不编造结论",
            effect="read",
        )
        menu.append(label)
    else:
        label = _match(menu, step)
        if label is None:
            label = _candidate_from_step(step, specs, why="执行当前步骤")
            menu.append(label)

    if not label.tool or not menu:
        return None
    if len(_trim(menu, label, 5)) < 2:
        return PacketSource(
            packet={},
            menu=[],
            label=label,
            blocked=blocked,
            source=source,
            tool_specs=specs,
            candidates_total=len(menu),
            skip_reason="single_candidate",
        )

    observations = [item for item in context.get("observations") or [] if isinstance(item, dict)]
    last = observations[-1] if observations else None
    last_evidence = None
    if last:
        last_evidence = {
            "round": last.get("round", 0),
            "tool": last.get("tool", ""),
            "arguments": dict(last.get("arguments") or {}),
            "summary": last.get("summary"),
        }

    packet = {
        "version": PACKET_VERSION,
        "objective": context.get("objective", ""),
        "round": int(context.get("round") or 1),
        "state": state,
        "last_evidence": last_evidence,
        "memory_cards": [],
        "budget": dict(context.get("budget") or {}),
        "candidates": [],
        "output": {"choice": "candidate id"},
    }
    trimmed = _trim(menu, label, 5)
    return PacketSource(
        packet=packet,
        menu=trimmed,
        label=label,
        blocked=blocked,
        source=source,
        tool_specs=specs,
        candidates_total=len(trimmed),
    )


def _repeat_negative(
    state: dict[str, Any],
    specs: dict[str, dict[str, Any]],
    label: LabCandidate,
) -> LabCandidate | None:
    reads = [str(item) for item in state.get("read_documents") or []]
    if reads:
        candidate = _candidate_from_step(
            {"id": "repeat", "tool": "read_document", "arguments": {"doc_id": reads[0]}},
            specs,
            why="重复读取已经读过的资料",
        )
        return None if _signature(candidate) == _signature(label) else candidate
    normalised = [str(item) for item in state.get("normalised_documents") or []]
    if normalised:
        candidate = _candidate_from_step(
            {"id": "repeat", "tool": "normalize_units", "arguments": {"doc_id": normalised[0]}},
            specs,
            why="重复归一已经归一过的资料",
        )
        return None if _signature(candidate) == _signature(label) else candidate
    return None


def _wrong_argument(
    label: LabCandidate,
    specs: dict[str, dict[str, Any]],
) -> LabCandidate | None:
    params = ((specs.get(label.tool) or {}).get("parameters") or {})
    doc = params.get("doc_id") if isinstance(params, dict) else None
    choices = [str(item) for item in (doc or {}).get("enum") or []] if isinstance(doc, dict) else []
    current = str(label.arguments.get("doc_id") or "")
    for candidate_doc in choices:
        if candidate_doc != current:
            return _candidate_from_step(
                {"id": "wrong", "tool": label.tool, "arguments": {"doc_id": candidate_doc}},
                specs,
                why="读取不符合当前步骤的资料",
            )
    return None


def _to_packet(packet: dict[str, Any], ordered: list[LabCandidate]) -> dict[str, Any]:
    built = copy.deepcopy(packet)
    built["candidates"] = [
        {
            "id": f"a{index}",
            "todo_id": candidate.todo_id,
            "tool": candidate.tool,
            "arguments": dict(candidate.arguments),
            "why": candidate.why,
            "effect": candidate.effect,
            "version": candidate.version,
        }
        for index, candidate in enumerate(ordered, start=1)
    ]
    return built


def expand_packet(
    source: PacketSource,
    *,
    variants: int = 3,
    seed: int = 7,
    position_offset: int = 0,
) -> list[dict[str, Any]]:
    """Return deduplicated ShareGPT records for one packet."""
    if source.skip_reason or not source.menu:
        return []
    rng = random.Random(f"{seed}:{source.source}")
    specs = source.tool_specs

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for variant in range(max(1, variants)):
        menu = list(source.menu)
        label = source.label
        negatives: list[str] = []

        def add_negative(candidate: LabCandidate, kind: str) -> None:
            if _signature(candidate) == _signature(label):
                return
            if any(_signature(candidate) == _signature(item) for item in menu):
                return
            while len(menu) >= 5:
                removed = False
                for index in range(len(menu) - 1, -1, -1):
                    if menu[index] is not label:
                        menu.pop(index)
                        removed = True
                        break
                if not removed:
                    break
            menu.append(candidate)
            negatives.append(kind)

        if not source.blocked and variant % 3 == 1:
            add_negative(LabCandidate(
                todo_id="stop",
                tool="report_blocked",
                arguments={"reason": "insufficient_evidence"},
                why="提前停止；当前证据并未宣称阻塞",
                effect="read",
            ), "premature_stop")
        if variant % 3 == 2:
            repeat = _repeat_negative(source.packet.get("state") or {}, specs, label)
            if repeat is not None:
                add_negative(repeat, "repeat")
            wrong = _wrong_argument(label, specs)
            if wrong is not None:
                add_negative(wrong, "wrong_argument")

        menu = _trim(menu, label, 5)
        others = [candidate for candidate in menu if candidate is not label]
        rng.shuffle(others)
        position = (position_offset + variant) % len(menu)
        ordered = others[:position] + [label] + others[position:]
        packet = _to_packet(source.packet, ordered)
        label_id = next(item["id"] for item, candidate in zip(packet["candidates"], ordered) if candidate is label)
        system, user = render_policy_prompt(packet)
        assistant = canonical({"choice": label_id})
        key = (user, assistant)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "conversations": [
                {"from": "system", "value": system},
                {"from": "user", "value": user},
                {"from": "assistant", "value": assistant},
            ],
            "source": source.source,
            "label_tool": label.tool,
            "position": ordered.index(label) + 1,
            "menu_size": len(ordered),
            "negatives": negatives,
            "blocked": source.blocked,
            "prompt_version": POLICY_PROMPT_VERSION,
        })
    return records


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _stats(records: list[dict[str, Any]], sources: list[PacketSource], skipped: Counter) -> dict[str, Any]:
    positions = Counter(record["position"] for record in records)
    tools = Counter(record["label_tool"] for record in records)
    menus = Counter(record["menu_size"] for record in records)
    negatives = Counter(
        kind for record in records for kind in record.get("negatives") or []
    )
    total_positions = sum(positions.values())
    entropy = 0.0
    if total_positions:
        import math
        for count in positions.values():
            probability = count / total_positions
            entropy -= probability * math.log2(probability)
    return {
        "sources": len(sources),
        "samples": len(records),
        "blocked_labels": sum(1 for record in records if record["blocked"]),
        "sources_blocked": sum(1 for source in sources if source.blocked),
        "label_tools": dict(sorted(tools.items())),
        "positions": dict(sorted(positions.items())),
        "position_entropy": round(entropy, 3),
        "menu_sizes": dict(sorted(menus.items())),
        "negatives": dict(sorted(negatives.items())),
        "skipped": dict(sorted(skipped.items())),
        "prompt_version": POLICY_PROMPT_VERSION,
    }


def _dedupe(
    train: list[dict[str, Any]],
    val: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """One record per user prompt; drop prompts whose labels disagree.

    The same packet can appear in many augmented rows. Keeping all of them
    overweights a handful of states, and putting copies on both sides of the
    split leaks. A conflicting label for identical bytes is not trainable, so
    the whole group is dropped and counted.
    """
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for split, items in (("train", train), ("val", val)):
        for record in items:
            groups.setdefault(record["conversations"][1]["value"], []).append((split, record))

    train_out: list[dict[str, Any]] = []
    val_out: list[dict[str, Any]] = []
    duplicates = 0
    conflicts = 0
    for items in groups.values():
        labels = {record["conversations"][2]["value"] for _, record in items}
        if len(labels) > 1:
            conflicts += 1
            continue
        duplicates += len(items) - 1
        split, record = next(
            ((split, record) for split, record in items if split == "train"),
            items[0],
        )
        (train_out if split == "train" else val_out).append(record)
    return train_out, val_out, {"duplicates_removed": duplicates, "conflicting_prompts": conflicts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-sft-choice-lab")
    parser.add_argument("--data", default="sft/data/train_full.jsonl")
    parser.add_argument("--train-out", default="sft/data/choices_lab_train.jsonl")
    parser.add_argument("--val-out", default="sft/data/choices_lab_val.jsonl")
    parser.add_argument("--stats-out", default="sft/data/choices_lab_stats.json")
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--val-every", type=int, default=7)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    rows = _load_rows(Path(args.data))
    sources: list[PacketSource] = []
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    skipped: Counter = Counter()

    for index, row in enumerate(rows):
        try:
            context = json.loads(row["conversations"][1]["value"])["context"]
            assistant = json.loads(row["conversations"][2]["value"])
            step = assistant["steps"][0]
        except (KeyError, IndexError, TypeError, ValueError):
            skipped["malformed_row"] += 1
            continue
        source = build_packet(context, step, source=f"row{index}")
        if source is None:
            skipped["unbuildable"] += 1
            continue
        if source.skip_reason:
            skipped[source.skip_reason] += 1
            continue
        records = expand_packet(
            source,
            variants=args.variants + (4 if source.blocked else 0),
            seed=args.seed,
            position_offset=index,
        )
        if not records:
            skipped["no_variants"] += 1
            continue
        source.variants = records
        sources.append(source)
        target = val if args.val_every > 1 and index % args.val_every == 0 else train
        target.extend(records)

    train, val, dedupe = _dedupe(train, val)
    for path, items in ((Path(args.train_out), train), (Path(args.val_out), val)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    stats = _stats(train + val, sources, skipped)
    stats.update(dedupe)
    stats["train"] = len(train)
    stats["val"] = len(val)
    Path(args.stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
