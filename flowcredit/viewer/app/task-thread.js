"use strict";

/* 统一任务线程：把一份 trace（实时快照、历史运行、普通项目、full 研究都算）
   投影成一条按时间排列的线程，再把条目渲染成同一套 DOM。

   这一层是纯函数：不读全局、不连 SSE、不改 trace、不发起请求。
   app.js 只负责把同样的 live 状态喂进来，并把变化的条目打补丁进 DOM。
   —— 信息层级：用户任务 → Agent 状态／计划 → 工具与审批 → 最终回复。 */

import { esc, safeUrl, num } from "../core/format.js";
import { aiAvatarHtml } from "../core/ai-avatar.js";
import { renderMarkdown } from "../core/chat-format.js";

/* ------------------------------------------------------------------ 常量 */

const TERMINAL_LABEL = {
  succeeded: ["succeeded", "已完成", "ok"],
  failed: ["failed", "运行失败", "bad"],
  stopped: ["stopped", "已停止", "warn"],
  denied: ["failed", "已拒绝", "bad"],
};

/* The compact status line speaks product language; the raw code stays on the
   error item below, where it is still available for debugging. */
const FAILURE_HINT = {
  PLAN_SCHEMA_INVALID: "模型返回的计划结构不合法",
  PLAN_ADDITION_INVALID: "计划扩展未通过校验",
  BUDGET_EXCEEDED: "预算已用尽",
  ROUND_LIMIT: "轮次已用尽",
  STOPPED_BY_OPERATOR: "操作员停止了运行",
  MODEL_OUTCOME_UNKNOWN: "模型结果未知，可对账后继续",
  TOOL_OUTCOME_UNKNOWN: "工具结果未知",
  TOOL_NOT_IN_TASK_SCOPE: "工具不在本任务授权范围",
  NO_PROGRESS: "连续多轮没有推进",
  REPAIR_BUDGET_EXCEEDED: "修复次数用尽",
  NO_TOOL_CALL: "模型没有产生工具调用",
  EMPTY_CONTENT: "模型返回了空内容",
  INVALID_TOOL_CALL: "模型返回了不合法的工具调用",
  ACTION_REJECTED: "动作未通过阶段门禁",
  ARTIFACT_CHECK_FAILED: "产物检查未通过",
  BUDGET_EXHAUSTED_REASONING: "模型推理占满预算，未产生动作",
  BUDGET_EXHAUSTED_ACTION: "有效动作预算用尽",
  ACTION_REPAIR_EXHAUSTED_NO_VALID: "修复次数用尽，仍未产生有效动作",
  ACTION_REPAIR_EXHAUSTED_PARTIAL: "修复次数用尽，产物仍未通过",
};

const PLAN_GLYPH = { done: "✓", blocked: "✕", abandoned: "–", active: "◐", pending: "○" };

/* The API controller and the renderer must use the same decision identity.
 * Keeping its construction here lets tests cover the exact object dispatched
 * by decideApproval(), rather than inventing a different fixture shape. */
export function decisionForPending(pending, allow, remember = false) {
  return {
    allow: !!allow,
    remember: !!remember,
    pendingId: String((pending || {}).id || ""),
  };
}

const TOOL_STATUS = {
  succeeded: ["succeeded", "完成", "ok"],
  running: ["running", "执行中", "live"],
  waiting: ["waiting", "等待", "pending"],
  failed: ["failed", "失败", "bad"],
  denied: ["failed", "已拒绝", "bad"],
  stopped: ["stopped", "已停止", "warn"],
  unknown: ["unknown", "未知", "warn"],
};

/* 工具图标：只画形状，不引入品牌资源。未知工具回退到终端方块。 */
const GLYPHS = {
  terminal: '<path d="M3 4h18v16H3z"/><path d="m7 10 2.5 2L7 14M12 14h5"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  pencil: '<path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="m13.5 6.5 3 3"/>',
  play: '<circle cx="12" cy="12" r="8.5"/><path d="m10 8.5 5 3.5-5 3.5z"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.1-3.6-8.5S9.6 5.8 12 3.5z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 4.5 4.5"/>',
  calc: '<path d="M19 5 5 19"/><circle cx="7.5" cy="7.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/>',
  table: '<path d="M3 5h18v14H3z"/><path d="M3 10h18M9 10v9"/>',
  branch: '<circle cx="6" cy="6" r="2.4"/><circle cx="6" cy="18" r="2.4"/><circle cx="18" cy="9" r="2.4"/><path d="M6 8.4v7.2M6 12h6a4 4 0 0 0 4-4v-.6"/>',
  shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.1 7.7 7.5 9.5 4.4-1.8 7.5-4.9 7.5-9.5V6z"/><path d="m8.5 12 2.5 2.5 4.5-4.5"/>',
  alert: '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4M12 17h.01"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  message: '<path d="M4 5h16v11H9l-5 4z"/>',
  plan: '<path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
  dot: '<circle cx="12" cy="12" r="3.2"/>',
};

const TOOL_GLYPH = {
  read_document: "file", read_file: "file", read_file_raw: "file",
  write_file: "pencil", edit_file: "pencil",
  list_documents: "folder", ls_workspace: "folder",
  fetch_url: "globe", collect_source: "globe", fetch_source: "globe",
  search_corpus: "search", search_cases: "search", query_facts: "search", read_case: "file",
  search_codebase: "search", grep_workspace: "search", repo_map: "folder",
  calculate_margin: "calc", normalize_units: "table", reconcile_basis: "branch",
  check_restatement: "branch", run_command: "terminal", working_paper_append: "file",
  write_document: "file", read_slice: "file", report_blocked: "shield",
  verify: "check", motion_check: "play",
  ask_operator: "message", ingest_file: "file",
};

function icon(name, cls = "th-glyph") {
  const body = GLYPHS[name] || GLYPHS.dot;
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
}

function glyphForTool(tool) {
  return icon(TOOL_GLYPH[tool] || "terminal");
}

/* ------------------------------------------------------------- 纯投影工具 */

export function capabilityArtifact(trace) {
  /* 能力运行时的交付物：从 capability_succeeded 事件取钉住的 artifact
     （path/bytes/sha256）。交付卡只认链上事件，不猜工作区。 */
  const events = (trace && trace.events) || [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event || event.type !== "capability_succeeded") continue;
    const detail = (event.payload && event.payload.detail) || {};
    const artifact = detail.artifact || null;
    if (!artifact || !artifact.path) continue;
    return {
      kind: String(detail.kind || ""),
      path: String(artifact.path),
      bytes: Number(artifact.bytes || 0),
      sha256: String(artifact.sha256 || ""),
    };
  }
  return null;
}

export function capabilityFailure(trace) {
  /* 能力运行时的失败诊断：从最后一条 capability_failed 事件取宿主检查
     产出的 typed facts（stage/reason/quality/required_fixes/artifact）。
     与 capabilityArtifact 同一纪律：只认链上事件，不猜工作区。 */
  const events = (trace && trace.events) || [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event || event.type !== "capability_failed") continue;
    const detail = (event.payload && event.payload.detail) || {};
    const artifact = detail.artifact && detail.artifact.path ? {
      path: String(detail.artifact.path),
      bytes: Number(detail.artifact.bytes || 0),
      sha256: String(detail.artifact.sha256 || ""),
    } : null;
    const diag = {
      kind: String(detail.kind || ""),
      stage: String(detail.stage || ""),
      reason: String(detail.reason || ""),
      quality: detail.quality && typeof detail.quality === "object" ? detail.quality : null,
      requiredFixes: Array.isArray(detail.required_fixes) ? detail.required_fixes : [],
      artifact,
    };
    // 没有任何可展示事实的事件（只有 reason/legacy_reason）不值得一张卡。
    if (!diag.stage && !diag.requiredFixes.length && !diag.artifact && !diag.quality) return null;
    return diag;
  }
  return null;
}

