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
});

test("卡片:内置无安装按钮，MCP 未安装给安装，已连接给卸载", () => {
  const builtin = pluginCardHtml(plugins[0]);
  assert.match(builtin, /内置/);
  assert.match(builtin, /无需安装/);
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
  });
  assert.equal(html.includes("<img>"), false);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;img&gt;/);
  assert.match(html, /&lt;script&gt;/);
});

test("目录徽记覆盖全部分类，未知分类有保守回退", async () => {
  const { marketplaceCategories, pluginIconName } = await import("../core/marketplace.js");
  const expected = {
    "检索": "search", "文件": "files", "执行": "terminal",
    "开发": "code", "工具": "wrench", "协作": "users",
  };
  for (const category of marketplaceCategories().filter(key => key !== "all")) {
    assert.equal(pluginIconName({ category, kind: "builtin" }), expected[category]);
  }
  assert.equal(pluginIconName({ category: "未知", kind: "mcp" }), "cube");
  assert.equal(pluginIconName({ kind: "builtin" }), "tool");
  for (const plugin of plugins) {
    assert.match(pluginCardHtml(plugin), new RegExp(`data-doodle="${pluginIconName(plugin)}"`));
  }
});
