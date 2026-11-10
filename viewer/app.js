"use strict";

import {
  canonical, sha256Hex, verifyChain, esc, pct, pp, num, shortHash, stripTags,
} from "./core/format.js";
import { parseSseFrame, renderMarkdown } from "./core/chat-format.js";
import { createLatestGate, createRunsPoller, settleLatest } from "./core/latest.js";
import { resolveModelLabel, traceModelLabel } from "./core/model-label.js";
import { connectionPresentation, initialConnection, reduceConnection } from "./core/sse-status.js";
import { composerKeyAction, commitCancelAction } from "./core/keyboard.js";
import { composerMode } from "./core/composer-mode.js";
import { ledgerCardHtml, statusCardHtml, scheduledCardHtml } from "./core/workspace-cards.js";
import { filterSlashCommands } from "./core/slash-scope.js";
import { findingsCardHtml, chainCardHtml, judgmentsCardHtml } from "./core/gov-cards.js";
import { applyChromeState } from "./core/chrome.js";
import { createReviewUI } from "./app/review.js";
import { createChatRuns } from "./app/chat-runs.js";
import { createRunEvidence } from "./app/run-evidence.js";
import { createProjectWork, isProjectTrace, parseVerificationCommand, projectDraftRows } from "./app/project-work.js";
import { createChatConfig } from "./app/chat-config.js";
import { createChatUI } from "./app/chat-ui.js";
import { createSidebar } from "./app/sidebar.js";
import { createPages } from "./app/pages.js";
import {
  fmtCountdown,
} from "./app/approval-card.js";
import { createThreadSurface } from "./app/thread-surface.js";
import { createRunActions } from "./app/run-actions.js";
import { createLiveSessions } from "./app/live-sessions.js";
import { createIntake } from "./app/intake.js";
import { createSettings } from "./app/settings.js";
import { createComposerPanel } from "./app/composer-panel.js";
import { createCommandPalette } from "./app/command-palette.js";
import { doodleSvg, hydrateDoodles } from "./core/doodles.js";
import {
  decisionForPending, lastBudget,
} from "./app/task-thread.js";
import { chatMessageHtml } from "./core/chat-view.js";
import { aiAvatarHtml, userAvatarHtml } from "./core/ai-avatar.js";
import { actionCardHtml, mirrorFrom, runMirrorHtml } from "./core/run-card.js";
import { moonshotStageHtml } from "./core/moonshot-stage.js";
import { composerAddMenuHtml } from "./core/composer-add-menu.js";
import { bindLatestScroll, bindWorkspaceNavigation, auditHashTarget } from "./core/work-surface.js";
import {
  chatRowsHtml, chatSectionHtml,
  pinnedSectionHtml, projectSectionHtml, quickStartHtml,
  runRowsHtml,
} from "./core/side-nav.js";
/* 审计台 surface:纯渲染模块原样复用,app.js 只加接线不改它(render* 全部留在 audit-view)。 */
import { createAuditSurface } from "./audit/audit-view.js";

/* =================================================================== 边界声明
   本轮结构重构只允许改“生成 DOM 的渲染函数”。
   - 不可改：core/format.js 的链验证；store/sessions 状态机；SSE apply/attach；
     预算比较与提交；审批/插话/报告/设置等 API 调用；data-* 事件契约。
   - 可改：下面标成 RENDER LAYER 的模板函数，以及混合函数里的模板字符串。
   - 混合函数（render/liveApply/syncLiveRounds 等）只允许更换模板与插槽
     包装，不允许改比较时机、缓存字段或调用顺序。
   ------------------------------------------------------------------- data */

const SCENARIOS = [
  ["full",          "完整研究"],
  ["tool-denied",   "越权工具"],
  ["budget",        "预算耗尽"],
  ["budget-resume", "预算追加"],
  ["crash",         "崩溃恢复"],
  ["unknown",       "未知对账"],
  ["open-ended",    "开放问题"],
];
/* SCENARIOS 现在只作 label 字典：把运行记录的 template key 翻译成中文标题。
   内置示例场景入口已随打包 mock traces 一并移除；数据可由 demo.py 生成到
   work/demo/ 后经 ?trace= 加载，或由 server.py 的真实运行产生。 */

const EVENT_DOT = {
  task_created: "", model_started: "info", model_unknown: "warn", model_reconciled: "ok",
  plan_validated: "", plan_updated: "", execution_started: "", tool_started: "info",
  tool_denied: "bad", tool_succeeded: "ok", verification_started: "info",
  task_succeeded: "ok", task_failed: "bad", task_stopped: "warn",
  approval_requested: "warn", approval_granted: "ok", approval_denied: "bad", approval_aborted: "warn",
  plan_steered: "info", budget_adjusted: "info", control_rejected: "bad", task_resumed: "info",
  operator_message: "you", memory_recalled: "info",
  policy_requested: "info", policy_decided: "info", auto_action: "ok",
  review_decided: "you",
};

/* 人的决定：审批、计划修订、预算调整、续跑、运行中消息、判断记录 */
const HUMAN_EVENTS = new Set([
  "approval_requested", "approval_granted", "approval_denied", "approval_aborted",
  "plan_steered", "budget_adjusted", "control_rejected", "task_resumed", "operator_message",
  "review_decided",
]);

const APPROVAL_MODES = [
  ["consequential", "关键审批", "只在会改变结论或宣告受阻的动作前暂停"],
  ["all", "逐步审批", "每次工具调用都等你确认"],
  ["off", "自动", "不暂停；权限门控与预算仍然生效"],
];


const ICON = {
  folder:  '<svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  branch:  '<svg class="i" viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.4"/><circle cx="6" cy="18" r="2.4"/><circle cx="18" cy="9" r="2.4"/><path d="M6 8.4v7.2M6 12h6a4 4 0 0 0 4-4v-.6"/></svg>',
  key:     '<svg class="i" viewBox="0 0 24 24"><circle cx="8" cy="14" r="3.4"/><path d="m10.5 11.5 8-8M15 7l2.5 2.5M12.5 9.5 15 12"/></svg>',
  file:    '<svg class="i" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
  term:    '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>',
  alert:   '<svg class="i" viewBox="0 0 24 24"><path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4M12 17h.01"/></svg>',
  check:   '<svg class="i" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  x:       '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  circle:  '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="7.5"/></svg>',
  lock:    '<svg class="i" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
  thought: '<svg class="i" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg>',
  inkTick: '<svg class="i ink-tick" viewBox="0 0 24 24"><path d="M4.5 12.8c2.2 1.4 4 3.1 5.6 5.1 2.9-4.4 6-7.6 9.4-9.8" /></svg>',
};

/* 失败原因的用户语言；代码仍保留在结果卡下方，方便审计 */
const FAILURE_REASONS = {
  BUDGET_EXCEEDED: "预算已耗尽",
  ROUND_LIMIT: "达到轮次上限",
  OPERATOR_DENIED: "操作员拒绝了关键工具调用",
  PLAN_BLOCKED: "计划被阻塞",
  PLAN_EXHAUSTED: "计划已没有可执行步骤",
  INSUFFICIENT_EVIDENCE: "证据不足，Runtime 拒绝给出未经证明的数字",
  STOPPED_BY_OPERATOR: "操作员停止了运行",
};

/* 判断记录：决定写进签名链，标签同时用于结果面与事件摘要。 */
const REVIEW_DECISIONS = [
  { key: "accept", label: "接受判断", requiresReason: false, hint: "理由可选", placeholder: "可补充接受依据（可选）" },
  { key: "needs_evidence", label: "需要补材料", requiresReason: true, hint: "写明缺什么", placeholder: "缺哪份材料、哪个口径，下一步由谁补" },
  { key: "qualified", label: "保留意见", requiresReason: true, hint: "写明保留措辞", placeholder: "保留意见的措辞与适用范围" },
  { key: "redo", label: "退回重做", requiresReason: true, hint: "写明退回原因", placeholder: "退回原因与需要重做的范围" },
];
const REVIEW_LABEL = Object.fromEntries(REVIEW_DECISIONS.map(item => [item.key, item.label]));
const REVIEW_DECISION = Object.fromEntries(REVIEW_DECISIONS.map(item => [item.key, item]));
/* 历史判断的计数行用短标签，避免在窄屏上折成两行 */
const REVIEW_COUNT_LABEL = {
  accept: "接受",
  needs_evidence: "补材料",
  qualified: "保留意见",
  redo: "退回",
};
const dot = cls => `<span class="ev-dot ${cls || ""}"></span>`;

/* 桌面壳桥：浏览器里返回 null，桌面端返回 pywebview 原生 API */
function desktopBridge() {
  return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
}
window.addEventListener("pywebviewready", () => {
  window.__fcodeDesktop = true;
  document.body.classList.add("desktop");
});

/* ------------------------------------- MIXED — 统计计算（不改）+ 状态行渲染 */
/* Codex TUI 的底栏：当前模型、轮次、预算用量与快捷键，常驻不占正文。 */
function setStatusline(leftHtml, rightText) {
  const el = document.getElementById("statusline");
  if (!el) return;
  el.innerHTML = `<span class="sl-left">${leftHtml}</span><span class="sl-right">${esc(rightText || "")}</span>`;
}

function updateStatusline() {
  paintModelBadge();
  if (ledgerOpen) {
    const finished = runHistory.filter(r => r.status !== "running");
    const succeeded = finished.filter(r => r.status === "succeeded").length;
    const totalCost = finished.reduce((sum, r) => sum + ((r.cost && r.cost.costMicrousd) || 0), 0);
    const rate = finished.length ? Math.round((succeeded / finished.length) * 100) : 0;
    setStatusline(`<b>工作台</b><span class="sl-sep">·</span>委托 ${runHistory.length} 项`,
      `成功率 ${rate}% · 总成本 ${num(totalCost)} µUSD · ⌘N 新建`);
    return;
  }
  const trace = shownTrace();
  if (!trace) {
    setStatusline("<b>FCode</b>", "⌘K 命令 · ⌘N 新建对话 · ⌘. 停止 · ↑↓ 历史");
    return;
  }
  const budget = lastBudget(trace);
  const lim = (budget && budget.limits) || {};
  const rounds = (trace.rounds || []).length;
  const statusMap = { succeeded: "成功", failed: "失败", denied: "拒绝", stopped: "已停止" };
  const s = activeSession();
  const state = s ? (s.finished ? "已结束" : `第 ${rounds} 轮`)
    : (statusMap[(trace.terminal && trace.terminal.status) || ""] || (trace.terminal && trace.terminal.status) || "");
  const left = `<b>${esc(traceModelLabel(trace) || "模型未知")}</b> <span class="sl-sep">·</span> ${state ? esc(state) : "—"}`;
  const right = budget
    ? `calls ${budget.calls_used}/${lim.max_calls ?? "?"} · reads ${budget.reads_used}/${lim.max_reads ?? "?"} · ${num(budget.cost_used_microusd)} µUSD · tokens ${num(budget.tokens_used)}`
    : "⌘K 命令 · ⌘N 新建对话";
  setStatusline(left, right);
}

const cache = new Map();
/* LOGIC LAYER — 单一 UI 状态源：视图键、激活会话、静态 trace、链校验与主区模式；sessions 仍只是会话注册表。 */
const store = {
  key: null,        // 当前视图键
  sessionId: null,  // 当前激活会话 id
  trace: null,      // 非会话视图的 trace
  check: null,      // 当前链校验结果
  mode: "run",      // 最近一次主区模式
};

/* 页面选择的世代号：旧请求即使后返回，也不得写当前 DOM。 */
const viewGate = createLatestGate();

function activeSession() {
  return store.sessionId ? sessions.get(store.sessionId) || null : null;
}

function shownTrace() {
  const s = activeSession();
  return (s && s.trace) || store.trace;
}
let railTab = "events";
/* 证据面板是浮层，默认收起；打开动作只改变 CSS 状态，不影响任何运行数据。 */
let railOpen = false;
let railFilter = localStorage.getItem("fa.railFilter") || "key";
if (!["all", "human", "key"].includes(railFilter)) railFilter = "key";
const evExpanded = new Set();
let approvalMode = localStorage.getItem("fa.approval") || "consequential";
if (!APPROVAL_MODES.some(([key]) => key === approvalMode)) approvalMode = "consequential";
const MOTION_KEY = "fa.motion";
let motionOn = localStorage.getItem(MOTION_KEY) !== "0";

/* live console state */
const TPL_FALLBACK = { full: "完整研究", "tool-denied": "越权工具", budget: "预算耗尽", "budget-resume": "预算追加", crash: "崩溃恢复", unknown: "未知对账", "open-ended": "开放问题" };
let health = null;      // /api/health payload
let templates = [];     // /api/templates
let tplKey = "full";    // selected template
let chatThreads = [];   // /api/chat/threads（侧栏「对话」）
let chatModels = [];    // /api/chat/models（对话模式模型菜单）
let chatConfig = null;  // /api/settings/chat-config（脱敏后的配置）
let chatConfigSource = null; // 这份配置从哪来、是否比进程里的新（见 config_source）
const CHAT_MODEL_KEY = "fa.chatModel";
function loadChatModelChoice() {
  try {
    const parsed = JSON.parse(localStorage.getItem(CHAT_MODEL_KEY) || "{}");
    return {
      key: typeof parsed.key === "string" ? parsed.key : "mock",
      touched: parsed.touched === true,
    };
  } catch {
    return { key: "mock", touched: false };
  }
}
const chatModelChoice = loadChatModelChoice();
let chatModelTouched = chatModelChoice.touched === true;
function saveChatModelChoice(touched = chatModelTouched) {
  chatModelTouched = touched;
  try {
    localStorage.setItem(CHAT_MODEL_KEY, JSON.stringify({
      key: chatModelChoice.key,
      touched: chatModelTouched,
    }));
  } catch { /* quota */ }
}
const chat = {
  active: false,        // composer 当前属于一段对话，而不是委托/运行
  current: null,        // 打开的线程（含 messages）
  threads: [],
  streaming: false,
  abort: null,
  attachments: [],
  editing: null,
  paintTimer: null,
  limit: 50,
  total: 0,
  deleteArmed: null,
  deleteTimer: null,
  cards: new Map(),     // messageId -> proposal card state (draft/mode/sources/busy/error)
  mirrors: new Map(),   // messageId -> { runId, trace, live, es } — the run's own facts
};
let runHistory = [];    // /api/runs
let toolContracts = null; // /api/tools，工具契约（含版本与 effect）
let modules = [];         // /api/modules，域模块（金融研究 / 审计 / 量化…）

/* 模型与思考强度选择（对齐 kimi.com composer 的模型徽章形态）。
   选择只作用于“下一个新委托”；运行中的徽章只读展示该 run 实际模型。 */
const EFFORT_LEVELS = ["low", "medium", "high"];
const EFFORT_LABEL = { low: "低", medium: "中", high: "高" };
let modelCatalog = [];  // /api/models
let modelChoice = (() => {
  try { return JSON.parse(localStorage.getItem("fa.model") || "") || {}; } catch { return {}; }
})();
modelChoice = {
  key: typeof modelChoice.key === "string" ? modelChoice.key : "mock",
  effort: EFFORT_LEVELS.includes(modelChoice.effort) ? modelChoice.effort : "medium",
  touched: modelChoice.touched === true,
};
/* 草稿预览的预算联动：/api/models?template= 返回各模型在该模板下真实生效的
   预算顶（服务端是缩放的单一事实源），按模板缓存，选择器变更即失效。 */
let modelBudget = { template: "", models: null };
/* Fast 模式（Work 委托默认开启）：一轮多动作 + 原生工具调用 + 免逐步审批；
   项目走 git worktree，terminal 只能写工作区。关闭即回退严格路径
   （JSON 单动作 + 逐动作审批）。对话/治理/财务不受影响。 */
let fastMode = (() => {
  try { return localStorage.getItem("fa.fastMode") !== "0"; } catch { return true; }
})();
function setFastMode(on) {
  fastMode = on;
  try { localStorage.setItem("fa.fastMode", on ? "1" : "0"); } catch { /* 私有模式忽略 */ }
}
/* “+”附加面板：资料选择 / 工具注册表 / 预算微调。
   三项都是真实委托输入：scope 与 budget 随 spec 提交并被 run 线程消费。 */
const attach = {
  open: false,
  openGroup: null,       // 手风琴单开：null | tpl | sources | tools | budget
  corpus: [],
  tools: [],
  loadedTemplate: "",
  scope: new Set(),
  budget: null,          // null = 未改，用当前模型/模板的服务端草案
  sources: [],           // 通用委托：fetch_url 可访问的 URL 清单（spec.sources）
  sourcesDraft: "",
  project: loadProjectContext(),
  projectEditing: false,
  mcp: { servers: [], loaded: false },  // MCP 导入的真实可用性
};
const projectWork = createProjectWork({ esc });

function loadProjectContext() {
  try {
    const saved = JSON.parse(localStorage.getItem("fa.workProject") || "{}");
    return {
      path: typeof saved.path === "string" ? saved.path : "",
      verification: typeof saved.verification === "string" && saved.verification ? saved.verification : "pytest -q",
    };
  } catch { return { path: "", verification: "pytest -q" }; }
}

function saveProjectContext() {
  try { localStorage.setItem("fa.workProject", JSON.stringify(attach.project)); } catch { /* quota */ }
}

function renderProjectComposer() {
  const host = document.getElementById("project-composer");
  if (!host) return;
  host.hidden = true;  // 工作模式退役后项目直接执行入口不再出现（chat 走 intake 的「添加项目」）
  host.innerHTML = projectWork.projectComposerHtml(attach.project, attach.projectEditing);
}

let scenesOpen = localStorage.getItem("fa.scenesOpen"); // null | "1" | "0"

/* 多会话：每个 run 一个会话对象；后台会话保持 SSE 流，只是不渲染 */
const sessions = new Map();

/* ---------------------------------------------------------------- helpers */

/* RENDER LAYER — 主区唯一 DOM 入口；innerHTML/dataset/scroll 可调，状态赋值只保留 mode。 */
function renderMain(html, { mode = "run", scroll = null } = {}) {
  const main = document.getElementById("main");
  if (!main) return;
  hideConnectionStatus();
  // 救援：舞台态时 composer 住在 #main 的 stage-dock 里，必须先搬回 footer，
  // 否则下面的 innerHTML 替换会连同单实例节点与其事件绑定一起销毁
  const liveComposer = document.querySelector(".stage-dock > .composer");
  if (liveComposer) {
    const wrap = document.querySelector(".composer-wrap");
    if (wrap) wrap.appendChild(liveComposer);
    document.getElementById("app")?.classList.remove("stage-mode");
  }
  main.innerHTML = html;
  // 离开审计面就清掉 #/audit hash:URL 必须始终反映当前 surface,否则用户
  // 刷新/分享时会被旧深链拽回审计台。renderMain 是视图变更的唯一入口,
  // 「新对话」等不经过 renderMode 的路径也在这里被覆盖。
  const previousView = main.dataset.view;
  if (previousView === "audit" && mode !== "audit"
      && auditHashTarget(location.hash) !== null) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  main.dataset.view = mode;
  store.mode = mode;
  paintNavActive();
  setStageMode(mode === "empty");
  document.getElementById("app")?.classList.toggle("chat-mode", mode === "chat");
  renderProjectComposer();
  // composer 只属于对话语境：功能页（账本 / 判断记录 / 审计台 / 定时任务 / 设置）不显示输入框
  const composerWrap = document.querySelector(".composer-wrap");
  // 审计台属治理区:保留治理 composer(零模型 console 在证据页也应可用);
  // reviews/scheduled/ledger/settings 仍是纯功能页,不显示输入框。
  if (composerWrap) composerWrap.hidden = (mode === "reviews" || mode === "scheduled"
    || mode === "ledger" || mode === "settings");
  const scroller = document.querySelector(".main");
  if (scroll === "top" && scroller) scroller.scrollTop = 0;
  else if (scroll === "bottom" && scroller) scroller.scrollTop = scroller.scrollHeight;
}

/* 治理视图的 composer 瘦身（零模型承诺要看得见）：
   隐藏模型徽章 / 审批按钮 / 附件按钮,placeholder 换成治理命令用法。
   离开治理视图时只恢复模型徽章——审批/附件由 paintChatChrome 按语境
   统一恢复(chat 隐藏、其余显示),避免两个画家互相打架。 */
const GOV_PLACEHOLDER = "输入治理命令：/挖洞 <target> · /判断 · /链 <audit_id>";

function paintGovChrome(gov) {
  const modelBtn = document.getElementById("model-btn");
  const apvBtn = document.getElementById("approval-btn");
  const plusBtn = document.getElementById("composer-open");
  const ta = document.getElementById("composer-ta");
  if (gov) {
    const fastBtn = document.getElementById("fast-btn");
    for (const el of [modelBtn, apvBtn, plusBtn, fastBtn]) if (el) el.style.display = "none";
    closeAddMenu();
    if (ta) {
      if (ta.dataset.govPlaceholder !== "1") {
        ta.dataset.prevPlaceholder = ta.placeholder;
        ta.dataset.govPlaceholder = "1";
      }
      ta.placeholder = GOV_PLACEHOLDER;
    }
    return;
  }
  if (modelBtn) modelBtn.style.display = "";
  if (plusBtn) {
    plusBtn.style.display = "";
    plusBtn.title = chat.active ? "添加内容" : "资料 / 工具 / 预算";
  }
  const fastBtn = document.getElementById("fast-btn");
  if (fastBtn) fastBtn.style.display = "";
  if (ta && ta.dataset.govPlaceholder === "1") {
    ta.placeholder = ta.dataset.prevPlaceholder || "给 FCode 发消息…";
    delete ta.dataset.govPlaceholder;
    delete ta.dataset.prevPlaceholder;
  }
}

/* 主区视图入口：只有导航动作走这里；各渲染函数是具体实现。 */
const MODE_ENTRY = {
  run: payload => render(payload.trace, payload.title),
  // R1:工作区对话的着陆点——区切换器的「工作区」= 全新对话,与治理台对称
  chat: () => newChat(),
  intake: () => renderIntake(),
  ledger: () => openLedger(),
  settings: () => openSettingsPage(),
  reviews: () => renderReviews(),
  gov: () => renderGov(),
  audit: () => renderAudit(),
  scheduled: () => renderScheduledPage(),
};

