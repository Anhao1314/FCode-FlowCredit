"""Host-seeded scaffold for the motion-render profile.

Two layers:

* the **base scaffold** (v1) is a minimal, verifier-compatible ``index.html``
  with two ``SECTION`` anchors; the model fills them through bounded edits;
* the **rig scaffold** (v2) additionally seeds host-owned geometry: fixed
  wheels, hip anchor, pedal orbit, telescoping legs, scenery track, and the
  CSS keyframes that keep them in sync. The model only fills the rider/scene
  visuals around the rig.

The rig exists because "foot follows pedal" and "leg stays out of the wheel"
are geometry, not prose. Asking a model to re-derive a rig from a paragraph
was the R6 quality wall; the host can emit the correct transforms once and
freeze them. Text between ``HOST-OWNED START`` / ``HOST-OWNED END`` markers is
read-only for the model: :class:`ActionGate` rejects edits inside it.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "HOST_OWNED_END",
    "HOST_OWNED_START",
    "MOTION_RIG_SCAFFOLD_HTML",
    "MOTION_SCAFFOLD_HTML",
    "MOTION_SCAFFOLD_MARKERS",
    "MOTION_SCAFFOLD_PATH",
    "edit_touches_host_owned",
    "ensure_motion_scaffold",
    "host_owned_blocks",
    "strip_host_owned",
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

HOST_OWNED_START = "HOST-OWNED START"
HOST_OWNED_END = "HOST-OWNED END"

_SCAFFOLD_MARKER_V1 = "<!-- FCODE MOTION SCAFFOLD v1 -->"
_SCAFFOLD_MARKER_V2 = "<!-- FCODE MOTION SCAFFOLD v2 RIG -->"

#: Verifier-compatible minimal skeleton: ``<svg id="scene" viewBox=...>`` is
#: part of the public bench contract, and the head/style/body structure keeps
#: every later edit a bounded, local diff.
MOTION_SCAFFOLD_HTML = _SCAFFOLD_MARKER_V1 + """
<!doctype html>
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

# --------------------------------------------------------------------- rig math

_HIP = (450, 250)
_BOTTOM_BRACKET = (450, 390)
_PEDAL_RADIUS = 34
_WHEEL_REAR = (280, 380)
_WHEEL_FRONT = (620, 380)
_WHEEL_RADIUS = 72
_LEG_BASE_LENGTH = 100
_CYCLE_MS = 2000
_SCENERY_SHIFT = round(2 * math.pi * _WHEEL_RADIUS, 1)  # one wheel turn


def _leg_keyframes(steps: int = 24) -> str:
    """Keyframes that place a unit leg from hip to the orbiting pedal.

    A line from ``(450,250)`` to ``(450,350)`` is rotated by ``a`` and scaled
    by ``L/100`` around the hip. Both values are exact at each sample; CSS
    interpolates them linearly, which stays within a couple of pixels of the
    pedal arc.
    """
    lines = ["@keyframes pedal {"]
    for index in range(steps + 1):
        t = index / steps
        phi = 2 * math.pi * t
        dx = _PEDAL_RADIUS * math.sin(phi)
        dy = (_BOTTOM_BRACKET[1] - _HIP[1]) - _PEDAL_RADIUS * math.cos(phi)
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(-dx, dy))
        scale = length / _LEG_BASE_LENGTH
        lines.append(
            f"  {t * 100:.4f}% {{ transform: rotate({angle:.4f}deg) "
            f"scaleY({scale:.6f}); }}"
        )
    lines.append("}")
    return "\n".join(lines)


def _spokes(cx: int, cy: int, radius: int = 52, count: int = 12) -> str:
    lines = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        lines.append(
            f'      <line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}"/>'
        )
    return "\n".join(lines)


def _wheel(cx: int, cy: int, classes: str) -> str:
    return f"""    <g class="wheel {classes}">
      <circle cx="{cx}" cy="{cy}" r="{_WHEEL_RADIUS}" fill="none" stroke="#31404e" stroke-width="6"/>
      <circle cx="{cx}" cy="{cy}" r="{_WHEEL_RADIUS - 13}" fill="none" stroke="#9fb3c4" stroke-width="2"/>
      <g stroke="#8aa0b3" stroke-width="2.5">
{_spokes(cx, cy)}
      </g>
      <circle cx="{cx}" cy="{cy}" r="7" fill="#31404e"/>
    </g>"""


