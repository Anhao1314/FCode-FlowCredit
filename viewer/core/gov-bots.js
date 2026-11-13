/* Governance team projections: roster cards, dispatch form, channel messages.
 *
 * Pure render layer. The server owns bot definitions and run facts
 * (`/api/gov/bots`, `/api/gov/runs`); this module only turns those facts into
 * the team surface. No run state is invented here.
 */

import { esc, num } from "./format.js";
import { aiAvatarHtml } from "./ai-avatar.js";
import { renderMarkdown } from "./chat-format.js";

const STATUS_LABEL = {
  queued: "排队",
  running: "执行中",
  pending: "待批准",
  done: "完成",
  failed: "失败",
  rejected: "已拒绝",
};

export function botStatusLabel(status) {
  return STATUS_LABEL[status] || "待命";
}

export function botStatusClass(status) {
  if (status === "running" || status === "queued") return "live";
  if (status === "pending") return "warn";
  if (status === "failed" || status === "rejected") return "bad";
  if (status === "done") return "ok";
  return "idle";
}

function botOf(bots, id) {
  return (bots || []).find(bot => bot.id === id) || null;
}

function avatarState(status) {
  if (status === "queued" || status === "running") return "running";
  if (status === "pending") return "pending";
  if (status === "failed" || status === "rejected") return "failed";
  if (status === "done") return "done";
  return "idle";
}

/* Bot 状态 → 对话同款 AI 脸的状态：running 扫视+呼吸环，pending 慢环等待，
   done/failed 收敛成 ok/bad。脸不背颜色语义，状态全在动效与环上。 */
function faceState(status) {
  return { running: "thinking", pending: "pending", failed: "bad", done: "ok" }[avatarState(status)] || "idle";
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
  const pending = list.reduce((sum, bot) => sum + Number((bot.counts || {}).pending || 0), 0);
  const totalRuns = list.reduce((sum, bot) => sum + Number((bot.counts || {}).runs || 0), 0);
  const totalRoutines = list.reduce((sum, bot) => sum + Number((bot.counts || {}).routines || 0), 0);
  return `<div class="gov-team-summary">
    <span><b>${num(list.length)}</b> 个常驻角色</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(running)}</b> 执行中</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(pending)}</b> 待批准</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(totalRuns)}</b> 次运行</span>
    <span class="gov-dot-sep"></span>
    <span><b>${num(totalRoutines)}</b> 个排程</span>
    <span class="gov-team-note">Phase C · 手动 + 定时 + 审批；判定仍来自确定性管线</span>
  </div>`;
}

