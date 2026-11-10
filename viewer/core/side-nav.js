/* 统一侧栏的纯渲染层。

   历史：桌面版 Kimi 有两套 sidebar（Work 的 AppSidebar / 网页版对话侧栏），
   FCode 一度照抄成 pane-work + pane-chat 双面板。2026-11-02 工作模式收敛
   退役（uiMode 恒为 chat），pane-work 成了不可达的幽灵 DOM；2026-11 适配
   重建把两套名单合并为单一结构：

       header(折叠钮+logo) → 新对话(⌘N) + 搜索与命令(⌘K) →
       区切换器(Work/Governance) → Pinned → Projects → 对话 → 任务 →
       footer(quickstart + 模型状态)

   功能导航已撤除（2026-11-08）：治理台不再占侧栏条目，由 Governance
   区切换器直达；审计台/判断记录/账本/定时走对话卡锚点与深路由。

   硬事实沿用（同一份 shell 样式表）：nav-item 高 36/内边距 8/圆角 12、
   悬停 F1、选中 F2；分组标题 12px/18px/Tertiary；滚动区 24px 渐变遮罩；
   footer padding 8、margin 0 -8px -8px。

   这里只放“生成 HTML 的纯函数”，状态机与事件接线仍在 app.js。 */

import { esc, num } from "./format.js";
import { aggregateTargets, targetLabel } from "./gov-workbench.js";
import { botAvatarHtml } from "./bot-avatar.js";
import { botStatusLabel } from "./gov-bots.js";

/* 侧栏导航已清空:治理台不再作为侧栏条目——两区对话面由区切换器直达
   （Work → 新对话,Governance → 治理台）。此处只留两区定义与切换器渲染。 */

/* S2.1 区切换器：工作区/治理区的身份 UI（Kimi 的 Chat/Work 分段同型），
   放在「新对话」hero 下方。指示 + 着陆，**不做内容过滤**——E2E 契约是
   任意视图下 4 个导航项都可达，过滤会把 audit.spec「首页直达审计台」
   这类路径打断。点击经 data-open 走 renderMode 一条路（着陆到该区主面：
   工作区→账本，治理区→审计台）。 */
export const ZONES = [
  { key: "work", label: "Work", land: "chat", icon: "layout-grid",
    title: "工作对话：新对话（/账本 /状态 /定时 命令卡）" },
  { key: "gov", label: "Governance", land: "gov", icon: "shield-check",
    title: "治理台挖洞与操作员判断" },
];

/* 当前视图属于哪个区；不属于任何区（设置、预留槽位页）返回 null——
   切换器保持上一次的点亮态，不闪。 */
export function zoneOf(view) {
  if (["gov", "audit", "reviews"].indexOf(view) >= 0) return "gov";
  if (["chat", "ledger", "run", "scheduled", "intake", "empty"].indexOf(view) >= 0) return "work";
  return null;
}

export function zoneSwitchHtml(activeKey) {
  return ZONES.map(function (zone) {
    const active = zone.key === activeKey;
    return '<button class="zone-btn' + (active ? " active" : "") + '" type="button"'
      + ' data-zone="' + zone.key + '" data-open="' + zone.land + '"'
      + ' title="' + esc(zone.title) + '" aria-pressed="' + (active ? "true" : "false") + '">'
      + '<span data-doodle="' + zone.icon + '"></span><span>' + esc(zone.label) + "</span></button>";
  }).join("");
}

/* 行状态点：Kimi 的任务行左侧是状态点（running 转动 / 完成 / 失败）。
   FCode 保留同一位置与语义，颜色走治理事实（PASS/DENIED/UNKNOWN）。 */
export function statusDotHtml(cls) {
  return '<span class="smark ' + esc(cls) + '" aria-hidden="true"></span>';
}

/* 置顶行沿用各自列表的行组件：任务用运行行（带菜单，能取消置顶），
   对话用对话行（带置顶开关）。Kimi 的 pinned-list 也是复用同一套行。 */
function pinnedRunRowHtml(item, rowHtml) {
  if (rowHtml) return rowHtml(item);
  return '<button class="session pin-row" data-run="' + esc(item.id) + '" title="' + esc(item.title) + '">'
    + '<span class="slabel">' + esc(item.title) + "</span>"
    + '<span class="smeta">' + esc(item.meta || "") + "</span>"
    + "</button>";
}

