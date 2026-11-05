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

export function toolCardHtml(block) {
  const status = block.status || "running";
  const label = STATE_LABEL[status] || status;
  const args = toolArgsSummary(block.name, block.arguments);
  const body = resultText(block);
  const meta = [
    block.result_hash ? `hash ${esc(String(block.result_hash).slice(0, 12))}…` : "",
    block.trust || "",
  ].filter(Boolean).join(" · ");
  return `<div class="chat-tool ${esc(status)}" data-tool-call="${esc(block.call_id || "")}">
    <div class="chat-tool-head">
      <span class="chat-tool-dot" aria-hidden="true"></span>
      <span class="chat-tool-name">${esc(block.name || "tool")}</span>
      <span class="chat-tool-args">${esc(args)}</span>
      <span class="chat-tool-state">${esc(label)}</span>
    </div>
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