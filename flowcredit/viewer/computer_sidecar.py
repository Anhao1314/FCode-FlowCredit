#!/usr/bin/env python3
"""macOS desktop sidecar for computer use (optional pyobjc dependency).

The Python host owns policy, approval and evidence. This process only:

* reads the frontmost app/window and a bounded Accessibility tree;
* captures the target window (never the whole desktop by default);
* performs one AX-first action, with coordinate fallback;
* releases keys/mouse on stop.

Protocol: one JSON object per line on stdin/stdout. When pyobjc or TCC
permissions are missing, every command returns a structured error and the host
degrades to ``available: false`` instead of failing a run.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import time

MAX_NODES = 300
MAX_DEPTH = 8
MAX_TEXT = 12_000

try:  # optional dependency
    import Quartz
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        AXValueGetValue,
        kAXChildrenAttribute,
        kAXDescriptionAttribute,
        kAXEnabledAttribute,
        kAXFocusedAttribute,
        kAXFocusedWindowAttribute,
        kAXPositionAttribute,
        kAXPressAction,
        kAXRoleAttribute,
        kAXSizeAttribute,
        kAXSubroleAttribute,
        kAXTitleAttribute,
        kAXValueAttribute,
        kAXValueCGPointType,
        kAXValueCGSizeType,
        kAXWindowsAttribute,
    )
    from Cocoa import NSWorkspace
    _IMPORT_ERROR = ""
except Exception as error:  # noqa: BLE001
    Quartz = None
    AXValueGetValue = None
    _IMPORT_ERROR = f"pyobjc unavailable: {error.__class__.__name__}"


def failure(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": {"code": code, "message": str(message)[:400], **extra}}


def accessibility_ok() -> bool:
    try:
        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return False


def screen_recording_ok() -> bool:
    checker = getattr(Quartz, "CGPreflightScreenCaptureAccess", None) if Quartz else None
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001
        return False


def _attr(element, name):
    try:
        result = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(result, tuple) and len(result) == 2:
        first, second = result
        # PyObjC returns (AXError, value) for out-parameters; some builds
        # expose (value, error). Handle both without pretending an error
        # value is content.
        if isinstance(first, int) or first is None:
            return second if first == 0 else None
        return first if second == 0 else None
    return result


def _text(value, limit=200) -> str:
    if value is None:
        return ""
    try:
        return str(value)[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _ax_value(value, kind):
    """AXValueGetValue lives in ApplicationServices; some builds expose it via Quartz."""
    if AXValueGetValue is not None:
        try:
            return AXValueGetValue(value, kind, None)
        except Exception:  # noqa: BLE001
            pass
    try:
        getter = getattr(Quartz, "AXValueGetValue", None)
        if getter is not None:
            return getter(value, kind, None)
    except Exception:  # noqa: BLE001
        pass
    return False, None


def _point(value):
    if Quartz is None or value is None:
        return None
    try:
        ok, point = _ax_value(value, kAXValueCGPointType)
        if not ok:
            return None
        return int(point.x), int(point.y)
    except Exception:  # noqa: BLE001
        return None


def _size(value):
    if Quartz is None or value is None:
        return None
    try:
        ok, size = _ax_value(value, kAXValueCGSizeType)
        if not ok:
            return None
        return int(size.width), int(size.height)
    except Exception:  # noqa: BLE001
        return None


def frontmost():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()), _text(app.bundleIdentifier(), 200), _text(app.localizedName(), 100)


def window_id_for(pid: int, frame: dict | None = None):
    if Quartz is None:
        return 0, {}
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
    except Exception:  # noqa: BLE001
        return 0, {}
    candidates = []
    for item in windows:
        if int(item.get("kCGWindowOwnerPID") or 0) != pid:
            continue
        if int(item.get("kCGWindowLayer") or 0) != 0:
            continue
        number = int(item.get("kCGWindowNumber") or 0)
        if not number:
            continue
        bounds = item.get("kCGWindowBounds") or {}
        info = {
            "title": _text(item.get("kCGWindowName"), 200),
            "x": int(bounds.get("X") or 0),
            "y": int(bounds.get("Y") or 0),
            "width": int(bounds.get("Width") or 0),
            "height": int(bounds.get("Height") or 0),
        }
        # The AX focused window and the CG window list must be the same window;
        # otherwise the screenshot and the AX coordinates describe different
        # surfaces and redaction/coordinate fallback would be off by a window.
        if frame and frame.get("width") and frame.get("height") and all(
            abs(int(frame.get(key) or 0) - info[key]) <= 2
            for key in ("x", "y", "width", "height")
        ):
            return number, info
        candidates.append((number, info))
    return candidates[0] if candidates else (0, {})


def png_size(path: str):
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
            return int(width), int(height)
    except OSError:
        pass
    return 0, 0


def build_tree(element, prefix: str, depth: int, out: list, elements: list, secure: list):
    if depth > MAX_DEPTH or len(elements) >= MAX_NODES:
        return
    role = _text(_attr(element, kAXRoleAttribute), 80)
    subrole = _text(_attr(element, kAXSubroleAttribute), 80)
    title = _text(_attr(element, kAXTitleAttribute), 200)
    desc = _text(_attr(element, kAXDescriptionAttribute), 200)
    value = _attr(element, kAXValueAttribute)
    is_secure = role == "AXSecureTextField" or subrole == "AXSecureTextField"
    value_text = "" if is_secure else (_text(value, 300) if isinstance(value, str) else "")
    point = _point(_attr(element, kAXPositionAttribute))
    size = _size(_attr(element, kAXSizeAttribute))
    entry = {
        "id": prefix,
        "role": role,
        "subrole": subrole,
        "title": title,
        "description": desc,
        "value": value_text,
        "enabled": bool(_attr(element, kAXEnabledAttribute) is not False),
        "x": point[0] if point else 0,
        "y": point[1] if point else 0,
        "width": size[0] if size else 0,
        "height": size[1] if size else 0,
        "secure": is_secure,
    }
    elements.append(entry)
    out.append(entry)
    if entry["secure"] and point and size:
        secure.append({
            "x": point[0], "y": point[1],
            "width": size[0], "height": size[1],
        })
    children = _attr(element, kAXChildrenAttribute)
    if isinstance(children, (list, tuple)):
        for index, child in enumerate(children):
            build_tree(child, f"{prefix}/{index}" if prefix else str(index),
                       depth + 1, out, elements, secure)


def current_window(app_element, pid: int):
    window = _attr(app_element, kAXFocusedWindowAttribute)
    if window is None:
        windows = _attr(app_element, kAXWindowsAttribute)
        if isinstance(windows, (list, tuple)) and windows:
            window = windows[0]
    return window


def window_frame(window):
    point = _point(_attr(window, kAXPositionAttribute))
    size = _size(_attr(window, kAXSizeAttribute))
    if not point or not size:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    return {"x": point[0], "y": point[1], "width": size[0], "height": size[1]}


def command_look(command):
    if Quartz is None:
        return failure("COMPUTER_UNAVAILABLE", _IMPORT_ERROR)
    if not accessibility_ok():
        return failure("COMPUTER_PERMISSION_DENIED",
                       "Accessibility permission is required", permission="accessibility")
    if not screen_recording_ok():
        return failure("COMPUTER_PERMISSION_DENIED",
                       "Screen Recording permission is required", permission="screen_recording")
    pid, bundle, name = frontmost()
    apps = [str(item) for item in (command.get("apps") or [])]
    if apps and bundle not in apps:
        return failure("COMPUTER_WINDOW_NOT_ALLOWED",
                       f"frontmost app {bundle or name} is outside the session scope")
    app_element = AXUIElementCreateApplication(pid)
    window = current_window(app_element, pid)
    if window is None:
        return failure("COMPUTER_WINDOW_NOT_ALLOWED", "no frontmost window")
    elements: list = []
    secure: list = []
    build_tree(window, "", 0, [], elements, secure)
    text_parts = []
    for item in elements[:80]:
        if item["title"] or item["value"]:
            text_parts.append(f"{item['role']}: {item['title'] or item['value']}")
    screenshot = str(command.get("screenshot_path") or "")
    ax_path = str(command.get("ax_path") or "")
    frame = window_frame(window)
    window_num, window_info = window_id_for(pid, frame)
    if not window_num:
        return failure("COMPUTER_WINDOW_NOT_ALLOWED", "no capturable on-screen window")
    width = height = 0
    if screenshot:
        try:
            subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-o", "-l", str(window_num), screenshot],
                check=True, capture_output=True, timeout=10,
            )
            width, height = png_size(screenshot)
        except Exception as error:  # noqa: BLE001
            return failure("COMPUTER_CAPTURE_FAILED", error.__class__.__name__)
    if ax_path:
        with open(ax_path, "w", encoding="utf-8") as handle:
            json.dump({"app": {"bundle_id": bundle, "name": name},
                       "window": window_info, "elements": elements}, handle,
                      ensure_ascii=False)
    return {
        "ok": True,
        "app": {"bundle_id": bundle, "name": name, "pid": pid},
        "window": window_info or window_frame(window),
        "elements": elements[:200],
        "text": "\n".join(text_parts)[:MAX_TEXT],
        "secure_regions": secure[:20],
        "width": width,
        "height": height,
    }


def _element_map(command):
    # IDs are paths into the last look's tree. Rebuild from the live tree on
    # every act; a vanished element is stale, never silently retargeted.
    pid, bundle, name = frontmost()
    app_element = AXUIElementCreateApplication(pid)
    window = current_window(app_element, pid)
    if window is None:
        return None, None, None
    mapping = {}

    def walk(element, prefix, depth):
        if depth > MAX_DEPTH:
            return
        mapping[prefix] = element
        children = _attr(element, kAXChildrenAttribute)
        if isinstance(children, (list, tuple)):
            for index, child in enumerate(children):
                walk(child, f"{prefix}/{index}" if prefix else str(index), depth + 1)

    walk(window, "", 0)
    return mapping, bundle, window_frame(window)


def destructive_label(element, markers):
    label = " ".join(filter(None, [
        _text(_attr(element, kAXTitleAttribute), 200),
        _text(_attr(element, kAXDescriptionAttribute), 200),
        _text(_attr(element, kAXValueAttribute), 200) if isinstance(
            _attr(element, kAXValueAttribute), str) else "",
    ])).lower()
    return any(str(marker).lower() in label for marker in markers)


def command_act(command):
    if Quartz is None:
        return failure("COMPUTER_UNAVAILABLE", _IMPORT_ERROR)
    if not accessibility_ok():
        return failure("COMPUTER_PERMISSION_DENIED",
                       "Accessibility permission is required", permission="accessibility")
    started = int(time.monotonic() * 1000)
    pid, bundle, name = frontmost()
    apps = [str(item) for item in (command.get("apps") or [])]
    if apps and bundle not in apps:
        return failure("COMPUTER_WINDOW_NOT_ALLOWED",
                       f"frontmost app {bundle or name} is outside the session scope")
    action = str(command.get("action") or "").lower()
    mapping, bundle, frame = _element_map(command)
    if mapping is None:
        return failure("COMPUTER_WINDOW_NOT_ALLOWED", "no frontmost window")
    element = mapping.get(str(command.get("element_id") or ""))
    if action in ("click_element", "type") and element is None and command.get("element_id"):
        return failure("COMPUTER_ELEMENT_STALE", "element id is no longer present")
    if action in ("click_element", "click") and element is not None:
        if not command.get("allow_destructive") and destructive_label(
                element, command.get("destructive_markers") or []):
            return failure("COMPUTER_DESTRUCTIVE_BLOCKED",
                           "target looks destructive; request allow_destructive=true")
    before_path = str(command.get("before_path") or "")
    after_path = str(command.get("after_path") or "")
    window_num, _ = window_id_for(pid, frame)
    width = height = 0

    def capture(path):
        nonlocal width, height
        if not path or not window_num:
            return
        subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(window_num), path],
                       check=True, capture_output=True, timeout=10)
        width, height = png_size(path)

    try:
        capture(before_path)
        if action in ("click_element", "click") and element is not None:
            AXUIElementPerformAction(element, kAXPressAction)
        elif action in ("click_xy", "click"):
            x = int(command.get("x") or 0)
            y = int(command.get("y") or 0)
            point = (x, y)
            down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, 0)
            up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        elif action in ("type", "fill"):
            text = str(command.get("text") or "")
            if element is not None and _text(_attr(element, kAXRoleAttribute), 80) == "AXSecureTextField":
                return failure("COMPUTER_SECURE_FIELD", "secure text fields are not writable")
            if element is not None:
                value = _attr(element, kAXValueAttribute)
                AXUIElementSetAttributeValue(element, kAXValueAttribute, text)
            else:
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
                up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
                Quartz.CGEventKeyboardSetUnicodeString(up, len(text), text)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        elif action == "press":
            keymap = {"return": 36, "enter": 36, "tab": 48, "space": 49,
                      "escape": 53, "delete": 51, "left": 123, "right": 124,
                      "down": 125, "up": 126}
            code = keymap.get(str(command.get("key") or "").lower())
            if code is None:
                return failure("COMPUTER_ACTION_INVALID", "unsupported key")
            down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
            up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        elif action == "scroll":
            delta = int(command.get("delta_y") or 0)
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitPixel, 1, delta)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        else:
            return failure("COMPUTER_ACTION_INVALID", "unsupported action")
        time.sleep(0.25)
        capture(after_path)
    except subprocess.CalledProcessError as error:
        return failure("COMPUTER_CAPTURE_FAILED", error.__class__.__name__)
    except Exception as error:  # noqa: BLE001
        return failure("COMPUTER_FAILED", error.__class__.__name__)
    return {
        "ok": True,
        "action": action,
        "target": _text(_attr(element, kAXTitleAttribute), 200) if element is not None else "",
        "width": width,
        "height": height,
        "duration_ms": int(time.monotonic() * 1000) - started,
    }


def command_release(command):
    if Quartz is None:
        return {"ok": True, "released": False}
    for code in (55, 54, 56, 58, 59, 62):  # cmd/alt/ctrl/shift variants
        try:
            up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "released": True}


def handle(command):
    name = str(command.get("command") or "").lower()
    if name == "ping":
        if Quartz is None:
            return failure("COMPUTER_UNAVAILABLE", _IMPORT_ERROR)
        return {"ok": True, "platform": "macos",
                "accessibility": accessibility_ok(),
                "screen_recording": screen_recording_ok()}
    if name == "look":
        return command_look(command)
    if name == "act":
        return command_act(command)
    if name == "release":
        return command_release(command)
    return failure("COMPUTER_COMMAND_INVALID", "unknown command")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError:
            print(json.dumps({"id": None, **failure("COMPUTER_BAD_REQUEST", "invalid JSON")}),
                  flush=True)
            continue
        request_id = command.get("id")
        try:
            result = handle(command)
        except Exception as error:  # noqa: BLE001
            result = failure("COMPUTER_FAILED", error.__class__.__name__)
        print(json.dumps({"id": request_id, **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
