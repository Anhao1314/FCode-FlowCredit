/* Plugin market surface: fetch server facts, keep view state, wire actions. */

import {
  filterPlugins, marketplacePageHtml, pluginGridHtml,
} from "../core/marketplace.js";

export function createMarketplace(deps) {
  const d = deps;
  const view = { query: "", category: "all" };
  const state = { plugins: [], loaded: false };

  function paint() {
    const host = document.getElementById("mkt-page");
    if (!host) return;
    const visible = filterPlugins(state.plugins, view);
    host.innerHTML = marketplacePageHtml(view, {
      visible: visible.length, total: state.plugins.length,
    }, state.plugins);
    const grid = document.getElementById("mkt-grid");
    if (grid) grid.innerHTML = pluginGridHtml(state.plugins, view);
  }

  function paintResults() {
    const grid = document.getElementById("mkt-grid");
    if (!grid) return;
    const visible = filterPlugins(state.plugins, view);
    grid.innerHTML = pluginGridHtml(state.plugins, view);
    const count = document.getElementById("mkt-count");
    if (count) count.textContent = `${visible.length} / ${state.plugins.length}`;
  }

  async function renderMarketplace() {
    document.getElementById("tb-title").textContent = "插件市场";
    document.getElementById("tb-chips").innerHTML = "";
    d.setFoot("插件市场", "");
    d.renderMain('<div class="mkt-page" id="mkt-page"><div class="gov-loading dim">读取插件目录…</div></div>',
      { mode: "market" });
    paint();
    const epoch = d.getViewGateSequence();
    try {
      const response = await d.fetchImpl("/api/plugins", { cache: "no-store" });
      const data = response.ok ? await response.json() : { error: `HTTP ${response.status}` };
      if (epoch !== d.getViewGateSequence()) return;
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      state.plugins = Array.isArray(data.plugins) ? data.plugins : [];
      state.loaded = true;
      paint();
    } catch (error) {
      if (epoch !== d.getViewGateSequence()) return;
      const host = document.getElementById("mkt-page");
      if (host) host.innerHTML =
        `<div class="gov-empty-state"><b>插件市场不可用</b><span>${error.message}</span></div>`;
    }
  }

  async function act(pluginId, action) {
    try {
      const response = await d.fetchImpl(`/api/plugins/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: pluginId }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      d.toast(action === "install" ? "插件已安装 · 桥接重连中" : "插件已卸载 · 桥接重连中");
      await renderMarketplace();
    } catch (error) {
      d.toast(`${action === "install" ? "安装" : "卸载"}失败：${error.message}`);
    }
  }

  function bind() {
    document.addEventListener("input", event => {
      const search = event.target.closest("[data-mkt-search]");
      if (!search) return;
      view.query = search.value;
      paintResults();
    });
    document.addEventListener("click", event => {
      const category = event.target.closest("[data-mkt-cat]");
      if (category) {
        view.category = category.dataset.mktCat || "all";
        paint();
        return;
      }
      const install = event.target.closest("[data-plugin-install]");
      if (install) { act(install.dataset.pluginInstall, "install"); return; }
      const uninstall = event.target.closest("[data-plugin-uninstall]");
      if (uninstall) act(uninstall.dataset.pluginUninstall, "uninstall");
    });
  }

  bind();
  return { renderMarketplace };
}