function pinnedChatRowHtml(item, options) {
  const pinnedOf = (options && options.pinnedOf) || function () { return true; };
  return '<button class="session chat-row pin-row" data-chat-id="' + esc(item.id) + '" title="' + esc(item.title) + '">'
    + '<span class="slabel">' + esc(item.title) + "</span>"
    + '<span class="chat-pin" data-chat-pin="' + esc(item.id) + '" role="button" title="'
    + (pinnedOf(item.id) ? "取消置顶" : "置顶") + '">' + (pinnedOf(item.id) ? "★" : "☆") + "</span>"
    + "</button>";
}

/* 可折叠的"行"区：Kimi 的 .sidebar-reveal-content。标题必须留在它外面，
   否则折叠后标题被裁掉，段标题按钮点不到 → 该段再也展不开。 */
function revealHtml(inner) {
  return '<div class="side-block-inner">' + inner + "</div>";
}

/* 置顶分组：Kimi 只在真的置顶过东西时才渲染这一段（pinned-list 为空即不出现）。 */
function sectionHeadHtml(label, extra, count) {
  return '<div class="side-section">'
    + '<button class="section-title-button" type="button" data-section-toggle="' + esc(extra.key) + '" aria-expanded="true">'
    + "<span>" + esc(label) + '</span><span class="chev" aria-hidden="true">▾</span></button>'
    + '<span class="side-count">' + num(count || 0) + "</span>"
    + (extra.action || "") + "</div>";
}

export function pinnedSectionHtml(pins, options) {
  const opts = options || {};
  if (!pins || !pins.length) return "";
  const rows = pins.map(function (item) {
    return opts.kind === "chat"
      ? pinnedChatRowHtml(item, opts)
      : pinnedRunRowHtml(item, opts.rowHtml);
  }).join("");
  return sectionHeadHtml("置顶", { key: "pinned" }, pins.length)
    + revealHtml('<nav class="nav" data-pin-nav>' + rows + "</nav>");
}

/* Projects 分组：标题 + “＋”动作（Kimi 的 workspace-add-pop：从零开始 / 用已有文件夹）。
   empty 只在真的没有项目时出现，文案对应 sidebar.workspaceEmpty。 */
export function projectSectionHtml(projects, options) {
  const opts = options || {};
  const rows = (projects || []).map(function (prj) {
    const count = opts.countOf ? opts.countOf(prj.id) : 0;
    const active = opts.filter === prj.id ? " active" : "";
    const pin = prj.pinned ? '<span class="pin-mark" title="已置顶">★</span>' : "";
    return '<button class="session proj-row' + active + '" data-proj="' + esc(prj.id) + '">'
      + pin + '<span class="slabel">' + esc(prj.name) + "</span>"
      + '<span class="smeta">' + (count || "") + "</span></button>";
  }).join("");
  const add = '<button class="section-action" data-proj-add type="button" aria-label="新建项目" title="新建项目">＋</button>';
  return sectionHeadHtml("Projects", { key: "projects", action: add }, (projects || []).length)
    + revealHtml('<nav class="nav" data-proj-nav>' + rows + "</nav>");
}

export function runRowsHtml(runs, options) {
  const opts = options || {};
  const list = runs || [];
  if (!list.length) {
    return '<div class="section-empty">' + (opts.empty || "任务会出现在这里") + "</div>";
  }
  return list.map(opts.rowHtml).join("");
}

export function chatRowsHtml(threads, options) {
  const opts = options || {};
  const list = threads || [];
  // 剩余数由调用方按服务端事实给出（total - 已加载服务端行数）；
  // 没有该字段时才退回旧的 DOM 行数算法，兼容独立使用模板的调用方。
  const hasRemaining = opts.remaining !== undefined
    && opts.remaining !== null
    && Number.isFinite(Number(opts.remaining));
  const rest = hasRemaining
    ? Math.max(0, Math.trunc(Number(opts.remaining)))
    : Math.max(0, (opts.total || 0) - list.length);
  const more = rest
    ? '<button class="side-more" type="button" data-chat-more>显示全部 · 还有 ' + rest + " 条</button>"
    : "";
  if (!list.length) {
    return '<div class="section-empty">还没有对话——在上面输入框里说第一句</div>' + more;
  }
  const pinnedOf = opts.pinnedOf || function () { return false; };
  const items = list.map(function (thread) {
    const active = opts.activeId && opts.activeId === thread.id ? " active" : "";
    // 行内动作与 Kimi 的 history-item 一致：置顶 + 删除（悬停才显现）。
    // 可及名称不能依赖字形（☆/×），role=button 配 aria-label。
    const pin = '<span class="chat-pin" data-chat-pin="' + esc(thread.id) + '" role="button" title="'
      + (pinnedOf(thread.id) ? "取消置顶" : "置顶") + '" aria-label="'
      + (pinnedOf(thread.id) ? "取消置顶" : "置顶") + '">' + (pinnedOf(thread.id) ? "★" : "☆") + "</span>";
    const del = opts.deletable === false ? ""
      : '<span class="chat-del" data-chat-delete="' + esc(thread.id) + '" role="button" title="从列表移除（事件链保留）" aria-label="从列表移除">×</span>';
    return '<button class="session chat-row' + active + '" data-chat-id="' + esc(thread.id) + '" title="' + esc(thread.title) + '">'
      + '<span class="slabel">' + esc(thread.title) + "</span>" + pin + del + "</button>";
  }).join("");
  return items + more;
}

