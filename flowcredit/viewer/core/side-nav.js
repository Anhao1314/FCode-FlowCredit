/* 两侧栏（工作 / 对话）的纯渲染层。

   依据不是猜的：桌面版自带两套 sidebar，我们分别照抄它们自己的结构。
   - 工作侧栏 = Kimi Work（kimi-agent.html 的 AppSidebar 组件，从
     /Applications/Kimi.app 的 app.asar 里解出来的 Vue 渲染函数读出的顺序）：
       header(折叠钮+logo+flavor 徽章) → [工作|对话] 分段 → 固定区 nav-item
       .new-task(图标 addConversation 18 + 标签 + ⌘K chip) → 滚动区
       nav-section(canvas/plugins/cron/remoteControl/apps) → Pinned →
       Projects(section-title-button + “＋” 菜单：startFromScratch / useExistingFolder)
       → Tasks(section-title-button + “＋” new task + 行 + 空态
       “Your tasks will appear here”) → footer(quickstart 卡片 + account 行)。
   - 对话侧栏 = kimi.com 网页版（桌面版的“对话”其实是在 kimi-agent 页面里点
     分段后 closePage 回到的网页 chat，桌面 app 缓存里有它的样式与组件名）：
       sidebar-header → mode-switcher → sidebar-fixed-new-chat →
       next-sidebar__top-list(mykimi / plugin / scheduled-tasks / inspiration)
       → next-sidebar__more-list + next-sidebar__more-menu(More 折叠：
       kimi-work / kfc / kimi-claw / platform) → claw → pinned-list →
       project-list(__create-label) → section(history: title / task-status /
       action / chat-unread) → footer(history-loading + user-profile-trigger)。

   两边共同的硬事实（同一份 shell 样式表）：nav-item 高 36/内边距 8/圆角 12、
   悬停 F1、选中 F2；分组标题 12px/18px/Tertiary；滚动区 24px 渐变遮罩；
   footer padding 8、margin 0 -8px -8px。

   这里只放“生成 HTML 的纯函数”，状态机与事件接线仍在 app.js。 */

import { esc, num } from "./format.js";

/* 工作侧栏的平坦导航：Kimi Work 把工作页排成一列，不做折叠。
   对话侧栏把二级入口收进 More（next-sidebar__more-list），顶部只留主入口。

   Kimi Work 自己的 5 个槽位是 canvas / plugins / cron / remoteControl / apps(Beta)
   （从商品包 kimi-agent-*.js 的路由表读出：chat / cron / remoteControl / pluginsHub /
   pluginDetail / canvas）。FCode 只有 3 个真页面，另外 4 个能力不存在，所以：

   - 3 条真页面（ledger / reviews / scheduled）在前 —— 与 Kimi 的顺序不同（它是
     canvas 打头），这是有意的：第一眼能点开的应该是真页面；
   - 4 条 reserved 在后，各自带 kimi 出处与 needs（落地需要什么），点开是**明确标注
     的预留槽位页**，不是伪装成可用的空页。行上带一个安静的「预留」标记。
   每条都记 kimi 字段：预留页要把"它对应 Kimi 的什么"直接印出来，单一来源。 */