def _rig_styles() -> str:
    return f"""/* {HOST_OWNED_START}: host-owned rig geometry and keyframes */
#scene {{ background: #eef3f8; }}
.wheel {{ transform-box: view-box; animation: wheel-spin {_CYCLE_MS}ms linear infinite; }}
.wheel-rear {{ transform-origin: {_WHEEL_REAR[0]}px {_WHEEL_REAR[1]}px; }}
.wheel-front {{ transform-origin: {_WHEEL_FRONT[0]}px {_WHEEL_FRONT[1]}px; }}
.leg {{ stroke: #2f3a45; stroke-width: 10; stroke-linecap: round; fill: none;
        transform-box: view-box; transform-origin: {_HIP[0]}px {_HIP[1]}px;
        animation: pedal {_CYCLE_MS}ms linear infinite; }}
.leg-right {{ animation-delay: {-_CYCLE_MS // 2}ms; }}
.pedal {{ transform-box: view-box; transform-origin: {_BOTTOM_BRACKET[0]}px {_BOTTOM_BRACKET[1]}px;
          animation: pedal-orbit {_CYCLE_MS}ms linear infinite; }}
.pedal-right {{ animation-delay: {-_CYCLE_MS // 2}ms; }}
.scenery {{ animation: scenery-scroll {_CYCLE_MS}ms linear infinite; }}
@keyframes wheel-spin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
@keyframes pedal-orbit {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
@keyframes scenery-scroll {{ from {{ transform: translateX(0); }} to {{ transform: translateX(-{_SCENERY_SHIFT}px); }} }}
{_leg_keyframes()}
/* {HOST_OWNED_END} */"""


def _scenery_tiles() -> str:
    tile_width = _SCENERY_SHIFT
    tiles = []
    for index in range(4):
        x = round(index * tile_width, 1)
        tiles.append(f"""    <g transform="translate({x} 0)">
      <rect x="0" y="440" width="{tile_width}" height="80" fill="#cfe3c8"/>
      <path d="M0 440 Q{tile_width * 0.25:.1f} 360 {tile_width * 0.5:.1f} 440
               Q{tile_width * 0.75:.1f} 380 {tile_width:.1f} 440 Z" fill="#b7d3ae"/>
      <rect x="{tile_width * 0.2:.1f}" y="352" width="10" height="88" fill="#8fb98a"/>
      <circle cx="{tile_width * 0.2 + 5:.1f}" cy="344" r="26" fill="#91bb8a"/>
      <rect x="{tile_width * 0.68:.1f}" y="368" width="9" height="72" fill="#8fb98a"/>
      <circle cx="{tile_width * 0.68 + 4.5:.1f}" cy="360" r="22" fill="#9cc394"/>
    </g>""")
    return "\n".join(tiles)


def _rig_svg() -> str:
    rear = _wheel(_WHEEL_REAR[0], _WHEEL_REAR[1], "wheel-rear")
    front = _wheel(_WHEEL_FRONT[0], _WHEEL_FRONT[1], "wheel-front")
    hip_x, hip_y = _HIP
    bb_x, bb_y = _BOTTOM_BRACKET
    pedal_top = bb_y - _PEDAL_RADIUS
    return f"""<!-- {HOST_OWNED_START}: host-owned rig geometry -->
<g class="bicycle">
{rear}
{front}
  <g class="bicycle-frame" fill="none" stroke="#31404e" stroke-width="8" stroke-linecap="round">
    <path d="M{_WHEEL_REAR[0]} {_WHEEL_REAR[1]} L{bb_x} {bb_y} L410 300 Z"/>
    <path d="M{bb_x} {bb_y} L588 300 L{_WHEEL_FRONT[0]} {_WHEEL_FRONT[1]}"/>
    <path d="M410 300 L{bb_x} {bb_y}"/>
    <path d="M588 300 L610 268 L640 272"/>
  </g>
  <line class="leg leg-left" x1="{hip_x}" y1="{hip_y}" x2="{hip_x}" y2="{hip_y + _LEG_BASE_LENGTH}" vector-effect="non-scaling-stroke"/>
  <line class="leg leg-right" x1="{hip_x}" y1="{hip_y}" x2="{hip_x}" y2="{hip_y + _LEG_BASE_LENGTH}" vector-effect="non-scaling-stroke"/>
  <g class="pedal pedal-left">
    <rect x="{bb_x - 16}" y="{pedal_top - 6}" width="32" height="12" rx="3" fill="#31404e"/>
    <ellipse class="foot" cx="{bb_x}" cy="{pedal_top}" rx="11" ry="7" fill="#e8a54b"/>
  </g>
  <g class="pedal pedal-right">
    <rect x="{bb_x - 16}" y="{pedal_top - 6}" width="32" height="12" rx="3" fill="#31404e"/>
    <ellipse class="foot" cx="{bb_x}" cy="{pedal_top}" rx="11" ry="7" fill="#d98f3a"/>
  </g>
  <circle class="hip" cx="{hip_x}" cy="{hip_y}" r="9" fill="#31404e"/>
</g>
<!-- {HOST_OWNED_END} -->"""


