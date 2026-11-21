"""P1：网站靶子的治理化快照器。

把"操作员手工 curl"变成宿主的类型化步骤。治理语义：

1. **robots 合规先行**：先取 /robots.txt，解析 Disallow 前缀；目标路径被拒则
   快照拒绝（fail-closed），合规结论写进溯源；
2. **低请求量**：落地页 1 次 + 同源静态资源（≤ max_assets，js/css/mjs），单轮、
   无递归、无深度爬取；每次失败如实记录，不静默；
3. **HTTP 层事实**：落地页与各资源的响应头全量入快照（headers.json）——
   web 攻击面最大的一块（CSP/HSTS/XFO/...）从此进审计范围；OWASP Secure
   Headers 基准的缺项判定写进溯源摘要；
4. **溯源自动入链**：PROVENANCE.md（人话）+ headers.json（事实）落在快照
   目录里，审计 manifest 会对目录全量哈希——溯源随链，不需要人手写。

纯 stdlib，无第三方依赖。外部扫描刀（retire/gitleaks）见 web_scanners.py。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
import shutil
import subprocess
import time
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

__all__ = ["snapshot_url", "SECURITY_HEADERS"]

# 隐私姿态：默认 UA 用通用浏览器串——对外不主动声称审计者身份（保护操作员
# 隐私）；但仍然尊重 robots、低请求量、不伪造他人身份/登录态。审计记录对
# 操作员全透明（PROVENANCE 里如实写出口与 UA 策略）。可用 FCODE_AUDIT_UA 覆盖。
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")


def audit_user_agent() -> str:
    import os
    return os.environ.get("FCODE_AUDIT_UA", "").strip() or DEFAULT_UA


def audit_proxy() -> str:
    """出口代理（http/https/socks5）。空 = 直连。egress 策略属于宿主参数。"""
    import os
    return (os.environ.get("FCODE_AUDIT_PROXY", "").strip()
            or os.environ.get("HTTPS_PROXY", "").strip()
            or os.environ.get("https_proxy", "").strip()
            or "")


def _browser_headers(kind: str = "document") -> dict[str, str]:
    """浏览器级请求头（保真模式）：与普通 Chrome 的 GET 一致。

    不做的事：不伪造他人身份/会话，不做 TLS 指纹对抗。目的有二——
    保护操作员隐私，且避免目标按"审计特征流量"返回差异内容导致审计失真。
    """
    common = {
        "User-Agent": audit_user_agent(),
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="127", "Not)A;Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    if kind == "document":
        return {
            **common,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
    accept = ("text/css,*/*;q=0.1" if kind == "style"
              else "*/*")
    dest = "style" if kind == "style" else "script"
    return {
        **common,
        "Accept": accept,
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }


# 反自动化挑战标记：命中 = 目标部署了 bot 防护。按治理口径**如实记录**
# （覆盖受限的事实），绝不尝试破解。
CHALLENGE_MARKERS = (
    "cf-chl", "captcha", "just a moment", "attention required",
    "enable javascript and cookies", "challenge-platform", "turnstile",
    "verify you are human",
)


def detect_challenge(status: int | None, body: bytes) -> str:
    """返回命中的挑战标记（空串 = 无挑战）。挑战是审计事实，不是要打败的敌人。"""
    if status in (403, 429, 503):
        return f"http-{status}"
    text = body[:20000].decode("utf-8", errors="ignore").lower()
    return next((marker for marker in CHALLENGE_MARKERS if marker in text), "")


_PAGE_RE = re.compile(r'href\s*=\s*["\'](/[^"\'#?]*)(?:[?#][^"\']*)?["\']', re.I)
SKIP_PREFIXES = ("/api/", "/account", "/login", "/logout", "/signin",
                 "/signup", "/auth", "/_next/", "/static/")


class _LinkScan(HTMLParser):
    """只认 <a href> 的站内路径——<link href> 的 CSS/字体资源不是页面种子。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name.lower() != "href" or not value:
                continue
            href = value.strip()
            if not href.startswith("/") or href.startswith("//"):
                return
            clean = href.split("#")[0].split("?")[0].rstrip("/") or "/"
            if clean not in self.paths:
                self.paths.append(clean)


