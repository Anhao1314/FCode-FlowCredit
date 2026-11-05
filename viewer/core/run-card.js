/* The execution card: a proposal inside the thread, then its run's progress.
 *
 * Two pure renderers and one projection. Everything the card shows is either
 * the message's own proposal (as the operator may have edited it) or a fact
 * read off the run's trace — nothing is inferred. Nothing here can start
 * work: only the confirmation endpoint does, and only from a click on
 * [data-run-start].
 */

import { esc, num } from "./format.js";

export const RUN_MODES = [
  ["script", "脚本", "在工作区写并运行文件"],
  ["research", "研究", "抓取指定来源并整理结论"],
];

export const APPROVAL_LABELS = {
  off: "不询问",
  consequential: "关键步骤前询问",
  all: "每次工具调用都询问",
};

export function modeLabel(mode) {
  const found = RUN_MODES.find(([key]) => key === mode);
  return found ? found[1] : RUN_MODES[0][1];
}

function approvalLabel(mode) {
  return APPROVAL_LABELS[mode] || APPROVAL_LABELS.consequential;
}

const ACTION_MODES = new Set(RUN_MODES.map(([key]) => key));

/* A message carries a proposal only when the action is a real, non-empty
 * object with the goal the server's parser would have required. An empty `{}`
 * — what the replay used to project onto every assistant message — is not a
 * proposal, and `!action` alone cannot tell the two apart (in JS `{}` is
 * truthy). Being strict here is what keeps blank cards out of the thread. */
export function isExecutionProposal(action) {
  if (!action || typeof action !== "object" || Array.isArray(action)) return false;
  const goal = action.goal;
  if (typeof goal !== "string" || !goal.trim()) return false;
  if (action.mode !== undefined && action.mode !== null && !ACTION_MODES.has(String(action.mode))) return false;
  return true;
}

/* A run-anchored message stays a run mirror even when its proposal is gone
 * from the projection: the anchor, not the action, is what the mirror needs. */
export function messageHasProposal(message) {
  return isExecutionProposal((message || {}).action);
}

function sourcesText(sources) {
  return (sources || []).filter(item => typeof item === "string" && item.trim()).join("\n");
}

/* ------------------------------------------------------------- the proposal */

/** The in-thread proposal: one compact "next step" line, with the editable
 *  goal/mode/sources behind a disclosure. Everything the confirmation
 *  endpoint needs is still a real control in the DOM; the card no longer
 *  interrupts the conversation with a full-width form. */
export function actionCardHtml(message, card = {}) {
  const id = esc(message.id);
  const action = message.action || {};
  const goal = card.draft !== undefined ? card.draft : String(action.goal || "");
  const mode = card.mode || action.mode || "script";
  const sources = card.sources !== undefined ? card.sources : sourcesText(action.sources);
  const busy = !!card.busy;
  const disabled = busy ? " disabled" : "";
  const error = card.error ? `<div class="run-card-error">${esc(card.error)}</div>` : "";
  const reason = action.reason
    ? `<div class="run-card-reason">模型理由：${esc(action.reason)}</div>`
    : "";
  const carried = Number((message.context || {}).messages || 0);
  const modeTitle = (RUN_MODES.find(([key]) => key === mode) || RUN_MODES[0])[1];
  return `<div class="run-card proposal compact" data-card="${id}" data-run-card="${id}">
    <div class="run-card-next">
      <span class="run-card-next-mark">下一步</span>
      <span class="run-card-next-goal">${esc(goal)}</span>
      <span class="run-card-next-mode">${esc(modeTitle)}</span>
      <span class="grow"></span>
      <button type="button" class="a-btn ghost" data-run-cancel="${id}"${disabled}>忽略</button>
      <button type="button" class="a-btn primary" data-run-start="${id}"${disabled}>${busy ? "正在创建运行…" : "开始执行"}</button>
    </div>
    ${error}
    <details class="run-card-more">
      <summary>调整目标与方式 · 确认后才会创建运行</summary>
      <label class="run-card-field">
        <span class="run-card-key">目标</span>
        <textarea class="run-card-input" rows="2" data-run-goal
          placeholder="这次运行要达成什么"${disabled}>${esc(goal)}</textarea>
      </label>
      <div class="run-card-field">
        <span class="run-card-key">方式</span>
        <div class="run-card-modes" role="group" aria-label="执行方式">
          ${RUN_MODES.map(([key, title, desc]) => `<button type="button"
            class="run-card-mode${key === mode ? " active" : ""}" aria-pressed="${key === mode}"
            data-run-mode="${esc(key)}" title="${esc(desc)}"${disabled}>${esc(title)}</button>`).join("")}
        </div>
      </div>
      ${mode === "research" ? `<label class="run-card-field">
        <span class="run-card-key">来源</span>
        <textarea class="run-card-input" rows="3" data-run-sources
          placeholder="每行一个 http(s) URL，最多 5 个"${disabled}>${esc(sources)}</textarea>
      </label>` : ""}
      ${reason}
      <div class="run-card-facts">
        <span class="k">审批</span><span class="v">${esc(approvalLabel(card.approval))}</span>
        <span class="k">工作区</span><span class="v">本次运行独立目录</span>
        ${carried ? `<span class="k">上下文</span><span class="v">携带本对话 ${num(carried)} 条</span>` : ""}
      </div>
    </details>
  </div>`;
}

