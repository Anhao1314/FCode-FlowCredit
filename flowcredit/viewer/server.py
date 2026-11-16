#!/usr/bin/env python3
"""Fcode console — live server for the flowcredit runtime.

Serves the static viewer plus a small JSON + SSE API that drives the *real*
flowcredit runtime (the flowagent package) from a checkout, so the console is
not only a replay player:
a task typed into the composer actually runs, round by round, and committed
events stream into the page while the run is still in flight.

    python3 server.py --repo /path/to/FCode [--port 8642] [--pace 0.8] [--approval-timeout 300]

Endpoints
    GET  /api/health              what the server can and cannot do
    GET  /api/templates           scenario templates the composer may run
    GET  /api/runs                run history (survives restarts, runs/*/meta.json)
    GET  /api/review-profile      past review decisions projected from exported traces
    POST /api/runs                {"objective": "...", "template": "full", "approval": "consequential"}
    GET  /api/runs/{id}/stream    server-sent events: trace snapshots as they grow
    POST /api/runs/{id}/stop      stop at the next round boundary
    POST /api/runs/{id}/decision  {"approvalId": "...", "allow": true, "remember": false}
    POST /api/runs/{id}/plan      {"action": "skip"|"promote", "todoId": "t4"}
    POST /api/runs/{id}/budget    raise limits; applies at the next round
    POST /api/runs/{id}/resume    add budget and continue a failed run
    POST /api/runs/{id}/review    append a human decision to a terminal run's chain
    POST /api/runs/{id}/meta      {"name": "...", "pinned": true} -> updated record

Standard library only, the same constraint as the rest of this viewer. The
runtime itself is imported from the checkout given with ``--repo``; if that is
missing the static viewer still works and /api/health says why live mode is off.

This file is the HTTP surface. Configuration and the model catalog live in
``console_common``, runtime execution in ``console_runtime``, and run history
in ``console_manager``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

VIEWER_DIR = Path(__file__).resolve().parent
if str(VIEWER_DIR) not in sys.path:
    sys.path.insert(0, str(VIEWER_DIR))

from console_common import (  # noqa: E402
    BROWSER_DIR,
    DOMAIN_KEY,
    MAX_REQUEST_BODY,
    RUNS_DIR,
    forbidden_static_path,
    migrate_legacy_chat_config,
    run_trace_path,
)
from audit_service import AuditService  # noqa: E402
from gov_bots import GovBotService  # noqa: E402
from plugin_market import PluginMarket  # noqa: E402
from console_manager import RunManager as _RunManager  # noqa: E402

class RunManager(_RunManager):
    """Bind the manager to this module's RUNS_DIR (tests patch it)."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("runs_dir", RUNS_DIR)
        super().__init__(*args, **kwargs)
from console_runtime import Run, Runtime  # noqa: E402

__all__ = ["create_server", "main"]

