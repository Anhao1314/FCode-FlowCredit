# 数据采集：主机配置源，模型只点名

`collect_source` 是 agent 的“外部数据工具手”。它不接收 URL，只接收一个
**主机配置过的源名**；源地址、凭证、超时、大小上限和私网策略全部由主机持有。
抓取结果会转成纯文本并写入 EvidenceStore，后续可以直接 `search_corpus`、
`read_slice`，引用时带行号、内容哈希和来源元数据。

## 配置

凭证不要放进 `base_url`（它可能出现在结果里）；用 `token_env` 从环境变量注入。

服务器启动时用 `FCODE_SOURCES` 指向一个 JSON 文件：

```json
{
  "sources": [
    {
      "name": "filings",
      "description": "公司公告与财报 API",
      "base_url": "https://api.example.com/v1/filings",
      "enabled": true,
      "query_param": "q",
      "token_env": "FILINGS_TOKEN",
      "token_header": "Authorization",
      "token_prefix": "Bearer ",
      "timeout": 10,
      "max_bytes": 1048576
    },
    {
      "name": "local-fixture",
      "base_url": "http://127.0.0.1:8080/page",
      "enabled": true,
      "allow_private": true
    }
  ]
}
```

```sh
FCODE_SOURCES=/etc/fcode/sources.json python3 flowcredit/viewer/server.py --repo .
```

没有配置时，目录里只有默认关闭的 `demo-feed`；调用会得到
`CONNECTOR_DISABLED`，网络不会真的打开。

## Crawl4AI 侧车（JS 页面 / PDF / Markdown）

对需要浏览器渲染的页面，不要把 Chromium 塞进 FCode 运行时，而是单独跑一个
Crawl4AI（Apache-2.0）侧车：

```sh
docker run -d --name crawl4ai -p 11235:11235 --shm-size=1g \
  unclecode/crawl4ai:latest
```

然后把它配置成一个源：

```json
{
  "name": "crawl-report",
  "adapter": "crawl4ai",
  "base_url": "http://127.0.0.1:11235/crawl",
  "target_url": "https://example.com/report",
  "enabled": true,
  "allow_private": true,
  "token_env": "CRAWL4AI_JWT",
  "timeout": 90,
  "max_bytes": 2097152,
  "crawler_config": {"cache_mode": "bypass"},
  "browser_config": {"headless": true}
}
```

- `target_url` 由主机配置，模型仍然只能传 `source` 和可选 `query`；
- Crawl4AI 源只走 `collect_source`；`fetch_source` 会以 `CONNECTOR_ADAPTER_REQUIRES_COLLECT` 拒绝，避免把侧车当成普通 feed；
- `adapter` 默认 `http`；设为 `crawl4ai` 后，连接器向侧车发 `POST /crawl`，
  请求体是 `{urls, browser_config, crawler_config}`；
- 侧车返回的 Markdown 会作为 Evidence 落库，`kind=markdown`，后续同样可
  `search_corpus` / `read_slice`；
- `allow_private: true` 只用于访问本机/内网侧车；主机配置本身是可信边界。
  公网目标仍然建议 HTTPS，并把侧车的 SSRF 防护和 JWT 保持开启；
- 侧车不可达、返回 `success:false`、没有 `results` 时，分别得到
  `CONNECTOR_UNKNOWN` / `CONNECTOR_UPSTREAM_ERROR` / `CONNECTOR_BAD_RESPONSE`，
  不会自动重试。

Crawl4AI 的 `/crawl` 响应可能包含 Markdown 对象或字符串，连接器两者都兼容；
`markdown.fit_markdown` / `raw_markdown` 优先，缺失时回退到 HTML 转文本。

## 工具调用

```json
{"source": "filings", "query": "恒星智算 FY2025"}
```

返回：

```json
{
  "source": "filings",
  "url": "https://api.example.com/v1/filings?q=...",
  "status": 200,
  "content_type": "text/html; charset=utf-8",
  "retrieved_at": "2026-10-21T...",
  "sha256": "...",
  "bytes": 18342,
  "doc_id": "evidence:...",
  "kind": "html",
  "lines": 124,
  "title": "...",
  "preview": "...",
  "truncated": true
}
```

随后：

```json
{"query": "毛利率", "limit": 5}
{"doc_id": "evidence:...", "start": 1, "end": 40}
```

`search_corpus` 的命中带 `source`；`read_slice` 的返回带 `metadata`
（source/url/status/content_type/retrieved_at/sha256）。

## 安全边界

- **模型不能传 URL。** `source` 是 schema 枚举，值只能来自主机配置。
- **凭证不落链。** token 在调用时从环境变量读取，只进请求头，不进结果、观测或 trace。
- **私网默认拒绝。** 目标解析到 loopback/私有/保留地址时拒绝；只有显式
  `allow_private: true` 的本机 fixture/内网源才放行。
- **只允许 http/https。** 公网源必须 HTTPS；纯 HTTP 只用于显式本地源。
- **跳转不跨主机。** 同主机跳转最多 2 次；跨主机或 HTTPS 降级拒绝。
- **有界读取。** 默认 2 MiB 上限，先截断再解码；超限直接报错。
- **不自动重试。** 超时是 `CONNECTOR_TIMEOUT`，传输未知是 `CONNECTOR_UNKNOWN`，
  是否重试由操作员决定。
- **内容白名单。** 只接受 HTML、JSON、CSV、XML、纯文本；二进制返回
  `CONNECTOR_CONTENT_TYPE`。
- **HTML 只取可读文本。** script/style 被丢弃，标题单独保留；这是启发式提取，
  不是浏览器渲染。

DNS 检查发生在连接前；主机配置本身是可信边界。高安全部署应再加出站代理和
域名白名单。当前只支持单次 GET，不支持 POST、分页、递归爬取或 PDF 解析。

## 测试

`tests/test_collect_source.py` 用真实 loopback HTTP 服务覆盖：

- HTML 转文本、入 Evidence、`search_corpus` 命中、`read_slice` 元数据；
- disabled 源、二进制、超限、私网、跨主机跳转；
- 同主机跳转、JSON 美化、`FCODE_SOURCES` 配置加载。
