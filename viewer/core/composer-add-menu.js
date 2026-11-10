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

/* Work composer 的 “+” 菜单：入口只做四类分流，细节仍落到
   attach-panel 的真实委托输入。顺序固定为「资料 → 项目 → 工具 → 设置」，
   避免把模板/预算和上下文混在同一层。 */
export function workAddMenuHtml({
  general = true,
  sourceCount = 0,
  scopeCount = 0,
  project = "",
  toolCount = 0,
  template = "",
  budget = "",
} = {}) {
  const sourceDesc = general
    ? `${Number(sourceCount) || 0}/5 个外部来源 · fetch_url 白名单`
    : (scopeCount ? `已限定 ${Number(scopeCount)} 份语料` : "全语料（默认）");
  const projectDesc = general
    ? (project ? project : "未选择 · 真实仓库的隔离副本")
    : "仅通用委托支持";
  return `<div class="add-menu-head"><span>添加内容</span><span class="add-menu-count">Work</span></div>
    <button type="button" class="add-menu-item" data-work-add="sources">
      <span class="add-menu-ic add-menu-at" aria-hidden="true">@</span>
      <span class="add-menu-copy"><span class="add-menu-title">资料与来源</span><span class="add-menu-desc">${esc(sourceDesc)}</span></span>
      <span class="add-menu-go" aria-hidden="true">›</span>
    </button>
    <button type="button" class="add-menu-item" data-work-add="project"${general ? "" : " disabled"}>
      <span class="add-menu-ic" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M3.5 7.5h6l1.8 2h9.2v9.5a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/><path d="M3.5 7.5V5.8A1.3 1.3 0 0 1 4.8 4.5h4.4l1.4 1.6"/></svg></span>
      <span class="add-menu-copy"><span class="add-menu-title">项目</span><span class="add-menu-desc">${esc(projectDesc)}</span></span>
      <span class="add-menu-go" aria-hidden="true">›</span>
    </button>
    <button type="button" class="add-menu-item" data-work-add="tools">
      <span class="add-menu-ic" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M14.5 6.5a4.5 4.5 0 0 0-5.8 5.8L4 17l3 3 4.7-4.7a4.5 4.5 0 0 0 5.8-5.8l-2.8 2.8-2.9-2.9z"/></svg></span>
      <span class="add-menu-copy"><span class="add-menu-title">工具</span><span class="add-menu-desc">${Number(toolCount) || 0} 个已注册工具 · 全部经门控</span></span>
      <span class="add-menu-go" aria-hidden="true">›</span>
    </button>
    <button type="button" class="add-menu-item" data-work-add="settings">
      <span class="add-menu-ic" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg></span>
      <span class="add-menu-copy"><span class="add-menu-title">更多设置</span><span class="add-menu-desc">${esc(template || "委托模板")} · ${esc(budget || "默认预算")}</span></span>
      <span class="add-menu-go" aria-hidden="true">›</span>
    </button>
    <div class="add-menu-foot">只影响本次委托；审批、预算与收据仍由主机守门。</div>`;
}