export const WORK_NAV = [
  { open: "ledger", label: "看板 · 账本", icon: "layout-grid",
    title: "委托汇总与成本账本", kimi: "FCode 自有（Kimi 无对应槽位）" },
  { open: "reviews", label: "判断记录", icon: "clipboard-list",
    title: "你在判断面里做过的选择", kimi: "FCode 自有（Kimi 无对应槽位）" },
  { open: "audit", label: "审计台", icon: "shield-check",
    title: "发现账本:审计器产出的缺陷与证据", kimi: "FCode 自有（Kimi 无对应槽位）" },
  { open: "scheduled", label: "定时任务", icon: "calendar",
    title: "把重复的委托排上日程", kimi: "cron（Kimi Work 的 Scheduled）" },
  { open: "canvas", label: "Canvas", icon: "cube", reserved: true,
    title: "预留槽位：对应 Kimi Work 的 canvas，FCode 尚未实现",
    kimi: "canvas", needs: "一个可读写的画布对象模型 + 与工作区文件的双向同步" },
  { open: "plugins", label: "插件", icon: "tool", reserved: true,
    title: "预留槽位：对应 Kimi Work 的 plugins，FCode 尚未实现",
    kimi: "pluginsHub / pluginDetail",
    needs: "插件清单、权限声明与安装通道（现有 MCP 桥只覆盖工具导入）" },
  { open: "remoteControl", label: "远程控制", icon: "link", reserved: true,
    title: "预留槽位：对应 Kimi Work 的 remoteControl，FCode 尚未实现",
    kimi: "remoteControl", needs: "远程序列与隧道；当前运行全部绑在本机 loopback" },
  { open: "apps", label: "Apps", icon: "book-open", reserved: true,
    title: "预留槽位：对应 Kimi Work 的 apps(Beta)，FCode 尚未实现",
    kimi: "apps（Beta）", needs: "第三方应用目录与 OAuth；目前只有本地模板与 MCP" },
];

export const CHAT_NAV = [
  { open: "reviews", label: "判断记录", icon: "user", title: "My Kimi 对应：操作者判断聚合" },
  { open: "scheduled", label: "定时任务", icon: "calendar", title: "把重复的委托排上日程" },
];

export const CHAT_MORE_NAV = [
  { open: "ledger", label: "看板 · 账本", icon: "layout-grid", title: "委托汇总与成本账本" },
  { open: "audit", label: "审计台", icon: "shield-check", title: "发现账本:审计器产出的缺陷与证据" },
];

/* 预留槽位的索引：open 键 → 条目。页面用它在预留页上印出"对应 Kimi 的什么、
   落地需要什么"，避免那两句在各处重复。 */
export function reservedSlots() {
  const out = {};
  for (const item of [...WORK_NAV, ...CHAT_NAV, ...CHAT_MORE_NAV]) {
    if (item.reserved) out[item.open] = item;
  }
  return out;
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
    + revealHtml('<nav class="nav" data-proj-nav>' + rows + "</nav>")
    + (rows ? "" : '<div class="section-empty">还没有项目——用「＋」建一个，把任务归到一起</div>');
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
export function modeChrome(mode) {
  const work = mode === "work";
  return {
    flavor: work ? "WORK" : "",
    newLabel: work ? "新建任务" : "新对话",
    newTitle: work ? "新建任务（⌘K）" : "新对话（⌘K）",
  };
}

export function panelHeadHtml(count, label) {
  return "<span>" + esc(label) + '</span><span class="side-count">' + num(count || 0) + "</span>";
}

/* 导航条目：Kimi 的 nav-item = 图标(18) + 标签 +（可选）计数/警示。
   打开哪个页面用 data-open 表达，两侧栏可以各放一份而不打架。
   预留槽位仍是可点的按钮（点开是标注过的预留页），只是多一个「预留」标记。 */
export function navItemsHtml(items, counts) {
  const seen = counts || {};
  return (items || []).map(function (item) {
    const count = seen[item.open];
    const tail = count
      ? '<span class="side-count">' + num(count) + "</span>"
      : (item.badge ? '<span class="nav-warn" aria-hidden="true">!</span>' : "");
    const mark = item.reserved
      ? '<span class="nav-reserved" aria-hidden="true">预留</span>'
      : "";
    return '<button class="nav-item' + (item.reserved ? " nav-item--reserved" : "") + '" type="button" data-open="' + esc(item.open) + '"'
      + (item.reserved ? ' data-reserved="1"' : "")
      + (item.title ? ' title="' + esc(item.title) + '"' : "") + ">"
      + '<span data-doodle="' + esc(item.icon) + '"></span>'
      + "<span>" + esc(item.label) + "</span>" + mark + tail + "</button>";
  }).join("");
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