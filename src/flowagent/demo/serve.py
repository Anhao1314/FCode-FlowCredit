"""Serve the viewer with the standard library, nothing else.

A static file server is all this needs: the page is plain static files and the
data is already on disk as JSON. ``http.server`` is in the standard library, so
serving the viewer costs no dependency and no build step — the same constraint
the rest of the project is under.

Bound to 127.0.0.1 on purpose. This is a local demo, not a deployment.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
import webbrowser
from pathlib import Path

__all__ = ["serve"]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Same handler, without a log line per asset request."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


def serve(root: Path, *, port: int = 0, open_browser: bool = True) -> int:
    """Serve *root* and print the viewer URL. Blocks until interrupted."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        actual = httpd.server_address[1]
        url = f"http://127.0.0.1:{actual}/viewer/index.html"
        print(f" serving  {root}")
        print(f" viewer   {url}")
        print(" ctrl-c   to stop")
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n stopped")
    return 0
