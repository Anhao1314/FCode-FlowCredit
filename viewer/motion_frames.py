"""Deterministic frame capture for single-file HTML motion deliverables.

The model that writes these files has no eyes. This module gives the host a
reproducible pair of eyes that need no model call: load the page in headless
Chromium, pause every CSS animation, seek to fixed millisecond offsets, take a
screenshot, and measure the DOM geometry that "the foot is on the pedal" and
"the leg does not pass through the wheel" actually mean.

The frames are written beside the run (``quality-frames/``), never into the
workspace, so they cannot become deliverables or be edited by the model. The
binary data is not hashed into the run chain; ``project.json`` records only
frame names, times and SHA-256 values for human inspection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

__all__ = ["HOST_VERIFY_TIMES_MS", "MotionProbe"]

#: Four samples catch a leg at the top, middle and bottom of a pedal stroke
#: without turning a quality check into a video render.
DEFAULT_TIMES_MS = (0, 300, 600, 900)

#: The final host gate deliberately samples **different** offsets from the
#: in-loop tool. It is still the same renderer, but the model cannot tune its
#: fix to one exact set of frames and call it done.
HOST_VERIFY_TIMES_MS = (150, 450, 750, 1050)


class MotionProbe:
    """Run the Node/Playwright probe against one sandbox root."""

    def __init__(
        self,
        root: Path,
        out_dir: Path,
        *,
        node: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.root = Path(root).resolve()
        self.out_dir = Path(out_dir).resolve()
        self.node = node or shutil.which("node") or "node"
        self.timeout_seconds = timeout_seconds

    def probe(
        self,
        html: str = "index.html",
        times: Iterable[int] = DEFAULT_TIMES_MS,
    ) -> dict[str, Any]:
        """Capture frames and geometry; always returns a JSON-safe dict.

        A missing browser, missing Node or broken page is reported as
        ``ok: false`` rather than raising: the run's functional verdict has
        already been established and an unavailable quality probe must not
        turn a delivered artifact into a runtime error.
        """
        try:
            relative = Path(str(html)).as_posix()
            target = (self.root / relative).resolve()
            target.relative_to(self.root)
        except (OSError, ValueError):
            return {"ok": False, "error": f"html path is outside the workspace: {html}"}
        if not target.is_file():
            return {"ok": False, "error": f"html file not found: {html}"}

        script = Path(__file__).with_name("motion_frames.js")
        if not script.is_file():
            return {"ok": False, "error": "motion_frames.js is missing from the viewer"}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        modules = str(Path(__file__).resolve().parent / "node_modules")
        env["NODE_PATH"] = os.pathsep.join(
            part for part in (modules, env.get("NODE_PATH", "")) if part
        )
        command = [
            self.node,
            str(script),
            str(target),
            str(self.out_dir),
            json.dumps([int(value) for value in times]),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                cwd=str(Path(__file__).resolve().parent),
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"ok": False, "error": f"frame probe failed: {error}"}
        try:
            payload = json.loads(completed.stdout.strip() or "{}")
            if not isinstance(payload, dict):
                payload = {"ok": False, "error": "frame probe returned a non-object"}
        except ValueError:
            payload = {
                "ok": False,
                "error": (completed.stderr or completed.stdout or "frame probe returned no JSON")[-800:],
            }
        if not payload.get("ok"):
            payload["ok"] = False
            payload.setdefault(
                "error",
                (completed.stderr or "frame probe failed")[-800:],
            )
            return payload
        payload["contact"] = self._contact_sheet(payload.get("frames") or [])
        return payload

    def _contact_sheet(self, frames: list[dict[str, Any]]) -> str:
        """Compose sampled frames into one PNG for the delivery card.

        Pillow is already a FCode dependency for media work. If it is absent
        the probe still returns geometry; only the visual strip is skipped.
        """
        if not frames:
            return ""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return ""
        images = []
        labels = []
        for frame in frames:
            path = self.out_dir / str(frame.get("name") or "")
            try:
                images.append(Image.open(path).convert("RGB"))
                labels.append(f"{int(frame.get('time_ms') or 0)} ms")
            except (OSError, ValueError):
                continue
        if not images:
            return ""
        width = max(image.width for image in images)
        height = max(image.height for image in images)
        gap = 8
        label_height = 22
        columns = min(2, len(images))
        rows = (len(images) + columns - 1) // columns
        canvas = Image.new(
            "RGB",
            (columns * width + (columns - 1) * gap,
             rows * (height + label_height) + (rows - 1) * gap),
            (248, 246, 241),
        )
        draw = ImageDraw.Draw(canvas)
        for index, image in enumerate(images):
            column = index % columns
            row = index // columns
            x = column * (width + gap)
            y = row * (height + label_height + gap)
            canvas.paste(image, (x, y + label_height))
            draw.text((x + 4, y + 5), labels[index], fill=(40, 38, 34))
        target = self.out_dir / "contact.png"
        canvas.save(target, format="PNG", optimize=True)
        return target.name
