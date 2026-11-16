"""Deterministic motion-quality checks for single-file HTML animations.

This is deliberately text-only: it can tell a run that its wheel and
background disagree about speed, or that its crank and legs run at different
periods, without needing a browser. The visual critique is a separate layer;
these numbers are the part a host can always reproduce.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "FOOT_PEDAL_TOLERANCE_PX",
    "FOOT_PEDAL_WARN_PX",
    "HIP_ANCHOR_TOLERANCE_PX",
    "LEG_ENDPOINT_TOLERANCE_PX",
    "LEG_LENGTH_MIN_PX",
    "analyze_motion",
    "merge_quality",
    "probe_quality",
]

_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)

#: A foot centre further than this from the nearest pedal centre is visibly off
#: the pedal at this render size. 25 px is roughly a third of a foot at the
#: 1000x600 reference viewport; 12 px is the warning threshold.
FOOT_PEDAL_TOLERANCE_PX = 25
FOOT_PEDAL_WARN_PX = 12

#: A leg must start near a hip marker and be long enough to read as a limb, not
#: a crank spoke. These are semantic constraints: the foot/pedal distance alone
#: is satisfiable by rotating the foot around the bottom bracket.
HIP_ANCHOR_TOLERANCE_PX = 30
LEG_LENGTH_MIN_PX = 60
LEG_ENDPOINT_TOLERANCE_PX = 25

_GEOMETRY_CHECKS = (
    "frame_capture",
    "animation_contract",
    "composition",
    "foot_pedal",
    "leg_presence",
    "hip_anchor",
    "leg_length",
    "leg_endpoint",
)

#: A wheel/part may touch the scene edge by this many pixels; beyond that it is
#: visibly clipped or off-canvas.
COMPOSITION_MARGIN_PX = 2


def _css(html: str) -> str:
    return "\n".join(_STYLE.findall(html))


def _rule(css: str, selector: str) -> str:
    # Prefer an exact selector rule; `.wheel--rear { ... }` must not shadow
    # the `.wheel { animation: ... }` rule just because it appears earlier.
    exact = re.search(
        r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}",
        css, re.DOTALL,
    )
    if exact:
        return exact.group(1)
    pattern = re.compile(
        r"([^{}]*" + re.escape(selector) + r"[^{}]*)\{([^{}]*)\}",
        re.DOTALL,
    )
    match = pattern.search(css)
    return match.group(2) if match else ""


def _keyframes(css: str, name: str) -> str:
    match = re.search(r"@keyframes\s+" + re.escape(name) + r"\s*\{", css, re.IGNORECASE)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:index]
    return ""


def _animation(rule: str) -> tuple[str, int]:
    """Return (keyframes name, duration in milliseconds) from a rule."""
    value = ""
    match = re.search(r"animation\s*:\s*([^;]+)", rule, re.IGNORECASE)
    if match:
        value = match.group(1)
    name = ""
    duration_ms = 0
    for token in value.replace(",", " ").split():
        lowered = token.lower()
        if lowered.endswith("ms") and lowered[:-2].isdigit():
            duration_ms = int(lowered[:-2])
        elif lowered.endswith("s") and lowered[:-1].replace(".", "", 1).isdigit():
            duration_ms = int(round(float(lowered[:-1]) * 1000))
        elif not name and token not in ("linear", "ease", "ease-in", "ease-out", "ease-in-out",
                                        "infinite", "normal", "forwards", "backwards", "both"):
            name = token
    if not name:
        name = (re.search(r"animation-name\s*:\s*([^;]+)", rule, re.IGNORECASE) or [None, ""])[1].strip()
    if not duration_ms:
        raw = (re.search(r"animation-duration\s*:\s*([^;]+)", rule, re.IGNORECASE) or [None, ""])[1].strip()
        if raw.endswith("ms") and raw[:-2].isdigit():
            duration_ms = int(raw[:-2])
        elif raw.endswith("s") and raw[:-1].replace(".", "", 1).isdigit():
            duration_ms = int(round(float(raw[:-1]) * 1000))
    return name, duration_ms


def _translate_px(body: str) -> int:
    values = [abs(float(value)) for value in re.findall(r"translateX\(\s*(-?[\d.]+)px\s*\)", body, re.IGNORECASE)]
    return int(round(max(values))) if values else 0


def _wheel_radius(html: str) -> int:
    values = [float(value) for value in re.findall(r"<circle\b[^>]*\br\s*=\s*[\"']([\d.]+)[\"']", html, re.IGNORECASE)]
    return int(round(max(values))) if values else 0


def _has_class(html: str, name: str) -> bool:
    pattern = r'class\s*=\s*["\'][^"\']*(?<![\w-])' + re.escape(name) + r'(?![\w-])'
    return bool(re.search(pattern, html, re.IGNORECASE))


def analyze_motion(html: str) -> dict[str, Any]:
    """Return a bounded, JSON-safe quality verdict for one HTML animation."""
    css = _css(html)
    wheel_rule = _rule(css, ".wheel")
    wheel_name, wheel_ms = _animation(wheel_rule)
    wheel_body = _keyframes(css, wheel_name) if wheel_name else ""
    radius = _wheel_radius(html)
    wheel_speed = int(round(2 * math.pi * radius * 1000 / wheel_ms)) if radius and wheel_ms else 0

    bg_rule = _rule(css, ".scenery") or _rule(css, ".background")
    bg_name, bg_ms = _animation(bg_rule)
    bg_body = _keyframes(css, bg_name) if bg_name else ""
    bg_px = _translate_px(bg_body)
    bg_speed = int(round(bg_px * 1000 / bg_ms)) if bg_px and bg_ms else 0

    crank_rule = _rule(css, ".crank")
    _, crank_ms = _animation(crank_rule)
    leg_rule = _rule(css, ".leg")
    _, leg_ms = _animation(leg_rule)

    slip_bp = int(round(abs(wheel_speed - bg_speed) / max(wheel_speed, 1) * 10000)) if wheel_speed and bg_speed else 0
    phase_bp = int(round(abs(crank_ms - leg_ms) / max(crank_ms, 1) * 10000)) if crank_ms and leg_ms else 0
    # Support is a role, not a bicycle-only noun: a bicycle has pedals, a
    # scooter has a deck, another rig may expose a generic support marker.
    support = any(_has_class(html, name) for name in ("pedal", "deck", "support"))
    foot_pedal = _has_class(html, "foot") and support

    issues: list[str] = []
    blocking = False
    if not radius or not wheel_ms or "rotate(" not in wheel_body.lower():
        issues.append("wheel: missing radius, spin duration, or rotate() keyframes")
        blocking = True
    if not bg_ms or not bg_px or "translatex(" not in bg_body.lower().replace(" ", ""):
        issues.append("background: missing scroll duration or translateX() keyframes")
        blocking = True
    if slip_bp > 1000:
        issues.append(
            f"wheel/ground speed mismatch: wheel {wheel_speed} px/s vs background {bg_speed} px/s "
            f"({slip_bp / 100:.1f}% slip)"
        )
        blocking = blocking or slip_bp > 2000
    if phase_bp > 500:
        issues.append(
            f"crank/leg period mismatch: crank {crank_ms} ms vs leg {leg_ms} ms "
            f"({phase_bp / 100:.1f}%)"
        )
        blocking = blocking or phase_bp > 2000
    if not foot_pedal:
        issues.append(
            "missing .foot/.pedal/.deck/.support hooks: foot-to-support contact "
            "cannot be verified"
        )

    score = 100
    if slip_bp:
        score -= min(30, int(round(slip_bp / 10000 * 30)))
    if phase_bp:
        score -= min(20, int(round(phase_bp / 10000 * 20)))
    if not foot_pedal:
        score -= 10
    if not radius or not wheel_ms or not bg_ms or not bg_px:
        score -= 30
    score = max(0, min(100, score))
    status = "fail" if blocking else ("warn" if issues or score < 90 else "pass")
    return {
        "status": status,
        "score": score,
        "issues": issues,
        "wheel_speed_px_s": wheel_speed,
        "background_speed_px_s": bg_speed,
        "slip_bp": slip_bp,
        "crank_period_ms": crank_ms,
        "leg_period_ms": leg_ms,
        "phase_mismatch_bp": phase_bp,
        "foot_pedal_hooks": foot_pedal,
    }


def probe_quality(probe: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a browser frame probe into a bounded geometry verdict.

    Coverage is part of the verdict. A missing measurement is never a pass:
    absent ``.hip``/leg-path/foot data produces ``unverified`` unless a measured
    invariant has already failed. Every number is integer pixels so the result
    can enter the hash chain.
    """
    required = list(_GEOMETRY_CHECKS)
    if not isinstance(probe, dict) or not probe.get("ok"):
        detail = str((probe or {}).get("error") or "frame probe unavailable")
        return _unverified_geometry(
            required,
            [f"geometry unavailable: {detail[:300]}"],
            contact="",
        )

    geometry = [item for item in (probe.get("geometry") or []) if isinstance(item, dict)]
    frames = [item for item in (probe.get("frames") or []) if isinstance(item, dict)]
    if not geometry:
        return _unverified_geometry(
            required,
            ["geometry unavailable: the frame probe returned no samples"],
            frames=frames,
            contact=str(probe.get("contact") or ""),
        )

    max_foot = 0
    unpaired = 0
    leg_wheel = 0
    leg_leg = 0
    missing_feet = 0
    missing_pedals = 0
    missing_legs = 0
    hip_count = 0
    legs_checked = 0
    max_hip_distance = 0
    min_leg_length: int | None = None
    max_foot_endpoint = 0
    details: list[dict[str, Any]] = []
    worst: dict[str, Any] | None = None
    every_foot_pedal = True
    every_leg_presence = True
    every_hip_anchor = True
    every_leg_length = True
    every_leg_endpoint = True
    every_animation_contract = True
    every_composition = True
    composition_measured = True
    wheel_count = 0
    offscreen_parts: list[str] = []
    animation_mismatch: list[str] = []

    for item in geometry:
        feet = int(item.get("feet") or 0)
        pedals = int(item.get("pedals") or 0)
        legs = int(item.get("legs") or 0)
        hips = item.get("hip_centers") or []
        names = item.get("animation_names") or {}
        leg_names = [str(name).strip() for name in (names.get("leg") or []) if str(name).strip()]
        wheel_names = [str(name).strip() for name in (names.get("wheel") or []) if str(name).strip()]
        background_names = [
            str(name).strip() for name in (names.get("background") or []) if str(name).strip()
        ]
        if not (leg_names and wheel_names and background_names):
            every_animation_contract = False
        else:
            if any(name != "pedal" for name in leg_names):
                animation_mismatch.extend(
                    name for name in leg_names if name != "pedal" and name not in animation_mismatch
                )
            if any(name != "wheel-spin" for name in wheel_names):
                animation_mismatch.extend(
                    name for name in wheel_names if name != "wheel-spin" and name not in animation_mismatch
                )
            if any(name != "scenery-scroll" for name in background_names):
                animation_mismatch.extend(
                    name for name in background_names
                    if name != "scenery-scroll" and name not in animation_mismatch
                )
        scene_box = item.get("scene_box")
        wheel_boxes = item.get("wheel_boxes")
        if not isinstance(scene_box, dict) or not isinstance(wheel_boxes, list):
            composition_measured = False
        else:
            wheel_count = max(wheel_count, len(wheel_boxes))
            if len(wheel_boxes) < 2:
                every_composition = False
                offscreen_parts.append("wheel count < 2")
            for wheel_index, wheel_box in enumerate(wheel_boxes):
                inside = _box_inside(wheel_box, scene_box)
                if inside is None:
                    composition_measured = False
                elif not inside:
                    every_composition = False
                    offscreen_parts.append(f"wheel[{wheel_index}]")
            for part_name in ("bicycle", "pelican"):
                part_box = item.get(part_name)
                if part_box is None:
                    continue
                inside = _box_inside(part_box, scene_box)
                if inside is None:
                    composition_measured = False
                elif not inside:
                    every_composition = False
                    offscreen_parts.append(part_name)
        hip_count = max(hip_count, len(hips))
        distances = [
            int(round(float(value)))
            for value in (item.get("foot_pedal_px") or [])
            if value is not None
        ]
        unpaired += sum(1 for value in (item.get("foot_pedal_px") or []) if value is None)
        max_foot = max(max_foot, *(distances or [0]))
        for detail in item.get("foot_pedal_details") or []:
            if not isinstance(detail, dict):
                continue
            distance = detail.get("distance_px")
            if distance is None:
                continue
            record = {
                "time_ms": int(item.get("time_ms") or 0),
                "foot_index": int(detail.get("foot_index") or 0),
                "foot_class": str(detail.get("foot_class") or "")[:40],
                "pedal_index": int(detail.get("pedal_index") or 0),
                "distance_px": int(round(float(distance))),
            }
            details.append(record)
            if worst is None or record["distance_px"] > worst["distance_px"]:
                worst = record
        leg_wheel = max(leg_wheel, int(item.get("leg_wheel_overlap_px2") or 0))
        leg_leg = max(leg_leg, int(item.get("leg_leg_overlap_px2") or 0))
        if feet == 0:
            missing_feet += 1
        if pedals == 0:
            missing_pedals += 1
        if legs == 0:
            missing_legs += 1
        every_foot_pedal = every_foot_pedal and feet > 0 and pedals > 0
        item_legs = [leg for leg in (item.get("leg_details") or []) if isinstance(leg, dict)]
        every_leg_presence = every_leg_presence and len(item_legs) >= 2
        item_has_hip = bool(hips)
        item_has_anchor = bool(item_legs) and item_has_hip
        item_has_length = bool(item_legs)
        item_has_endpoint = bool(item_legs) and feet > 0
        for leg in item_legs:
            legs_checked += 1
            start = leg.get("start")
            end = leg.get("end")
            length = int(round(float(leg.get("length_px") or 0)))
            if not leg.get("has_path") or length <= 0:
                item_has_length = False
            if start is None:
                item_has_anchor = False
            else:
                nearest_hip = _nearest_distance(start, hips)
                if nearest_hip is not None:
                    max_hip_distance = max(max_hip_distance, nearest_hip)
                else:
                    item_has_anchor = False
            if end is None:
                item_has_endpoint = False
            else:
                nearest_foot = _nearest_distance(end, item.get("foot_centers") or [])
                if nearest_foot is not None:
                    max_foot_endpoint = max(max_foot_endpoint, nearest_foot)
                else:
                    item_has_endpoint = False
            if length > 0:
                min_leg_length = length if min_leg_length is None else min(min_leg_length, length)
        every_hip_anchor = every_hip_anchor and item_has_anchor
        every_leg_length = every_leg_length and item_has_length
        every_leg_endpoint = every_leg_endpoint and item_has_endpoint

    covered = ["frame_capture"] if frames else []
    if composition_measured:
        covered.append("composition")
    if every_animation_contract:
        covered.append("animation_contract")
    if every_foot_pedal:
        covered.append("foot_pedal")
    if every_leg_presence:
        covered.append("leg_presence")
    if every_hip_anchor:
        covered.append("hip_anchor")
    if every_leg_length:
        covered.append("leg_length")
    if every_leg_endpoint:
        covered.append("leg_endpoint")
    uncovered = [name for name in required if name not in covered]

    issues: list[str] = []
    required_fixes: list[dict[str, Any]] = []
    blocking = False
    score = 100
    if missing_feet:
        issues.append("no .foot element: foot-to-pedal contact cannot be measured")
        required_fixes.append({
            "code": "MISSING_FOOT_HOOK", "selector": ".foot",
            "expected": "at least one .foot", "actual": "0",
            "message": "add .foot elements for both feet",
        })
        blocking = True
        score -= 40
    if missing_pedals:
        issues.append("no .pedal element: foot-to-pedal contact cannot be measured")
        required_fixes.append({
            "code": "MISSING_PEDAL_HOOK", "selector": ".pedal",
            "expected": "at least one .pedal", "actual": "0",
            "message": "add .pedal elements for both pedals",
        })
        blocking = True
        score -= 40
    if missing_legs:
        issues.append("no .leg element: leg geometry cannot be measured")
        required_fixes.append({
            "code": "MISSING_LEG_HOOK", "selector": ".leg",
            "expected": "two .leg paths", "actual": "0",
            "message": "draw two legs as hip-to-foot paths",
        })
        blocking = True
        score -= 30
    if composition_measured and not every_composition:
        parts = ", ".join(dict.fromkeys(offscreen_parts)) or "scene part"
        issues.append(
            f"composition: {parts} is outside or clipped by the scene; "
            "keep both wheels and the bike/pelican fully inside the viewBox"
        )
        if wheel_count < 2:
            fix = {
                "code": "WHEEL_COUNT", "selector": ".wheel",
                "expected": "at least two .wheel elements",
                "actual": str(wheel_count),
                "message": "draw both bicycle wheels, not one",
            }
        elif any(part.startswith("wheel[") for part in offscreen_parts):
            fix = {
                "code": "WHEEL_OFFSCREEN", "selector": ".wheel",
                "expected": "both wheels inside the scene viewBox",
                "actual": parts,
                "message": "move the wheels back inside the scene; do not trade composition for leg fixes",
            }
        else:
            fix = {
                "code": "PART_OFFSCREEN", "selector": ".bicycle/.pelican",
                "expected": "main parts inside the scene viewBox",
                "actual": parts,
                "message": "keep the bicycle and pelican inside the visible scene",
            }
        required_fixes.append(fix)
        blocking = True
        score -= 40
    if unpaired:
        issues.append(f"{unpaired} sampled foot(s) have no pedal to contact")
        required_fixes.append({
            "code": "FOOT_WITHOUT_PEDAL", "selector": ".foot/.pedal",
            "expected": "every .foot near a .pedal", "actual": f"{unpaired} unpaired",
            "message": "align each foot with one pedal at every sampled frame",
        })
        blocking = True
        score -= 30
    if max_foot > FOOT_PEDAL_TOLERANCE_PX:
        where = ""
        if worst is not None:
            label = worst["foot_class"] or f"foot[{worst['foot_index']}]"
            where = (
                f"; worst is {label} at {worst['time_ms']}ms, "
                f"nearest .pedal[{worst['pedal_index']}]"
            )
        issues.append(
            f"foot leaves pedal: max centre distance {max_foot}px "
            f"(must stay <= {FOOT_PEDAL_TOLERANCE_PX}px at sampled frames){where}"
        )
        required_fixes.append({
            "code": "FOOT_PEDAL_DISTANCE", "selector": ".foot/.pedal",
            "expected": f"<= {FOOT_PEDAL_TOLERANCE_PX}px",
            "actual": f"{max_foot}px",
            "message": "make the foot follow the pedal through the whole cycle",
        })
        blocking = True
        score -= 35
    elif max_foot > FOOT_PEDAL_WARN_PX:
        issues.append(
            f"foot/pedal contact is loose: max centre distance {max_foot}px "
            f"(aim for <= {FOOT_PEDAL_WARN_PX}px)"
        )
        score -= 15
    if animation_mismatch:
        issues.append(
            "animation contract: computed names are "
            + ", ".join(sorted(animation_mismatch)[:4])
            + "; .wheel must use wheel-spin, .leg must use pedal, "
            "background must use scenery-scroll"
        )
        required_fixes.append({
            "code": "ANIMATION_NAME", "selector": ".wheel/.leg/.scenery",
            "expected": "wheel-spin / pedal / scenery-scroll",
            "actual": ", ".join(sorted(animation_mismatch)[:6]),
            "message": "rename or rewire the animation to the exact contract names",
        })
        blocking = True
        score -= 25
    if leg_wheel > 0:
        issues.append(
            f"leg/wheel overlap: bounding boxes intersect by {leg_wheel}px² "
            "(limb appears to pass through the wheel)"
        )
        required_fixes.append({
            "code": "LEG_WHEEL_OVERLAP", "selector": ".leg/.wheel",
            "expected": "0px²", "actual": f"{leg_wheel}px²",
            "message": "move the leg or wheel so their bounds do not intersect",
        })
        blocking = True
        score -= 30
    if every_hip_anchor and max_hip_distance > HIP_ANCHOR_TOLERANCE_PX:
        issues.append(
            f"leg does not start at the hip: farthest leg start is {max_hip_distance}px "
            f"from the nearest .hip (must be <= {HIP_ANCHOR_TOLERANCE_PX}px)"
        )
        required_fixes.append({
            "code": "HIP_ANCHOR_DISTANCE", "selector": ".leg/.hip",
            "expected": f"<= {HIP_ANCHOR_TOLERANCE_PX}px",
            "actual": f"{max_hip_distance}px",
            "message": "move every leg start onto the .hip anchor",
        })
        blocking = True
        score -= 35
    if min_leg_length is not None and min_leg_length < LEG_LENGTH_MIN_PX:
        issues.append(
            f"leg is too short: shortest leg is {min_leg_length}px "
            f"(must be >= {LEG_LENGTH_MIN_PX}px)"
        )
        required_fixes.append({
            "code": "LEG_TOO_SHORT", "selector": ".leg",
            "expected": f">= {LEG_LENGTH_MIN_PX}px", "actual": f"{min_leg_length}px",
            "message": "draw the leg from hip to foot instead of a short crank spoke",
        })
        blocking = True
        score -= 30
    if every_leg_endpoint and max_foot_endpoint > LEG_ENDPOINT_TOLERANCE_PX:
        issues.append(
            f"leg does not end at the foot: farthest leg end is {max_foot_endpoint}px "
            f"from the nearest .foot (must be <= {LEG_ENDPOINT_TOLERANCE_PX}px)"
        )
        required_fixes.append({
            "code": "LEG_ENDPOINT_DISTANCE", "selector": ".leg/.foot",
            "expected": f"<= {LEG_ENDPOINT_TOLERANCE_PX}px",
            "actual": f"{max_foot_endpoint}px",
            "message": "end each leg at its foot",
        })
        blocking = True
        score -= 30
    for name in uncovered:
        issues.append(f"coverage: {name} is not measured")
        required_fixes.append({
            "code": "COVERAGE_MISSING", "selector": "",
            "expected": name, "actual": "not measured",
            "message": f"measure or provide {name}",
        })
    score = max(0, min(100, score))
    if blocking:
        status = "fail"
    elif uncovered:
        status = "unverified"
        score = int(round(100 * len(covered) / len(required)))
    else:
        status = "warn" if issues or score < 90 else "pass"
    coverage = {
        "required": required,
        "covered": covered,
        "uncovered": uncovered,
        "complete": not uncovered,
    }
    return {
        "status": status,
        "score": score,
        "issues": issues,
        "required_fixes": required_fixes,
        "coverage": coverage,
        "frame_count": len(frames),
        "hip_count": hip_count,
        "legs_checked": legs_checked,
        "max_leg_start_to_hip_px": max_hip_distance,
        "min_leg_length_px": min_leg_length or 0,
        "max_leg_end_to_foot_px": max_foot_endpoint,
        "max_foot_pedal_px": max_foot,
        "unpaired_feet": unpaired,
        "leg_wheel_overlap_px2": leg_wheel,
        "leg_leg_overlap_px2": leg_leg,
        "composition_ok": bool(composition_measured and every_composition),
        "composition_measured": composition_measured,
        "wheel_count": wheel_count,
        "offscreen_parts": list(dict.fromkeys(offscreen_parts)),
        "geometry_status": status,
        "frames": _frame_records(frames),
        "foot_pedal_details": details[:8],
        "contact": str(probe.get("contact") or ""),
    }