MOTION_RIG_SCAFFOLD_HTML = _SCAFFOLD_MARKER_V2 + """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Motion</title>
  <style>
""" + _rig_styles() + """
/* SECTION: styles */
  </style>
</head>
<body>
<svg id="scene" viewBox="0 0 900 520" xmlns="http://www.w3.org/2000/svg">
<!-- HOST-OWNED START: seeded scenery -->
<rect class="sky" x="0" y="0" width="900" height="440" fill="#eef3f8"/>
<g class="scenery">
""" + _scenery_tiles() + """
</g>
<!-- HOST-OWNED END -->
""" + _rig_svg() + """
<g class="pelican">
<!-- HOST-OWNED START: composition placeholder -->
<circle cx="450" cy="190" r="24" fill="none"/>
<!-- HOST-OWNED END -->
<!-- SECTION: scene -->
</g>
</svg>
</body>
</html>
"""


def _host_owned_pattern() -> str:
    return (
        r"(?:/\*\s*" + HOST_OWNED_START + r".*?" + HOST_OWNED_END + r"\s*\*/"
        r"|<!--\s*" + HOST_OWNED_START + r".*?" + HOST_OWNED_END + r"\s*-->)"
    )


def host_owned_blocks(content: str) -> list[str]:
    import re

    return re.findall(_host_owned_pattern(), str(content or ""),
                      re.DOTALL | re.IGNORECASE)


def strip_host_owned(content: str) -> str:
    import re

    return re.sub(_host_owned_pattern(), "", str(content or ""),
                  flags=re.DOTALL | re.IGNORECASE)


def edit_touches_host_owned(content: str, old_text: str) -> bool:
    """Whether an edit's anchor or replacement intersects a host-owned block."""
    import re

    anchor = str(old_text or "")
    text = str(content or "")
    if not anchor:
        return False
    if HOST_OWNED_START in anchor or HOST_OWNED_END in anchor:
        return True
    spans = [
        match.span()
        for match in re.finditer(_host_owned_pattern(), text,
                                 re.DOTALL | re.IGNORECASE)
    ]
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            break
        end = index + len(anchor)
        if any(index < block_end and end > block_start
               for block_start, block_end in spans):
            return True
        start = index + 1
    return False


def ensure_motion_scaffold(sandbox: Any, *, rig: bool = False) -> tuple[str, ...]:
    """Seed ``index.html`` for a motion run; return the gated relative paths.

    Three cases:

    * no ``index.html`` — write the requested scaffold (base or rig);
    * a placeholder without anchors and without any real implementation
      (no ``<svg`` and no ``@keyframes``) — replace it;
    * an existing scaffold is upgraded to the rig only when the file is still
      scaffold-shaped (no ``@keyframes``); a real implementation is never
      overwritten, even if it lacks the rig.
    """
    relative = MOTION_SCAFFOLD_PATH
    target, _ = sandbox.resolve(MOTION_SCAFFOLD_PATH)
    template = MOTION_RIG_SCAFFOLD_HTML if rig else MOTION_SCAFFOLD_HTML
    if target.is_file():
        content = str(sandbox.read(relative).get("content") or "")
        if rig and _SCAFFOLD_MARKER_V2 in content:
            return (relative,)
        if (not rig
                and all(marker in content for marker in MOTION_SCAFFOLD_MARKERS)
                and _SCAFFOLD_MARKER_V2 not in content):
            return (relative,)
        if "@keyframes" in content:
            return ()
        if all(marker in content for marker in MOTION_SCAFFOLD_MARKERS):
            sandbox.write(MOTION_SCAFFOLD_PATH, template)
            return (relative,)
        if "<svg" in content:
            return ()
    sandbox.write(MOTION_SCAFFOLD_PATH, template)
    return (relative,)
