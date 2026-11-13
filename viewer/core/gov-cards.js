"use strict";

/* G2 治理台发现处置卡：纯渲染层。
 *
 * 发现卡 = 一次挖洞的结果投影（bundle 落盘事实）：每条发现一行
 * （类别/严重度/根因）+ 操作员三档处置下拉。处置选择由 app.js 接线
 * （POST /api/audit/disposition/<dir>/<finding_id>），服务端原子写盘
 * 并落签名链——卡片只表达当前态，不改任何事实。
 *
 * DISPOSITION_LABELS 的单一来源从 audit-view.js 迁入这里（治理台是
 * 第三个消费方，标签归 core 层）；audit-view 转发导出保持旧路径。
 */

import { esc, num } from "./format.js";

/* 操作员处置的单一标签来源：审计台下拉、判断记录页、治理台发现卡共用。 */
export const DISPOSITION_LABELS = {
  "": "— 无 —",
  accepted_risk: "接受风险",
  false_positive_disputed: "误报争议",
  wont_fix: "不修复",
  repaired: "已修复",
};

const DISPOSITION_VALUES = ["", "accepted_risk", "false_positive_disputed", "wont_fix", "repaired"];

const SEVERITY_LABEL = { high: "高", medium: "中", low: "低" };

/* 一条发现 + 处置下拉 +（源码目标）派修复。select 的 data-dir/data-finding
   供 app.js 委托接线;修复按钮只在可修目标(非 artifact)上出现。 */
function findingRowHtml(dir, finding, opts) {
  const fid = finding.finding_id || "";
  const current = finding.operator_disposition || "";
  const severity = SEVERITY_LABEL[finding.severity] || finding.severity || "?";
  const options = DISPOSITION_VALUES.map(value =>
    `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(DISPOSITION_LABELS[value] || value)}</option>`
  ).join("");
  return `<div class="gov-finding" data-finding="${esc(fid)}">
    <div class="gov-finding-id"><span class="gov-sev sev-${esc(finding.severity || "low")}">${esc(severity)}</span>
      <b>${esc(fid)}</b><span class="gov-finding-cat">${esc(finding.category || "")}</span></div>
    <div class="gov-finding-root">${esc(finding.root_cause_group || "")}</div>
    <div class="gov-finding-disp"><span class="gov-disp-label">operator</span>
      <select class="gov-disp" data-dir="${esc(dir)}" data-finding="${esc(fid)}">${options}</select>
      ${opts && opts.repairable === false ? "" : `<button class="gov-repair" type="button" data-gov-repair="${esc(fid)}" data-dir="${esc(dir)}" title="把这条发现派给 Work 区的 agent 修复">派 agent 修复</button>`}</div>
  </div>`;
}

/* 发现处置卡：dir = audit 目录名（深链与处置 POST 都用它）。
   findings 为空时不渲染空卡——调用方跳过。 */
