"""Loopback browser inspection: navigate, DOM/text/elements, screenshot.

The browser itself runs in a small Node/Playwright sidecar. Playwright is an
optional viewer dev dependency: when it is absent this tool returns
``available: false`` instead of failing the run. The URL policy is host-owned
and deliberately narrow — loopback ``http(s)`` only. That makes the tool useful
for a local preview without turning it into an SSRF or public-web egress path.
"""

from __future__ import annotations

import difflib
import hashlib
import ipaddress
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ...core.contracts import AgentError

__all__ = ["BrowserRunner", "browser_artifact_dir", "validate_browser_url"]

MAX_URL_CHARS = 2_000
MAX_TEXT_CHARS = 40_000
MAX_ELEMENTS = 200
MAX_WAIT_MS = 5_000


def browser_artifact_dir() -> Path:
    override = os.environ.get("FCODE_BROWSER_DIR", "").strip()
    return Path(override) if override else Path(tempfile.gettempdir()) / "fcode-browser"


def _loopback(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_browser_url(url: str) -> str:
    value = str(url or "").strip()
    if not value or len(value) > MAX_URL_CHARS:
        raise AgentError("BROWSER_URL_INVALID", "url must be a non-empty loopback URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise AgentError("BROWSER_URL_DENIED", "only http(s) loopback URLs are allowed")
    if parsed.username or parsed.password:
        raise AgentError("BROWSER_URL_DENIED", "URLs with credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as error:
        raise AgentError("BROWSER_URL_INVALID", "url has an invalid port") from error
    if not parsed.hostname or not _loopback(parsed.hostname):
        raise AgentError(
            "BROWSER_URL_DENIED",
            "browser_inspect only opens 127.0.0.1 / localhost / ::1 previews",
        )
    if port is not None and not 1 <= port <= 65535:
        raise AgentError("BROWSER_URL_INVALID", "url port is out of range")
    return value


class BrowserRunner:
    """One workspace's browser sidecar. Lazy: no process until inspect()."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        artifact_root: Path | None = None,
        node: str | None = None,
        sidecar: Path | None = None,
        timeout: float = 30.0,
        persistent: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.artifact_root = Path(artifact_root) if artifact_root else browser_artifact_dir()
        self._node = node
        self._sidecar = sidecar
        self.timeout = float(timeout)
        self._persistent = bool(persistent)
        self._session = None
        self._session_lock = threading.RLock()
        self._session_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._session_stderr: list[str] = []
        self._session_seq = 0

    def sidecar_path(self) -> Path:
        if self._sidecar:
            return Path(self._sidecar)
        return Path(__file__).resolve().parents[4] / "viewer" / "browser_sidecar.mjs"

    def session_path(self) -> Path:
        return Path(__file__).resolve().parents[4] / "viewer" / "browser_session.mjs"

    def availability(self) -> tuple[bool, str]:
        node = self._node or shutil.which("node")
        if not node:
            return False, "node is not installed"
        sidecar = self.session_path() if self._persistent else self.sidecar_path()
        if not sidecar.is_file():
            return False, f"browser sidecar not found: {sidecar}"
        if not (sidecar.parent / "node_modules" / "playwright").is_dir():
            return False, "Playwright is not installed in viewer/node_modules"
        return True, ""

    def _env(self) -> dict[str, str]:
        return {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
            if key in os.environ
        }

    def _start_session(self) -> None:
        path = self.session_path()
        self._session_stderr = []
        self._session = subprocess.Popen(
            [self._node or shutil.which("node") or "node", str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(path.parent),
            env=self._env(),
        )
        self._session_queue = queue.Queue()
        threading.Thread(
            target=self._read_session, args=(self._session,), daemon=True,
            name=f"fcode-browser-{id(self)}",
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(self._session,), daemon=True,
            name=f"fcode-browser-err-{id(self)}",
        ).start()

    def _read_session(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout or []:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._session_queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self._session_queue.put({"ok": False, "error": {
                "code": "BROWSER_SESSION_CLOSED",
                "message": "browser session closed",
            }})

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr or []:
                self._session_stderr.append(str(line).strip()[:500])
                if len(self._session_stderr) > 20:
                    self._session_stderr.pop(0)
        except Exception:  # noqa: BLE001 - stderr is diagnostic only
            pass

    def _stop_session(self) -> None:
        process = self._session
        self._session = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            try:
                process.kill()
            except OSError:
                pass

    def close(self) -> None:
        with self._session_lock:
            self._stop_session()

    def _session_call(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with self._session_lock:
            if self._session is None or self._session.poll() is not None:
                try:
                    self._start_session()
                except OSError as error:
                    return None, {"code": "BROWSER_UNAVAILABLE", "message": str(error)}
            assert self._session is not None
            self._session_seq += 1
            request_id = self._session_seq
            request = dict(payload, id=request_id)
            try:
                if self._session.stdin is None:
                    raise OSError("browser session has no stdin")
                self._session.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._session.stdin.flush()
            except OSError as error:
                self._stop_session()
                return None, {"code": "BROWSER_SESSION_CLOSED", "message": str(error)}
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    self._stop_session()
                    return None, {"code": "BROWSER_TIMEOUT", "message": "browser session timed out"}
                try:
                    response = self._session_queue.get(timeout=remaining)
                except queue.Empty:
                    self._stop_session()
                    return None, {"code": "BROWSER_TIMEOUT", "message": "browser session timed out"}
                if response.get("id") not in (None, request_id):
                    continue
                if not response.get("ok"):
                    error = response.get("error") or {
                        "code": "BROWSER_PAGE_FAILED", "message": "browser command failed",
                    }
                    return None, {
                        "code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                        "message": str(error.get("message") or "browser command failed"),
                    }
                response.pop("id", None)
                return response, None

    def _run_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self._persistent:
            return self._session_call(payload)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [self._node or shutil.which("node") or "node", str(self.sidecar_path())],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                cwd=str(self.sidecar_path().parent),
                env=self._env(),
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return None, {"code": "BROWSER_TIMEOUT", "message": "browser sidecar timed out"}
        except OSError as error:
            return None, {"code": "BROWSER_UNAVAILABLE", "message": str(error)}
        output = completed.stdout.strip()
        try:
            data = json.loads(output) if output else {}
        except json.JSONDecodeError:
            data = {}
        if not data:
            message = (completed.stderr or "browser sidecar returned no JSON").strip()[:500]
            return None, {"code": "BROWSER_BAD_RESPONSE", "message": message}
        if not data.get("ok"):
            error = data.get("error") or {"code": "BROWSER_PAGE_FAILED", "message": "page failed"}
            return None, {
                "code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                "message": str(error.get("message") or "page failed"),
            }
        return data, None

    def _artifact_dir(self) -> Path:
        key = hashlib.sha256(str(self.workspace_root.resolve()).encode("utf-8")).hexdigest()[:12]
        target = self.artifact_root / key
        target.mkdir(parents=True, exist_ok=True)
        return target

    def inspect(
        self,
        url: str,
        *,
        full_page: bool = False,
        wait_ms: int = 300,
        max_text: int = 12_000,
        max_elements: int = MAX_ELEMENTS,
    ) -> dict[str, Any]:
        safe_url = validate_browser_url(url)
        available, reason = self.availability()
        if not available:
            return {
                "available": False, "ok": False, "url": safe_url,
                "reason": reason, "error": {"code": "BROWSER_UNAVAILABLE", "message": reason},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "dom_diff": "", "diff": None, "truncated": False, "duration_ms": 0,
            }

        directory = self._artifact_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        token = secrets.token_hex(4)
        image = directory / f"inspect-{stamp}-{token}.png"
        dom = directory / f"inspect-{stamp}-{token}.html"
        payload = {
            "command": "inspect",
            "url": safe_url,
            "full_page": bool(full_page),
            "wait_ms": max(0, min(int(wait_ms), MAX_WAIT_MS)),
            "max_text": max(100, min(int(max_text), MAX_TEXT_CHARS)),
            "max_elements": max(1, min(int(max_elements), MAX_ELEMENTS)),
            "screenshot_path": str(image),
            "dom_path": str(dom),
        }
        started = time.perf_counter()
        data, error = self._run_payload(payload)
        if error is not None:
            return {
                "available": error.get("code") != "BROWSER_UNAVAILABLE",
                "ok": False, "url": safe_url,
                "reason": str(error.get("message") or ""),
                "error": {"code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                          "message": str(error.get("message") or "page failed")},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        assert data is not None

        screenshot = None
        if image.is_file():
            screenshot = {
                "path": image.relative_to(self.artifact_root).as_posix(),
                "url": "/api/browser/artifact?path=" + quote(image.relative_to(self.artifact_root).as_posix()),
                "bytes": image.stat().st_size,
                "width": int((data.get("document") or {}).get("width") or 0),
                "height": int((data.get("document") or {}).get("height") or 0),
                "full_page": bool(full_page),
            }
        dom_file = None
        if dom.is_file():
            dom_file = {
                "path": dom.relative_to(self.artifact_root).as_posix(),
                "bytes": dom.stat().st_size,
            }
        text = str(data.get("text") or "")
        return {
            "available": True,
            "ok": True,
            "url": str(data.get("url") or safe_url),
            "title": str(data.get("title") or ""),
            "text": text,
            "elements": list(data.get("elements") or []),
            "console_errors": list(data.get("console_errors") or []),
            "screenshot": screenshot,
            "dom": dom_file,
            "truncated": len(text) >= int(payload["max_text"]),
            "duration_ms": int(data.get("duration_ms") or (time.perf_counter() - started) * 1000),
        }

    def act(
        self,
        url: str = "",
        *,
        action: str,
        selector: str,
        text: str = "",
        key: str = "Enter",
        full_page: bool = False,
        wait_ms: int = 300,
    ) -> dict[str, Any]:
        """Perform one approved click/fill/press on a loopback preview.

        The sidecar takes a screenshot before and after the action. A missing
        selector still leaves the before screenshot as evidence.
        """
        safe_url = validate_browser_url(url) if str(url or "").strip() else ""
        verb = str(action or "").strip().lower()
        if verb not in ("click", "fill", "press"):
            raise AgentError("BROWSER_ACTION_INVALID", "action must be click, fill or press")
        target = str(selector or "").strip()
        if not target or len(target) > 300:
            raise AgentError("BROWSER_SELECTOR_INVALID", "selector must be 1–300 characters")
        value = str(text or "")
        if len(value) > 4_000:
            raise AgentError("BROWSER_TEXT_INVALID", "fill text is limited to 4,000 characters")
        key_name = str(key or "Enter").strip()
        if not key_name or len(key_name) > 40:
            raise AgentError("BROWSER_KEY_INVALID", "key must be 1–40 characters")

        available, reason = self.availability()
        if not available:
            return {
                "available": False, "ok": False, "url": safe_url,
                "action": verb, "selector": target, "reason": reason,
                "error": {"code": "BROWSER_UNAVAILABLE", "message": reason},
                "before": None, "after": None, "dom": None,
                "title": "", "text": "", "elements": [], "console_errors": [],
                "dom_diff": "", "diff": None, "truncated": False, "duration_ms": 0,
            }

        directory = self._artifact_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        token = secrets.token_hex(4)
        before = directory / f"{verb}-{stamp}-{token}-before.png"
        after = directory / f"{verb}-{stamp}-{token}-after.png"
        dom = directory / f"{verb}-{stamp}-{token}.html"
        payload = {
            "command": verb,
            "action": verb,
            "selector": target,
            "text": value,
            "key": key_name,
            "full_page": bool(full_page),
            "wait_ms": max(0, min(int(wait_ms), MAX_WAIT_MS)),
            "before_path": str(before),
            "after_path": str(after),
            "dom_path": str(dom),
        }
        if safe_url:
            payload["url"] = safe_url
        started = time.perf_counter()
        data, error = self._run_payload(payload)
        if error is not None:
            return {
                "available": error.get("code") != "BROWSER_UNAVAILABLE",
                "ok": False, "url": safe_url,
                "action": verb, "selector": target,
                "reason": str(error.get("message") or ""),
                "error": {"code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                          "message": str(error.get("message") or "page failed")},
                "before": self._artifact(before, 0, 0),
                "after": self._artifact(after, 0, 0),
                "dom": self._artifact(dom, 0, 0, image=False),
                "title": "", "text": "", "elements": [], "console_errors": [],
                "dom_diff": "", "diff": None, "truncated": False, "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        assert data is not None
        width = int((data.get("document") or {}).get("width") or 0)
        height = int((data.get("document") or {}).get("height") or 0)
        text = str(data.get("text") or "")
        return {
            "available": True,
            "ok": True,
            "url": str(data.get("url") or safe_url),
            "action": verb,
            "selector": target,
            "before": self._artifact(before, width, height),
            "after": self._artifact(after, width, height),
            "dom": self._artifact(dom, 0, 0, image=False),
            "title": str(data.get("title") or ""),
            "text": text,
            "elements": list(data.get("elements") or []),
            "console_errors": list(data.get("console_errors") or []),
            "dom_diff": self._text_diff(data.get("before_text"), data.get("after_text")),
            "diff": self._image_diff(before, after),
            "truncated": len(text) >= 12_000,
            "duration_ms": int(data.get("duration_ms") or (time.perf_counter() - started) * 1000),
        }

    @staticmethod
    def _text_diff(before: Any, after: Any, limit: int = 4_000) -> str:
        left = str(before or "").splitlines()
        right = str(after or "").splitlines()
        if left == right:
            return ""
        diff = "\n".join(difflib.unified_diff(left, right, fromfile="before", tofile="after", lineterm=""))
        return diff[:limit]

    @staticmethod
    def _image_diff(before: Path, after: Path) -> dict[str, Any] | None:
        if not before.is_file() or not after.is_file():
            return None
        before_hash = hashlib.sha256(before.read_bytes()).hexdigest()
        after_hash = hashlib.sha256(after.read_bytes()).hexdigest()
        result: dict[str, Any] = {
            "changed": before_hash != after_hash,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "size_changed": False,
            "changed_pixels": None,
            "ratio": None,
        }
        try:
            from PIL import Image, ImageChops  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 — Pillow is optional
            return result
        try:
            left = Image.open(before).convert("RGB")
            right = Image.open(after).convert("RGB")
            if left.size != right.size:
                result["size_changed"] = True
                return result
            difference = ImageChops.difference(left, right).convert("L")
            histogram = difference.histogram()
            changed = sum(histogram[1:])
            total = left.size[0] * left.size[1]
            result["changed_pixels"] = changed
            result["ratio"] = round(changed / total, 6) if total else None
            return result
        except Exception:  # noqa: BLE001 — pixel comparison is evidence, not a gate
            return result

    def _artifact(self, path: Path, width: int, height: int, *, image: bool = True) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        relative = path.relative_to(self.artifact_root).as_posix()
        data = {
            "path": relative,
            "url": "/api/browser/artifact?path=" + quote(relative),
            "bytes": path.stat().st_size,
        }
        if image:
            data.update({"width": width, "height": height})
        return data