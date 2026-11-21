/* Governance chat projections: normal model messages, task proposals,
 * run cards and the compact team rail. Pure render only; execution stays in
 * app.js -> /api/gov/chat/* and /api/gov/runs/*. */

import { esc, num } from "./format.js";
import { renderMarkdown } from "./chat-format.js";
import { aiAvatarHtml } from "./ai-avatar.js";
import { botStatusClass, botStatusLabel, govPendingPanelHtml } from "./gov-bots.js";

const BOT_NAME = { auditor: "审计员", verifier: "复核员", analyst: "分析员" };
const ACTION_LABEL = { audit: "扫描", verify: "验证链", analyze: "分析" };

function botName(id) {
  return BOT_NAME[String(id || "")] || String(id || "团队");
}

function actionText(action) {
  const item = action || {};
  return `${botName(item.bot_id)} · ${ACTION_LABEL[item.action] || item.action || "任务"}`;
}

function targetText(value) {
  const text = String(value || "").replace(/[\\/]+$/, "");
  return text || "最近一次完成的审计";
}

function when(value) {
  const text = String(value || "");
  const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(text);
  return match ? match[1] : "";
}

export function govChatThreadBarHtml(threads, currentId = "") {
  const list = Array.isArray(threads) ? threads : [];
  const options = ['<option value="">新对话</option>'].concat(
    list.map(thread => `<option value="${esc(thread.id)}"${thread.id === currentId ? " selected" : ""}>${esc(thread.title || "治理对话")}</option>`),
  ).join("");
  return `<div class="gov-chat-bar">
    <label class="gov-chat-thread-pick">
      <select data-gov-chat-thread aria-label="治理对话线程">${options}</select>
    </label>
  </div>`;
}

export function govActionCardHtml(message, { busy = false, dismissed = false, error = "" } = {}) {
  const action = message.action || {};
  const id = esc(message.id);
  if (dismissed) {
    return `<div class="gov-proposal dismissed" data-gov-proposal="${id}">
      <span>已忽略这个任务提案</span>
      <button class="gov-wb-button quiet" type="button" data-gov-proposal-reconsider="${id}">重新考虑</button>
    </div>`;
  }
  const target = action.target ? targetText(action.target) : "最近一次完成的审计";
  return `<div class="gov-proposal" data-gov-proposal="${id}">
    <div class="gov-proposal-head"><span class="gov-proposal-kicker">任务提案</span><b>${esc(actionText(action))}</b></div>
    <div class="gov-proposal-target"><span>目标</span><code>${esc(target)}</code></div>
    ${action.reason ? `<p class="gov-proposal-reason">${esc(action.reason)}</p>` : ""}
    ${error ? `<div class="gov-proposal-error">${esc(error)}</div>` : ""}
    <div class="gov-proposal-actions">
      <button class="gov-wb-button strong" type="button" data-gov-proposal-confirm="${id}"${busy ? " disabled" : ""}>${busy ? "正在派活…" : "确认派活"}</button>
      <button class="gov-wb-button quiet" type="button" data-gov-proposal-dismiss="${id}"${busy ? " disabled" : ""}>取消</button>
    </div>
  </div>`;
}