def merge_quality(base: dict[str, Any], probe: dict[str, Any] | None) -> dict[str, Any]:
    """Combine text invariants with coverage-aware rendered geometry.

    Failures outrank missing coverage; missing coverage outranks a text pass.
    The terminal run status is deliberately not part of this function: a
    finished artifact can be functionally correct and semantically unverified.
    """
    geometry = probe_quality(probe)
    required = ["text_invariants", *geometry["coverage"]["required"]]
    covered = ["text_invariants", *geometry["coverage"]["covered"]]
    uncovered = [name for name in required if name not in covered]
    issues = list(base.get("issues") or [])
    for issue in geometry["issues"]:
        if issue not in issues:
            issues.append(issue)
    fixes = list(geometry.get("required_fixes") or [])
    existing_fixes = {(fix.get("code"), fix.get("message")) for fix in fixes}
    for issue in base.get("issues") or []:
        candidate = {"code": "TEXT_INVARIANT", "selector": "", "expected": "", "actual": "",
                     "message": str(issue)}
        key = (candidate["code"], candidate["message"])
        if key not in existing_fixes:
            fixes.append(candidate)
            existing_fixes.add(key)
    if str(base.get("status") or "") == "fail" or geometry["status"] == "fail":
        status = "fail"
    elif uncovered:
        status = "unverified"
    elif "warn" in (base.get("status"), geometry["status"]):
        status = "warn"
    else:
        status = "pass"
    if status == "unverified":
        score = int(round(100 * len(covered) / max(len(required), 1)))
    else:
        score = min(int(base.get("score") or 0), int(geometry.get("score") or 0))
    merged = dict(base)
    merged["status"] = status
    merged["score"] = max(0, min(100, score))
    merged["issues"] = issues
    merged["required_fixes"] = fixes
    merged["coverage"] = {
        "required": required,
        "covered": covered,
        "uncovered": uncovered,
        "complete": not uncovered,
    }
    for key in (
        "frame_count", "hip_count", "legs_checked",
        "max_leg_start_to_hip_px", "min_leg_length_px", "max_leg_end_to_foot_px",
        "max_foot_pedal_px", "unpaired_feet", "leg_wheel_overlap_px2",
        "leg_leg_overlap_px2", "geometry_status", "frames",
        "foot_pedal_details", "contact",
        "composition_ok", "composition_measured", "wheel_count", "offscreen_parts",
    ):
        merged[key] = geometry[key]
    return merged


