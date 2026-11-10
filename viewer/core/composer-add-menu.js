import { esc } from "./format.js";

/* Chat composer 的 “+” 菜单：只回答“这条消息还能加什么”。
   文件、工作区引用是两类上下文；写操作/工具审批不在这里，避免把 + 变成设置页。 */
export function composerAddMenuHtml({ attachmentCount = 0, workspaceOpen = false, error = "" } = {}) {
  const full = attachmentCount >= 3;
  return `<div class="add-menu-head"><span>添加内容</span><span class="add-menu-count">${attachmentCount}/3</span></div>
    <button type="button" class="add-menu-item" data-add-action="files"${full ? " disabled" : ""}>
      <span class="add-menu-ic" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 12.5 15.5 5a3.5 3.5 0 0 1 5 5L10 20.5a5.5 5.5 0 0 1-7.8-7.8L9.8 5"/></svg></span>
      <span class="add-menu-copy"><span class="add-menu-title">上传文本文件</span><span class="add-menu-desc">${full ? "已达 3 个上限" : "≤64 KiB · 文本文件"}</span></span>
    </button>
    <button type="button" class="add-menu-item" data-add-action="workspace">
      <span class="add-menu-ic add-menu-at" aria-hidden="true">@</span>
      <span class="add-menu-copy"><span class="add-menu-title">引用工作区文件</span><span class="add-menu-desc">只读 · 以附件形式加入</span></span>
    </button>
    ${workspaceOpen ? `<form class="add-menu-form" data-add-workspace-form>
      <input type="text" data-add-workspace-input placeholder="相对路径，如 src/app.js" autocomplete="off" spellcheck="false" aria-label="工作区文件路径">
      <button type="submit">添加</button>
    </form>${error ? `<div class="add-menu-error">${esc(error)}</div>` : ""}` : ""}
    <div class="add-menu-foot">文本会折进消息；不会执行工具。</div>`;
}