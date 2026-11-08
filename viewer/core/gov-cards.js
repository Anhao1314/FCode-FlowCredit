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
};

const DISPOSITION_VALUES = ["", "accepted_risk", "false_positive_disputed", "wont_fix"];

const SEVERITY_LABEL = { high: "高", medium: "中", low: "低" };

/* 一条发现 + 处置下拉。select 的 data-dir/data-finding 供 app.js 委托接线。 */
function findingRowHtml(dir, finding) {
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
      <select class="gov-disp" data-dir="${esc(dir)}" data-finding="${esc(fid)}">${options}</select></div>
  </div>`;
}

/* 发现处置卡：dir = audit 目录名（深链与处置 POST 都用它）。
   findings 为空时不渲染空卡——调用方跳过。 */
export function findingsCardHtml(dir, findings) {
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
      <button class="wcard-jump" type="button" data-gov-open-bundle="${esc(dir)}" title="在审计台查看证据、oracle 与签名链">查看证据 →</button></div>
    <div class="wcard-meta">${esc(meta)}</div>
    <div class="gov-finding-list">${list.map(finding => findingRowHtml(dir, finding)).join("")}</div>
  </div>`;
}
