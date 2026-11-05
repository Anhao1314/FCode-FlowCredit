"use strict";

import { esc } from "../core/format.js";

/* Real-project Work: command parsing and project-only presentation. */
export function parseVerificationCommand(text) {
  const input = String(text || "").trim();
  if (!input) return null;
  const parts = [];
  const pattern = /"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|(\S+)/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(input))) {
    if (match.index !== cursor) return null;
    const value = match[3] !== undefined ? match[3] : (match[1] ?? match[2] ?? "");
    parts.push(match[3] !== undefined ? value : value.replace(/\\([\\"'])/g, "$1"));
    cursor = pattern.lastIndex;
    if (input[cursor] && !/\s/.test(input[cursor])) return null;
    while (/\s/.test(input[cursor] || "")) cursor += 1;
    pattern.lastIndex = cursor;
  }
  if (!parts.length || !parts[0]) return null;
  return { name: `项目测试：${parts.join(" ")}`.slice(0, 200), command: parts[0], args: parts.slice(1), expectExitCode: 0, timeoutSeconds: 120 };
}

export function isProjectTrace(trace) {
  return !!trace?.task?.binding?.spec?.project?.path;
}

export function projectRunSpec(project) {
  const path = String(project?.path || "").trim();
  const verification = parseVerificationCommand(project?.verification);
  if (!path) throw new Error("请先选择本地项目");
  if (!verification) throw new Error("请填写项目独立验证命令，例如 pytest -q");
  return {
    mode: "open",
    project: { path },
    verification,
    reportPath: "FCODE_RESULT.md",
  };
}

export function directWorkRunSpec(project) {
  return String(project?.path || "").trim()
    ? projectRunSpec(project)
    : { mode: "open", reportPath: "FCODE_RESULT.md" };
}

export function createProjectWork({ esc }) {
  const folderIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

  function projectAttachBody(project) {
    return `<div class="project-attach-form">
      <label>本地项目绝对路径<input type="text" data-project-path value="${esc(project.path)}" placeholder="/Users/you/code/my-project" spellcheck="false"></label>
      <label>独立验证命令<input type="text" data-project-verification value="${esc(project.verification)}" placeholder="pytest -q" spellcheck="false"></label>
      <div class="attach-rule">复制到本次运行的隔离副本后修改；不会写回原目录。排除 .git、依赖、缓存、.env 与私钥；上限 2,000 文件 / 32 MiB。</div>
      <div class="attach-rule">支持白名单内的 Python / pytest / unittest / Node / Git 命令，无 shell。最终由 Runtime 在冻结副本上独立复跑，模型自己跑过不算验证。</div>
    </div>`;
  }

  function projectComposerHtml(project, editing = false) {
    const path = String(project.path || "").trim();
    const name = path.split("/").filter(Boolean).pop() || "添加项目（可选）";
    if (!editing) {
      return `<button class="project-context-button" type="button" data-project-context-edit aria-expanded="false" title="添加项目或更改验证命令">
        <span class="project-context-icon">${folderIcon}</span><span class="project-context-name">${esc(name)}</span>
        <span class="project-context-mode">${path ? "隔离副本" : "处理本地代码时添加"}</span><span class="project-context-chevron">⌄</span>
      </button>`;
    }
    return `<div class="project-quick-edit" role="dialog" aria-label="工作项目设置">
      <div class="project-quick-head"><b>工作项目</b><span>只修改隔离副本，不写回原目录</span></div>
      <label>项目绝对路径<input type="text" data-project-path value="${esc(project.path)}" placeholder="/Users/you/code/my-project" spellcheck="false" autocomplete="off"></label>
      <label>验证命令<input type="text" data-project-verification value="${esc(project.verification)}" placeholder="pytest -q" spellcheck="false" autocomplete="off"></label>
      <div class="project-quick-foot"><span>完成后由 Runtime 独立复跑</span><button type="button" class="a-btn" data-project-context-done>完成</button></div>
    </div>`;
  }

  function projectContextHtml(trace) {
    const spec = trace?.task?.binding?.spec || {};
    if (!spec.project?.path) return "";
    const verification = spec.verification || {};
    const command = [verification.command, ...(verification.args || [])].filter(Boolean).join(" ");
    return `<div class="project-context"><span class="project-context-mark">${folderIcon}</span><b>${esc(spec.project.path.split("/").pop() || "项目")}</b><span class="project-context-path" title="${esc(spec.project.path)}">${esc(spec.project.path)}</span><code>${esc(command)}</code><span class="project-isolated">隔离副本</span></div>`;
  }

  function projectTabsHtml(trace, active = "activity") {
    const count = trace.project?.filesChanged?.length || 0;
    return [["activity", "执行"], ["changes", `变更${count ? ` ${count}` : ""}`], ["files", "文件"]].map(([key, label]) =>
      `<button type="button" role="tab" data-work-tab="${key}" aria-selected="${active === key}" class="work-view-tab${active === key ? " active" : ""}">${label}</button>`
    ).join("");
  }

  function projectOutcomeHtml(trace) {
    const terminal = trace?.terminal || {};
    if (!terminal.status) return "";
    const succeeded = terminal.status === "succeeded";
    const count = trace.project?.filesChanged?.length || 0;
    const failure = terminal.failure || {};
    const message = failure.message || failure.code || "任务未能完成，请查看执行过程。";
    return `<section class="project-outcome ${succeeded ? "succeeded" : "failed"}" aria-label="任务结果">
      <div class="project-outcome-icon">${succeeded ? "✓" : "!"}</div>
      <div class="project-outcome-copy"><h2>${succeeded ? "任务已完成" : "任务未完成"}</h2><p>${succeeded ? `已在隔离副本中完成修改，并通过独立验证。${count ? `共变更 ${count} 个文件。` : "没有文件变更。"}` : esc(message)}</p></div>
      <div class="project-outcome-actions">${succeeded
        ? '<button type="button" data-work-tab="changes">查看变更</button><button type="button" data-work-tab="files">浏览文件</button>'
        : '<button type="button" class="a-btn" data-act="retry">重试任务</button>'}</div>
    </section>`;
  }

  function diffHtml(patch) {
    return String(patch || "").split("\n").slice(0, 4000).map(line => {
      const kind = line.startsWith("+") && !line.startsWith("+++") ? "add"
        : line.startsWith("-") && !line.startsWith("---") ? "del"
        : line.startsWith("@@") ? "hunk" : "ctx";
      return `<div class="project-diff-line ${kind}">${esc(line) || " "}</div>`;
    }).join("");
  }

  function projectResultHtml(trace) {
    const project = trace.project;
    if (!project) return `<div class="work-pane-empty"><b>代码变更会出现在这里</b><span>任务完成后，可逐行检查 Patch，再下载应用。</span></div>`;
    const changes = project.filesChanged || [];
    const patch = project.patch || "";
    return `<section class="card project-result" data-project-result="${esc(trace.task?.id || "")}">
      <div class="project-result-head"><div><span class="eyebrow">CODE REVIEW</span><h3>${changes.length} 个文件变更</h3></div><span class="project-isolated">原目录未修改</span></div>
      <div class="project-result-layout">
        <div class="project-file-list" aria-label="变更文件">${changes.length ? changes.map(file => `<button data-project-file="${esc(file.path)}"><span class="project-file-status">${esc(file.status)}</span><span>${esc(file.path)}</span></button>`).join("") : '<div class="attach-empty">没有代码变更</div>'}</div>
        <div class="project-diff">${patch ? diffHtml(patch) : '<div class="attach-empty">运行结束后可查看 Patch</div>'}</div>
      </div>
      <pre class="project-file-content" data-project-file-content hidden></pre>
      <div class="project-result-foot"><span>${esc(project.stat || "无统计变更")}</span><span class="grow"></span><button class="a-btn" data-project-patch${project.patchAvailable && patch ? "" : " disabled"}>下载 changes.patch</button></div>
    </section>`;
  }

  function projectFilesHtml(trace) {
    const workspace = trace.projectWorkspace;
    if (!workspace) return `<div class="work-pane-empty"><b>正在读取工作区…</b><span>文件来自本次任务的隔离副本。</span></div>`;
    const entries = workspace.entries || [];
    if (workspace.error) return `<div class="work-pane-empty"><b>无法读取工作区</b><span>${esc(workspace.error)}</span></div>`;
    const changed = new Map((trace.project?.filesChanged || []).map(file => [file.path, file.status]));
    return `<section class="card project-files" data-project-files="${esc(trace.task?.id || "")}">
      <div class="project-files-head"><div><b>任务工作区</b><span>${entries.length} 个文件 · 只读预览</span></div><span class="project-isolated">隔离副本</span></div>
      <div class="project-browser">
        <div class="project-file-list" aria-label="工作区文件">${entries.length ? entries.map(file => `<button data-project-file="${esc(file.path)}"><span class="project-file-status">${esc(changed.get(file.path) || "·")}</span><span>${esc(file.path)}</span><small>${esc(file.bytes)} B</small></button>`).join("") : '<div class="attach-empty">工作区还没有文件</div>'}</div>
        <pre class="project-file-content project-browser-content" data-project-file-content>选择左侧文件查看内容</pre>
      </div>
    </section>`;
  }

  return { projectAttachBody, projectComposerHtml, projectContextHtml, projectTabsHtml, projectOutcomeHtml, projectResultHtml, projectFilesHtml, diffHtml };
}

export function projectDraftRows(project) {
  if (!project.path.trim()) return "";
  return `<div class="intake-row"><span>项目</span><b>${esc(project.path.split("/").pop() || "本地项目")}</b><span class="intake-scale-note">${esc(project.path)} · 只改隔离副本</span></div>
    <div class="intake-row"><span>验证</span><b>${esc(project.verification || "未填写")}</b><span class="intake-scale-note">最终由 Runtime 独立复跑</span></div>`;
}