export function govBotCardHtml(bot) {
  const counts = bot.counts || {};
  const status = bot.status === "running" ? "running"
    : bot.status === "pending" ? "pending" : "idle";
  const active = status === "running" ? bot.current : status === "pending" ? bot.pending : null;
  const last = bot.last;
  const pill = status === "running" ? "执行中" : status === "pending" ? "待批准" : "待命";
  const currentHtml = status === "running"
    ? `<span class="gov-job-dot" aria-hidden="true"></span><span>${esc(targetLabel(active.target))}</span><span class="dim">正在执行…</span>`
    : status === "pending"
      ? `<span>${esc(targetLabel(active.target))}</span><span class="dim">等待操作员批准</span>`
      : last
        ? `<span>最近：${esc(targetLabel(last.target))}</span><span class="dim">${esc(botStatusLabel(last.status))}</span>`
        : `<span class="dim">还没有运行记录</span>`;
  return `<article class="gov-bot-card${status === "running" ? " is-running" : ""}${status === "pending" ? " is-pending" : ""}" data-bot-id="${esc(bot.id)}">
    <header class="gov-bot-card-head">
      ${aiAvatarHtml(faceState(status), 38, bot.name)}
      <div class="gov-bot-identity">
        <b>${esc(bot.name)}</b>
        <span>${esc(bot.role || "")}</span>
      </div>
      <span class="gov-bot-status ${botStatusClass(status)}">${pill}</span>
    </header>
    <p class="gov-bot-charter">${esc(bot.charter || "")}</p>
    <div class="gov-bot-current">${currentHtml}</div>
    <footer class="gov-bot-card-foot">
      <span>${num(counts.runs || 0)} 次运行 · ${num(counts.findings || 0)} 发现${
        counts.routines ? ` · ${num(counts.routines)} 个排程` : ""
      }${counts.pending ? ` · ${num(counts.pending)} 待批准` : ""}</span>
      <button class="gov-wb-button quiet" type="button" data-gov-bot-pick="${esc(bot.id)}">派活 →</button>
      <button class="gov-bot-link" type="button" data-gov-bot-open="${esc(bot.id)}">详情</button>
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

export function govBotRoutineFormHtml(bots, selected = "auditor") {
  const list = bots || [];
  const active = list.some(bot => bot.id === selected) ? selected : (list[0] && list[0].id) || "auditor";
  const options = list.map(bot =>
    `<option value="${esc(bot.id)}"${bot.id === active ? " selected" : ""}>${esc(bot.name)}</option>`
  ).join("");
  return `<form class="gov-routine-form" id="gov-routine-form">
    <label class="gov-routine-field">
      <span>Bot</span>
      <select id="gov-routine-bot" aria-label="选择排程 Bot">${options}</select>
    </label>
    <label class="gov-routine-field grow">
      <span>目标</span>
      <input id="gov-routine-target" type="text" autocomplete="off" spellcheck="false"
        placeholder="目标路径；复核员可留空 = 最新审计" aria-label="排程目标">
    </label>
    <label class="gov-routine-field">
      <span>频率</span>
      <select id="gov-routine-frequency" aria-label="排程频率">
        <option value="每天">每天</option>
        <option value="工作日">工作日</option>
        <option value="每周">每周</option>
      </select>
    </label>
    <label class="gov-routine-field">
      <span>时间</span>
      <input id="gov-routine-time" type="time" value="09:00" aria-label="排程时间">
    </label>
    <button class="gov-wb-button strong" type="submit">添加排程</button>
  </form>`;
}

export function govBotRoutinesHtml(bots) {
  const list = [];
  for (const bot of bots || []) {
    for (const routine of bot.routines || []) list.push({ bot, routine });
  }
  if (!list.length) {
    return `<div class="gov-empty-state"><b>还没有定时排程</b><span>给审计员或复核员加一条 routine，到点会自动创建运行。</span></div>`;
  }
  return `<div class="gov-routine-list">${list.map(({ bot, routine }) => {
    const target = routine.target
      ? `<code>${esc(routine.target)}</code>`
      : `<span class="dim">最新审计</span>`;
    const last = routine.last_fired_at
      ? `上次 ${esc(formatTime(routine.last_fired_at))}`
      : "还没触发";
    const lastState = routine.last_status && routine.last_status !== "running"
      ? ` · ${routine.last_status.startsWith("error:") ? "失败" : botStatusLabel(routine.last_status)}`
      : "";
    return `<div class="gov-routine-row" data-routine-id="${esc(routine.id)}">
      ${aiAvatarHtml("idle", 30, bot.name)}
      <div class="gov-routine-main">
        <div class="gov-routine-title"><b>${esc(bot.name)}</b><span>·</span>${target}</div>
        <div class="gov-routine-meta">${esc(routine.next || routine.frequency)} · ${last}${esc(lastState)}</div>
      </div>
      <label class="gov-routine-toggle">
        <input type="checkbox" data-gov-routine-toggle="${esc(routine.id)}"${routine.enabled ? " checked" : ""}>
        启用
      </label>
      <button class="gov-routine-delete" type="button" data-gov-routine-delete="${esc(routine.id)}">删除</button>
    </div>`;
  }).join("")}</div>`;
}

function runStatusLine(bot, run) {
  if (run.status === "rejected") {
    return `<div class="gov-dialog-error">操作员拒绝执行${run.error ? ` · ${esc(run.error)}` : ""}</div>`;
  }
  if (run.status === "failed") {
    return `<div class="gov-dialog-error">${esc(run.error || "运行失败")}</div>`;
  }
  if (run.status === "pending") {
    return `<div class="gov-bot-progress is-pending">等待操作员批准 · ${esc(targetLabel(run.target))}</div>`;
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

function runActions(run, opts = {}) {
  if (run.status === "pending") {
    return `<div class="gov-bot-run-actions">
      <button class="gov-wb-button strong" type="button" data-gov-run-approve="${esc(run.run_id)}">批准执行</button>
      <button class="gov-wb-button quiet" type="button" data-gov-run-reject="${esc(run.run_id)}">拒绝</button>
    </div>`;
  }
  if (run.status !== "done") return "";
  const links = run.audit_id
    ? `<button class="gov-wb-button quiet" type="button" data-gov-hist="${esc(run.audit_id)}">工作台 →</button>
       <button class="gov-wb-button quiet" type="button" data-gov-open-bundle="${esc(run.audit_id)}">审计台 →</button>`
    : "";
  const teach = opts.teach === false ? ""
    : `<button class="gov-wb-button quiet" type="button" data-gov-skill-run="${esc(run.run_id)}"
      title="从这次已完成的运行生成可复用技能">教一次</button>`;
  const explain = run.audit_id
    ? `<button class="gov-wb-button quiet" type="button" data-gov-explain="${esc(run.run_id)}"
      title="基于审计包生成模型解读（非裁决，不进审计包）">AI 解读</button>`
    : "";
  return `<div class="gov-bot-run-actions">${links}${teach}${explain}</div>`;
}

export function govBotSettingsFormHtml(bot) {
  const settings = bot.settings || {};
  const limit = Number(settings.daily_run_limit || 12);
  const used = Number(bot.runs_today || 0);
  const triage = String(settings.triage_mode || "off");
  const triageSelect = bot.id === "auditor" ? `<label class="gov-bot-setting">
      <span>跟进策略</span>
      <select id="gov-bot-triage-mode">
        <option value="off"${triage === "off" ? " selected" : ""}>完整扫描 · 不做 triage</option>
        <option value="shadow"${triage === "shadow" ? " selected" : ""}>影子 triage · 只记录优先级</option>
        <option value="active"${triage === "active" ? " selected" : ""}>受控 triage · 提议待批准复扫</option>
      </select>
    </label>` : "";
  return `<form class="gov-bot-settings-form" id="gov-bot-settings-form" data-bot-id="${esc(bot.id)}">
    <label class="gov-bot-setting">
      <span>每日运行上限</span>
      <input id="gov-bot-daily-limit" type="number" min="1" max="50" value="${num(limit)}">
    </label>
    <label class="gov-bot-check">
      <input id="gov-bot-routine-approval" type="checkbox"${settings.routine_approval ? " checked" : ""}>
      定时运行需操作员批准
    </label>
    ${triageSelect}
    <button class="gov-wb-button strong" type="submit">保存设置</button>
    <span class="gov-bot-budget">今日已用 ${num(used)} / ${num(limit)}</span>
  </form>`;
}

export function govBotSkillsHtml(bot) {
  const skills = bot.skills || [];
  if (!skills.length) {
    return `<div class="gov-empty-state"><b>还没有技能</b><span>在团队频道里选一次已完成的运行，点「教一次」生成技能。</span></div>`;
  }
  return `<div class="gov-skill-list">${skills.map(skill => `<div class="gov-skill-row" data-skill-id="${esc(skill.id)}">
    <div class="gov-skill-main">
      <b>${esc(skill.name || "未命名技能")}</b>
      <span>${esc(skill.target || "—")} · 来源 ${esc(skill.source_run_id || "—")} · v${num(skill.version || 1)}</span>
    </div>
    <time>${esc(formatTime(skill.created))}</time>
    <button class="gov-bot-link" type="button" data-gov-skill-schedule="${esc(skill.id)}"
      title="用这条技能创建每天 09:00 的排程">排程</button>
    <button class="gov-routine-delete" type="button" data-gov-skill-delete="${esc(skill.id)}">删除</button>
  </div>`).join("")}</div>`;
}

function runRowHtml(bot, run) {
  const trigger = run.trigger === "routine" ? "定时" : "手动";
  const target = targetLabel(run.target);
  const state = botStatusLabel(run.status);
  const captured = (bot.skills || []).some(skill => skill.source_run_id === run.run_id);
  return `<div class="gov-bot-run-row" data-run-id="${esc(run.run_id)}">
    ${aiAvatarHtml(faceState(run.status), 26, bot.name)}
    <div class="gov-bot-run-main">
      <div class="gov-bot-run-title"><b>${esc(target)}</b><span>${esc(trigger)}</span><span class="${botStatusClass(run.status)}">${esc(state)}</span></div>
      <div class="gov-bot-run-meta">${esc(formatTime(run.finished_at || run.started_at))}${
        run.audit_id ? ` · ${esc(run.audit_id)}` : ""
      }${run.error ? ` · ${esc(run.error)}` : ""}</div>
    </div>
    ${runActions(run, { teach: !captured })}
  </div>`;
}

export function govBotDetailHtml(bot, runs = []) {
  const counts = bot.counts || {};
  const settings = bot.settings || {};
  const status = bot.status === "running" ? "running"
    : bot.status === "pending" ? "pending" : "idle";
  return `<div class="gov-bot-detail">
    <header class="gov-bot-detail-head">
      ${aiAvatarHtml(faceState(status), 72, bot.name)}
      <div class="gov-bot-detail-title">
        <p class="eyebrow">GOVERNANCE / BOT</p>
        <h1>${esc(bot.name)}</h1>
        <p>${esc(bot.role || "")} · <span class="gov-bot-status ${botStatusClass(status)}">${
          status === "running" ? "执行中" : status === "pending" ? "待批准" : "待命"
        }</span></p>
      </div>
      <button class="gov-wb-button quiet" type="button" data-gov-bot-back>← 团队</button>
    </header>
    <p class="gov-bot-detail-charter">${esc(bot.charter || "")}</p>
    <div class="gov-bot-detail-grid">
      <section class="gov-bot-panel">
        <h3>职责与权限</h3>
        <dl class="gov-bot-facts">
          <div><dt>工具</dt><dd>${esc((bot.tools || []).join(" · "))}</dd></div>
          <div><dt>模型策略</dt><dd>${esc(bot.model_policy || "deterministic")}</dd></div>
          <div><dt>今日预算</dt><dd>${num(bot.runs_today || 0)} / ${num(settings.daily_run_limit || 0)}</dd></div>
          <div><dt>累计运行</dt><dd>${num(counts.runs || 0)} 次 · ${num(counts.findings || 0)} 发现</dd></div>
        </dl>
      </section>
      <section class="gov-bot-panel">
        <h3>预算与审批</h3>
        ${govBotSettingsFormHtml(bot)}
      </section>
    </div>
    <section class="gov-bot-panel">
      <h3>技能</h3>
      ${govBotSkillsHtml(bot)}
    </section>
    <section class="gov-bot-panel">
      <h3>排程</h3>
      ${govBotRoutinesHtml([bot])}
    </section>
    <section class="gov-bot-panel">
      <h3>最近运行</h3>
      ${(runs || []).length
        ? `<div class="gov-bot-run-list">${runs.slice(0, 12).map(run => runRowHtml(bot, run)).join("")}</div>`
        : `<div class="gov-empty-state"><b>还没有运行</b><span>回到团队页给 ${esc(bot.name)} 派活。</span></div>`}
    </section>
  </div>`;
}

export function govBotRunHtml(bot, run, explanations = {}) {
  const target = targetLabel(run.target);
  const verb = run.trigger === "routine"
    ? (bot.id === "verifier" ? "定时验证" : "定时扫描")
    : (bot.id === "verifier" ? "验证" : "扫描");
  const captured = (bot.skills || []).some(skill => skill.source_run_id === run.run_id);
  const explainByRun = explanations || {};
  return `<div class="gov-entry user"><span class="gov-echo">@${esc(bot.name)} ${
    verb + " " + esc(bot.id === "verifier" ? (run.audit_id || target) : target)
  }</span></div>
  <div class="gov-bot-message">
    ${aiAvatarHtml(faceState(run.status), 30, bot.name)}
    <div class="gov-bot-message-body">
      <header class="gov-bot-message-head">
        <b>${esc(bot.name)}</b><span>${esc(bot.role || "")}</span>
        <time>${esc(formatTime(run.finished_at || run.started_at))}</time>
        <span class="gov-bot-run-state ${botStatusClass(run.status)}">${esc(botStatusLabel(run.status))}</span>
      </header>
      ${runStatusLine(bot, run)}
      ${runActions(run, { teach: !captured })}
      ${govTriageBlock(run)}
      ${govExplainBlock(explainByRun[run.run_id])}
    </div>
  </div>`;
}

/* 受控 triage：模型只提议跟进优先级，不裁决、不改扫描覆盖。
   shadow=只记录；active=额外生成一条待操作员批准的完整复扫。 */
export function govTriageBlock(run) {
  const status = String(run.triage_status || "");
  if (!status) return "";
  const plan = run.triage || {};
  const mode = String(run.triage_mode || plan.mode || "");
  if (status === "pending") {
    return `<div class="gov-triage" data-gov-triage="pending">
      <span class="gov-triage-badge">TRIAGE</span>
      <span class="gov-triage-note">正在生成跟进优先级…</span>
    </div>`;
  }
  if (status === "error") {
    return `<div class="gov-triage is-error" data-gov-triage="error">
      <span class="gov-triage-badge">TRIAGE</span>
      <span class="gov-triage-note">${esc(run.triage_error || "triage 不可用")}</span>
      <span class="gov-triage-boundary">审计已照常完成；triage 失败不改变任何证据。</span>
    </div>`;
  }
  const priorities = plan.priorities || [];
  const modeLabel = mode === "active" ? "受控 · 提议复扫（待批准）" : "影子 · 仅记录";
  const items = priorities.map(item => `<li>
    <code>${esc(item.finding_id)}</code>
    <span>${esc(item.why)}</span>
    <em>${esc(item.action)}</em>
  </li>`).join("");
  const focus = (plan.focus_categories || []).map(item => `<span class="gov-triage-focus">${esc(item)}</span>`).join("");
  const spawn = run.triage_run_id
    ? `<p class="gov-triage-spawn">已生成待批准复扫 <code>${esc(run.triage_run_id)}</code> · 批准后才执行</p>`
    : (mode === "active" && priorities.length
      ? '<p class="gov-triage-spawn dim">复扫提议未生成（可能触及今日预算），可手动派活。</p>'
      : "");
  return `<div class="gov-triage" data-gov-triage="done">
    <div class="gov-triage-head"><span class="gov-triage-badge">TRIAGE</span>
      <span class="gov-triage-mode">${esc(modeLabel)}</span>
      <span class="gov-triage-confidence">置信 ${esc(plan.confidence || "medium")}</span>
    </div>
    ${plan.summary ? `<p class="gov-triage-summary">${esc(plan.summary)}</p>` : ""}
    ${focus ? `<div class="gov-triage-focus-row">关注类别 ${focus}</div>` : ""}
    ${priorities.length
      ? `<ol class="gov-triage-list">${items}</ol>`
      : '<p class="gov-triage-note">模型没有给出需要优先跟进的项。</p>'}
    ${spawn}
    <p class="gov-triage-meta">${esc(plan.model || "")} · ${num(plan.tokens || 0)} tokens · ${num(plan.cost_microusd || 0)} µUSD · 只提议优先级，判据仍来自规则与重放</p>
  </div>`;
}

/* 解释层产物：非确定性、非裁决，渲染时必须带边界标签。 */
export function govExplainBlock(explanation) {
  const e = explanation || {};
  if (!e.text) return "";
  return `<details class="gov-explain" data-gov-explain-block="${esc(e.run_id || "")}" open>
    <summary><span class="gov-explain-badge">AI 解读</span><span class="gov-explain-boundary">非裁决 · 引用确定性证据 · 不进审计包</span></summary>
    <div class="gov-explain-body">${renderMarkdown(e.text)}</div>
    <p class="gov-explain-meta">${esc(e.model || "")} · ${num(e.tokens || 0)} tokens · prompt ${esc(String(e.prompt_sha256 || "").slice(0, 12))}</p>
  </details>`;
}

export function govBotChannelHtml(runs, bots, explanations = {}) {
  const explainByRun = explanations || {};
  const list = (runs || []).slice().reverse(); // API is newest-first; channel is chronological
  if (!list.length) {
    return `<div class="gov-empty-state"><b>团队频道还是空的</b><span>把第一个目标派给审计员，运行结果会出现在这里。</span></div>`;
  }
  return list.map(run => {
    const bot = botOf(bots, run.bot_id);
    return bot ? govBotRunHtml(bot, run, explainByRun) : "";
  }).join("");
}