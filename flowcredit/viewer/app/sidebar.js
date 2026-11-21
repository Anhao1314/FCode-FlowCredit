"use strict";

/* 侧栏渲染与外壳交互（P2 自 app.js 抽离）。

   范围：
   - 段落折叠（fa.sections）
   - 区切换器与治理导航点亮（治理台/团队/工作台/审计台）
   - 置顶段（对话）、首次使用卡（当前收敛为空壳）
   - 三类外壳接线：拖拽调宽 / 滚动遮罩淡入 / 长标题跑马灯

   不在此模块：renderRuns（运行行）与 renderProjects（项目段）——它们与
   运行历史、项目过滤状态耦合，仍留在 app.js，由 renderNav 通过 deps 编排。
   置顶（fa.pins）状态留在 app.js：聊天行渲染（chat-ui）也要读它。 */

import { pinnedSectionHtml, projectSectionHtml, govHistorySectionHtml, runSectionHtml, zoneOf, zoneSwitchHtml } from "../core/side-nav.js";
import { govSurfaceSwitchHtml } from "../core/gov-workbench.js";
import { esc, num } from "../core/format.js";

export function createSidebar(deps) {
  const d = deps;

  /* ---------------------------------------------------------------- nav */

  /* S2.1 区切换器点亮态：跟随当前视图所属区；视图不属于任何区（设置、
     预留页）时保持上一次的区，不闪。 */
  let zonePainted = "work";

  let govHistPainted = false;
  let govHistBusy = false;
  let govHistSig = "";
  let govHistTimer = null;

  function paintZoneSwitch() {
    const host = document.getElementById("zone-switch");
    if (!host) return;
    const view = document.getElementById("main")?.dataset.view || "";
    const zone = zoneOf(view);
    const changed = zone !== null && zone !== zonePainted;
    if (zone) zonePainted = zone;
    host.innerHTML = zoneSwitchHtml(zonePainted);
    // 分区历史互斥：data-zone 驱动 CSS 显隐
    // （Work=对话/任务，Governance=团队运行 + 审计记录）
    document.body.dataset.zone = zonePainted;
    const flavor = document.getElementById("side-flavor");
    if (flavor) flavor.textContent = zonePainted === "gov" ? "GOV" : "WORK";
    // 治理台四面（治理台/团队/工作台/审计台）切换收进侧栏：进 gov 区就点亮当前面。
    if (zonePainted === "gov") paintGovSurfaceNav();
    // 插件市场是功能页不是区：按钮自己点亮，不改变 Work/Governance 分区。
    const marketButton = document.querySelector(".side-market");
    if (marketButton) marketButton.classList.toggle("active", view === "market");
    if (zonePainted === "gov" && (changed || !govHistPainted)) renderGovHistory();
    ensureGovHistoryPolling();
  }

  function ensureGovHistoryPolling() {
    if (zonePainted !== "gov") {
      if (govHistTimer) {
        clearInterval(govHistTimer);
        govHistTimer = null;
      }
      return;
    }
    if (govHistTimer) return;
    // Bot runs and audits finish in the background; the history is a live
    // projection, not a one-shot list. Ten seconds is plenty for a sidebar.
    govHistTimer = setInterval(() => renderGovHistory(), 10000);
  }

  async function safeJson(url, fallback) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      return response.ok ? await response.json() : fallback;
    } catch {
      return fallback;
    }
  }

  /* 治理区历史：团队运行 + 审计记录。每 10 秒静默刷新，数据签名不变
     就不重建 DOM（避免动画头像和滚动位置被无意义重绘）。 */
  async function renderGovHistory() {
    if (zonePainted !== "gov" || govHistBusy) return;
    const host = document.getElementById("gov-history");
    if (!host) return;
    govHistBusy = true;
    const first = !govHistPainted;
    govHistPainted = true;
    if (first || !host.innerHTML.trim()) {
      host.innerHTML = '<div class="section-empty">读取审计记录…</div>';
    }
    try {
      const bundleData = await safeJson("/api/audit/bundles", { bundles: [] });
      const bundles = bundleData.bundles || [];
      const signature = JSON.stringify([
        bundles.map(item => [item.dir, item.findings, item.unhandled, item.ruleset_version]),
      ]);
      if (signature !== govHistSig || host.textContent.includes("读取审计记录")) {
        govHistSig = signature;
        host.innerHTML = govHistorySectionHtml(bundles);
        paintSections();
      }
    } catch {
      if (first) host.innerHTML = '<div class="section-empty">审计记录读取失败</div>';
    } finally {
      govHistBusy = false;
    }
  }

  function renderSidebarNav() {
    // 功能导航已撤除(nav 宿主删除);这里只剩区切换器的点亮渲染。
    paintZoneSwitch();
  }

  /* 治理两面切换（对话/审计库）：团队、工作台与角色详情都是对话下钻，
     只有审计库是独立 view。 */
  function paintGovSurfaceNav() {
    const host = document.getElementById("gov-surface-nav");
    if (!host) return;
    const view = document.getElementById("main")?.dataset.view || "";
    const active = view === "audit" ? "audit" : "dialog";
    host.innerHTML = govSurfaceSwitchHtml(active);
    d.hydrateDoodles?.(host);
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
    // 区切换器点亮必须挂这里：renderMode 先写 main.dataset.view 再调本函数，
    // 而 renderNav 里的渲染发生在视图更新之前——挂那里会滞后一拍（冒烟抓到）。
    paintZoneSwitch();
  }

  function renderNav() {
    renderSidebarNav();
    paintNavActive();
    d.renderChats();
    renderProjects();
    renderRuns();
    renderPinned();
    renderQuickStart();
    // 侧栏内容全部是注入的，图标槽位要在注入之后再水合一次
    const frame = document.querySelector(".sidebar");
    if (frame) d.hydrateDoodles(frame);
    // 内容换了但没滚动时，滚动遮罩的显隐也要跟上（否则停在上一轮的判断上）
    paintScrollMasks();
  }

  /* --------------------------------------------------------------- 置顶 */

  function renderPinned() {
    // 置顶对话与置顶运行都渲染；运行的置顶不再让它从侧栏消失
    //（此前 renderPinned 只渲染 chat，而平坦列表又过滤 pinned——两边都不出现）。
    const threads = d.getChatThreads().filter(thread => d.isChatPinned(thread.id));
    const runs = (d.getRunHistory() || []).filter(run => run.pinned);
    const host = document.getElementById("chat-pinned");
    if (!host) return;
    let html = "";
    if (threads.length) {
      html += pinnedSectionHtml(threads, {
        kind: "chat", pinnedOf: d.isChatPinned, title: "置顶对话",
      });
    }
    if (runs.length) {
      html += pinnedSectionHtml(runs, {
        kind: "run", rowHtml: runRowHtml, title: "置顶运行",
      });
    }
    host.innerHTML = html;
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

  /* ============================================================ 运行行与项目段
     渲染与展示状态都在模块内（projects/runProjects/projectFilter/runFilter/
     olderOpen/navCounts）；app.js 通过与运行历史/项目相关的动作 API 读写
     （renderRuns、renderProjects、addProject、createProject、assignRunProject、
     setProjectFilter、getProjects、getRunProject、setOlderOpen）。 */

  let projects = [];
  let runProjects = {};
  let projectFilter = null;
  let expandedProjects = new Set();
  try {
    const stored = JSON.parse(localStorage.getItem("fa.projExpanded") || "[]");
    if (Array.isArray(stored)) expandedProjects = new Set(stored);
  } catch { /* ignore a damaged preference */ }
  let addingProject = false;
  let editingProjectId = null;
  let runFilter = "";
  let olderOpen = false;
  let navCounts = {};

  function persistExpanded() {
    try {
      localStorage.setItem("fa.projExpanded", JSON.stringify([...expandedProjects]));
    } catch { /* quota */ }
  }

  /* 项目与 run 归属都来自服务端快照（/api/projects、/api/runs 的 projectId）。
     本地不再写 fa.projects/fa.runProjects；旧 key 原样保留，不迁移、不删除。 */
  function syncProjects() {
    projects = Array.isArray(d.getProjects?.()) ? d.getProjects() : [];
    runProjects = {};
    for (const run of (d.getRunHistory?.() || [])) {
      if (run && run.projectId) runProjects[run.id] = run.projectId;
    }
  }


  function renderProjects(force = false) {
    syncProjects();
    const editing = editingProjectId
      ? projects.find(project => project.id === editingProjectId) || null
      : null;
    // 轮询触发的整体重绘不能吞掉正在填写的表单：表单开着时跳过重绘，
    // 等 begin*/cancel 显式 force 或提交/取消后自然恢复。
    const host = document.getElementById("chat-projects");
    const formOpen = addingProject || editingProjectId;
    if (!force && formOpen && host && host.querySelector("[data-proj-form]")) return;
    const html = projectSectionHtml(projects, {
      filter: projectFilter,
      runs: d.getRunHistory() || [],
      expanded: expandedProjects,
      rowHtml: runRowHtml,
      adding: addingProject,
      editing,
    });
    if (host) host.innerHTML = html;
  }

  function beginProjectCreate() {
    addingProject = true;
    editingProjectId = null;
    renderProjects(true);
  }

  function beginProjectEdit(projectId) {
    editingProjectId = projectId;
    addingProject = false;
    renderProjects(true);
  }

  function cancelProjectForm() {
    addingProject = false;
    editingProjectId = null;
    renderProjects(true);
  }

  function toggleProject(projectId) {
    const selecting = projectFilter !== projectId;
    projectFilter = selecting ? projectId : null;
    if (selecting) expandedProjects.add(projectId);
    else expandedProjects.delete(projectId);
    persistExpanded();
    renderProjects();
    renderRuns();
    d.renderChats?.();
    return selecting;
  }

  function getProject(projectId) {
    syncProjects();
    return projects.find(project => project.id === projectId) || null;
  }

  function createProject() {
    beginProjectCreate();
    return "";
  }

  function assignRunProject(runId, pid) {
    if (!runId) return;
    runProjects[runId] = pid;
    d.onAssignProject?.(runId, pid);
  }

  function getProjects() { syncProjects(); return projects; }

  function getRunProject(runId) { syncProjects(); return runProjects[runId]; }

  function getProjectFilter() { return projectFilter; }

  function setProjectFilter(pid) { projectFilter = pid; }

  function setOlderOpen(open) { olderOpen = !!open; }

  function addProject() {
    beginProjectCreate();
  }

  /* 运行行的状态点图标（治理事实：PASS/DENIED/UNKNOWN）。 */
  const STATUS_MARK = {
    ok: '<svg viewBox="0 0 12 12"><path d="M2.3 6.5c1.2.9 2.1 1.9 2.8 2.9 1.1-2.3 2.4-4 4.1-5.4"/></svg>',
    bad: '<svg viewBox="0 0 12 12"><path d="M3 3.1c2 1.9 4 3.9 5.9 5.9"/><path d="M9 3c-2 2-3.9 4-5.8 6"/></svg>',
    warn: '<svg viewBox="0 0 12 12"><path d="M2.4 8.2c1.3-1.5 2.5-2.2 3.3-1.8 1 .5.2 1.9 1.2 2.1.8.1 1.6-.6 2.5-1.7"/></svg>',
    live: '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="2.4" fill="currentColor" stroke="none"/></svg>',
  };

  function runGroupOf(created) {
    const when = new Date(created || "");
    if (Number.isNaN(when.getTime())) return 2;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    if (when.getTime() >= today) return 0;
    if (when.getTime() >= today - 86400000) return 1;
    return 2;
  }

  function runTimeText(created) {
    const when = new Date(created || "");
    if (Number.isNaN(when.getTime())) return "—";
    if (runGroupOf(created) === 2) {
      return `${String(when.getMonth() + 1).padStart(2, "0")}-${String(when.getDate()).padStart(2, "0")}`;
    }
    return when.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function runRowHtml(run) {
    const active = d.getActiveKey() === "run:" + run.id;
    const running = run.status === "running";
    const cls = running ? "live" : d.stateClass(run.status);
    const meta = running ? "运行中" : runTimeText(run.created);
    const pinMark = run.pinned ? `<span class="pin-mark" title="已固定">${d.doodleSvg("star")}</span>` : "";
    const bits = [d.tplTitle(run.template)];
    if (running) bits.push(`第 ${run.rounds || 0} 轮`);
    else if (run.rounds) bits.push(`${run.rounds} 轮`);
    if (!running && run.cost && run.cost.costMicrousd !== undefined) {
      bits.push(`${num(run.cost.costMicrousd)} µ`);
    }
    const sub = bits.filter(Boolean).join(" · ");
    const mark = `<span class="smark ${cls}">${STATUS_MARK[cls] || STATUS_MARK.warn}</span>`;
    return `<div class="session run-row${active ? " active" : ""}${run.pinned ? " pinned" : ""}" data-run="${esc(run.id)}" tabindex="0" title="${esc(run.objective)}${run.rounds ? " · " + run.rounds + " 轮" : ""}">
    <div class="r-top">
      ${mark}
      <span class="slabel" data-name>${esc(d.runLabel(run))}</span>
      ${pinMark}
      <span class="smeta">${meta}</span>
      <button class="row-more" data-more="${esc(run.id)}" title="更多">${d.doodleSvg("ellipsis")}</button>
    </div>
    <div class="r-sub">${esc(sub)}</div>
    <div class="row-menu" data-menu="${esc(run.id)}" hidden>
      <button data-row-act="pin">${run.pinned ? "取消固定" : "固定"}</button>
      <button data-row-act="rename">重命名</button>
      <button data-row-act="project">移动到项目…</button>
      <button data-row-act="delete">删除</button>
    </div>
  </div>`;
  }

  function updateRunCounts() {
    const runs = d.getRunHistory();
    navCounts = { ledger: runs.length };
    renderSidebarNav();
    for (const el of document.querySelectorAll("[data-task-count]")) {
      el.textContent = String(runs.length);
    }
  }

  function renderRuns() {
    // 统一侧栏重建后运行行只有一个宿主 #chat-tasks。
    const chatTasks = document.getElementById("chat-tasks");
    if (!chatTasks) return;
    syncProjects();
    updateRunCounts();
    const query = runFilter.trim().toLowerCase();
    let runs = d.getRunHistory()
      .filter(run => !query || `${run.name || ""} ${run.objective} ${run.id}`.toLowerCase().includes(query));
    // Project runs live nested under their project row. The flat list keeps
    // only unassigned runs; with no projects it is the whole history.
    if (projects.length) runs = runs.filter(run => !run.projectId);
    // 置顶的运行由「置顶」分组渲染，主列表不再重复
    runs = runs.filter(run => !run.pinned);
    const title = "任务";

    if (!runs.length) {
      const emptyHtml = query
        ? `<div class="section-empty side-empty-search">没有匹配的任务</div>`
        : `<div class="section-empty">${projects.length ? "没有未归属的运行" : "任务会出现在这里"}</div>`;
      chatTasks.innerHTML = runSectionHtml(title, 0) + emptyHtml;
      return;
    }

    let html = runSectionHtml(title, runs.length);
    if (query) {
      html += `<div class="side-head-label">${runs.length} 条结果</div>`;
      html += runs.map(runRowHtml).join("");
    } else {
      const groups = [
        { label: "今天", items: [] },
        { label: "昨天", items: [] },
        { label: "更早", items: [] },
      ];
      for (const run of runs) groups[runGroupOf(run.created)].items.push(run);
      for (const group of groups) {
        if (!group.items.length) continue;
        html += `<div class="side-head-label"><span>${group.label}</span></div>`;
        const visible = group.label === "更早" && !olderOpen ? group.items.slice(0, 5) : group.items;
        html += visible.map(runRowHtml).join("");
        if (group.label === "更早" && group.items.length > 5) {
          html += olderOpen
            ? `<button class="side-more" data-collapse-older>收起</button>`
            : `<button class="side-more" data-more-older>显示全部 ${group.items.length} 条</button>`;
        }
      }
    }
    chatTasks.innerHTML = html;
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
    renderNav, renderSidebarNav, paintNavActive, paintZoneSwitch, paintGovSurfaceNav,
    refreshGovHistory: renderGovHistory,
    renderPinned, renderQuickStart, markQuickStart, dismissQuickStart, quickState,
    paintSections, toggleSection, hiddenSections,
    renderRuns, renderProjects, addProject, createProject, assignRunProject,
    beginProjectCreate, beginProjectEdit, cancelProjectForm, toggleProject, getProject,
    getProjects, getRunProject, getProjectFilter, setProjectFilter, setOlderOpen,
    paintScrollMasks, mountShell,
  };
}