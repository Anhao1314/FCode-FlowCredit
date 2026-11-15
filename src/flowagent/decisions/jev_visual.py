"""Scene-descriptor-driven, advisory Jev visual criticism.

The core is intentionally subject-agnostic. A scene descriptor supplies the
subject, vehicle, support surface and which relations are expected to be
visible. Specific characters and vehicles live only in JSON fixtures; the same
request builder and classifier serve every scene.

Nothing here is allowed to turn a host verifier's ``fail`` into ``pass``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "DEFAULT_SCENE",
    "build_request",
    "classify_visual",
    "load_env_file",
    "load_scene",
    "prepare_image",
    "run_systemone",
]

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MAX_IMAGE_SIDE = 1568
MIN_IMAGE_SIDE = 280
MAX_TOTAL_PIXELS = 2_458_624

#: The fallback is deliberately generic. A real task supplies a scene JSON; the
#: core never knows a specific character or vehicle name.
DEFAULT_SCENE: dict[str, Any] = {
    "id": "generic-scene",
    "subject": {
        "label": "main character",
        "description": "the main character of the scene",
    },
    "vehicle": {
        "label": "vehicle",
        "description": "the vehicle or support the character is using",
        "support_label": "support surface",
    },
    "features": {
        "two_legs": True,
        "legs_attach": True,
        "feet_on_support": True,
    },
    "composition": "the character using the vehicle",
    "crop": None,
    "scale": 1.0,
}


def load_env_file(path: Path | str) -> dict[str, str]:
    """Read ``export KEY=VALUE`` lines without ever logging a value."""
    values: dict[str, str] = {}
    text = Path(path).expanduser().read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def load_scene(path: Path | str) -> dict[str, Any]:
    """Load and minimally normalise one scene descriptor."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scene descriptor must be a JSON object")
    scene = {**DEFAULT_SCENE, **data}
    scene["subject"] = {**DEFAULT_SCENE["subject"], **(data.get("subject") or {})}
    scene["vehicle"] = {**DEFAULT_SCENE["vehicle"], **(data.get("vehicle") or {})}
    scene["features"] = {**DEFAULT_SCENE["features"], **(data.get("features") or {})}
    return scene


def prepare_image(
    image_path: Path | str,
    *,
    crop: tuple[int, int, int, int] | None = None,
    scale: float = 1.0,
) -> dict[str, Any]:
    """Crop, resize and encode one PNG within Jev's image limits.

    ``crop`` comes from the scene descriptor, not from a hardcoded character.
    The returned metadata records the transform so a Box answer can be mapped
    back to source pixels later.
    """
    from PIL import Image  # imported lazily so non-visual deployments need no PIL

    source = Image.open(image_path).convert("RGB")
    if crop is None:
        image = source
        left, top, right, bottom = 0, 0, source.width, source.height
    else:
        left, top, right, bottom = crop
        left = max(0, min(left, source.width - 1))
        top = max(0, min(top, source.height - 1))
        right = max(left + 1, min(right, source.width))
        bottom = max(top + 1, min(bottom, source.height))
        image = source.crop((left, top, right, bottom))
    width = max(MIN_IMAGE_SIDE, min(MAX_IMAGE_SIDE, int(round(image.width * scale))))
    height = max(MIN_IMAGE_SIDE, min(MAX_IMAGE_SIDE, int(round(image.height * scale))))
    if width * height > MAX_TOTAL_PIXELS:
        shrink = (MAX_TOTAL_PIXELS / (width * height)) ** 0.5
        width = max(MIN_IMAGE_SIDE, int(width * shrink))
        height = max(MIN_IMAGE_SIDE, int(height * shrink))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    return {
        "data_base64": base64.b64encode(data).decode("ascii"),
        "format": "png",
        "bytes": len(data),
        "width": width,
        "height": height,
        "crop": {"left": left, "top": top, "right": right, "bottom": bottom},
        "scale": scale,
    }


def _features(scene: dict[str, Any]) -> tuple[bool, bool, bool]:
    features = scene.get("features") or {}
    return (
        bool(features.get("two_legs", True)),
        bool(features.get("legs_attach", True)),
        bool(features.get("feet_on_support", True)),
    )


