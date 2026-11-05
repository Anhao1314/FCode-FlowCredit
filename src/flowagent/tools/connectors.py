"""Host-configured sources: the only way a tool may touch the network.

The model never sees a URL. It names a source from a host-configured registry;
the registry owns the endpoint, credentials, timeout and size cap. That is the
same authority split the rest of the runtime uses: the model may propose *what*
to ask, never *where* to send it.

Two read paths:

* :meth:`ConnectorRegistry.fetch` returns a small raw payload for the existing
  ``fetch_source`` tool;
* :meth:`ConnectorRegistry.collect` fetches, converts HTML/JSON/text to plain
  text, and is paired with :class:`EvidenceStore` by ``collect_source`` so the
  corpus can cite line anchors and hashes.

Failure semantics are deliberately conservative:

* A timeout or transport failure raises ``CONNECTOR_TIMEOUT`` /
  ``CONNECTOR_UNKNOWN`` and is **not retried inside the tool**.
* Responses are capped in bytes before decoding; a source cannot exhaust memory.
* Credentials are resolved at call time and never copied into the result,
  observation or trace.
* Private/loopback destinations are refused unless the host explicitly marks a
  source as local (for fixtures and intranet feeds).

Connectors are disabled by default. ``demo-feed`` exists so the catalog is
deterministic and the refusal is demonstrable offline.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from ..core.contracts import AgentError

__all__ = ["Connector", "ConnectorRegistry", "default_registry", "load_registry"]

MAX_BYTES = 256 * 1024
COLLECT_MAX_BYTES = 2 * 1024 * 1024
CRAWL4AI_RESPONSE_MAX = 8 * 1024 * 1024
MAX_TEXT = 400_000
TEXT_EXCERPT = 4000

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


class _HTMLText(HTMLParser):
    """Turn HTML into readable text without a third-party parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave the configured host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if old.hostname != new.hostname or _effective_port(old) != _effective_port(new):
            raise AgentError(
                "CONNECTOR_REDIRECT_DENIED",
                f"redirect left {old.hostname!r}:{_effective_port(old)} for {new.hostname!r}:{_effective_port(new)}; not followed",
            )
        if old.scheme == "https" and new.scheme != "https":
            raise AgentError("CONNECTOR_REDIRECT_DENIED", "refusing an HTTPS downgrade")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Connector:
    name: str
    base_url: str
    enabled: bool = False
    timeout: float = 10.0
    max_bytes: int = MAX_BYTES
    query_param: str = "key"
    #: Resolved lazily on every call. Never placed in results or traces.
    credentials: Callable[[], Mapping[str, str]] | None = field(default=None, repr=False)
    description: str = ""
    #: Allow loopback/private destinations. Only for host-owned local fixtures.
    allow_private: bool = False
    #: "http" uses base_url directly; "crawl4ai" POSTs target_url to a sidecar.
    adapter: str = "http"
    target_url: str = ""
    browser_config: dict[str, Any] = field(default_factory=dict)
    crawler_config: dict[str, Any] = field(default_factory=dict)