const DIAGNOSTIC_QUALITY_KEYS = [
  ["score", "质量分"], ["composition_ok", "构图"], ["coverage_complete", "覆盖完整"],
];

export function capabilityFailureHtml(diag) {
  /* A3 失败诊断卡：宿主检查的 typed facts，中文为主，原始 code 作为
     小号 mono 证据。没有任何诊断事实时返回空串（不渲染空段落）。 */
  if (!diag) return "";
  const rows = [];
  if (diag.stage) rows.push(`<span class="fdiag-item"><span class="fdiag-k">阶段</span><code>${esc(diag.stage)}</code></span>`);
  const fixes = (diag.requiredFixes || []).slice(0, 4).map(fix => {
    const parts = [];
    if (fix.code) parts.push(`<code>${esc(String(fix.code))}</code>`);
    if (fix.expected !== undefined && fix.expected !== null) parts.push(`预期 <code>${esc(String(fix.expected))}</code>`);
    if (fix.actual !== undefined && fix.actual !== null) parts.push(`实际 <code>${esc(String(fix.actual))}</code>`);
    if (fix.selector) parts.push(`<code>${esc(String(fix.selector))}</code>`);
    const head = parts.join(" · ");
    const msg = fix.message ? `<div class="fdiag-msg">${esc(String(fix.message))}</div>` : "";
    return `<li>${head}${msg}</li>`;
  }).join("");
  if (fixes) rows.push(`<span class="fdiag-item"><span class="fdiag-k">该修什么</span><ul class="fdiag-fixes">${fixes}</ul></span>`);
  const quality = diag.quality || null;
  if (quality) {
    const facts = [];
    for (const [key, label] of DIAGNOSTIC_QUALITY_KEYS) {
      const value = quality[key];
      if (value === undefined || value === null) continue;
      facts.push(`${label} ${esc(String(value))}`);
    }
    const issues = Array.isArray(quality.issues) ? quality.issues.slice(0, 3)
      .map(issue => typeof issue === "string" ? issue : String((issue && issue.message) || "")).filter(Boolean) : [];
    if (facts.length) rows.push(`<span class="fdiag-item"><span class="fdiag-k">质量实测</span>${facts.map(f => `<code>${f}</code>`).join(" ")}</span>`);
    if (issues.length) rows.push(`<span class="fdiag-item"><span class="fdiag-k">问题</span><ul class="fdiag-fixes">${issues.map(i => `<li>${esc(i)}</li>`).join("")}</ul></span>`);
  }
  if (diag.artifact) {
    const kb = diag.artifact.bytes >= 1024
      ? `${(diag.artifact.bytes / 1024).toFixed(1)} KB`
      : `${diag.artifact.bytes} B`;
    rows.push(`<span class="fdiag-item"><span class="fdiag-k">产物</span><code>${esc(diag.artifact.path)}</code> <span class="fdiag-dim">${kb} · ${esc(String(diag.artifact.sha256 || "").slice(0, 16))}…</span></span>`);
  }
  if (!rows.length) return "";
  return `<div class="fdiag" aria-label="失败诊断">
    <div class="fdiag-head">失败诊断 <span class="fdiag-src">宿主检查 · 链上事件</span></div>
    ${rows.map(row => `<div class="fdiag-row">${row}</div>`).join("")}
  </div>`;
}

export function lastBudget(trace) {
  const rounds = (trace && trace.rounds) || [];
  for (let i = rounds.length - 1; i >= 0; i--) {
    const budget = rounds[i].budget_after;
    if (budget && budget.calls_used !== undefined) return budget;
  }
  return null;
}

export function lastPlan(trace) {
  const rounds = (trace && trace.rounds) || [];
  for (let i = rounds.length - 1; i >= 0; i--) {
    if (rounds[i].plan_after) return rounds[i].plan_after;
    if (rounds[i].plan) return rounds[i].plan;
  }
  return null;
}

export function isRoundEmpty(round) {
  return !round.proposal && !round.decision && !round.observation && !round.receipt;
}

/* 未知结果只在“空轮次 + model_unknown”且随后有匹配的 model_reconciled 时对账；
   与 app.js 原来的注释语义一致，只是搬进了纯函数，方便单测。 */
export function annotateRounds(trace) {
  const rounds = (trace && trace.rounds) || [];
  const events = (trace && trace.events) || [];
  const unknowns = events.filter(event => event.type === "model_unknown");
  const reconciles = events.filter(event => event.type === "model_reconciled");
  const annotations = [];
  let u = 0, r = 0, pendingIndex = null;
  for (const round of rounds) {
    const ann = {};
    if (isRoundEmpty(round) && u < unknowns.length) {
      const event = unknowns[u++];
      ann.unknown = ((event.payload || {}).detail || {}).cause || "UNKNOWN";
      pendingIndex = round.index;
    } else if (!isRoundEmpty(round) && pendingIndex !== null && pendingIndex === round.index && r < reconciles.length) {
      const event = reconciles[r++];
      ann.reconciled = ((event.payload || {}).detail || {}).receipt_ref || "";
      pendingIndex = null;
    } else if (!isRoundEmpty(round) && pendingIndex !== null && pendingIndex !== round.index) {
      pendingIndex = null;
    }
    annotations.push(ann);
  }
  const terminal = ((trace || {}).terminal || {}).status || "";
  if (terminal && rounds.length) {
    const index = rounds.length - 1;
    if (isRoundEmpty(rounds[index]) && annotations[index] && !annotations[index].unknown) {
      annotations[index].terminal = terminal;
    }
  }
  return annotations;
}

/* 工具调用的真实结局。没有 observation 就说“等待/停止/未知”，绝不补造结果。 */
export function toolOutcome(round, ann = {}, live = {}) {
  const observation = round.observation || null;
  const decision = round.decision || {};
  if (ann.unknown) return { status: "unknown", code: ann.unknown, message: "模型调用结果未知，等待对账", denied: false };
  if (decision.allowed === false) {
    return {
      status: "denied", denied: true,
      code: decision.code || "DENIED",
      message: decision.reason || "主机门控拒绝了这次调用",
    };
  }
  if (!observation) {
    const finished = !!(live.finished || (live.trace && live.trace.terminal));
    if (!finished) return { status: "waiting", denied: false, code: "", message: "等待工具返回" };
    const terminal = terminalKind(live.terminal || {});
    if (terminal === "stopped") return { status: "stopped", denied: false, code: "STOPPED_BY_OPERATOR", message: "运行在工具返回前被停止" };
    if (terminal === "failed") return { status: "failed", denied: false, code: "", message: "运行在工具返回前失败" };
    return { status: "unknown", denied: false, code: "", message: "没有留下这次调用的结果" };
  }
  const result = observation.result || {};
  const failure = observation.failure || result.failure || observation.error || result.error;
  if (failure) {
    return {
      status: "failed", denied: false,
      code: String(failure.code || failure.message || failure).slice(0, 40),
      message: String(failure.message || ""),
    };
  }
  if (observation.blocked || result.blocked) {
    return { status: "failed", denied: false, code: "BLOCKED", message: String(result.reason || "") };
  }
  if (result.computed === false || result.reconciled === false) {
    return { status: "failed", denied: false, code: "NO_RESULT", message: String(result.reason || (result.blockers || []).join("；")) };
  }
  if (result.exit_code !== undefined && Number(result.exit_code) !== 0) {
    return { status: "failed", denied: false, code: `EXIT_${result.exit_code}`, message: "" };
  }
  return { status: "succeeded", denied: false, code: "", message: "" };
}

