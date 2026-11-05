"""fetch_url: a model-addressable web read, SSRF-guarded end to end.

Unlike the host-configured connectors (where the model only names a source),
``fetch_url`` accepts a URL the model chose — so the guard must hold for
*arbitrary* destinations:

* **Resolve-then-pin.** The hostname is resolved, every address is checked
  against the private/loopback policy, and the connection is then made to the
  *validated IP* — never re-resolved. A DNS rebinding answer that flips
  between the check and the connect has nothing to flip: the checked address
  is the connected address. TLS keeps SNI and certificate verification on the
  original hostname while the socket talks to the pinned IP.
* **Bounded.** 30-second timeout, 1 MiB body cap, at most three redirects,
  each hop re-validated from scratch.
* **Honest.** A pre-dispatch refusal (bad scheme, private address) is a
  confirmed denial. A dispatched call that times out or dies mid-flight is an
  *unknown outcome* — the request left, no answer returned — and is marked as
  such so the runtime never retries it.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ...core.contracts import AgentError
from ..connectors import _extract, _kind_for

__all__ = ["UrlFetcher", "FETCH_MAX_BYTES", "FETCH_TIMEOUT_SECONDS", "MAX_REDIRECTS"]

FETCH_MAX_BYTES = 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pinned IP; SNI and certificate checks stay on the hostname."""

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(ip, port, timeout=timeout, context=ssl.create_default_context())
        self._sni_host = host

    def connect(self) -> None:  # noqa: D102 - see http.client
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._sni_host)


class UrlFetcher:
    """Fetches one URL per call under the SSRF policy above."""

    def __init__(
        self,
        *,
        timeout: float = FETCH_TIMEOUT_SECONDS,
        max_bytes: int = FETCH_MAX_BYTES,
        allow_hosts: tuple[str, ...] = (),
        resolver: Callable[[str, int], list[str]] | None = None,
        transport: Callable[..., tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        #: Hosts the host explicitly opened to private addresses (fixtures,
        #: intranet feeds). Exact names or dot-suffixes; never a wildcard.
        self.allow_hosts = tuple(allow_hosts)
        self._resolver = resolver or self._resolve_dns
        self._transport = transport or self._request

    # ------------------------------------------------------------------ public

    def fetch(self, url: str) -> dict[str, Any]:
        started = time.monotonic()
        current = url
        final: dict[str, Any] | None = None
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port, target = self._validate(current)
            ip = self._pick_address(host, port, target)
            status, headers, data = self._dispatch(scheme, host, ip, port, target)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location") or headers.get("location")
                if not location:
                    final = self._result(current, host, status, headers, data, started)
                    break
                current = self._join_redirect(current, location)
                continue
            final = self._result(current, host, status, headers, data, started)
            break
        else:  # pragma: no cover - loop guard below is the honest exit
            raise AgentError("FETCH_REDIRECT_LIMIT", f"{url!r} exceeded {MAX_REDIRECTS} redirects")
        if final is None:  # pragma: no cover - defensive
            raise AgentError("FETCH_UNKNOWN", "no response was produced")
        return final

    # --------------------------------------------------------------- validation

    def _validate(self, url: str) -> tuple[str, str, int, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise AgentError("FETCH_URL_DENIED", "only http/https URLs are fetchable")
        host = parsed.hostname
        if not host:
            raise AgentError("FETCH_URL_DENIED", "the URL has no hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise AgentError("FETCH_URL_DENIED", f"invalid port: {error}") from error
        if not 1 <= port <= 65535:
            raise AgentError("FETCH_URL_DENIED", f"invalid port {port}")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return parsed.scheme, host, port, target

    def _pick_address(self, host: str, port: int, target: str) -> str:
        """Resolve, check *every* address, and pin the first public one."""
        try:
            addresses = list(dict.fromkeys(self._resolver(host, port)))
        except (socket.gaierror, OSError) as error:
            # Nothing was dispatched: this is a confirmed failure, not unknown.
            raise AgentError("FETCH_UNRESOLVED", f"cannot resolve {host!r}: {error}") from error
        if not addresses:
            raise AgentError("FETCH_UNRESOLVED", f"{host!r} has no addresses")
        if not self._host_allowed(host):
            for raw in addresses:
                address = ipaddress.ip_address(raw)
                if not address.is_global:
                    raise AgentError(
                        "FETCH_HOST_DENIED",
                        f"{host!r} resolves to the non-public address {raw}; "
                        "private/loopback destinations are refused",
                    )
        return addresses[0]

    def _host_allowed(self, host: str) -> bool:
        lowered = host.lower()
        return any(
            lowered == entry.lower() or lowered.endswith("." + entry.lower())
            for entry in self.allow_hosts
        )

    @staticmethod
    def _resolve_dns(host: str, port: int) -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, scheme: str, host: str, ip: str, port: int, target: str):
        try:
            return self._transport(scheme, host, ip, port, target)
        except AgentError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise AgentError(
                "FETCH_TIMEOUT",
                f"{host!r} timed out after {self.timeout:.0f}s; the request was dispatched, "
                "no answer returned — outcome unknown, not retried",
                details={"outcome": "unknown"},
            ) from error
        except (http.client.HTTPException, OSError) as error:
            raise AgentError(
                "FETCH_UNKNOWN",
                f"transport to {host!r} failed mid-flight: {error}; outcome unknown, not retried",
                details={"outcome": "unknown"},
            ) from error

    def _request(self, scheme: str, host: str, ip: str, port: int, target: str):
        authority = host if port in (80, 443) else f"{host}:{port}"
        headers = {
            "Host": authority,
            "Accept": "text/html, application/json, text/plain, text/csv, application/xml;q=0.9, */*;q=0.1",
            "User-Agent": "fcode-fetch/1",
        }
        if scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(host, ip, port, self.timeout)
        else:
            connection = http.client.HTTPConnection(ip, port, timeout=self.timeout)
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            data = response.read(self.max_bytes + 1)
            return int(response.status), {k: v for k, v in response.getheaders()}, data
        finally:
            connection.close()

    # ------------------------------------------------------------------ result

    def _result(
        self,
        url: str,
        host: str,
        status: int,
        headers: dict[str, str],
        data: bytes,
        started: float,
    ) -> dict[str, Any]:
        if len(data) > self.max_bytes:
            raise AgentError(
                "FETCH_TOO_LARGE",
                f"{host!r} exceeded the {self.max_bytes}-byte cap",
            )
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        charset = "utf-8"
        for part in content_type.split(";"):
            if part.strip().startswith("charset="):
                charset = part.strip().split("=", 1)[1].strip("\"' ") or "utf-8"
        kind = _kind_for(content_type)
        if kind is None:
            raise AgentError(
                "FETCH_CONTENT_TYPE",
                f"{host!r} returned unsupported content type {content_type!r}",
            )
        title, text = _extract(data, kind, charset)
        return {
            "url": url,
            "host": host,
            "status": int(status),
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "title": title,
            "text": text,
            "content_truth": "not-verified",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def _join_redirect(base: str, location: str) -> str:
        if not location:
            raise AgentError("FETCH_URL_DENIED", "redirect without a Location header")
        split = urlsplit(location)
        if split.scheme:
            if split.scheme not in ("http", "https"):
                raise AgentError("FETCH_URL_DENIED", f"redirect to {split.scheme!r} refused")
            return urlunsplit(split)
        root = urlsplit(base)
        return urlunsplit((root.scheme, root.netloc, split.path or "/", split.query, ""))
