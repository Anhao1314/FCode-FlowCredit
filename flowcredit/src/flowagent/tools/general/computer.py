"""Computer use: session-scoped desktop and external-browser control.

This module is the host-owned policy and transport layer. It deliberately
reuses the browser tool's shape: a small, strict Python policy in front of an
optional sidecar process that only executes and returns structured evidence.

Nothing here is model-authorised:

* tools are registered only when the run explicitly enables computer use;
* ``computer_request`` is an approval tool, so a session exists only after the
  operator approves its scope;
* the model can never widen or renew a session;
* every action is counted, bounded by a deadline and written to the chain as a
  normal ``tool_succeeded`` observation with artifact references (no pixels).

Desktop control lives in ``viewer/computer_sidecar.py`` (pyobjc, optional).
External browsing lives in ``viewer/browser_external.mjs`` (Playwright,
optional). Missing dependencies degrade to ``available: false``; they never
fail a run by themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ...core.contracts import AgentError

__all__ = [
    "COMPUTER_TOOLS",
    "COMPUTER_USE_INSTRUCTION",
    "DESTRUCTIVE_MARKERS",
    "ComputerPolicy",
    "ComputerRuntime",
    "ComputerSessionManager",
    "build_computer_tools",
]

COMPUTER_TOOLS = (
    "computer_request",
    "computer_look",
    "computer_act",
    "computer_stop",
)

DESTRUCTIVE_MARKERS = (
    "delete", "remove", "erase", "uninstall", "pay", "purchase", "buy",
    "send", "publish", "submit", "confirm", "allow", "grant", "reset",
    "format", "shutdown", "restart", "empty trash", "move to trash",
    "删除", "移除", "抹掉", "卸载", "支付", "付款", "购买", "发送", "发布",
    "提交", "确认", "允许", "授权", "重置", "格式化", "关机", "重启", "清空",
)

COMPUTER_USE_INSTRUCTION = """\
【computer use】本 run 允许申请计算机使用，但默认没有任何权限。
先调用 computer_request 说明 mode（desktop/browser/both）、目标 App/域名、时长和动作上限；
宿主会把这次申请作为审批卡交给操作员，批准后才存在会话。会话不能被扩大或续期。
桌面操作优先用 computer_look 读取 AX 元素，再按 element_id 操作；AX 不可用时才用坐标，
坐标点击要求会话显式允许 destructive。外部浏览器只用会话域名白名单内的页面。
安全输入框、密码管理器、钥匙串、支付/删除/发送类目标会被宿主阻止；被阻止时不要绕过，
向操作员说明并请求一个更窄的会话。结束时调用 computer_stop，释放输入并关闭 sidecar。"""

MAX_APPS = 4
MAX_DOMAINS = 5
MAX_REASON = 500
MAX_SCREENSHOTS = 120
DEFAULT_TIMEOUT = 45.0


def _now() -> int:
    return int(time.time())


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_path() -> Path:
    override = os.environ.get("FCODE_COMPUTER_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".fcode" / "computer.json"


@dataclass(frozen=True)
class AppTarget:
    bundle_id: str
    name: str


@dataclass(frozen=True)
class ComputerPolicy:
    """Operator-owned allowlists. Missing config means "nothing allowed"."""

    allowed_apps: tuple[AppTarget, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    denied_apps: tuple[str, ...] = ()
    max_session_seconds: int = 900
    max_actions: int = 100
    config_path: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "ComputerPolicy":
        target = Path(path) if path else _config_path()
        data = _load_json(target)
        apps: list[AppTarget] = []
        for item in data.get("allowed_apps") or []:
            if isinstance(item, str):
                apps.append(AppTarget(bundle_id=item.strip(), name=item.strip()))
            elif isinstance(item, dict):
                bundle = str(item.get("bundle_id") or item.get("bundleId") or "").strip()
                if bundle:
                    apps.append(AppTarget(
                        bundle_id=bundle,
                        name=str(item.get("name") or bundle).strip(),
                    ))
        domains = []
        for item in data.get("allowed_domains") or []:
            value = str(item or "").strip().lower().lstrip(".")
            if value:
                domains.append(value)
        denied = tuple(
            str(item or "").strip().lower()
            for item in data.get("denied_apps") or []
            if str(item or "").strip()
        )
        return cls(
            allowed_apps=tuple(app for app in apps if app.bundle_id),
            allowed_domains=tuple(dict.fromkeys(domains)),
            denied_apps=denied,
            max_session_seconds=max(30, min(int(data.get("max_session_seconds") or 900), 900)),
            max_actions=max(1, min(int(data.get("max_actions") or 100), 100)),
            config_path=str(target),
        )

    def app(self, value: str) -> AppTarget | None:
        needle = str(value or "").strip()
        if not needle:
            return None
        lower = needle.lower()
        if lower in self.denied_apps:
            return None
        for app in self.allowed_apps:
            if lower in (app.bundle_id.lower(), app.name.lower()):
                return app
        return None

    def domain_allowed(self, domain: str) -> bool:
        value = str(domain or "").strip().lower().lstrip(".")
        if not value:
            return False
        return any(value == allowed or value.endswith("." + allowed)
                   for allowed in self.allowed_domains)


@dataclass
class ComputerSession:
    session_id: str
    mode: str
    apps: tuple[AppTarget, ...]
    domains: tuple[str, ...]
    allow_destructive: bool
    started_at: int
    expires_at: int
    max_actions: int
    actions: int = 0
    looks: int = 0
    stopped: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "apps": [app.bundle_id for app in self.apps],
            "app_names": [app.name for app in self.apps],
            "domains": list(self.domains),
            "allow_destructive": self.allow_destructive,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "max_actions": self.max_actions,
            "actions": self.actions,
            "looks": self.looks,
        }


class ComputerSessionManager:
    """One run's session. Sessions never survive a restart."""

    def __init__(self, policy: ComputerPolicy, *, clock: Callable[[], int] = _now) -> None:
        self.policy = policy
        self._clock = clock
        self._session: ComputerSession | None = None

    @property
    def session(self) -> ComputerSession | None:
        current = self._session
        if current is None:
            return None
        if self._clock() >= current.expires_at or current.stopped:
            current.stopped = True
            return None
        return current

    def request(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.session is not None:
            raise AgentError("COMPUTER_SESSION_ACTIVE", "a computer session is already active")
        mode = str(arguments.get("mode") or "").strip().lower()
        if mode not in ("desktop", "browser", "both"):
            raise AgentError("COMPUTER_SCOPE_INVALID", "mode must be desktop, browser or both")
        wants_desktop = mode in ("desktop", "both")
        wants_browser = mode in ("browser", "both")
        raw_apps = arguments.get("apps") or []
        raw_domains = arguments.get("domains") or []
        if not isinstance(raw_apps, list) or not isinstance(raw_domains, list):
            raise AgentError("COMPUTER_SCOPE_INVALID", "apps and domains must be arrays")
        apps: list[AppTarget] = []
        for item in raw_apps[:MAX_APPS + 1]:
            app = self.policy.app(str(item))
            if app is None:
                raise AgentError(
                    "COMPUTER_APP_DENIED",
                    f"app {item!r} is not in the operator allowlist",
                )
            if app not in apps:
                apps.append(app)
        if len(apps) > MAX_APPS:
            raise AgentError("COMPUTER_SCOPE_INVALID", f"at most {MAX_APPS} apps per session")
        domains: list[str] = []
        for item in raw_domains[:MAX_DOMAINS + 1]:
            value = str(item or "").strip().lower().lstrip(".")
            if not self.policy.domain_allowed(value):
                raise AgentError(
                    "COMPUTER_DOMAIN_DENIED",
                    f"domain {item!r} is not in the operator allowlist",
                )
            if value not in domains:
                domains.append(value)
        if len(domains) > MAX_DOMAINS:
            raise AgentError("COMPUTER_SCOPE_INVALID", f"at most {MAX_DOMAINS} domains per session")
        if wants_desktop and not apps:
            raise AgentError("COMPUTER_SCOPE_INVALID", "desktop sessions need at least one app")
        if wants_browser and not domains:
            raise AgentError("COMPUTER_SCOPE_INVALID", "browser sessions need at least one domain")
        try:
            duration = int(arguments.get("duration_seconds") or 300)
            limit = int(arguments.get("max_actions") or 30)
        except (TypeError, ValueError) as error:
            raise AgentError("COMPUTER_SCOPE_INVALID", "duration and max_actions must be integers") from error
        duration = max(30, min(duration, self.policy.max_session_seconds))
        limit = max(1, min(limit, self.policy.max_actions))
        reason = _text(arguments.get("reason"), MAX_REASON)
        now = self._clock()
        session = ComputerSession(
            session_id="cs-" + secrets.token_hex(8),
            mode=mode,
            apps=tuple(apps),
            domains=tuple(domains),
            allow_destructive=bool(arguments.get("allow_destructive") or False),
            started_at=now,
            expires_at=now + duration,
            max_actions=limit,
        )
        self._session = session
        return {
            "available": True,
            "ok": True,
            "reason": reason,
            **session.snapshot(),
        }

    def current(self) -> ComputerSession:
        session = self.session
        if session is None:
            raise AgentError(
                "COMPUTER_SESSION_REQUIRED",
                "no active computer session; request one and wait for operator approval",
            )
        return session

    def require(self, needed: str) -> ComputerSession:
        session = self.current()
        if needed == "desktop" and session.mode not in ("desktop", "both"):
            raise AgentError("COMPUTER_SESSION_MODE", "this session does not allow desktop control")
        if needed == "browser" and session.mode not in ("browser", "both"):
            raise AgentError("COMPUTER_SESSION_MODE", "this session does not allow external browsing")
        return session

    def count_look(self) -> None:
        session = self.current()
        session.looks += 1
        if session.looks > MAX_SCREENSHOTS:
            raise AgentError(
                "COMPUTER_LOOK_LIMIT",
                f"look limit reached ({MAX_SCREENSHOTS}); stop the session",
            )

    def count_action(self) -> None:
        session = self.session
        if session is None:
            raise AgentError("COMPUTER_SESSION_REQUIRED", "no active computer session")
        session.actions += 1
        if session.actions > session.max_actions:
            raise AgentError("COMPUTER_ACTION_LIMIT", "session action limit reached")

    def stop(self, reason: str = "") -> dict[str, Any]:
        session = self._session
        self._session = None
        if session is None:
            return {"available": True, "ok": True, "stopped": False, "reason": _text(reason)}
        session.stopped = True
        return {
            "available": True,
            "ok": True,
            "stopped": True,
            "session_id": session.session_id,
            "actions": session.actions,
            "reason": _text(reason),
        }


class _SidecarProcess:
    """Persistent NDJSON sidecar: request ids, bounded wait, best-effort kill."""

    debug_name = "computer"

    def __init__(self, argv: list[str], *, cwd: Path | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.argv = argv
        self.cwd = cwd
        self.timeout = float(timeout)
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue[dict] = queue.Queue()
        self._lock = threading.RLock()
        self._seq = 0

    def _start(self) -> None:
        self._process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(self.cwd) if self.cwd else None,
            env={key: os.environ[key] for key in (
                "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG",
            ) if key in os.environ},
        )
        self._queue = queue.Queue()
        threading.Thread(target=self._read, args=(self._process,), daemon=True).start()
        threading.Thread(target=self._drain, args=(self._process,), daemon=True).start()

    def _read(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout or []:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self._queue.put({"ok": False, "error": {
                "code": "COMPUTER_UNAVAILABLE", "message": "sidecar closed",
            }})

    def _drain(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr or []:
                line = line.strip()
                if line:
                    print(f"[{self.debug_name}] {line[:300]}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass

    def _stop(self) -> None:
        process = self._process
        self._process = None
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
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            self._stop()

    def call(self, payload: Mapping[str, Any]) -> tuple[dict | None, dict | None]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                try:
                    self._start()
                except OSError as error:
                    return None, {"code": "COMPUTER_UNAVAILABLE", "message": str(error)}
            assert self._process is not None and self._process.stdin is not None
            self._seq += 1
            request_id = self._seq
            try:
                self._process.stdin.write(json.dumps(
                    {**payload, "id": request_id}, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except OSError as error:
                self._stop()
                return None, {"code": "COMPUTER_UNAVAILABLE", "message": str(error)}
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    return None, {"code": "COMPUTER_TIMEOUT", "message": "sidecar timed out"}
                try:
                    response = self._queue.get(timeout=remaining)
                except queue.Empty:
                    self._stop()
                    return None, {"code": "COMPUTER_TIMEOUT", "message": "sidecar timed out"}
                if response.get("id") not in (None, request_id):
                    continue
                if not response.get("ok"):
                    error = response.get("error") or {
                        "code": "COMPUTER_FAILED", "message": "sidecar command failed",
                    }
                    return None, {
                        "code": str(error.get("code") or "COMPUTER_FAILED"),
                        "message": str(error.get("message") or "sidecar command failed"),
                        **({"permission": error["permission"]} if error.get("permission") else {}),
                    }
                response.pop("id", None)
                return response, None


class DesktopBackend(_SidecarProcess):
    debug_name = "computer-desktop"

    def __init__(self, *, cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        script = cwd / "computer_sidecar.py"
        super().__init__([sys.executable, str(script)], cwd=cwd, timeout=timeout)
        self.script = script

    def availability(self) -> tuple[bool, str]:
        if not self.script.is_file():
            return False, f"desktop sidecar not found: {self.script}"
        data, error = self.call({"command": "ping"})
        if error is not None:
            return False, str(error.get("message") or error.get("code") or "unavailable")
        if not data or not data.get("ok"):
            return False, "desktop sidecar did not answer"
        if not data.get("accessibility"):
            return False, "Accessibility permission is not granted for this process"
        if not data.get("screen_recording"):
            return False, "Screen Recording permission is not granted for this process"
        return True, ""


class BrowserBackend(_SidecarProcess):
    debug_name = "computer-browser"

    def __init__(self, *, cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        script = cwd / "browser_external.mjs"
        node = shutil.which("node") or "node"
        super().__init__([node, str(script)], cwd=cwd, timeout=timeout)
        self.script = script

    def availability(self) -> tuple[bool, str]:
        if not self.script.is_file():
            return False, f"external browser sidecar not found: {self.script}"
        if not shutil.which("node"):
            return False, "node is not installed"
        if not (self.script.parent / "node_modules" / "playwright").is_dir():
            return False, "Playwright is not installed in viewer/node_modules"
        data, error = self.call({"command": "ping"})
        if error is not None:
            return False, str(error.get("message") or error.get("code") or "unavailable")
        return (True, "") if data and data.get("ok") else (False, "external browser did not answer")


class ComputerRuntime:
    """Policy + session + sidecars for one run."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        run_id: str = "",
        policy: ComputerPolicy | None = None,
        viewer_dir: Path | None = None,
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.policy = policy or ComputerPolicy.load()
        self.sessions = ComputerSessionManager(self.policy)
        base = Path(viewer_dir) if viewer_dir else Path(__file__).resolve().parents[4] / "viewer"
        base = base.resolve()
        self.desktop = DesktopBackend(cwd=base)
        self.browser = BrowserBackend(cwd=base)
        self._closed = False

    def close(self) -> None:
        self._closed = True
        try:
            self.desktop.call({"command": "release"})
        except Exception:  # noqa: BLE001
            pass
        self.desktop.close()
        self.browser.close()

    def _artifact(self, path: Path, width: int = 0, height: int = 0) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        relative = path.relative_to(self.artifact_root).as_posix()
        item: dict[str, Any] = {
            "path": relative,
            "url": (f"/api/runs/{self.run_id}/computer-frame?path={relative}"
                    if self.run_id else ""),
            "bytes": int(path.stat().st_size),
            "sha256": _sha256_file(path),
        }
        if width or height:
            item["width"] = int(width)
            item["height"] = int(height)
        return item

    def _new_path(self, prefix: str, suffix: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return self.artifact_root / f"{prefix}-{stamp}-{secrets.token_hex(4)}{suffix}"

    def _redact(self, path: Path, regions: list[dict], window: dict) -> int:
        """Black out secure-field bounds before the image becomes evidence.

        Returns the painted count, or -1 when secure regions exist but cannot be
        painted: the screenshot is deleted so an unredacted frame never lands in
        the run directory.
        """
        if not path.is_file() or not regions:
            return 0
        try:
            from PIL import Image, ImageDraw  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 — Pillow is optional
            path.unlink(missing_ok=True)
            return -1
        try:
            image = Image.open(path).convert("RGB")
            draw = ImageDraw.Draw(image)
            origin_x = int(window.get("x") or 0)
            origin_y = int(window.get("y") or 0)
            window_w = max(1, int(window.get("width") or image.width))
            window_h = max(1, int(window.get("height") or image.height))
            scale_x = image.width / window_w
            scale_y = image.height / window_h
            painted = 0
            for region in regions[:20]:
                x = int((int(region.get("x") or 0) - origin_x) * scale_x)
                y = int((int(region.get("y") or 0) - origin_y) * scale_y)
                w = int(int(region.get("width") or 0) * scale_x)
                h = int(int(region.get("height") or 0) * scale_y)
                if w <= 0 or h <= 0:
                    continue
                draw.rectangle((x, y, x + w, y + h), fill=(0, 0, 0))
                painted += 1
            image.save(path)
            return painted
        except Exception:  # noqa: BLE001 — redaction must not fail the look
            path.unlink(missing_ok=True)
            return -1

    def look(self, session: ComputerSession, arguments: Mapping[str, Any]) -> dict[str, Any]:
        requested = str(arguments.get("mode") or "").strip().lower()
        mode = requested if session.mode == "both" and requested in ("desktop", "browser") \
            else session.mode
        out: dict[str, Any] = {"available": True, "ok": True, "mode": mode}
        self.sessions.count_look()
        if mode in ("desktop", "both"):
            screenshot = self._new_path("look", ".png")
            ax_file = self._new_path("look", ".json")
            payload = {
                "command": "look",
                "apps": [app.bundle_id for app in session.apps],
                "screenshot_path": str(screenshot),
                "ax_path": str(ax_file),
            }
            data, error = self.desktop.call(payload)
            if error is not None:
                return {"available": error.get("code") != "COMPUTER_UNAVAILABLE",
                        "ok": False, "mode": mode, "error": error,
                        "screenshot": None, "elements": [], "text": ""}
            assert data is not None
            secure_regions = list(data.get("secure_regions") or [])[:20]
            redacted = self._redact(screenshot, secure_regions, data.get("window") or {})
            image = self._artifact(
                screenshot, int(data.get("width") or 0), int(data.get("height") or 0))
            ax = self._artifact(ax_file)
            out.update({
                "app": data.get("app") or {},
                "window": data.get("window") or {},
                "screenshot": image,
                "ax": ax,
                "elements": list(data.get("elements") or [])[:200],
                "text": str(data.get("text") or "")[:12_000],
                "secure_regions": secure_regions,
                "redacted_regions": redacted,
                "duration_ms": int(data.get("duration_ms") or 0),
            })
            return out
        url = str(arguments.get("url") or "").strip()
        screenshot = self._new_path("look", ".png")
        dom_file = self._new_path("look", ".html")
        payload = {
            "command": "look",
            "domains": list(session.domains),
            "screenshot_path": str(screenshot),
            "dom_path": str(dom_file),
        }
        if url:
            payload["url"] = url
        data, error = self.browser.call(payload)
        if error is not None:
            return {"available": error.get("code") != "COMPUTER_UNAVAILABLE",
                    "ok": False, "mode": mode, "error": error,
                    "screenshot": None, "elements": [], "text": ""}
        assert data is not None
        image = self._artifact(
            screenshot, int(data.get("width") or 0), int(data.get("height") or 0))
        dom = self._artifact(dom_file)
        out.update({
            "url": str(data.get("url") or ""),
            "title": str(data.get("title") or ""),
            "screenshot": image,
            "dom": dom,
            "elements": list(data.get("elements") or [])[:200],
            "text": str(data.get("text") or "")[:12_000],
            "console_errors": list(data.get("console_errors") or [])[:20],
            "duration_ms": int(data.get("duration_ms") or 0),
        })
        return out

    def act(self, session: ComputerSession, arguments: Mapping[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in ("click", "click_element", "click_xy", "type", "fill",
                          "press", "scroll"):
            raise AgentError("COMPUTER_ACTION_INVALID", "unsupported action")
        selector = str(arguments.get("selector") or "").strip()
        url = str(arguments.get("url") or "").strip()
        mode = session.mode
        if mode == "browser":
            backend = "browser"
        elif mode == "desktop":
            backend = "desktop"
        else:
            backend = "browser" if (selector or url) else "desktop"
        element_id = str(arguments.get("element_id") or "").strip()
        text = str(arguments.get("text") or "")
        key = str(arguments.get("key") or "").strip()
        if backend == "desktop":
            if selector or url:
                raise AgentError(
                    "COMPUTER_ACTION_INVALID",
                    "this is a desktop-only session; selectors and URLs need a browser session",
                )
            if action == "click_element" and not element_id:
                raise AgentError(
                    "COMPUTER_ACTION_INVALID",
                    "click_element needs the element_id returned by computer_look",
                )
            if action in ("click", "click_element") and not element_id:
                if not session.allow_destructive:
                    raise AgentError(
                        "COMPUTER_DESTRUCTIVE_BLOCKED",
                        "coordinate clicks are only allowed in a session with "
                        "allow_destructive=true; click by element_id instead",
                    )
                if arguments.get("x") is None or arguments.get("y") is None:
                    raise AgentError(
                        "COMPUTER_ACTION_INVALID",
                        "coordinate clicks need both x and y",
                    )
            if action == "click_xy":
                if not session.allow_destructive:
                    raise AgentError(
                        "COMPUTER_DESTRUCTIVE_BLOCKED",
                        "coordinate clicks are only allowed in a session with "
                        "allow_destructive=true",
                    )
                if arguments.get("x") is None or arguments.get("y") is None:
                    raise AgentError("COMPUTER_ACTION_INVALID", "click_xy needs both x and y")
            if action in ("type", "fill") and not text:
                raise AgentError("COMPUTER_ACTION_INVALID", "typing needs non-empty text")
            if action == "press" and not key:
                raise AgentError("COMPUTER_ACTION_INVALID", "press needs a key")
            if action == "scroll" and not int(arguments.get("delta_y") or 0):
                raise AgentError("COMPUTER_ACTION_INVALID", "scroll needs a non-zero delta_y")
        else:
            if not selector:
                raise AgentError(
                    "COMPUTER_ACTION_INVALID",
                    "browser actions need the CSS selector of one element",
                )
            if action == "scroll":
                raise AgentError(
                    "COMPUTER_ACTION_INVALID",
                    "browser actions support click, fill and press only",
                )
        self.sessions.count_action()
        before = self._new_path("act", "-before.png")
        after = self._new_path("act", "-after.png")
        if backend == "desktop":
            desktop_action = action
            if action == "click":
                desktop_action = "click_element" if element_id else "click_xy"
            elif action == "fill":
                desktop_action = "type"
            payload = {
                "command": "act",
                "action": desktop_action,
                "apps": [app.bundle_id for app in session.apps],
                "element_id": element_id,
                "x": int(arguments.get("x") or 0),
                "y": int(arguments.get("y") or 0),
                "text": text[:4_000],
                "key": key[:40],
                "delta_y": int(arguments.get("delta_y") or 0),
                "allow_destructive": session.allow_destructive,
                "destructive_markers": list(DESTRUCTIVE_MARKERS),
                "before_path": str(before),
                "after_path": str(after),
            }
            data, error = self.desktop.call(payload)
            backend = "desktop"
        else:
            browser_action = action
            if action in ("click", "click_element"):
                browser_action = "click"
            elif action in ("type", "fill"):
                browser_action = "fill"
            payload = {
                "command": "act",
                "action": browser_action,
                "domains": list(session.domains),
                "selector": selector[:300],
                "text": text[:4_000],
                "key": key[:40],
                "allow_destructive": session.allow_destructive,
                "destructive_markers": list(DESTRUCTIVE_MARKERS),
                "before_path": str(before),
                "after_path": str(after),
            }
            if url:
                payload["url"] = url
            data, error = self.browser.call(payload)
            backend = "browser"
        if error is not None:
            return {"available": error.get("code") != "COMPUTER_UNAVAILABLE",
                    "ok": False, "action": action, "mode": backend,
                    "error": error, "before": None, "after": None}
        assert data is not None
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        return {
            "available": True,
            "ok": True,
            "action": action,
            "mode": backend,
            "target": str(data.get("target") or "")[:300],
            "before": self._artifact(before, width, height),
            "after": self._artifact(after, width, height),
            "text": str(data.get("text") or "")[:12_000],
            "dom_diff": str(data.get("dom_diff") or "")[:4_000],
            "duration_ms": int(data.get("duration_ms") or 0),
        }

    def stop(self, reason: str = "") -> dict[str, Any]:
        try:
            self.desktop.call({"command": "release"})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.browser.call({"command": "release"})
        except Exception:  # noqa: BLE001
            pass
        return self.sessions.stop(reason)

    def availability(self, mode: str) -> tuple[bool, str]:
        if mode in ("desktop", "both"):
            ok, reason = self.desktop.availability()
            if not ok:
                return False, reason
        if mode in ("browser", "both"):
            return self.browser.availability()
        return True, ""


def build_computer_tools(runtime: ComputerRuntime) -> list[ToolSpec]:
    """Build the four ToolSpecs bound to one run's computer runtime."""
    from ..base import EFFECT_READ, EFFECT_WRITE, Param, ToolSpec

    def request(arguments: dict[str, Any]) -> dict[str, Any]:
        mode = str(arguments.get("mode") or "both")
        available, reason = runtime.availability(mode)
        if not available:
            return {
                "available": False, "ok": False, "mode": mode, "reason": reason,
                "error": {"code": "COMPUTER_UNAVAILABLE", "message": reason},
            }
        if not runtime.policy.allowed_apps and mode in ("desktop", "both"):
            return {
                "available": False, "ok": False, "mode": mode,
                "reason": f"no allowed desktop apps in {runtime.policy.config_path}",
                "error": {"code": "COMPUTER_CONFIG_MISSING",
                          "message": "configure ~/.fcode/computer.json first"},
            }
        if not runtime.policy.allowed_domains and mode in ("browser", "both"):
            return {
                "available": False, "ok": False, "mode": mode,
                "reason": f"no allowed domains in {runtime.policy.config_path}",
                "error": {"code": "COMPUTER_CONFIG_MISSING",
                          "message": "configure ~/.fcode/computer.json first"},
            }
        return {"available": True, **runtime.sessions.request(arguments)}

    def look(arguments: dict[str, Any]) -> dict[str, Any]:
        current = runtime.sessions.session
        mode = str(arguments.get("mode") or "").strip().lower()
        if mode in ("desktop", "browser"):
            session = runtime.sessions.require(mode)
        elif current is not None and current.mode == "browser":
            session = runtime.sessions.require("browser")
        elif current is not None and current.mode == "desktop":
            session = runtime.sessions.require("desktop")
        elif current is not None and current.apps:
            session = runtime.sessions.require("desktop")
        else:
            session = runtime.sessions.require("browser")
        return runtime.look(session, arguments)

    def act(arguments: dict[str, Any]) -> dict[str, Any]:
        session = runtime.sessions.current()
        return runtime.act(session, arguments)

    def stop(arguments: dict[str, Any]) -> dict[str, Any]:
        return runtime.stop(str(arguments.get("reason") or ""))

    return [
        ToolSpec(
            name="computer_request",
            summary=("Request a session-scoped computer-use grant (desktop apps and/or "
                     "external browser domains). Requires operator approval; the model can "
                     "never widen or renew the grant."),
            effect=EFFECT_WRITE,
            params=(
                Param(name="mode", kind="string", enum=("desktop", "browser", "both")),
                Param(name="apps", kind="array", required=False, maximum=4,
                      description="allowed app bundle ids or names from the host allowlist"),
                Param(name="domains", kind="array", required=False, maximum=5,
                      description="allowed external domains from the host allowlist"),
                Param(name="duration_seconds", kind="integer", required=False, minimum=30, maximum=900),
                Param(name="max_actions", kind="integer", required=False, minimum=1, maximum=100),
                Param(name="allow_destructive", kind="boolean", required=False),
                Param(name="reason", kind="string", required=False, max_length=500),
            ),
            handler=request,
            outputs=(Param.output("available", "boolean"), Param.output("ok", "boolean")),
            version="1.0",
        ),
        ToolSpec(
            name="computer_look",
            summary=("Read the current desktop window (accessibility tree + screenshot) or "
                     "the external browser page (DOM text/elements + screenshot). Requires an "
                     "active computer session."),
            effect=EFFECT_READ,
            params=(
                Param(name="mode", kind="string", required=False, enum=("desktop", "browser")),
                Param(name="url", kind="string", required=False, max_length=2_000),
            ),
            handler=look,
            outputs=(Param.output("available", "boolean"), Param.output("ok", "boolean")),
            version="1.0",
        ),
        ToolSpec(
            name="computer_act",
            summary=("Perform one action in an active computer session: AX element click, "
                     "coordinate click (requires allow_destructive), type, press or scroll; "
                     "external browser click/fill/press by selector."),
            effect=EFFECT_WRITE,
            params=(
                Param(name="action", kind="string",
                      enum=("click", "click_element", "click_xy", "type", "fill",
                            "press", "scroll")),
                Param(name="element_id", kind="string", required=False, max_length=200),
                Param(name="x", kind="integer", required=False, minimum=0, maximum=20_000),
                Param(name="y", kind="integer", required=False, minimum=0, maximum=20_000),
                Param(name="text", kind="string", required=False, max_length=4_000),
                Param(name="key", kind="string", required=False, max_length=40),
                Param(name="delta_y", kind="integer", required=False, minimum=-20_000, maximum=20_000),
                Param(name="selector", kind="string", required=False, max_length=300),
                Param(name="url", kind="string", required=False, max_length=2_000),
            ),
            handler=act,
            outputs=(Param.output("available", "boolean"), Param.output("ok", "boolean")),
            version="1.0",
        ),
        ToolSpec(
            name="computer_stop",
            summary="Stop the active computer session, release input and close the sidecar.",
            effect=EFFECT_READ,
            params=(Param(name="reason", kind="string", required=False, max_length=500),),
            handler=stop,
            outputs=(Param.output("available", "boolean"), Param.output("ok", "boolean")),
            version="1.0",
        ),
    ]
