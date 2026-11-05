/* Chat message rendering: pure functions over one message projection.
 *
 * State (which message is being edited) is passed in, so the renderer can be
 * tested with the Node runner and cannot read a global by accident.
 *
 * `extraHtml` is the slot for what belongs to this answer but is rendered by
 * someone else — today the execution card (core/run-card.js). It sits inside
 * the message so the card hugs its answer instead of floating in the thread.
 */

import { esc, num } from "./format.js";
import { renderMarkdown } from "./chat-format.js";
import { aiAvatarHtml, avatarStateFor } from "./ai-avatar.js";
import { toolBlocksHtml } from "./tool-card.js";

export function chatMessageHtml(message, editingId = null, extraHtml = "") {
  const id = esc(message.id);
  const streaming = message.status === "streaming";
  if (message.role === "user") {
    if (editingId === message.id) {
      return `<div class="chat-msg user editing" data-msg-id="${id}">
        <div class="chat-edit-box">
          <textarea id="chat-edit-input" class="chat-edit-input">${esc(message.text)}</textarea>
          <div class="chat-msg-actions always">
            <button class="chat-act" type="button" data-chat-cancel>取消</button>
            <button class="chat-act strong" type="button" data-chat-save="${id}">保存并重新生成</button>
          </div>
        </div>
      </div>`;
    }
    const files = (message.attachments || [])
      .map(file => `<span class="chat-file">${esc(file.name || "")}</span>`).join("");
    return `<div class="chat-msg user" data-msg-id="${id}">
      <div class="chat-bubble">${esc(message.text)}${files ? `<div class="chat-files">${files}</div>` : ""}</div>
      <div class="chat-msg-actions">
        <button class="chat-act" type="button" data-chat-edit="${id}">编辑</button>
        <button class="chat-act" type="button" data-chat-delegate="${id}">转为委托</button>
      </div>
    </div>`;
  }
  const actions = [];
  if (!streaming) {
    actions.push(`<button class="chat-act" type="button" data-chat-copy="${id}">复制</button>`);
    if (message.status !== "error") {
      actions.push(`<button class="chat-act" type="button" data-chat-regen="${id}">重新生成</button>`);
    }
  }
  const reasoning = message.reasoning
    ? `<details class="chat-reasoning"><summary>思考过程</summary><pre>${esc(message.reasoning)}</pre></details>`
    : "";
  const error = message.error && message.error.message
    ? `<div class="chat-error">${esc(message.error.code || "CHAT_FAILED")} · ${esc(message.error.message)}</div>`
    : "";
  const wait = streaming && message.waiting
    ? `<div class="chat-wait">等待 ${esc(message.waiting)} 秒额度窗口…</div>`
    : "";
  const typing = streaming && !message.text
    ? `<span class="chat-typing" aria-label="正在生成"><i></i><i></i><i></i><i></i><i></i></span>`
    : "";
  const usage = message.usage && message.usage.total_tokens
    ? `<div class="chat-meta">${num(message.usage.total_tokens)} tokens · ${num(message.usage.cost_microusd)} µUSD${message.receipt_ref ? " · 已签收据" : ""}</div>`
    : "";
  const stopped = message.status === "cancelled" ? `<div class="chat-meta">已停止生成</div>` : "";
  // 对话 AI 的头像（core/ai-avatar.js）：状态由这条消息自己决定；
  // 只有锚定的运行真的 succeeded 才换成「已完成」的脸。
  const avatar = aiAvatarHtml(avatarStateFor(message), 28);
  // Agentic chat：有 blocks 的消息按「文本 → 工具卡 → 文本」顺序渲染；
  // 旧消息没有 blocks，保持单段 Markdown。
  const hasBlocks = Array.isArray(message.blocks) && message.blocks.length > 0;
  const body = hasBlocks
    ? toolBlocksHtml(message.blocks, renderMarkdown)
    : `<div class="chat-md">${renderMarkdown(message.text)}</div>`;
  return `<div class="chat-msg assistant${streaming ? " streaming" : ""}${message.status === "cancelled" ? " cancelled" : ""}" data-msg-id="${id}"${streaming ? ' data-streaming="1"' : ""}>
    <div class="chat-msg-row">${avatar}<div class="chat-blocks">${body}</div></div>
    ${wait}${typing}${reasoning}${error}${usage}${stopped}${extraHtml}
    <div class="chat-msg-actions">${actions.join("")}</div>
  </div>`;
}
