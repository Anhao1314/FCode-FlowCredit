"use strict";

/* ⌘K 命令面板（自 app.js 拆分切片 4d 迁入）。
 *
 * 动作清单按语境组装（运行中/有 trace/离线各不相同），匹配、渲染、
 * 键盘高亮与执行都在这里。slash 菜单与输入历史留在 app.js——它们
 * 与 composer 的键盘状态机共享同一份可变游标。
 */

import { esc } from "../core/format.js";

export function createCommandPalette(deps) {
  const d = deps;

  let palItems = [];
  let palActive = 0;
  let paletteReturnFocus = null;

  function setAllRounds(open) {
    for (const el of document.querySelectorAll(".th-item.th-tool")) {
      el.dataset.open = String(!!open);
      el.dataset.user = "1";
      el.classList.toggle("open", !!open);
      const toggle = el.querySelector("[data-thread-toggle]");
      if (toggle) toggle.setAttribute("aria-expanded", String(!!open));
    }
    d.toast(open ? "已展开全部工具调用" : "已折叠全部工具调用");
  }

  function focusComposer() {
    const ta = document.getElementById("composer-ta");
    if (!ta) return;
    if (ta.disabled) { d.toast("有运行中的任务，先停止再派新任务"); return; }
    ta.focus();
  }

  function paletteActions() {
    const s = d.activeSession();
    const actions = [
      { ic: "＋", label: "新委托 · 需求选择", run: () => d.startIntake("") },
      { ic: "✎", label: "聚焦任务输入框", run: focusComposer },
    ];
    if (s && s.running) actions.push({ ic: "■", label: "停止当前运行", run: d.stopRun });
    if (d.currentRun() && !(s && s.running)) actions.push({ ic: "↻", label: "重试当前运行", run: d.retryCurrent });
    const trace = d.shownTrace();
    if (trace) {
      actions.push({ ic: "⧉", label: "复制运行诊断", run: d.copyDiagnostics });
      actions.push({ ic: "↓", label: "导出 trace JSON", run: d.exportTrace });
      if (!(d.store.key || "").startsWith("run:")) {
        actions.push({ ic: "⟲", label: "重放当前场景", run: d.replay });
      }
      if (trace.terminal && trace.terminal.status) {
        actions.push({ ic: "⎘", label: "签发审计报告", run: d.openReport });
      }
      actions.push({ ic: "✓", label: "重新验证签名链", run: d.reverify });
      actions.push({ ic: "▾", label: "展开全部轮次", run: () => setAllRounds(true) });
      actions.push({ ic: "▸", label: "折叠全部轮次", run: () => setAllRounds(false) });
    }
    actions.push({ ic: "＋", label: "打开 trace JSON 文件…", run: () => document.getElementById("pick").click() });
    actions.push({ ic: "◔", label: d.getNotifyOn() ? "关闭完成通知" : "开启完成通知", run: d.toggleNotify });
    return actions;
  }

  function paletteMatches() {
    const query = (document.getElementById("palette-input").value || "").trim().toLowerCase();
    const out = [];
    const push = (group, item) => out.push({ group, ...item });
    for (const item of paletteActions()) {
      if (!query || item.label.toLowerCase().includes(query)) push("动作", item);
    }
    const runs = d.getRunHistory()
      .slice()
      .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0))
      .filter(run => !query || `${d.runLabel(run)} ${run.objective}`.toLowerCase().includes(query))
      .slice(0, 6);
    for (const run of runs) push("运行", {
      label: d.runLabel(run),
      hint: run.status === "running" ? "运行中" : (run.rounds ? run.rounds + " 轮" : ""),
      run: () => d.selectRun(run.id),
    });
    return out;
  }

  function renderPalette() {
    palItems = paletteMatches();
    if (palActive >= palItems.length) palActive = Math.max(0, palItems.length - 1);
    const list = document.getElementById("palette-list");
    let html = "";
    let lastGroup = "";
    palItems.forEach((item, index) => {
      if (item.group !== lastGroup) {
        html += `<div class="pal-group" role="presentation">${esc(item.group)}</div>`;
        lastGroup = item.group;
      }
      html += `<button type="button" class="pal-item${index === palActive ? " active" : ""}"
        id="palette-option-${index}" role="option" aria-selected="${index === palActive}" tabindex="-1" data-idx="${index}">
        <span class="pal-ic">${esc(item.ic || "")}</span>
        <span class="pal-label">${esc(item.label)}</span>
        ${item.hint ? `<span class="pal-hint">${esc(item.hint)}</span>` : ""}
      </button>`;
    });
    list.innerHTML = html || `<div class="pal-empty">没有匹配项</div>`;
    highlightPalette();
  }

  function highlightPalette() {
    const list = document.getElementById("palette-list");
    for (const button of list.querySelectorAll(".pal-item")) {
      const selected = Number(button.dataset.idx) === palActive;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    }
    const active = list.querySelector(".pal-item.active");
    const input = document.getElementById("palette-input");
    if (active) {
      input.setAttribute("aria-activedescendant", active.id);
      active.scrollIntoView({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  }

  function openPalette() {
    const overlay = document.getElementById("palette");
    if (!overlay) return;
    const input = document.getElementById("palette-input");
    if (!overlay.hidden) { input.focus(); return; }
    paletteReturnFocus = document.activeElement;
    overlay.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.value = "";
    palActive = 0;
    renderPalette();
    input.focus();
  }

  function closePalette() {
    const overlay = document.getElementById("palette");
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    const input = document.getElementById("palette-input");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    const returnFocus = paletteReturnFocus;
    paletteReturnFocus = null;
    if (returnFocus && returnFocus.isConnected !== false && typeof returnFocus.focus === "function") {
      /* Restore after the hidden dialog's native focus cleanup. A following
         palette action runs in the next timer and may intentionally move focus. */
      setTimeout(() => {
        if (overlay.hidden && returnFocus.isConnected !== false) returnFocus.focus({ preventScroll: true });
      }, 0);
    }
  }

  function togglePalette() {
    const overlay = document.getElementById("palette");
    if (!overlay) return;
    if (overlay.hidden) openPalette();
    else closePalette();
  }

  function runPaletteItem(index) {
    const item = palItems[index];
    if (!item) return;
    closePalette();
    setTimeout(() => {
      try { item.run(); } catch (error) { d.toast("执行失败：" + error.message); }
    }, 0);
  }

  /* 键盘导航与悬停:游标归模块所有,绑定层只发指令 */
  function activeIndex() {
    return palActive;
  }

  function setActiveIndex(index) {
    if (palActive !== index) {
      palActive = index;
      highlightPalette();
    }
  }

  function paletteNav(delta) {
    palActive = delta > 0
      ? Math.min(palItems.length - 1, palActive + 1)
      : Math.max(0, palActive - 1);
    highlightPalette();
  }

  function resetPaletteActive() {
    palActive = 0;
    renderPalette();
  }

  return {
    setAllRounds, focusComposer, paletteActions, paletteMatches,
    renderPalette, highlightPalette, openPalette, closePalette,
    togglePalette, runPaletteItem,
    activeIndex, setActiveIndex, paletteNav, resetPaletteActive,
  };
}