document.addEventListener("click", event => {
  const suggestion = event.target.closest("[data-stage-prompt]");
  if (suggestion) {
    const input = document.getElementById("composer-ta");
    input.value = suggestion.dataset.stagePrompt || "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    autosize(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
});

function renderMode(mode, payload = {}) {
  // 所有导航入口都让旧页面加载失效；selectRun/select 自己已持有当日世代，
  // 通过 internalView 避免把自身误判成陈旧请求。
  if (!payload.internalView) viewGate.begin();
  if (mode !== "chat") leaveChat();
  const entry = MODE_ENTRY[mode];
  if (!entry) throw new Error(`unknown view mode: ${mode}`);
  store.mode = mode;
  const result = entry(payload);
  // 治理区两个视图(治理台/审计台)共用零模型 chrome
  paintGovChrome(mode === "gov" || mode === "audit");
  return result;
}

const runLabel = run => run.name || shortObjective(run.objective);

const stateClass = status =>
  status === "succeeded" ? "ok" : (status === "denied" || status === "failed") ? "bad" : "warn";
const stateText = status =>
  ({ succeeded: "成功", failed: "失败", denied: "拒绝",
     recovery_required: "待恢复", running: "运行中", stopped: "已停止" }[status] || (status || "?"));


/* ------------------------------------------------ RENDER LAYER — 工具摘要 */

function describe(observation) {
  const tool = observation.tool;
  const r = observation.result || {};
  switch (tool) {
    case "list_documents":
      return [(r.documents || []).map(d => `${d.id}(${d.unit_label}/${d.basis})`).join("  ")];
    case "read_document":
      return [`${r.period} · ${r.unit_label} · ${r.basis}`, `<span class="dim">${esc((r.body || "").slice(0, 72))}…</span>`];
    case "normalize_units":
      return [`${r.source_unit} → ${r.target_unit} ×${num(r.factor)} · 毛利 ${num(r.gross_profit_cny)} 元`];
    case "reconcile_basis":
      if (r.reconciled === false) return [`${r.period} · <span class="yel">无法桥接：${esc(r.reason || "没有公布的桥接说明")}</span>`];
      return [`${r.period} ${r.from_basis} → ${r.to_basis} · 差异 ${r.delta >= 0 ? "+" : ""}${r.delta}`, `<span class="dim">${esc(r.reason)}</span>`];
    case "check_restatement":
      return r.restated
        ? [`<span class="yel">已重述</span> ${num(r.original_gross_profit)} → ${num(r.restated_gross_profit)}（${r.delta >= 0 ? "+" : ""}${r.delta}）`, `<span class="dim">${esc(r.reason || "")}</span>`]
        : ["无重述"];
    case "calculate_margin":
      return r.computed
        ? [`2024 ${pct(r.margin_bp_2024)} · 2025 ${pct(r.margin_bp_2025)} · <span class="grn">${pp(r.change_bp)}</span>`]
        : [`<span class="red">无法计算：${esc((r.blockers || []).join("；"))}</span>`];
    case "report_blocked":
      return [`声明受阻：${esc(r.reason)}`];
    case "ls_workspace": {
      const entries = r.entries || [];
      return [`${num(entries.length)} 个文件`, `<span class="dim">${esc((entries[0] || {}).path || "空工作区")}</span>`];
    }
    case "read_file":
      return [`${esc(r.path || "")} · ${num(r.bytes)} bytes`, `<span class="dim">${esc(String(r.content || "").slice(0, 72))}…</span>`];
    case "write_file":
      return [`${esc(r.path || "")} · ${r.changed ? "已写入" : "无变化"} · ${num(r.bytes)} bytes`, `<span class="dim">sha ${esc(shortHash(r.sha256_after))}…${r.commit ? " · " + esc(r.commit) : ""}</span>`];
    case "edit_file":
      return [`${esc(r.path || "")} · 锚定替换 · ${num(r.bytes)} bytes`, `<span class="dim">sha ${esc(shortHash(r.sha256_after))}…</span>`];
    case "run_command":
      return [`${esc(r.command || "")} · 退出码 ${esc(r.exit_code)} · ${num(r.duration_ms)} ms`, `<span class="dim">${esc(String(r.output || "").split("\n")[0].slice(0, 72))}…</span>`];
    case "fetch_url":
      return [`${esc(r.host || r.url || "")} · HTTP ${esc(r.status)} · ${num(r.bytes)} bytes`, `<span class="dim">${esc(r.title || r.url || "")}</span>`];
    default:
      return [esc(JSON.stringify(r))];
  }
}

/* ------------------------------------------------ RENDER LAYER — 侧栏导航 */

const labelOf = key => (SCENARIOS.find(s => s[0] === key) || [key, key])[1];
const tplTitle = key => {
  const template = templates.find(t => t.key === key);
  return template ? template.title.split("：")[0] : (TPL_FALLBACK[key] || key || "—");
};
function templateGroups() {
  const groups = new Map();
  for (const template of templates) {
    const key = template.module || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(template);
  }
  return [...groups.entries()].map(([key, items]) => ({
    key,
    title: (modules.find(module => module.key === key) || {}).title || key || "模块",
    items,
  }));
}

const shortObjective = text => {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length > 22 ? s.slice(0, 22) + "…" : s;
};

let ledgerOpen = false;
let ledgerConfirmId = null;

/* 侧栏渲染：两套结构，同一份渲染函数。
   - 工作侧栏照 Kimi Work（AppSidebar）：平坦导航 + 置顶 + Projects + 任务。
   - 对话侧栏照 kimi.com 网页版：主入口 + More 折叠 + 置顶 + Projects + 对话。
   导航条目全部走 data-open，两个面板各放一份也不会互相抢 active。 */

/* ================= Projects（运行分组，v1 单设备存储） ================= */
/* 置顶分组（Kimi 的 pinned-list），没有置顶就整段不渲染。
   运行用服务端 run.pinned（/api/runs/{id}/meta）；对话目前没有可变 meta，
   本机置顶（fa.pins），语义在文档里写明。 */
const PIN_KEY = "fa.pins";
let pins = JSON.parse(localStorage.getItem(PIN_KEY) || "[]");
const savePins = () => { try { localStorage.setItem(PIN_KEY, JSON.stringify(pins)); } catch { /* quota */ } };
const isChatPinned = id => pins.includes(id);

function toggleChatPin(threadId) {
  pins = isChatPinned(threadId) ? pins.filter(id => id !== threadId) : pins.concat(threadId);
  savePins();
  renderNav();
}

/* 侧栏动作：与 Kimi 的三个动作一一对应。
   - 新建任务 = Kimi Work 固定区那一行（图标 addConversation + ⌘K chip）
   - 新建项目 = Projects 标题右侧的「＋」（网页版叫 create-label）
   - 段落折叠 = section-title-button 的 chevron（NextSidebarSection 的 collapsible） */
function newTask() {
  // 统一侧栏只有一套列表:新建任务 = 新对话(工作模式已退役)。
  newChat();
}

function quickStartGo(step) {
  if (step === "provider") { if (!settingsOpen) toggleSettingsPage(); return; }
  if (step === "firstRun") { newTask(); return; }
  markQuickStart("ledgerSeen");
  renderMode("ledger");
}

function clearNavActive() {
  for (const el of document.querySelectorAll(".nav-item.active")) el.classList.remove("active");
}

/* ------------------ MIXED — trace 成本回填逻辑（不改）+ 账本渲染模板 */

function openLedger() {
  leaveChat();
  if (intake.active) closeIntake(false);
  if (settingsOpen) closeSettingsPage(false);
  ledgerOpen = true;
  if (activeSession()) detachLive();
  store.key = null;
  store.trace = null;
  renderNav();
  renderTabs();
  document.getElementById("tb-title").textContent = "工作台";
  document.getElementById("tb-chips").innerHTML = "";
  const badge = document.getElementById("chain-badge");
  if (badge) {
    badge.className = "chain-pill";
    badge.innerHTML = `<span class="dot"></span><span>未加载</span>`;
  }
  const scroller = document.querySelector(".main");
  if (scroller) scroller.scrollTop = 0;
  renderLedger();
}

/* 运行轨迹一律走 API，而不是 `runs/<id>/trace.json` 这个静态路径：静态路径只在
   运行目录恰好位于 viewer 检出内（本地开发默认）时才取得到，桌面版把运行放在
   Application Support，于是重启后点开任何已结束的运行都是 404。服务端从运行目录
   读并做 jail 校验，两种部署走同一条路。 */
function runTraceUrl(runId) {
  return `/api/runs/${encodeURIComponent(runId)}/trace`;
}

async function renderLedger() {
  const main = document.getElementById("main");
  if (!main || !ledgerOpen) return;
  const epoch = viewGate.sequence;
  renderMain(`<div class="empty">统计账本…</div>`, { mode: "ledger" });
  // 旧运行 meta 里没有成本摘要：按需拉 trace 补齐（有缓存）
  const missing = runHistory.filter(run => !run.cost && run.tracePath);
  await Promise.all(missing.map(async run => {
    try {
      const response = await fetch(runTraceUrl(run.id), { cache: "no-store" });
      if (!response.ok) return;
      const trace = await response.json();
      cache.set("run:" + run.id, trace);
      const rounds = trace.rounds || [];
      for (let i = rounds.length - 1; i >= 0; i--) {
        const b = rounds[i].budget_after;
        if (b && b.calls_used !== undefined) {
          const lim = b.limits || {};
          run.cost = {
            calls: b.calls_used, reads: b.reads_used,
            costMicrousd: b.cost_used_microusd, tokens: b.tokens_used,
            maxCalls: lim.max_calls, maxCostMicrousd: lim.max_cost_microusd,
          };
          break;
        }
      }
    } catch { /* 单条失败不拖垮账本 */ }
  }));
  if (!ledgerOpen || epoch !== viewGate.sequence) return;

  const finished = runHistory.filter(run => run.status !== "running");
  const totalCost = finished.reduce((s, r) => s + ((r.cost && r.cost.costMicrousd) || 0), 0);
  const totalCalls = finished.reduce((s, r) => s + ((r.cost && r.cost.calls) || 0), 0);
  const succeeded = finished.filter(run => run.status === "succeeded").length;
  const running = runHistory.filter(run => run.status === "running").length;
  const rate = finished.length ? Math.round((succeeded / finished.length) * 100) : 0;
  const rows = runHistory.slice(0, 30).map(run => {
    const cost = run.cost ? `${num(run.cost.costMicrousd)} µUSD` : "—";
    const created = String(run.created || "").replace("T", " ").slice(5, 16);
    const confirmDelete = ledgerConfirmId === run.id;
    // 父子关系:子代理行亮"子代理"徽章;父运行行亮扇出计数。徽章本身
    // 可点:子行跳父,父行跳第一个子(数据来自轮询的 runHistory)。
    const parentId = (run.spec || {}).parent_run_id || "";
    const childRuns = runHistory.filter(r => (r.spec || {}).parent_run_id === run.id);
    const childCount = childRuns.length;
    const lineage = parentId
      ? ` <button class="led-lineage" type="button" data-led-row-jump="${esc(parentId)}" title="父运行 ${esc(parentId)} · 点击打开">子代理</button>`
      : (childCount ? ` <button class="led-lineage" type="button" data-led-row-jump="${esc(childRuns[0].id)}" title="${childCount} 个子代理运行 · 点击打开">子代理 ×${childCount}</button>` : "");
    // 来源:manual 不标(默认);scheduled/subagent 如实标注发起方
    const sourceChip = run.source === "scheduled"
      ? ` <span class="led-lineage" title="由定时任务调度器创建">排程</span>`
      : (run.source === "subagent" ? ` <span class="led-lineage" title="由父运行的 spawn 扇出创建">子代理来源</span>` : "");
    return `<tr data-led-row="${esc(run.id)}" title="${esc(run.objective)}">
      <td><span class="sdot ${run.status === "running" ? "live" : stateClass(run.status)}"></span> ${esc(stateText(run.status))}</td>
      <td class="led-name">${esc(runLabel(run))}${lineage}${sourceChip}</td>
      <td class="dim">${esc(tplTitle(run.template))}</td>
      <td class="num">${run.rounds || "—"}</td>
      <td class="num">${cost}</td>
      <td class="dim">${esc(created)}</td>
      <td><button class="a-btn${confirmDelete ? " danger" : ""}" data-led-del="${esc(run.id)}">${confirmDelete ? "确认删除？" : "删除"}</button></td>
    </tr>`;
  }).join("");

  renderMain(`
    <section class="prompt">
      <div class="prompt-kicker"><span class="kicker-label">WORKBENCH</span><span>委托账本</span></div>
      <p class="prompt-text">委托账本</p>
      <p class="prompt-note">所有委托的状态、轮次与成本。点击行打开对应运行；删除会移除运行目录，运行中不可删。</p>
    </section>
    <div class="led-stats">
      <div class="led-stat"><div class="k">委托总数</div><div class="v">${runHistory.length}</div></div>
      <div class="led-stat"><div class="k">运行中</div><div class="v">${running}</div></div>
      <div class="led-stat"><div class="k">成功率</div><div class="v">${rate}<i>%</i></div></div>
      <div class="led-stat"><div class="k">总成本</div><div class="v">${num(totalCost)}<i> µUSD</i></div></div>
      <div class="led-stat"><div class="k">总调用</div><div class="v">${num(totalCalls)}</div></div>
    </div>
    <div class="card led-card">
      <table class="led-table">
        <tr><th>状态</th><th>委托</th><th>类型</th><th>轮次</th><th>成本</th><th>创建</th><th></th></tr>
        ${rows || `<tr><td colspan="7" class="dim" style="text-align:center;padding:22px">还没有委托 — 点上方「新建委托」开始</td></tr>`}
      </table>
    </div>`, { mode: "ledger" });
  updateStatusline();
}

async function deleteRun(id) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
  } catch (error) {
    toast("删除失败：" + error.message);
    return;
  }
  const s = sessions.get(id);
  if (s) {
    setConnection(s, { type: "close" });
    if (s.es) s.es.close();
    sessions.delete(id);
  }
  if (store.sessionId === id) store.sessionId = null;
  cache.delete("run:" + id);
  if (store.key === "run:" + id) {
    store.key = null;
    store.trace = null;
    renderMode("ledger");
  } else if (ledgerOpen) {
    renderLedger();
  }
  renderTabs();
  refreshRuns({ force: true });
  toast("委托已删除");
}

function setFoot(text, cls = "") {
  const label = document.getElementById("foot-text");
  if (label) label.textContent = text;
  const dotEl = document.getElementById("foot-dot");
  if (dotEl) dotEl.className = "fdot" + (cls ? " " + cls : "");
}

function closeRowMenus() {
  for (const menu of document.querySelectorAll(".row-menu")) menu.hidden = true;
}

/* ------------------------------- RENDER LAYER — 证据面板内容与事件列表 */

const {
  renderRail, renderMeters, tokenSplitHtml,
  resultConclusion, failureConclusion, renderToolDetail, resultBlockKind,
} = createRunEvidence({
  esc, pct, pp, num, shortHash, stripTags, doodleSvg, describe, lastBudget,
  EVENT_DOT, HUMAN_EVENTS, REVIEW_LABEL, ICON, FAILURE_REASONS, evExpanded,
  getRailFilter: () => railFilter,
  getRailTab: () => railTab,
  getToolContracts: () => toolContracts,
});



/* ----------------------- RENDER LAYER — 判断面 / 结论后的结构化块 */

function resultUnit(result) {
  if (result.unit) return result.unit;
  const periods = result.periods || [];
  if (periods.some(period => period && (period.gross_profit_cny !== undefined || period.revenue_cny !== undefined))) return "CNY";
  if ((result.citations || []).some(citation => citation && citation.value_cny !== undefined)) return "CNY";
  return "—";
}

const {
  reviewRunId, loadReviewProfile, reviewPanelHtml, chooseReviewDecision,
  startAppendReview, applyReviewSuggestion, syncReviewDraft, submitReview,
  waitForReviewTraceRefresh,
} = createReviewUI({
  esc, verifyChain, REVIEW_DECISIONS, REVIEW_DECISION, REVIEW_LABEL, REVIEW_COUNT_LABEL,
  store, cache, activeSession, currentRun, shownTrace, runTraceUrl, setChainBadge, toast,
});

function judgmentFactsHtml(result) {
  const periods = result.periods || [];
  const first = periods[0] || {};
  const second = periods[1] || {};
  const sources = (result.citations || [])
    .filter(citation => citation && citation.doc_id)
    .map(citation => `${citation.period ? citation.period + " " : ""}${citation.doc_id}`);
  return `<div class="judgment-facts">
    <span class="jf"><span class="jk">${esc(first.period || "FY2024")}</span><span class="jv">${pct(result.margin_bp_2024)}</span></span>
    <span class="jf"><span class="jk">${esc(second.period || "FY2025")}</span><span class="jv">${pct(result.margin_bp_2025)}</span></span>
    <span class="jf"><span class="jk">变化</span><span class="jv">${pp(result.change_bp)}</span></span>
    ${sources.length ? `<span class="judgment-source">来源 ${esc(sources.join(" · "))}</span>` : ""}
  </div>`;
}

function judgmentRowsHtml(result) {
  const comparable = result.comparable === true ? "可比" : result.comparable === false ? "不可比" : "未标注";
  const basis = result.basis || "—";
  const restatement = result.restatement_applied ? "已应用重述" : "未应用重述";
  return `<div class="judgment-rows">
    <div class="jrow"><span class="jk">可比性</span><span class="jv"><b>${esc(comparable)}</b> · 准则 ${esc(basis)} · ${esc(restatement)}</span></div>
    <div class="jrow"><span class="jk">单位/准则</span><span class="jv"><b>已归一</b> · ${esc(resultUnit(result))} · ${esc(basis)}</span></div>
  </div>`;
}

function judgmentSurfaceHtml(trace, result) {
  const citations = (result.citations || []).length;
  return `<section class="judgment" aria-label="判断面">
    <div class="judgment-head">
      <span class="judgment-kicker">JUDGMENT · 判断面</span>
      <span class="judgment-meta">${esc(result.verification || "runtime-recomputed")}${citations ? ` · ${citations} 处引用` : ""}</span>
    </div>
    ${judgmentFactsHtml(result)}
    ${judgmentRowsHtml(result)}
    <div class="judgment-block">
      <div class="jk">解释候选</div>
      <div class="j-explanations">
        <div class="j-exp">
          <div class="j-exp-head"><span class="j-exp-title">经营变化</span><span class="j-tag pending">剩余解释</span></div>
          <div class="j-exp-note">缺分产品/销量数据，无法区分价格 vs 结构</div>
        </div>
        <div class="j-exp">
          <div class="j-exp-head"><span class="j-exp-title">口径/重述</span><span class="j-tag ok">已核对 · 已排除</span></div>
        </div>
        <div class="j-exp">
          <div class="j-exp-head"><span class="j-exp-title">单位/准则</span><span class="j-tag ok">已核对 · 已排除</span></div>
        </div>
      </div>
    </div>
    <div class="judgment-block">
      <div class="jk">待判断</div>
      <ol class="j-pending">
        <li>是否接受“经营变化”</li>
        <li>是否需要补充分产品数据</li>
      </ol>
    </div>
    <div class="judgment-impact">
      <span class="jk">会影响结论</span>
      <span>分产品毛利缺失时，应保留意见</span>
    </div>
    <div class="review-profile" data-review-profile hidden></div>
    ${reviewPanelHtml(trace)}
  </section>`;
}

function failureJudgmentHtml(trace, terminal) {
  const failure = terminal.failure || {};
  const code = failure.code || String(terminal.status || "failed").toUpperCase();
  const message = failure.message || FAILURE_REASONS[failure.code] || "";
  return `<section class="judgment judgment-fail" aria-label="判断面">
    <div class="judgment-head">
      <span class="judgment-kicker">JUDGMENT · 判断面</span>
      <span class="judgment-meta">${esc(terminal.status || "failed")} · 未产出可签字结论</span>
    </div>
    <div class="judgment-fail-note">
      <span class="jk">失败依据</span>
      <code>${esc(code)}</code>
      ${message ? `<span class="jv">${esc(message)}</span>` : ""}
    </div>
    <p class="judgment-empty">失败不补解释；只记录你对失败依据的判断。</p>
    <div class="review-profile" data-review-profile hidden></div>
    ${reviewPanelHtml(trace)}
  </section>`;
}

function generalResultHtml(trace, result, artifacts) {
  const plan = result.plan || {};
  const truth = (artifacts.find(entry => entry.content_truth) || {}).content_truth || "runtime-verified-file-state";
  return `<div class="card result">
    <div class="card-head">
      <span class="card-title">${ICON.inkTick} 产物</span>
      <span class="badge pass" style="margin-left:2px">${doodleSvg("shield-check", "draw")}<span>已验证</span></span>
      <span class="grow"></span>
      <span class="card-meta">succeeded · ${esc(result.verification || "runtime-recomputed")}</span>
    </div>
    <p class="result-answer">通用委托已完成；${artifacts.length} 个文件通过 Runtime 文件状态复核。</p>
    <div class="metrics">
      ${artifacts.map(entry => `<div class="metric"><div class="k">${esc(entry.path)}</div><div class="v">${num(entry.bytes)} B</div></div>`).join("")}
      <div class="metric"><div class="k">计划</div><div class="v">${esc(plan.done ?? "—")}/${esc(plan.total ?? "—")}</div></div>
    </div>
    <div class="proof-line">
      <span class="proof-mark">已复算</span>
      <span>${esc(result.verification || "runtime-recomputed")}</span>
      <span class="proof-sep">·</span>
      <span>${artifacts.length} 个文件</span>
      <span class="proof-sep">·</span>
      <span>${(trace.events || []).length} 个事件</span>
    </div>
    <div class="result-foot">
      <div class="chips" style="margin-top:2px">
        ${artifacts.map(entry => `<span class="mini" title="${esc(entry.sha256 || "")}">${esc(entry.path)} · ${esc(shortHash(entry.sha256))}…</span>`).join("")}
        <span class="mini">${esc(truth)}</span>
      </div>
      ${runActionsHtml(trace)}
    </div>
  </div>`;
}

function projectRunId(trace) {
  const taskId = String(trace?.task?.id || "");
  return taskId.startsWith("task-") ? taskId.slice(5) : (activeSession()?.id || "");
}

function currentWorkTab() {
  return activeSession()?.workTab || store.workTab || "activity";
}

function refreshProjectViews(trace) {
  if (!isProjectTrace(trace) || shownTrace()?.task?.id !== trace.task?.id) return;
  const active = currentWorkTab();
  const tabs = document.querySelector("[data-work-tabs]");
  if (tabs) tabs.innerHTML = projectWork.projectTabsHtml(trace, active);
  const changes = document.getElementById("changes-slot");
  if (changes) changes.innerHTML = projectWork.projectResultHtml(trace);
  const files = document.getElementById("files-slot");
  if (files && trace.projectWorkspace) files.innerHTML = projectWork.projectFilesHtml(trace);
  if (active === "files" && !trace.projectWorkspace) loadProjectWorkspace(trace);
}

async function loadProjectDelivery(trace) {
  if (!trace.project || trace.project.patchAvailable || trace.project.loading) return;
  const runId = projectRunId(trace);
  if (!runId) return;
  trace.project.loading = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/project`, { cache: "no-store" });
    if (!response.ok) return;
    trace.project = await response.json();
    refreshProjectViews(trace);
  } catch { /* The code changes remain visible in the trace and workspace. */ }
  finally { if (trace.project) delete trace.project.loading; }
}

async function loadProjectWorkspace(trace) {
  if (!isProjectTrace(trace) || trace.projectWorkspace || trace.projectWorkspaceLoading) return;
  const runId = projectRunId(trace);
  if (!runId) return;
  trace.projectWorkspaceLoading = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/workspace`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    trace.projectWorkspace = data;
  } catch (error) {
    trace.projectWorkspace = { entries: [], error: error.message };
  } finally {
    delete trace.projectWorkspaceLoading;
    refreshProjectViews(trace);
  }
}

function renderHead(trace, title) {
  const task = trace.task || {};
  const heading = title || task.id || "FCode";
  document.getElementById("tb-title").textContent = heading;
  const bridge = desktopBridge();
  if (bridge && bridge.set_title) bridge.set_title(heading);
  document.getElementById("tb-chips").innerHTML = "";
}

