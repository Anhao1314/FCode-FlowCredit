/* Governance team projections: roster cards, dispatch form, channel messages.
 *
 * Pure render layer. The server owns bot definitions and run facts
 * (`/api/gov/bots`, `/api/gov/runs`); this module only turns those facts into
 * the team surface. No run state is invented here.
 */

import { esc, num } from "./format.js";

const STATUS_LABEL = {
  queued: "排队",
  running: "执行中",
  done: "完成",
  failed: "失败",
};

export function botStatusLabel(status) {
  return STATUS_LABEL[status] || "待命";
}

export function botStatusClass(status) {
  if (status === "running" || status === "queued") return "live";
  if (status === "failed") return "bad";
  if (status === "done") return "ok";
  return "idle";
}

function botOf(bots, id) {
  return (bots || []).find(bot => bot.id === id) || null;
}

function formatTime(value) {
  const text = String(value || "");
  const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(text);
  return match ? match[1] : "";
}

function targetLabel(value) {
  const target = String(value || "").replace(/[\\/]+$/, "");
  return target.split(/[\\/]/).filter(Boolean).pop() || target || "—";
}

export function govBotSummaryHtml(bots) {
  const list = bots || [];
  const running = list.filter(bot => bot.status === "running").length;
  const totalRuns = list.reduce((sum, bot) => sum + Number((bot.counts || {}).runs || 0), 0);
  return `<div class="gov-team-summary">
    <span><b>${num(list.length)}</b> 个常驻角色</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(running)}</b> 执行中</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(totalRuns)}</b> 次运行</span>
    <span class="gov-team-note">Phase A · 手动触发；判定仍来自确定性管线</span>
  </div>`;
}

export function govBotCardHtml(bot) {
  const current = bot.current || bot.last || null;
  const counts = bot.counts || {};
  const status = bot.status === "running" ? "running" : "idle";
  const running = status === "running" && current;
  const target = running ? current.target : (bot.last && bot.last.target);
  const last = bot.last;
  return `<article class="gov-bot-card${status === "running" ? " is-running" : ""}" data-bot-id="${esc(bot.id)}">
    <header class="gov-bot-card-head">
      <span class="gov-bot-avatar" aria-hidden="true">${esc(bot.initial || bot.name.slice(0, 1))}</span>
      <div class="gov-bot-identity">
        <b>${esc(bot.name)}</b>
        <span>${esc(bot.role || "")}</span>
      </div>
      <span class="gov-bot-status ${botStatusClass(status)}">${running ? "执行中" : "待命"}</span>
    </header>
    <p class="gov-bot-charter">${esc(bot.charter || "")}</p>
    <div class="gov-bot-current">
      ${running
        ? `<span class="gov-job-dot" aria-hidden="true"></span><span>${esc(targetLabel(target))}</span><span class="dim">正在执行…</span>`
        : last
          ? `<span>最近：${esc(targetLabel(last.target))}</span><span class="dim">${esc(botStatusLabel(last.status))}</span>`
          : `<span class="dim">还没有运行记录</span>`}
    </div>
    <footer class="gov-bot-card-foot">
      <span>${num(counts.runs || 0)} 次运行 · ${num(counts.findings || 0)} 发现</span>
      <button class="gov-wb-button quiet" type="button" data-gov-bot-pick="${esc(bot.id)}">派活 →</button>
    </footer>
  </article>`;
}

export function govBotRosterHtml(bots) {
  const list = bots || [];
  if (!list.length) {
    return `<div class="gov-empty-state"><b>团队还没有编制</b><span>治理 Bot 服务未就绪。</span></div>`;
  }
  return `<div class="gov-bot-roster">${list.map(govBotCardHtml).join("")}</div>`;
}

