#!/usr/bin/env python3
"""FCode desktop shell: one embedded console and one native webview window.

The HTTP server runs in this process. Frozen builds therefore never assume
that ``python`` or a source checkout exists on the user's machine.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import MutableMapping
from pathlib import Path
from urllib.parse import urlencode


def isolate_provider_environment(env: MutableMapping[str, str]) -> None:
    """Keep a parent agent's provider credentials out of the desktop app.

    Desktop providers are configured in FCode's settings page. Inheriting
    ``KIMI_*`` from the agent that launched the app can otherwise override the
    saved key and redirect it to that agent's private proxy. Advanced users
    who intentionally configure the desktop through the shell can opt in.
    """
    if env.get("FCODE_USE_HOST_PROVIDER_ENV", "").strip() == "1":
        return
    for name in tuple(env):
        if name.startswith(("KIMI_", "DEEPSEEK_")):
            env.pop(name, None)


def bundle_root() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("FCODE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FCode"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "FCode"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "fcode"


class DesktopConsole:
    """Own the loopback server, its authentication token and its lifetime."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.token = secrets.token_hex(32)
        self.httpd = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        if self.httpd is not None:
            return self.url
        isolate_provider_environment(os.environ)
        state = data_dir()
        state.mkdir(parents=True, exist_ok=True)
        os.environ["FCODE_RUNS_DIR"] = str(state / "runs")
        os.environ["FCODE_CONFIG_DIR"] = str(state / "config")
        # These imports read FCODE_* paths at module import time; set them first.
        viewer = str(self.repo / "viewer")
        if viewer not in sys.path:
            sys.path.insert(0, viewer)
        console = importlib.import_module("server")
        self.httpd = console.create_server(
            repo=self.repo,
            host="127.0.0.1",
            port=0,
            token=self.token,
            pace=0.0,
            local_endpoint=os.environ.get("FCODE_LOCAL_ENDPOINT", ""),
        )
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="fcode:console",
            daemon=True,
        )
        self.thread.start()
        return self.url

    @property
    def url(self) -> str:
        if self.httpd is None:
            raise RuntimeError("desktop console is not running")
        query = urlencode({"desktop": "1", "token": self.token})
        return f"http://127.0.0.1:{self.httpd.server_port}/?{query}"

    def health(self) -> dict:
        if self.httpd is None:
            raise RuntimeError("desktop console is not running")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_port}/api/health",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if not payload.get("ok") or not payload.get("live"):
            raise RuntimeError(f"desktop runtime is unavailable: {payload}")
        return payload

    def stop(self) -> None:
        httpd, thread = self.httpd, self.thread
        self.httpd = None
        self.thread = None
        if httpd is None:
            return
        if thread is not None and thread.is_alive():
            httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        # Disconnect host-configured MCP children as part of application exit.
        bridge = httpd.RequestHandlerClass.manager.mcp.get("bridge")
        if bridge is not None:
            bridge.disconnect()


def _fcode_window_id() -> int:
    """Return the on-screen FCode window id for native, window-only screenshots."""
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    for window in windows:
        if window.get("kCGWindowOwnerName") == "FCode" and window.get("kCGWindowLayer") == 0:
            return int(window["kCGWindowNumber"])
    raise RuntimeError("no visible FCode window")


def _acceptance_screenshot(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    window_id = _fcode_window_id()
    subprocess.run(
        ["/usr/sbin/screencapture", "-o", "-x", "-l", str(window_id), str(path)],
        check=True,
        timeout=15,
    )
    return {"path": str(path), "windowId": window_id}


def run_acceptance_plan(window, plan_path: str) -> None:
    """Execute an explicit, local JSON plan inside the native webview.

    This exists for automated acceptance on machines where macOS Accessibility
    permission cannot be granted to the test process. It drives the *real*
    native window's DOM through pywebview; it is not a second HTTP/browser UI.
    """
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    output_path = Path(plan_path).with_suffix(".results.jsonl")
    if not window.events.loaded.wait(timeout=20):
        raise RuntimeError("native webview did not finish loading")

    with output_path.open("w", encoding="utf-8") as results:
        for index, step in enumerate(plan):
            kind = str(step.get("type") or "")
            record = {"step": index, "type": kind, "name": step.get("name", "")}
            try:
                if kind == "wait":
                    seconds = max(0.0, float(step.get("ms", 0)) / 1000)
                    time.sleep(seconds)
                    record["result"] = {"ms": step.get("ms", 0)}
                elif kind == "resize":
                    window.resize(int(step["width"]), int(step["height"]))
                    record["result"] = {"width": window.width, "height": window.height}
                elif kind == "screenshot":
                    record["result"] = _acceptance_screenshot(Path(step["path"]))
                elif kind == "js":
                    record["result"] = window.evaluate_js(str(step["code"]))
                elif kind == "exit":
                    record["result"] = {"exiting": True}
                    results.write(json.dumps(record, ensure_ascii=False) + "\n")
                    results.flush()
                    window.destroy()
                    return
                else:
                    raise ValueError(f"unknown acceptance step type: {kind!r}")
            except Exception as error:  # keep one bad step from losing the earlier evidence
                record["error"] = f"{type(error).__name__}: {error}"
            results.write(json.dumps(record, ensure_ascii=False) + "\n")
            results.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FCode desktop application")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="start the embedded console, verify its authenticated health API, and exit",
    )
    parser.add_argument(
        "--acceptance-plan",
        metavar="PATH",
        help="test-only: run a JSON DOM plan in the native window and write PATH.results.jsonl",
    )
    args = parser.parse_args(argv)
    console = DesktopConsole(bundle_root())
    console.start()
    try:
        if args.smoke_test:
            health = console.health()
            print(json.dumps({"ok": True, "live": health["live"], "templates": health["templates"]}))
            return 0
        import webview

        webview.create_window(
            "FCode",
            console.url,
            width=1500,
            height=950,
            min_size=(1080, 680),
            background_color="#FBFAF9",
        )
        if args.acceptance_plan:
            webview.start(run_acceptance_plan, args=(webview.windows[0], args.acceptance_plan))
        else:
            webview.start()
        return 0
    finally:
        console.stop()


if __name__ == "__main__":
    sys.exit(main())