def _unverified_geometry(
    required: list[str],
    issues: list[str],
    *,
    frames: list[dict[str, Any]] | None = None,
    contact: str = "",
) -> dict[str, Any]:
    fixes = [
        {
            "code": "COVERAGE_MISSING",
            "selector": "",
            "expected": name,
            "actual": "not measured",
            "message": f"measure or provide {name}",
        }
        for name in required
    ]
    return {
        "status": "unverified",
        "score": 0,
        "issues": issues,
        "required_fixes": fixes,
        "coverage": {
            "required": required,
            "covered": [],
            "uncovered": list(required),
            "complete": False,
        },
        "frame_count": len(frames or []),
        "hip_count": 0,
        "legs_checked": 0,
        "max_leg_start_to_hip_px": 0,
        "min_leg_length_px": 0,
        "max_leg_end_to_foot_px": 0,
        "max_foot_pedal_px": 0,
        "unpaired_feet": 0,
        "leg_wheel_overlap_px2": 0,
        "leg_leg_overlap_px2": 0,
        "composition_ok": False,
        "composition_measured": False,
        "wheel_count": 0,
        "offscreen_parts": [],
        "geometry_status": "unverified",
        "frames": _frame_records(frames or []),
        "foot_pedal_details": [],
        "contact": contact,
    }