class ConnectorRegistry:
    """Named sources; the model may only pick from :meth:`names`."""

    def __init__(self, connectors: list[Connector] | None = None) -> None:
        self._by_name = {connector.name: connector for connector in connectors or []}

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def get(self, name: str) -> Connector:
        connector = self._by_name.get(name)
        if connector is None:
            raise AgentError("CONNECTOR_UNKNOWN", f"{name!r} is not a configured source")
        return connector

    @staticmethod
    def _headers(connector: Connector, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "fcode-connector/1"}
        if connector.credentials is not None:
            headers.update({str(k): str(v) for k, v in connector.credentials().items()})
        return headers

    @staticmethod
    def _url(connector: Connector, key: str | None) -> str:
        url = connector.base_url
        if key:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode({connector.query_param: key})}"
        return url

    def _check_destination(self, connector: Connector, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise AgentError("CONNECTOR_URL_DENIED", "only http/https sources are allowed")
        if parsed.scheme == "http" and not connector.allow_private:
            raise AgentError("CONNECTOR_URL_DENIED", "plain HTTP is only allowed for local sources")
        if not parsed.hostname:
            raise AgentError("CONNECTOR_URL_DENIED", "source has no hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise AgentError("CONNECTOR_URL_DENIED", f"invalid port: {error}") from error
        try:
            infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise AgentError("CONNECTOR_UNKNOWN", f"cannot resolve {parsed.hostname!r}: {error}") from error
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global and not connector.allow_private:
                raise AgentError(
                    "CONNECTOR_HOST_DENIED",
                    f"{parsed.hostname!r} resolves to a non-public address; set allow_private only for host-owned sources",
                )

    def fetch(self, name: str, key: str | None = None) -> dict[str, Any]:
        connector = self.get(name)
        if not connector.enabled:
            raise AgentError(
                "CONNECTOR_DISABLED",
                f"{name!r} is configured but disabled by the host",
            )
        if connector.adapter != "http":
            raise AgentError(
                "CONNECTOR_ADAPTER_REQUIRES_COLLECT",
                f"{name!r} uses the {connector.adapter} adapter; call collect_source instead",
            )
        url = self._url(connector, key)
        headers = self._headers(connector, "application/json")
        request_id = uuid.uuid4().hex
        request = urllib.request.Request(url, headers=headers, method="GET")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=connector.timeout) as response:
                status = int(response.status)
                content_type = response.headers.get("Content-Type", "")
                data = response.read(connector.max_bytes + 1)
        except urllib.error.HTTPError as error:
            raise AgentError(
                "CONNECTOR_HTTP_ERROR",
                f"{name!r} answered HTTP {error.code}",
            ) from error
        except (TimeoutError, socket.timeout) as error:
            raise AgentError(
                "CONNECTOR_TIMEOUT",
                f"{name!r} timed out after {connector.timeout:.0f}s; not retried",
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise AgentError(
                    "CONNECTOR_TIMEOUT",
                    f"{name!r} timed out after {connector.timeout:.0f}s; not retried",
                ) from error
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{name!r} transport failed; outcome unknown, not retried: {error.reason}",
            ) from error
        except OSError as error:
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{name!r} transport failed; outcome unknown, not retried: {error}",
            ) from error
        if len(data) > connector.max_bytes:
            raise AgentError(
                "CONNECTOR_TOO_LARGE",
                f"{name!r} exceeded {connector.max_bytes} bytes",
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if "json" in content_type.lower():
            try:
                payload: dict[str, Any] = {"json": json.loads(data.decode("utf-8"))}
            except ValueError:
                payload = {"text": data.decode("utf-8", errors="replace")[:TEXT_EXCERPT]}
        else:
            payload = {"text": data.decode("utf-8", errors="replace")[:TEXT_EXCERPT]}
        return {
            "source": name,
            "url": url,
            "status": status,
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "payload": payload,
            "request_id": request_id,
            "latency_ms": latency_ms,
        }

    def collect(self, name: str, key: str | None = None) -> dict[str, Any]:
        """Fetch one named source and return plain text plus provenance.

        HTML is reduced to readable text, JSON is pretty-printed, and CSV/XML
        stay as text. The caller decides what to do with the text; usually it is
        ingested into :class:`EvidenceStore` so the model can search and cite it.
        """
        connector = self.get(name)
        if not connector.enabled:
            raise AgentError(
                "CONNECTOR_DISABLED",
                f"{name!r} is configured but disabled by the host",
            )
        if connector.adapter == "crawl4ai":
            return self._collect_crawl4ai(connector, key)
        url = self._url(connector, key)
        self._check_destination(connector, url)
        headers = self._headers(
            connector,
            "text/html, application/xhtml+xml, application/json, text/plain, text/csv, application/xml;q=0.9, */*;q=0.1",
        )
        request_id = uuid.uuid4().hex
        request = urllib.request.Request(url, headers=headers, method="GET")
        opener = urllib.request.build_opener(_SameHostRedirect())
        started = time.monotonic()
        try:
            with opener.open(request, timeout=connector.timeout) as response:
                status = int(response.status)
                content_type = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
                data = response.read(min(connector.max_bytes, COLLECT_MAX_BYTES) + 1)
        except AgentError:
            raise
        except urllib.error.HTTPError as error:
            raise AgentError("CONNECTOR_HTTP_ERROR", f"{name!r} answered HTTP {error.code}") from error
        except (TimeoutError, socket.timeout) as error:
            raise AgentError(
                "CONNECTOR_TIMEOUT",
                f"{name!r} timed out after {connector.timeout:.0f}s; not retried",
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise AgentError(
                    "CONNECTOR_TIMEOUT",
                    f"{name!r} timed out after {connector.timeout:.0f}s; not retried",
                ) from error
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{name!r} transport failed; outcome unknown, not retried: {error.reason}",
            ) from error
        except OSError as error:
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{name!r} transport failed; outcome unknown, not retried: {error}",
            ) from error
        cap = min(connector.max_bytes, COLLECT_MAX_BYTES)
        if len(data) > cap:
            raise AgentError("CONNECTOR_TOO_LARGE", f"{name!r} exceeded {cap} bytes")
        kind = _kind_for(content_type)
        if kind is None:
            raise AgentError(
                "CONNECTOR_CONTENT_TYPE",
                f"{name!r} returned unsupported content type {content_type!r}",
            )
        title, text = _extract(data, kind, charset)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT]
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "source": name,
            "adapter": "http",
            "url": url,
            "status": status,
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "title": title,
            "text": text,
            "request_id": request_id,
            "latency_ms": latency_ms,
        }

    def _collect_crawl4ai(self, connector: Connector, key: str | None) -> dict[str, Any]:
        """POST a host-owned target URL to a Crawl4AI sidecar.

        The sidecar does the browser work; the model still never chooses the URL.
        The target is part of the source configuration, while ``key`` only
        appends a query parameter to that configured target.
        """
        if not connector.target_url:
            raise AgentError("CONNECTOR_CONFIG", "crawl4ai source needs target_url")
        endpoint = connector.base_url
        self._check_destination(connector, endpoint)
        target = connector.target_url
        if key:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{urlencode({connector.query_param: key})}"
        self._check_destination(connector, target)
        browser = {"headless": True}
        browser.update(connector.browser_config or {})
        crawler = {"stream": False, "cache_mode": "bypass"}
        crawler.update(connector.crawler_config or {})
        crawler["stream"] = False
        payload = {
            "urls": [target],
            "browser_config": {"type": "BrowserConfig", "params": browser},
            "crawler_config": {"type": "CrawlerRunConfig", "params": crawler},
        }
        headers = self._headers(connector, "application/json")
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        request_id = uuid.uuid4().hex
        opener = urllib.request.build_opener(_SameHostRedirect())
        started = time.monotonic()
        try:
            with opener.open(request, timeout=connector.timeout) as response:
                status = int(response.status)
                data = response.read(CRAWL4AI_RESPONSE_MAX + 1)
        except AgentError:
            raise
        except urllib.error.HTTPError as error:
            raise AgentError("CONNECTOR_HTTP_ERROR", f"{connector.name!r} sidecar answered HTTP {error.code}") from error
        except (TimeoutError, socket.timeout) as error:
            raise AgentError(
                "CONNECTOR_TIMEOUT",
                f"{connector.name!r} sidecar timed out after {connector.timeout:.0f}s; not retried",
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise AgentError(
                    "CONNECTOR_TIMEOUT",
                    f"{connector.name!r} sidecar timed out after {connector.timeout:.0f}s; not retried",
                ) from error
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{connector.name!r} sidecar transport failed; outcome unknown, not retried: {error.reason}",
            ) from error
        except OSError as error:
            raise AgentError(
                "CONNECTOR_UNKNOWN",
                f"{connector.name!r} sidecar transport failed; outcome unknown, not retried: {error}",
            ) from error
        if len(data) > CRAWL4AI_RESPONSE_MAX:
            raise AgentError("CONNECTOR_TOO_LARGE", f"{connector.name!r} sidecar response exceeded {CRAWL4AI_RESPONSE_MAX} bytes")
        try:
            response_payload = json.loads(data.decode("utf-8"))
        except ValueError as error:
            raise AgentError("CONNECTOR_BAD_RESPONSE", f"{connector.name!r} sidecar returned non-JSON") from error
        results = response_payload.get("results") if isinstance(response_payload, dict) else None
        if not isinstance(results, list) or not results:
            raise AgentError("CONNECTOR_BAD_RESPONSE", f"{connector.name!r} sidecar returned no results")
        item = results[0] if isinstance(results[0], dict) else {}
        if not item.get("success", False):
            error_message = str(item.get("error_message") or "crawl failed")
            raise AgentError("CONNECTOR_UPSTREAM_ERROR", f"{connector.name!r}: {error_message[:300]}")
        markdown = item.get("markdown")
        if isinstance(markdown, dict):
            text = (markdown.get("fit_markdown") or markdown.get("raw_markdown")
                    or markdown.get("markdown_with_citations") or "")
        elif isinstance(markdown, str):
            text = markdown
        else:
            text = item.get("fit_markdown") or ""
        text = str(text)
        if not text.strip() and item.get("html"):
            _, text = _extract(str(item["html"]).encode("utf-8"), "html", "utf-8")
        if not text.strip():
            raise AgentError("EVIDENCE_EMPTY", f"{connector.name!r} sidecar returned no readable text")
        encoded = text.encode("utf-8")
        cap = min(connector.max_bytes, COLLECT_MAX_BYTES)
        if len(encoded) > cap:
            raise AgentError("CONNECTOR_TOO_LARGE", f"{connector.name!r} sidecar text exceeded {cap} bytes")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "source": connector.name,
            "adapter": "crawl4ai",
            "url": target,
            "status": status,
            "content_type": "text/markdown",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "markdown",
            "title": str(metadata.get("title") or item.get("title") or ""),
            "text": text,
            "request_id": request_id,
            "latency_ms": latency_ms,
        }