/* RENDER LAYER — 舞台式空状态：标识、字标与提示；composer 由 setStageMode
   以单实例 appendChild 搬进 stage-dock，模板 chips 在其下。 */
function stageEmptyHtml(note = "给想法一个运行空间", opts = {}) {
  return moonshotStageHtml(note, { ...opts, footnote: WORK_STAGE_FOOTNOTE });
}

/* RENDER LAYER — composer 双形态（对齐 kimi.com 的居中舞台 → 吸底节奏）。
   单实例搬运：同一个 .composer 节点在 footer 与舞台容器之间 appendChild，
   不复制第二份 DOM；事件绑定都在 composer 内部元素上，随节点走零风险。 */
function setStageMode(on) {
  const app = document.getElementById("app");
  if (!app) return;
  const composer = document.querySelector(".composer-wrap > .composer, .stage-dock > .composer");
  if (!composer) return;
  const dock = document.getElementById("stage-dock");
  const wrap = document.querySelector(".composer-wrap");
  if (on && dock && composer.parentElement !== dock) {
    dock.appendChild(composer);
    renderStageChips();
  } else if (!on && wrap && composer.parentElement !== wrap) {
    wrap.appendChild(composer);
  }
  app.classList.toggle("stage-mode", !!on);
}

/* 居中 composer 下的模板 chips：与 composer 模板选择器同源（/api/templates），
   点击 = 选中该模板 + 预填目标 + 聚焦，是真实委托入口。

   七条可靠性场景（越权工具/预算耗尽/崩溃恢复/未知结果/目标无法完成…）是这台机器
   的故障注入夹具，用来证明门控与恢复是真的——它们是演示素材，不是产品前门。
   因此真实任务模板直接露出，夹具收进折叠的「演示场景」里，仍然可达。 */
/* 只有故障注入的那几个才算演示场景——它们演示的是"这台机器出错时如实交代"，
   属于可靠的证据，不属于任务形态。「完整研究」是真任务，留在前台。 */
const DEMO_SCENARIOS = new Set([
  "tool-denied", "budget", "budget-resume", "crash", "unknown", "open-ended",
]);

function renderStageChips() {
  const slot = document.getElementById("stage-chips");
  if (!slot) return;
  const source = (templates && templates.length)
    ? templates.map(t => [t.key, (t.title || t.key).split("：")[0]])
    : SCENARIOS.map(([key, label]) => [key, label]);
  const tasks = source.filter(([key]) => !DEMO_SCENARIOS.has(key));
  const demos = source.filter(([key]) => DEMO_SCENARIOS.has(key));
  const chip = ([key, label]) =>
    `<button type="button" class="stage-chip" data-stage-chip="${esc(key)}" title="以「${esc(label)}」模板开始委托">${esc(label)}</button>`;
  slot.innerHTML = [
    tasks.map(chip).join(""),
    demos.length ? `<details class="stage-demos"><summary>演示场景 · ${demos.length}</summary>
      <div class="stage-demo-chips">${demos.map(chip).join("")}</div>
      <p class="stage-demo-note">可靠性夹具：故意触发越权、耗尽预算、杀掉进程或制造未知结果，用来看门控与恢复是否如实。</p>
    </details>` : "",
  ].join("");
}

/* RENDER LAYER — 阅读列会话头：标题下方一行 meta；证据面板按需打开。 */
function sessionHeadHtml(trace, title, note = "") {
  const task = trace.task || {};
  const binding = task.binding || {};
  const grant = task.model_grant || {};
  const moduleKey = binding.module || "";
  const moduleTitle = moduleKey
    ? ((modules.find(item => item.key === moduleKey) || {}).title || moduleKey)
    : "";
  const meta = [
    moduleTitle,
    binding.corpus,
    task.id,
    grant.grant_id ? `${grant.grant_id} · r${grant.revision ?? ""}` : "",
    note,
  ].filter(Boolean).map(item => esc(item)).join(" · ");
  const origin = (binding.spec || {}).origin_thread || null;
  const originChip = origin
    ? `<button class="meta-button origin-chip" type="button" data-origin-thread="${esc(origin.threadId || "")}" title="来自对话${origin.threadTitle ? "：" + esc(origin.threadTitle) : ""} · 点击跳回">来自对话${origin.threadTitle ? " · " + esc(origin.threadTitle.slice(0, 18)) : ""}</button>`
    : "";
  // 子代理谱系:子运行亮"子代理·可跳父";父运行亮扇出计数(可跳第一个子)
  const parentRunId = (binding.spec || {}).parent_run_id || "";
  const childRuns = runHistory.filter(r => (r.spec || {}).parent_run_id === task.id);
  const lineageChip = parentRunId
    ? `<button class="meta-button" type="button" data-led-row-jump="${esc(parentRunId)}" title="父运行 ${esc(parentRunId)} · 点击打开">子代理 · 父 ${esc(parentRunId.slice(-8))}</button>`
    : (childRuns.length
      ? `<button class="meta-button" type="button" data-led-row-jump="${esc(childRuns[0].id)}" title="${childRuns.length} 个子代理运行 · 点击打开">子代理 ×${childRuns.length}</button>`
      : "");
  const name = (currentRun() && currentRun().name) || shortObjective(task.objective || "") || "未命名委托";
  return `<header class="session-head">
    <p class="session-kicker">${esc(title || task.id || "运行")}</p>
    <h1 class="session-title">${esc(name)}</h1>
    <div class="session-meta">
      <span>${meta}</span>
      ${lineageChip}
      ${originChip}
      <span class="grow"></span>
      <button class="meta-button" type="button" data-rail-open>事件 / 链验证</button>
    </div>
  </header>`;
}

function setChainBadge(check) {
  const badge = document.getElementById("chain-badge");
  badge.className = "chain-pill " + (check.ok ? "ok" : "bad");
  const shield = (check.ok && window.DOODLE_ICONS && window.DOODLE_ICONS.animated
    && window.DOODLE_ICONS.animated["shield-check"]) || "";
  badge.innerHTML = check.ok
    ? `${shield}<span>链验证通过 · ${check.events} events</span>`
    : `<span class="dot"></span><span>链断裂 · ${esc(check.reason)}</span>`;
}

function setMobileNavigation(open) {
  const app = document.getElementById("app");
  app.classList.toggle("mobile-nav-open", !!open);
  document.getElementById("workspace-menu")?.setAttribute("aria-expanded", String(!!open));
}

/* RENDER LAYER — 纯 UI 开关：侧栏折叠与证据浮层不接触运行状态。 */
function setChromeState({ sidebar = null, rail = null } = {}) {
  if (rail !== null) railOpen = rail;
  applyChromeState({
    app: document.getElementById("app"),
    sideToggle: document.getElementById("sidebar-toggle"),
    railPanel: document.querySelector(".rail"),
    railToggle: document.getElementById("rail-toggle"),
  }, { sidebar, rail });
}

function bindToggles(root) {
  /* toggle 已改为 #main 上的事件委托（bind()），这里保留空实现以兼容旧调用 */
}

/* ============================================================ 统一任务线程
   一条线程同时服务：实时运行、历史回放、项目任务、完整研究。
   投影在 app/task-thread.js（纯函数）；这里只负责产品语言注入与 DOM 打补丁。 */

/* 线程接线（threadCtx/paintThread/patchThread/挂载模板）在 app/thread-surface.js
   （拆分切片 3b）；投影仍在 app/task-thread.js。这里只保留进入运行视图的
   render() 与回放渲染，deps 注入 app 侧渲染器。 */
const threadSurface = createThreadSurface({
  describe,
  resultConclusion, failureConclusion, resultBlockKind, renderMeters,
  judgmentSurfaceHtml, failureJudgmentHtml, generalResultHtml,
  runActionsHtml, tplTitle, currentWorkTab,
  projectWork,
  getStore: () => store,
});
const { paintThread, threadMountHtml, projectRunHtml } = threadSurface;

async function render(trace, title) {
  const epoch = viewGate.sequence;
  const check = await verifyChain(trace);
  // 验证链是异步的；期间用户可能已选了别的页面。旧 trace 不得写标题、
  // 主区、状态栏或 store。
  if (epoch !== viewGate.sequence) return;
  store.trace = trace;
  store.check = check;
  store.threadCache = new Map();
  const rounds = trace.rounds || [];
  const terminal = trace.terminal || {};

  renderHead(trace, title);
  setChainBadge(check);

  const started = (trace.events && trace.events[0] && trace.events[0].at)
    ? String(trace.events[0].at).replace("T", " ").slice(0, 19) : "";
  const events = (trace.events || []).length;
  const note = `FCode · ${rounds.length} 轮 · ${events} 个事件 · ${check.ok ? "全链验证通过" : "链验证失败"}${started ? " · " + started + "Z" : ""}`;

  const body = isProjectTrace(trace) ? projectRunHtml(trace)
    : `${threadMountHtml()}`;
  renderMain(`${sessionHeadHtml(trace, title, note)}${body}`, { mode: "run" });
  paintThread(null, trace);
  if (isProjectTrace(trace)) refreshProjectViews(trace);

  if (terminal && terminal.status) {
    loadReviewProfile(trace);
    loadProjectDelivery(trace);
  }
  const replay = !(store.key && store.key.startsWith("run:"));
  const terminalStatus = terminal.status || "";
  const stateText = terminalStatus
    ? `${replay ? "只读回放 · " : ""}${terminalStatus === "succeeded" ? "运行完成" : `运行结束（${terminalStatus}）`}`
    : (replay ? "只读回放" : "运行记录");
  const tone = terminalStatus === "succeeded" ? "ok"
    : (terminalStatus === "failed" || terminalStatus === "denied") ? "bad"
      : terminalStatus ? "warn" : "";
  setFoot(`${traceModelLabel(trace) || "模型未知"} · ${stateText}`, tone);
  if (activeSession()) paintConnection(activeSession());
  renderRail(trace, check);
  updateStatusline();
}

/* -- LOGIC LAYER + RENDER SLOT — SSE/差异渲染状态机不可改；仅模板字符串可改 */

function nearMainBottom() {
  const el = document.querySelector(".main");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}
function scrollMainBottom() {
  const el = document.querySelector(".main");
  el.scrollTop = el.scrollHeight;
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 132) + "px";
}

function liveShell(trace) {
  const task = trace.task || {};
  const body = isProjectTrace(trace) ? projectRunHtml(trace, { live: true }) : threadMountHtml();
  renderMain(`${sessionHeadHtml(trace, "运行 · " + tplTitle(task.binding ? task.binding.scenario : ""), "")}${body}`, { mode: "run" });
  const session = activeSession();
  if (session) session.threadCache = new Map();
}

function liveApply(s, data) {
  const trace = data.snapshot;
  if (!trace) return;
  const stick = nearMainBottom();
  const first = !s.trace;
  s.trace = trace;
  s.lastData = data;
  s.lastError = "";
  store.trace = trace;
  store.key = "run:" + s.id;
  store.check = { ok: true, live: true, events: (trace.events || []).length };

  const scenario = trace.task && trace.task.binding ? trace.task.binding.scenario : s.template;
  renderHead(trace, "运行 · " + tplTitle(scenario));

  if (first) {
    liveShell(trace);
    s.threadCache = new Map();
    s.eventCount = 0;
    s.resultLoaded = false;
  }

  const eventList = trace.events || [];
  const events = eventList.length;

  const pending = data.pending || null;
  if (pending && (!s.pending || s.pending.id !== pending.id)) {
    s.pendingDeadline = Date.now() + (Number(pending.expiresIn) || 0) * 1000;
    s.deciding = null;
  }
  if (!pending) s.deciding = null;
  s.pending = pending;

  // 消息入链回执：排队中的消息一旦以 operator_message 事件落进快照，就移出排队区
  if (s.queuedMsgs && s.queuedMsgs.length) {
    const deliveredIds = new Set();
    const deliveredTexts = new Set();
    for (const e of eventList) {
      if (e.type !== "operator_message") continue;
      const d = (e.payload || {}).detail || {};
      if (d.message_id) deliveredIds.add(String(d.message_id));
      deliveredTexts.add(String(d.text || ""));
    }
    const before = s.queuedMsgs.length;
    s.queuedMsgs = s.queuedMsgs.filter(m => {
      const hit = (m.serverId && deliveredIds.has(m.serverId))
        || (!m.serverId && deliveredTexts.has(m.text));
      if (hit) m.state = "delivered";
      return !hit;
    });
    if (s.queuedMsgs.length !== before) toast("消息已入链 · 下轮生效");
  }

  // 队列权威同步：server 队列是事实源（SSE 帧携带）；在出链回执之后执行，
  // 消费竞态下最多闪现一帧，不吞掉回执提示
  if (Array.isArray(data.queue)) reconcileQueue(s, data.queue);

  if (s.planQueued && events > s.planQueued.events) {
    const added = eventList.slice(s.planQueued.events);
    if (added.some(e => e.type === "plan_steered")) {
      s.planQueued = null;
      toast("计划已修订");
    } else if (added.some(e => e.type === "control_rejected" && ((e.payload || {}).detail || {}).kind === "plan")) {
      s.planQueued = null;
      toast("计划指令被拒绝");
    }
  }
  if (s.budgetQueued && events > s.budgetQueued.events) {
    const added = eventList.slice(s.budgetQueued.events);
    if (added.some(e => e.type === "budget_adjusted")) {
      s.budgetQueued = null;
      toast("预算已生效");
    } else if (added.some(e => e.type === "control_rejected" && ((e.payload || {}).detail || {}).kind === "budget")) {
      s.budgetQueued = null;
      toast("预算调整被拒绝");
    }
  }

  /* 统一线程：只补丁发生变化的条目。展开状态、details open、焦点和
     用户向上查看历史的位置都由 patchThread 保留。 */
  paintThread(s, trace, { live: true });
  paintQueuedMsgs();

  if (trace.terminal && trace.terminal.status) {
    if (!s.resultLoaded) {
      s.resultLoaded = true;
      loadReviewProfile(trace);
      loadProjectDelivery(trace);
    }
  }

  if (events !== s.eventCount) {
    s.eventCount = events;
    const railBody = document.getElementById("rail-body");
    const railStick = railBody.scrollHeight - railBody.scrollTop - railBody.clientHeight < 60;
    renderRail(trace, store.check);
    if (railStick) railBody.scrollTop = railBody.scrollHeight;
  }

  if (trace.terminal && trace.terminal.status) {
    const runId = s.id;
    verifyChain(trace).then(check => {
      if (!sessions.has(runId) || store.key !== "run:" + runId) return;
      store.check = check;
      setChainBadge(check);
      renderRail(trace, check);
    });
  } else {
    const badge = document.getElementById("chain-badge");
    badge.className = "chain-pill";
    badge.innerHTML = `<span class="dot" style="background:var(--accent)"></span><span>签名中 · ${events} events</span>`;
  }

  const chip = document.getElementById("run-chip-text");
  if (chip) {
    const rounds = (trace.rounds || []).length;
    chip.textContent = pending
      ? `等待授权${rounds ? " · 第 " + rounds + " 轮" : ""}`
      : (rounds ? `运行中 · 第 ${rounds} 轮` : "运行中");
  }

  if (stick) scrollMainBottom();
  paintConnection(s);
  updateStatusline();
}

function catalogModelLabel(key) {
  if (!key) return "";
  const entry = modelCatalog.find(model => model.key === key);
  if (!entry) return key;
  if (entry.key === "mock") return "mock";
  return String(entry.model || entry.label || key);
}

/* 运行事实优先：trace.model → 本次运行已确认的选择。绝不在这里读取
   用户“下一次运行”的新选择，否则历史运行会被改写成另一个模型。 */
function sessionModelLabel(s) {
  if (!s) return "";
  return resolveModelLabel({
    trace: s.trace,
    runModel: catalogModelLabel(s.confirmedModel),
  });
}

function composerFooterText(running) {
  if (!health || !health.live) return "离线 · 只读回放";
  const s = activeSession();
  if (running) {
    const label = sessionModelLabel(s);
    return `${label ? label + " · " : ""}运行中`;
  }
  if (s) {
    const label = sessionModelLabel(s);
    const status = (s.trace && s.trace.terminal && s.trace.terminal.status)
      || (s.failed ? "failed" : "");
    const state = status
      ? `运行${status === "succeeded" ? "完成" : `结束（${status}）`}`
      : (s.finished ? "已结束" : "可运行");
    return `${label ? label + " · " : ""}${state}`;
  }
  const trace = shownTrace();
  if (trace) {
    const label = traceModelLabel(trace) || "模型未知";
    return `${label} · ${store.key && store.key.startsWith("run:") ? "运行记录" : "只读回放"}`;
  }
  const selected = catalogModelLabel(modelChoice.key);
  return `${selected ? selected + " · " : ""}可运行`;
}

/* 上下文容量圆圈：tokens_used / max_tokens，超过 60% 变黄、85% 变红。 */
function paintCtxRing() {
  const ring = document.getElementById("ctx-ring");
  if (!ring) return;
  // 从当前展示的 trace（回放或 live）取最新 budget_after
  const trace = shownTrace() || (activeSession() && activeSession().trace) || null;
  const rounds = trace ? (trace.rounds || []) : [];
  let budget = null;
  for (let i = rounds.length - 1; i >= 0; i--) {
    const b = (rounds[i].budget_after || {});
    if (b.tokens_used !== undefined && b.limits && b.limits.max_tokens) {
      budget = b;
      break;
    }
  }
  if (!budget) {
    ring.style.display = "none";
    return;
  }
  const used = budget.tokens_used || 0;
  const max = budget.limits.max_tokens || 1;
  const pct = Math.min(100, Math.round((used / max) * 100));
  const circumference = 2 * Math.PI * 7;
  const arc = ring.querySelector(".ctx-arc");
  const pctText = ring.querySelector(".ctx-pct");
  if (arc) arc.setAttribute("stroke-dasharray", `${(pct / 100) * circumference} ${circumference}`);
  if (pctText) pctText.textContent = `${pct}%`;
  ring.classList.toggle("warn", pct >= 60 && pct < 85);
  ring.classList.toggle("danger", pct >= 85);
  ring.style.display = "";
}

function setComposerRunning(running) {
  const ta = document.getElementById("composer-ta");
  const btn = document.getElementById("send");
  const chip = document.getElementById("run-chip");
  if (!btn || !ta) return;
  if (running) {
    chip.hidden = false;
    document.getElementById("run-chip-text").textContent = "运行中";
    btn.disabled = false;
    btn.title = "发送消息 · 下一轮交给 agent";
    btn.innerHTML = `<span data-doodle="arrow-up"></span>`;
    hydrateDoodles(btn);
    ta.disabled = false;
    ta.placeholder = "运行中 — 直接输入，消息将排队并在下一轮交给 agent（⌘. 停止）";
  } else {
    chip.hidden = true;
    btn.title = "发送任务";
    btn.innerHTML = `<span data-doodle="arrow-up"></span>`;
    hydrateDoodles(btn);
    const enabled = !!(health && health.live);
    btn.disabled = !enabled;
    ta.disabled = !enabled;
    // 恢复任务模式的占位符（消息模式会覆盖它）
    const template = templates.find(t => t.key === tplKey);
    ta.placeholder = template ? template.objective : "给 agent 派一个任务…";
  }
  const s = activeSession();
  const status = s && s.trace && s.trace.terminal && s.trace.terminal.status;
  const tone = running ? "live"
    : status === "succeeded" ? "ok"
      : (status === "failed" || status === "denied") ? "bad"
        : status ? "warn"
          : (health && health.live ? "ok" : "");
  setFoot(composerFooterText(running), tone);
}

/* ------------------------ MIXED — 审批状态逻辑（不改）+ 审批卡接线
   审批卡纯渲染（动词映射 / diff / 影响区）已迁入 app/approval-card.js
   （拆分切片 3a）；这里只剩会话侧的接线。 */

/* 审批决定/问题回答/计划与预算指令在 app/run-actions.js（拆分切片 3c）；
   这里经工厂接线，注入会话读取、线程补丁与 toast。 */
const {
  paintApproval, answerQuestion, decideApproval,
  paintPlan, paintBudget, queuePlan, queueBudget,
} = createRunActions({
  activeSession, paintThread, toast,
  fetchImpl: (...args) => fetch(...args),
});

/* 会话生命周期与运行流连接在 app/live-sessions.js（拆分切片 3d）；
   liveApply 状态机仍在本文件（SSE 差异渲染不可改），经 deps 注入。 */
const {
  createSession, tabTitle, openLive,
  hideConnectionStatus, paintConnection, setConnection,
  applyDelta, attachStream, stashSessionUpdate, repaintSession,
  finishSession, detachLive, closeSessionTab,
} = createLiveSessions({
  sessions, store, cache, viewGate,
  activeSession,
  // 以下在接线点之后定义(const 解构),用箭头延迟读取
  leaveChat: (...args) => leaveChat(...args),
  renderNav: (...args) => renderNav(...args),
  renderTabs, renderMain, render, refreshRuns,
  liveApply, liveShell,
  paintThread, paintApproval, paintQueuedMsgs,
  setComposerRunning, toast,
  notify: (...a) => notify(...a),
  updateStatusline, shownTrace, openReport,
  loadReviewProfile, loadProjectDelivery,
  verifyChain, setChainBadge, renderRail,
  shortObjective, tplTitle,
  setLedgerOpen: value => { ledgerOpen = value; },
});

function renderTabs() {
  const bar = document.getElementById("tabs");
  if (!bar) return;
  const items = [...sessions.values()];
  bar.hidden = items.length === 0;
  bar.innerHTML = items.map(s => {
    const active = activeSession() === s && store.key === "run:" + s.id;
    const status = s.failed ? "bad"
      : s.finished ? (((s.trace || {}).terminal || {}).status === "succeeded" ? "ok" : "bad")
      : "live";
    return `<div class="tab${active ? " active" : ""}" data-tab="${esc(s.id)}" title="${esc(s.objective || "")}">
      <span class="tdot ${status}"></span>
      <span class="tlabel">${esc(tabTitle(s))}</span>
      <button class="tx" data-tabclose="${esc(s.id)}" title="关闭标签">×</button>
    </div>`;
  }).join("");
}

/* ------------------------------- 委托入口（app/intake.js，拆分切片 4a）
   intake 会话状态、clarifier 往返、草稿与偏好都在模块里；app 侧 let
   （health/tplKey/approvalMode/modelChoice/modelBudget/settingsOpen）
   经 getter 注入，attach 与会话动作按引用/函数注入。 */
const {
  intake, renderIntake, startIntake, closeIntake,
  intakeChoose, intakeFreeText, intakeBack, startIntakeRun,
} = createIntake({
  APPROVAL_MODES, store, attach,
  approvalTitle,
  isGeneralTemplate: (...a) => isGeneralTemplate(...a),
  renderAttachPanel: (...a) => renderAttachPanel(...a),
  delegationSpecExtras: (...a) => delegationSpecExtras(...a),
  autosize, hydrateDoodles, renderMode, renderMain, stageEmptyHtml,
  select, activeSession, openLive, refreshRuns, setComposerRunning, toast,
  getHealth: () => health,
  getTemplates: () => templates,
  getTplKey: () => tplKey,
  getApprovalMode: () => approvalMode,
  getModelChoice: () => modelChoice,
  getModelCatalog: () => modelCatalog,
  getModelBudget: () => modelBudget,
  setModelBudget: value => { modelBudget = value; },
  getFastMode: () => fastMode,
  getSettingsOpen: () => settingsOpen,
  closeSettingsPage: (...a) => closeSettingsPage(...a),
  leaveChat: (...args) => leaveChat(...args),
  fetchImpl: (...args) => fetch(...args),
});