/* -------------------------------------------------------------- the mirror */

/** One token for what a round ended up doing. Deliberately coarse: the card
 *  mirrors the run, it does not replace the run's own evidence view. */
export function verdictOf(observation) {
  const seen = observation || {};
  const result = seen.result || {};
  if ((seen.decision || {}).allowed === false) return "未获授权";
  const failure = seen.failure || result.failure || seen.error || result.error;
  if (failure) return String(failure.code || failure.message || failure).slice(0, 40);
  if (seen.blocked || result.blocked) return "受阻";
  if (result.exit_code !== undefined) return result.exit_code === 0 ? "退出码 0" : `退出码 ${result.exit_code}`;
  if (result.changed === true) return "已写入";
  if (result.changed === false) return "无变化";
  if (result.reconciled === false || result.computed === false) return "无法完成";
  if (result.fetched === false) return "抓取失败";
  return "完成";
}

/**
 * The card's projection of one run. ``live`` carries what the stream knows but
 * the trace does not (status, pending approval, cost, finish). ``describeRow``
 * is the viewer's own one-line tool description, so the mirror speaks the same
 * language as the run's evidence view.
 */
export function mirrorFrom(snapshot, live = {}, describeRow = null) {
  const trace = snapshot || {};
  const rounds = trace.rounds || [];
  const terminal = trace.terminal || {};
  const results = ((terminal.result || {}).results) || [];
  return {
    rows: rounds.slice(-3).map(round => {
      const observation = round.observation;
      const tool = (observation || {}).tool
        || ((round.proposal || {}).steps || []).map(step => step.tool).join("+");
      /* A round with a proposal but no observation has not executed yet — it
         is waiting at the gate or about to run. Saying "完成" there, or asking
         the viewer to describe an empty result, would both be invented. */
      const waiting = !observation;
      return {
        n: round.index || 0,
        tool: tool || "—",
        verdict: waiting
          ? (((round.decision || {}).allowed === false) ? "未获授权" : "等待")
          : verdictOf(observation),
        detail: !waiting && describeRow ? String(describeRow(observation) || "").slice(0, 90) : "",
      };
    }),
    rounds: rounds.length,
    artifacts: results.map(item => ({ path: item.path, bytes: item.bytes })),
    status: String(live.status || terminal.status || "").trim(),
    verified: (terminal.result || {}).verified === true,
    failure: terminal.failure || null,
    cost: live.cost || null,
    pending: live.pending || null,
    deciding: live.deciding || null,
    error: String(live.error || "").trim(),
    finished: !!live.finished,
  };
}

function pendingHtml(pending, mirror) {
  if (!pending) return "";
  const args = Object.entries(pending.arguments || {})
    .map(([key, value]) => {
      const text = String(value);
      return `${key}=${text.length > 60 ? text.slice(0, 60) + "…" : text}`;
    }).join(", ");
  /* A submitted decision only covers the gate it was submitted for: the
     moment the server hands over a different one, the buttons are live again. */
  const deciding = mirror.deciding && mirror.deciding.pendingId === pending.id
    ? mirror.deciding
    : null;
  const state = deciding
    ? `<span class="run-card-hint">${deciding.allow ? "已允许 · 等待继续…" : "已拒绝 · 运行将终止…"}</span>`
    : (pending.expiresIn ? `<span class="run-card-hint">${num(pending.expiresIn)} 秒后按默认处理</span>` : "");
  return `<div class="run-card-approval">
    <div class="run-card-approval-head">
      <span class="run-card-key">第 ${esc(pending.round)} 轮 · 需要授权</span>
      <span class="grow"></span>${state}
    </div>
    <div class="run-card-approval-body"><code>${esc(pending.tool)}(${esc(args)})</code></div>
    <div class="run-card-actions">
      <button type="button" class="a-btn ghost" data-run-deny="${esc(pending.id)}"${deciding ? " disabled" : ""}>拒绝</button>
      <button type="button" class="a-btn primary" data-run-allow="${esc(pending.id)}"${deciding ? " disabled" : ""}>允许</button>
    </div>
  </div>`;
}