export function findingsCardHtml(dir, findings, opts) {
  const list = findings || [];
  if (!dir || !list.length) return "";
  const counts = list.reduce((acc, f) => {
    const key = f.severity || "low";
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const meta = `${list.length} 发现 · 高 ${num(counts.high || 0)} / 中 ${num(counts.medium || 0)} / 低 ${num(counts.low || 0)}`;
  return `<div class="wcard govcard gov-findings" data-thread-item="govcard">
    <div class="wcard-head"><span class="wcard-icon" data-doodle="shield-check"></span>
      <b class="wcard-title">发现 · ${esc(dir)}</b>
      <button class="wcard-jump" type="button" data-gov-run-by-audit="${esc(dir)}" title="在团队频道定位这次运行">频道 →</button>
      <button class="wcard-jump" type="button" data-gov-redig="${esc(dir)}" title="修复后重新挖洞:同目标再审计,回归对比看 fixed">重新挖洞 →</button>
      <button class="wcard-jump" type="button" data-gov-open-bundle="${esc(dir)}" title="在审计台查看证据、oracle 与签名链">查看证据 →</button></div>
    <div class="wcard-meta">${esc(meta)}</div>
    <div class="gov-finding-list">${list.map(finding => findingRowHtml(dir, finding, opts)).join("")}</div>
  </div>`;
}

/* G3 链验证卡：/链 <audit_dir> 的响应快照。状态语义与审计台一致：
   完整（kernel 逐事件重算）/ 断裂（篡改证据原文）/ 仅链接校验（弱校验，
   kernel 不可用的降级）/ 无会话日志。事件只列最近几条——全量在审计台。 */
export function chainCardHtml(dir, chain) {
  const events = Array.isArray(chain && chain.events) ? chain.events : [];
  const intact = !!(chain && chain.chain_intact);
  const verification = (chain && chain.verification) || "none";
  const stateClass = intact ? (verification === "link-only" ? "live" : "ok") : "bad";
  const stateText = !intact ? "链断裂"
    : verification === "link-only" ? "链完整（仅链接校验）"
    : verification === "none" ? "无会话日志"
    : "链完整";
  const tamper = !intact && chain.tamper
    ? `<div class="wcard-meta">${esc(String(chain.tamper).slice(0, 160))}</div>` : "";
  const tail = events.slice(-4).map((event, i) => {
    const seq = events.length - events.slice(-4).length + i + 1;
    const hash = event && event.hash ? String(event.hash).slice(0, 10) : "";
    const payload = event && event.payload && typeof event.payload === "object"
      ? Object.keys(event.payload).slice(0, 3).join(",") : "";
    return `<div class="wcard-row"><span class="wcard-run-label">${num(seq)} · ${esc((event && event.type) || "?")}${payload ? " · " + esc(payload) : ""}</span>
      <span class="wcard-run-meta">${esc(hash)}</span></div>`;
  }).join("");
  return `<div class="wcard govcard gov-chain" data-thread-item="govcard">
    <div class="wcard-head"><span class="wcard-icon" data-doodle="link"></span>
      <b class="wcard-title">签名链 · ${esc(dir)}</b>
      <button class="wcard-jump" type="button" data-gov-run-by-audit="${esc(dir)}" title="在团队频道定位这次运行">频道 →</button>
      <button class="wcard-jump" type="button" data-gov-open-bundle="${esc(dir)}" title="在审计台查看完整链与处置">审计台 →</button></div>
    <div class="gov-state-row"><span class="gov-state ${stateClass}">🔗 ${esc(stateText)}</span>
      <span class="wcard-run-meta">${num(events.length)} 事件 · ${esc(verification)}</span></div>
    ${tamper}
    ${tail ? `<div class="gov-finding-list">${tail}</div>` : ""}
  </div>`;
}

/* G3 判断卡：/判断 的响应快照——运行判断四类计数 + 审计处置三档,
   语义不同、并列展示不并计数(与判断记录页同一原则)。 */
export function judgmentsCardHtml(profile) {
  const p = profile || {};
  const counts = p.counts || {};
  const four = [
    ["接受判断", counts.accept], ["需要补材料", counts.needs_evidence],
    ["保留意见", counts.qualified], ["退回重做", counts.redo],
  ].map(pair => `${pair[0]} ${num(pair[1] ?? 0)}`).join(" · ");
  const recent = (Array.isArray(p.recent) ? p.recent : []).slice(0, 4);
  const judgeRows = recent.map(item => {
    const what = item.reason || item.note || item.objective || "";
    return `<div class="wcard-row"><span class="wcard-run-label">${esc(item.decision || item.decision_label || "—")}</span>
      <span class="wcard-run-meta">${esc(String(what).slice(0, 40))} ${esc(item.at || item.created || "")}</span></div>`;
  }).join("");
  const dispositions = (Array.isArray(p.audit_dispositions) ? p.audit_dispositions : [])
    .filter(item => item && item.disposition).slice(0, 4);
  const dispRows = dispositions.map(item => `<div class="wcard-row"><span class="wcard-run-label">${esc(DISPOSITION_LABELS[item.disposition] || item.disposition)}</span>
    <span class="wcard-run-meta">${esc(item.category || "")} · ${esc(item.finding_id || "")}</span></div>`).join("");
  const empty = !recent.length && !dispositions.length
    ? '<div class="wcard-empty">还没有判断记录——在运行的判断面里做出选择后开始积累。</div>' : "";
  return `<div class="wcard govcard gov-judgments" data-thread-item="govcard">
    <div class="wcard-head"><span class="wcard-icon" data-doodle="clipboard-list"></span>
      <b class="wcard-title">操作员判断</b>
      <button class="wcard-jump" type="button" data-gov-channel title="在团队频道查看运行流">频道 →</button>
      <button class="wcard-jump" type="button" data-open="reviews" title="打开判断记录页">判断记录 →</button></div>
    <div class="wcard-meta">${esc(four)}</div>
    ${empty}
    ${judgeRows ? `<div class="gov-finding-list">${judgeRows}</div>` : ""}
    ${dispRows ? `<div class="gov-finding-list gov-disp-list">${dispRows}</div>` : ""}
  </div>`;
}
