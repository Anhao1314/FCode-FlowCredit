"use strict";

/* 审批卡的纯渲染层（自 app.js 拆分切片 3a 迁入）。
 *
 * 输入是待批动作（tool 名 + preview/arguments），输出 HTML 字符串；
 * 不读全局、不碰会话状态——审批的会话侧接线仍在 app.js。
 * 层级对照官方 ApprovalDialog：动词化标题 + 影响区默认展开。
 */

import { esc } from "../core/format.js";

export function fmtCountdown(seconds) {
  const total = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

/* 审批动词映射：标题动词化（对照官方 ApprovalDialog 的 "Allow this write?"）。
   映射表集中定义；缺省回退工具名，不猜动作。 */
const APPROVAL_VERBS = {
  read_document: "读取",
  list_documents: "列出资料",
  normalize_units: "换算",
  reconcile_basis: "核对口径",
  check_restatement: "核对重述",
  calculate_margin: "计算毛利率",
  report_blocked: "宣告受阻",
  ls_workspace: "列出工作区",
  read_file: "读取文件",
  write_file: "写入文件",
  edit_file: "编辑文件",
  run_command: "执行命令",
  fetch_url: "抓取网页",
};

/* 有状态变更的写入类工具：审批默认展开影响区（对照官方 diff 自动展开）。
   edit_file / run_command 同属 consequential（能力基线三大件）。 */
const WRITE_TOOLS = new Set([
  "working_paper_append", "collect_source", "write_document", "write_file",
  "edit_file", "run_command", "browser_act",
]);
const WRITE_VERBS = {
  write_file: "写入", edit_file: "编辑", run_command: "执行", browser_act: "操作浏览器",
};

/* MCP 导入工具默认 consequential：未经主机标注 readonly 的都按写入对待 */
export function isWriteLike(tool) {
  return WRITE_TOOLS.has(tool) || String(tool).startsWith("mcp__");
}

export function approvalVerb(tool) {
  if (WRITE_VERBS[tool]) return WRITE_VERBS[tool];
  if (isWriteLike(tool)) return "写入";
  return APPROVAL_VERBS[tool] || "";
}

/* 注意：审批档位菜单另有同名概念的 approvalTitle(mode)，这里是待批动作标题 */
export function approvalActionTitle(tool) {
  const verb = approvalVerb(tool);
  return verb ? `允许这次${verb}？` : `允许这次 ${tool} 调用？`;
}

/* run_command 审批：完整命令行与治理要点（能力基线三大件） */
export function approvalCommandHtml(preview) {
  return `<div class="apv-impact">
    <div class="apv-impact-head">命令执行 · 白名单受限终端</div>
    <div class="apv-cmdline"><code>${esc(preview.command || "")}</code></div>
    <div class="kv">
      <span class="k">截止</span><span class="v">${esc(preview.timeout || 30)} 秒 · 超时记 unknown，不自动重跑</span>
      <span class="k">环境</span><span class="v">无 shell · 参数逐个校验 · provider 凭据不进入子进程</span>
      <span class="k">范围</span><span class="v">工作区沙箱内；文件变化全部进入 git 快照</span>
    </div>
  </div>`;
}

/* write_file / edit_file 审批：主机在执行前算好的真实 git diff，默认展开 */
export function approvalDiffHtml(preview) {
  if (preview.error) {
    return `<div class="apv-impact">
      <div class="apv-impact-head">写入影响 · 预览被沙箱拒绝</div>
      <div class="apv-diff"><div class="diff-line err">${esc(preview.error)}</div></div>
    </div>`;
  }
  const lines = String(preview.diff || "").split("\n").map(line => {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "ctx";
    return `<div class="diff-line ${cls}">${esc(line) || "&nbsp;"}</div>`;
  }).join("");
  const stat = `${preview.created ? "新建文件" : "修改文件"} · +${preview.additions} / −${preview.removals}${preview.truncated ? " · 已截断（完整 diff 在收据链上）" : ""}`;
  return `<div class="apv-impact">
    <div class="apv-impact-head">写入影响 · 真实 diff（${esc(stat)}）</div>
    <div class="apv-diff">${lines || '<div class="diff-line ctx">（无内容变化）</div>'}</div>
  </div>`;
}

/* 写入类审批的影响区：列出参数与影响对象，默认展开（对照官方 diff 自动展开） */
export function approvalImpactHtml(argsObj) {
  const rows = Object.entries(argsObj).filter(([, v]) => v !== undefined && v !== null && v !== "");
  const target = rows.find(([k]) => ["target", "doc_id", "document_id", "section", "path"].includes(k));
  const affected = target ? `${target[0]}=${target[1]}` : "本次调用声明的目标对象";
  return `<div class="apv-impact">
    <div class="apv-impact-head">写入影响 · 默认展开</div>
    <div class="kv">
      <span class="k">影响对象</span><span class="v">${esc(affected)}</span>
      ${rows.map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span>`).join("")}
    </div>
  </div>`;
}