// Classes are the viewer's own badge tones (design.css): run = live accent,
// pass = ok, deny = bad. The mirror must not invent a third vocabulary.
const STATUS_BADGE = {
  planned: ["run", "已计划"],
  running: ["run", "运行中"],
  succeeded: ["pass", "已完成"],
  failed: ["deny", "失败"],
  stopped: ["", "已停止"],
};

export function runMirrorHtml(message, mirror = {}, anchors = {}) {
  const id = esc(message.id);
  const action = message.action || {};
  const run = anchors.run || {};
  const goal = String(run.goal || action.goal || "");
  // run.mode is what the operator approved (the anchor records it); action.mode
  // is only the model's first draft and may have been switched at the card.
  const runMode = run.mode || action.mode || "script";
  const state = mirror.status || (mirror.finished ? "succeeded" : "running");
  const [tone, label] = STATUS_BADGE[state] || ["run", state];
  const rows = mirror.rows && mirror.rows.length
    ? `<div class="run-card-rows">${mirror.rows.map(row => `<div class="run-card-row">
        <span class="rno">#${esc(row.n)}</span>
        <span class="tool">${esc(row.tool)}</span>
        ${row.detail ? `<span class="detail">${esc(row.detail)}</span>` : ""}
        <span class="grow"></span>
        <span class="verdict">${esc(row.verdict)}</span>
      </div>`).join("")}</div>`
    : `<div class="run-card-rows waiting"><span class="run-card-hint">${mirror.finished ? "这次运行没有留下可读的轮次" : "等待 Runtime 的第一个轮次…"}</span></div>`;
  const artifacts = (mirror.artifacts || []).length
    ? `<div class="run-card-artifacts">${mirror.artifacts.map(item =>
        `<div class="run-card-artifact"><span class="path">${esc(item.path || "")}</span>
          <span class="grow"></span><span class="bytes">${num(item.bytes || 0)} B</span></div>`).join("")}</div>`
    : "";
  const failure = mirror.failure
    ? `<div class="run-card-error">${esc(mirror.failure.code || "失败")} · ${esc(mirror.failure.message || "")}</div>`
    : (mirror.error ? `<div class="run-card-error">${esc(mirror.error)}</div>` : "");
  const facts = [];
  if (mirror.rounds) facts.push(`第 ${num(mirror.rounds)} 轮`);
  if (mirror.cost && mirror.cost.microusd !== undefined) facts.push(`${num(mirror.cost.microusd)} µUSD`);
  if (mirror.verified) facts.push("链已校验");
  const actions = [];
  if (!mirror.finished && run.id) {
    actions.push(`<button type="button" class="a-btn ghost" data-run-stop="${esc(run.id)}">停止</button>`);
  }
  if (run.id) {
    actions.push(`<button type="button" class="a-btn" data-run-open="${esc(run.id)}">在 Work 中打开</button>`);
  }
  return `<div class="run-card mirror" data-card="${id}" data-run-card="${id}" data-run="${esc(run.id || "")}">
    <div class="run-card-head">
      <span class="run-card-title">${esc(modeLabel(runMode))}运行</span>
      <span class="grow"></span>
      <span class="badge ${tone}"><span class="dot"></span>${esc(label)}</span>
    </div>
    ${goal ? `<div class="run-card-goal">${esc(goal)}</div>` : ""}
    ${rows}
    ${pendingHtml(mirror.pending, mirror)}
    ${artifacts}
    ${failure}
    ${facts.length ? `<div class="run-card-meta">${facts.map(fact => `<span>${esc(fact)}</span>`).join("")}</div>` : ""}
    <div class="run-card-actions">${actions.join("")}</div>
  </div>`;
}