class Handler(SimpleHTTPRequestHandler):
    server_version = "FcodeConsole/1.1"
    manager: RunManager  # set by main()
    token: str | None = None  # set by main() in desktop mode
    repo: Path | None = None  # set by create_server(); audit artifacts live here
    audit: AuditService | None = None  # set by create_server(); Governance-zone service
    gov_bots: GovBotService | None = None  # set by create_server(); governance team roster
    plugins: PluginMarket | None = None  # set by create_server(); curated plugin market

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(VIEWER_DIR), **kwargs)

    def _authorized(self) -> bool:
        """Desktop mode: cookie or bearer token. Browser dev mode: open."""
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and secrets.compare_digest(header[7:], self.token):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fcode_token" and secrets.compare_digest(value, self.token):
                return True
        return False

    def _reject_unauthorized(self, path: str) -> None:
        if path.startswith("/api/"):
            self._json({"error": "unauthorized"}, 401)
        else:
            self.send_error(403)

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (self.path or "") and self.command == "GET" and self.path.endswith("/stream"):
            return
        sys.stderr.write(f"[console] {self.address_string()} {fmt % args}\n")

    # ------------------------------------------------------------------- utils

    def end_headers(self) -> None:  # noqa: N802
        # Static assets change under the operator's feet; never let the browser cache them.
        if (self.path or "").split("?")[0].endswith((".html", ".css", ".js")):
            self.send_header("Cache-Control", "no-cache")
        # 74 处 innerHTML 的第二道防线：脚本一律外部文件（boot.js 已抽取），
        # 样式允许既有 esc() 产出的内联属性。任何一处转义遗漏不再等于 XSS。
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        super().end_headers()

    def _json(self, payload: dict | list, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY:
            # Drain a bounded amount so the client gets a clean 413 instead of
            # a reset connection, then let the caller reject the request.
            self.rfile.read(min(length, 65_536))
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _same_origin(self) -> bool:
        """Refuse browser cross-site writes; non-browser clients have no Origin."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        parsed = urlparse(origin)
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and parsed.netloc.lower() == host

    def _mutation_guard(self) -> bool:
        """Return False and write the error response when a write is refused."""
        if not self._same_origin():
            self._json({"error": "cross-origin request refused"}, 403)
            return False
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BODY:
            # Drain a bounded prefix so the client can read the 413 instead of
            # hitting a broken pipe; anything beyond 8 MiB is not worth reading.
            try:
                self.rfile.read(min(length, 8_388_608))
            except OSError:
                pass
            self._json({"error": "request body too large"}, 413)
            self.close_connection = True
            return False
        if length > 0:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._json({"error": "content-type must be application/json"}, 415)
                return False
        return True

    def _sse(self, event: str, payload: dict) -> bool:
        try:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # -------------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        supplied = parse_qs(parsed.query).get("token", [None])[0]
        if self.token and supplied is not None:
            if not secrets.compare_digest(supplied, self.token):
                return self.send_error(403)
            # Bootstrap: set an HttpOnly cookie, then strip only the token from
            # the URL. Other parameters (desktop=1, template=...) survive.
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("token", None)
            location = path or "/"
            if params:
                location += "?" + urlencode(params, doseq=True)
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Set-Cookie", f"fcode_token={self.token}; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers()
            return
        if not self._authorized():
            return self._reject_unauthorized(path)
        if path.startswith("/api/"):
            return self._api_get(path)
        if path == "/audit.html":
            # 审计台已并入统一壳(#/audit surface);独立页退役,旧链接不断。
            self.send_response(302)
            self.send_header("Location", "/#/audit")
            self.end_headers()
            return
        if forbidden_static_path(path):
            self.send_error(404)
            return
        return super().do_GET()

    def _api_get(self, path: str) -> None:
        mgr = self.manager
        if path == "/api/health":
            live = mgr.runtime is not None
            return self._json(
                {
                    "ok": True,
                    "live": live,
                    "repo": str(mgr.runtime.repo) if live else "",
                    "runtimeError": mgr.runtime_error,
                    "pace": mgr.pace,
                    "templates": len(mgr.runtime.templates()) if live else 0,
                    "domain": (
                        {"key": DOMAIN_KEY, "title": mgr.runtime.get_domain(DOMAIN_KEY).title}
                        if live else None
                    ),
                }
            )
        if path == "/api/memory":
            return self._json(mgr.memory_summary())
        if path == "/api/templates":
            if mgr.runtime is None:
                return self._json({"error": mgr.runtime_error}, 503)
            return self._json(mgr.runtime.templates())
        if path == "/api/models":
            query = parse_qs(urlparse(self.path).query)
            template_key = (query.get("template") or [""])[0].strip()
            return self._json({
                "default": mgr.default_model(),
                "template": template_key or None,
                "models": mgr.model_catalog_with_budget(template_key or None),
            })
        if path == "/api/chat/workspace-file":
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            relative = str((query.get("path") or [""])[0] or "").strip()
            if not relative:
                return self._json({"error": "path is required"}, 400)
            try:
                return self._json(mgr.chat.read_workspace_file(relative))
            except Exception as error:  # noqa: BLE001 — AgentError is a host refusal
                return self._json({
                    "error": str(error) or "workspace file is unavailable",
                    "code": getattr(error, "code", "CHAT_WORKSPACE_READ_FAILED"),
                }, 400)
        if path == "/api/chat/workspace-files":
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            needle = str((query.get("q") or [""])[0] or "")
            try:
                limit = int((query.get("limit") or ["12"])[0])
            except (TypeError, ValueError):
                return self._json({"error": "limit must be an integer"}, 400)
            try:
                return self._json(mgr.chat.list_workspace_files(needle, limit))
            except Exception as error:  # noqa: BLE001 — AgentError is a host refusal
                return self._json({
                    "error": str(error) or "workspace files are unavailable",
                    "code": getattr(error, "code", "CHAT_WORKSPACE_LIST_FAILED"),
                }, 400)
        if path == "/api/browser/artifact":
            query = parse_qs(urlparse(self.path).query)
            relative = str((query.get("path") or [""])[0] or "").strip()
            if not relative:
                return self._json({"error": "path is required"}, 400)
            root = BROWSER_DIR.resolve()
            target = (root / relative).resolve()
            if target == root or root not in target.parents:
                return self._json({"error": "artifact not found"}, 404)
            if target.suffix.lower() not in (".png", ".html") or not target.is_file():
                return self._json({"error": "artifact not found"}, 404)
            data = target.read_bytes()
            self.send_response(200)
            # DOM HTML is untrusted page content: never serve it as text/html on
            # this origin (same-origin script execution). PNG is safe to render.
            self.send_header(
                "Content-Type",
                "image/png" if target.suffix.lower() == ".png" else "text/plain; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/chat/threads":
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("limit") or ["50"])[0])
                offset = int((query.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                return self._json({"error": "limit and offset must be integers"}, 400)
            if not 1 <= limit <= 200 or offset < 0:
                return self._json({"error": "limit must be 1..200 and offset >= 0"}, 400)
            return self._json({
                "threads": mgr.chat.list_threads(limit, offset),
                "total": mgr.chat.thread_count(),
                "limit": limit,
                "offset": offset,
            })
        if path == "/api/chat/models":
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            return self._json({
                "models": mgr.chat.chat_models(),
                "default": mgr.chat.default_model(),
            })
        if path == "/api/schedules":
            if getattr(mgr, "schedules", None) is None:
                return self._json({"error": "scheduler is unavailable"}, 503)
            import datetime

            from console_scheduler import describe_next

            now = datetime.datetime.now()
            entries = [dict(e, next=describe_next(e, now)) for e in mgr.schedules.list()]
            return self._json({"schedules": entries})
        if path == "/api/chat/memory":
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            return self._json({"memories": mgr.chat.memory.list()})
        if path in ("/api/settings/model-config", "/api/settings/chat-config"):
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            return self._json(mgr.chat.config_snapshot())
        if path.startswith("/api/chat/threads/"):
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            thread_id = path[len("/api/chat/threads/") :]
            if not thread_id or "/" in thread_id:
                return self._json({"error": "no such thread"}, 404)
            try:
                thread = mgr.chat.get_thread(thread_id)
            except Exception as error:  # noqa: BLE001 — a missing thread is a 404
                code = getattr(error, "code", "")
                status = 404 if code in ("CHAT_THREAD_NOT_FOUND", "TASK_NOT_FOUND") else 400
                return self._json({"error": str(error), "code": code}, status)
            return self._json(thread)
        if path == "/api/corpus":
            if mgr.runtime is None:
                return self._json({"error": mgr.runtime_error}, 503)
            query = parse_qs(urlparse(self.path).query)
            template_key = (query.get("template") or [""])[0].strip()
            try:
                documents = mgr.runtime.corpus(template_key or None)
            except Exception:  # noqa: BLE001 — unknown template is a 400, not a crash
                return self._json({"error": "unknown template"}, 400)
            return self._json({"documents": documents, "default": "full"})
        if path.startswith("/api/templates/") and path.endswith("/tools"):
            if mgr.runtime is None:
                return self._json({"error": mgr.runtime_error}, 503)
            template_key = path[len("/api/templates/") : -len("/tools")]
            try:
                tools = mgr.runtime.template_tools(template_key)
            except Exception:  # noqa: BLE001
                return self._json({"error": f"unknown template {template_key!r}"}, 400)
            return self._json({"template": template_key, "tools": tools})
        if path == "/api/modules":
            if mgr.runtime is None:
                return self._json({"error": mgr.runtime_error}, 503)
            return self._json({"modules": mgr.runtime.modules()})
        if path == "/api/mcp":
            bridge = mgr.mcp.get("bridge")
            return self._json({
                "ready": mgr.mcp.get("ready", False),
                "servers": mgr.mcp_tools(),
                "approvalTools": list(bridge.approval_names()) if bridge else [],
            })
        if path == "/api/plugins":
            plugins = getattr(self, "plugins", None)
            if plugins is None:
                return self._json({"error": "plugin market is unavailable"}, 503)
            tool_names: set[str] = set()
            if mgr.runtime is not None:
                try:
                    tool_names = {
                        str(item.get("name"))
                        for item in mgr.runtime.template_tools("general-research")
                        if isinstance(item, dict)
                    }
                except Exception:  # noqa: BLE001 — market still lists MCP plugins
                    tool_names = set()
            return self._json({
                "plugins": plugins.entries(
                    tool_names=tool_names,
                    mcp_servers=mgr.mcp_tools(),
                ),
                "mcp_ready": bool(mgr.mcp.get("ready", False)),
            })
        if path == "/api/tools":
            if mgr.runtime is None:
                return self._json({"error": mgr.runtime_error}, 503)
            return self._json({"tools": mgr.runtime.tool_contracts()})
        if path == "/api/runs":
            query = parse_qs(urlparse(self.path).query)
            project_id = str(
                (query.get("projectId") or [""])[0] or ""
            ).strip() or None
            return self._json(mgr.list_runs(project_id=project_id))
        if path == "/api/projects":
            query = parse_qs(urlparse(self.path).query)
            include_archived = str(
                (query.get("includeArchived") or [""])[0] or ""
            ).strip().lower() in ("1", "true", "yes")
            return self._json({
                "projects": mgr.list_projects(include_archived=include_archived),
            })
        # ---- 治理团队（Phase A）：Bot 花名册 + 运行日志 ----
        if path == "/api/gov/bots":
            bots = getattr(self, "gov_bots", None)
            if bots is None:
                return self._json({"error": "governance bots are unavailable"}, 503)
            return self._json(bots.snapshot())
        if path == "/api/gov/runs":
            bots = getattr(self, "gov_bots", None)
            if bots is None:
                return self._json({"error": "governance bots are unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            bot_id = str((query.get("bot") or [""])[0] or "").strip() or None
            try:
                limit = int((query.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                return self._json({"error": "limit must be an integer"}, 400)
            return self._json({
                "runs": bots.runs(bot_id, limit),
                "explanations": bots.explanations_map(),
            })
        if path == "/api/gov/swarms":
            bots = getattr(self, "gov_bots", None)
            if bots is None:
                return self._json({"error": "governance bots are unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                return self._json({"error": "limit must be an integer"}, 400)
            return self._json({"swarms": bots.swarms(limit)})
        if path == "/api/gov/fixes":
            fixes = getattr(self, "gov_fixes", None)
            if fixes is None:
                return self._json({"error": "fixer is unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                return self._json({"error": "limit must be an integer"}, 400)
            return self._json({"fixes": fixes.list(limit)})
        # ---- 审计域只读 API：发现账本的数据源（work/audit/<audit_id>/）----
        if path == "/api/audit/bundles":
            return self._json(self.audit.bundles())
        if path.startswith("/api/audit/bundle/"):
            result = self.audit.bundle(path[len("/api/audit/bundle/"):].strip("/"))
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/evidence/"):
            parts = [p for p in path.split("/") if p]
            # /api/audit/evidence/<audit_dir>/<poc_id>
            if len(parts) != 5:
                return self._json({"error": "bad path"}, 400)
            result = self.audit.evidence(parts[3], parts[4])
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/regression/"):
            audit_dir = path[len("/api/audit/regression/"):].strip("/")
            result = self.audit.regression(audit_dir)
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/spec-gap/"):
            audit_dir = path[len("/api/audit/spec-gap/"):].strip("/")
            result = self.audit.spec_gap(audit_dir)
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/chain/"):
            audit_dir = path[len("/api/audit/chain/"):].strip("/")
            result = self.audit.chain(audit_dir)
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/rea/"):
            parts = [p for p in path.split("/") if p]
            # /api/audit/rea/<audit_dir>/<filename>
            if len(parts) != 5:
                return self._json({"error": "bad path"}, 400)
            result = self.audit.rea(parts[3], parts[4])
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        if path.startswith("/api/audit/jobs/"):
            job = self.audit.job_status(path[len("/api/audit/jobs/"):].strip("/"))
            if job is None:
                return self._json({"error": "not found"}, 404)
            return self._json(job)
        if path == "/api/review-profile":
            query = parse_qs(urlparse(self.path).query)

            def param(name: str) -> str:
                values = query.get(name) or [""]
                return str(values[0] or "").strip()

            profile = mgr.review_profile(
                module=param("module"),
                template=param("template"),
                outcome=param("outcome"),
            )
            profile["audit_dispositions"] = self.audit.disposition_summary()
            return self._json(profile)
        if path.startswith("/api/runs/"):
            rest = path[len("/api/runs/") :]
            run_id, _, action = rest.partition("/")
            run = mgr.get(run_id)
            if run is None:
                return self._json({"error": "no such run"}, 404)
            if action == "stream":
                return self._stream(run)
            if action == "trace":
                return self._run_trace(run)
            if action == "":
                return self._json(run.to_json(detail=True))
            if action == "queue":
                control = getattr(run, "controller", None)
                queue = control.queue_snapshot() if control is not None else (run.queue or [])
                return self._json({"id": run.id, "queue": queue})
            if action == "project":
                project_path = RUNS_DIR / run.id / "project.json"
                if not project_path.is_file():
                    return self._json({"error": "this run has no imported project"}, 404)
                try:
                    payload = json.loads(project_path.read_text(encoding="utf-8"))
                    patch_path = RUNS_DIR / run.id / "changes.patch"
                    payload["patch"] = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
                    payload["patchAvailable"] = patch_path.is_file()
                except (OSError, ValueError) as error:
                    return self._json({"error": f"project delivery is unreadable: {error}"}, 500)
                return self._json(payload)
            if action == "workspace" or action.startswith("workspace/"):
                # Read-only views of the run's sandbox, jail-checked by the
                # same WorkspaceSandbox the tools use — never raw paths.
                if mgr.runtime is None:
                    return self._json({"error": mgr.runtime_error}, 503)
                sandbox = mgr.runtime.WorkspaceSandbox(RUNS_DIR / run.id / "workspace")
                target = action[len("workspace"):].lstrip("/")
                if not target:
                    if not sandbox.root.exists():
                        return self._json({"entries": [], "count": 0})
                    return self._json({"entries": sandbox.ls(), "count": len(sandbox.ls())})
                try:
                    record = sandbox.read(target)
                except Exception as error:  # noqa: BLE001 — jail refusals are 404/400, not crashes
                    code = getattr(error, "code", "")
                    status = 400 if code == "SANDBOX_PATH_DENIED" else 404
                    return self._json({"error": code or str(error)}, status)
                return self._json(record)
            if action == "workspace-media":
                # Raw bytes for a delivered image/video. The JSON workspace
                # endpoint is for text; an <img>/<video> tag needs the actual
                # bytes under the same jail and same-origin policy.
                if mgr.runtime is None:
                    return self._json({"error": mgr.runtime_error}, 503)
                query = parse_qs(urlparse(self.path).query)
                relative = str((query.get("path") or [""])[0] or "").strip()
                if not relative:
                    return self._json({"error": "path is required"}, 400)
                sandbox = mgr.runtime.WorkspaceSandbox(RUNS_DIR / run.id / "workspace")
                try:
                    target_path, _ = sandbox.resolve(relative)
                except Exception as error:  # noqa: BLE001 — jail refusal, not a crash
                    code = getattr(error, "code", "")
                    return self._json({"error": code or str(error)}, 404)
                media_types = {
                    ".gif": "image/gif",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                    ".mp4": "video/mp4",
                    ".webm": "video/webm",
                }
                content_type = media_types.get(target_path.suffix.lower())
                if content_type is None or not target_path.is_file():
                    return self._json({"error": "media not found"}, 404)
                try:
                    size = target_path.stat().st_size
                except OSError:
                    return self._json({"error": "media not found"}, 404)
                if size > 20 * 1024 * 1024:
                    return self._json({"error": "media too large"}, 413)
                data = target_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
                return
            if action == "quality-frame":
                # The deterministic probe's contact sheet, generated by the
                # host outside the workspace. Only flat PNG names are accepted;
                # the directory is fixed, so no path traversal is possible.
                query = parse_qs(urlparse(self.path).query)
                name = str((query.get("name") or ["contact.png"])[0] or "contact.png")
                if not re.fullmatch(r"[A-Za-z0-9._-]+\.png", name):
                    return self._json({"error": "invalid frame name"}, 400)
                frames_dir = (RUNS_DIR / run.id / "quality-frames").resolve()
                target = (frames_dir / name).resolve()
                if target.parent != frames_dir or not target.is_file():
                    return self._json({"error": "quality frame not found"}, 404)
                try:
                    size = target.stat().st_size
                except OSError:
                    return self._json({"error": "quality frame not found"}, 404)
                if size > 20 * 1024 * 1024:
                    return self._json({"error": "quality frame too large"}, 413)
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json({"error": "unknown action"}, 404)
        return self._json({"error": "unknown endpoint"}, 404)

    def _run_trace(self, run: Run) -> None:
        """One run's trace, read out of the manager's runs directory.

        ``runs/<id>/trace.json`` is only reachable as a *static* path when the
        runs directory happens to sit inside the viewer checkout (the
        development default). The desktop bundle keeps runs in Application
        Support, where the static handler cannot see them, so a finished run
        opened after a restart used to answer 404. Serving it here gives both
        deployments one code path, and the jail check in ``run_trace_path``
        keeps the read inside the run's own directory.
        """
        path = run_trace_path(self.manager.runs_dir, run.id)
        if path is None:
            return self._json({"error": "refused: trace path is outside the runs directory"}, 400)
        if not path.is_file():
            return self._json({"error": "this run has no trace on disk yet"}, 404)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return self._json({"error": f"trace is unreadable: {error}"}, 500)
        return self._json(payload)

    def _stream(self, run: Run) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if not self._sse("hello", {"runId": run.id, "status": run.status, "queue": run.queue or []}):
            return
        last = -1
        # Incremental protocol: the first frame carries the whole trace, every
        # later frame carries only what changed. The watcher rebuilds the full
        # snapshot each tick, so the delta is computed here by slicing: events
        # are append-only, and at most the last previously-sent round can still
        # mutate (round_start -> proposal -> observation), so it is resent.
        sent_events = 0
        sent_rounds = 0
        sent_round_tail = None
        sent_full = False
        while True:
            with run.cond:
                run.cond.wait_for(lambda: run.version > last or run.finished, timeout=10)
                version = run.version
                snapshot = run.snapshot
                finished = run.finished
                status = run.status
                trace_path = run.trace_path
                error = run.error
                pending = run.pending
                approval = run.approval
                queue = run.queue
            if version > last and snapshot is not None:
                last = version
                if not sent_full:
                    sent_full = True
                    events = snapshot.get("events") or []
                    rounds = snapshot.get("rounds") or []
                    sent_events = len(events)
                    sent_rounds = len(rounds)
                    ok = self._sse(
                        "snapshot",
                        {
                            "version": version,
                            "status": status,
                            "finished": finished,
                            "tracePath": trace_path,
                            "error": error,
                            "snapshot": snapshot,
                            "pending": pending,
                            "approval": approval,
                            "queue": queue or [],
                        },
                    )
                else:
                    events = snapshot.get("events") or []
                    rounds = snapshot.get("rounds") or []
                    round_start = sent_rounds
                    if sent_rounds and (len(rounds) < sent_rounds or rounds[sent_rounds - 1] != sent_round_tail):
                        round_start = sent_rounds - 1
                    round_start = max(0, round_start)
                    delta = {
                        "version": version,
                        "status": status,
                        "finished": finished,
                        "tracePath": trace_path,
                        "error": error,
                        "pending": pending,
                        "approval": approval,
                        "queue": queue or [],
                        "roundStart": round_start,
                        "rounds": rounds[round_start:],
                        "events": events[sent_events:],
                        "terminal": snapshot.get("terminal"),
                        "chain": snapshot.get("chain"),
                        "task": snapshot.get("task"),
                        "model": snapshot.get("model"),
                    }
                    sent_events = len(events)
                    sent_rounds = len(rounds)
                    sent_round_tail = rounds[-1] if rounds else None
                    ok = self._sse("append", delta)
                if not ok:
                    return
            elif finished:
                if snapshot is None:
                    self._sse("failed", {"status": status, "error": error})
                    return
            if finished and version == last and snapshot is not None:
                self._sse("done", {"status": status, "tracePath": trace_path, "error": error})
                return
            if version == last:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    def _stream_chat(self, events) -> None:
        """Relay a ChatService event generator as SSE.

        A client disconnect makes ``_sse`` return False; closing the generator
        lets the service record the cancelled turn in the event chain.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            for event in events:
                kind = str(event.get("type") or "message")
                if not self._sse(kind, event):
                    break
        except Exception as error:  # noqa: BLE001 — surface config/provider failures in-band
            self._sse("error", {
                "code": getattr(error, "code", "CHAT_FAILED"),
                "message": str(error),
            })
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 — the response is already over
                    pass

    # ------------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        from console_post import dispatch_post

        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/"):
            return self.send_error(404)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if not self._mutation_guard():
            return
        if path.startswith("/api/audit/disposition/"):
            parts = [p for p in path.split("/") if p]
            # /api/audit/disposition/<audit_dir>/<finding_id>
            if len(parts) != 5:
                return self._json({"error": "bad path"}, 400)
            body = self._read_body()
            result = self.audit.disposition(parts[3], parts[4],
                                            str(body.get("disposition") or ""))
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        return dispatch_post(self, path)

    def do_DELETE(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/"):
            return self.send_error(404)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if not self._mutation_guard():
            return
        mgr = self.manager
        if path.startswith("/api/chat/threads/"):
            thread_id = path[len("/api/chat/threads/") :]
            if not thread_id or "/" in thread_id:
                return self._json({"error": "no such thread"}, 404)
            if mgr.chat is None:
                return self._json({"error": "chat is unavailable"}, 503)
            try:
                result = mgr.chat.archive_thread(thread_id)
            except Exception as error:  # noqa: BLE001 — missing thread is a 404
                code = getattr(error, "code", "")
                status = 404 if code in ("CHAT_THREAD_NOT_FOUND", "TASK_NOT_FOUND") else 400
                return self._json({"error": str(error), "code": code}, status)
            return self._json(result)
        if not path.startswith("/api/runs/") or path.count("/") != 3:
            return self.send_error(404)
        if mgr.runtime is None:
            return self._json({"error": "live mode unavailable"}, 503)
        run_id = path[len("/api/runs/") :]
        ok, reason = mgr.delete(run_id)
        if not ok:
            return self._json({"error": reason}, 404 if reason == "no such run" else 409)
        return self._json({"deleted": True, "id": run_id})


# ------------------------------------------------------------------------ main


class ConsoleHTTPServer(ThreadingHTTPServer):
    """永不解析反向 DNS 的 HTTP 服务。

    标准库 ``HTTPServer.server_bind`` 在 bind 之后会执行 ``socket.getfqdn(host)``。
    反向 DNS 慢或不可达的机器上这句会挂很久：CI 的 macOS runner 实测
    ``getfqdn('127.0.0.1') -> 'localhost' in 35.06s``。而它发生在我们打印任何
    启动日志、进入 serve_forever 之前，于是外部看到的现象是「套接字已经 listen、
    请求却没人处理、日志一行没有」——两份以子进程方式起控制台的测试因此连续
    二十余次在 macOS 上超时（ubuntu 的解析快，一直是绿的）。

    我们只绑回环，且 ``server_name`` 不参与任何路由，所以直接用字面量填掉它。
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def create_server(
    *,
    repo: str | Path = "",
    host: str = "127.0.0.1",
    port: int = 0,
    token: str = "",
    pace: float = 0.0,
    approval_timeout: float = 300.0,
    local_endpoint: str | None = None,
) -> ConsoleHTTPServer:
    """Construct a bound console for either the CLI or an embedded desktop."""
    runtime: Runtime | None = None
    runtime_error = ""
    if repo:
        try:
            runtime = Runtime(Path(repo).expanduser())
        except Exception as exc:  # noqa: BLE001 — health reports why live mode is off
            runtime_error = f"{type(exc).__name__}: {exc}"
    else:
        runtime_error = "no repo given; set FLOWAGENT_REPO to enable live runs"

    migrate_legacy_chat_config()
    manager = RunManager(
        runtime, runtime_error, pace, approval_timeout,
        local_endpoint=(local_endpoint or "").strip() or None,
    )
    Handler.manager = manager
    Handler.token = token or None
    Handler.repo = Path(repo).expanduser().resolve() if repo else None
    # Governance-zone service: own root, own path jail, own write path. The
    # HTTP handler translates; it does not import flowaudit.
    Handler.audit = AuditService(
        repo=Handler.repo,
        runtime=runtime,
    )
    # Governance team roster: durable run log beside the other console state.
    # The bot service is only a scheduler/log; audit bundles remain the truth.
    try:
        Handler.gov_bots = GovBotService(
            Handler.audit,
            manager.runs_dir.parent / "governance.sqlite",
            # triage 与解释层共用同一个 provider 目录纪律（共享默认模型、
            # mock fail-closed）；triage 的产物只进 bot_runs，不进审计包。
            provider_factory=manager.gov_explainer_provider,
        )
    except Exception:  # noqa: BLE001 — team UI degrades, audit stays available
        traceback.print_exc()
    # Fixer write path: validate in an isolated copy, apply only on approval,
    # then start a fresh manual audit so the finding is re-decided by evidence.
    try:
        from gov_fixes import GovFixService

        bots = getattr(Handler, "gov_bots", None)

        def _delegate_fix(objective, project_path, verification, model_key):
            run = manager.create_fix_run(
                objective, project_path, verification, model_key=model_key)
            return {"run_id": run.id}

        def _work_fix_lookup(run_id):
            run = manager.get(str(run_id or ""))
            if run is None:
                return {"finished": True, "status": "missing",
                        "error": "Work run not found"}
            run_dir = manager.runs_dir / run.id
            source = ""
            project_path = run_dir / "project.json"
            try:
                if project_path.is_file():
                    source = str(json.loads(
                        project_path.read_text(encoding="utf-8")
                    ).get("source") or "")
            except (OSError, ValueError):
                source = ""
            patch_path = run_dir / "changes.patch"
            try:
                patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
            except OSError:
                patch = ""
            return {
                "finished": bool(run.finished),
                "status": str(getattr(run, "status", "") or ""),
                "patch": patch,
                "source": source,
                "error": str(getattr(run, "error", "") or ""),
            }

        Handler.gov_fixes = GovFixService(
            Handler.audit,
            (bots.start_manual_run if bots is not None else None),
            root=(Handler.audit.root.parent / "fixes") if Handler.audit.root else None,
            delegate=_delegate_fix,
            work_lookup=_work_fix_lookup,
        )
    except Exception:  # noqa: BLE001 — audit/team remain usable without fixes
        traceback.print_exc()
        Handler.gov_bots = None
    # 解释层：模型解释确定性证据，不做裁决。bundle 读取走审计服务（同一 jail），
    # provider 构建走与运行相同的目录纪律；产物落 governance.sqlite。
    Handler.gov_explainer = None
    if Handler.gov_bots is not None:
        try:
            from gov_explainer import GovExplainer

            Handler.gov_explainer = GovExplainer(
                service=Handler.gov_bots,
                bundle_loader=lambda name: Handler.audit.bundle(name),
                provider_factory=lambda key: manager.gov_explainer_provider(key),
                default_model=os.environ.get("FCODE_DEFAULT_RUN_MODEL", ""),
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            Handler.gov_explainer = None
    # Curated plugin market over mcp.json. Install/uninstall is a host action
    # with catalog-only ids; the UI cannot invent commands.
    try:
        Handler.plugins = PluginMarket(
            config_path=os.environ.get("FCODE_MCP_CONFIG")
            or (Handler.repo / "mcp.json" if Handler.repo else ""),
            repo=Handler.repo,
        )
    except Exception:  # noqa: BLE001 — market UI degrades, runtime stays up
        traceback.print_exc()
        Handler.plugins = None
    httpd = ConsoleHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="console-server", description="Fcode console — static viewer + live runtime API."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("FLOWAGENT_REPO", ""),
        help="path to the FCode checkout (or set FLOWAGENT_REPO)",
    )
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--pace", type=float, default=0.8, help="simulated thinking seconds per round (0 = as fast as possible)"
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=300.0,
        help="seconds a paused approval waits for a human before denying (default 300)",
    )
    parser.add_argument(
        "--local-endpoint",
        default=os.environ.get("FCODE_LOCAL_ENDPOINT", ""),
        help="Ollama-compatible base URL for the local provider (enables the 本地 Qwen model entry)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="require this token (cookie bootstrap via /?token=…); desktop mode uses it",
    )
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print(
            "[console] refusing to listen on a non-loopback host without --token; "
            "configure --token (the desktop app does this) or bind 127.0.0.1"
        )
        return 2

    httpd = create_server(
        repo=args.repo,
        host=args.host,
        port=args.port,
        token=args.token,
        pace=args.pace,
        approval_timeout=args.approval_timeout,
        local_endpoint=args.local_endpoint,
    )
    manager = Handler.manager
    print(f"[console] serving {VIEWER_DIR} on http://{args.host}:{httpd.server_port}")
    if manager.runtime is not None:
        print(
            f"[console] live mode on — runtime from {manager.runtime.src} "
            f"(pace {args.pace}s/round, approval timeout {args.approval_timeout:.0f}s)"
        )
    else:
        print(f"[console] live mode OFF — {manager.runtime_error}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] bye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
