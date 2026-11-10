/* Governance workbench projections: targets, summary, findings table, detail.
 *
 * Pure functions only. The server remains the fact source (bundle list +
 * bundle detail); this module decides how those facts are grouped and shown.
 */

import { esc, num } from "./format.js";
import { DISPOSITION_LABELS } from "./gov-cards.js";

const SEVERITY_LABEL = { high: "高", medium: "中", low: "低" };
const SEVERITY_ORDER = { high: 0, medium: 1, low: 2 };

export function targetKey(entry) {
  return String((entry && (entry.target || entry.dir)) || "");
}

export function targetLabel(entry) {
  const target = targetKey(entry).replace(/[\\/]+$/, "");
  return target.split(/[\\/]/).filter(Boolean).pop() || target || "审计记录";
}

/* One row per target: latest run wins, older runs only increment the count. */
export function aggregateTargets(bundles) {
  const byKey = new Map();
  for (const entry of bundles || []) {
    const key = targetKey(entry);
    if (!key) continue;
    const current = byKey.get(key);
    if (!current) {
      byKey.set(key, {
        key,
        label: targetLabel(entry),
        target: String(entry.target || ""),
        dir: String(entry.dir || ""),
        auditId: String(entry.audit_id || entry.dir || ""),
        runs: 1,
        findings: Number(entry.findings || 0),
        severity: entry.severity || {},
        unhandled: Number(entry.unhandled || 0),
        regression: entry.regression || null,
      });
      continue;
    }
    current.runs += 1;
    if (String(entry.dir || "") > current.dir) {
      Object.assign(current, {
        dir: String(entry.dir || ""),
        auditId: String(entry.audit_id || entry.dir || ""),
        findings: Number(entry.findings || 0),
        severity: entry.severity || {},
        unhandled: Number(entry.unhandled || 0),
        regression: entry.regression || null,
      });
    }
  }
  // 目标之间按最近审计日期排；同一天再按 dir 兜底。直接比 dir 会把
  // 目标 slug 的首字母当成时间顺序（flowcredit 永远排在 evox 前面）。
  const day = value => {
    const match = /(\d{4}-\d{2}-\d{2})/.exec(String(value || ""));
    return match ? match[1] : "";
  };
  return [...byKey.values()].sort((a, b) => {
    const byDay = day(b.dir).localeCompare(day(a.dir));
    return byDay || String(b.dir).localeCompare(String(a.dir));
  });
}

function severityBadge(severity) {
  const key = SEVERITY_LABEL[severity] ? severity : "low";
  return `<span class="gov-sev sev-${esc(key)}">${esc(SEVERITY_LABEL[key])}</span>`;
}

function dispositionLabel(value) {
  return DISPOSITION_LABELS[value] || value || "未处置";
}

export function filterFindings(findings, view = {}) {
  const query = String(view.query || "").trim().toLowerCase();
  return (findings || []).filter(finding => {
    if (view.severity && view.severity !== "all" && finding.severity !== view.severity) return false;
    if (view.unhandled && finding.operator_disposition) return false;
    if (!query) return true;
    return [finding.finding_id, finding.category, finding.root_cause_group]
      .some(value => String(value || "").toLowerCase().includes(query));
  }).sort((a, b) => {
    const sa = SEVERITY_ORDER[a.severity] ?? 9;
    const sb = SEVERITY_ORDER[b.severity] ?? 9;
    if (sa !== sb) return sa - sb;
    return String(a.finding_id || "").localeCompare(String(b.finding_id || ""));
  });
}

export function govSummaryHtml({ dir = "", bundle = null, findings = [] } = {}) {
  if (!dir || !bundle) {
    return `<div class="gov-empty-state"><b>还没有审计记录</b><span>输入目标路径，发起第一次离线审计。</span></div>`;
  }
  const target = bundle.target || {};
  const sessionRef = (((bundle.sessions || [])[0] || {}).session_ref) || "no-session";
  const counts = { high: 0, medium: 0, low: 0 };
  let unhandled = 0;
  for (const finding of findings) {
    const key = SEVERITY_LABEL[finding.severity] ? finding.severity : "low";
    counts[key] += 1;
    if (!finding.operator_disposition) unhandled += 1;
  }
  return `<section class="gov-target-head">
    <div class="gov-target-title">
      <p class="eyebrow">AUDIT TARGET</p>
      <h2>${esc(targetLabel({ target: target.identity, dir }))}</h2>
      <p class="gov-target-path">${esc(target.identity || dir)}</p>
    </div>
    <div class="gov-target-actions">
      <button class="gov-wb-button" type="button" data-gov-redig="${esc(dir)}">重新挖洞</button>
      <button class="gov-wb-button quiet" type="button" data-gov-chain="${esc(dir)}">验证链</button>
      <button class="gov-wb-button quiet" type="button" data-gov-open-bundle="${esc(dir)}">审计台 →</button>
    </div>
  </section>
  <div class="gov-summary-line">
    <span class="gov-mono">${esc(bundle.audit_id || dir)}</span>
    <span>${num(findings.length)} 发现</span>
    <span>高 ${num(counts.high)} · 中 ${num(counts.medium)} · 低 ${num(counts.low)}</span>
    <span>${num(unhandled)} 未处置</span>
    <span class="gov-dot-sep"></span>
    <span class="gov-summary-session">session <code>${esc(sessionRef)}</code></span>
  </div>`;
}