/* 审批事件的已决记录：让历史重开与实时线程拥有同一份“这里发生过一次审批”。 */
function resolvedApprovals(events) {
  const out = [];
  let current = null;
  for (const event of events || []) {
    const detail = (event.payload || {}).detail || {};
    if (event.type === "approval_requested") {
      current = {
        id: String(detail.id || `req-${event.sequence}`),
        round: Number(detail.round) || 0,
        tool: String(detail.tool || ""),
        policy: String(detail.policy || ""),
        requestedSeq: event.sequence,
      };
    } else if (event.type === "approval_granted" || event.type === "approval_denied" || event.type === "approval_aborted") {
      const id = current ? current.id : String(detail.approval_id || `req-${event.sequence}`);
      out.push({
        id,
        key: `approval:${id}`,
        round: current ? current.round : Number(detail.round) || 0,
        tool: current ? current.tool : String(detail.tool || ""),
        by: String(detail.by || ""),
        code: String(detail.code || ""),
        remember: !!detail.remember,
        outcome: event.type === "approval_granted" ? "granted"
          : event.type === "approval_denied" ? "denied" : "aborted",
        seq: current ? current.requestedSeq : event.sequence,
      });
      current = null;
    }
  }
  return { resolved: out, unresolved: current };
}

function operatorMessages(trace) {
  const byRound = new Map();
  for (const event of (trace && trace.events) || []) {
    if (event.type !== "operator_message") continue;
    const detail = (event.payload || {}).detail || {};
    const round = Number(detail.round) || 0;
    if (!byRound.has(round)) byRound.set(round, []);
    byRound.get(round).push({
      key: `op:${event.sequence}`,
      seq: event.sequence,
      round,
      text: String(detail.text || ""),
      steered: !!detail.steered,
    });
  }
  return byRound;
}

function planTodos(plan) {
  return ((plan && plan.todos) || []).map(todo => ({
    id: String(todo.id || ""),
    text: String(todo.text || todo.id || ""),
    status: String(todo.status || "pending"),
    tool: String(todo.tool || ""),
    reason: String(todo.reason || ""),
  }));
}

/* ---------------------------------------------------------------- 状态投影 */

function terminalKind(terminal) {
  const status = String((terminal || {}).status || "");
  const code = String(((terminal || {}).failure || {}).code || "");
  // A stopped run is recorded as failed + STOPPED_BY_OPERATOR by the runtime;
  // the thread must say "stopped", not "failed".
  if (status === "stopped" || code === "STOPPED_BY_OPERATOR") return "stopped";
  return status;
}

/* 一条运行的耗时就是链上首末事件之差：终态取末事件，运行中取当前时刻。
   事件时间戳是 UTC ISO，Date.parse 对秒级精度不会丢；解析失败的条目跳过，
   宁可没有耗时，也不拿本地时钟和链上时间混算。 */