export function govChatRunCardHtml(run, { bot = null } = {}) {
  const item = run || {};
  const status = String(item.status || "");
  const name = botName(item.bot_id);
  const pending = status === "pending";
  const done = status === "done";
  const failed = status === "failed" || status === "rejected";
  const cls = botStatusClass(status);
  const target = targetText(item.target);
  const result = done
    ? `<div class="gov-chat-run-meta">${num(item.findings || 0)} 发现 · ${num(item.evidence || 0)} 证据${item.audit_id ? ` · <code>${esc(item.audit_id)}</code>` : ""}</div>`
    : failed
      ? `<div class="gov-chat-run-error">${esc(item.error || "运行失败")}</div>`
      : pending
        ? `<div class="gov-chat-run-meta">等待操作员批准 · ${esc(target)}</div>`
        : `<div class="gov-chat-run-meta">${esc(botStatusLabel(status))} · ${esc(target)}</div>`;
  const actions = pending
    ? `<div class="gov-chat-run-actions">
        <button class="gov-wb-button strong" type="button" data-gov-run-approve="${esc(item.run_id)}">批准执行</button>
        <button class="gov-wb-button quiet" type="button" data-gov-run-reject="${esc(item.run_id)}">拒绝</button>
      </div>`
    : done
      ? `<div class="gov-chat-run-actions">
          <button class="gov-wb-button quiet" type="button" data-gov-hist="${esc(item.audit_id || "")}">工作台 →</button>
          <button class="gov-wb-button quiet" type="button" data-gov-open-bundle="${esc(item.audit_id || "")}">审计库 →</button>
        </div>`
      : "";
  return `<div class="gov-chat-run" data-gov-chat-run="${esc(item.run_id || "")}">
    ${aiAvatarHtml(status === "running" || status === "queued" ? "thinking" : status === "pending" ? "pending" : done ? "ok" : failed ? "bad" : "idle", 28, name)}
    <div class="gov-chat-run-body">
      <div class="gov-chat-run-head"><b>${esc(name)}</b><span class="gov-chat-run-state ${esc(cls)}">${esc(botStatusLabel(status))}</span>${when(item.started_at || item.finished_at) ? `<time>${esc(when(item.started_at || item.finished_at))}</time>` : ""}</div>
      ${result}
      ${actions}
    </div>
  </div>`;
}

export function govChatExplainCardHtml(item) {
  const ex = item.explain || {};
  const finding = String(ex.finding_id || "");
  const audit = String(ex.audit_id || "");
  const isHunt = String(ex.kind || "explain") === "hunt";
  const badge = isHunt ? "探员候选" : "AI 解读";
  const target = finding
    ? `<code class="gov-explain-target">${esc(finding)}</code>`
    : `<span class="gov-explain-target">${
        isHunt && ex.candidate_count ? `${num(ex.candidate_count)} 条候选` : "整包摘要"
      }</span>`;
  const foot = [
    item.model ? esc(String(item.model)) : "",
    item.usage && item.usage.total_tokens ? `${num(item.usage.total_tokens)} tokens` : "",
  ].filter(Boolean).join(" · ");
  return `<div class="gov-chat-explain" data-gov-explain-card="${esc(finding)}">
    <header class="gov-chat-explain-head">
      <span class="gov-explain-badge">${esc(badge)}</span>
      ${target}
      ${audit ? `<span class="gov-explain-audit" title="${esc(audit)}">${esc(audit)}</span>` : ""}
      <span class="gov-explain-boundary">${isHunt ? "shadow · 未晋升 · 不进审计包" : "非裁决 · 不进审计包"}</span>
    </header>
    <div class="gov-chat-explain-body">${renderMarkdown(String(item.text || ""))}</div>
    ${foot ? `<div class="gov-chat-explain-foot">${foot}</div>` : ""}
  </div>`;
}

/* CC 式实时状态行：阶段动词 + 思考尾行 + 计时 + 中断提示。
   纯函数；计时与思考尾行由调用方每秒原地更新，不走整树重绘。 */
export function govLiveLineHtml({ phase = "", tail = "", seconds = 0 } = {}) {
  if (!phase) return "";
  const tailHtml = tail
    ? `<span class="gov-live-tail" data-gov-live-tail>${esc(String(tail).slice(-96))}</span>`
    : "";
  return `<div class="gov-live" data-gov-live role="status" aria-live="polite">
    <span class="gov-live-dot" aria-hidden="true"></span>
    <span class="gov-live-verb" data-gov-live-verb>${esc(phase)}</span>
    ${tailHtml}
    <span class="gov-live-time" data-gov-live-time>${Number(seconds) || 0}s</span>
    <span class="gov-live-esc">Esc 中断</span>
  </div>`;
}

