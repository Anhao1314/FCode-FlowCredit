"use strict";

/* W1 工作区对话卡：/账本 /状态 /定时 三张命令卡的纯渲染层。
 *
 * 卡片是对话线程里的**本地视图**——以 role:"wcard" 的本地消息挂在
 * thread.messages 尾部：mergeServerThread 对未匹配本地消息保留原样，
 * 服务端从不回显，模型上下文也永远看不到（消息不经 POST）。
 * 密集页面（账本/定时任务）降级为卡内跳转的详情面，路由不删。
 *
 * 所有用户/模型可见文本经 esc；跳转按钮用 .wcard-jump + data-open，
 * 由 app.js 的 data-open → renderMode 一条路接线。
 */

import { esc, num } from "./format.js";

const SOURCE_LABEL = {
  manual: "手动",
  scheduled: "排程",
  subagent: "子代理",
};

function cardShell(icon, title, meta, rowsHtml, jumpOpen, jumpLabel) {
  return `<div class="wcard" data-thread-item="wcard">
    <div class="wcard-head"><span class="wcard-icon" data-doodle="${esc(icon)}"></span>
      <b class="wcard-title">${esc(title)}</b>
      <button class="wcard-jump" type="button" data-open="${esc(jumpOpen)}" title="${esc(jumpLabel)}">${esc(jumpLabel)} →</button></div>
    ${meta ? `<div class="wcard-meta">${meta}</div>` : ""}
    <div class="wcard-rows">${rowsHtml}</div>
  </div>`;
}

function runRow(run) {
  const label = run.name || run.objective || run.id;
  const cost = run.cost && run.cost.costMicrousd !== undefined ? `${num(run.cost.costMicrousd)} µ` : "";
  const rounds = run.rounds ? `${run.rounds} 轮` : "";
  const source = SOURCE_LABEL[run.source] || "";
  const bits = [rounds, cost, source].filter(Boolean).join(" · ");
  return `<div class="wcard-row"><span class="wcard-run-label">${esc(label)}</span>
    <span class="wcard-run-meta">${esc(bits)}</span></div>`;
}

/* 账本卡：总运行数 + 总成本 + 最近 5 条（created 倒序）。 */
export function ledgerCardHtml(runs, opts) {
  const list = (runs || []).slice();
  const totalCost = list.reduce((sum, run) => sum + ((run.cost && run.cost.costMicrousd) || 0), 0);
  const meta = list.length
    ? `${list.length} 次运行 · 累计 ${num(totalCost)} µUSD`
    : "";
  const recent = list
    .slice()
    .sort((a, b) => String(b.created || "").localeCompare(String(a.created || "")))
    .slice(0, 5);
  const rows = recent.length
    ? recent.map(runRow).join("")
    : '<div class="wcard-empty">还没有委托运行——在对话里描述任务，或「转为委托」。</div>';
  return cardShell("layout-grid", "工作区账本", meta, rows, "ledger", "完整账本");
}

/* 状态卡：进行中的运行（轮次 / 来源）。 */
export function statusCardHtml(runs) {
  const running = (runs || []).filter(run => run.status === "running");
  const rows = running.length
    ? running.map(runRow).join("")
    : '<div class="wcard-empty">当前没有进行中的运行。</div>';
  return cardShell("gauge", "进行中的运行", "", rows, "ledger", "看板 · 账本");
}

/* 定时卡：排程列表（目标 / 模板·审批 / 下次触发）。W1 只读——
   对话内创建排程留给后续切片，卡内跳定时任务页操作。 */
export function scheduledCardHtml(schedules) {
  const list = schedules || [];
  const rows = list.length
    ? list.map(t => `<div class="wcard-row"><span class="wcard-run-label">${esc(t.objective || "（未命名）")}</span>
      <span class="wcard-run-meta">${esc([t.next || `${t.frequency || ""} ${t.time || ""}`.trim(), t.enabled ? "" : "已暂停"].filter(Boolean).join(" · "))}</span></div>`).join("")
    : '<div class="wcard-empty">还没有排程——到定时任务页把重复的委托排上日程。</div>';
  return cardShell("calendar", "定时任务", "", rows, "scheduled", "定时任务页");
}
