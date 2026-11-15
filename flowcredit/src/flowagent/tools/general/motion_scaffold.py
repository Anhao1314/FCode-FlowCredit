"""Host-seeded scaffold for the motion-render profile.

A 10–15 KB single-file artifact is exactly the shape that races a provider's
single-response budget. The scaffold removes the need for the model to emit
the file in one response: the host seeds a minimal, verifier-compatible
``index.html`` with two ``SECTION`` anchors, and the tool gate then forbids
whole-file ``write_file`` on the seeded path — the model fills the anchors
with bounded ``edit_file`` calls instead.

The scaffold is generic: the same bytes seed every motion scene (pelican,
robot/scooter, abstract bounce). Scene-specific geometry lives in the README,
not in the host's seed.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MOTION_SCAFFOLD_PATH",
    "MOTION_SCAFFOLD_MARKERS",
    "MOTION_SCAFFOLD_HTML",
    "ensure_motion_scaffold",
]

#: The deliverable every motion-render run produces.
MOTION_SCAFFOLD_PATH = "index.html"

#: Anchor comments the model fills through edit_file. Both must survive in
#: the seeded file for the gate to consider the scaffold active; the quality
#: gate never counts them as implementation.
MOTION_SCAFFOLD_MARKERS = (
    "/* SECTION: styles */",
    "<!-- SECTION: scene -->",
)

#: Verifier-compatible minimal skeleton: ``<svg id="scene" viewBox=...>`` is
#: part of the public bench contract, and the head/style/body structure keeps
#: every later edit a bounded, local diff.
MOTION_SCAFFOLD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Motion</title>
  <style>
/* SECTION: styles */
  </style>
</head>
<body>
<svg id="scene" viewBox="0 0 900 520" xmlns="http://www.w3.org/2000/svg">
<!-- SECTION: scene -->
</svg>
</body>
</html>
"""


def ensure_motion_scaffold(sandbox: Any) -> tuple[str, ...]:
    """Seed ``index.html`` for a motion run; return the gated relative paths.

    Runs once at assembly, before the model acts. Three cases:

    * no ``index.html`` — write the scaffold;
    * a placeholder without the anchors and without any real implementation
      (no ``<svg`` and no ``@keyframes``) — replace it: the bench ships a
      bare skeleton the model cannot anchor-edit;
    * anything containing the anchors (already seeded) or looking like a real
      implementation — left untouched; a real artifact is never overwritten.
    """
    _, relative = sandbox.resolve(MOTION_SCAFFOLD_PATH)
    target, _ = sandbox.resolve(MOTION_SCAFFOLD_PATH)
    if target.is_file():
        content = str(sandbox.read(relative).get("content") or "")
        if all(marker in content for marker in MOTION_SCAFFOLD_MARKERS):
            return (relative,)
        if "<svg" in content or "@keyframes" in content:
            return ()
    sandbox.write(MOTION_SCAFFOLD_PATH, MOTION_SCAFFOLD_HTML)
    return (relative,)
