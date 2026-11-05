"use strict";

/* 对话内工具卡的纯渲染层。

   数据来自服务端消息的 `blocks`（类型见 chat.py 的投影）：
   - {type:"text", text}          → 普通 Markdown 段落
   - {type:"tool", call_id, name, arguments, round, status,
      result?, result_hash?, trust?, error?}
   渲染保持"证据优先"：工具名与参数直接可见，结果放在展开体里；
   status 三态（running/ok/error）是唯一的状态来源，不写形容词。 */

import { esc } from "./format.js";

const STATE_LABEL = { running: "运行中", ok: "完成", error: "失败" };

function parseArguments(text) {
  if (!text) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

/** 一行摘要：read_file 显示路径，ls_workspace 显示"工作区"，其余显示首个键值。 */
export function toolArgsSummary(name, argumentsText) {
  const args = parseArguments(argumentsText);
  if (!args) return String(argumentsText || "").slice(0, 60);
  if (name === "read_file" && typeof args.path === "string") return args.path;
  if (name === "ls_workspace") return "工作区文件";
  const first = Object.entries(args)[0];
  if (!first) return "";
  const [key, value] = first;
  const shown = typeof value === "string" ? value : JSON.stringify(value);
  return `${key}=${String(shown).slice(0, 50)}`;
}

function resultText(block) {
  if (block.error) {
    return `ERROR ${block.error.code || "TOOL_FAILED"}\n${block.error.message || ""}`;
  }
  if (block.result === undefined) return "";
  const text = JSON.stringify(block.result, null, 2);
  return text.length > 4000 ? `${text.slice(0, 4000)}\n…` : text;
}

const AWAITING_LABEL = { consequential: "关键审批", all: "逐步审批" };

function previewLines(preview) {
  const lines = [];
  if (typeof preview.diff === "string" && preview.diff) {
    lines.push(`<pre class="chat-tool-result">${esc(preview.diff)}</pre>`);
    if (preview.additions !== undefined) {
      lines.push(`<div class="chat-tool-meta">+${esc(String(preview.additions))} / −${esc(String(preview.removals ?? 0))}${preview.created ? " · 新建文件" : ""}${preview.truncated ? " · diff 已截断" : ""}</div>`);
    }
  }
  if (preview.argv) {
    const badge = preview.whitelist === "refused"
      ? '<span class="chat-tool-approval-badge refused">白名单外</span>'
      : (preview.readonly ? '<span class="chat-tool-approval-badge">可证只读</span>' : "");
    lines.push(`<div class="chat-tool-meta"><span class="chat-tool-cmd">${esc(preview.argv)}</span> ${badge}</div>`);
    if (preview.cwd) lines.push(`<div class="chat-tool-meta">cwd ${esc(preview.cwd)}</div>`);
    if (preview.whitelist === "refused" && preview.reason) {
      lines.push(`<div class="chat-tool-meta">将被拒绝：${esc(preview.reason)}</div>`);
    }
  }
  if (preview.url) {
    lines.push(`<div class="chat-tool-meta">域名 <span class="chat-tool-approval-badge">${esc(preview.host || "?")}</span> · ${esc(preview.url)}</div>`);
  }
  if (preview.path) lines.push(`<div class="chat-tool-meta">路径 ${esc(preview.path)}${preview.content_chars !== undefined ? ` · ${esc(String(preview.content_chars))} 字符` : ""}</div>`);
  if (preview.arguments) lines.push(`<pre class="chat-tool-result">${esc(JSON.stringify(preview.arguments, null, 2))}</pre>`);
  if (preview.preview_error) lines.push(`<div class="chat-tool-meta">预览失败：${esc(preview.preview_error)}</div>`);
  return lines;
}

function approvalHtml(block) {
  const approval = block.approval || {};
  const preview = approval.preview || {};
  const lines = previewLines(preview);
  const mode = AWAITING_LABEL[approval.mode] || "审批";
  return `<div class="chat-tool-approval">
    <div class="chat-tool-approval-head">${esc(mode)} · 这个调用需要你确认</div>
    ${lines.join("")}
    <div class="chat-tool-approval-actions">
      <button class="chat-act strong" type="button"
        data-tool-allow="${esc(block.call_id || "")}" data-tool-approval="${esc(approval.approval_id || "")}">允许</button>
      <button class="chat-act" type="button"
        data-tool-remember="${esc(block.call_id || "")}" data-tool-approval="${esc(approval.approval_id || "")}">允许并不再询问（本回合）</button>
      <button class="chat-act" type="button"
        data-tool-deny="${esc(block.call_id || "")}" data-tool-approval="${esc(approval.approval_id || "")}">拒绝</button>
    </div>
  </div>`;
}

export function toolCardHtml(block) {
  const status = block.status || "running";
  const label = STATE_LABEL[status] || status;
  const args = toolArgsSummary(block.name, block.arguments);
  const body = resultText(block);
  const meta = [
    block.result_hash ? `hash ${esc(String(block.result_hash).slice(0, 12))}…` : "",
    block.trust || "",
  ].filter(Boolean).join(" · ");
  const approval = status === "awaiting" ? approvalHtml(block) : "";
  return `<div class="chat-tool ${esc(status)}" data-tool-call="${esc(block.call_id || "")}">
    <div class="chat-tool-head">
      <span class="chat-tool-dot" aria-hidden="true"></span>
      <span class="chat-tool-name">${esc(block.name || "tool")}</span>
      <span class="chat-tool-args">${esc(args)}</span>
      <span class="chat-tool-state">${esc(label)}</span>
    </div>
    ${approval}
    ${body || meta ? `<details class="chat-tool-detail">
      <summary>参数与结果</summary>
      ${body ? `<pre class="chat-tool-result">${esc(body)}</pre>` : ""}
      ${meta ? `<div class="chat-tool-meta">${esc(meta)}</div>` : ""}
    </details>` : ""}
  </div>`;
}

/** blocks → HTML。文本块用 renderMarkdown 渲染（由调用方传入，保持纯函数）。 */
export function toolBlocksHtml(blocks, renderMarkdown) {
  return (blocks || []).map((block) => {
    if (!block || typeof block !== "object") return "";
    if (block.type === "tool") return toolCardHtml(block);
    return `<div class="chat-md">${renderMarkdown(String(block.text || ""))}</div>`;
  }).join("");
}