#!/usr/bin/env python3
"""Run an advisory Jev visual critique over one rendered motion frame.

The scene descriptor (config/scenes/*.json) supplies subject/vehicle labels,
which relations matter, and the crop. The core module contains no
pelican-specific logic.

This is an experiment, not a runtime gate. It sends one cropped PNG to
TypeSafe's System One endpoint, asks typed visual questions, and prints the
advisory verdict. It never changes a run's status or quality report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flowagent.decisions.jev_visual import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    build_request,
    classify_visual,
    env_credentials,
    load_scene,
    prepare_image,
    run_systemone,
)

DEFAULT_SCENE = ROOT / "config" / "scenes" / "pelican_bicycle.json"


def _find_frame(run_dir: Path, frame_ms: int) -> Path:
    frames = run_dir / "quality-frames"
    exact = frames / f"frame-{frame_ms:04d}.png"
    if exact.is_file():
        return exact
    candidates = sorted(frames.glob("frame-*.png"))
    if candidates:
        return candidates[0]
    raise SystemExit(f"no frame PNG found under {frames}")


def _resolve_scene(path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Advisory Jev visual critique")
    parser.add_argument("--image", help="explicit frame PNG")
    parser.add_argument("--run", help="run directory; picks quality-frames/frame-0450.png")
    parser.add_argument("--frame-ms", type=int, default=450)
    parser.add_argument("--scene", default=str(DEFAULT_SCENE),
                        help="scene descriptor JSON (default: config/scenes/pelican_bicycle.json)")
    parser.add_argument("--crop", nargs=4, type=int, metavar=("L", "T", "R", "B"),
                        default=None, help="override the scene crop")
    parser.add_argument("--scale", type=float, default=None,
                        help="override the scene scale")
    parser.add_argument("--env-file", default="~/.config/claimledger/jev.env")
    parser.add_argument("--model", default=None)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--no-boxes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", help="write the full JSON result here")
    args = parser.parse_args()

    if args.image:
        image_path = Path(args.image).expanduser()
    elif args.run:
        image_path = _find_frame(Path(args.run).expanduser(), args.frame_ms)
    else:
        raise SystemExit("pass --image or --run")

    scene_path = _resolve_scene(args.scene)
    if not scene_path.is_file():
        raise SystemExit(f"scene descriptor not found: {scene_path}")
    scene = load_scene(scene_path)
    crop = tuple(args.crop) if args.crop else scene.get("crop")
    if isinstance(crop, list):
        crop = tuple(crop)
    scale = args.scale if args.scale is not None else float(scene.get("scale") or 1.0)
    prepared = prepare_image(image_path, crop=crop, scale=scale)
    meta = {key: value for key, value in prepared.items() if key != "data_base64"}
    meta["image"] = str(image_path)
    model = args.model or DEFAULT_MODEL
    request = build_request(
        model, prepared, scene, include_boxes=not args.no_boxes,
    )

    if args.dry_run:
        preview = {
            "model": request["model"],
            "scene": scene.get("id"),
            "state": request["state"],
            "questions": request["questions"],
            "asset": {**meta, "base64_chars": len(prepared["data_base64"])},
            "note": "dry run: no network request, no API key read",
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    api_key, configured_model = env_credentials(args.env_file)
    if args.model is None:
        model = configured_model
        request["model"] = model
    response = run_systemone(
        request,
        api_key=api_key,
        endpoint=args.endpoint,
        timeout=args.timeout,
        danger_images=True,
    )
    answers = response.get("answers") or {}
    advisory = classify_visual(answers, scene)
    usage = response.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    result = {
        "model": response.get("model") or model,
        "scene": scene.get("id"),
        "image": meta,
        "usage": usage,
        # $0.042 per 1M input tokens = 42 µUSD per 1K tokens; output is free.
        "cost_microusd": (input_tokens * 42) // 1000,
        "advisory": advisory,
        "answers": answers,
        "note": "advisory only; not part of the host quality gate",
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
