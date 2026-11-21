"use strict";

/* 设置页与本机偏好的 DOM 同步（自 app.js 拆分切片 4b 迁入）。
 *
 * 偏好变量本体（settingsOpen/motionOn/notifyOn/railFilter/approvalMode/
 * toolContracts）仍归 app.js——它们被多个 surface 与绑定处读写；
 * 这里经 getter/setter 注入，设置页只负责呈现与改写。
 * resetSettingsPreferences 是唯一的一次性恢复默认入口。
 */

import { esc } from "../core/format.js";

export function createSettings(deps) {
  const d = deps;

  let settingsSection = "appearance";
  const settingsScroll = { main: 0, rail: 0 };
  let memoryStats = null;
  let settingsResetArmed = false;
  let settingsResetTimer = null;
  let toolsLoaded = false;

  function applyMotion() {
    document.body.classList.toggle("reduce-motion", !d.getMotionOn());
    const toggle = document.getElementById("settings-motion");
    if (toggle) toggle.setAttribute("aria-checked", d.getMotionOn() ? "true" : "false");
  }

  function setMotion(on) {
    d.setMotionOn(!!on);
    localStorage.setItem(d.MOTION_KEY, d.getMotionOn() ? "1" : "0");
    applyMotion();
    d.toast(d.getMotionOn() ? "界面动效已开启" : "界面动效已关闭");
  }

  function notificationStateText() {
    if (!("Notification" in window)) return "此浏览器不支持系统通知";
    if (d.getNotifyOn() && Notification.permission === "granted") return "已开启 · 浏览器已授权";
    if (d.getNotifyOn()) return "已开启 · 等待浏览器授权";
    return "关闭 · 完成时只更新页面";
  }

  function syncSettings() {
    const modes = document.getElementById("settings-modes");
    if (modes) {
      for (const button of modes.querySelectorAll("[data-set-approval]")) {
        const active = button.dataset.setApproval === d.getApprovalMode();
        button.classList.toggle("active", active);
        button.setAttribute("aria-checked", active ? "true" : "false");
      }
    }
    const motion = document.getElementById("settings-motion");
    if (motion) motion.setAttribute("aria-checked", d.getMotionOn() ? "true" : "false");
    const notifyToggle = document.getElementById("settings-notify");
    if (notifyToggle) notifyToggle.setAttribute("aria-checked", d.getNotifyOn() ? "true" : "false");
    const notifyState = document.getElementById("settings-notify-state");
    if (notifyState) notifyState.textContent = notificationStateText();

    for (const button of document.querySelectorAll("[data-set-filter]")) {
      button.classList.toggle("active", button.dataset.setFilter === d.getRailFilter());
    }

    const state = document.getElementById("settings-state");
    if (state) {
      const health = d.getHealth();
      const online = !!(health && health.live);
      state.textContent = online ? "live · local" : "offline";
      state.classList.toggle("on", online);
    }

    const cases = memoryStats && memoryStats.cases;
    const facts = memoryStats && memoryStats.facts;
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };
    setText("settings-cases", cases ? `${cases.active} / ${cases.total}` : "—");
    setText("settings-facts", facts ? `${facts.active} / ${facts.total}` : "—");
    setText("settings-artifacts", memoryStats && memoryStats.artifacts
      ? `${memoryStats.artifacts.active} / ${memoryStats.artifacts.total}`
      : "—");
    setText("settings-runs", String(d.getRunHistory().length));
    const toolContracts = d.getToolContracts();
    setText("settings-tools", toolContracts ? `${toolContracts.length} 个工具 · 契约已加载` : "—");
    const store = d.store;
    setText("settings-chain", store.check
      ? (store.check.ok ? `链验证通过 · ${store.check.events} events` : `链断裂 · ${store.check.reason}`)
      : "当前没有加载运行");
    const health = d.getHealth();
    setText("settings-repo", health && health.repo ? `${health.repo}/flowcredit/viewer/runs` : "—");
  }

  function renderSettings() {
    const modes = document.getElementById("settings-modes");
    if (modes && modes.dataset.ready !== "1") {
      modes.innerHTML = d.APPROVAL_MODES.map(([key, title, desc]) =>
        `<button class="set-mode" type="button" role="radio" data-set-approval="${esc(key)}" aria-checked="false">
          <span class="set-dot"></span>
          <b>${esc(title)}</b>
          <small>${esc(desc)}</small>
        </button>`).join("");
      modes.dataset.ready = "1";
    }
    syncSettings();
  }

  async function refreshSettingsStats() {
    const health = d.getHealth();
    if (health && health.live) {
      try {
        const response = await d.fetchImpl("/api/memory", { cache: "no-store" });
        if (response.ok) memoryStats = await response.json();
      } catch { /* the page shows — offline */ }
      if (!d.getToolContracts() && !toolsLoaded) {
        toolsLoaded = true;
        try {
          const response = await d.fetchImpl("/api/tools", { cache: "no-store" });
          if (response.ok) {
            const data = await response.json();
            if (Array.isArray(data.tools)) d.setToolContracts(data.tools);
          }
        } catch { toolsLoaded = false; }
      }
    }
    syncSettings();
  }

  function activateSettingsSection(name) {
    const section = document.getElementById(`set-${name}`);
    if (!section) return;
    settingsSection = name;
    for (const button of document.querySelectorAll("#settings-nav [data-set-section]")) {
      const active = button.dataset.setSection === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
    }
    for (const panel of document.querySelectorAll("#settings-content [data-set-panel]")) {
      const active = panel === section;
      panel.hidden = !active;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `settings-tab-${panel.dataset.setPanel}`);
    }
    document.getElementById("settings-content").scrollTop = 0;
  }

  function openSettingsPage() {
    if (d.getSettingsOpen()) return;
    d.leaveChat();
    if (d.intake.active) d.closeIntake(false);
    d.setSettingsOpen(true);
    settingsScroll.main = document.querySelector(".main")?.scrollTop || 0;
    settingsScroll.rail = document.getElementById("rail-body").scrollTop;
    document.getElementById("workspace").classList.add("settings-open");
    document.getElementById("app").classList.add("settings-open");
    document.getElementById("settings-page").hidden = false;
    document.getElementById("settings-back").textContent = "×";
    document.getElementById("settings-back").title = "关闭设置 (Esc)";
    const anchor = document.getElementById("settings-mobile") || document.getElementById("settings-btn");
    anchor?.setAttribute("aria-current", "page");
    renderSettings();
    d.syncChatConfig();
    d.refreshChatMemory();
    document.getElementById("settings-content").scrollTop = 0;

    activateSettingsSection(settingsSection, "auto");
    refreshSettingsStats();
    document.getElementById("settings-back").focus({ preventScroll: true });
  }

  function closeSettingsPage(returnFocus = true) {
    if (!d.getSettingsOpen()) return;
    d.setSettingsOpen(false);
    d.store.mode = document.getElementById("main")?.dataset.view || "run";
    document.getElementById("workspace").classList.remove("settings-open");
    document.getElementById("app").classList.remove("settings-open");
    document.getElementById("settings-page").hidden = true;
    (document.getElementById("settings-mobile") || document.getElementById("settings-btn"))?.removeAttribute("aria-current");
    requestAnimationFrame(() => {
      const scroller = document.querySelector(".main");
      if (scroller) scroller.scrollTop = settingsScroll.main;
      document.getElementById("rail-body").scrollTop = settingsScroll.rail;
    });
    if (returnFocus) (document.getElementById("settings-mobile") || document.getElementById("settings-btn"))?.focus({ preventScroll: true });
  }

  function toggleSettingsPage() {
    if (d.getSettingsOpen()) closeSettingsPage(true);
    else d.renderMode("settings");
  }

  function disarmSettingsReset() {
    settingsResetArmed = false;
    clearTimeout(settingsResetTimer);
    const button = document.getElementById("settings-reset");
    if (button) {
      button.textContent = "恢复本机默认";
      button.classList.remove("confirm");
    }
  }

  function resetSettingsPreferences() {
    disarmSettingsReset();
    d.setApprovalMode("consequential");
    d.setMotionOn(true);
    localStorage.setItem(d.MOTION_KEY, "1");
    applyMotion();
    d.setNotifyOn(false);
    localStorage.setItem(d.NOTIFY_KEY, "0");
    d.setRailFilter("key");
    localStorage.setItem("fa.railFilter", d.getRailFilter());
    const trace = d.shownTrace();
    if (trace) d.renderRail(trace, d.store.check);
    syncSettings();
    d.toast("本机偏好已恢复默认");
  }

  function bindSettings() {
    document.getElementById("settings-page").addEventListener("click", async event => {
      const target = event.target;
      if (target.closest("#settings-back") || target.closest("#settings-done")) { closeSettingsPage(true); return; }
      if (target.closest("#settings-motion")) { setMotion(!d.getMotionOn()); return; }
      if (target.closest("#settings-notify")) { await d.toggleNotify(); syncSettings(); return; }
      const section = target.closest("[data-set-section]");
      if (section) { activateSettingsSection(section.dataset.setSection); return; }
      const filter = target.closest("[data-set-filter]");
      if (filter) {
        d.setRailFilter(filter.dataset.setFilter);
        localStorage.setItem("fa.railFilter", d.getRailFilter());
        const trace = d.shownTrace();
        if (trace) d.renderRail(trace, d.store.check);
        syncSettings();
        return;
      }
      if (target.closest("#settings-clear")) {
        d.clearPromptHistory();
        try { localStorage.removeItem("fa.history"); } catch { /* quota */ }
        syncSettings();
        d.toast("输入历史已清空");
        return;
      }
      if (target.closest("#settings-refresh-memory")) {
        refreshSettingsStats().then(() => d.toast("记忆统计已刷新"));
        return;
      }
      const memDel = target.closest("[data-mem-del]");
      if (memDel) { d.deleteChatMemory(memDel.dataset.memDel); return; }
      if (target.closest("#settings-reset")) {
        if (!settingsResetArmed) {
          settingsResetArmed = true;
          const button = document.getElementById("settings-reset");
          button.textContent = "再次点击确认";
          button.classList.add("confirm");
          clearTimeout(settingsResetTimer);
          settingsResetTimer = setTimeout(disarmSettingsReset, 3000);
          return;
        }
        resetSettingsPreferences();
        return;
      }
      if (target.closest("#settings-diag")) {
        if (!d.shownTrace()) { d.toast("先打开一条运行再复制诊断"); return; }
        d.copyDiagnostics();
        return;
      }
      if (target.closest("#settings-reverify")) {
        if (!d.shownTrace()) { d.toast("当前没有加载运行"); return; }
        d.reverify();
        setTimeout(refreshSettingsStats, 600);
        return;
      }
      const approval = target.closest("[data-set-approval]");
      if (approval) {
        d.setApprovalMode(approval.dataset.setApproval);
        syncSettings();
        return;
      }
      const save = target.closest("[data-chatcfg-save]");
      if (save) { d.saveChatConfig(save.dataset.chatcfgSave); return; }
      const clearKey = target.closest("[data-chatcfg-clearkey]");
      if (clearKey) { d.clearChatConfigKey(clearKey.dataset.chatcfgClearkey); return; }
      const defaultPick = target.closest("[data-chatcfg-default]");
      if (defaultPick && !defaultPick.disabled) { d.setDefaultChatModel(defaultPick.dataset.chatcfgDefault); return; }
      const addProvider = target.closest("[data-chatcfg-add]");
      if (addProvider) { d.addChatConfigProvider(); return; }
      // 两步内联确认（不用原生 confirm）：第一次点只进入待确认态，
      // 4 秒不点则还原；真的删除发生在第二次点击。
      const deleteProvider = target.closest("[data-chatcfg-delete]");
      if (deleteProvider) {
        if (deleteProvider.dataset.armed !== "1") {
          deleteProvider.dataset.armed = "1";
          deleteProvider.textContent = "确认删除？";
          setTimeout(() => {
            if (deleteProvider.isConnected && deleteProvider.dataset.armed === "1") {
              deleteProvider.dataset.armed = "0";
              deleteProvider.textContent = "删除端点";
            }
          }, 4000);
          return;
        }
        deleteProvider.dataset.armed = "0";
        d.deleteChatConfigProvider(deleteProvider.dataset.chatcfgDelete);
        return;
      }
    });
    document.getElementById("settings-nav").addEventListener("keydown", event => {
      const current = event.target.closest("[data-set-section]");
      if (!current) return;
      const tabs = [...document.querySelectorAll("#settings-nav [data-set-section]")];
      const index = tabs.indexOf(current);
      let next = -1;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next < 0) return;
      event.preventDefault();
      activateSettingsSection(tabs[next].dataset.setSection);
      tabs[next].focus({ preventScroll: true });
    });
  }

  function notify(title, body) {
    const bridge = d.desktopBridge();
    if (bridge && bridge.notify) {
      bridge.notify(title, body);
      return;
    }
    if (!d.notifyReady() || !document.hidden) return;
    try {
      const note = new Notification(title, { body, silent: true });
      note.onclick = () => { window.focus(); note.close(); };
    } catch { /* 通知失败不影响运行 */ }
  }

  return {
    applyMotion, setMotion, syncSettings, renderSettings, refreshSettingsStats,
    activateSettingsSection, openSettingsPage, closeSettingsPage, toggleSettingsPage,
    resetSettingsPreferences, bindSettings, notify, notificationStateText,
  };
}