def _same_origin_links(body: bytes, *, cap: int = 12) -> list[str]:
    """落地页里的同源 <a href> 路径（深度 1 种子；去重、去查询串）。

    不做前缀过滤——分类交给抓取循环（每个跳过都带原因记录，覆盖声明才诚实）。
    """
    scan = _LinkScan()
    try:
        scan.feed(body.decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001 — 畸形 HTML 不阻断
        pass
    return scan.paths[:cap]


def anchor_path(snapshot_dir: Path) -> Path:
    """锚点 = 密文外的兄弟文件（只含哈希，加密快照仍可 diff）。"""
    return snapshot_dir.with_name(snapshot_dir.name + ".anchor.json")


def read_anchor(snapshot_dir: Path) -> dict[str, Any] | None:
    path = anchor_path(snapshot_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def diff_anchors(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """两级变化视图的内容层：页面/资产哈希对比。只报路径与哈希，不含内容。"""
    old_pages = old.get("pages") or {}
    new_pages = new.get("pages") or {}
    changed = sorted(p for p in (set(old_pages) & set(new_pages))
                     if old_pages[p] != new_pages[p])
    added = sorted(set(new_pages) - set(old_pages))
    removed = sorted(set(old_pages) - set(new_pages))
    old_assets = int((old.get("assets") or {}).get("count") or 0)
    new_assets = int((new.get("assets") or {}).get("count") or 0)
    old_bytes = int((old.get("assets") or {}).get("bytes") or 0)
    new_bytes = int((new.get("assets") or {}).get("bytes") or 0)
    return {
        "since": str(old.get("fetched_at") or ""),
        "pages_changed": changed,
        "pages_added": added,
        "pages_removed": removed,
        "assets_delta": new_assets - old_assets,
        "bytes_delta": new_bytes - old_bytes,
        "changed": bool(changed or added or removed
                        or new_assets != old_assets or new_bytes != old_bytes),
    }


def _opener():
    """按出口策略构造 urllib opener。直连或走代理，均不改变请求形态。"""
    proxy = audit_proxy()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()
MAX_ASSETS_DEFAULT = 40
FETCH_TIMEOUT = 25

# OWASP Secure Headers 基准（应存在的安全响应头）——缺项判定写进溯源。
SECURITY_HEADERS = (
    "content-security-policy",
    "strict-transport-security",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
)

_ASSET_RE = re.compile(r'(?:src|href)\s*=\s*["\'](/[^"\']+\.(?:js|mjs|css))(?:\?[^"\']*)?["\']', re.I)


def _fetch(url: str, *, timeout: int = FETCH_TIMEOUT, kind: str = "document") -> tuple[int | None, dict[str, str], bytes]:
    """单次 GET。返回 (status, headers, body)；网络错误 status=None。"""
    request = urllib.request.Request(url, headers=_browser_headers(kind))
    try:
        with _opener().open(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, b""
    except OSError:
        return None, {}, b""


def _robots_allowed(url: str) -> dict[str, Any]:
    """robots.txt 合规判定。取不到 robots.txt = 允许（公开约定），如实记录。"""
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    status, _headers, body = _fetch(robots_url, timeout=15)
    record: dict[str, Any] = {"url": robots_url, "status": status, "path_allowed": True}
    if status != 200 or not body:
        record["note"] = "robots.txt 不可读——按公开约定允许，已如实记录"
        return record
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    record["path_allowed"] = parser.can_fetch(audit_user_agent(), url)
    return record


def _capture_runtime(url: str, out: Path) -> dict[str, Any]:
    """运行时证据（fail-soft）：node + playwright 可用时采集网络清单/console/
    storage 键名；任何缺失如实降级，不阻断快照与审计。"""
    script = Path(__file__).resolve().parent / "web_runtime.mjs"
    node = shutil.which("node")
    if node is None or not script.is_file():
        return {"status": "unavailable", "reason": "node or web_runtime.mjs missing"}
    target = out / "runtime-evidence.json"
    try:
        proc = subprocess.run(
            [node, str(script), url, str(target)],
            capture_output=True, text=True, timeout=90,
        )
        if target.is_file():
            doc = json.loads(target.read_text(encoding="utf-8"))
            return {
                "status": str(doc.get("status") or "ok"),
                "origins": sorted({row.get("origin") for row in doc.get("requests") or [] if row.get("origin")}),
                "console_errors": len(doc.get("console_errors") or []),
                "reason": str(doc.get("reason") or "")[:160],
            }
        return {"status": "error", "reason": (proc.stderr or "no output")[:160]}
    except Exception as error:  # noqa: BLE001 — 运行时证据是增强面，不是前提
        return {"status": "error", "reason": f"{type(error).__name__}: {error}"[:160]}


def snapshot_url(
    url: str,
    out_dir: Path | str,
    *,
    max_assets: int = MAX_ASSETS_DEFAULT,
    max_pages: int = 3,
    runtime: bool = True,
) -> dict[str, Any]:
    """治理化快照：把一个 http(s) URL 变成审计管线可用的本地靶子目录。

    返回摘要（也写进快照目录的 headers.json / PROVENANCE.md）。
    robots 拒绝 / 非 http(s) / 落地页不可达 → ValueError，不产出半成品目录。
    """
    url = str(url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"target must be an http(s) URL, got: {url[:120]!r}")

    robots = _robots_allowed(url)
    if not robots["path_allowed"]:
        raise ValueError(f"robots.txt 拒绝该路径：{url}")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    status, headers, body = _fetch(url)
    challenge = detect_challenge(status, body)
    if challenge:
        raise ValueError(
            f"目标返回反自动化挑战（{challenge}）——审计覆盖受限，如实失败而非破解。url={url}")
    if status != 200 or not body:
        raise ValueError(f"落地页不可达：HTTP {status} for {url}")
    (out / "index.html").write_bytes(body)

    # 部署锚点：重爬前先读旧锚点（密文外的兄弟文件，含哈希不含内容）。
    previous_anchor = read_anchor(out)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    robots_parser = urllib.robotparser.RobotFileParser()
    robots_allowed_paths: list[str] = []
    robots_skipped: list[dict[str, str]] = []
    robots_status, _rh, robots_body = _fetch(robots["url"], timeout=15)
    if robots_status == 200 and robots_body:
        robots_parser.parse(robots_body.decode("utf-8", errors="replace").splitlines())

    def path_allowed(path: str) -> bool:
        if not robots_body:
            return True
        return robots_parser.can_fetch(audit_user_agent(), urljoin(url, path))

    # 子页面（深度 1）：落地页链接 + 逐路径 robots + 限速；上限 max_pages。
    pages_crawled = [out / "index.html"]
    page_bytes = body
    if max_pages > 1:
        # 检查全部候选（跳过的都记录原因），只抓够 max_pages。
        for path in _same_origin_links(body):
            if any(path.startswith(prefix) for prefix in SKIP_PREFIXES):
                robots_skipped.append({"path": path, "reason": "sensitive-prefix"})
                continue
            if not path_allowed(path):
                robots_skipped.append({"path": path, "reason": "robots"})
                continue
            if len(pages_crawled) >= max_pages:
                robots_skipped.append({"path": path, "reason": "page-cap"})
                continue
            time.sleep(0.8)  # 限速：低请求量承诺不变
            status_p, _h, body_p = _fetch(urljoin(url, path), kind="document")
            challenge_p = detect_challenge(status_p, body_p)
            if challenge_p:
                robots_skipped.append({"path": path, "reason": f"challenge:{challenge_p}"})
                break
            if status_p != 200 or not body_p:
                robots_skipped.append({"path": path, "reason": f"http-{status_p}"})
                continue
            target = out / path.lstrip("/")
            # 统一落成 .html：web 事实扫描只认 *.html，无扩展名的干净路由
            # 会漏掉页面级事实（且保持路径语义）。
            if target.suffix.lower() not in (".html", ".htm"):
                target = target.with_suffix(".html")
            if target.is_dir() or target == out / "index.html":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body_p)
            pages_crawled.append(target)

    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in _ASSET_RE.findall(body.decode("utf-8", errors="replace"))[: max_assets + 8]:
        asset_url = urljoin(url, path)
        if asset_url in seen or len(seen) >= max_assets:
            continue
        seen.add(asset_url)
        asset_kind = "style" if asset_url.split("?")[0].endswith(".css") else "script"
        asset_status, asset_headers, asset_body = _fetch(asset_url, kind=asset_kind)
        entry = {
            "url": asset_url,
            "status": asset_status,
            "bytes": len(asset_body),
            "content_type": asset_headers.get("content-type", ""),
        }
        assets.append(entry)
        if asset_status == 200 and asset_body:
            target = out / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(asset_body)

    missing_security_headers = [h for h in SECURITY_HEADERS if h not in headers]
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runtime_summary = _capture_runtime(url, out) if runtime else {"status": "skipped"}
    summary = {
        "source_url": url,
        "runtime": runtime_summary,
        "fetched_at": fetched_at,
        "user_agent": audit_user_agent(),
        "egress": "proxy" if audit_proxy() else "direct",
        "mode": "fidelity（浏览器级请求头；不破解挑战、不伪造身份）",
        "robots": robots,
        "landing": {"status": status, "bytes": len(body)},
        "assets": {"requested": len(seen), "fetched": sum(1 for a in assets if a["status"] == 200)},
        "security_headers": {
            "present": [h for h in SECURITY_HEADERS if h in headers],
            "missing": missing_security_headers,
        },
        "pages": {
            "crawled": [str(p.relative_to(out)) for p in pages_crawled],
            "skipped": robots_skipped[:8],
            "discovered": len(_same_origin_links(body)),
        },
        "note": "只覆盖本快照内的客户端静态资源；传输层与运行时行为以 headers.json/后续证据为准",
    }
    anchor = {
        "source_url": url,
        "fetched_at": fetched_at,
        "pages": {
            str(p.relative_to(out)): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
            for p in pages_crawled
        },
        "assets": {"count": len(seen), "bytes": sum(int(a.get("bytes") or 0) for a in assets)},
    }
    anchor_path(out).write_text(
        json.dumps(anchor, ensure_ascii=False, indent=1), encoding="utf-8")
    if previous_anchor:
        change = diff_anchors(previous_anchor, anchor)
        (out.with_name(out.name + ".snapshot-diff.json")).write_text(
            json.dumps(change, ensure_ascii=False, indent=1), encoding="utf-8")
        summary["change_since_last"] = change
    (out / "headers.json").write_text(
        json.dumps({"source_url": url, "landing_headers": headers, "assets": assets}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (out / "PROVENANCE.md").write_text(
        "# 快照来源说明（宿主自动生成）\n\n"
        f"- 来源 URL：{url}\n- 抓取时间：{fetched_at}（UTC）\n"
        f"- 抓取方式：落地页 1 次 + 同源静态资源 {len(seen)} 次（单轮、无递归）\n"
        f"- robots：{'允许' if robots['path_allowed'] else '拒绝'}（{robots['url']}，HTTP {robots['status']}）\n"
        f"- 出口：{'代理' if audit_proxy() else '直连'}；UA：{audit_user_agent()[:60]}…（对目标低调，对本记录透明）\n"
        f"- 安全响应头缺项：{', '.join(missing_security_headers) or '无'}\n"
        f"- 页面覆盖：抓取 {len(pages_crawled)} 页"
        f"（发现 {len(_same_origin_links(body))} 条站内链接，跳过 {len(robots_skipped)} 条："
        f"{', '.join(sorted({s['reason'] for s in robots_skipped})) or '无'}）\n"
        "- 用途：本地静态安全审计；非内容采集、非模型训练\n"
        "- 范围声明：只覆盖快照内的客户端静态资源，不代表服务端安全状况\n",
        encoding="utf-8",
    )
    return summary
