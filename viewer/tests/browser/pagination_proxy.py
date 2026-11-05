#!/usr/bin/env python3
"""Tiny controlled-pagination proxy for FCode browser acceptance.

- GET /api/chat/threads: serves a 5-row fake server list, two rows per page,
  optionally delayed once for in-flight race checks.
- DELETE /api/chat/threads/vol-*: removes from the fake list.
- Everything else is streamed through to the real local FCode server.
No external model is called by this proxy; chat traffic goes to the local
server, which is configured with the offline mock chat provider.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = ("127.0.0.1", 8642)
PORT = 8643
LOG = Path("/tmp/fcode-pagination/proxy.log")

LOCK = threading.Lock()
THREADS = [
    {"id": "vol-1", "title": "服务端分页线程 1", "updated_at": "2026-10-27T10:00:01Z"},
    {"id": "vol-2", "title": "服务端分页线程 2", "updated_at": "2026-10-27T10:00:02Z"},
    {"id": "vol-3", "title": "服务端分页线程 3", "updated_at": "2026-10-27T10:00:03Z"},
    {"id": "vol-4", "title": "服务端分页线程 4", "updated_at": "2026-10-27T10:00:04Z"},
    {"id": "vol-5", "title": "服务端分页线程 5", "updated_at": "2026-10-27T10:00:05Z"},
]
DELAY = {"ms": 0, "once": False}


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def take_delay() -> int:
    with LOCK:
        ms = int(DELAY["ms"])
        if ms and DELAY["once"]:
            DELAY["ms"] = 0
            DELAY["once"] = False
        return ms


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        log("[proxy] " + (fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in {"host", "connection", "proxy-connection",
                               "transfer-encoding", "content-length"}:
                continue
            if key.lower() == "origin":
                value = "http://127.0.0.1:8642"
            if key.lower() == "referer" and value.startswith("http://127.0.0.1:8643/"):
                value = "http://127.0.0.1:8642/" + value[len("http://127.0.0.1:8643/"):]
            headers[key] = value
        headers["Host"] = "127.0.0.1:8642"
        conn = http.client.HTTPConnection(*BACKEND, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in {"connection", "transfer-encoding", "content-length",
                                   "server", "date"}:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        finally:
            conn.close()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/__control":
            query = urllib.parse.parse_qs(parsed.query)
            with LOCK:
                DELAY["ms"] = int((query.get("delay_ms") or ["0"])[0])
                DELAY["once"] = (query.get("once") or ["0"])[0] == "1"
                state = dict(DELAY, threads=[t["id"] for t in THREADS])
            log(f"[control] {state}")
            return self._json(state)
        if parsed.path == "/__state":
            with LOCK:
                return self._json({"threads": list(THREADS), "delay": dict(DELAY)})
        if parsed.path == "/api/chat/threads":
            query = urllib.parse.parse_qs(parsed.query)
            offset = max(0, int((query.get("offset") or ["0"])[0]))
            limit = max(1, int((query.get("limit") or ["50"])[0]))
            with LOCK:
                snapshot = list(THREADS)
            delay = take_delay()
            if delay:
                time.sleep(delay / 1000)
            page = snapshot[offset:offset + 2]
            payload = {
                "threads": page,
                "total": len(snapshot),
                "limit": 2,
                "offset": offset,
            }
            log(
                f"[threads] request limit={limit} offset={offset} delay={delay} "
                f"-> ids={[item['id'] for item in page]} total={len(snapshot)}"
            )
            return self._json(payload)
        return self._proxy()

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        match = re.fullmatch(r"/api/chat/threads/([^/]+)", parsed.path)
        if match and match.group(1).startswith("vol-"):
            thread_id = urllib.parse.unquote(match.group(1))
            with LOCK:
                before = len(THREADS)
                THREADS[:] = [item for item in THREADS if item["id"] != thread_id]
                after = len(THREADS)
            log(f"[archive] {thread_id} total {before}->{after}")
            return self._json({"id": thread_id, "archived": True})
        return self._proxy()

    def do_POST(self):
        return self._proxy()

    def do_PUT(self):
        return self._proxy()

    def do_PATCH(self):
        return self._proxy()


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"pagination proxy on http://127.0.0.1:{PORT} -> {BACKEND}", flush=True)
    server.serve_forever()