/* 首次使用的三步卡（Kimi Work footer 的 sidebar-quickstart）。
   每一步都由真实状态推导，不写死“已完成”；全部做完后转成收尾态，
   dismissed 后整卡不再出现（fa.quickstart）。 */
export function quickStartHtml(state) {
  const s = state || {};
  if (s.dismissed) return "";
  const steps = [
    { id: "provider", done: !!s.providerReady, label: "接通一个模型 provider", hint: "设置 → 模型" },
    { id: "firstRun", done: !!s.firstRunDone, label: "跑完第一个任务", hint: "⌘K 新建" },
    { id: "ledger", done: !!s.ledgerSeen, label: "看一眼账本与判断记录", hint: "看板 · 账本" },
  ];
  const done = steps.filter(function (step) { return step.done; }).length;
  const rows = steps.map(function (step) {
    return '<button class="quickstart-task' + (step.done ? " done" : "") + '" type="button" data-quickstart="' + step.id + '">'
      + '<span class="quickstart-checkbox" aria-hidden="true">' + (step.done ? "✓" : "") + "</span>"
      + '<span class="quickstart-task-title">' + esc(step.label) + "</span>"
      + '<span class="smeta">' + esc(step.hint) + "</span>"
      + "</button>";
  }).join("");
  const allDone = done === steps.length;
  return '<div class="sidebar-quickstart' + (allDone ? " is-done" : "") + '">'
    + '<div class="quickstart-header"><span class="quickstart-title">快速开始</span>'
    + '<span class="smeta">' + done + "/" + steps.length + "</span>"
    + '<button class="quickstart-close" type="button" data-quickstart-close aria-label="不再提示">×</button></div>'
    + '<div class="quickstart-list">' + rows + "</div>"
    + (allDone ? '<div class="quickstart-done-text">都试过了。之后想做重复的事，去「定时任务」。</div>' : "")
    + "</div>";
}

/* 侧栏版本：header 的 flavor 徽章只在工作侧栏出现（Kimi Work 的 sidebar-flavor-badge）；
   对话侧栏（网页版）header 只有 logo 与折叠钮。 */
export function panelHeadHtml(count, label) {
  return "<span>" + esc(label) + '</span><span class="side-count">' + num(count || 0) + "</span>";
}

/* 治理区历史（Kimi 式分区历史）：审计记录按日期分组（今天/昨天/更早）。
   数据源 = /api/audit/bundles 的服务端事实；行点击深链 #/audit/<dir>
   进审计台详情面。Work 区看到的是对话/任务历史，Governance 区看到的是
   审计历史——两区各一套，互不混排（侧栏 DOM 都在，由 body[data-zone]
   的 CSS 互斥显隐）。 */
