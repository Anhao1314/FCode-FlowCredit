import test from "node:test";
import assert from "node:assert/strict";

import {
  filterPlugins, marketplaceToolbarHtml, pluginCardHtml, pluginGridHtml,
} from "../core/marketplace.js";

const plugins = [
  {
    id: "code-search", name: "代码检索", category: "检索", kind: "builtin",
    summary: "repo_map / search_codebase", tools: ["repo_map"], status: "connected",
  },
  {
    id: "filesystem", name: "文件系统 MCP", category: "文件", kind: "mcp",
    summary: "挂目录", tools: [], status: "not_installed", command: ["npx", "-y", "fs"],
  },
  {
    id: "fetch", name: "网页抓取 MCP", category: "检索", kind: "mcp",
    summary: "fetch server", tools: ["fetch"], status: "connected",
    command: ["uvx", "mcp-server-fetch"], reason: "",
  },
];

test("筛选:分类 + 大小写无关搜索", () => {
  assert.deepEqual(filterPlugins(plugins, {}).map(p => p.id),
    ["code-search", "filesystem", "fetch"]);
  assert.deepEqual(filterPlugins(plugins, { category: "检索" }).map(p => p.id),
    ["code-search", "fetch"]);
  assert.deepEqual(filterPlugins(plugins, { query: "MCP" }).map(p => p.id),
    ["filesystem", "fetch"]);
  assert.deepEqual(filterPlugins(plugins, { query: "repo_map" }).map(p => p.id),
    ["code-search"]);
  assert.equal(filterPlugins(plugins, { query: "nope" }).length, 0);
  // 描述参与搜索：摘要之外的词也能命中
  const withDesc = { ...plugins[1], description: "写操作仍走运行审批。" };
  assert.deepEqual(filterPlugins([withDesc], { query: "审批" }).map(p => p.id),
    ["filesystem"]);
});

test("卡片:内置无安装按钮也无冗余注释，MCP 未安装给安装，已连接给卸载", () => {
  const builtin = pluginCardHtml(plugins[0]);
  assert.match(builtin, /内置/);
  assert.doesNotMatch(builtin, /无需安装/);
  assert.doesNotMatch(builtin, /mkt-card-summary/);
  assert.doesNotMatch(builtin, /data-plugin-install/);
  const pending = pluginCardHtml(plugins[1]);
  assert.match(pending, /data-plugin-install="filesystem"/);
  assert.match(pending, /安装/);
  const connected = pluginCardHtml(plugins[2]);
  assert.match(connected, /data-plugin-uninstall="fetch"/);
  assert.match(connected, /卸载/);
});

test("状态徽章来自服务端事实，失败展示原因", () => {
  const connected = pluginCardHtml(plugins[2]);
  assert.match(connected, /已连接/);
  const failed = pluginCardHtml({
    ...plugins[1], installed: true, status: "failed", reason: "spawn failed",
  });
  assert.match(failed, /连接失败/);
  assert.match(failed, /spawn failed/);
});

test("工具栏:计数宿主与分类按钮，网格空态不伪造", () => {
  const toolbar = marketplaceToolbarHtml({ query: "", category: "all" }, { visible: 2, total: 3 });
  assert.match(toolbar, /id="mkt-count">2 \/ 3</);
  assert.match(toolbar, /data-mkt-cat="all"/);
  assert.match(toolbar, /data-mkt-cat="文件"/);
  const grid = pluginGridHtml(plugins, { query: "nope" });
  assert.match(grid, /没有匹配的插件/);
  assert.doesNotMatch(grid, /data-plugin-id=/);
});

test("用户可见文本转义", () => {
  const html = pluginCardHtml({
    id: "bad", name: "<img>", category: "文件", kind: "mcp", summary: "<script>",
    status: "failed", reason: "<script>x</script>", command: ["npx", "<bad>"],
    description: "<b>fake</b> description",
  });
  assert.equal(html.includes("<img>"), false);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<b>fake"), false);
  assert.match(html, /&lt;img&gt;/);
  assert.match(html, /&lt;script&gt;/);
});

test("图案:每个插件有图标，未知 id 按分类兜底，再兜底通用形", () => {
  const card = pluginCardHtml(plugins[0]);
  assert.match(card, /class="mkt-icon"/);
  assert.match(card, /<svg/);
  const byCategory = pluginCardHtml({
    id: "mystery", name: "神秘插件", category: "开发", kind: "builtin",
    summary: "s", status: "connected",
  });
  assert.match(byCategory, /mkt-icon/);
  assert.match(byCategory, /<svg/);
  const fallback = pluginCardHtml({
    id: "mystery", name: "x", category: "不存在", kind: "builtin",
    summary: "", status: "connected",
  });
  assert.match(fallback, /<svg/);
});

test("简介:服务端 description 进卡片，缺省不渲染空段", () => {
  const withDesc = pluginCardHtml({
    id: "files", name: "文件读写", category: "文件", kind: "builtin",
    summary: "s", status: "connected",
    description: "工作区内的最小改动读写。",
  });
  assert.match(withDesc, /class="mkt-card-description"/);
  assert.match(withDesc, /最小改动读写/);
  const bare = pluginCardHtml(plugins[0]);
  assert.doesNotMatch(bare, /mkt-card-description/);
});