/* 输入框统一入口：S1 分态后 dispatch 只查模式表（core/composer-mode.js），
   intake/chat/running 三类判据不再散布在提交路径里。 */
const COMPOSER_SUBMIT = {
  intake: () => {
    const ta = document.getElementById("composer-ta");
    const text = (ta.value || "").trim();
    if (!text) { toast("先选一个选项，或在下方描述你的需求"); return; }
    ta.value = "";
    autosize(ta);
    return intakeFreeText(text);
  },
  /* G1 治理台：零模型命令流——只接受斜杠命令，其余输入提示用法。 */
  gov: () => {
    const ta = document.getElementById("composer-ta");
    const text = (ta.value || "").trim();
    if (!text) return;
    if (!text.startsWith("/")) { toast("治理台用命令交互：/挖洞 <target>（如 /挖洞 /Applications/EvoX.app）"); return; }
    runSlash(text.slice(1).split(/\s+/)[0]);
  },
  chat: () => sendChatFromComposer(),
  running: () => sendOperatorMessage(),
  work: () => sendTask(),
};

function composerSubmit() {
  const s = activeSession();
  const mode = composerMode({
    intakeActive: intake.active,
    govActive: ["gov", "audit"].includes(document.getElementById("main")?.dataset.view),
    chatActive: chat.active,
    sessionRunning: !!(s && s.running && !s.finished),
  });
  return COMPOSER_SUBMIT[mode]();
}

/* 运行中插话：入队渲染在排队面板，入链后由 snapshot 事件确认并移出队列 */
async function sendOperatorMessage() {
  const s = activeSession();
  if (!s || !s.running || s.finished) return;
  const ta = document.getElementById("composer-ta");
  const text = (ta.value || "").trim();
  if (!text) return;
  ta.value = "";
  autosize(ta);
  const msg = { id: queueMsgId(), text, state: "queued" };
  s.queuedMsgs.push(msg);
  paintQueuedMsgs();
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(s.id)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    const data = await response.json().catch(() => ({}));
    if (data.messageId) msg.serverId = data.messageId;
    paintQueuedMsgs();
  } catch (error) {
    msg.state = "failed";
    msg.error = error.message;
    paintQueuedMsgs();
    toast("消息未送达：" + error.message);
  }
}

let queueMsgSeq = 0;
function queueMsgId() {
  queueMsgSeq += 1;
  return `q${Date.now().toString(36)}-${queueMsgSeq}`;
}

/* 排队面板（对照官方 QueuedMessagesPanel）：两行截断摘要 + hover 操作
   （steer 闪电 / 编辑 / 上移 / 删除）；草稿条目点击回填输入框。 */
function paintQueuedMsgs() {
  const slot = document.getElementById("queued-slot");
  if (!slot) return;
  const s = activeSession();
  const msgs = s && s.queuedMsgs ? s.queuedMsgs : [];
  if (!msgs.length) { slot.innerHTML = ""; return; }
  const items = msgs.map((m, index) => {
    if (s.queueEditingId && s.queueEditingId === m.id) {
      return `<div class="q-item editing" data-mid="${esc(m.id)}">
        <input class="q-edit" data-q-edit-input value="${esc(m.text)}" spellcheck="false">
        <div class="q-acts">
          <button class="q-act" data-q-act="save" title="保存 (↵)">✓</button>
          <button class="q-act" data-q-act="cancel" title="取消 (esc)">×</button>
        </div>
      </div>`;
    }
    const first = index === 0;
    const acts = [];
    if (m.state === "queued" && s.running && !s.finished) {
      acts.push(`<button class="q-act bolt${m.steered ? " done" : ""}" data-q-act="steer" title="${m.steered ? "已插话 · 下一轮优先处理" : "立即插入（下一轮优先处理）"}">⚡</button>`);
    }
    if (m.state === "draft") {
      acts.push(`<button class="q-act" data-q-act="refill" title="回填输入框">↩</button>`);
    } else {
      acts.push(`<button class="q-act" data-q-act="edit" title="编辑">✎</button>`);
      if (!first && m.state === "queued") acts.push(`<button class="q-act" data-q-act="moveup" title="上移">↑</button>`);
      acts.push(`<button class="q-act danger" data-q-act="remove" title="删除">🗑</button>`);
    }
    const state = m.state === "failed" ? `未送达 · ${esc(m.error || "")}`
      : m.state === "draft" ? "草稿 · 未入链"
      : m.state === "delivered" ? "已入链"
      : m.steered ? "⚡ 插话 · 下一轮优先入链"
      : "排队中 · 下一轮入链";
    return `<div class="q-item ${esc(m.state)}${m.steered ? " steered" : ""}" data-mid="${esc(m.id)}">
      <div class="q-main">
        <div class="q-text">${esc(m.text)}</div>
        <div class="q-state">${state}</div>
      </div>
      <div class="q-acts">${acts.join("")}</div>
    </div>`;
  }).join("");
  slot.innerHTML = `<div class="queued-msgs">
    <div class="q-head"><span>排队插话 · ${msgs.length} 条</span><span class="grow"></span><span class="q-hint">下一轮边界入链 · 顺序即生效顺序</span></div>
    ${items}
  </div>`;
  const editing = slot.querySelector(".q-edit");
  if (editing) { editing.focus(); editing.setSelectionRange(editing.value.length, editing.value.length); }
}

/* 队列管理指令：全部走 server 端点，server 侧队列才是生效顺序的事实源 */
async function queueCommand(op, msg, newText) {
  const s = activeSession();
  if (!s || !msg) return false;
  if (op === "steer" && !msg.serverId) { toast("这条消息还在发送中，稍后再插话"); return false; }
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(s.id)}/${op === "steer" ? "steer" : "queue"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(op === "steer"
        ? { messageId: msg.serverId }
        : { op, messageId: msg.serverId, text: newText }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    return true;
  } catch (error) {
    toast("队列操作未送达：" + error.message);
    return false;
  }
}

/* 队列权威同步：server 队列是事实源。已确认条目（有 serverId）以 server 为准
   （文本 / steer 标记 / 存在性——被其他客户端删除或已消费的不再留在面板）；
   server 有而本地没有的是别的客户端（或 API）入队的项，收编进本地面板；
   本地尚无 serverId 的条目是 POST 在途，先按文本认领再乐观保留。 */
function reconcileQueue(s, serverQueue) {
  if (!s) return;
  if (!Array.isArray(s.queuedMsgs)) s.queuedMsgs = [];
  const serverIds = new Set(serverQueue.map(item => item && item.id).filter(Boolean));
  for (const item of serverQueue) {
    if (!item || !item.id) continue;
    let local = s.queuedMsgs.find(m => m.serverId === item.id);
    if (!local) {
      // POST 在途（还没有 serverId）的同文本条目直接认领，避免重复
      local = s.queuedMsgs.find(m => !m.serverId && m.state === "queued" && m.text === item.text);
    }
    if (local) {
      local.serverId = item.id;
      local.text = String(item.text || "");
      local.steered = !!item.steered;
      if (local.state !== "queued") local.state = "queued";
      local.error = "";
    } else {
      s.queuedMsgs.push({
        id: queueMsgId(),
        serverId: item.id,
        text: String(item.text || ""),
        steered: !!item.steered,
        state: "queued",
      });
    }
  }
  s.queuedMsgs = s.queuedMsgs.filter(m => !m.serverId || serverIds.has(m.serverId));
  paintQueuedMsgs();
}

function findQueuedMsg(el) {
  const item = el.closest("[data-mid]");
  const s = activeSession();
  if (!item || !s) return null;
  return (s.queuedMsgs || []).find(m => m.id === item.dataset.mid) || null;
}

function handleQueueAction(button) {
  const act = button.dataset.qAct;
  const msg = findQueuedMsg(button);
  const s = activeSession();
  if (!msg || !s) return;
  if (act === "edit") { s.queueEditingId = msg.id; paintQueuedMsgs(); return; }
  if (act === "cancel") { s.queueEditingId = null; paintQueuedMsgs(); return; }
  if (act === "save") { saveQueueEdit(s, msg); return; }
  if (act === "refill") {
    const ta = document.getElementById("composer-ta");
    ta.value = msg.text;
    autosize(ta);
    ta.focus();
    s.queuedMsgs = s.queuedMsgs.filter(m => m.id !== msg.id);
    paintQueuedMsgs();
    return;
  }
  if (act === "remove") {
    (async () => {
      if (msg.serverId && !(await queueCommand("remove", msg))) return;
      s.queuedMsgs = s.queuedMsgs.filter(m => m.id !== msg.id);
      paintQueuedMsgs();
    })();
    return;
  }
  if (act === "moveup") {
    (async () => {
      if (msg.serverId && !(await queueCommand("moveUp", msg))) return;
      const list = s.queuedMsgs;
      const i = list.indexOf(msg);
      if (i > 0) { list.splice(i, 1); list.splice(i - 1, 0, msg); }
      paintQueuedMsgs();
    })();
    return;
  }
  if (act === "steer") {
    (async () => {
      if (!(await queueCommand("steer", msg))) return;
      const list = s.queuedMsgs;
      const i = list.indexOf(msg);
      if (i > 0) { list.splice(i, 1); list.unshift(msg); }
      msg.steered = true;
      paintQueuedMsgs();
      toast("已插话 · 下一轮优先处理");
    })();
  }
}

function saveQueueEdit(s, msg) {
  const item = document.querySelector(`[data-mid="${CSS.escape(msg.id)}"]`);
  const input = item ? item.querySelector(".q-edit") : null;
  const text = (input ? input.value : "").trim();
  if (!text) { toast("内容不能为空"); return; }
  (async () => {
    if (msg.serverId && !(await queueCommand("edit", msg, text))) return;
    msg.text = text;
    s.queueEditingId = null;
    paintQueuedMsgs();
  })();
}

async function sendTask() {
  const ta = document.getElementById("composer-ta");
  const text = (ta.value || "").trim();
  if (!text) { toast("先描述要完成的任务"); return; }
  if (!health || !health.live) {
    toast("回放模式：用 python3 viewer/server.py 启动后才能派任务");
    return;
  }
  ta.value = "";
  autosize(ta);
  startIntake(text);
}

async function stopRun() {
  const s = activeSession();
  if (!s || !s.running) return;
  document.getElementById("run-chip-text").textContent = "正在停止…";
  try {
    await fetch(`/api/runs/${encodeURIComponent(s.id)}/stop`, { method: "POST" });
  } catch { /* the stream will settle it */ }
}


let runSignature = "";

/* 串行轮询 + 世代号：旧响应后到不会把侧栏回滚到旧列表。 */
const runsPoller = createRunsPoller({
  canFetch: () => !!(health && health.live),
  fetchRuns: () => fetch("/api/runs", { cache: "no-store" }).then(r => r.json()),
  apply: next => {
    // 只在有变化时重绘：轮询 2.5s 一次，但不能打断重命名/悬停等本地状态
    const signature = next.map(run => [
      run.id, run.status, run.rounds, run.name, run.pinned ? 1 : 0,
      (run.cost && run.cost.costMicrousd) || 0,
    ].join(":")).join("|");
    if (signature === runSignature) return;
    runSignature = signature;
    runHistory = next;
    renderNav();
  },
});

function refreshRuns(options) {
  return runsPoller(options);
}

function setTemplate(key) {
  tplKey = key;
  const template = templates.find(t => t.key === key);
  const ta = document.getElementById("composer-ta");
  if (template && ta && !ta.value) ta.placeholder = template.objective;
  if (attach.open) renderAttachPanel();
  refreshAttachData();
}

function approvalTitle(mode = approvalMode) {
  const found = APPROVAL_MODES.find(([key]) => key === mode);
  return found ? found[1] : "关键审批";
}

function setApprovalMode(mode) {
  if (!APPROVAL_MODES.some(([key]) => key === mode)) return;
  approvalMode = mode;
  localStorage.setItem("fa.approval", mode);
  const label = document.getElementById("approval-label");
  if (label) label.textContent = approvalTitle();
  renderApprovalMenu();
  syncSettings();
}

function renderApprovalMenu() {
  const menu = document.getElementById("approval-menu");
  if (!menu) return;
  menu.innerHTML = APPROVAL_MODES.map(([key, title, desc]) => `<button class="tpl-item${key === approvalMode ? " active" : ""}" data-approval-mode="${esc(key)}">
    <span class="t"><span>${esc(title)}</span><span class="m">${esc(key)}</span></span>
    <span class="d">${esc(desc)}</span>
  </button>`).join("") + `<div class="tpl-note">审批只发生在轮次边界，不打断工具执行；拒绝会终止运行并写入签名链。档位对新任务生效。</div>`;
}

/* 模型选择器与“+”附加面板（app/composer-panel.js，拆分切片 4c）。
   状态本体（attach/chat/chatModelChoice/modelChoice）按引用注入，
   let 型目录经 getter 注入。 */
const {
  saveModelChoice, adoptDefaultWorkModel, liveRunEffortLabel, paintModelBadge,
  isGeneralTemplate, effectiveDraftBudget, renderAttachPanel, refreshAttachData,
  readAttachBudgetInputs, delegationSpecExtras, placeModelMenu, renderModelMenu,
} = createComposerPanel({
  attach, chat, chatModelChoice, saveChatModelChoice,
  EFFORT_LEVELS, EFFORT_LABEL,
  templateGroups, projectWork, sessionModelLabel, activeSession,
  renderIntake,
  getIntake: () => intake,
  getHealth: () => health,
  getTemplates: () => templates,
  getTplKey: () => tplKey,
  getModelCatalog: () => modelCatalog,
  getModelChoice: () => modelChoice,
  getModelBudget: () => modelBudget,
  setModelBudget: value => { modelBudget = value; },
  getFastMode: () => fastMode,
  setFastMode,
  getChatModels: () => chatModels,
  fetchImpl: (...args) => fetch(...args),
});

async function initLive() {
  setApprovalMode(approvalMode);
  try {
    health = await fetch("/api/health", { cache: "no-store" }).then(r => r.json());
  } catch {
    health = { ok: false, live: false, runtimeError: "静态托管模式：没有本地 API" };
  }
  if (health.live) {
    try {
      const data = await fetch("/api/modules", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
      if (data && Array.isArray(data.modules)) modules = data.modules;
    } catch {
      modules = [];
    }
    try {
      templates = await fetch("/api/templates", { cache: "no-store" }).then(r => r.json());
    } catch {
      templates = [];
    }
    if (!templates.length) health.live = false;
    renderStageChips();
    try {
      const data = await fetch("/api/models", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
      modelCatalog = data && Array.isArray(data.models) ? data.models : [];
    } catch {
      modelCatalog = [];
    }
  }
  if (!health.live) {
    const ta = document.getElementById("composer-ta");
    ta.placeholder = "静态回放模式 — 用 python3 server.py --repo … 启动控制台即可真实运行任务";
    document.getElementById("approval-btn").disabled = true;
    const modelBtn = document.getElementById("model-btn");
    if (modelBtn) modelBtn.disabled = true;
    document.getElementById("send").disabled = true;
    setFoot("离线 · 只读回放", "");
    paintModelBadge();
    return;
  }
  // 通用委托是产品默认入口；金融研究作为参考负载仍可显式选择。
  const preferred = ["general-research", "full"].find(key => templates.some(t => t.key === key));
  setTemplate(preferred || templates[0].key);
  setComposerRunning(false);
  paintModelBadge();
  updateStatusline();
  fetch("/api/tools", { cache: "no-store" }).then(r => r.ok ? r.json() : null).then(data => {
    if (!data || !Array.isArray(data.tools)) return;
    toolContracts = data.tools;
    const trace = shownTrace();
    if (trace) renderRail(trace, store.check || { ok: false, reason: "" });
  }).catch(() => { /* the tools tab will show 加载中 */ });
  autosize(document.getElementById("composer-ta"));
  refreshRuns();
  setInterval(refreshRuns, 2500);
}

async function selectRun(id) {
  const ticket = viewGate.begin();
  leaveChat();
  if (intake.active) closeIntake(false);
  if (settingsOpen) closeSettingsPage(false);
  ledgerOpen = false;
  evExpanded.clear();
  if (!ticket.isCurrent()) return;
  const existing = sessions.get(id);
  if (existing) return openLive(id, existing.objective, existing.template);
  const run = runHistory.find(item => item.id === id);
  if (run && (run.status === "running" || !run.tracePath)) {
    return openLive(id, run.objective, run.template);
  }
  if (activeSession()) detachLive();
  store.key = "run:" + id;
  renderNav();
  const cached = cache.get("run:" + id);
  if (cached) {
    await renderMode("run", {
      trace: cached,
      title: "运行 · " + tplTitle(run ? run.template : ""),
      internalView: true,
    });
    return;
  }
  renderMain(`<div class="empty">加载运行记录…</div>`, { mode: "loading" });
  const outcome = await settleLatest(ticket, () => fetch(runTraceUrl(id), { cache: "no-store" }).then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }));
  if (outcome.stale) return;
  if (outcome.error) {
    renderMain(`<div class="empty">无法加载运行记录：${esc(outcome.error.message)}</div>`, { mode: "error" });
    return;
  }
  const trace = outcome.value;
  cache.set("run:" + id, trace);
  if (!ticket.isCurrent()) return;
  await renderMode("run", {
    trace,
    title: "运行 · " + tplTitle(run ? run.template : ""),
    internalView: true,
  });
  if (!ticket.isCurrent()) return;
  renderNav();
}

/* ------------------------------------- LOGIC LAYER — trace 加载与视图状态 */

const TRACE_CANDIDATES = key => [
  `traces/${key}.trace.json`,
  `../work/demo/${key}.trace.json`,
  `work/demo/${key}.trace.json`,
];

async function loadTrace(key) {
  if (cache.has(key)) return cache.get(key);
  for (const url of TRACE_CANDIDATES(key)) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) continue;
      const trace = await response.json();
      cache.set(key, trace);
      return trace;
    } catch { /* try next */ }
  }
  return null;
}

async function select(key, label) {
  const ticket = viewGate.begin();
  leaveChat();
  if (intake.active) closeIntake(false);
  if (settingsOpen) closeSettingsPage(false);
  if (key.startsWith("run:")) return selectRun(key.slice(4));
  ledgerOpen = false;
  evExpanded.clear();
  if (activeSession()) detachLive();
  store.key = key;
  renderNav();
  renderMain(`<div class="empty">加载 ${esc(label)}…</div>`, { mode: "loading" });
  const trace = await loadTrace(key);
  if (!ticket.isCurrent()) return;
  if (!trace) {
    document.getElementById("chain-badge").className = "chain-pill";
    document.getElementById("chain-badge").innerHTML = `<span class="dot"></span><span>无数据</span>`;
    renderMain(`<div class="empty">
      ${esc(key)} 还没有 trace。先运行 <code>python demo.py ${esc(key)}</code>，或 <code>python demo.py all</code> 一次生成全部。
    </div>`, { mode: "error" });
    return;
  }
  await renderMode("run", { trace, title: label, internalView: true });
  if (!ticket.isCurrent()) return;
  renderNav();
}

/* ----------------- LOGIC LAYER — 哈希验证 / 复制 / 导出 / 指令绑定入口 */

async function reverify() {
  const trace = shownTrace();
  if (!trace) return;
  const check = await verifyChain(trace);
  const badge = document.getElementById("chain-badge");
  badge.className = "chain-pill " + (check.ok ? "ok" : "bad") + " pulse";
  badge.innerHTML = check.ok
    ? `<span class="dot"></span><span>链验证通过 · ${check.events} events</span>`
    : `<span class="dot"></span><span>链断裂 · ${esc(check.reason)}</span>`;
  setTimeout(() => badge.classList.remove("pulse"), 700);
}