def _effective_port(parsed) -> int:
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _kind_for(content_type: str) -> str | None:
    lowered = (content_type or "").lower()
    if "html" in lowered:
        return "html"
    if "json" in lowered:
        return "json"
    if "csv" in lowered:
        return "csv"
    if lowered.startswith("text/") or "xml" in lowered:
        return "text"
    if not lowered:
        return "text"
    return None


def _extract(data: bytes, kind: str, charset: str) -> tuple[str, str]:
    text = data.decode(charset or "utf-8", errors="replace")
    if kind == "html":
        parser = _HTMLText()
        try:
            parser.feed(text)
            parser.close()
        except Exception:  # noqa: BLE001 — malformed HTML is still evidence
            return "", text
        return parser.title.strip(), parser.text()
    if kind == "json":
        try:
            return "", json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            return "", text
    return "", text


def _demo_registry() -> ConnectorRegistry:
    """Deterministic catalog, network closed: one configured-but-disabled source."""
    return ConnectorRegistry([
        Connector(
            name="demo-feed",
            base_url="http://127.0.0.1:9/feed",
            enabled=False,
            description="disabled demo feed",
            allow_private=True,
        ),
    ])


def load_registry(path: str | Path | None = None) -> ConnectorRegistry:
    """Load host-owned sources from JSON, or fall back to the demo catalog.

    ``FCODE_SOURCES`` points at a JSON file::

        {"sources": [{"name": "filings", "base_url": "https://...",
                      "enabled": true, "token_env": "FILINGS_TOKEN",
                      "query_param": "q", "max_bytes": 1048576}]}

    Credentials are read from the environment at call time; the token is never
    copied into the tool result or the trace.
    """
    target = path or os.environ.get("FCODE_SOURCES")
    if not target:
        return _demo_registry()
    data = json.loads(Path(target).expanduser().read_text(encoding="utf-8"))
    items = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("FCODE_SOURCES must contain a list of sources")
    connectors: list[Connector] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name") or not item.get("base_url"):
            raise ValueError("each source needs name and base_url")
        base_headers = {str(k): str(v) for k, v in (item.get("headers") or {}).items()}
        token_env = item.get("token_env")
        token_header = str(item.get("token_header") or "Authorization")
        token_prefix = str(item.get("token_prefix") if item.get("token_prefix") is not None else "Bearer ")

        if token_env:
            def credentials(env=token_env, header=token_header, prefix=token_prefix, base=base_headers):
                headers = dict(base)
                token = os.environ.get(str(env), "")
                if token:
                    headers[header] = f"{prefix}{token}"
                return headers
        elif base_headers:
            def credentials(base=base_headers):
                return dict(base)
        else:
            credentials = None

        adapter = str(item.get("adapter", "http")).lower()
        if adapter not in ("http", "crawl4ai"):
            raise ValueError(f"unsupported adapter {adapter!r} for source {item['name']!r}")
        target_url = str(item.get("target_url", ""))
        if adapter == "crawl4ai" and not target_url:
            raise ValueError(f"crawl4ai source {item['name']!r} needs target_url")
        connectors.append(Connector(
            name=str(item["name"]),
            base_url=str(item["base_url"]),
            enabled=bool(item.get("enabled", False)),
            timeout=float(item.get("timeout", 10.0)),
            max_bytes=int(item.get("max_bytes", COLLECT_MAX_BYTES)),
            query_param=str(item.get("query_param", "key")),
            credentials=credentials,
            description=str(item.get("description", "")),
            allow_private=bool(item.get("allow_private", False)),
            adapter=adapter,
            target_url=target_url,
            browser_config=dict(item.get("browser_config") or {}),
            crawler_config=dict(item.get("crawler_config") or {}),
        ))
    return ConnectorRegistry(connectors)


def default_registry() -> ConnectorRegistry:
    return load_registry()