def _nearest_distance(point: dict[str, Any], centers: list[dict[str, Any]]) -> int | None:
    distances = []
    for center in centers:
        if not isinstance(center, dict):
            continue
        try:
            distances.append(
                ((float(point["x"]) - float(center["cx"])) ** 2
                 + (float(point["y"]) - float(center["cy"])) ** 2) ** 0.5
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not distances:
        return None
    return int(round(min(distances)))


def _box_inside(box: Any, scene: Any, margin: int = COMPOSITION_MARGIN_PX) -> bool | None:
    """Whether a rendered box stays inside the scene, allowing a small touch.

    ``None`` means the measurement is missing/malformed, not that the part is
    outside; coverage handles the missing case.
    """
    if not isinstance(box, dict) or not isinstance(scene, dict):
        return None
    try:
        x = float(box["x"])
        y = float(box["y"])
        w = float(box["w"])
        h = float(box["h"])
        sx = float(scene["x"])
        sy = float(scene["y"])
        sw = float(scene["w"])
        sh = float(scene["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return (
        x >= sx - margin
        and y >= sy - margin
        and x + w <= sx + sw + margin
        and y + h <= sy + sh + margin
    )


def _frame_records(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the fields the UI/audit need; no browser object can leak through."""
    records = []
    for frame in frames:
        records.append({
            "time_ms": int(frame.get("time_ms") or 0),
            "name": str(frame.get("name") or ""),
            "sha256": str(frame.get("sha256") or ""),
            "width": int(frame.get("width") or 0),
            "height": int(frame.get("height") or 0),
        })
    return records