function exportTrace() {
  const trace = shownTrace();
  if (!trace) return;
  const name = (store.key ? store.key.replace(/:/g, "-") : "trace") + ".trace.json";
  const blob = new Blob([JSON.stringify(trace, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
  toast("已导出 trace JSON");
}

/* -------------------------------------- LOGIC LAYER — 运行创建 / 恢复 / 重试 */

function currentRun() {
  if (store.key && store.key.startsWith("run:")) {
    return runHistory.find(item => item.id === store.key.slice(4)) || null;
  }
  return null;
}

async function togglePin(id) {
  const run = runHistory.find(item => item.id === id);
  if (!run) return;
  run.pinned = !run.pinned;
  // 置顶会同时改变两处：主列表少一行、置顶段多一行 —— 一次重绘两处
  renderNav();
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(id)}/meta`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: run.pinned }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    toast(run.pinned ? "已固定" : "已取消固定");
  } catch {
    run.pinned = !run.pinned;
    renderNav();
    toast("固定失败");
  }
}

function startRename(id, labelEl) {
  const run = runHistory.find(item => item.id === id);
  if (!run || !labelEl) return;
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = run.name || shortObjective(run.objective);
  input.spellcheck = false;
  labelEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = async save => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (save && value && value !== (run.name || shortObjective(run.objective))) {
      try {
        const response = await fetch(`/api/runs/${encodeURIComponent(id)}/meta`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: value }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const updated = await response.json();
        run.name = updated.name || "";
        toast("已重命名");
      } catch {
        toast("重命名失败");
      }
    }
    renderRuns();
  };
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); commit(true); }
    else if (event.key === "Escape") { event.preventDefault(); commit(false); }
  });
  input.addEventListener("blur", () => commit(false));
}

function costChipsHtml(trace) {
  const b = lastBudget(trace);
  if (!b) return "";
  const lim = b.limits || {};
  return `<div class="chips">
    <span class="mini">成本 ${num(b.cost_used_microusd)} µUSD</span>
    <span class="mini">calls ${b.calls_used}/${num(lim.max_calls || 0)}</span>
    <span class="mini">reads ${b.reads_used}/${num(lim.max_reads || 0)}</span>
    <span class="mini">tokens ${num(b.tokens_used)}</span>
  </div>${tokenSplitHtml(trace)}`;
}

function runActionsHtml(trace) {
  const terminal = trace.terminal || {};
  const status = terminal.status || "";
  if (!status) return "";
  const report = `<button class="a-btn strong" data-act="report">签发报告</button>`;
  if (!(store.key && store.key.startsWith("run:"))) {
    return `<div class="chips">${costChipsHtml(trace)}</div><div class="actions">${report}</div>`;
  }
  const failure = terminal.failure || {};
  const run = currentRun();
  const session = activeSession();
  const liveId = session && session.id;
  const canResume = !run || !!run.taskId || (liveId && liveId === run.id);
  const resumable = (failure.code === "BUDGET_EXCEEDED" || failure.code === "ROUND_LIMIT")
    && !(session && session.running)
    && canResume;
  const resume = resumable ? `<button class="a-btn strong" data-act="resume">追加预算并继续</button>` : "";
  const retry = status && status !== "succeeded"
    ? `<button class="a-btn" data-act="retry">重试</button>` : "";
  return `<div class="chips">${costChipsHtml(trace)}</div><div class="actions">${resume}${retry}${report}<button class="a-btn" data-act="diag">复制诊断</button></div>`;
}

/* ---------------- MIXED — 报告构建/链验证逻辑（不改）+ 浮层 DOM 操作 */

let lastReportHtml = null;
let lastReportName = "fcode-report.html";

async function openReport() {
  const initial = shownTrace();
  if (!initial || !(initial.terminal && initial.terminal.status)) {
    toast("运行结束后才能签发报告");
    return;
  }
  const run = currentRun();
  const currentRunId = (store.key || "").startsWith("run:") ? store.key.slice(4) : (run && run.id);
  const pendingReview = currentRunId ? waitForReviewTraceRefresh(currentRunId) : null;
  if (pendingReview) await pendingReview;
  const trace = shownTrace();
  const check = await verifyChain(trace);
  const latest = shownTrace();
  const task = latest.task || {};
  const title = run ? (run.name || shortObjective(run.objective)) : shortObjective(task.objective || "工作底稿");
  lastReportHtml = FCReport.build(latest, {
    title,
    runId: run ? run.id : (store.key || "trace"),
    check,
    generatedAt: new Date().toISOString().replace("T", " ").slice(0, 19) + "Z",
  });
  lastReportName = "fcode-report-" + String(run ? run.id : (store.key || "trace")).replace(/[^a-zA-Z0-9-]+/g, "-") + ".html";
  document.getElementById("report-title").textContent = title;
  document.getElementById("report-frame").srcdoc = lastReportHtml;
  document.getElementById("report-layer").hidden = false;
}

function closeReport() {
  const layer = document.getElementById("report-layer");
  if (layer) layer.hidden = true;
  const frame = document.getElementById("report-frame");
  if (frame) frame.srcdoc = "";
  lastReportHtml = null;
}

function downloadReport() {
  if (!lastReportHtml) return;
  const bridge = desktopBridge();
  if (bridge && bridge.save_report) {
    bridge.save_report(lastReportName, lastReportHtml).then(result => {
      if (result && result.saved) toast("报告已保存 · " + result.path);
      else if (result) toast("已取消保存");
    }).catch(() => toast("保存失败"));
    return;
  }
  const blob = new Blob([lastReportHtml], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = lastReportName; a.click();
  URL.revokeObjectURL(url);
  toast("报告已下载 · 可离线打开与转发");
}

function printReport() {
  const frame = document.getElementById("report-frame");
  try {
    frame.contentWindow.focus();
    frame.contentWindow.print();
  } catch {
    toast("此环境不支持内嵌打印，请下载后打开打印");
  }
}

function openResumeForm() {
  const trace = shownTrace() || {};
  const budget = lastBudget(trace);
  const lim = (budget && budget.limits) || {};
  const host = document.querySelector('[data-thread-item="error"] .th-answer-actions')
    || document.querySelector('[data-thread-item="answer"] .th-answer-actions');
  if (!host) return;
  if (host.querySelector(".resume-form")) return;
  const calls = (lim.max_calls || 0) + 6;
  const reads = (lim.max_reads || 0) + 10;
  const cost = (lim.max_cost_microusd || 0) + 20000;
  const form = document.createElement("div");
  form.className = "resume-form";
  form.innerHTML = `
    <div class="rf-title">追加预算并继续</div>
    <div class="rf-grid">
      <label>calls <input type="number" data-resume="maxCalls" min="${lim.max_calls || 0}" value="${calls}" inputmode="numeric"></label>
      <label>reads <input type="number" data-resume="maxReads" min="${lim.max_reads || 0}" value="${reads}" inputmode="numeric"></label>
      <label>µUSD <input type="number" data-resume="maxCostMicrousd" min="${lim.max_cost_microusd || 0}" value="${cost}" inputmode="numeric"></label>
    </div>
    <div class="rf-foot">
      <span class="hint">从事件日志续跑，已读资料不重发 · 轮次上限 +6</span>
      <span class="grow"></span>
      <button class="a-btn" data-resume-cancel>取消</button>
      <button class="a-btn strong" data-resume-apply>继续运行</button>
    </div>
    <div class="rf-error" hidden></div>`;
  host.appendChild(form);
  const first = form.querySelector("input");
  if (first) first.focus();
}

async function resumeRun() {
  const form = document.querySelector(".resume-form");
  if (!form) return;
  const s = activeSession();
  const id = store.key && store.key.startsWith("run:") ? store.key.slice(4) : null;
  if (!id) { toast("没有可续跑的运行"); return; }
  const saved = runHistory.find(item => item.id === id);
  if (saved && !saved.taskId && !(s && s.id === id)) { toast("这条运行没有可续跑的持久任务"); return; }
  const read = key => Number((form.querySelector(`[data-resume="${key}"]`) || {}).value || 0);
  const payload = {
    maxCalls: read("maxCalls"),
    maxReads: read("maxReads"),
    maxCostMicrousd: read("maxCostMicrousd"),
    extraRounds: 6,
  };
  if (payload.maxCalls < 1 || payload.maxReads < 1 || payload.maxCostMicrousd < 1) {
    toast("预算上限必须是正整数");
    return;
  }
  const button = form.querySelector("[data-resume-apply]");
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    const current = activeSession();
    const run = runHistory.find(item => item.id === id) || (current && current.id === id ? current : null);
    const trace = shownTrace();
    const objective = (run && run.objective) || (trace && trace.task && trace.task.objective) || "";
    const template = (run && run.template) || ((trace && trace.task && trace.task.binding || {}).scenario) || "full";
    toast("预算已追加 · 从事件日志续跑");
    // 续跑让同一个 run 恢复运行：丢弃旧会话缓存，重新以运行态打开
    const stale = sessions.get(id);
    const priorModel = stale
      ? (stale.confirmedModel || (stale.trace && stale.trace.model && stale.trace.model.provider) || "")
      : "";
    if (stale) { if (stale.es) stale.es.close(); sessions.delete(id); }
    if (store.sessionId === id) store.sessionId = null;
    cache.delete("run:" + id);
    openLive(id, objective, template, priorModel);
    refreshRuns({ force: true });
  } catch (error) {
    if (button) button.disabled = false;
    const line = form.querySelector(".rf-error");
    if (line) {
      line.hidden = false;
      line.textContent = error.message;
    }
  }
}

async function retryRun(objective, template) {
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ objective, template, approval: approvalMode }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const run = await response.json();
    toast("已重新提交");
    openLive(run.id, objective, run.template, modelChoice.key);
    refreshRuns({ force: true });
  } catch (error) {
    toast("重试失败：" + error.message);
  }
}

function retryCurrent() {
  const run = currentRun();
  const trace = shownTrace() || {};
  const task = trace.task || {};
  const s = activeSession();
  const objective = (run && run.objective) || task.objective || (s && s.objective);
  const template = (run && run.template) || (task.binding || {}).scenario || (s && s.template);
  if (objective && template) retryRun(objective, template);
  else toast("没有可重试的运行");
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch { /* fall through to execCommand */ }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

async function copyDiagnostics() {
  const run = currentRun();
  const trace = shownTrace() || {};
  const terminal = trace.terminal || {};
  const failure = terminal.failure || {};
  const task = trace.task || {};
  const s = activeSession();
  const active = s && s.running ? s : null;
  const check = store.check;
  const lines = [
    "FCode · 运行诊断",
    `run: ${(run && run.id) || (active && active.id) || store.key || "（未选择运行）"}`,
    `template: ${(run && run.template) || (task.binding || {}).scenario || (active && active.template) || "—"}`,
    `objective: ${task.objective || (run && run.objective) || (active && active.objective) || "—"}`,
    `status: ${terminal.status || (run && run.status) || (active ? "running" : "—")}` +
      (failure.code ? ` · ${failure.code}` : "") + (failure.message ? ` · ${failure.message}` : ""),
    `rounds/events: ${(trace.rounds || []).length}/${(trace.events || []).length}` +
      (check ? ` · chain: ${check.ok ? "ok" : "broken(" + check.reason + ")"}` : ""),
    run && run.error ? `error: ${run.error}` : "",
    `at: ${new Date().toISOString()}`,
  ].filter(Boolean);
  const ok = await copyText(lines.join("\n"));
  toast(ok ? "诊断信息已复制" : "复制失败");
}

/* ------------------------------------------------ LOGIC LAYER — 通知授权 */

const NOTIFY_KEY = "fa.notify";
let notifyOn = localStorage.getItem(NOTIFY_KEY) === "1";

function notifyReady() {
  return notifyOn && ("Notification" in window) && Notification.permission === "granted";
}

async function toggleNotify() {
  if (notifyOn) {
    notifyOn = false;
    localStorage.setItem(NOTIFY_KEY, "0");
    syncSettings();
    toast("完成通知已关闭");
    return;
  }
  if (!("Notification" in window)) {
    syncSettings();
    toast("此浏览器不支持系统通知");
    return;
  }
  const permission = Notification.permission === "granted"
    ? "granted"
    : await Notification.requestPermission();
  if (permission !== "granted") {
    syncSettings();
    toast("通知权限未授予");
    return;
  }
  notifyOn = true;
  localStorage.setItem(NOTIFY_KEY, "1");
  syncSettings();
  toast("完成通知已开启");
}

let settingsOpen = false;   // 多个 surface 读写（quickStart/openLedger/chat-ui/键盘），归本文件

/* 设置页（app/settings.js，拆分切片 4b）。偏好变量本体仍在本文件
   （被多个 surface 读写），经 getter/setter 注入;chat-config 的解构
   在本接线点之后,用箭头延迟读取。 */
const {
  applyMotion, syncSettings, openSettingsPage, closeSettingsPage,
  toggleSettingsPage, bindSettings, notify,
} = createSettings({
  APPROVAL_MODES, MOTION_KEY, NOTIFY_KEY, store,
  getMotionOn: () => motionOn,
  setMotionOn: v => { motionOn = v; },
  getNotifyOn: () => notifyOn,
  setNotifyOn: v => { notifyOn = v; },
  getRailFilter: () => railFilter,
  setRailFilter: v => { railFilter = v; },
  getApprovalMode: () => approvalMode,
  setApprovalMode,
  getToolContracts: () => toolContracts,
  setToolContracts: v => { toolContracts = v; },
  getRunHistory: () => runHistory,
  getHealth: () => health,
  getSettingsOpen: () => settingsOpen,
  setSettingsOpen: v => { settingsOpen = v; },
  intake, closeIntake: (...a) => closeIntake(...a),
  leaveChat: (...a) => leaveChat(...a),
  renderMode, toast, shownTrace, renderRail,
  copyDiagnostics, reverify, toggleNotify,
  refreshChatMemory: (...a) => refreshChatMemory(...a),
  deleteChatMemory: (...a) => deleteChatMemory(...a),
  desktopBridge, notifyReady,
  syncChatConfig: (...a) => syncChatConfig(...a),
  saveChatConfig: (...a) => saveChatConfig(...a),
  clearChatConfigKey: (...a) => clearChatConfigKey(...a),
  setDefaultChatModel: (...a) => setDefaultChatModel(...a),
  clearPromptHistory: () => { promptHistory.length = 0; historyIndex = -1; },
  fetchImpl: (...args) => fetch(...args),
});

/* --------------------------------------------------- RENDER LAYER — toast */

let toastTimer = null;
function toast(message) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("on"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove("on");
    setTimeout(() => { if (!el.classList.contains("on")) el.hidden = true; }, 220);
  }, 1700);
}

/* ⌘K 命令面板（app/command-palette.js，拆分切片 4d）。
   动作清单按语境组装与键盘游标都在模块里;slash 菜单与输入历史
   与 composer 键盘状态机共享游标,仍在本文件。 */
const {
  setAllRounds, focusComposer, renderPalette, openPalette, closePalette,
  togglePalette, runPaletteItem, activeIndex, setActiveIndex, paletteNav,
  resetPaletteActive,
} = createCommandPalette({
  activeSession, currentRun, stopRun, retryCurrent, copyDiagnostics, exportTrace,
  openReport, reverify, replay, selectRun, startIntake, toggleNotify,
  shownTrace, store, toast, runLabel,
  getNotifyOn: () => notifyOn,
  getRunHistory: () => runHistory,
});

/* -------------------- MIXED — 命令匹配/执行（不改）+ 菜单渲染 */
/* Codex 式入口：输入框以 / 开头弹出命令面板，↑↓ 选择，Tab 补全，Enter 执行。 */
const SLASH_COMMANDS = [
  { cmd: "/new", desc: "新建任务", run: () => startIntake("") },
  { cmd: "/账本", desc: "对话内打开工作区账本卡", run: () => pushWorkspaceCard("ledger") },
  { cmd: "/状态", desc: "对话内查看进行中的运行", run: () => pushWorkspaceCard("status") },
  { cmd: "/定时", desc: "对话内查看定时任务", run: () => pushWorkspaceCard("scheduled") },
  { cmd: "/挖洞", desc: "治理台:对目标发起 REA 挖洞（/挖洞 <target>）", run: arg => startGovAudit(arg) },
  { cmd: "/audit", desc: "治理台:对目标发起 REA 挖洞（/audit <target>）", run: arg => startGovAudit(arg) },
  { cmd: "/判断", desc: "治理台:操作员判断与审计处置汇总卡", run: () => showGovJudgments() },
  { cmd: "/链", desc: "治理台:验证一次审计的签名链（/链 <audit_id>）", run: arg => showGovChain(arg) },
  { cmd: "/ledger", desc: "打开工作台账本", run: () => pushWorkspaceCard("ledger") },
  { cmd: "/report", desc: "签发当前运行的审计报告", run: () => openReport() },
  { cmd: "/stop", desc: "在轮次边界停止当前运行", run: () => stopRun() },
  { cmd: "/allow", desc: "批准挂起的审批请求", run: () => decideApproval(true) },
  { cmd: "/deny", desc: "拒绝挂起的审批请求", run: () => decideApproval(false) },
  { cmd: "/remember", desc: "把一句话存进长期记忆（如：/remember 口径基准是 CNY）", run: arg => rememberChatMemory(arg) },
  { cmd: "/help", desc: "快捷键与命令说明", run: () => toast("⌘K 面板 · ⌘N 新建对话 · ⌘, 设置 · ⌘. 停止 · ⌘↵ 允许 · ⌘⌫ 拒绝 · ↑↓ 历史") },
  { cmd: "/clear", desc: "清空输入框", run: () => { const ta = document.getElementById("composer-ta"); ta.value = ""; autosize(ta); } },
];
let slashIndex = 0;
let slashItems = [];
const promptHistory = (() => {
  try { return JSON.parse(localStorage.getItem("fa.history") || "[]"); } catch { return []; }
})();
let historyIndex = -1;
let draftBeforeHistory = "";

function slashMenuEl() {
  let el = document.getElementById("slash-menu");
  if (!el) {
    el = document.createElement("div");
    el.id = "slash-menu";
    el.className = "slash-menu";
    el.hidden = true;
    document.getElementById("composer-box").appendChild(el);
    el.addEventListener("mousedown", event => {
      const item = event.target.closest("[data-slash]");
      if (item) { event.preventDefault(); runSlash(item.dataset.slash); }
    });
  }
  return el;
}

function updateSlashMenu() {
  const ta = document.getElementById("composer-ta");
  const menu = slashMenuEl();
  const value = ta.value;
  if (!value.startsWith("/")) { menu.hidden = true; slashItems = []; return; }
  // 只按命令词匹配（输入 /remember 一句话 时菜单保持开启）——
  // 此前用整段输入匹配，参数一打菜单就关，Enter 会把命令当普通消息发出
  const word = value.slice(1).trim().toLowerCase().split(/\s+/)[0] || "";
  // 治理视图只列治理命令(零模型控制台:委托/记忆类命令在这里没有语义)
  const govView = ["gov", "audit"].includes(document.getElementById("main")?.dataset.view);
  slashItems = filterSlashCommands(SLASH_COMMANDS, { gov: govView }).filter(c =>
    c.cmd.slice(1).startsWith(word) || c.desc.toLowerCase().includes(word));
  if (!slashItems.length) { menu.hidden = true; return; }
  slashIndex = Math.min(slashIndex, slashItems.length - 1);
  menu.innerHTML = slashItems.map((c, i) => `<div class="slash-item${i === slashIndex ? " active" : ""}" data-slash="${esc(c.cmd.slice(1))}">
    <span class="cmd">${esc(c.cmd)}</span><span class="desc">${esc(c.desc)}</span></div>`).join("");
  menu.hidden = false;
}

function runSlash(name) {
  const command = SLASH_COMMANDS.find(c => c.cmd === "/" + name);
  const ta = document.getElementById("composer-ta");
  // 带参命令（如 /remember 一句话）：命令词后面的剩余文本作为参数
  const rest = command ? ta.value.slice(command.cmd.length).trim() : "";
  ta.value = "";
  autosize(ta);
  slashMenuEl().hidden = true;
  slashItems = [];
  if (command) command.run(rest);
}

function pushHistory(text) {
  if (!text) return;
  promptHistory.push(text);
  while (promptHistory.length > 50) promptHistory.shift();
  try { localStorage.setItem("fa.history", JSON.stringify(promptHistory)); } catch { /* quota */ }
  historyIndex = -1;
}

/* W1 工作区命令卡：把账本/状态/定时渲染成对话内的本地卡片（role:"wcard"，
   不 POST、不进模型上下文）。没有打开的线程时回落到对应页面——
   卡片是对话功能的入口，不是替代页面路由。 */
async function pushWorkspaceCard(kind) {
  if (!chat.current) {
    renderMode(kind === "scheduled" ? "scheduled" : "ledger");
    return;
  }
  try {
    let cardHtml = "";
    if (kind === "ledger") {
      cardHtml = ledgerCardHtml(runHistory, { tplTitle });
    } else if (kind === "status") {
      cardHtml = statusCardHtml(runHistory);
    } else if (kind === "scheduled") {
      const r = await fetch("/api/schedules", { cache: "no-store" });
      const data = r.ok ? await r.json() : { schedules: [] };
      cardHtml = scheduledCardHtml(data.schedules || []);
    }
    if (!cardHtml) return;
    chat.current.messages.push({ id: "wcard-" + Date.now(), role: "wcard", cardHtml });
    renderChat({ forceBottom: true });
  } catch (error) {
    toast("命令卡生成失败：" + error.message);
  }
}

/* ================================================================ 治理台（G1）
   治理区的对话面：零模型操作员命令流。卡片 = 服务端事实的投影
   （bundle 落盘持久 / 作业状态内存态，重启后以 bundle 为准），
   命令 = 确定性动作（/挖洞 经 POST /api/audit/run → 离线审计管线）。
   模型不进环——审计证据的公信力押在 "no model in the loop" 上。 */

const govJobs = new Map(); // job_id → {job_id, target, status, audit_id, findings, evidence, error}
const govFindings = new Map(); // audit_dir → {findings, target, profile}（G2 发现卡投影；S1 补 target/profile 支撑修洞闭环）
const govRespCards = []; // G3 命令响应卡快照(新在前,会话内存,重启即失)
let govPollTimer = null;

/* 治理对话流的条目包装:操作员动作右对齐回显(用户头像),控制台响应
   左侧印章头像 + 卡片体。零模型不等于没有"对话感"——只是另一侧是控制台。 */
function govEntryHtml(role, bodyHtml) {
  if (!bodyHtml) return "";
  if (role === "user") {
    return `<div class="gov-entry user"><span class="gov-echo">${bodyHtml}</span>${userAvatarHtml(28)}</div>`;
  }
  return `<div class="gov-entry console">${aiAvatarHtml("idle", 28)}<div class="gov-entry-body">${bodyHtml}</div></div>`;
}

/* G3:命令响应卡落到治理台卡流顶部。快照语义——卡片记录命令时刻的
   服务端事实,重进治理台仍在;最新事实以审计台为准。
   echoText:操作员侧的回显(命令或动作),让每条响应有"谁发起的"。 */
function pushGovRespCard(html, echoText) {
  if (!html) return;
  const entry = (echoText ? govEntryHtml("user", esc(echoText)) : "") + govEntryHtml("console", html);
  govRespCards.unshift(entry);
  if (govRespCards.length > 6) govRespCards.length = 6;
  paintGovCards();
}

/* 治理历史 → 对话内打开记录:拉 bundle 进投影,发现卡落到命令卡区顶部。
   零发现的 bundle 给出可见提示,不伪造空卡。 */
async function openGovRecord(dir) {
  renderMode("gov");
  try {
    const r = await fetch("/api/audit/bundle/" + encodeURIComponent(dir), { cache: "no-store" });
    const bundle = r.ok ? await r.json() : null;
    if (!bundle || bundle.error) throw new Error((bundle && bundle.error) || `HTTP ${r.status}`);
    const findings = Array.isArray(bundle.findings) ? bundle.findings : [];
    if (!findings.length) { toast("该审计没有发现"); return; }
    govFindings.set(dir, {
      findings,
      target: (bundle.target || {}).identity || "",
      profile: (bundle.target || {}).profile || "",
    });
    pushGovRespCard(govFindingsCard(dir), `打开记录 ${dir}`);
  } catch (error) {
    toast("打开审计记录失败：" + error.message);
  }
}

function paintGovCards() {
  const host = document.getElementById("gov-cards");
  if (!host) return;
  host.innerHTML = govRespCards.join("");
  if (document.querySelector(".main")) hydrateDoodles(host);
}

function showGovChain(dir) {
  const d = String(dir || "").trim();
  if (!d) { renderMode("gov"); toast("用法：/链 <audit_id>（如 /链 evox.app-2026-11-08-5c093a）"); return; }
  (async () => {
    try {
      renderMode("gov");
      const r = await fetch("/api/audit/chain/" + encodeURIComponent(d), { cache: "no-store" });
      const chain = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(chain.error || `HTTP ${r.status}`);
      pushGovRespCard(chainCardHtml(d, chain), `/链 ${d}`);
    } catch (error) {
      toast("链查询失败：" + error.message);
    }
  })();
}

function showGovJudgments() {
  (async () => {
    try {
      renderMode("gov");
      const r = await fetch("/api/review-profile", { cache: "no-store" });
      const profile = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(profile.error || `HTTP ${r.status}`);
      pushGovRespCard(judgmentsCardHtml(profile), "/判断");
    } catch (error) {
      toast("判断汇总失败：" + error.message);
    }
  })();
}

function govJobCardHtml(job) {
  const state = job.status === "done" ? '<span class="gov-state ok">完成</span>'
    : job.status === "failed" ? `<span class="gov-state bad">失败 · ${esc(String(job.error || "").slice(0, 120))}</span>`
    : '<span class="gov-state live">挖掘中…</span>';
  const stats = job.status === "done"
    ? `<div class="wcard-meta">${num(job.findings || 0)} 发现 · ${num(job.evidence || 0)} 证据记录${job.audit_id ? " · " + esc(job.audit_id) : ""}</div>`
    : "";
  const jump = job.status === "done" && job.audit_id
    ? `<button class="wcard-jump" type="button" data-gov-open-bundle="${esc(job.audit_id)}" title="在审计台查看证据与处置">查看证据 →</button>`
    : "";
  return `<div class="wcard govcard" data-thread-item="govcard">
    <div class="wcard-head"><span class="wcard-icon" data-doodle="stamp"></span>
      <b class="wcard-title">REA 挖洞 · ${esc(job.target || "")}</b>${jump}</div>
    <div class="gov-state-row">${state}</div>${stats}
  </div>`;
}


function paintGovStream() {
  // 只重绘作业段——bundle 段由 renderGov 异步填充，轮询重绘整段会把它
  // 打回"读取中"（冒烟抓到）。两段各一个宿主，互不覆盖。
  const host = document.getElementById("gov-jobs");
  if (!host) return;
  const jobCards = [...govJobs.values()]
    .sort((a, b) => String(b.job_id).localeCompare(String(a.job_id)))
    .map(job => (job.echo ? govEntryHtml("user", esc(job.echo)) : "")
      // G2:作业完成的后面跟一张发现处置卡（内存投影有数据才渲染）
      + govEntryHtml("console", govJobCardHtml(job)
        + (job.status === "done" && job.audit_id && govFindings.get(job.audit_id)
          ? govFindingsCard(job.audit_id)
          : "")))
    .join("");
  host.innerHTML = jobCards;
  if (document.querySelector(".main")) hydrateDoodles(host);
}

/* S1:统一发现卡渲染——artifact 目标不可修（修洞只对源码树开放）。 */
function govFindingsCard(dir) {
  const meta = govFindings.get(dir);
  if (!meta) return "";
  return findingsCardHtml(dir, meta.findings, { repairable: meta.profile !== "artifact" });
}

/* G2:拉一个 bundle 的发现清单进内存投影（含 operator_disposition 当前值）。 */
async function loadGovFindings(auditDir) {
  if (!auditDir || govFindings.has(auditDir)) return;
  try {
    const r = await fetch("/api/audit/bundle/" + encodeURIComponent(auditDir), { cache: "no-store" });
    const bundle = r.ok ? await r.json() : null;
    if (bundle && Array.isArray(bundle.findings) && bundle.findings.length) {
      govFindings.set(auditDir, {
        findings: bundle.findings,
        target: (bundle.target || {}).identity || "",
        profile: (bundle.target || {}).profile || "",
      });
    }
  } catch { /* 单次失败不影响卡流,下次进入重取 */ }
}

async function renderGov() {
  document.getElementById("tb-title").textContent = "治理台";
  document.getElementById("tb-chips").innerHTML = `<span class="chip">治理</span>`;
  setFoot("治理台 · 零模型操作员命令流（/挖洞 <target>）", "");
  renderMain(`<div class="gov-head">
      <p class="eyebrow">GOVERNANCE CONSOLE</p>
      <h1 class="page-title">治理台</h1>
      <p class="page-meta">挖洞、判断、链——都以命令与卡片进行。模型不进环：审计证据由离线确定性管线产出，操作员处置直接落签名链。</p>
    </div>
    <div class="gov-start" aria-label="治理命令">
      <button class="gov-cmd" type="button" data-gov-fill="/挖洞 "><span class="gov-cmd-key">/挖洞</span> <span class="gov-cmd-arg">&lt;target&gt;</span></button>
      <button class="gov-cmd" type="button" data-gov-fill="/判断"><span class="gov-cmd-key">/判断</span></button>
      <button class="gov-cmd" type="button" data-gov-fill="/链 "><span class="gov-cmd-key">/链</span> <span class="gov-cmd-arg">&lt;audit_id&gt;</span></button>
    </div>
    <div class="gov-stream" id="gov-stream">
      <div id="gov-cards"></div>
      <div id="gov-jobs"></div>
      <div id="gov-empty" hidden><div class="gov-hint dim">还没有审计记录——输入 /挖洞 &lt;target&gt; 发起第一次。</div></div>
    </div>`, { mode: "gov" });
  paintGovCards();
  paintGovStream();
  const epoch = viewGate.sequence;
  try {
    const r = await fetch("/api/audit/bundles", { cache: "no-store" });
    const data = r.ok ? await r.json() : { bundles: [] };
    if (epoch !== viewGate.sequence) return;
    const bundles = (data.bundles || [])
      .slice()
      .sort((a, b) => String(b.dir).localeCompare(String(a.dir)));
    // 首进且对话流为空:把最新一条记录作为控制台陈述载入（无操作员回显）
    if (bundles[0] && !govRespCards.length && !govJobs.size) {
      await loadGovFindings(bundles[0].dir);
      if (epoch !== viewGate.sequence) return;
      if (govFindings.get(bundles[0].dir)) pushGovRespCard(govFindingsCard(bundles[0].dir));
    }
    const empty = document.getElementById("gov-empty");
    if (empty) empty.hidden = !!(govRespCards.length || govJobs.size || bundles.length);
  } catch { /* 网络抖动：保留加载态，下次进入重取 */ }
  scheduleGovPoll();
}

function scheduleGovPoll() {
  if (govPollTimer) return;
  govPollTimer = setInterval(async () => {
    const running = [...govJobs.entries()].filter(([, job]) => job.status === "running");
    if (!running.length) {
      clearInterval(govPollTimer);
      govPollTimer = null;
      return;
    }
    await Promise.all(running.map(async ([id, job]) => {
      try {
        const r = await fetch("/api/audit/jobs/" + encodeURIComponent(id), { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        if (data && data.status && data.status !== job.status) {
          Object.assign(job, data);
          // G2:作业刚完成 → 拉发现清单,卡流补上处置卡
          if (job.status === "done" && job.audit_id) await loadGovFindings(job.audit_id);
        }
      } catch { /* 单次轮询失败保旧态 */ }
    }));
    if (document.getElementById("main")?.dataset.view === "gov") paintGovStream();
  }, 2500);
}

function startGovAudit(target) {
  const t = String(target || "").trim();
  if (!t) {
    renderMode("gov");
    toast("用法：/挖洞 <target>（如 /挖洞 /Applications/EvoX.app）");
    return;
  }
  (async () => {
    try {
      const r = await fetch("/api/audit/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: t }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      govJobs.set(data.job_id, { job_id: data.job_id, target: t, status: "running", echo: `/挖洞 ${t}` });
      renderMode("gov");
      toast("挖洞作业已发起 · 治理台跟踪进度");
      scheduleGovPoll();
    } catch (error) {
      toast("挖洞发起失败：" + error.message);
    }
  })();
}

/* G2 发现处置:change 事件文档级委托(卡流重绘频繁,逐卡绑定会被拆掉)。
   POST 落盘+落链;失败回滚 select 并报原因——处置是操作员决定,
   必须给出可见结果(与审计台的处置语义逐条一致)。 */
document.addEventListener("change", event => {
  const select = event.target.closest(".gov-disp");
  if (!select) return;
  const dir = select.dataset.dir;
  const fid = select.dataset.finding;
  const disposition = select.value;
  if (!dir || !fid) return;
  (async () => {
    try {
      const r = await fetch(
        `/api/audit/disposition/${encodeURIComponent(dir)}/${encodeURIComponent(fid)}`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ disposition }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      // 内存投影同步:重绘/轮询后下拉仍显示操作员的选择
      const meta = govFindings.get(dir);
      const finding = meta && meta.findings.find(f => f.finding_id === fid);
      if (finding) finding.operator_disposition = disposition || null;
      toast(`已记录 · ${fid} → ${disposition ? "已落链" : "已清除"}`);
    } catch (error) {
      // 回滚到投影中的当前值
      const meta = govFindings.get(dir);
      const finding = meta && meta.findings.find(f => f.finding_id === fid);
      select.value = (finding && finding.operator_disposition) || "";
      toast("处置失败：" + error.message);
    }
  })();
});

/* ================================================================ Chat
   对话是“回答”，不是“运行”：每次回答仍是一笔受预算约束、进事件链的
   模型调用，但不执行工具。需要执行时，用户点「转为委托」。 */

const {
  adoptDefaultChatModel, loadChatModels, renderChatModelMenu, syncChatConfig,
  renderChatConfig, saveChatConfig, clearChatConfigKey, setDefaultChatModel,
} = createChatConfig({
  esc, num, chat, chatModelChoice, saveChatModelChoice, paintModelBadge, toast,
  adoptDefaultWorkModel,
  getHealth: () => health,
  getChatModels: () => chatModels,
  setChatModels: value => { chatModels = value; },
  getChatConfig: () => chatConfig,
  setChatConfig: value => { chatConfig = value; },
  getChatConfigSource: () => chatConfigSource,
  setChatConfigSource: value => { chatConfigSource = value; },
  getChatModelTouched: () => chatModelTouched,
});

const WORK_STAGE_FOOTNOTE = "模型提议，主机守门。每次工具调用都过权限与预算，每一步都有证据。";

/* 判断记录与定时任务两个 surface（viewer/app/pages.js）。
   数据一个来自 API、一个来自 localStorage；渲染原语仍由本文件提供。 */
const {
  renderReviews, renderScheduledPage,
} = createPages({
  renderMain, setFoot, toast,
  getViewGateSequence: () => viewGate.sequence,
  getScenarios: () => SCENARIOS,
  fetchImpl: (...args) => fetch(...args),
});

/* 审计台 surface（viewer/audit/audit-view.js 的 createAuditSurface）。
   渲染、hash 维护、深链预选与死链自愈都在模块里;这里只注入 app 侧
   关注点：进入收尾、chrome、主区渲染与当前 mode 读取。本文件保留的
   挂点只有三个：MODE_ENTRY 的 audit 项、hashchange 监听、DOMContentLoaded
   深链分支。收尾动作与 openLedger 同源（关浮层/断开实时流/清 store）。 */
const {
  renderAudit, healAuditDeepLink,
} = createAuditSurface({
  beforeEnter() {
    if (intake.active) closeIntake(false);
    if (settingsOpen) closeSettingsPage(false);
    if (activeSession()) detachLive();
    store.key = null;
    store.trace = null;
    renderNav();
    renderTabs();
    const scroller = document.querySelector(".main");
    if (scroller) scroller.scrollTop = 0;
  },
  setChrome() {
    document.getElementById("tb-title").textContent = "审计台 · 发现账本";
    document.getElementById("tb-chips").innerHTML = "";
    const badge = document.getElementById("chain-badge");
    if (badge) {
      badge.className = "chain-pill";
      badge.innerHTML = `<span class="dot"></span><span>未加载</span>`;
    }
  },
  renderMain,
  getMode: () => store.mode,
});

/* 侧栏渲染与外壳交互已抽成独立模块（viewer/app/sidebar.js）。运行行/项目段
   仍在本文件，由 renderNav 通过 deps 编排；置顶状态也留在本文件（chat-ui 需读）。 */
const {
  renderNav, renderSidebarNav, paintNavActive,
  renderPinned, renderQuickStart, markQuickStart, dismissQuickStart,
  paintSections, toggleSection, mountShell: mountSidebarShell,
  renderRuns, renderProjects, addProject, createProject, assignRunProject,
  getProjects, getRunProject, getProjectFilter, setProjectFilter, setOlderOpen,
} = createSidebar({
  renderChats: (...args) => renderChats(...args),
  getRunHistory: () => runHistory,
  getChatThreads: () => chatThreads,
  getModelCatalog: () => modelCatalog,
  getLedgerOpen: () => ledgerOpen,
  getActiveKey: () => store.key,
  isChatPinned,
  stateClass,
  hydrateDoodles,
  doodleSvg,
  tplTitle,
  runLabel,
});

const {
  loadChatThreads, renderChats, handleChatDelete, leaveChat, paintChatChrome, renderChatHead,
  rememberChatMemory, refreshChatMemory, deleteChatMemory,
  detachChatChrome, switchToChat, setUiMode, newChat, openChat, renderChat,
  renderChatAttachChips, handleChatFiles, paintSendState, restoreSendButton, setChatStreaming,
  stopChat, sendChatFromComposer, streamChat, beginChatEdit, cancelChatEdit, saveChatEdit,
  regenerateChat, copyChatMessage, delegateChatMessage,
} = createChatUI({
  chat, chatModelChoice, esc, chatSectionHtml, chatRowsHtml, isChatPinned, toast, intake,
  store, activeSession, detachLive, setChromeState, closeIntake, closeSettingsPage,
  renderNav, renderMain, stageEmptyHtml, autosize, paintSections, selectRun,
  prepareCardRuns: (...args) => prepareCardRuns(...args),
  aiAvatarHtml, chatMessageHtml,
  withRunStatus: (...args) => withRunStatus(...args),
  chatCardHtml: (...args) => chatCardHtml(...args),
  nearMainBottom, scrollMainBottom, hydrateDoodles, parseSseFrame, renderMarkdown,
  copyText, startIntake, threadOrigin, adoptDefaultChatModel, loadChatModels,
  getHealth: () => health,
  getChatThreads: () => chatThreads,
  setChatThreads: value => { chatThreads = value; },
  getChatConfig: () => chatConfig,
  getSettingsOpen: () => settingsOpen,
  setLedgerOpen: value => { ledgerOpen = value; },
  getViewEpoch: () => viewGate.sequence,
  invalidateView: () => viewGate.invalidate(),
});

/* 对话上下文随委托一起走：本轮目标是 message.text，但“周三/当天/到了后”
   指代的是本线程前文。取尾部 24 条（越近越相关），每条截断 600 字，
   与后端 validate_delegation_spec 的上限一致。 */
const ORIGIN_MESSAGES = 24;
const ORIGIN_CHARS = 600;

function threadOrigin(thread) {
  if (!thread) return null;
  const messages = (thread.messages || [])
    .filter(item => item.status !== "error" && (item.role === "user" || item.role === "assistant"))
    .map(item => ({ role: item.role, text: String(item.text || "").slice(0, ORIGIN_CHARS) }))
    .filter(item => item.text)
    .slice(-ORIGIN_MESSAGES);
  return { threadId: thread.id || null, threadTitle: thread.title || "", messages };
}

/* ------------------------------------------- 对话内执行卡（chat v2 · P1）
   对话本身绝不执行工具：模型只能在回答里提出一个提案块，前端把它渲染成一张
   卡片；只有用户点「开始执行」，卡片才把提案（含用户的编辑）交给确认端点，
   由运行流水线创建一次受治理的运行。卡片之后的进度不是前端猜的——它订阅运行
   自己的 SSE，与 Work 视图读同一份签名轨迹。 */

const {
  chatCardHtml, cardState, readCardDraft, paintCard, startCardRun, dismissCard, reconsiderCard,
  withRunStatus, prepareCardRuns, decideFromCard, stopFromCard, openRunFromCard,
} = createChatRuns({
  esc, actionCardHtml, runMirrorHtml, mirrorFrom, stripTags, describe, chat, modelChoice,
  getApprovalMode: () => approvalMode,
  getRunHistory: () => runHistory,
  modelSupportsEffort: key => {
    const entry = modelCatalog.find(model => model.key === key);
    return !!(entry && entry.supportsEffort);
  },
  renderChat, loadChatThreads, toast, applyDelta, setUiMode, openLive,
});

let addMenuWorkspaceOpen = false;
let addMenuError = "";

function bindChatAttachments() {
  const files = document.getElementById("chat-files");
  if (files) files.addEventListener("change", handleChatFiles);
}

function renderAddMenu() {
  const menu = document.getElementById("composer-add-menu");
  const plus = document.getElementById("composer-open");
  if (!menu) return;
  menu.innerHTML = composerAddMenuHtml({
    attachmentCount: chat.attachments.length,
    workspaceOpen: addMenuWorkspaceOpen,
    error: addMenuError,
  });
  menu.hidden = false;
  plus?.classList.add("open");
  plus?.setAttribute("aria-expanded", "true");
  plus?.setAttribute("title", "收起添加内容");
}

function closeAddMenu() {
  const menu = document.getElementById("composer-add-menu");
  const plus = document.getElementById("composer-open");
  if (menu) menu.hidden = true;
  addMenuWorkspaceOpen = false;
  addMenuError = "";
  plus?.classList.remove("open");
  plus?.setAttribute("aria-expanded", "false");
  plus?.setAttribute("title", chat.active ? "添加内容" : "资料 / 工具 / 预算");
}

function toggleAddMenu() {
  const menu = document.getElementById("composer-add-menu");
  if (menu && !menu.hidden) {
    closeAddMenu();
    return;
  }
  if (attach.open) {
    attach.open = false;
    renderAttachPanel();
  }
  addMenuWorkspaceOpen = false;
  addMenuError = "";
  renderAddMenu();
}

async function attachWorkspaceFile(path) {
  const relative = String(path || "").trim();
  if (!relative) {
    addMenuError = "请输入工作区相对路径";
    renderAddMenu();
    return;
  }
  if (chat.attachments.length >= 3) {
    addMenuError = "最多 3 个附件";
    renderAddMenu();
    return;
  }
  try {
    const response = await fetch(`/api/chat/workspace-file?path=${encodeURIComponent(relative)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || typeof data.text !== "string") {
      addMenuError = data.error || "读取失败";
      renderAddMenu();
      return;
    }
    if (chat.attachments.some(item => item.name === data.name && item.text === data.text)) {
      addMenuError = "这个文件已经添加";
      renderAddMenu();
      return;
    }
    chat.attachments.push({ name: data.name, text: data.text });
    renderChatAttachChips();
    paintSendState();
    closeAddMenu();
  } catch {
    addMenuError = "读取失败";
    renderAddMenu();
  }
}

function bindComposerAddMenu() {
  const menu = document.getElementById("composer-add-menu");
  if (!menu) return;
  menu.addEventListener("click", event => {
    const row = event.target.closest("[data-add-action]");
    if (!row) return;
    event.stopPropagation();
    if (row.disabled) return;
    const action = row.dataset.addAction;
    if (action === "files") {
      closeAddMenu();
      document.getElementById("chat-files")?.click();
      return;
    }
    if (action === "workspace") {
      addMenuWorkspaceOpen = true;
      addMenuError = "";
      renderAddMenu();
      menu.querySelector("[data-add-workspace-input]")?.focus();
    }
  });
  menu.addEventListener("submit", event => {
    if (!event.target.closest("[data-add-workspace-form]")) return;
    event.preventDefault();
    const input = menu.querySelector("[data-add-workspace-input]");
    attachWorkspaceFile(input ? input.value : "");
  });
  menu.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    closeAddMenu();
    document.getElementById("composer-open")?.focus();
  });
}

