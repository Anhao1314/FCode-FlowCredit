/* Plugin market projections: filters, cards, page shell.
 *
 * Pure functions only. The server remains the fact source (`/api/plugins`);
 * this module decides how those facts are grouped and shown. A plugin is
 * never shown as installed unless the server says so.
 */

import { esc, num } from "./format.js";

const CATEGORIES = ["all", "检索", "文件", "执行", "开发", "工具", "协作"];

const STATUS_LABEL = {
  connected: "已连接",
  installed: "已安装",
  not_installed: "未安装",
  failed: "连接失败",
  unavailable: "当前模板不可用",
};

export function marketplaceCategories() {
  return [...CATEGORIES];
}

export function filterPlugins(plugins, view = {}) {
  const query = String(view.query || "").trim().toLowerCase();
  const category = String(view.category || "all");
  return (plugins || []).filter(plugin => {
    if (category !== "all" && plugin.category !== category) return false;
    if (!query) return true;
    return [plugin.name, plugin.summary, plugin.category,
      ...(plugin.tools || [])].some(value => String(value || "").toLowerCase().includes(query));
  });
}

function statusBadge(plugin) {
  const key = STATUS_LABEL[plugin.status] ? plugin.status : "not_installed";
  return `<span class="mkt-status is-${esc(key)}">${esc(STATUS_LABEL[key])}</span>`;
}

function kindBadge(plugin) {
  return `<span class="mkt-kind">${plugin.kind === "mcp" ? "MCP" : "内置"}</span>`;
}

function actionButton(plugin) {
  if (plugin.kind !== "mcp") {
    return `<span class="mkt-note">内置能力 · 无需安装</span>`;
  }
  const installed = plugin.installed || plugin.status === "connected";
  return installed
    ? `<button class="gov-wb-button quiet" type="button" data-plugin-uninstall="${esc(plugin.id)}">卸载</button>`
    : `<button class="gov-wb-button strong" type="button" data-plugin-install="${esc(plugin.id)}">安装</button>`;
}

function metaLine(plugin) {
  const tools = plugin.tools || [];
  if (plugin.kind === "mcp") {
    const command = plugin.command || [];
    return `<code class="mkt-command">${esc(command.join(" "))}</code>`;
  }
  return `<span class="mkt-tools">${tools.map(tool => `<code>${esc(tool)}</code>`).join(" ")}</span>`;
}

export function pluginCardHtml(plugin) {
  return `<article class="mkt-card" data-plugin-id="${esc(plugin.id)}">
    <header class="mkt-card-head">
      ${kindBadge(plugin)}
      <b>${esc(plugin.name)}</b>
      ${statusBadge(plugin)}
    </header>
    <p class="mkt-card-summary">${esc(plugin.summary || "")}</p>
    <div class="mkt-meta">${metaLine(plugin)}</div>
    ${plugin.reason ? `<p class="mkt-reason">${esc(plugin.reason)}</p>` : ""}
    <footer class="mkt-card-foot">
      <span class="mkt-cat">${esc(plugin.category || "")}</span>
      ${actionButton(plugin)}
    </footer>
  </article>`;
}

export function pluginGridHtml(plugins, view = {}) {
  const list = filterPlugins(plugins, view);
  if (!list.length) {
    return `<div class="gov-empty-state"><b>没有匹配的插件</b><span>换个关键词或分类试试。</span></div>`;
  }
  return `<div class="mkt-grid">${list.map(pluginCardHtml).join("")}</div>`;
}

export function marketplaceToolbarHtml(view = {}, counts = {}) {
  const category = view.category || "all";
  const button = (key, label) =>
    `<button class="mkt-filter${category === key ? " active" : ""}" type="button"
      data-mkt-cat="${esc(key)}" aria-pressed="${category === key}">${esc(label)}</button>`;
  return `<div class="mkt-toolbar">
    <input class="mkt-search" type="search" data-mkt-search placeholder="搜索插件、工具或命令…"
      value="${esc(view.query || "")}" aria-label="搜索插件">
    <div class="mkt-filters" role="group" aria-label="按分类筛选">
      ${marketplaceCategories().map(key => button(key, key === "all" ? "全部" : key)).join("")}
    </div>
    <span class="mkt-count" id="mkt-count">${num(counts.visible || 0)} / ${num(counts.total || 0)}</span>
  </div>`;
}

export function marketplacePageHtml(view = {}, counts = {}, plugins = []) {
  const connected = plugins.filter(plugin => plugin.status === "connected").length;
  return `<div class="mkt-page">
    <header class="gov-wb-header">
      <div class="gov-wb-intro">
        <p class="eyebrow">PLUGIN MARKET</p>
        <h1 class="page-title">插件市场</h1>
        <p class="page-meta">内置能力与经过审查的 MCP 插件都在这里发现。安装只写入本机
          <code>mcp.json</code>，桥接后台重连；失败会如实显示，不会伪装成功。</p>
      </div>
      <div class="mkt-summary">
        <span><b>${num(plugins.length)}</b> 个插件</span>
        <span class="gov-dot-sep"></span>
        <span><b>${num(connected)}</b> 已连接</span>
      </div>
    </header>
    ${marketplaceToolbarHtml(view, counts)}
    <div id="mkt-grid" class="mkt-grid-host"></div>
  </div>`;
}