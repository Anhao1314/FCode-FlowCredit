"use strict";

/* 侧栏渲染与外壳交互（P2 自 app.js 抽离）。

   范围：
   - 「更多」折叠（fa.sideMore.v2）、段落折叠（fa.sections）
   - 功能导航渲染（SIDEBAR_NAV/MORE_NAV）与 active 高亮
   - 置顶段（对话）、首次使用卡（当前收敛为空壳）
   - 三类外壳接线：拖拽调宽 / 滚动遮罩淡入 / 长标题跑马灯

   不在此模块：renderRuns（运行行）与 renderProjects（项目段）——它们与
   运行历史、项目过滤状态耦合，仍留在 app.js，由 renderNav 通过 deps 编排。
   置顶（fa.pins）状态留在 app.js：聊天行渲染（chat-ui）也要读它。 */

import { SIDEBAR_NAV, MORE_NAV, navItemsHtml, pinnedSectionHtml } from "../core/side-nav.js";

export function createSidebar(deps) {
  const d = deps;

  /* ---------------------------------------------------------------- nav */

  function renderSidebarNav() {
    // 统一侧栏（适配重建）:4 个真实 surface 扁平排布,4 个预留槽位进「更多」。
    const nav = document.getElementById("side-nav-items");
    if (nav) nav.innerHTML = navItemsHtml(SIDEBAR_NAV, d.getNavCounts());
    const more = document.getElementById("more-nav");
    if (more) more.innerHTML = navItemsHtml(MORE_NAV, d.getNavCounts());
    paintMoreNav();
  }

  function paintNavActive() {
    const open = document.getElementById("main")?.dataset.view || "";
    for (const el of document.querySelectorAll(".nav-item[data-open]")) {
      const isLedger = el.dataset.open === "ledger";
      // 账本高亮的特例只服务「从账本点开运行」这一个态:view 是 run 且
      // ledgerOpen 才保留高亮。ledgerOpen 是会话标志,不随视图变化清零,
      // 只看它会让离开账本后判断记录/定时任务页仍高亮账本(重建验收抓到)。
      const ledgerActive = open === "ledger" || (d.getLedgerOpen() && open === "run");
      el.classList.toggle("active", isLedger ? ledgerActive : el.dataset.open === open);
    }
  }

  function renderNav() {
    renderSidebarNav();
    paintNavActive();
    d.renderChats();
    d.renderProjects();
    d.renderRuns();
    renderPinned();
    renderQuickStart();
    // 侧栏内容全部是注入的，图标槽位要在注入之后再水合一次
    const frame = document.querySelector(".sidebar");
    if (frame) d.hydrateDoodles(frame);
    // 内容换了但没滚动时，滚动遮罩的显隐也要跟上（否则停在上一轮的判断上）
    paintScrollMasks();
  }

  /* ------------------------------------------------------------- 更多折叠 */

  /* 只装 4 个预留槽位（kimi.com 的 next-sidebar__more-list 手法）。
     适配重建后功能面全部提到第一屏;v2 键不继承旧 IA 的持久化状态——旧状态
     曾被 openLedger/renderAudit 强制写成"展开",按新结构应为默认收起。 */
  const MORE_KEY = "fa.sideMore.v2";

  function moreOpen() {
    try { return localStorage.getItem(MORE_KEY) === "1"; } catch { return false; }
  }

  function paintMoreNav() {
    const wrap = document.getElementById("more-nav");
    const toggle = document.getElementById("nav-more");
    if (!wrap || !toggle) return;
    const open = moreOpen();
    wrap.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.classList.toggle("open", open);
  }

  function setMoreNav(open) {
    try { localStorage.setItem(MORE_KEY, open ? "1" : "0"); } catch { /* quota */ }
    paintMoreNav();
  }

  /* --------------------------------------------------------------- 置顶 */

  function renderPinned() {
    // 置顶运行行原宿主在 pane-work（已随侧栏重建删除），不再渲染；
    // 对话置顶保持在本机，行上带 ★ 开关。
    const threads = d.getChatThreads().filter(thread => d.isChatPinned(thread.id));
    const chatHost = document.getElementById("chat-pinned");
    if (chatHost) {
      chatHost.innerHTML = pinnedSectionHtml(threads, {
        kind: "chat", pinnedOf: d.isChatPinned,
      });
    }
  }

  /* ------------------------------------------------------- 首次使用卡 */

  const QUICK_KEY = "fa.quickstart";

  function quickState() {
    let seen = {};
    try { seen = JSON.parse(localStorage.getItem(QUICK_KEY) || "{}"); } catch { seen = {}; }
    const runs = d.getRunHistory();
    const catalog = d.getModelCatalog();
    return {
      dismissed: !!seen.dismissed,
      ledgerSeen: !!seen.ledgerSeen || d.getLedgerOpen(),
      // 由真实状态推导：存在任何可用的非 mock provider 才算接通
      // （mock 是离线确定性替身，接通它不等于接通了模型）。
      providerReady: Array.isArray(catalog) && catalog.some(m => m.available && m.key !== "mock"),
      firstRunDone: runs.some(run => d.stateClass(run.status) === "ok"),
    };
  }

  function renderQuickStart() {
    const host = document.getElementById("side-quickstart");
    if (!host) return;
    // 真实任务优先：不把演示引导当作 Work 的主入口。
    host.innerHTML = "";
  }

  function dismissQuickStart() {
    let seen = {};
    try { seen = JSON.parse(localStorage.getItem(QUICK_KEY) || "{}"); } catch { seen = {}; }
    seen.dismissed = true;
    try { localStorage.setItem(QUICK_KEY, JSON.stringify(seen)); } catch { /* quota */ }
    // Kimi 的 quickstart-dismiss：scale .804 + 淡出，不是"啪"地消失。
    // 播放失败也要收场（兜底 timeout），否则卡片会留在那儿点不动。
    const card = document.querySelector(".sidebar-quickstart");
    if (card && !document.body.classList.contains("reduce-motion")) {
      let settled = false;
      const settle = () => { if (settled) return; settled = true; renderQuickStart(); };
      card.classList.add("dismissing");
      card.addEventListener("animationend", settle, { once: true });
      setTimeout(settle, 260);
      return;
    }
    renderQuickStart();
  }

  function markQuickStart(key) {
    let seen = {};
    try { seen = JSON.parse(localStorage.getItem(QUICK_KEY) || "{}"); } catch { seen = {}; }
    seen[key] = true;
    try { localStorage.setItem(QUICK_KEY, JSON.stringify(seen)); } catch { /* quota */ }
    renderQuickStart();
  }

  /* --------------------------------------------------------------- 段落折叠 */

  const SECTIONS_KEY = "fa.sections";

  function hiddenSections() {
    try {
      const raw = JSON.parse(localStorage.getItem(SECTIONS_KEY) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch { return []; }
  }

  function paintSections() {
    const hidden = hiddenSections();
    document.body.dataset.hide = hidden.join(" ");
    for (const button of document.querySelectorAll("[data-section-toggle]")) {
      button.setAttribute("aria-expanded", String(!hidden.includes(button.dataset.sectionToggle)));
    }
  }

  function toggleSection(button) {
    const key = button.dataset.sectionToggle;
    const hidden = hiddenSections();
    const next = hidden.includes(key) ? hidden.filter(item => item !== key) : hidden.concat(key);
    try { localStorage.setItem(SECTIONS_KEY, JSON.stringify(next)); } catch { /* quota */ }
    paintSections();
  }

  /* ================================================================= 外壳接线
     Kimi 3.2.20 侧栏里三样交互，逐条对应它的实现：
     拖拽调宽（.side-resizer）、滚动遮罩淡入（.side-scroll-top-mask.is-visible）、
     长标题跑马灯（conversation-title-marquee）。全部只碰侧栏，不碰页面与事件契约。 */

  /* 1. 拖拽调宽。命中区 4px、指示条 1px 跟随指针。多两件 Kimi 没明说的：
     记忆宽度（fa.sidebarW）与双击回到 240。 */
  const SIDEBAR_W_KEY = "fa.sidebarW";
  const SIDEBAR_W_MIN = 200;
  const SIDEBAR_W_MAX = 420;

  function applySidebarW(px) {
    const w = Math.max(SIDEBAR_W_MIN, Math.min(SIDEBAR_W_MAX, Math.round(px)));
    document.documentElement.style.setProperty("--sidebar-w", w + "px");
    return w;
  }

  function rememberSidebarW(px) {
    try { localStorage.setItem(SIDEBAR_W_KEY, String(px)); } catch { /* quota */ }
  }

  function mountResizer() {
    try {
      const saved = Number(localStorage.getItem(SIDEBAR_W_KEY));
      if (saved) applySidebarW(saved);
    } catch { /* quota */ }
    const handle = document.getElementById("side-resizer");
    const frame = document.querySelector(".sidebar-frame");
    if (!handle || !frame) return;
    let dragging = false;
    handle.addEventListener("pointerdown", event => {
      dragging = true;
      handle.classList.add("dragging");
      try { handle.setPointerCapture(event.pointerId); } catch { /* 老浏览器 */ }
      event.preventDefault();
    });
    handle.addEventListener("pointermove", event => {
      const box = frame.getBoundingClientRect();
      handle.style.setProperty("--sidebar-indicator-y", (event.clientY - box.top) + "px");
      if (dragging) applySidebarW(event.clientX - box.left);
    });
    const stop = () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      const current = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w"));
      rememberSidebarW(Number.isFinite(current) ? current : 240);
    };
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
    handle.addEventListener("pointerleave", () => {
      if (!dragging) handle.style.removeProperty("--sidebar-indicator-y");
    });
    handle.addEventListener("dblclick", () => { applySidebarW(240); rememberSidebarW(240); });
  }

  /* 2. 滚动遮罩淡入。顶部遮罩是个 height:0 的元素，滚了才 .is-visible。 */
  function paintScrollMasks() {
    for (const list of document.querySelectorAll(".side-list")) {
      const mask = list.querySelector(".side-scroll-top-mask");
      if (mask) mask.classList.toggle("is-visible", list.scrollTop > 1);
    }
  }

  function mountScrollMasks() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    // scroll 不冒泡，用捕获阶段一次性覆盖
    sidebar.addEventListener("scroll", paintScrollMasks, true);
    paintScrollMasks();
  }

  /* 3. 长标题跑马灯。只在真的溢出时挂 data-marquee（量出滚动距离写进变量），
     悬停才播——Kimi 的 conversation-title-marquee 就是这个变量驱动的。
     距离必须在悬停态下量：悬停时行尾的时间戳让位给 ⋯ 按钮，标签会变宽。 */
  function mountTitleMarquee() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    sidebar.addEventListener("mouseover", event => {
      const label = event.target.closest && event.target.closest(".slabel");
      if (!label) return;
      const overflow = label.scrollWidth - label.clientWidth;
      if (overflow > 4) {
        label.style.setProperty("--k-marquee-distance", overflow + "px");
        if (label.dataset.marquee !== "1") label.dataset.marquee = "1";
      } else if (label.dataset.marquee === "1") {
        // 宽度被拖大之后不再溢出，就别留着动画
        label.dataset.marquee = "0";
        label.style.removeProperty("--k-marquee-distance");
      }
    });
  }

  /** DOMContentLoaded 后调用一次：挂上拖宽/遮罩/跑马灯（原 app.js 底部 IIFE）。 */
  function mountShell() {
    mountResizer();
    mountScrollMasks();
    mountTitleMarquee();
  }

  return {
    renderNav, renderSidebarNav, paintNavActive,
    paintMoreNav, setMoreNav, moreOpen,
    renderPinned, renderQuickStart, markQuickStart, dismissQuickStart, quickState,
    paintSections, toggleSection, hiddenSections,
    paintScrollMasks, mountShell,
  };
}