function bind() {
  hydrateDoodles();
  applyMotion();
  bindSettings();
  bindChatAttachments();
  bindComposerAddMenu();
  bindComposer();
  bindGlobalActions();
  bindNavigation();
  bindEvidence();
  bindMainInteractions();
  bindShell();
  // 上下文容量圆圈:每 3s 从 runHistory 取最新 budget 数据刷新
  setInterval(paintCtxRing, 3000);
  paintCtxRing();
}

function bindComposer() {
  document.getElementById("pick").addEventListener("change", event => {
    if (event.target.files[0]) fromFile(event.target.files[0]);
  });
  const ta = document.getElementById("composer-ta");
  ta.addEventListener("input", () => {
    autosize(ta);
    updateSlashMenu();
    if (chat.active) { paintSendState(); return; }
    const s = activeSession();
    if (!(s && s.running && !s.finished)) {
      document.getElementById("send").disabled = !(health && health.live);
    }
  });
  ta.addEventListener("keydown", event => {
    // 判定在 core/keyboard.js（Node 可测）：IME 守卫 → 斜杠菜单 → 提交/历史。
    // 这里只执行效果；行为与内联实现逐条一致（含 preventDefault 的时机）。
    const menu = document.getElementById("slash-menu");
    const action = composerKeyAction(event, {
      slashMenuOpen: !!(menu && !menu.hidden),
      caretAtStart: ta.selectionStart === 0,
      hasHistory: promptHistory.length > 0,
      historyActive: historyIndex !== -1,
    });
    if (action === "ime") return;
    if (action === "slash-down") { event.preventDefault(); slashIndex = Math.min(slashItems.length - 1, slashIndex + 1); updateSlashMenu(); return; }
    if (action === "slash-up") { event.preventDefault(); slashIndex = Math.max(0, slashIndex - 1); updateSlashMenu(); return; }
    if (action === "slash-tab") {
      event.preventDefault();
      const command = slashItems[slashIndex];
      if (command) { ta.value = command.cmd + " "; autosize(ta); updateSlashMenu(); }
      return;
    }
    if (action === "slash-run") {
      event.preventDefault();
      const command = slashItems[slashIndex];
      if (command) runSlash(command.cmd.slice(1));
      return;
    }
    if (action === "slash-close") { event.preventDefault(); menu.hidden = true; return; }
    if (action === "submit") {
      event.preventDefault();
      pushHistory(ta.value.trim());
      composerSubmit();
      return;
    }
    if (action === "history-prev") {
      event.preventDefault();
      if (historyIndex === -1) {
        draftBeforeHistory = ta.value;
        historyIndex = promptHistory.length - 1;
      } else {
        historyIndex = Math.max(0, historyIndex - 1);
      }
      ta.value = promptHistory[historyIndex];
      autosize(ta);
      return;
    }
    if (action === "history-next") {
      event.preventDefault();
      if (historyIndex < promptHistory.length - 1) {
        historyIndex += 1;
        ta.value = promptHistory[historyIndex];
      } else {
        historyIndex = -1;
        ta.value = draftBeforeHistory;
      }
      autosize(ta);
    }
  });
  const apvBtn = document.getElementById("approval-btn");
  const apvMenu = document.getElementById("approval-menu");
  const modelBtn = document.getElementById("model-btn");
  const chatModelMenu = document.getElementById("chat-model-menu");
  const workModelMenu = document.getElementById("work-model-menu");
  const closeModelMenus = () => { chatModelMenu.hidden = true; workModelMenu.hidden = true; };
  apvBtn.addEventListener("click", event => {
    event.stopPropagation();
    closeModelMenus();
    apvMenu.hidden = !apvMenu.hidden;
  });
  const fastBtn = document.getElementById("fast-btn");
  fastBtn?.addEventListener("click", () => {
    const next = !getFastMode();
    setFastMode(next);
    paintModelBadge();
    toast(next
      ? "Fast 已开：一轮多动作 · 免逐步审批 · 项目走隔离副本"
      : "Fast 已关：恢复逐步审批与单动作");
  });
  const plusBtn = document.getElementById("composer-open");
  const attachPanel = document.getElementById("attach-panel");
  plusBtn.addEventListener("click", event => {
    event.stopPropagation();
    apvMenu.hidden = true;
    closeModelMenus();
    if (chat.active) {
      toggleAddMenu();
      return;
    }
    closeAddMenu();
    attach.open = !attach.open;
    renderAttachPanel();
    if (attach.open) refreshAttachData();
  });
  attachPanel.addEventListener("click", event => {
    const sum = event.target.closest("[data-attach-sum]");
    if (sum) {
      event.stopPropagation();
      const key = sum.dataset.attachSum;
      attach.openGroup = attach.openGroup === key ? null : key;
      renderAttachPanel();
      return;
    }
    const tplSel = event.target.closest("[data-attach-tpl]");
    if (tplSel) {
      event.stopPropagation();
      setTemplate(tplSel.dataset.attachTpl);
      return;
    }
    const doc = event.target.closest("[data-attach-doc]");
    if (doc) {
      event.stopPropagation(); // 重渲会移走原节点；先止泡，避免 document 误判为外部点击
      if (doc.checked) attach.scope.add(doc.dataset.attachDoc);
      else attach.scope.delete(doc.dataset.attachDoc);
      renderAttachPanel();
      if (intake.active) renderIntake();
      return;
    }
    const addBtn = event.target.closest("[data-attach-source-add]");
    if (addBtn) {
      event.stopPropagation();
      const input = attachPanel.querySelector("[data-attach-source-input]");
      const url = String(input ? input.value : "").trim();
      if (!/^https?:\/\/.+\./.test(url)) { toast("来源必须是合法的 http(s) URL"); return; }
      if (attach.sources.includes(url)) { toast("该来源已添加"); return; }
      if (attach.sources.length >= 5) { toast("最多 5 个来源"); return; }
      attach.sources.push(url);
      attach.sourcesDraft = "";
      renderAttachPanel();
      // intake 草稿的“来源 n/5”读的是同一份状态;面板里增删后草稿要跟着重绘
      if (intake.active) renderIntake();
      return;
    }
    const removeBtn = event.target.closest("[data-attach-source-remove]");
    if (removeBtn) {
      event.stopPropagation();
      attach.sources.splice(Number(removeBtn.dataset.attachSourceRemove), 1);
      renderAttachPanel();
      if (intake.active) renderIntake();
      return;
    }
    event.stopPropagation(); // 面板内部点击不关闭
  });
  attachPanel.addEventListener("input", event => {
    if (event.target.closest("[data-attach-budget]")) readAttachBudgetInputs();
    const sourceInput = event.target.closest("[data-attach-source-input]");
    if (sourceInput) attach.sourcesDraft = sourceInput.value;
    const projectPath = event.target.closest("[data-project-path]");
    if (projectPath) attach.project.path = projectPath.value;
    const verification = event.target.closest("[data-project-verification]");
    if (verification) attach.project.verification = verification.value;
    if (projectPath || verification) saveProjectContext();
  });
  attachPanel.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    const sourceInput = event.target.closest("[data-attach-source-input]");
    if (!sourceInput) return;
    event.preventDefault();
    const url = String(sourceInput.value || "").trim();
    if (!url) return;
    if (!/^https?:\/\/.+\./.test(url)) { toast("来源必须是合法的 http(s) URL"); return; }
    if (attach.sources.includes(url)) { toast("该来源已添加"); return; }
    if (attach.sources.length >= 5) { toast("最多 5 个来源"); return; }
    attach.sources.push(url);
    attach.sourcesDraft = "";
    renderAttachPanel();
  });
  const projectComposer = document.getElementById("project-composer");
  projectComposer.addEventListener("click", event => {
    event.stopPropagation();
    if (event.target.closest("[data-project-context-edit]")) {
      attach.projectEditing = true;
      renderProjectComposer();
      projectComposer.querySelector("[data-project-path]")?.focus();
      return;
    }
    if (event.target.closest("[data-project-context-done]")) {
      attach.projectEditing = false;
      saveProjectContext();
      renderProjectComposer();
      document.getElementById("composer-ta")?.focus();
    }
  });
  projectComposer.addEventListener("input", event => {
    if (event.target.matches("[data-project-path]")) attach.project.path = event.target.value;
    if (event.target.matches("[data-project-verification]")) attach.project.verification = event.target.value;
    saveProjectContext();
  });
  projectComposer.addEventListener("keydown", event => {
    const action = commitCancelAction(event); // 判定在 core/keyboard.js（Node 可测）
    if (action === "commit") {
      event.preventDefault();
      attach.projectEditing = false;
      saveProjectContext();
      renderProjectComposer();
      document.getElementById("composer-ta")?.focus();
    }
    if (action === "cancel") {
      attach.projectEditing = false;
      renderProjectComposer();
    }
  });
  modelBtn.addEventListener("click", event => {
    event.stopPropagation();
    if (modelBtn.classList.contains("readonly")) {
      toast("运行中使用当前模型；选择器只对新委托生效");
      return;
    }
    apvMenu.hidden = true;
    if (attach.projectEditing) { attach.projectEditing = false; renderProjectComposer(); }
    // S1 分态：模式决定开哪个菜单，另一个强制关闭——两个节点永不同现
    const openMenu = chat.active ? chatModelMenu : workModelMenu;
    const otherMenu = chat.active ? workModelMenu : chatModelMenu;
    if (openMenu.hidden) {
      if (chat.active) {
        renderChatModelMenu();
        // 服务端枚举回来后会重渲染（行数变化 → 高度/锚点变化），重渲染后要重新量一次
        loadChatModels().then(() => {
          if (openMenu.hidden) return;
          renderChatModelMenu();
          placeModelMenu(openMenu);
        });
      } else {
        renderModelMenu();
      }
    }
    otherMenu.hidden = true;
    openMenu.hidden = !openMenu.hidden;
    if (!openMenu.hidden) placeModelMenu(openMenu);
  });
  /* 对话菜单：模型挑选 + 设置入口。 */
  chatModelMenu.addEventListener("click", event => {
    const chatSettings = event.target.closest("[data-chatmodel-settings]");
    if (chatSettings) {
      chatModelMenu.hidden = true;
      renderMode("settings");
      activateSettingsSection("model");
      return;
    }
    const chatPick = event.target.closest("[data-chatmodel-pick]");
    if (chatPick && !chatPick.disabled) {
      chatModelChoice.key = chatPick.dataset.chatmodelPick;
      chatModelTouched = true;
      saveChatModelChoice();
      renderChatModelMenu();
      paintModelBadge();
      chatModelMenu.hidden = true;
    }
  });
  /* 工作菜单：委托模型挑选 + 思考强度档位。 */
  workModelMenu.addEventListener("click", event => {
    const pick = event.target.closest("[data-model-pick]");
    if (pick && !pick.disabled) {
      modelChoice.key = pick.dataset.modelPick;
      modelChoice.touched = true;
      saveModelChoice();
      modelBudget.models = null;
      attach.budget = null;
      renderModelMenu();
      paintModelBadge();
      workModelMenu.hidden = true;
      return;
    }
    const effort = event.target.closest("[data-effort-pick]");
    if (effort) {
      modelChoice.key = "kimi";
      modelChoice.effort = effort.dataset.effortPick;
      modelChoice.touched = true;
      saveModelChoice();
      modelBudget.models = null;
      attach.budget = null;
      renderModelMenu();
      paintModelBadge();
    }
  });
}