def _questions(scene: dict[str, Any], include_boxes: bool = True) -> dict[str, Any]:
    subject = str((scene.get("subject") or {}).get("label") or "main character")
    subject_description = str(
        (scene.get("subject") or {}).get("description") or subject
    )
    vehicle = str((scene.get("vehicle") or {}).get("label") or "vehicle")
    vehicle_description = str(
        (scene.get("vehicle") or {}).get("description") or vehicle
    )
    support = str(
        (scene.get("vehicle") or {}).get("support_label") or "support surface"
    )
    two_legs, legs_attach, feet_on_support = _features(scene)
    composition = str(scene.get("composition") or f"{subject} using the {vehicle}")

    questions: dict[str, Any] = {
        "subject_visible": {
            "type": "noul",
            "instructions": f"Is a {subject} ({subject_description}) clearly visible in `frame-main`?",
        },
        "vehicle_visible": {
            "type": "noul",
            "instructions": f"Is a {vehicle} ({vehicle_description}) clearly visible in `frame-main`?",
        },
    }
    if two_legs:
        questions["two_legs_visible"] = {
            "type": "noul",
            "instructions": (
                f"Can you see two distinct {subject} legs involved in the expected "
                "motion, rather than one static shape or one hidden leg?"
            ),
        }
    if legs_attach:
        questions["legs_attach_to_body"] = {
            "type": "noul",
            "instructions": (
                f"Do the visible legs appear to start at the {subject}'s body/hip and "
                f"reach toward the {support}, rather than starting at the crank center?"
            ),
        }
    if feet_on_support:
        questions["near_foot_on_support"] = {
            "type": "noul",
            "instructions": f"Is the foot nearest the viewer on the {support}?",
        }
        questions["far_foot_on_support"] = {
            "type": "noul",
            "instructions": f"Is the farther foot on the {support}?",
        }

    occlusion_criteria = {
        "none": "The body and any limbs are readable, with normal vehicle layering.",
        "one_leg_hidden": "One leg is mostly hidden, so the motion is hard to read.",
        "legs_through_vehicle": "A leg visibly passes through the vehicle frame/body.",
        "legs_through_wheel": "A leg visibly passes through a wheel.",
        "unclear": "The image is too small or ambiguous to tell.",
    }
    questions["occlusion"] = {
        "type": "choice",
        "instructions": "What best describes the limbs' visibility?",
        "criteria": occlusion_criteria,
    }
    questions["composition"] = {
        "type": "score",
        "instructions": f"How readable is this composition: {composition}?",
        "criteria": ["broken", "weak", "acceptable", "good", "strong"],
    }

    if include_boxes:
        if legs_attach:
            questions["hip_box"] = {
                "type": "box",
                "instructions": f"Locate the {subject}'s hip/pelvis where the legs attach in `frame-main`.",
            }
        if feet_on_support:
            questions["near_foot_box"] = {
                "type": "box",
                "instructions": "Locate the foot nearest the viewer in `frame-main`.",
            }
            questions["near_support_box"] = {
                "type": "box",
                "instructions": f"Locate the {support} under the nearest foot in `frame-main`.",
            }
    return questions


def build_request(
    model: str,
    image: dict[str, Any],
    scene: dict[str, Any] | None = None,
    *,
    include_boxes: bool = True,
) -> dict[str, Any]:
    """Build a System One request for one image and one scene descriptor."""
    scene = scene or DEFAULT_SCENE
    asset = "frame-main"
    return {
        "model": model,
        "state": {"frame": asset},
        "assets": {
            asset: {
                "type": "image",
                "format": image["format"],
                "data_base64": image["data_base64"],
            },
        },
        "questions": _questions(scene, include_boxes),
    }


def _noul(answers: dict[str, Any], key: str) -> float | None:
    answer = answers.get(key)
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _choice(answers: dict[str, Any], key: str) -> dict[str, Any] | None:
    answer = answers.get(key)
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None
    return {
        "choice": choice,
        "confidence": float(answer.get("confidence") or 0.0),
        "probabilities": answer.get("probabilities") or {},
    }


def _score(answers: dict[str, Any], key: str) -> dict[str, Any] | None:
    answer = answers.get(key)
    if not isinstance(answer, dict):
        return None
    value = answer.get("score")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return {
        "score": float(value),
        "confidence": float(answer.get("confidence") or 0.0),
        "legend": answer.get("legend") or [],
    }


