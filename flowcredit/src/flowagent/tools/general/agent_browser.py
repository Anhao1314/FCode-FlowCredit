"""agent-browser backend: the same browser tools, driven by Vercel's CLI.

`vercel-labs/agent-browser` is a Rust CLI built for agents: pages are read as
compact accessibility-tree text with element refs (``@e1``), actions take a ref,
and a per-session daemon keeps the browser alive between short command calls.
This runner speaks the exact :class:`BrowserRunner` result contract, so the
``browser_inspect``/``browser_act`` tools, their summaries and the evidence
frontend all keep working unchanged.

Security stays host-owned: URLs pass through the same loopback validation as
the Playwright path, and the CLI is only ever invoked with a per-workspace
session name. Opt in with ``FCODE_BROWSER_BACKEND=agent-browser``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from .browser import BrowserRunner, MAX_TEXT_CHARS, validate_browser_url

__all__ = ["AgentBrowserRunner", "agent_browser_bin", "parse_snapshot"]

MAX_SNAPSHOT_CHARS = 24_000
MAX_ERRORS = 20
MAX_ACTION_TEXT = 4_000
_SNAPSHOT_LINE = re.compile(
    r"^\s*-\s+(?P<role>[A-Za-z_ ]+?)\s+(?:\"(?P<name>[^\"]*)\")?\s*(?:\[(?P<meta>[^\]]*)\])?\s*$"
)


def agent_browser_bin() -> str:
    override = os.environ.get("FCODE_AGENT_BROWSER_BIN", "").strip()
    return override or "agent-browser"


def parse_snapshot(text: str, *, max_elements: int = 200) -> list[dict[str, Any]]:
    """Parse ``snapshot -i`` lines into the evidence ``elements`` shape."""
    elements: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if not match:
            continue
        meta = match.group("meta") or ""
        ref_match = re.search(r"ref=(?P<ref>\w+)", meta)
        if not ref_match:
            continue
        entry: dict[str, Any] = {
            "role": match.group("role").strip().lower(),
            "name": match.group("name") or "",
            "ref": ref_match.group("ref"),
        }
        checked = re.search(r"checked=(\w+)", meta)
        if checked:
            entry["checked"] = checked.group(1) == "true"
        level = re.search(r"level=(\d+)", meta)
        if level:
            entry["level"] = int(level.group(1))
        elements.append(entry)
        if len(elements) >= max_elements:
            break
    return elements


class AgentBrowserRunner(BrowserRunner):
    """BrowserRunner contract on top of the agent-browser CLI."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        artifact_root: Path | None = None,
        bin: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(workspace_root, artifact_root=artifact_root, timeout=timeout)
        self._bin = bin or agent_browser_bin()
        digest = hashlib.sha256(str(Path(workspace_root).resolve()).encode("utf-8")).hexdigest()[:12]
        self._session = f"fcode-{digest}"

    # ------------------------------------------------------------------ cli

    def availability(self) -> tuple[bool, str]:
        if not shutil.which(self._bin):
            return False, (
                "agent-browser CLI is not installed; "
                "install it with `npm install -g agent-browser`"
            )
        return True, ""

    def _cli(self, args: list[str], *, timeout: float | None = None) -> str:
        completed = subprocess.run(
            [self._bin, "--session", self._session, *args],
            text=True,
            capture_output=True,
            timeout=timeout or self.timeout,
            env=self._env(),
            shell=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "agent-browser command failed").strip()
            raise AgentError("BROWSER_PAGE_FAILED", message[:500])
        return completed.stdout

    # ------------------------------------------------------------- contract

    def close(self) -> None:
        try:
            self._cli(["close"], timeout=10)
        except (AgentError, OSError, subprocess.TimeoutExpired):
            pass  # daemon already gone is fine; close is best-effort

    def _console_errors(self) -> list[str]:
        try:
            output = self._cli(["errors"])
        except (AgentError, OSError, subprocess.TimeoutExpired):
            return []
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith("✓")]
        return [line[:300] for line in lines[:MAX_ERRORS]]

    def _screenshot(self, path: Path, *, full_page: bool) -> dict[str, Any] | None:
        args = ["screenshot", str(path)]
        if full_page:
            args.append("--full-page")
        self._cli(args)
        return self._artifact(path, 0, 0)

    def _current_url(self) -> str:
        try:
            return self._cli(["get", "url"]).strip()
        except (AgentError, subprocess.TimeoutExpired):
            return ""

    def _snapshot_text(self) -> str:
        """Accessibility snapshot with a one-shot retry.

        A snapshot taken while the document is being replaced (right after
        ``open``) can fail or come back empty — the CLI's own guidance is to
        run it again. Silent emptiness here poisons every ref that follows.
        """
        for attempt in (0, 1):
            try:
                text = self._cli(["snapshot", "-i"])
            except (AgentError, subprocess.TimeoutExpired):
                text = ""
            if text.strip():
                return text[:MAX_SNAPSHOT_CHARS]
            if attempt == 0:
                time.sleep(0.3)
        return ""

    def inspect(
        self,
        url: str,
        *,
        full_page: bool = False,
        wait_ms: int = 300,
        max_text: int = 12_000,
        max_elements: int = 200,
    ) -> dict[str, Any]:
        safe_url = validate_browser_url(url)
        available, reason = self.availability()
        if not available:
            return {
                "available": False, "ok": False, "url": safe_url,
                "reason": reason, "error": {"code": "BROWSER_UNAVAILABLE", "message": reason},
                "title": "", "text": "", "elements": [], "console_errors": [],
                "screenshot": None, "dom": None, "dom_diff": "", "diff": None,
                "truncated": False, "duration_ms": 0,
            }
        started = time.perf_counter()
        directory = self._artifact_dir()
        image = directory / f"ab-inspect-{int(time.time() * 1000)}.png"
        try:
            # 同页复访不重开：保住会话状态，也让已持有的 refs 保持新鲜。
            if self._current_url() != safe_url:
                self._cli(["open", safe_url])
            snapshot = self._snapshot_text()
            title = ""
            try:
                title = self._cli(["get", "title"]).strip()
            except (AgentError, subprocess.TimeoutExpired):
                title = ""
            current_url = self._cli(["get", "url"]).strip() or safe_url
            shot = None
            try:
                shot = self._screenshot(image, full_page=full_page)
            except (AgentError, subprocess.TimeoutExpired):
                shot = None
        except AgentError as error:
            return {
                "available": True, "ok": False, "url": safe_url,
                "reason": error.message,
                "error": {"code": "BROWSER_PAGE_FAILED", "message": error.message},
                "title": "", "text": "", "elements": [], "console_errors": self._console_errors(),
                "screenshot": None, "dom": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        text = snapshot[: min(max_text, MAX_TEXT_CHARS)]
        if wait_ms:
            time.sleep(min(int(wait_ms), 5_000) / 1_000)
        return {
            "available": True,
            "ok": True,
            "url": current_url,
            "title": title,
            "text": text,
            "elements": parse_snapshot(snapshot, max_elements=max_elements),
            "console_errors": self._console_errors(),
            "screenshot": shot,
            "dom": None,
            "truncated": len(snapshot) > len(text),
            "duration_ms": int((time.perf_counter() - started) * 1000),
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
        safe_url = validate_browser_url(url) if str(url or "").strip() else ""
        verb = str(action or "").strip().lower()
        if verb not in ("click", "fill", "press"):
            raise AgentError("BROWSER_ACTION_INVALID", "action must be click, fill or press")
        target = str(selector or "").strip()
        if not target or len(target) > 300:
            raise AgentError("BROWSER_SELECTOR_INVALID", "selector must be 1–300 characters")
        if verb == "fill" and len(str(text or "")) > MAX_ACTION_TEXT:
            raise AgentError("BROWSER_TEXT_INVALID", "fill text is limited to 4,000 characters")

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

        started = time.perf_counter()
        directory = self._artifact_dir()
        stamp = int(time.time() * 1000)
        before = directory / f"ab-{verb}-{stamp}-before.png"
        after = directory / f"ab-{verb}-{stamp}-after.png"

        try:
            # 同页复访不重开：refs 来自模型上一次的快照，重开反而把文档换掉。
            if safe_url and self._current_url() != safe_url:
                self._cli(["open", safe_url])
            before_text = self._snapshot_text()
            before_shot = None
            try:
                before_shot = self._screenshot(before, full_page=full_page)
            except (AgentError, subprocess.TimeoutExpired):
                before_shot = None
            try:
                if verb == "click":
                    self._cli(["click", target])
                elif verb == "fill":
                    self._cli(["fill", target, str(text or "")])
                else:
                    self._cli(["press", str(key or "Enter")])
            except AgentError as error:
                # 引用过期（文档已换）：重拍快照后按同一引用重试一次。
                if "not found" in error.message.lower() and target.startswith("@"):
                    self._snapshot_text()
                    if verb == "click":
                        self._cli(["click", target])
                    elif verb == "fill":
                        self._cli(["fill", target, str(text or "")])
                    else:
                        self._cli(["press", str(key or "Enter")])
                else:
                    raise
            if wait_ms:
                time.sleep(min(int(wait_ms), 5_000) / 1_000)
            after_text = self._snapshot_text()
            after_shot = None
            try:
                after_shot = self._screenshot(after, full_page=full_page)
            except (AgentError, subprocess.TimeoutExpired):
                after_shot = None
            title = ""
            try:
                title = self._cli(["get", "title"]).strip()
            except (AgentError, subprocess.TimeoutExpired):
                title = ""
            current_url = self._cli(["get", "url"]).strip() or safe_url
        except AgentError as error:
            return {
                "available": True, "ok": False, "url": safe_url,
                "action": verb, "selector": target,
                "reason": error.message,
                "error": {"code": "BROWSER_PAGE_FAILED", "message": error.message},
                "before": self._artifact(before, 0, 0),
                "after": self._artifact(after, 0, 0),
                "dom": None, "title": "", "text": "", "elements": [],
                "console_errors": self._console_errors(),
                "dom_diff": "", "diff": None, "truncated": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        return {
            "available": True,
            "ok": True,
            "url": current_url,
            "action": verb,
            "selector": target,
            "before": before_shot or self._artifact(before, 0, 0),
            "after": after_shot or self._artifact(after, 0, 0),
            "dom": None,
            "title": title,
            "text": after_text[: min(12_000, MAX_TEXT_CHARS)],
            "elements": parse_snapshot(after_text),
            "console_errors": self._console_errors(),
            "dom_diff": self._text_diff(before_text, after_text),
            "diff": self._image_diff(before, after),
            "truncated": len(after_text) >= MAX_SNAPSHOT_CHARS,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