export function findingsToolbarHtml(view = {}, counts = {}) {
  const severity = view.severity || "all";
  const button = (key, label) =>
    `<button class="gov-filter${severity === key ? " active" : ""}" type="button" data-gov-sev="${key}" aria-pressed="${severity === key}">${label}</button>`;
  return `<div class="gov-findings-toolbar">
    <div class="gov-filters" role="group" aria-label="按严重度筛选">
      ${button("all", "全部")}${button("high", "高")}${button("medium", "中")}${button("low", "低")}
    </div>
    <label class="gov-unhandled"><input type="checkbox" data-gov-unhandled${view.unhandled ? " checked" : ""}> 只看未处置</label>
    <input class="gov-search" type="search" data-gov-search placeholder="搜索 finding / 类别 / 根因…" value="${esc(view.query || "")}" aria-label="搜索发现">
    <span class="gov-count" id="gov-findings-count">${num(counts.visible || 0)} / ${num(counts.total || 0)}</span>
  </div>`;
}

export function findingsTableHtml(dir, findings, view = {}, selected = "") {
  const list = filterFindings(findings, view);
  if (!list.length) {
    return `<div class="gov-empty-state"><b>没有匹配的发现</b><span>调整筛选，或对该目标重新挖洞。</span></div>`;
  }
  return `<div class="gov-findings-head" aria-hidden="true">
    <span>严重度</span><span>编号</span><span>类别</span><span>根因</span><span>处置</span><span></span>
  </div><div class="gov-findings-table" role="list">${list.map(finding => {
    const fid = String(finding.finding_id || "");
    const active = fid && fid === selected ? " active" : "";
    const disp = finding.operator_disposition ? dispositionLabel(finding.operator_disposition) : "未处置";
    return `<button class="gov-finding-row${active}" type="button" role="listitem" data-gov-finding="${esc(fid)}">
      ${severityBadge(finding.severity)}
      <code class="gov-row-id">${esc(fid)}</code>
      <span class="gov-row-cat">${esc(finding.category || "")}</span>
      <span class="gov-row-root">${esc(finding.root_cause_group || "")}</span>
      <span class="gov-disp-pill${finding.operator_disposition ? " set" : ""}">${esc(disp)}</span>
      <span class="gov-row-chev" aria-hidden="true">›</span>
    </button>`;
  }).join("")}</div>`;
}

export function findingDetailHtml(dir, finding, opts = {}) {
  if (!finding) {
    return `<div class="gov-detail-empty">选择一条发现，查看证据与处置。</div>`;
  }
  const fid = String(finding.finding_id || "");
  const options = ["", "accepted_risk", "false_positive_disputed", "wont_fix", "repaired"]
    .map(value => `<option value="${esc(value)}"${(finding.operator_disposition || "") === value ? " selected" : ""}>${esc(DISPOSITION_LABELS[value] || value)}</option>`)
    .join("");
  const refs = (finding.poc_refs || []).map(ref => `<code>${esc(ref)}</code>`).join(" ");
  return `<aside class="gov-detail-card" aria-label="发现详情">
    <header class="gov-detail-head">
      <div>${severityBadge(finding.severity)} <code>${esc(fid)}</code></div>
      <button class="gov-detail-close" type="button" data-gov-detail-close aria-label="关闭详情">×</button>
    </header>
    <div class="gov-detail-kicker">${esc(finding.category || "")}</div>
    <h3>${esc(finding.root_cause_group || "未命名根因")}</h3>
    <p class="gov-detail-note">${esc(finding.root_cause_note || "证据摘要见审计台；处置会写入签名链。")}</p>
    <div class="gov-detail-section">
      <span class="gov-detail-label">证据引用</span>
      <div class="gov-detail-refs">${refs || '<span class="dim">无 PoC 引用</span>'}</div>
    </div>
    <div class="gov-detail-section">
      <span class="gov-detail-label">操作员处置</span>
      <select class="gov-disp" data-dir="${esc(dir)}" data-finding="${esc(fid)}">${options}</select>
    </div>
    <div class="gov-detail-actions">
      ${opts.repairable === false ? "" : `<button class="gov-wb-button strong" type="button" data-gov-repair="${esc(fid)}" data-dir="${esc(dir)}">派 agent 修复</button>`}
      <button class="gov-wb-button quiet" type="button" data-gov-open-bundle="${esc(dir)}">在审计台查看证据 →</button>
    </div>
  </aside>`;
}