def classify_visual(
    answers: dict[str, Any],
    scene: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn typed answers into an advisory verdict with explicit uncertainty.

    Every required relation comes from the scene descriptor. Missing answers
    yield ``unverified``, never ``pass``. Low-confidence occlusion is a warning,
    not a hard fail: the image primitive is explicitly experimental.
    """
    scene = scene or DEFAULT_SCENE
    two_legs, legs_attach, feet_on_support = _features(scene)
    subject = _noul(answers, "subject_visible")
    vehicle = _noul(answers, "vehicle_visible")
    two = _noul(answers, "two_legs_visible") if two_legs else None
    attach = _noul(answers, "legs_attach_to_body") if legs_attach else None
    near_foot = _noul(answers, "near_foot_on_support") if feet_on_support else None
    far_foot = _noul(answers, "far_foot_on_support") if feet_on_support else None
    occlusion = _choice(answers, "occlusion")
    composition = _score(answers, "composition")

    signals = {
        "subject_visible": subject,
        "vehicle_visible": vehicle,
        "two_legs_visible": two,
        "legs_attach_to_body": attach,
        "near_foot_on_support": near_foot,
        "far_foot_on_support": far_foot,
        "occlusion": occlusion,
        "composition": composition,
    }
    issues: list[str] = []
    hard_fail = False
    soft_warn = False

    required_noul = [("subject_visible", subject), ("vehicle_visible", vehicle)]
    if two_legs:
        required_noul.append(("two_legs_visible", two))
    if legs_attach:
        required_noul.append(("legs_attach_to_body", attach))
    for key, value in required_noul:
        if value is None:
            issues.append(f"{key}: no answer")
            return {
                "status": "unverified",
                "advisory_only": True,
                "issues": issues,
                "signals": signals,
            }
        if value < 0.3:
            issues.append(f"{key}: low probability ({value:.2f})")
            hard_fail = True
        elif value < 0.7:
            issues.append(f"{key}: uncertain ({value:.2f})")
            soft_warn = True

    if feet_on_support:
        for key, value in (("near_foot_on_support", near_foot),
                           ("far_foot_on_support", far_foot)):
            if value is None:
                issues.append(f"{key}: no answer")
                return {
                    "status": "unverified",
                    "advisory_only": True,
                    "issues": issues,
                    "signals": signals,
                }
            if value < 0.7:
                issues.append(f"{key}: uncertain ({value:.2f})")
                soft_warn = True

    if occlusion is None:
        return {
            "status": "unverified",
            "advisory_only": True,
            "issues": [*issues, "occlusion: no answer"],
            "signals": signals,
        }
    occlusion_confidence = float(occlusion.get("confidence") or 0.0)
    bad_occlusion = occlusion["choice"] in (
        "legs_through_vehicle", "legs_through_wheel",
        # compatibility with the pre-descriptor experiment's answer keys
        "leg_through_frame", "leg_through_wheel",
    )
    if bad_occlusion:
        if occlusion_confidence >= 0.7:
            issues.append(
                f"occlusion: {occlusion['choice']} (confidence {occlusion_confidence:.2f})"
            )
            hard_fail = True
        else:
            issues.append(
                f"occlusion: possible {occlusion['choice']} "
                f"(low confidence {occlusion_confidence:.2f}; needs human/visual check)"
            )
            soft_warn = True
    elif occlusion["choice"] == "one_leg_hidden":
        issues.append("occlusion: one leg hidden")
        soft_warn = True
    elif occlusion["choice"] == "unclear":
        issues.append("occlusion: unclear")
        soft_warn = True

    if composition is not None and composition["score"] < 2.0:
        issues.append(f"composition: {composition['score']:.2f} / 4")
        soft_warn = True

    if hard_fail:
        status = "fail"
    elif soft_warn:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "advisory_only": True,
        "issues": issues,
        "signals": signals,
        "scene": str(scene.get("id") or "generic-scene"),
        "note": (
            "Jev visual advisory only; it does not change the host quality gate, "
            "and confidence is distribution concentration, not correctness."
        ),
    }


def run_systemone(
    request: dict[str, Any],
    *,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 120.0,
    danger_images: bool = True,
) -> dict[str, Any]:
    """POST one System One request and return its JSON response."""
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if danger_images and request.get("assets"):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    http_request = urllib.request.Request(
        endpoint, data=body, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Jev HTTP {error.code}: {detail}") from error


def env_credentials(
    env_file: Path | str | None = None,
) -> tuple[str, str]:
    """Resolve API key/model from the process or an env file without printing."""
    values = dict(os.environ)
    if env_file:
        path = Path(env_file).expanduser()
        if path.is_file():
            values.update(load_env_file(path))
    api_key = values.get("TYPESAFE_API_KEY", "").strip()
    model = values.get("TYPESAFE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set; pass --env-file or export it")
    return api_key, model
