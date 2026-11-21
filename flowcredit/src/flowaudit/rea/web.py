"""Web 靶子事实提取：web 规则 v11 的确定性数据面。

输入是治理化快照目录（web_snapshot.py 产出：index.html + 同源资源 +
headers.json + PROVENANCE.md）。这里只做**确定性投影**——不联网、不执行
任何 JS、不调模型：

* third_party_scripts / missing_sri：HTML 里跨源 script 清单与 SRI 缺失；
* inline_event_handlers：HTML 内联 on* 属性（CSP-unsafe 模式）；
* eval_usage：出货 JS 里的 eval / new Function；
* sourcemap_refs：出货 JS 的 sourceMappingURL 引用；
* missing/present_security_headers：headers.json 的响应头事实（P1 采集）。

每条事实是 dict 行（file/line/name 键），与 REA 证据行同一形状——通用
vibe oracle 可直接判读。负数（阴性）事实为空列表：控制投影清零即可让
"总是报"的 oracle 露馅。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

__all__ = ["scan_web_surface"]

MAX_ROWS = 40
MAX_JS_BYTES = 4_000_000

_EVAL_RE = re.compile(r"\beval\s*\(|new\s+Function\s*\(")
_SOURCEMAP_RE = re.compile(r"[#@]\s*sourceMappingURL=([^\s*]+)")


class _PageScan(HTMLParser):
    """收集单页的 script 外链与内联事件属性。"""

    def __init__(self, file: str) -> None:
        super().__init__(convert_charrefs=True)
        self.file = file
        self.scripts: list[dict[str, Any]] = []
        self.inline_handlers: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if tag == "script" and attr_map.get("src"):
            self.scripts.append({
                "file": self.file,
                "line": line,
                "src": attr_map["src"][:300],
                "sri": bool(attr_map.get("integrity", "").strip()),
            })
        for name in attr_map:
            if name.startswith("on") and len(name) > 2:
                self.inline_handlers.append({
                    "file": self.file,
                    "line": line,
                    "snippet": f"<{tag} {name}=…>"[:80],
                })
                break


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def scan_web_surface(root: Path | str) -> dict[str, Any]:
    """对一个 web 快照目录做确定性 web 面投影（无网络、无执行）。"""
    root = Path(root)
    facts: dict[str, Any] = {
        "pages": 0,
        "third_party_scripts": [],
        "missing_sri": [],
        "inline_event_handlers": [],
        "eval_usage": [],
        "sourcemap_refs": [],
        "missing_security_headers": [],
        "present_security_headers": [],
        "runtime_third_party_origins": [],
        "runtime_console_errors": [],
    }
    # 站点自身 host 以 headers.json 的 source_url 为准（快照元数据）；
    # 缺失时保守处理：所有绝对 URL 都算第三方（静态快照引用外部源即为外部）。
    site_host = ""
    headers_path = root / "headers.json"
    headers_doc: dict[str, Any] = {}
    if headers_path.is_file():
        try:
            headers_doc = json.loads(headers_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            headers_doc = {}
        try:
            from urllib.parse import urlsplit
            site_host = urlsplit(str(headers_doc.get("source_url") or "")).netloc
        except ValueError:
            site_host = ""
    html_files = sorted(root.rglob("*.html"))[:20]
    for page in html_files:
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        facts["pages"] += 1
        scan = _PageScan(_rel(page, root))
        try:
            scan.feed(text)
        except Exception:  # noqa: BLE001 — 畸形 HTML 不阻断事实提取
            pass
        for script in scan.scripts:
            src = script["src"]
            match = re.match(r"^(?:https?:)?//([^/]+)/", src)
            if not match:
                continue  # 同源/相对资源不是第三方面
            origin = match.group(1)
            if site_host and origin == site_host:
                continue
            if len(facts["third_party_scripts"]) < MAX_ROWS:
                facts["third_party_scripts"].append({
                    "file": script["file"],
                    "src": src,
                    "origin": origin,
                    "sri": script["sri"],
                })
            if not script["sri"] and len(facts["missing_sri"]) < MAX_ROWS:
                facts["missing_sri"].append({
                    "file": script["file"],
                    "src": src,
                    "origin": origin,
                })
        facts["inline_event_handlers"].extend(scan.inline_handlers[: MAX_ROWS - len(facts["inline_event_handlers"])])

    for script_file in sorted(root.rglob("*.js"))[:60]:
        try:
            if script_file.stat().st_size > MAX_JS_BYTES:
                continue
            text = script_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(script_file, root)
        if len(facts["eval_usage"]) < MAX_ROWS:
            for number, line in enumerate(text.splitlines(), start=1):
                if _EVAL_RE.search(line):
                    facts["eval_usage"].append({
                        "file": rel, "line": number,
                        "snippet": line.strip()[:120],
                    })
                    if len(facts["eval_usage"]) >= MAX_ROWS:
                        break
        if len(facts["sourcemap_refs"]) < MAX_ROWS:
            for number, line in enumerate(text.splitlines(), start=1):
                found = _SOURCEMAP_RE.search(line)
                if found:
                    facts["sourcemap_refs"].append({
                        "file": rel, "line": number,
                        "target": found.group(1)[:200],
                    })
                    if len(facts["sourcemap_refs"]) >= MAX_ROWS:
                        break

    runtime_path = root / "runtime-evidence.json"
    if runtime_path.is_file():
        try:
            runtime_doc = json.loads(runtime_path.read_text(encoding="utf-8"))
            for row in runtime_doc.get("requests") or []:
                origin = str((row or {}).get("origin") or "")
                if not origin:
                    continue
                host = origin.split("://")[-1]
                if site_host and host == site_host:
                    continue
                if any(existing["origin"] == origin for existing in facts["runtime_third_party_origins"]):
                    continue
                if len(facts["runtime_third_party_origins"]) < MAX_ROWS:
                    facts["runtime_third_party_origins"].append({
                        "name": origin,
                        "origin": origin,
                        "resource": str((row or {}).get("resource") or ""),
                    })
            for text in (runtime_doc.get("console_errors") or [])[:MAX_ROWS]:
                facts["runtime_console_errors"].append({"name": f"console: {str(text)[:60]}", "snippet": str(text)[:200]})
        except (OSError, ValueError):
            pass

    if headers_doc:
        try:
            landing = {str(k).lower(): v for k, v in (headers_doc.get("landing_headers") or {}).items()}
            for name in ("content-security-policy", "strict-transport-security",
                         "x-frame-options", "x-content-type-options",
                         "referrer-policy", "permissions-policy"):
                row = {"name": name}
                if name in landing:
                    facts["present_security_headers"].append(row)
                else:
                    facts["missing_security_headers"].append(row)
        except (OSError, ValueError):
            pass
    return facts