export function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.round(Number(milliseconds) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${String(seconds % 60).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${String(minutes % 60).padStart(2, "0")}m`;
}

export function elapsedLabel(events, terminal, now = 0) {
  const stamps = (events || [])
    .map(entry => Date.parse(String((entry && entry.at) || "")))
    .filter(Number.isFinite);
  if (!stamps.length) return "";
  const started = stamps[0];
  const finished = terminal && terminal.status;
  const ended = finished ? stamps[stamps.length - 1] : Number(now || stamps[stamps.length - 1]);
  if (!Number.isFinite(ended) || ended < started) return "";
  return formatElapsed(ended - started);
}

export function threadStatus(trace, live = {}) {
  const terminal = (trace && trace.terminal) || (live.terminal || {});
  const rounds = (trace && trace.rounds) || [];
  const pending = live.pending || null;
  if (pending) {
    const question = pending.kind === "question";
    return {
      state: "waiting",
      label: question ? "等待你的回答" : "等待你的授权",
      tone: "warn",
      detail: question
        ? `第 ${pending.round || "?"} 轮 · 模型提出了一个问题`
        : `第 ${pending.round || "?"} 轮 · ${pending.tool || "工具调用"} 需要确认`,
    };
  }
  const kind = terminalKind(terminal);
  const known = TERMINAL_LABEL[kind];
  if (known) {
    const [state, label, tone] = known;
    const failure = terminal.failure || {};
    return {
      state, label, tone,
      detail: kind === "succeeded"
        ? `${rounds.length} 轮 · ${((trace && trace.events) || []).length} 个事件`
        : String(failure.code ? (FAILURE_HINT[failure.code] || failure.code) : ""),
    };
  }
  if (!trace) {
    return { state: live.submitted === false ? "unknown" : "running", label: live.submitted === false ? "状态未知" : "启动中", tone: live.submitted === false ? "warn" : "live", detail: "等待 Runtime 的第一个快照" };
  }
  if (live.error) {
    return { state: "unknown", label: "状态未知", tone: "warn", detail: String(live.error) };
  }
  if (live.running) {
    const lastRound = rounds[rounds.length - 1];
    const waitingTool = lastRound && lastRound.proposal && !lastRound.observation
      && (lastRound.decision || {}).allowed !== false;
    if (!rounds.length) return { state: "running", label: "正在准备", tone: "live", detail: "还没有第一个轮次" };
    if (waitingTool) {
      const tool = (((lastRound.proposal || {}).steps || [])[0] || {}).tool || "";
      return { state: "running", label: "正在执行", tone: "live", detail: `第 ${lastRound.index} 轮${tool ? " · " + tool : ""}` };
    }
    return { state: "running", label: "运行中", tone: "live", detail: `已完成 ${rounds.length} 轮` };
  }
  return { state: "unknown", label: "状态未知", tone: "warn", detail: live.error || "没有终态记录" };
}

/* ---------------------------------------------------------------- 主投影 */

/**
 * @param {object|null} trace 运行快照（可为 null：还没有第一帧）
 * @param {object} live { running, finished, pending, deciding, answering, error, terminal }
 * @param {object} options { annotations, planEditing, planConfirm, planQueued, summaryOf,
 *                           answeredMessage, answerDetailHtml, answerActionsHtml, ... }
 * @returns {{items: Array, meta: object}}
 */
export function projectTaskThread(trace, live = {}, options = {}) {
  const source = trace || {};
  const rounds = source.rounds || [];
  const events = source.events || [];
  const annotations = options.annotations || annotateRounds(source);
  const terminal = source.terminal || live.terminal || {};
  const terminalUnsuccessful = !!terminal.status && terminal.status !== "succeeded";
  const items = [];

  // Host verification is a real, successful step, but it is not a model tool
  // observation. Without this map the final verification round (decision only,
  // observation:null) is projected as a red "unknown" row even when the run
  // succeeded and the command exited 0.
  const verificationRounds = new Map();
  for (const event of events) {
    if (event.type !== "verification_result") continue;
    const detail = (event.payload || {}).detail || {};
    if (detail.round !== undefined) verificationRounds.set(Number(detail.round), detail);
  }

  if (!source.task && !rounds.length && !events.length && !terminal.status) {
    return { items, meta: { status: "unknown", terminal: "", rounds: 0, events: 0, budget: null, plan: null, hasPending: !!live.pending } };
  }

  const objective = String((source.task || {}).objective || "").trim();
  if (objective) {
    // 对话不做分类标签：用户消息就是一句话，项目/场景在上面上下文行里。
    items.push({ kind: "user", key: "user:objective", text: objective, chips: [] });
  }

  const status = threadStatus(source, Object.assign({}, live, { terminal }));
  const elapsed = elapsedLabel(events, terminal, live.now);
  const budget = lastBudget(source);
  const plan = lastPlan(source);
  items.push({
    kind: "status",
    key: "status",
    state: status.state,
    label: status.label,
    tone: status.tone,
    detail: status.detail,
    elapsed,
    rounds: rounds.length,
    events: events.length,
    budget: budget ? {
      calls: Number(budget.calls_used || 0),
      maxCalls: Number(((budget.limits || {}).max_calls) || 0),
      cost: Number(budget.cost_used_microusd || 0),
      maxCost: Number(((budget.limits || {}).max_cost_microusd) || 0),
      tokens: Number(budget.tokens_used || 0),
      maxTokens: Number(((budget.limits || {}).max_tokens) || 0),
    } : null,
    budgetHtml: options.budgetHtml || "",
    artifactHtml: options.statusArtifactHtml ? String(options.statusArtifactHtml(source) || "") : "",
  });

  if (plan && (plan.todos || []).length) {
    const todos = planTodos(plan);
    items.push({
      kind: "plan",
      key: "plan",
      revision: plan.revision,
      todos,
      done: todos.filter(todo => todo.status === "done").length,
      total: todos.length,
      editing: !!options.planEditing,
      failed: !!terminal.status && terminal.status !== "succeeded",
      confirmId: options.planConfirm || null,
      queued: options.planQueued || "",
      editable: options.planEditable !== false,
    });
  }

  const approvals = resolvedApprovals(events);
  const pendingId = live.pending ? String(live.pending.id || "") : "";
  const resolvedByRound = new Map();
  for (const approval of approvals.resolved) {
    if (!resolvedByRound.has(approval.round)) resolvedByRound.set(approval.round, []);
    resolvedByRound.get(approval.round).push(approval);
  }
  const opsByRound = operatorMessages(source);
  const pending = live.pending || null;
  let pendingPlaced = false;
  const pushPending = roundIndex => {
    if (!pending || pendingPlaced) return;
    if (pending.round !== undefined && Number(pending.round) !== Number(roundIndex)) return;
    pendingPlaced = true;
    items.push({
      kind: "approval",
      key: `pending:${String(pending.id || "current")}`,
      resolved: false,
      pending,
      deciding: live.deciding || null,
      answering: !!live.answering,
    });
  };

  /* 一个轮次在线程里的顺序：操作员消息 → 已决审批 → 工具调用；
     等待中的审批落在它所属轮次的位置（没有轮次就落在时间线末尾）。 */
  const pushRound = round => {
    for (const message of opsByRound.get(round) || []) {
      items.push({ kind: "operator", key: message.key, text: message.text, round: message.round, steered: message.steered });
    }
    for (const approval of resolvedByRound.get(round) || []) {
      items.push({
        kind: "approval",
        key: approval.key,
        resolved: true,
        outcome: approval.outcome,
        tool: approval.tool,
        by: approval.by,
        code: approval.code,
        remember: approval.remember,
        round: approval.round,
      });
    }
    pushPending(round);
  };

  pushRound(0);

  for (let i = 0; i < rounds.length; i++) {
    const round = rounds[i];
    const ann = annotations[i] || {};
    pushRound(round.index);
    const verification = verificationRounds.get(Number(round.index));
    if (verification && !round.observation && !round.proposal) {
      const expected = Number(verification.expect_exit_code ?? 0);
      const ok = verification.verified !== false && Number(verification.exit_code) === expected;
      const status = ok ? "succeeded" : "failed";
      items.push({
        kind: "tool",
        key: `tool:${round.index}`,
        index: round.index,
        tool: "独立校验",
        args: {},
        status,
        tone: TOOL_STATUS[status][2],
        label: status === "succeeded" && terminalUnsuccessful ? "调用成功" : TOOL_STATUS[status][1],
        code: ok ? "" : `EXIT_${verification.exit_code}`,
        message: "",
        denied: false,
        summary: `${verification.name || "项目验证"} · 退出码 ${verification.exit_code}`,
        observation: null,
        decision: round.decision || {},
        proposal: null,
        receipt: null,
        planUpdate: null,
        blockKind: "brief",
        detailHtml: "",
        hostVerification: true,
      });
      continue;
    }
    if (!isRoundEmpty(round)) {
      const observation = round.observation || null;
      const decision = round.decision || {};
      const step = ((round.proposal || {}).steps || [])[0] || {};
      const tool = (observation && observation.tool) || decision.tool || step.tool || "";
      const args = (observation && observation.arguments) || step.arguments || {};
      const outcome = toolOutcome(round, ann, {
        finished: live.finished || !!terminal.status,
        terminal,
      });
      items.push({
        kind: "tool",
        key: `tool:${round.index}`,
        index: round.index,
        tool: tool || "工具调用",
        args,
        status: outcome.status,
        tone: (TOOL_STATUS[outcome.status] || TOOL_STATUS.unknown)[2],
        label: outcome.status === "succeeded" && terminalUnsuccessful
          ? "调用成功" : (TOOL_STATUS[outcome.status] || TOOL_STATUS.unknown)[1],
        code: outcome.code,
        message: outcome.message,
        denied: outcome.denied,
        summary: options.summaryOf ? String(options.summaryOf(observation || {}, round) || "") : "",
        observation,
        decision,
        proposal: round.proposal || null,
        receipt: round.receipt || null,
        planUpdate: planDelta(round, i > 0 ? rounds[i - 1] : null),
        blockKind: options.blockKindOf ? options.blockKindOf(round) : "brief",
        detailHtml: "",
      });
    } else if (ann.unknown) {
      items.push({
        kind: "tool",
        key: `tool:${round.index}`,
        index: round.index,
        tool: "模型调用",
        args: {},
        status: "unknown",
        tone: "warn",
        label: "未知",
        code: ann.unknown,
        message: "未知不等于失败：不自动重发，等待签名收据显式对账。",
        denied: false,
        summary: "",
        observation: null,
        decision: null,
        proposal: null,
        receipt: null,
        planUpdate: null,
        blockKind: "brief",
        detailHtml: "",
      });
    }
  }

  if (pending && !pendingPlaced) {
    items.push({
      kind: "approval",
      key: `pending:${String(pending.id || "current")}`,
      resolved: false,
      pending,
      deciding: live.deciding || null,
      answering: !!live.answering,
    });
    pendingPlaced = true;
  }
  if (!pending && approvals.unresolved && approvals.unresolved.id !== pendingId) {
    // 请求已写进链，但没有决议事件（进程在某处停下）。如实标注，不补造。
    items.push({
      kind: "approval",
      key: `approval:${approvals.unresolved.id}`,
      resolved: true,
      outcome: "unresolved",
      tool: approvals.unresolved.tool,
      by: "",
      code: "",
      round: approvals.unresolved.round,
    });
  }

  const failure = terminal.failure || {};
  const stopped = terminalKind(terminal) === "stopped";
  if (terminal.status && terminal.status !== "succeeded") {
    const code = String(failure.code || "").toUpperCase();
    items.push({
      kind: "error",
      key: "error",
      status: stopped ? "stopped" : String(terminal.status),
      tone: stopped ? "warn" : "bad",
      code: code || String(terminal.status).toUpperCase(),
      message: String(failure.message || ""),
      text: options.failureText ? String(options.failureText(terminal) || "") : "",
      diagnosticsHtml: options.failureDiagnosticsHtml
        ? String(options.failureDiagnosticsHtml(source) || "")
        : "",
      detailHtml: options.failureDetailHtml ? String(options.failureDetailHtml(source) || "") : "",
      actionsHtml: options.answerActionsHtml ? String(options.answerActionsHtml(source) || "") : "",
      recoverHtml: recoveryActionHtml(code),
    });
  } else if (terminal.status === "succeeded") {
    const result = terminal.result || {};
    const facts = [];
    if (result.verified === true) facts.push("Runtime 独立校验");
    const citations = (result.citations || []).length;
    if (citations) facts.push(`${citations} 处引用`);
    // 产物通过独立校验，但报告里有无处可查的引用：运行仍算完成，结论
    // 按“未采信”处理，警告必须出现在最终回复上而不是埋在证据栏里。
    const unresolved = (result.evidence_unresolved || []).length;
    if (unresolved) facts.push(`${unresolved} 处引用未核实`);
    items.push({
      kind: "answer",
      key: "answer",
      status: "succeeded",
      warning: unresolved,
      text: options.answerText
        ? String(options.answerText(source) || "")
        : defaultAnswerText(source),
      detailTitle: options.answerDetailTitle
        ? String(options.answerDetailTitle(source) || "结构化结果")
        : "结构化结果",
      detailHtml: options.answerDetailHtml ? String(options.answerDetailHtml(source) || "") : "",
      deliveryHtml: options.answerDeliveryHtml ? String(options.answerDeliveryHtml(source) || "") : "",
      actionsHtml: options.answerActionsHtml ? String(options.answerActionsHtml(source) || "") : "",
      facts,
      result,
    });
  } else if (live.error && !live.running) {
    items.push({
      kind: "error",
      key: "error",
      status: "unknown",
      tone: "warn",
      code: "RUN_SNAPSHOT_UNAVAILABLE",
      message: String(live.error),
      text: "运行没有留下可读快照；状态未知，不按失败处理。",
      actionsHtml: "",
    });
  }

  // 终态运行都把工具调用收进摘要：工具返回成功不代表计划或产物已验收。
  if (terminal.status === "succeeded" || terminal.status === "failed") {
    const firstTool = items.findIndex(item => item.kind === "tool");
    const toolCount = items.filter(item => item.kind === "tool").length;
    if (firstTool >= 0) {
      const failed = terminal.status === "failed";
      items.splice(firstTool, 0, {
        kind: "process", key: "process", count: toolCount, collapsed: true,
        label: failed ? "执行摘要" : "执行过程",
        countLabel: failed ? `${toolCount} 次调用` : `${toolCount} 个工具`,
      });
    }
  }

  return {
    items,
    meta: {
      status: status.state,
      terminal: terminalKind(terminal),
      rounds: rounds.length,
      events: events.length,
      budget,
      plan,
      hasPending: !!pending,
    },
  };
}

function planDelta(round, previous) {
  const after = round.plan_after || round.plan || null;
  if (!after) return null;
  const before = previous ? (previous.plan_after || previous.plan) : null;
  if (!before) return null;
  if (Number(after.revision || 1) <= Number(before.revision || 1)) return null;
  const beforeMap = new Map(((before.todos) || []).map(todo => [todo.id, todo]));
  const rows = ((after.todos) || []).map(todo => {
    const prior = beforeMap.get(todo.id);
    return {
      id: String(todo.id || ""),
      text: String(todo.text || todo.id || ""),
      status: String(todo.status || "pending"),
      changed: !prior || prior.status !== todo.status || prior.text !== todo.text,
      added: !prior,
    };
  });
  if (!rows.some(row => row.changed)) return null;
  return { revision: after.revision, fromRevision: before.revision, rows };
}

function defaultAnswerText(trace) {
  const terminal = trace.terminal || {};
  const result = terminal.result || {};
  const artifacts = Array.isArray(result.results)
    ? result.results.filter(entry => entry && entry.path)
    : (result.path ? [result] : []);
  if (trace.project) {
    const count = (trace.project.filesChanged || []).length;
    return `已在隔离副本中完成修改${count ? `，共变更 ${count} 个文件` : ""}；原目录未改动。`;
  }
  if (artifacts.length) return `任务已完成；${artifacts.length} 个文件通过 Runtime 文件状态复核。`;
  if (result.change_bp !== undefined) {
    return `两期合并毛利率${result.change_bp >= 0 ? "上升" : "下降"} ${(Math.abs(result.change_bp) / 100).toFixed(2)} 个百分点。`;
  }
  return "任务已完成，结果通过 Runtime 独立校验。";
}

/* ------------------------------------------------------------------ 渲染 */

function badge(tone, label) {
  return `<span class="th-badge ${esc(tone)}"><span class="dot"></span>${esc(label)}</span>`;
}

function argsLine(args) {
  const rows = Object.entries(args || {}).filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (!rows.length) return "";
  return rows.map(([key, value]) => {
    const text = String(value);
    return `<span class="th-arg"><span class="k">${esc(key)}</span><span class="v">${esc(text.length > 180 ? text.slice(0, 180) + "…" : text)}</span></span>`;
  }).join("");
}

function commandOf(item) {
  const args = item.args || {};
  const result = (item.observation || {}).result || {};
  if (typeof result.command === "string" && result.command) return result.command;
  if (typeof args.command === "string" && args.command) {
    const rest = Array.isArray(args.args) ? args.args.map(String) : [];
    return [args.command, ...rest].join(" ");
  }
  return "";
}

function diffOf(item) {
  const result = (item.observation || {}).result || {};
  const diff = result.diff || result.patch || "";
  if (!diff) return null;
  return { text: String(diff), truncated: !!result.truncated };
}

function filesOf(item) {
  const result = (item.observation || {}).result || {};
  const files = result.files || result.changes || [];
  if (!Array.isArray(files) || !files.length) return null;
  return files.map(file => ({
    status: String(file.status || file.change || "M").slice(0, 3),
    path: String(file.path || file.name || ""),
    additions: file.additions === undefined ? null : Number(file.additions),
    removals: file.removals === undefined ? null : Number(file.removals),
  }));
}

function toolBodyHtml(item, ctx) {
  const sections = [];
  const command = commandOf(item);
  if (command) sections.push(`<pre class="th-code" aria-label="命令">${esc(command)}</pre>`);
  const args = argsLine(item.args);
  if (args) sections.push(`<div class="th-args">${args}</div>`);
  const files = filesOf(item);
  if (files) {
    sections.push(`<div class="th-files">${files.map(file => `<div class="th-file"><span class="s">${esc(file.status)}</span><span class="p">${esc(file.path)}</span>${file.additions !== null ? `<span class="n">+${num(file.additions)} / −${num(file.removals || 0)}</span>` : ""}</div>`).join("")}</div>`);
  }
  const diff = diffOf(item);
  if (diff) {
    const lines = diff.text.split("\n").slice(0, 800).map(line => {
      const kind = line.startsWith("+") && !line.startsWith("+++") ? "add"
        : line.startsWith("-") && !line.startsWith("---") ? "del"
          : line.startsWith("@@") ? "hunk" : "ctx";
      return `<div class="th-diff-line ${kind}">${esc(line) || " "}</div>`;
    }).join("");
    sections.push(`<div class="th-diff" role="group" aria-label="差异">${lines}</div>`);
  }
  const browserResult = item.observation && item.observation.result;
  const browserShots = browserResult ? ["before", "after"]
    .map((key, index) => ({ key, shot: browserResult[key], label: index === 0 ? "操作前" : "操作后" }))
    .filter(entry => entry.shot && entry.shot.url) : [];
  if (!browserShots.length && browserResult && browserResult.screenshot && browserResult.screenshot.url) {
    browserShots.push({ key: "screenshot", shot: browserResult.screenshot, label: "页面" });
  }
  if (browserShots.length) {
    const figures = browserShots.map(entry => `<figure class="th-browser-figure"><a class="th-browser-shot" href="${esc(safeUrl(entry.shot.url))}" target="_blank" rel="noreferrer"><img src="${esc(safeUrl(entry.shot.url))}" alt="浏览器截图 · ${esc(entry.label)}" loading="lazy"></a><figcaption>${esc(entry.label)}</figcaption></figure>`).join("");
    const diff = browserResult.diff || {};
    const diffText = diff.changed === undefined ? ""
      : diff.size_changed ? "截图尺寸变化"
        : diff.changed ? `截图变化 ${diff.ratio === null || diff.ratio === undefined ? "（已变化）" : (diff.ratio * 100).toFixed(2) + "%"}`
          : "截图无变化";
    const domDiff = browserResult.dom_diff
      ? `<details class="th-browser-diff"><summary>DOM 文本变化</summary><pre>${esc(browserResult.dom_diff)}</pre></details>` : "";
    sections.push(`<div class="th-browser-pair">${figures}</div><div class="th-browser-meta">${esc(browserResult.title || "")}${browserResult.title && browserResult.url ? " · " : ""}${esc(browserResult.url || "")}${diffText ? " · " + esc(diffText) : ""}</div>${domDiff}`);
  }
  if (item.detailHtml) sections.push(`<div class="th-tool-detail">${item.detailHtml}</div>`);
  if (!sections.length && item.message) sections.push(`<p class="th-note">${esc(item.message)}</p>`);
  const raw = item.observation ? {
    arguments: item.observation.arguments,
    result: item.observation.result,
    result_hash: item.observation.result_hash,
    tool_version: item.observation.tool_version,
    decision: item.decision,
    receipt: item.receipt,
  } : null;
  if (raw) {
    sections.push(`<details class="th-raw" data-keep="raw"><summary>原始返回与签名收据</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre>${item.observation.result_hash ? `<div class="th-raw-foot"><code>${esc(item.observation.result_hash)}</code><button type="button" class="th-mini" data-copy="${esc(item.observation.result_hash)}">复制</button></div>` : ""}</details>`);
  }
  return sections.join("");
}

function toolItemHtml(item, ctx) {
  const body = toolBodyHtml(item, ctx);
  const open = item.status === "waiting" || item.status === "running" ? "true" : "false";
  const summary = item.summary && item.status !== "waiting" ? item.summary : (item.message || "");
  const plan = item.planUpdate ? `<div class="th-plan-delta"><div class="th-plan-delta-head">计划更新 · rev ${esc(item.planUpdate.fromRevision)} → ${esc(item.planUpdate.revision)}</div>${item.planUpdate.rows.filter(row => row.changed).map(row => `<div class="th-plan-delta-row ${esc(row.status)}"><span>${PLAN_GLYPH[row.status] || "○"}</span><span>${esc(row.text)}</span><span class="tag">${row.added ? "新增" : "修订"}</span></div>`).join("")}</div>` : "";
  return `<article class="th-item th-tool ${esc(item.status)}" data-thread-key="${esc(item.key)}" data-open="${open}" data-thread-item="tool">
    <button type="button" class="th-tool-head" data-thread-toggle aria-expanded="${open}">
      <span class="th-ic ${esc(item.status)}">${glyphForTool(item.tool)}</span>
      <span class="th-tool-name">${esc(item.tool)}</span>
      ${summary ? `<span class="th-tool-summary">${esc(summary)}</span>` : ""}
      <span class="grow"></span>
      ${item.code && item.status !== "succeeded" ? `<code class="th-tool-code">${esc(item.code)}</code>` : ""}
      ${badge(item.tone, item.label)}
      <span class="th-chev" aria-hidden="true">▾</span>
    </button>
    <div class="th-tool-body">
      <div class="th-tool-meta"><span class="th-round-no">第 ${esc(item.index)} 轮</span>${item.receipt && item.receipt.tokens !== undefined ? `<span>· ${num(item.receipt.tokens)} tokens</span>` : ""}${item.receipt && item.receipt.cost_microusd !== undefined ? `<span>· ${num(item.receipt.cost_microusd)} µUSD</span>` : ""}</div>
      ${body || `<p class="th-note">这次调用没有返回可展示的细节。</p>`}
      ${plan}
    </div>
  </article>`;
}

function planItemHtml(item) {
  const editing = item.editing;
  const pending = Math.max(0, Number(item.total || 0) - Number(item.done || 0));
  return `<details class="th-item th-plan${editing ? " editing" : ""}" data-thread-key="plan" data-thread-item="plan"${editing ? " open" : ""}>
    <summary class="th-plan-head">
      <span class="th-ic">${icon("plan")}</span>
      <span class="th-plan-title">${item.failed ? "计划记录 · 未验收" : "计划"}</span>
      <span class="th-plan-meta">${item.failed ? `${pending} 项未完成` : `${item.done}/${item.total}`}</span>
      <span class="grow"></span>
      ${item.queued ? `<span class="th-plan-queued">${esc(item.queued)}</span>` : ""}
      ${item.editable ? `<button type="button" class="th-mini${editing ? " active" : ""}" data-plan-edit>${editing ? "完成" : "调整"}</button>` : ""}
      <span class="th-chev" aria-hidden="true">▾</span>
    </summary>
    <ol class="th-plan-list">
      ${item.todos.map(todo => `<li class="th-plan-row ${esc(todo.status)}">
        <span class="th-plan-glyph" aria-hidden="true">${PLAN_GLYPH[todo.status] || "○"}</span>
        <span class="th-plan-text">${esc(todo.text)}${todo.reason ? ` · ${esc(todo.reason)}` : ""}</span>
        ${todo.tool ? `<span class="th-plan-tool">${esc(todo.tool)}</span>` : ""}
        ${editing && (todo.status === "pending" || todo.status === "active") ? planActions(todo, item.confirmId) : ""}
      </li>`).join("")}
    </ol>
  </details>`;
}

function planActions(todo, confirmId) {
  if (confirmId === todo.id) {
    return `<span class="th-plan-act confirm"><span>跳过？</span><button type="button" data-plan-action="skip-confirm" data-todo="${esc(todo.id)}">是</button><button type="button" data-plan-action="cancel">否</button></span>`;
  }
  return `<span class="th-plan-act"><button type="button" data-plan-action="promote" data-todo="${esc(todo.id)}" title="提前：下一轮先做这一步">↑</button><button type="button" data-plan-action="skip" data-todo="${esc(todo.id)}" title="跳过：不再执行（不可撤销）">–</button></span>`;
}

function statusItemHtml(item) {
  const budget = item.budget;
  // 已完成的运行把成本/轮次留给最终回复与结果明细，顶部只留状态与耗时，
  // 避免同一条对话里三处重复预算。
  const showBudget = budget && item.state !== "succeeded" && item.state !== "failed";
  const facts = [];
  if (showBudget) {
    facts.push(`预算 ${num(budget.calls)}${budget.maxCalls ? "/" + num(budget.maxCalls) : ""} calls`);
    facts.push(`${num(budget.cost)}${budget.maxCost ? "/" + num(budget.maxCost) : ""} µUSD`);
  }
  // 失败码/未知原因还留在状态行；轮次、事件数、工具名都退到执行过程，
  // 状态行只做一件事：这一段处理了多久、结果是什么。
  const detail = item.state === "failed" || item.state === "unknown" ? item.detail : "";
  const budgetBlock = showBudget
    ? `<details class="th-budget" data-keep="budget"><summary>预算与用量</summary>${item.budgetHtml || `<div class="th-budget-rows">${[
      ["calls", budget.calls, budget.maxCalls],
      ["µUSD", budget.cost, budget.maxCost],
      ["tokens", budget.tokens, budget.maxTokens],
    ].map(([label, used, max]) => `<div class="th-budget-row"><span class="k">${esc(label)}</span><span class="v">${num(used)}${max ? "/" + num(max) : ""}</span></div>`).join("")}</div>`}</details>`
    : "";
  const avatarState = item.state === "running" ? "thinking"
    : item.state === "succeeded" ? "ok"
      : item.state === "failed" ? "bad" : "warn";
  return `<article class="th-item th-status ${esc(item.state)}" data-thread-key="status" data-thread-item="status" aria-live="polite">
    <div class="th-avatar-col">${aiAvatarHtml(avatarState, 32)}</div>
    <div class="th-status-main">
      <div class="th-agent-head"><span class="th-agent-name">FCode</span><span class="th-agent-tag">本机 Agent</span></div>
      <div class="th-status-line">
        <span class="th-status-dot" aria-hidden="true"></span>
        <span class="th-status-label">${esc(item.label)}</span>
        ${item.elapsed ? `<span class="th-status-elapsed">${esc(item.elapsed)}</span>` : ""}
        ${detail ? `<span class="th-status-detail">${esc(detail)}</span>` : ""}
        <span class="grow"></span>
        <button type="button" class="th-mini" data-rail-open>运行详情</button>
      </div>
      ${facts.length ? `<div class="th-status-facts">${facts.map(fact => `<span>${esc(fact)}</span>`).join("")}</div>` : ""}
      ${budgetBlock}
      ${item.artifactHtml || ""}
    </div>
  </article>`;
}

function userItemHtml(item) {
  return `<article class="th-item th-user" data-thread-key="${esc(item.key)}" data-thread-item="user" data-objective="1">
    ${item.chips && item.chips.length ? `<div class="th-user-head"><span class="th-who">任务</span><span class="th-chips">${item.chips.map(chip => `<span class="th-chip">${esc(chip)}</span>`).join("")}</span></div>` : ""}
    <div class="th-user-text">${esc(item.text)}</div>
  </article>`;
}

function operatorItemHtml(item) {
  return `<article class="th-item th-operator${item.steered ? " steered" : ""}" data-thread-key="${esc(item.key)}" data-thread-item="operator">
    <div class="th-user-head"><span class="th-who">你</span><span class="th-op-state">${item.steered ? `已插话 · 第 ${esc(item.round)} 轮前优先入链` : `已入链 · 第 ${esc(item.round)} 轮前生效`}</span></div>
    <div class="th-user-text">${esc(item.text)}</div>
  </article>`;
}

function approvalItemHtml(item, ctx) {
  if (item.resolved) {
    const outcome = item.outcome;
    const label = outcome === "granted" ? "已允许" : outcome === "denied" ? "已拒绝" : outcome === "aborted" ? "已中止" : "结果未记录";
    const tone = outcome === "granted" ? "ok" : outcome === "unresolved" ? "warn" : "bad";
    return `<article class="th-item th-approval resolved" data-thread-key="${esc(item.key)}" data-thread-item="approval">
      <div class="th-approval-line">
        <span class="th-ic">${icon(outcome === "granted" ? "check" : "shield")}</span>
        <span class="th-approval-title">审批 · ${esc(item.tool || "工具调用")}</span>
        ${badge(tone, label)}${item.by ? `<span class="th-approval-by">${esc(item.by)}${item.remember ? " · 本运行免询" : ""}</span>` : ""}
        ${item.code ? `<code class="th-approval-code">${esc(item.code)}</code>` : ""}
        <span class="grow"></span>
        <span class="th-approval-round">第 ${esc(item.round)} 轮</span>
      </div>
    </article>`;
  }
  const pending = item.pending || {};
  if (pending.kind === "question") {
    const busy = !!item.answering;
    // 建议答案来自同一次 tool call 的 options。点选只填入输入框，提交仍是
    // 一个普通字符串回答；模型没有给建议时，这张卡就是原来的自由输入。
    const options = Array.isArray(pending.options)
      ? pending.options.filter(value => typeof value === "string" && value.trim())
      : [];
    const optionsHtml = options.length
      ? `<div class="th-q-options" role="group" aria-label="建议回答">${options.map(option =>
        `<button type="button" class="th-q-option" data-question-option="${esc(option)}" aria-pressed="false"${busy ? " disabled" : ""}><span class="th-q-mark" aria-hidden="true"></span><span>${esc(option)}</span></button>`
      ).join("")}</div>`
      : "";
    return `<article class="th-item th-approval question" data-thread-key="${esc(item.key)}" data-thread-item="approval" data-question="${esc(pending.id || "")}">
      <div class="th-approval-head"><span class="th-ic">${icon("message")}</span><span class="th-approval-title">运行需要你确认一件事</span><span class="grow"></span>${badge(busy ? "live" : "warn", busy ? "已提交" : "等待回答")}</div>
      <p class="th-question-text">${esc(pending.question || "")}</p>
      ${pending.why ? `<p class="th-note">${esc(pending.why)}</p>` : ""}
      ${optionsHtml}
      <textarea class="th-q-input" rows="2" maxlength="600" placeholder="${options.length ? "选一项，或自己写一句（≤600 字）；空着提交等于跳过" : "回答一句即可（≤600 字）；空着提交等于跳过"}"${busy ? " disabled" : ""} data-question-input></textarea>
      <div class="th-approval-actions">
        <button type="button" class="th-btn" data-question-skip${busy ? " disabled" : ""}>跳过</button>
        <button type="button" class="th-btn primary" data-question-submit${busy ? " disabled" : ""}>提交回答</button>
      </div>
    </article>`;
  }
  const deciding = item.deciding && String(item.deciding.pendingId || item.deciding.pending_id || "") === String(pending.id || "")
    ? item.deciding
    : null;
  const write = ctx.isWriteLike ? ctx.isWriteLike(pending.tool) : false;
  const preview = ctx.approvalPreviewHtml ? String(ctx.approvalPreviewHtml(pending) || "") : "";
  const previewBlock = preview ? `<div class="th-approval-preview">${preview}</div>` : "";
  const countdown = countdownText(pending.expiresIn);
  return `<article class="th-item th-approval${write ? " write" : ""}" data-thread-key="${esc(item.key)}" data-thread-item="approval" data-approval="${esc(pending.id || "")}">
    <div class="th-approval-head">
      <span class="th-ic">${icon("shield")}</span>
      <span class="th-approval-title">${esc(ctx.approvalTitle ? ctx.approvalTitle(pending.tool) : `允许这次 ${pending.tool || "工具"} 调用？`)}</span>
      <span class="grow"></span>
      ${badge(deciding ? "live" : "warn", deciding ? "已提交" : "等待授权")}
    </div>
    <div class="th-approval-body">
      <div class="th-approval-line"><span class="th-round-no">第 ${esc(pending.round)} 轮</span><code class="th-tool-code">${esc(pending.tool || "")}</code>${argsLine(pending.arguments)}</div>
      ${previewBlock}
      <p class="th-note">${esc(ctx.approvalWhy ? ctx.approvalWhy(pending) : "关键步骤：这个动作会改变结论或宣告任务受阻，执行前需要你确认。")}</p>
      <div class="th-approval-actions" role="group" aria-label="审批选项">
        <button type="button" class="th-btn primary apv-choice-inline" data-decision="allow" aria-keyshortcuts="1"${deciding ? " disabled" : ""}><kbd class="apv-key">1</kbd><span>本次允许</span></button>
        <button type="button" class="th-btn apv-choice-inline" data-decision="allow-remember" aria-keyshortcuts="2"${deciding ? " disabled" : ""}><kbd class="apv-key">2</kbd><span>本运行允许</span></button>
        <button type="button" class="th-btn danger apv-choice-inline" data-decision="deny" aria-keyshortcuts="3"${deciding ? " disabled" : ""}><kbd class="apv-key">3</kbd><span>拒绝</span></button>
        <span class="grow"></span>
        <span class="th-approval-hint">${deciding
          ? (deciding.allow ? "已允许 · 等待 Runtime 继续…" : "已拒绝 · 运行将终止…")
          : `<span data-countdown>${esc(countdown)}</span> 后按默认处理 · 1/2/3 直选 · ⌘↵ 允许 / ⌘⌫ 拒绝`}</span>
      </div>
    </div>
  </article>`;
}

function countdownText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function answerItemHtml(item) {
  const unresolved = Number(item.warning || 0);
  return `<article class="th-item th-answer" data-thread-key="answer" data-thread-item="answer">
    <div class="th-answer-head"><span class="th-answer-kicker">最终回复</span>${unresolved
      ? badge("warn", `已完成 · ${unresolved} 处引用待核实`)
      : badge("ok", "已完成")}</div>
    <div class="th-answer-text chat-md">${renderMarkdown(item.text || "")}</div>
    ${item.deliveryHtml || ""}
    ${unresolved ? `<p class="th-note th-answer-warning">产物已通过独立校验；${unresolved} 处引用未在链上找到，相关结论按未采信处理。</p>` : ""}
    ${(item.facts || []).length ? `<div class="th-answer-facts">${item.facts.map(fact => `<span>${esc(fact)}</span>`).join("")}</div>` : ""}
    ${item.actionsHtml ? `<div class="th-answer-actions">${item.actionsHtml}</div>` : ""}
    ${item.detailHtml ? `<details class="th-answer-detail" data-keep="answer-detail"><summary>${esc(item.detailTitle || "结构化结果")}</summary><div class="th-answer-detail-body">${item.detailHtml}</div></details>` : ""}
  </article>`;
}

/* Unknown model outcome is the one failure with an operator recovery exit.
   The wording must say "可能重复计费": the disputed round stays charged and
   unknown, and re-issuing prices a brand-new round. Confirmation is an inline
   DOM second step (no native confirm), so the projection stays Node-testable. */
function recoveryActionHtml(code) {
  if (code !== "MODEL_OUTCOME_UNKNOWN") return "";
  return `<div class="th-recover" data-recover-box>
    <button class="a-btn strong" type="button" data-run-recover>确认恢复</button>
    <p class="th-note">这一轮模型调用的结果未知，且没有可用回执。恢复会以新的一轮重新出价并独立计费，可能重复计费；旧轮仍保持未知，不会被标记成功。</p>
    <div class="th-recover-confirm" data-recover-confirm hidden>
      <span>确认以新一轮继续？</span>
      <button class="a-btn strong" type="button" data-recover-apply>确认恢复并继续</button>
      <button class="a-btn" type="button" data-recover-cancel>取消</button>
    </div>
  </div>`;
}

function errorItemHtml(item) {
  return `<article class="th-item th-error ${esc(item.tone || "bad")}" data-thread-key="error" data-thread-item="error" role="alert">
    <div class="th-error-head"><span class="th-ic">${icon("alert")}</span><span class="th-error-title">${esc(item.status === "stopped" ? "运行已停止" : item.status === "unknown" ? "运行状态未知" : "运行未能完成")}</span><span class="grow"></span><code class="th-tool-code">${esc(item.code)}</code></div>
    ${item.text ? `<p class="th-error-text">${esc(item.text)}</p>` : ""}
    ${item.message ? `<p class="th-note">${esc(item.message)}</p>` : ""}
    ${item.diagnosticsHtml || ""}
    ${item.recoverHtml || ""}
    ${item.actionsHtml ? `<div class="th-answer-actions">${item.actionsHtml}</div>` : ""}
    ${item.detailHtml ? `<details class="th-answer-detail" data-keep="error-detail"><summary>失败依据与判断</summary><div class="th-answer-detail-body">${item.detailHtml}</div></details>` : ""}
  </article>`;
}

/** 单条条目的 HTML。ctx 携带产品侧注入（审批动词、写入判定、详情渲染）。 */
function processItemHtml(item) {
  return `<article class="th-item th-process" data-thread-key="process" data-thread-item="process">
    <button type="button" class="th-process-toggle" data-process-toggle aria-expanded="${item.collapsed ? "false" : "true"}">
      <span class="th-process-label">${esc(item.label || "执行过程")}</span>
      <span class="th-process-count">${esc(item.countLabel || `${item.count} 个工具`)}</span>
      <span class="th-chev" aria-hidden="true">▾</span>
    </button>
  </article>`;
}

export function threadItemHtml(item, ctx = {}) {
  switch (item.kind) {
    case "user": return userItemHtml(item, ctx);
    case "operator": return operatorItemHtml(item, ctx);
    case "status": return statusItemHtml(item, ctx);
    case "plan": return planItemHtml(item);
    case "process": return processItemHtml(item);
    case "tool": return toolItemHtml(item, ctx);
    case "approval": return approvalItemHtml(item, ctx);
    case "answer": return answerItemHtml(item);
    case "error": return errorItemHtml(item);
    default: return "";
  }
}


/** Per-item HTML for one projection snapshot. */
export function threadItemHtmlMap(items, ctx = {}) {
  const map = new Map();
  for (const item of items || []) map.set(item.key, threadItemHtml(item, ctx));
  return map;
}

/** Keys whose HTML differs from the previous snapshot. An SSE frame only
 *  re-renders these items, so every other node (and its open state, focus and
 *  scroll position) is left untouched. */
export function changedThreadKeys(cache, htmlMap) {
  const changed = [];
  for (const [key, html] of htmlMap) {
    if (!cache || cache.get(key) !== html) changed.push(key);
  }
  return changed;
}