function localDayString(date) {
  const pad = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/* 治理历史 = 团队运行 + 审计记录。审计记录仍按目标聚合（一个 target
   一行，指向最近一次审计）；团队运行保留最近几条 Bot 事实，点击回到
   Bot 详情。审计行的版本号来自 bundle.toolchain.ruleset_version。 */
function govRunAvatarState(status) {
  if (status === "queued" || status === "running") return "running";
  if (status === "pending") return "pending";
  if (status === "failed" || status === "rejected") return "failed";
  if (status === "done") return "done";
  return "idle";
}

function govRunMeta(run) {
  if (run.status === "pending") return "待批准";
  if (run.status === "queued" || run.status === "running") return "执行中";
  if (run.status === "failed") return "失败";
  if (run.status === "rejected") return "已拒绝";
  if (run.bot_id === "verifier") {
    return run.chain_intact === false ? "链断裂" : run.chain_intact === true ? "链完整" : "链未知";
  }
  return `${num(run.findings || 0)} 发现`;
}

function govRunRowHtml(run, bots) {
  const bot = (bots || []).find(item => item.id === run.bot_id);
  const name = (bot && bot.name) || run.bot_id || "Bot";
  const target = targetLabel({ target: run.target });
  const title = `${run.run_id || ""} · ${run.target || ""} · ${run.trigger || "manual"}`;
  return '<button class="session chat-row gov-run-row" type="button"'
    + ' data-gov-bot-open="' + esc(run.bot_id) + '" data-gov-run-id="' + esc(run.run_id) + '"'
    + ' title="' + esc(title) + '">'
    + '<span class="gov-run-avatar">' + botAvatarHtml(run.bot_id, govRunAvatarState(run.status), 20, name) + "</span>"
    + '<span class="slabel">' + esc(name) + " · " + esc(target) + "</span>"
    + '<span class="smeta">' + esc(govRunMeta(run)) + "</span></button>";
}

export function govHistorySectionHtml(bundles, options = {}) {
  const runs = (options.runs || []).slice(0, 6);
  const bots = options.bots || [];
  const rows = aggregateTargets((bundles || []).slice()).map(row => {
    const match = /(\d{4}-\d{2}-\d{2})/.exec(row.dir)
      || /(\d{4}-\d{2}-\d{2})/.exec(String(row.auditId || ""));
    return { ...row, day: match ? match[1] : "" };
  });
  const now = new Date();
  const today = localDayString(now);
  const yesterday = localDayString(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const groups = { "今天": [], "昨天": [], "更早": [] };
  for (const row of rows) {
    groups[row.day === today ? "今天" : row.day === yesterday ? "昨天" : "更早"].push(row);
  }
  let body = "";
  if (runs.length) {
    body += '<div class="side-head-label"><span>团队运行</span><span class="grp-count">' + num(runs.length) + "</span></div>";
    body += runs.map(run => govRunRowHtml(run, bots)).join("");
  }
  if (rows.length) {
    body += '<div class="side-head-label"><span>审计记录</span><span class="grp-count">' + num(rows.length) + "</span></div>";
  }
  for (const name of ["今天", "昨天", "更早"]) {
    const items = groups[name];
    if (!items.length) continue;
    body += '<div class="side-head-label"><span>' + name + '</span><span class="grp-count">' + num(items.length) + "</span></div>";
    body += items.map(row => {
      const high = Number((row.severity || {}).high || 0);
      const version = row.rulesetVersion ? ` · v${row.rulesetVersion}` : "";
      const meta = (high ? `高${high} · ${num(row.findings)} 发现` : `${num(row.findings)} 发现`) + version;
      // 行内只放发现数/规则集版本；运行次数进 title，避免长目标名被挤没。
      const title = row.auditId + (row.runs > 1 ? ` · ${num(row.runs)} 次审计` : "")
        + (row.rulesetVersion ? ` · ruleset v${row.rulesetVersion}` : "");
      return '<button class="session chat-row gov-target-row" type="button" data-gov-hist="' + esc(row.dir) + '" data-gov-target="' + esc(row.key) + '" title="' + esc(title) + '">'
        + '<span class="slabel">' + esc(row.label) + '</span><span class="smeta">' + esc(meta) + "</span></button>";
    }).join("");
  }
  const total = runs.length + rows.length;
  const inner = total
    ? '<div class="side-block-inner"><nav class="nav" data-gov-hist-nav>' + body + "</nav></div>"
    : '<div class="section-empty">还没有治理记录——在团队派活，或开始一次审计。</div>';
  return sectionHeadHtml("治理历史", { key: "govhist" }, total) + inner;
}

/* 对话分组标题（kimi.com 的 history 段）。 */
export function chatSectionHtml(count) {
  return sectionHeadHtml("对话", { key: "chats" }, count);
}

/* 任务分组标题（Kimi Work 的 Tasks 段）：标题可折叠 + “＋”直接新建任务。 */
export function taskSectionHtml(count) {
  const add = '<button class="section-action" type="button" data-new-task aria-label="新建任务" title="新建任务">＋</button>';
  return sectionHeadHtml("任务", { key: "tasks", action: add }, count || 0)
    .replace('class="side-count"', 'class="side-count" data-task-count');
}