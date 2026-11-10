"""Loopback browser inspection: navigate, DOM/text/elements, screenshot.

The browser itself runs in a small Node/Playwright sidecar. Playwright is an
optional viewer dev dependency: when it is absent this tool returns
``available: false`` instead of failing the run. The URL policy is host-owned
and deliberately narrow — loopback ``http(s)`` only. That makes the tool useful
for a local preview without turning it into an SSRF or public-web egress path.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import tempfile
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
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.artifact_root = Path(artifact_root) if artifact_root else browser_artifact_dir()
        self._node = node
        self._sidecar = sidecar
        self.timeout = float(timeout)

    def sidecar_path(self) -> Path:
        if self._sidecar:
            return Path(self._sidecar)
        return Path(__file__).resolve().parents[4] / "viewer" / "browser_sidecar.mjs"

    def availability(self) -> tuple[bool, str]:
        node = self._node or shutil.which("node")
        if not node:
            return False, "node is not installed"
        sidecar = self.sidecar_path()
        if not sidecar.is_file():
            return False, f"browser sidecar not found: {sidecar}"
        if not (sidecar.parent / "node_modules" / "playwright").is_dir():
            return False, "Playwright is not installed in viewer/node_modules"
        return True, ""

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
                "screenshot": None, "dom": None, "truncated": False, "duration_ms": 0,
            }

        directory = self._artifact_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        token = secrets.token_hex(4)
        image = directory / f"inspect-{stamp}-{token}.png"
        dom = directory / f"inspect-{stamp}-{token}.html"
        payload = {
            "url": safe_url,
            "full_page": bool(full_page),
            "wait_ms": max(0, min(int(wait_ms), MAX_WAIT_MS)),
            "max_text": max(100, min(int(max_text), MAX_TEXT_CHARS)),
            "max_elements": max(1, min(int(max_elements), MAX_ELEMENTS)),
            "screenshot_path": str(image),
            "dom_path": str(dom),
        }
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
            if key in os.environ
        }
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [self._node or shutil.which("node") or "node", str(self.sidecar_path())],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                cwd=str(self.sidecar_path().parent),
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "available": True, "ok": False, "url": safe_url,
                "reason": f"browser timed out after {self.timeout:.0f}s",
                "error": {"code": "BROWSER_TIMEOUT", "message": "browser sidecar timed out"},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except OSError as error:
            return {
                "available": False, "ok": False, "url": safe_url,
                "reason": str(error), "error": {"code": "BROWSER_UNAVAILABLE", "message": str(error)},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        output = completed.stdout.strip()
        try:
            data = json.loads(output) if output else {}
        except json.JSONDecodeError:
            data = {}
        if not data:
            message = (completed.stderr or "browser sidecar returned no JSON").strip()[:500]
            return {
                "available": True, "ok": False, "url": safe_url, "reason": message,
                "error": {"code": "BROWSER_BAD_RESPONSE", "message": message},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        if not data.get("ok"):
            error = data.get("error") or {"code": "BROWSER_PAGE_FAILED", "message": "page failed"}
            return {
                "available": True, "ok": False, "url": safe_url,
                "reason": str(error.get("message") or ""),
                "error": {"code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                          "message": str(error.get("message") or "page failed")},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int(data.get("duration_ms") or (time.perf_counter() - started) * 1000),
            }

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
        url: str,
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
        safe_url = validate_browser_url(url)
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
                "truncated": False, "duration_ms": 0,
            }

        directory = self._artifact_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        token = secrets.token_hex(4)
        before = directory / f"{verb}-{stamp}-{token}-before.png"
        after = directory / f"{verb}-{stamp}-{token}-after.png"
        dom = directory / f"{verb}-{stamp}-{token}.html"
        payload = {
            "url": safe_url,
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
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
            if key in os.environ
        }
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [self._node or shutil.which("node") or "node", str(self.sidecar_path())],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                cwd=str(self.sidecar_path().parent),
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "available": True, "ok": False, "url": safe_url,
                "action": verb, "selector": target,
                "reason": f"browser timed out after {self.timeout:.0f}s",
                "error": {"code": "BROWSER_TIMEOUT", "message": "browser sidecar timed out"},
                "before": self._artifact(before, 0, 0), "after": None, "dom": None,
                "title": "", "text": "", "elements": [], "console_errors": [],
                "truncated": False, "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except OSError as error:
            return {
                "available": False, "ok": False, "url": safe_url,
                "action": verb, "selector": target, "reason": str(error),
                "error": {"code": "BROWSER_UNAVAILABLE", "message": str(error)},
                "before": None, "after": None, "dom": None,
                "title": "", "text": "", "elements": [], "console_errors": [],
                "truncated": False, "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        try:
            data = json.loads(completed.stdout.strip() or "{}")
        except json.JSONDecodeError:
            data = {}
        if not data:
            message = (completed.stderr or "browser sidecar returned no JSON").strip()[:500]
            return {
                "available": True, "ok": False, "url": safe_url,
                "action": verb, "selector": target, "reason": message,
                "error": {"code": "BROWSER_BAD_RESPONSE", "message": message},
                "before": self._artifact(before, 0, 0), "after": None, "dom": None,
                "title": "", "text": "", "elements": [], "console_errors": [],
                "truncated": False, "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        if not data.get("ok"):
            error = data.get("error") or {"code": "BROWSER_PAGE_FAILED", "message": "page failed"}
            return {
                "available": True, "ok": False, "url": safe_url,
                "action": verb, "selector": target,
                "reason": str(error.get("message") or ""),
                "error": {"code": str(error.get("code") or "BROWSER_PAGE_FAILED"),
                          "message": str(error.get("message") or "page failed")},
                "before": self._artifact(before, 0, 0),
                "after": self._artifact(after, 0, 0),
                "dom": None,
                "title": "", "text": "", "elements": [], "console_errors": [],
                "truncated": False, "duration_ms": int(data.get("duration_ms") or (time.perf_counter() - started) * 1000),
            }
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
            "truncated": len(text) >= 12_000,
            "duration_ms": int(data.get("duration_ms") or (time.perf_counter() - started) * 1000),
        }

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