export function govBotDispatchHtml(bots, selected = "auditor") {
  const list = bots || [];
  const active = list.some(bot => bot.id === selected) ? selected : (list[0] && list[0].id) || "auditor";
  const options = list.map(bot =>
    `<option value="${esc(bot.id)}"${bot.id === active ? " selected" : ""}>${esc(bot.name)} · ${esc(bot.role || "")}</option>`
  ).join("");
  const bot = botOf(list, active);
  const placeholder = bot && bot.id === "verifier"
    ? "审计 id（留空 = 最新一次）"
    : "目标路径，如 fixtures/demo-target";
  return `<form class="gov-bot-dispatch" id="gov-bot-dispatch">
    <label class="gov-bot-dispatch-field">
      <span>派给</span>
      <select id="gov-bot-select" aria-label="选择治理 Bot">${options}</select>
    </label>
    <label class="gov-bot-dispatch-field grow">
      <span>目标</span>
      <input id="gov-bot-target" type="text" autocomplete="off" spellcheck="false"
        placeholder="${esc(placeholder)}" aria-label="运行目标">
    </label>
    <button class="gov-wb-button strong" type="submit">派活</button>
  </form>`;
}

function runStatusLine(bot, run) {
  if (run.status === "failed") {
    return `<div class="gov-dialog-error">${esc(run.error || "运行失败")}</div>`;
  }
  if (run.status === "queued" || run.status === "running") {
    return `<div class="gov-bot-progress"><span class="gov-job-dot" aria-hidden="true"></span>正在执行 ${esc(targetLabel(run.target))}…</div>`;
  }
  if (bot.id === "verifier") {
    const chain = run.chain_intact === true
      ? `<span class="gov-state ok">链完整</span>`
      : run.chain_intact === false
        ? `<span class="gov-state bad">链断裂</span>`
        : `<span class="gov-state">链状态未知</span>`;
    const reg = run.regression || {};
    const changed = Number(reg.new || 0) + Number(reg.fixed || 0) + Number(reg.regressed || 0);
    return `<div class="gov-bot-result-line">${chain}
      <span>${num(run.events || 0)} 事件</span>
      <span>·</span>
      <span>${changed ? `回归 new ${num(reg.new || 0)} / fixed ${num(reg.fixed || 0)} / regressed ${num(reg.regressed || 0)}` : "无回归变化"}</span>
    </div>`;
  }
  return `<div class="gov-bot-result-line">
    <span>${num(run.findings || 0)} 发现</span>
    <span>·</span>
    <span>${num(run.evidence || 0)} 证据记录</span>
    ${run.audit_id ? `<span class="gov-mono dim">${esc(run.audit_id)}</span>` : ""}
  </div>`;
}

function runActions(run) {
  if (run.status !== "done" || !run.audit_id) return "";
  return `<div class="gov-bot-run-actions">
    <button class="gov-wb-button quiet" type="button" data-gov-hist="${esc(run.audit_id)}">工作台 →</button>
    <button class="gov-wb-button quiet" type="button" data-gov-open-bundle="${esc(run.audit_id)}">审计台 →</button>
  </div>`;
}

export function govBotRunHtml(bot, run) {
  const target = targetLabel(run.target);
  return `<div class="gov-entry user"><span class="gov-echo">@${esc(bot.name)} ${
    bot.id === "verifier" ? "验证 " + esc(run.audit_id || target) : "扫描 " + esc(target)
  }</span></div>
  <div class="gov-bot-message">
    <span class="gov-bot-avatar" aria-hidden="true">${esc(bot.initial || bot.name.slice(0, 1))}</span>
    <div class="gov-bot-message-body">
      <header class="gov-bot-message-head">
        <b>${esc(bot.name)}</b><span>${esc(bot.role || "")}</span>
        <time>${esc(formatTime(run.finished_at || run.started_at))}</time>
        <span class="gov-bot-run-state ${botStatusClass(run.status)}">${esc(botStatusLabel(run.status))}</span>
      </header>
      ${runStatusLine(bot, run)}
      ${runActions(run)}
    </div>
  </div>`;
}

export function govBotChannelHtml(runs, bots) {
  const list = (runs || []).slice().reverse(); // API is newest-first; channel is chronological
  if (!list.length) {
    return `<div class="gov-empty-state"><b>团队频道还是空的</b><span>把第一个目标派给审计员，运行结果会出现在这里。</span></div>`;
  }
  return list.map(run => {
    const bot = botOf(bots, run.bot_id);
    return bot ? govBotRunHtml(bot, run) : "";
  }).join("");
}