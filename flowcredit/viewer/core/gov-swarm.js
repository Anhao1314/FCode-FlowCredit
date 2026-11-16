/* 目标级 fan-out 的纯渲染层：蜂群表单、进度卡与目标运行图。
 *
 * 蜂群只调度，不产生判定：每个目标是一条独立 bot_run，状态仍来自
 * /api/gov/swarms；取消只停止排队，已在跑的 run 会跑完。
 */

import { esc, num } from "./format.js";

const SWARM_STATUS = {
  running: "运行中",
  queued: "排队中",
  done: "完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
};
const RUN_STATUS = {
  queued: "排队",
  running: "执行中",
  done: "完成",
  failed: "失败",
  rejected: "已取消",
};

function targetLabel(target) {
  const value = String(target || "").replace(/[\\/]+$/, "");
  return value.split(/[\\/]/).filter(Boolean).pop() || value || "目标";
}

export function govSwarmFormHtml() {
  return `<form class="gov-swarm-form" id="gov-swarm-form">
    <label class="gov-swarm-field grow">
      <span>目标（每行一个）</span>
      <textarea id="gov-swarm-targets" rows="3" spellcheck="false"
        placeholder="/Applications/EvoX.app&#10;fixtures/demo-target"
        aria-label="蜂群目标"></textarea>
    </label>
    <label class="gov-swarm-field">
      <span>并发</span>
      <select id="gov-swarm-concurrency" aria-label="蜂群并发">
        <option value="1">1</option>
        <option value="2" selected>2</option>
        <option value="3">3</option>
        <option value="4">4</option>
      </select>
    </label>
    <label class="gov-swarm-field grow">
      <span>目标描述</span>
      <input id="gov-swarm-target-md" type="text" autocomplete="off" spellcheck="false"
        placeholder="target.md（可选，如 fixtures/evox/target.md）" aria-label="蜂群目标描述">
    </label>
    <label class="gov-swarm-field grow">
      <span>OSV 快照</span>
      <input id="gov-swarm-osv-snapshot" type="text" autocomplete="off" spellcheck="false"
        placeholder="work/audit/osv.json（可选）" aria-label="蜂群 OSV 快照">
    </label>
    <button class="gov-wb-button strong" type="submit">发起蜂群</button>
  </form>
  <p class="gov-swarm-hint">每个目标一条独立 run，计入审计员今日预算；取消只停止排队，已在跑的会跑完。</p>`;
}

export function govSwarmCardHtml(swarm) {
  const counts = swarm.counts || {};
  const status = String(swarm.status || "running");
  const total = Number(counts.total || 0);
  const finished = Number(counts.finished || 0);
  const done = Number(counts.done || 0);
  const failed = Number(counts.failed || 0);
  const rejected = Number(counts.rejected || 0);
  const running = Number(counts.running || 0);
  const queued = Number(counts.queued || 0);
  const percent = total ? Math.round((finished * 100) / total) : 0;
  const cancel = status === "running" || status === "queued"
    ? `<button class="gov-wb-button quiet" type="button" data-gov-swarm-cancel="${esc(swarm.id)}">取消排队</button>`
    : "";
  const retry = failed + rejected > 0
    ? `<button class="gov-wb-button quiet" type="button" data-gov-swarm-retry="${esc(swarm.id)}">重试失败</button>`
    : "";
  const runs = (swarm.runs || []).map(run => {
    const runStatus = String(run.status || "queued");
    const label = RUN_STATUS[runStatus] || runStatus;
    const meta = run.status === "done"
      ? `${num(run.findings || 0)} 发现 · ${num(run.evidence || 0)} 证据`
      : run.error ? String(run.error).slice(0, 90) : "";
    const locate = run.audit_id
      ? `<button class="gov-swarm-link" type="button" data-gov-hist="${esc(run.audit_id)}">工作台</button>`
      : "";
    return `<div class="gov-swarm-run is-${esc(runStatus)}" data-gov-swarm-run="${esc(run.run_id)}">
      <span class="gov-swarm-dot" aria-hidden="true"></span>
      <b>${esc(targetLabel(run.target))}</b>
      <span class="gov-swarm-run-state">${esc(label)}</span>
      <span class="gov-swarm-run-meta">${esc(meta)}</span>
      ${locate}
    </div>`;
  }).join("");
  return `<article class="gov-swarm-card" data-swarm-id="${esc(swarm.id)}">
    <header class="gov-swarm-card-head">
      <div class="gov-swarm-title">
        <b>${esc(swarm.name || "蜂群")}</b>
        <span class="gov-swarm-state is-${esc(status)}">${esc(SWARM_STATUS[status] || status)}</span>
      </div>
      <div class="gov-swarm-actions">${retry}${cancel}</div>
    </header>
    <div class="gov-swarm-progress" role="progressbar" aria-valuemin="0"
      aria-valuemax="${num(total)}" aria-valuenow="${num(finished)}">
      <span style="width:${percent}%"></span>
    </div>
    <div class="gov-swarm-meta">${num(finished)} / ${num(total)} 完成
      · ${num(running)} 执行中 · ${num(queued)} 排队${failed ? ` · ${num(failed)} 失败` : ""}${
        rejected ? ` · ${num(rejected)} 已取消` : ""}</div>
    <div class="gov-swarm-runs">${runs || '<div class="gov-swarm-empty">还没有目标</div>'}</div>
  </article>`;
}

export function govSwarmListHtml(swarms) {
  const list = swarms || [];
  if (!list.length) {
    return `<div class="gov-empty-state"><b>还没有蜂群</b><span>把多个目标排成一次批量审计，宿主会按并发启动。</span></div>`;
  }
  return `<div class="gov-swarm-list">${list.map(govSwarmCardHtml).join("")}</div>`;
}