function bindGlobalActions() {
  const apvBtn = document.getElementById("approval-btn");
  const apvMenu = document.getElementById("approval-menu");
  const modelBtn = document.getElementById("model-btn");
  const chatModelMenu = document.getElementById("chat-model-menu");
  const workModelMenu = document.getElementById("work-model-menu");
  const plusBtn = document.getElementById("composer-open");
  const attachPanel = document.getElementById("attach-panel");
  document.addEventListener("click", event => {
    if (!apvMenu.hidden && !apvBtn.contains(event.target) && !apvMenu.contains(event.target)) apvMenu.hidden = true;
    for (const menu of [chatModelMenu, workModelMenu]) {
      if (!menu.hidden && !modelBtn.contains(event.target) && !menu.contains(event.target)) menu.hidden = true;
    }
    if (attach.open && !plusBtn.contains(event.target) && !attachPanel.contains(event.target)) {
      attach.open = false;
      renderAttachPanel();
    }
    const addMenu = document.getElementById("composer-add-menu");
    if (addMenu && !addMenu.hidden && !plusBtn.contains(event.target) && !addMenu.contains(event.target)) {
      closeAddMenu();
    }
    const projectComposer = document.getElementById("project-composer");
    if (attach.projectEditing && projectComposer && !projectComposer.contains(event.target)) {
      attach.projectEditing = false;
      renderProjectComposer();
    }
    const slashMenu = document.getElementById("slash-menu");
    const composerBox = document.getElementById("composer-box");
    if (slashMenu && !slashMenu.hidden && composerBox && !composerBox.contains(event.target)) slashMenu.hidden = true;
    if (!event.target.closest(".run-row")) closeRowMenus();

    if (event.target.closest("#proj-add")) {
      event.stopPropagation();
      const addBtn = event.target.closest("#proj-add");
      const input = document.createElement("input");
      input.className = "proj-new-input";
      input.placeholder = "项目名称，回车保存";
      addBtn.replaceWith(input);
      input.focus();
      const commit = () => {
        const name = (input.value || "").trim();
        if (name) createProject(name);
        renderProjects();
      };
      input.addEventListener("keydown", e => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") renderProjects();
      });
      input.addEventListener("blur", commit);
      return;
    }
    const projAssign = event.target.closest("[data-proj-assign]");
    if (projAssign) {
      event.stopPropagation();
      const runRow = projAssign.closest("[data-run]");
      let pid = projAssign.dataset.projAssign;
      if (pid === "new") {
        const name = (prompt("项目名称") || "").trim();
        if (!name) return;
        pid = createProject(name);
      }
      if (runRow) assignRunProject(runRow.dataset.run, pid);
      closeRowMenus();
      renderProjects();
      renderRuns();
      return;
    }
    if (!document.getElementById("foot-menu").hidden
        && !event.target.closest("#user-btn") && !event.target.closest("#foot-menu")) {
      document.getElementById("foot-menu").hidden = true;
      document.getElementById("user-btn")?.setAttribute("aria-expanded", "false");
    }
    // 侧栏 nav-item / 顶栏审计台按钮 / S2.1 区切换器 / W1 对话卡跳转共用 data-open → renderMode 一条路。
    const navOpen = event.target.closest(".nav-item[data-open], .tb-audit-link[data-open], .zone-btn[data-open], .wcard-jump[data-open]");
    if (navOpen) { renderMode(navOpen.dataset.open); return; }
    // G1 治理台:作业卡/记录卡的「查看证据」锚进审计台对应 bundle(深链预选)。
    const govBundle = event.target.closest("[data-gov-open-bundle]");
    if (govBundle) {
      const dir = govBundle.dataset.govOpenBundle;
      if (location.hash === "#/audit/" + dir) renderMode("audit");
      else location.hash = "#/audit/" + dir;
      return;
    }
    // 治理区历史行:在治理台对话内打开该记录(发现卡落卡流,可继续处置/
    // 派修复/重挖)——「查看证据」才是进审计台密集视图的出口。
    const govHist = event.target.closest("[data-gov-hist]");
    if (govHist && govHist.dataset.govHist) {
      openGovRecord(govHist.dataset.govHist);
      return;
    }
    // 治理命令提示条:填充命令词(等操作员补参数后按 ↵),不直接执行
    const govFill = event.target.closest("[data-gov-fill]");
    if (govFill) {
      const ta = document.getElementById("composer-ta");
      if (ta) {
        ta.value = govFill.dataset.govFill;
        autosize(ta);
        ta.focus();
      }
      return;
    }
    // S1 修洞闭环:发现 → Work 委托(目标带发现上下文,验证目标由操作员在确认页补)
    const govRepair = event.target.closest("[data-gov-repair]");
    if (govRepair) {
      const dir = govRepair.dataset.dir;
      const fid = govRepair.dataset.govRepair;
      const meta = govFindings.get(dir);
      const finding = meta && meta.findings.find(f => f.finding_id === fid);
      if (!finding) { toast("发现上下文缺失，请先重新挖洞"); return; }
      const severity = { high: "高", medium: "中", low: "低" }[finding.severity] || finding.severity || "";
      // 自动挂项目:修复运行拿到的是目标的隔离副本(基线 git),否则 agent
      // 在空 workspace 里根本看不到要修的文件。终态后可一键把补丁应用回原目录。
      if (meta.target) {
        attach.project.path = meta.target;
        attach.project.verification = "";
      }
      (async () => {
        // 把 POC 证据摘要注入目标:agent 在隔离副本里无法读 work/audit
        // (work/ 被拷贝排除),不注入摘要它就无从得知具体文件与行号。
        let evidenceNote = "";
        try {
          const pocRef = (finding.poc_refs || [])[0];
          if (pocRef) {
            const r = await fetch(`/api/audit/evidence/${encodeURIComponent(dir)}/${encodeURIComponent(pocRef)}`, { cache: "no-store" });
            const record = r.ok ? await r.json() : null;
            const note = String((record && (record.notes || record.expectation)) || "").replace(/\s+/g, " ").trim();
            if (note) evidenceNote = note.slice(0, 600);
          }
        } catch { /* 证据拉取失败不阻断派修 */ }
        // 服务端 objective 上限 600 字符；证据摘要只能吃剩余预算，
        // 否则"头 + 证据 + 尾"超限会让委托直接 400。
        const head = [
          `修复审计发现 ${fid}（${finding.category || "?"} · ${finding.root_cause_group || "?"} · ${severity}）`,
          meta.target ? `审计目标：${meta.target}` : "",
          "你的工作区是审计目标的隔离副本（work/ 等目录不在副本内，不要去找审计记录文件）。",
        ].filter(Boolean).join("\n");
        const tail = "要求：在工作区副本里定位并修复根因（不掩盖症状）；优先最小改动，保持现有行为，跑通相关测试后写清改了什么。";
        const evidenceBudget = 600 - head.length - tail.length - 2;
        const evidence = evidenceNote && evidenceBudget > 20
          ? `证据摘要：${evidenceNote.slice(0, evidenceBudget - "证据摘要：".length)}`
          : "";
        const objective = [head, evidence, tail].filter(Boolean).join("\n");
        startIntake(objective);
      })();
      return;
    }
    // S1 修洞闭环:修复后重新挖洞——同目标再审计,回归对比看 fixed / regressed
    const govRedig = event.target.closest("[data-gov-redig]");
    if (govRedig) {
      const dir = govRedig.dataset.govRedig;
      const meta = govFindings.get(dir);
      if (!meta || !meta.target) { toast("审计目标缺失，无法重新挖洞"); return; }
      startGovAudit(meta.target);
      return;
    }
    // G2 治理台发现卡:处置下拉 change 事件在 document 级委托(见下方
    // bindGovDisposition),落 POST /api/audit/disposition → 服务端原子写盘+落链。
    // 对话内写工具的逐调用审批（agentic chat）
    const toolAct = event.target.closest("[data-tool-allow], [data-tool-deny], [data-tool-remember]");
    if (toolAct) {
      event.stopPropagation();
      const approvalId = toolAct.dataset.toolApproval;
      const threadId = chat.current && chat.current.id;
      if (!approvalId || !threadId) return;
      const allow = toolAct.hasAttribute("data-tool-allow");
      const remember = toolAct.hasAttribute("data-tool-remember");
      const card = toolAct.closest(".chat-tool");
      if (card) {
        for (const button of card.querySelectorAll(".chat-tool-approval-actions button")) {
          button.disabled = true;
        }
      }
      fetch(`/api/chat/threads/${encodeURIComponent(threadId)}/tool-decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvalId, allow, remember }),
      }).catch(() => {
        if (card) for (const b of card.querySelectorAll(".chat-tool-approval-actions button")) b.disabled = false;
      });
      return;
    }
    if (event.target.closest("[data-new-task]")) { newTask(); return; }
    if (event.target.closest("[data-proj-add]")) { addProject(event.target.closest("[data-proj-add]")); return; }
    const sectionToggle = event.target.closest("[data-section-toggle]");
    if (sectionToggle) { toggleSection(sectionToggle); return; }
    if (event.target.closest("[data-quickstart-close]")) { dismissQuickStart(); return; }
    const quick = event.target.closest("[data-quickstart]");
    if (quick) { quickStartGo(quick.dataset.quickstart); return; }
    const chatPin = event.target.closest("[data-chat-pin]");
    if (chatPin) { toggleChatPin(chatPin.dataset.chatPin); return; }
    const chatDelete = event.target.closest("[data-chat-delete]");
    if (chatDelete) { handleChatDelete(chatDelete.dataset.chatDelete, chatDelete); return; }
    if (event.target.closest("[data-chat-more]")) { loadChatThreads(false); return; }
    const chatRow = event.target.closest("#chat-nav [data-chat-id]");
    if (chatRow) { openChat(chatRow.dataset.chatId); return; }
    const chatEdit = event.target.closest("[data-chat-edit]");
    if (chatEdit) { beginChatEdit(chatEdit.dataset.chatEdit); return; }
    const chatSave = event.target.closest("[data-chat-save]");
    if (chatSave) { saveChatEdit(chatSave.dataset.chatSave); return; }
    if (event.target.closest("[data-chat-cancel]")) { cancelChatEdit(); return; }
    const chatRegen = event.target.closest("[data-chat-regen]");
    if (chatRegen) { regenerateChat(chatRegen.dataset.chatRegen); return; }
    const chatCopy = event.target.closest("[data-chat-copy]");
    if (chatCopy) { copyChatMessage(chatCopy.dataset.chatCopy); return; }
    const chatDelegate = event.target.closest("[data-chat-delegate]");
    if (chatDelegate) { delegateChatMessage(chatDelegate.dataset.chatDelegate); return; }
    const cardStart = event.target.closest("[data-run-start]");
    if (cardStart) { startCardRun(cardStart.dataset.runStart); return; }
    const cardCancel = event.target.closest("[data-run-cancel]");
    if (cardCancel) { dismissCard(cardCancel.dataset.runCancel); return; }
    const cardAgain = event.target.closest("[data-run-reconsider]");
    if (cardAgain) { reconsiderCard(cardAgain.dataset.runReconsider); return; }
    const cardMode = event.target.closest("[data-run-mode]");
    if (cardMode) {
      const holder = cardMode.closest("[data-run-card]");
      if (holder) {
        readCardDraft(holder.dataset.runCard);
        cardState(holder.dataset.runCard).mode = cardMode.dataset.runMode;
        paintCard(holder.dataset.runCard);
      }
      return;
    }
    const cardAllow = event.target.closest("[data-run-allow]");
    if (cardAllow) {
      const holder = cardAllow.closest("[data-run-card]");
      if (holder) decideFromCard(holder.dataset.runCard, cardAllow.dataset.runAllow, true);
      return;
    }
    const cardDeny = event.target.closest("[data-run-deny]");
    if (cardDeny) {
      const holder = cardDeny.closest("[data-run-card]");
      if (holder) decideFromCard(holder.dataset.runCard, cardDeny.dataset.runDeny, false);
      return;
    }
    const cardStop = event.target.closest("[data-run-stop]");
    if (cardStop) { stopFromCard(cardStop.dataset.runStop); return; }
    const cardOpen = event.target.closest("[data-run-open]");
    if (cardOpen) { openRunFromCard(cardOpen.dataset.runOpen); return; }
    const chatRemove = event.target.closest("[data-chat-attach-remove]");
    if (chatRemove) {
      const index = Number(chatRemove.dataset.chatAttachRemove);
      if (Number.isInteger(index)) chat.attachments.splice(index, 1);
      renderChatAttachChips();
      paintSendState();
      return;
    }
    if (event.target.closest("#new-run")) { newChat(); return; }
    if (event.target.closest("#send")) {
      if (chat.active && chat.streaming) { stopChat(); return; }
      composerSubmit();
      return;
    }
    if (event.target.closest("#run-chip")) { stopRun(); return; }
    if (event.target.closest("#chain-badge")) { reverify(); return; }
    if (event.target.closest("[data-rail-close]")) { setChromeState({ rail: false }); return; }
    if (event.target.closest("#report-close") || event.target.closest("[data-report-close]")) { closeReport(); return; }
    if (event.target.closest("#report-print")) { printReport(); return; }
    if (event.target.closest("#report-download")) { downloadReport(); return; }
    if (event.target.closest("#foot-palette")) { openPalette(); return; }
    if (event.target.closest("#settings-btn") || event.target.closest("#foot-settings")) { toggleSettingsPage(); return; }
    if (event.target.closest("#settings-mobile")) { renderMode("settings"); return; }
  });
  document.addEventListener("input", event => {
    const field = event.target.closest("[data-run-goal], [data-run-sources]");
    if (!field) return;
    const holder = field.closest("[data-run-card]");
    if (!holder) return;
    readCardDraft(holder.dataset.runCard);
  });
}

function bindNavigation() {
  const palInput = document.getElementById("palette-input");
  palInput.addEventListener("input", () => { resetPaletteActive(); });
  palInput.addEventListener("keydown", event => {
    if (event.key === "ArrowDown") { event.preventDefault(); paletteNav(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); paletteNav(-1); }
    else if (event.key === "Enter") { event.preventDefault(); runPaletteItem(activeIndex()); }
    else if (event.key === "Escape") { event.preventDefault(); closePalette(); }
  });
  document.getElementById("palette").addEventListener("click", event => {
    if (event.target.closest("[data-palette-close]")) { closePalette(); return; }
    const item = event.target.closest("[data-idx]");
    if (item) runPaletteItem(Number(item.dataset.idx));
  });
  document.getElementById("palette").addEventListener("mousemove", event => {
    const item = event.target.closest("[data-idx]");
    if (!item) return;
    const index = Number(item.dataset.idx);
    setActiveIndex(index);
  });
  document.getElementById("approval-menu").addEventListener("click", event => {
    const button = event.target.closest("[data-approval-mode]");
    if (!button) return;
    const changed = button.dataset.approvalMode !== approvalMode;
    setApprovalMode(button.dataset.approvalMode);
    document.getElementById("approval-menu").hidden = true;
    const s = activeSession();
    if (changed && s && s.running) toast("已切换 · 下一个任务生效");
  });
  // 运行行出现在两处（主列表 + 置顶段），事件委托绑在侧栏容器上
  const sidebarContainer = document.querySelector(".sidebar");
  sidebarContainer.addEventListener("click", event => {
    const moreBtn = event.target.closest("[data-more]");
    if (moreBtn) {
      event.stopPropagation();
      const row = moreBtn.closest("[data-run]");
      const menu = row && row.querySelector(".row-menu");
      if (!menu) return;
      const wasHidden = menu.hidden;
      closeRowMenus();
      menu.hidden = !wasHidden;
      if (!menu.hidden) {
        const del = menu.querySelector('[data-row-act="delete"]');
        if (del) { del.textContent = "删除"; delete del.dataset.confirm; }
      }
      return;
    }
    const rowAct = event.target.closest("[data-row-act]");
    if (rowAct) {
      event.stopPropagation();
      const row = rowAct.closest("[data-run]");
      if (!row) return;
      const label = row.querySelector("[data-name]");
      const act = rowAct.dataset.rowAct;
      if (act === "pin") { closeRowMenus(); togglePin(row.dataset.run); }
      else if (act === "rename") { closeRowMenus(); startRename(row.dataset.run, label); }
      else if (act === "delete") {
        if (rowAct.dataset.confirm === "1") { closeRowMenus(); deleteRun(row.dataset.run); }
        else { rowAct.dataset.confirm = "1"; rowAct.textContent = "确认删除？"; }
      }
      else if (act === "project") {
        const pid = getRunProject(row.dataset.run);
        const projectList = getProjects();
        menu.innerHTML = '<div class="row-menu-title">移动到项目</div>'
          + (projectList.length
            ? projectList.map(prj => '<button data-proj-assign="' + esc(prj.id) + '">' + esc(prj.name)
              + (pid === prj.id ? ' ✓' : '') + '</button>').join("")
            : '<div class="dim" style="padding:4px 10px">还没有项目</div>')
          + '<button data-proj-assign="new">＋ 新建项目…</button>';
        return;
      }
      return;
    }
    if (event.target.closest("[data-more-older]")) { setOlderOpen(true); renderRuns(); return; }
    if (event.target.closest("[data-collapse-older]")) { setOlderOpen(false); renderRuns(); return; }
    const row = event.target.closest("[data-run]");
    if (!row) return;
    if (event.target.closest(".row-menu") || event.target.closest(".rename-input")) return;
    selectRun(row.dataset.run);
  });
  // Projects 段落只有一个宿主（统一侧栏），事件仍由侧栏统一委托
  {
    const el = document.getElementById("chat-projects");
    if (el) el.addEventListener("click", event => {
      const row = event.target.closest("[data-proj]");
      if (!row) return;
      setProjectFilter(getProjectFilter() === row.dataset.proj ? null : row.dataset.proj);
      renderProjects();
      renderRuns();
      renderChats();
    });
  }
  sidebarContainer.addEventListener("keydown", event => {
    const row = event.target.closest("[data-run]");
    if (!row || event.target !== row) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectRun(row.dataset.run);
    }
  });
  sidebarContainer.addEventListener("dblclick", event => {
    const label = event.target.closest("[data-name]");
    if (!label) return;
    const row = label.closest("[data-run]");
    if (!row) return;
    event.stopPropagation();
    startRename(row.dataset.run, label);
  });
  document.getElementById("tabs").addEventListener("click", event => {
    const close = event.target.closest("[data-tabclose]");
    if (close) { event.stopPropagation(); closeSessionTab(close.dataset.tabclose); return; }
    const tab = event.target.closest("[data-tab]");
    if (!tab) return;
    const id = tab.dataset.tab;
    if (store.key === "run:" + id && activeSession() === sessions.get(id)) return;
    const s = sessions.get(id);
    if (s) openLive(id, s.objective, s.template);
  });
}

function bindEvidence() {
  document.getElementById("rail-tabs").addEventListener("click", event => {
    const button = event.target.closest("[data-tab]");
    if (!button) return;
    railTab = button.dataset.tab;
    const trace = shownTrace();
    if (trace) renderRail(trace, store.check || { ok: false, reason: "" });
  });
  document.getElementById("rail-body").addEventListener("click", async event => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const ok = await copyText(copy.dataset.copy);
      toast(ok ? "哈希已复制" : "复制失败");
      return;
    }
    if (event.target.closest("[data-copy-raw]")) {
      const trace = shownTrace();
      if (!trace) return;
      const ok = await copyText(JSON.stringify(trace, null, 2));
      toast(ok ? "原文已复制" : "复制失败");
      return;
    }
    if (event.target.closest("[data-download-raw]")) {
      const trace = shownTrace();
      if (!trace) return;
      const blob = new Blob([JSON.stringify(trace, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = "trace.json"; link.click();
      URL.revokeObjectURL(url);
      return;
    }
    const chip = event.target.closest("[data-rail-filter]");
    if (chip) {
      railFilter = chip.dataset.railFilter;
      localStorage.setItem("fa.railFilter", railFilter);
      const trace = shownTrace();
      if (trace) renderRail(trace, store.check || { ok: false, reason: "" });
      return;
    }
    const toggle = event.target.closest("[data-ev-group]");
    if (toggle) {
      const round = Number(toggle.dataset.evGroup);
      if (evExpanded.has(round)) evExpanded.delete(round); else evExpanded.add(round);
      const trace = shownTrace();
      if (trace) renderRail(trace, store.check || { ok: false, reason: "" });
    }
  });
}

function bindMainInteractions() {
  document.getElementById("main").addEventListener("click", async event => {
    const workTab = event.target.closest("[data-work-tab]");
    if (workTab) {
      const tab = workTab.dataset.workTab;
      const s = activeSession();
      if (s) s.workTab = tab; else store.workTab = tab;
      const layout = workTab.closest("[data-project-work]");
      layout?.querySelectorAll("[data-work-pane]").forEach(pane => { pane.hidden = pane.dataset.workPane !== tab; });
      layout?.querySelectorAll("[data-work-tab]").forEach(button => {
        const selected = button.dataset.workTab === tab;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-selected", String(selected));
      });
      if (tab === "files" && shownTrace()) loadProjectWorkspace(shownTrace());
      return;
    }
    const applyButton = event.target.closest("[data-project-apply]");
    if (applyButton) {
      // 两击确认:应用会修改原目录,第一次点击只进入待确认态。
      if (applyButton.dataset.armed !== "1") {
        applyButton.dataset.armed = "1";
        applyButton.textContent = "再点一次确认应用";
        setTimeout(() => {
          if (applyButton.dataset.armed === "1") {
            applyButton.dataset.armed = "0";
            applyButton.textContent = "应用到原项目";
          }
        }, 4000);
        return;
      }
      applyButton.dataset.armed = "0";
      applyButton.textContent = "应用到原项目";
      const owner = applyButton.closest("[data-project-result], [data-project-work]");
      const taskId = owner?.dataset.projectResult || owner?.dataset.projectWork || "";
      const runId = taskId.startsWith("task-") ? taskId.slice(5) : projectRunId(shownTrace());
      if (!runId) { toast("找不到运行 id"); return; }
      (async () => {
        try {
          const r = await fetch(`/api/runs/${encodeURIComponent(runId)}/apply-patch`, { method: "POST" });
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
          toast(`补丁已应用到原项目（${data.files?.length || 0} 个文件）· 建议重新挖洞验证`);
        } catch (error) {
          toast("应用失败：" + error.message);
        }
      })();
      return;
    }
    const patchButton = event.target.closest("[data-project-patch]");
    if (patchButton) {
      const trace = shownTrace();
      const patch = trace?.project?.patch;
      if (!patch) return;
      const url = URL.createObjectURL(new Blob([patch], { type: "text/x-diff;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "changes.patch";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return;
    }
    const projectFile = event.target.closest("[data-project-file]");
    if (projectFile) {
      const scope = projectFile.closest("[data-work-pane], .project-result");
      const preview = scope?.querySelector("[data-project-file-content]");
      const owner = projectFile.closest("[data-project-result], [data-project-work]");
      const taskId = owner?.dataset.projectResult || owner?.dataset.projectWork || "";
      const runId = taskId.startsWith("task-") ? taskId.slice(5) : projectRunId(shownTrace());
      if (!runId || !preview) return;
      const path = projectFile.dataset.projectFile;
      scope.querySelectorAll("[data-project-file]").forEach(button => button.classList.toggle("selected", button === projectFile));
      preview.hidden = false;
      preview.dataset.file = path;
      preview.textContent = "读取文件中…";
      try {
        const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/workspace/${encodeURIComponent(path)}`, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (preview.dataset.file === path) preview.textContent = `${path}\n\n${data.content || ""}`;
      } catch (error) { if (preview.dataset.file === path) preview.textContent = `读取失败：${error.message}`; }
      return;
    }
    const railOpenButton = event.target.closest("[data-rail-open]");
    if (railOpenButton) { setChromeState({ rail: true }); return; }
    const railCloseButton = event.target.closest("[data-rail-close]");
    if (railCloseButton) { setChromeState({ rail: false }); return; }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const ok = await copyText(copy.dataset.copy);
      toast(ok ? "哈希已复制" : "复制失败");
      return;
    }
    const qAct = event.target.closest("[data-q-act]");
    if (qAct) { handleQueueAction(qAct); return; }
    const stageChip = event.target.closest("[data-stage-chip]");
    if (stageChip) {
      const key = stageChip.dataset.stageChip;
      setTemplate(key);
      const template = (templates || []).find(t => t.key === key);
      const objective = (template && template.objective) || "";
      startIntake(objective);
      return;
    }
    const intakeChoice = event.target.closest("[data-intake-choice]");
    if (intakeChoice) {
      intakeChoose(intakeChoice.dataset.intakeField, intakeChoice.dataset.intakeValue, intakeChoice.dataset.intakeLabel || "");
      return;
    }
    if (event.target.closest("[data-intake-start]")) { startIntakeRun(); return; }
    if (event.target.closest("[data-intake-back]")) { intakeBack(); return; }
    if (event.target.closest("[data-intake-cancel]")) { closeIntake(true); return; }
    if (event.target.closest("[data-intake-open]")) { startIntake(""); return; }
    const reviewChoice = event.target.closest("[data-review-decision]");
    if (reviewChoice) {
      const panel = reviewChoice.closest("[data-review-panel]");
      if (panel) chooseReviewDecision(panel.dataset.reviewRun, reviewChoice.dataset.reviewDecision);
      return;
    }
    if (event.target.closest("[data-review-submit]")) {
      const panel = event.target.closest("[data-review-panel]");
      if (panel) submitReview(panel.dataset.reviewRun);
      return;
    }
    const reviewAppend = event.target.closest("[data-review-append]");
    if (reviewAppend) {
      const panel = reviewAppend.closest("[data-review-panel]");
      if (panel) startAppendReview(panel.dataset.reviewRun);
      return;
    }
    const reviewApply = event.target.closest("[data-review-apply]");
    if (reviewApply) {
      const runId = reviewRunId();
      if (runId) applyReviewSuggestion(runId, reviewApply.dataset.reviewApply, reviewApply.dataset.reviewApplyReason || "");
      return;
    }
    const ledDel = event.target.closest("[data-led-del]");
    if (ledDel) {
      const id = ledDel.dataset.ledDel;
      if (ledgerConfirmId !== id) { ledgerConfirmId = id; renderLedger(); }
      else { ledgerConfirmId = null; deleteRun(id); }
      return;
    }
    const lineageJump = event.target.closest("[data-led-row-jump]");
    if (lineageJump) { selectRun(lineageJump.dataset.ledRowJump); return; }
    const ledRow = event.target.closest("[data-led-row]");
    if (ledRow && !event.target.closest("[data-led-del]")) { selectRun(ledRow.dataset.ledRow); return; }
    const originChip = event.target.closest("[data-origin-thread]");
    if (originChip) {
      const threadId = originChip.dataset.originThread;
      if (threadId) { setUiMode(); openChat(threadId); }
      return;
    }
    const threadToggle = event.target.closest("[data-thread-toggle]");
    if (threadToggle) {
      const item = threadToggle.closest(".th-item");
      if (item) {
        const open = item.dataset.open !== "true";
        item.dataset.open = String(open);
        item.dataset.user = "1";
        item.classList.toggle("open", open);
        threadToggle.setAttribute("aria-expanded", String(open));
        return;
      }
    }
    const questionSubmit = event.target.closest("[data-question-submit]");
    if (questionSubmit) { answerQuestion(false); return; }
    const questionSkip = event.target.closest("[data-question-skip]");
    if (questionSkip) { answerQuestion(true); return; }
    const decision = event.target.closest("[data-decision]");
    if (decision) {
      const choice = decision.dataset.decision;
      decideApproval(choice !== "deny", choice === "allow-remember");
      return;
    }
    const planEdit = event.target.closest("[data-plan-edit]");
    if (planEdit) {
      const s = activeSession();
      if (!s) return;
      s.planEditing = !s.planEditing;
      s.planConfirm = null;
      paintPlan();
      return;
    }
    const planAction = event.target.closest("[data-plan-action]");
    const planSession = activeSession();
    if (planAction && planSession) {
      const action = planAction.dataset.planAction;
      if (action === "cancel") {
        planSession.planConfirm = null;
        paintPlan();
      } else if (action === "skip") {
        planSession.planConfirm = planAction.dataset.todo;
        planSession.planEditing = true;
        paintPlan();
      } else if (action === "skip-confirm") {
        queuePlan("skip", planAction.dataset.todo);
      } else if (action === "promote") {
        queuePlan("promote", planAction.dataset.todo);
      }
      return;
    }
    const budgetEdit = event.target.closest("[data-budget-edit]");
    if (budgetEdit) {
      const s = activeSession();
      if (!s) return;
      s.budgetEditing = !s.budgetEditing;
      paintBudget();
      return;
    }
    const budgetCancel = event.target.closest("[data-budget-cancel]");
    if (budgetCancel) {
      const s = activeSession();
      if (!s) return;
      s.budgetEditing = false;
      paintBudget();
      return;
    }
    if (event.target.closest("[data-budget-apply]")) { queueBudget(); return; }
    if (event.target.closest("[data-resume-apply]")) { resumeRun(); return; }
    const resumeCancel = event.target.closest("[data-resume-cancel]");
    if (resumeCancel) {
      const form = resumeCancel.closest(".resume-form");
      if (form) form.remove();
      return;
    }
    const button = event.target.closest("[data-act]");
    if (!button) return;
    if (button.dataset.act === "retry") retryCurrent();
    else if (button.dataset.act === "diag") copyDiagnostics();
    else if (button.dataset.act === "report") openReport();
    else if (button.dataset.act === "resume") openResumeForm();
  });
  document.getElementById("main").addEventListener("input", event => {
    const input = event.target.closest("[data-review-reason]");
    if (!input) return;
    const panel = input.closest("[data-review-panel]");
    if (panel) syncReviewDraft(panel.dataset.reviewRun, panel, input);
  });
  document.getElementById("main").addEventListener("keydown", event => {
    const action = commitCancelAction(event); // 判定在 core/keyboard.js（Node 可测）
    if (action !== "commit" && action !== "cancel") return;
    const input = event.target.closest(".q-edit");
    if (!input) return;
    const item = input.closest("[data-mid]");
    const s = activeSession();
    if (!s || !item) return;
    const msg = (s.queuedMsgs || []).find(m => m.id === item.dataset.mid);
    if (!msg) return;
    if (action === "commit") { event.preventDefault(); saveQueueEdit(s, msg); }
    else if (action === "cancel") { event.preventDefault(); s.queueEditingId = null; paintQueuedMsgs(); }
  });
}