export function govChatMessageHtml(message, { bots = [], runsById = {}, streaming = false, dismissed = false, busy = false, proposalError = "", live = null } = {}) {
  const item = message || {};
  if (item.role === "user") {
    return `<div class="gov-chat-msg user" data-gov-chat-msg="${esc(item.id)}">
      <div class="gov-chat-bubble">${esc(item.text || "")}</div>
    </div>`;
  }
  const model = String(item.model || item.provider || "");
  const text = String(item.text || "");
  const body = streaming && !text
    ? `<span class="chat-typing" aria-label="正在生成"><i></i><i></i><i></i><i></i><i></i></span>`
    : renderMarkdown(text);
  const usage = item.usage && item.usage.total_tokens
    ? `<span class="gov-chat-meta">${num(item.usage.total_tokens)} tokens${item.receipt_ref ? " · 已签收据" : ""}</span>`
    : "";
  const error = item.error && item.error.message
    ? `<div class="gov-chat-error">${esc(item.error.code || "CHAT_FAILED")} · ${esc(item.error.message)}</div>`
    : "";
  const run = item.run && item.run.id
    ? (runsById[item.run.id] || {
        run_id: item.run.id,
        status: "queued",
        bot_id: item.action && item.action.bot_id,
        target: item.action && item.action.target,
      })
    : null;
  const bot = run ? (bots || []).find(candidate => candidate.id === run.bot_id) : null;
  const proposal = item.action && item.action.kind === "gov" && !run
    ? govActionCardHtml(item, { busy, dismissed, error: proposalError })
    : "";
  const runCard = run ? govChatRunCardHtml(run, { bot }) : "";
  // 解读消息渲染成结构化卡片：徽章 + 目标 chip + 边界标注，正文不加前缀。
  if (item.explain) {
    return `<div class="gov-chat-msg assistant" data-gov-chat-msg="${esc(item.id)}">
      ${aiAvatarHtml(streaming ? "thinking" : item.status === "error" ? "bad" : "ok", 30, "治理助手")}
      <div class="gov-chat-msg-body">${govChatExplainCardHtml(item)}</div>
    </div>`;
  }
  return `<div class="gov-chat-msg assistant" data-gov-chat-msg="${esc(item.id)}">
    ${aiAvatarHtml(streaming ? "thinking" : item.status === "error" ? "bad" : "ok", 30, "治理助手")}
    <div class="gov-chat-msg-body">
      <div class="gov-chat-msg-head"><b>治理助手</b>${model ? `<span>${esc(model)}</span>` : ""}</div>
      <div class="gov-chat-text">${body}</div>
      ${streaming ? govLiveLineHtml(live || {}) : ""}
      ${error}${proposal}${runCard}
      <div class="gov-chat-foot">${usage}</div>
    </div>
  </div>`;
}

export function govChatTeamRailHtml(bots, runs) {
  const list = Array.isArray(bots) ? bots : [];
  const runList = Array.isArray(runs) ? runs : [];
  const pending = runList.filter(run => run.status === "pending");
  const roles = list.map(bot => {
    const status = bot.status || "idle";
    return `<div class="gov-chat-role">
      ${aiAvatarHtml(status === "running" ? "thinking" : status === "pending" ? "pending" : "idle", 24, bot.name)}
      <div class="gov-chat-role-main"><b>${esc(bot.name || bot.id)}</b><span>${esc(bot.role || "")}</span></div>
      <span class="gov-chat-role-state ${esc(botStatusClass(status))}">${esc(botStatusLabel(status))}</span>
    </div>`;
  }).join("");
  return `<div class="gov-chat-team-rail">
    <div class="gov-chat-roles">${roles || `<div class="gov-chat-rail-empty">团队不可用</div>`}</div>
    ${pending.length ? `<div class="gov-chat-team-pending"><span>待批准</span><b>${num(pending.length)}</b></div>${govPendingPanelHtml(list, runList)}` : ""}
  </div>`;
}
