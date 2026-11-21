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

/* 单色线性图案（stroke 继承 currentColor），与整体设计语言一致。
   目录是代码不是用户输入，id → 图案的映射是静态事实；未知 id 按分类兜底，
   再兜不住给通用六边形，保证任何目录项都有图案可用。 */
const GLYPH_SVG = {
  "code-search": `<circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 4.5 4.5"/><path d="M8.5 10.5h4M10.5 8.5v4"/>`,
  "files": `<path d="M6 3.5h8l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 5 19V5A1.5 1.5 0 0 1 6.5 3.5z"/><path d="M14 3.5V8h4.5"/><path d="M8 12h8M8 15.5h5"/>`,
  "browser": `<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 8.5h17"/><circle cx="6.2" cy="6.5" r=".55" fill="currentColor" stroke="none"/><circle cx="8.6" cy="6.5" r=".55" fill="currentColor" stroke="none"/><path d="m11 12.5 3.5 5 .8-2.2 2.2-.8z"/>`,
  "terminal": `<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="m7 10 2.5 2L7 14"/><path d="M12.5 14.5h4.5"/>`,
  "web-fetch": `<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.6 2.3 3.9 5.1 3.9 8.5s-1.3 6.2-3.9 8.5c-2.6-2.3-3.9-5.1-3.9-8.5s1.3-6.2 3.9-8.5z"/>`,
  "operator-ask": `<path d="M4.5 5.5h15A1.5 1.5 0 0 1 21 7v8a1.5 1.5 0 0 1-1.5 1.5H12l-4.5 4v-4H4.5A1.5 1.5 0 0 1 3 15V7a1.5 1.5 0 0 1 1.5-1.5z"/><path d="M10.2 9.6A1.9 1.9 0 1 1 12 11.8v1"/><circle cx="12" cy="14.6" r=".55" fill="currentColor" stroke="none"/>`,
  "filesystem": `<path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h4l2 2.5h8A1.5 1.5 0 0 1 20.5 9v9A1.5 1.5 0 0 1 19 19.5H5A1.5 1.5 0 0 1 3.5 18z"/><path d="M3.5 10.5h17"/>`,
  "fetch": `<circle cx="12" cy="11" r="7.5"/><path d="M4.7 8.5h14.6M4.7 13.5h14.6" transform="translate(0 -1)"/><path d="M12 3.5c-2.3 2-3.5 4.5-3.5 7s1.2 5 3.5 7c2.3-2 3.5-4.5 3.5-7s-1.2-5-3.5-7z" transform="translate(0 -1)"/><path d="M12 14.5v6"/><path d="m9.5 18 2.5 2.5L14.5 18"/>`,
  "git": `<circle cx="6.5" cy="6" r="2.2"/><circle cx="6.5" cy="18" r="2.2"/><circle cx="17.5" cy="9" r="2.2"/><path d="M6.5 8.2v7.6"/><path d="M17.5 11.2c0 3.3-3 4.3-6.5 4.3"/>`,
  "time": `<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>`,
};

const CATEGORY_GLYPH = {
  "检索": GLYPH_SVG["code-search"],
  "文件": GLYPH_SVG["files"],
  "执行": GLYPH_SVG["terminal"],
  "开发": GLYPH_SVG["git"],
  "工具": GLYPH_SVG["time"],
  "协作": GLYPH_SVG["operator-ask"],
};

const FALLBACK_GLYPH = `<path d="M12 2.8 19.7 7v10L12 21.2 4.3 17V7z"/>`;

export function pluginGlyph(plugin) {
  const path = GLYPH_SVG[plugin.id] || CATEGORY_GLYPH[plugin.category] || FALLBACK_GLYPH;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
}

export function marketplaceCategories() {
  return [...CATEGORIES];
}

export function filterPlugins(plugins, view = {}) {
  const query = String(view.query || "").trim().toLowerCase();
  const category = String(view.category || "all");
  return (plugins || []).filter(plugin => {
    if (category !== "all" && plugin.category !== category) return false;
    if (!query) return true;
    return [plugin.name, plugin.summary, plugin.description, plugin.category,
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
  if (plugin.kind !== "mcp") return "";
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
    <div class="mkt-top">
      <span class="mkt-icon" aria-hidden="true">${pluginGlyph(plugin)}</span>
      <div class="mkt-headline">
        <header class="mkt-card-head">
          <b>${esc(plugin.name)}</b>
          ${kindBadge(plugin)}
        </header>
      </div>
      ${statusBadge(plugin)}
    </div>
    ${plugin.description ? `<p class="mkt-card-description">${esc(plugin.description)}</p>` : ""}
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
        <p class="page-meta">内置能力与经过审查的 MCP 插件都在这里发现；安装只改本机配置，失败如实显示。</p>
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