function bindShell() {
  const app = document.getElementById("app");
  setChromeState({ rail: railOpen });
  bindWorkspaceNavigation({ openPalette, setMobileNavigation });
  bindLatestScroll();
  document.getElementById("sidebar-toggle").addEventListener("click", () => {
    if (innerWidth <= 900) setMobileNavigation(false);
    else setChromeState({ sidebar: !app.classList.contains("side-collapsed") });
  });
  document.querySelector(".sidebar").addEventListener("click", event => {
    if (innerWidth <= 900 && event.target.closest("[data-run], [data-chat-id], [data-open], #new-run")) {
      setMobileNavigation(false);
    }
  });
  const userBtn = document.getElementById("user-btn");
  const footMenu = document.getElementById("foot-menu");
  userBtn.addEventListener("click", event => {
    event.stopPropagation();
    const open = footMenu.hidden;
    footMenu.hidden = !open;
    userBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  footMenu.addEventListener("click", event => event.stopPropagation());
  document.getElementById("foot-settings").addEventListener("click", () => {
    footMenu.hidden = true;
    userBtn.setAttribute("aria-expanded", "false");
    toggleSettingsPage();
  });
  document.getElementById("foot-github").addEventListener("click", () => {
    window.open("https://github.com/Anhao1314/FCode", "_blank", "noopener");
    footMenu.hidden = true;
  });
  document.getElementById("rail-toggle").addEventListener("click", () => {
    setChromeState({ rail: !railOpen });
  });
  document.querySelectorAll("[data-rail-open]").forEach(button => {
    button.addEventListener("click", () => setChromeState({ rail: true }));
  });
  document.querySelectorAll("[data-rail-close]").forEach(button => {
    button.addEventListener("click", () => setChromeState({ rail: false }));
  });

  document.addEventListener("dragenter", e => e.preventDefault());
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", e => {
    e.preventDefault();
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) fromFile(file);
  });

  document.addEventListener("keydown", e => {
    const mod = e.metaKey || e.ctrlKey;
    const s = activeSession();
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); togglePalette(); return; }
    if (mod && e.key.toLowerCase() === "n") {
      // 工作模式已收敛退役（uiMode 恒为 chat）:⌘N 只有"新对话"一种语义。
      e.preventDefault();
      newChat();
      return;
    }
    if (mod && e.key === ",") { e.preventDefault(); toggleSettingsPage(); return; }
    // ⌘1/⌘2 的工作/对话切换随 pane-work 一起退役——侧栏只剩一套列表。
    if (mod && e.key.toLowerCase() === "b") {
      e.preventDefault();
      const shell = document.getElementById("app");
      setChromeState({ sidebar: !shell.classList.contains("side-collapsed") });
      return;
    }
    if (mod && e.key === ".") { e.preventDefault(); if (chat.active && chat.streaming) { stopChat(); return; } if (s && s.running) stopRun(); return; }
    if (mod && s && s.pending && !s.deciding && (e.key === "Enter" || e.key === "Backspace" || e.key === "Delete")) {
      const palette = document.getElementById("palette");
      if (palette && !palette.hidden) return;
      e.preventDefault();
      decideApproval(e.key === "Enter");
      return;
    }
    const typing = e.target && /INPUT|TEXTAREA/.test(e.target.tagName);
    if (!typing && !mod && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      const rows = [...document.querySelectorAll(".run-row")];
      if (rows.length) {
        e.preventDefault();
        const index = rows.indexOf(document.activeElement);
        const step = e.key === "ArrowDown" ? 1 : -1;
        const next = index === -1 ? 0 : Math.max(0, Math.min(rows.length - 1, index + step));
        rows[next].focus();
        return;
      }
    }
    if (!typing && !mod && e.key === "/") {
      // Kimi 的侧栏没有搜索框：搜索一律走命令面板（⌘K / "/"）
      e.preventDefault();
      openPalette();
      return;
    }
    if (!typing && s && s.pending && !s.deciding && (e.key === "1" || e.key === "2" || e.key === "3")) {
      const overlay = ["palette", "report-layer"].some(id => {
        const el = document.getElementById(id);
        return el && !el.hidden;
      });
      if (!overlay) {
        e.preventDefault();
        // 数字键直选：等于点击对应档位按钮；先给焦点环，再提交决定
        const button = document.querySelector(`.apv-choice[data-decision="${e.key === "1" ? "allow" : e.key === "2" ? "allow-remember" : "deny"}"]`);
        if (button) {
          button.classList.add("kb-selected");
          button.focus();
          decideApproval(e.key !== "3", e.key === "2");
        }
        return;
      }
    }
    if (e.key === "Escape") {
      if (app.classList.contains("mobile-nav-open")) { setMobileNavigation(false); return; }
      const reportLayer = document.getElementById("report-layer");
      if (reportLayer && !reportLayer.hidden) { closeReport(); return; }
      const palette = document.getElementById("palette");
      if (palette && !palette.hidden) { closePalette(); return; }
      const footMenu = document.getElementById("foot-menu");
      if (footMenu && !footMenu.hidden) {
        footMenu.hidden = true;
        document.getElementById("user-btn")?.setAttribute("aria-expanded", "false");
        return;
      }
      const menu = document.getElementById("tpl-menu");
      if (menu && !menu.hidden) { menu.hidden = true; return; }
      const approval = document.getElementById("approval-menu");
      if (approval && !approval.hidden) { approval.hidden = true; return; }
      const modelMenus = ["chat-model-menu", "work-model-menu"]
        .map(id => document.getElementById(id)).filter(menu => menu && !menu.hidden);
      if (modelMenus.length) { modelMenus.forEach(menu => { menu.hidden = true; }); return; }
      const addMenu = document.getElementById("composer-add-menu");
      if (addMenu && !addMenu.hidden) { closeAddMenu(); return; }
      if (attach.open) { attach.open = false; renderAttachPanel(); return; }
      if (settingsOpen) { closeSettingsPage(true); return; }
      if (!typing && innerWidth <= 1100 && railOpen) setChromeState({ rail: false });
      return;
    }
    if (typing || mod) return;
    if (e.key === "r") replay();
  });

  setInterval(() => {
    const el = document.querySelector("[data-countdown]");
    const s = activeSession();
    if (!el || !s || !s.pendingDeadline) return;
    const left = (s.pendingDeadline - Date.now()) / 1000;
    el.textContent = left > 0 ? fmtCountdown(left) : "已到时限 · 等待 Runtime 拒绝";
  }, 500);
}

function replay() {
  if (intake.active) closeIntake(false);
  if (settingsOpen) closeSettingsPage(false);
  const key = store.key;
  if (key && key.startsWith("run:")) return; // run records are immutable
  if (key) { cache.delete(key); select(key, labelOf(key)); }
  else {
    const trace = shownTrace();
    if (trace) render(trace, "trace");
  }
}

async function fromFile(file) {
  const ticket = viewGate.begin();
  try {
    if (intake.active) closeIntake(false);
    if (settingsOpen) closeSettingsPage(false);
    ledgerOpen = false;
    const trace = JSON.parse(await file.text());
    if (!ticket.isCurrent()) return;
    if (activeSession()) detachLive();
    store.key = null;
    renderNav();
    await render(trace, file.name);
  } catch (error) {
    if (!ticket.isCurrent()) return;
    renderMain(`<div class="empty">无法解析 ${esc(file.name)}：${esc(error.message)}</div>`, { mode: "error" });
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  mountSidebarShell();   // 拖宽 / 滚动遮罩 / 跑马灯（原底部 IIFE，P2 抽离）
  bind();
  renderNav();
  // await：?run= 直链要在 health/runHistory 就绪后才能判定运行状态
  await initLive();
  await loadChatThreads();
  loadChatModels();
  const params = new URLSearchParams(location.search);
  const runParam = params.get("run");
  const param = params.get("trace");
  // #/audit 深链优先:审计面自成一体,不依赖 runHistory 就绪。
  if (auditHashTarget(location.hash) !== null) {
    renderMode("audit");
    document.body.dataset.booted = "1";
    return;
  }
  if (runParam) {
    // 运行直链：运行中 → 接入 SSE；已结束 → 拉 trace 回放。
    // selectRun 需要 runHistory 判定状态，先等一次运行列表。
    const ticket = viewGate.begin();
    renderMain(`<div class="empty">加载运行 ${esc(runParam)}…</div>`, { mode: "loading" });
    refreshRuns().then(() => {
      if (ticket.isCurrent()) selectRun(runParam);
    }).catch(() => {
      if (!ticket.isCurrent()) return;
      renderMain(`<div class="empty">无法加载运行 ${esc(runParam)}：运行不存在或已删除</div>`, { mode: "error" });
    });
  } else if (param) {
    const ticket = viewGate.begin();
    fetch(param, { cache: "no-store" })
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(trace => {
        if (!ticket.isCurrent()) return;
        render(trace, param.split("/").pop());
      })
      .catch(error => {
        if (!ticket.isCurrent()) return;
        renderMain(`<div class="empty">无法加载 ${esc(param)}：${esc(error.message)}</div>`, { mode: "error" });
      });
  } else {
    newChat();
    setUiMode();
  }
  // boot 完成标记：以上 await（initLive/loadChatThreads）之后的首页渲染是
  // 视图定稿点——在此之前进入的任何视图都会被这里的 newChat 打回首页。
  // E2E（与外部集成）用它判定"可以开始交互"。
  document.body.dataset.booted = "1";
});

/* #/audit 深链的统一入口:chat 完成卡片的链接、地址栏直达、前进后退都走这里。
   不在审计面 → 进审计面(renderAudit 内部按 hash 预选包);已在审计面 →
   只切换选中的包。选包的 replaceState 不触发本监听(无 hashchange 事件)。 */
window.addEventListener("hashchange", () => {
  const target = auditHashTarget(location.hash);
  if (target === null) return;
  if (store.mode !== "audit") { renderMode("audit"); return; }
  if (!target) return;
  const current = document.querySelector("#at-bundles .at-bitem.at-sel")
    ?.getAttribute("data-dir") || "";
  if (target === current) return;
  const item = document.querySelector(
    `#at-bundles .at-bitem[data-dir="${CSS.escape(target)}"]`);
  if (item) item.click();
  else healAuditDeepLink();
});

