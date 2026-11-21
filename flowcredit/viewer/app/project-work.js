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

export function createProjectWork({ esc }) {
  const folderIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

  function projectAttachBody(project) {
    return `<div class="project-attach-form">
      <label>本地项目绝对路径<input type="text" data-project-path value="${esc(project.path)}" placeholder="/Users/you/code/my-project" spellcheck="false"></label>
      <label>独立验证命令<input type="text" data-project-verification value="${esc(project.verification)}" placeholder="npm test / pytest -q / cargo test" spellcheck="false"></label>
      <div class="attach-rule">复制到本次运行的隔离副本后修改；不会写回原目录。排除 .git、依赖、缓存、.env 与私钥；上限 2,000 文件 / 32 MiB。</div>
      <div class="attach-rule">支持白名单内的 Python / Node / Rust / Go / JVM / .NET / Ruby 等测试命令，无 shell。完整默认集见 commands.json 或 docs/general-capabilities.md。最终由 Runtime 在冻结副本上独立复跑，模型自己跑过不算验证。</div>
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
      <label>验证命令<input type="text" data-project-verification value="${esc(project.verification)}" placeholder="npm test / pytest -q / cargo test" spellcheck="false" autocomplete="off"></label>
      <div class="project-quick-foot"><span>完成后由 Runtime 独立复跑</span><button type="button" class="a-btn" data-project-context-done>完成</button></div>
    </div>`;
  }

  function projectContextHtml(trace) {
    const spec = trace?.task?.binding?.spec || {};
    if (!spec.project?.path) return "";
    const verification = spec.verification || {};
    const command = [verification.command, ...(verification.args || [])].filter(Boolean).join(" ");
    return `<div class="project-context"><span class="project-context-mark">${folderIcon}</span><b title="${esc(spec.project.path)}">${esc(spec.project.path.split("/").pop() || "项目")}</b><span class="project-context-path" title="${esc(spec.project.path)}">${esc(spec.project.path)}</span><code>${esc(command)}</code><span class="project-isolated">隔离副本</span></div>`;
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
      <div class="project-result-foot"><span>${esc(project.stat || "无统计变更")}</span><span class="grow"></span><button class="a-btn" data-project-apply${project.patchAvailable && patch ? "" : " disabled"} title="把补丁应用回原项目目录（先做冲突检测，失败不动原目录）">应用到原项目</button><button class="a-btn" data-project-patch${project.patchAvailable && patch ? "" : " disabled"}>下载 changes.patch</button></div>
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

  const MEDIA_TYPES = {
    gif: "image", png: "image", jpg: "image", jpeg: "image", webp: "image",
    mp4: "video", webm: "video",
  };
  const HTML_TYPES = new Set(["html", "htm"]);

  /* One quality block for both delivered and failed artifacts: the probe is
     the same deterministic evidence, only the surrounding verdict differs. */
  function qualityBlockHtml(quality, runId, { headless = false } = {}) {
    if (!quality) return "";
    const qualityIssues = Array.isArray(quality.issues) ? quality.issues.slice(0, 5) : [];
    const contact = quality.contact && runId
      ? `/api/runs/${encodeURIComponent(runId)}/quality-frame?name=${encodeURIComponent(quality.contact)}`
      : "";
    const coverage = quality.coverage;
    const covered = Array.isArray(coverage?.covered) ? coverage.covered.length : 0;
    const required = Array.isArray(coverage?.required) ? coverage.required.length : 0;
    const coverageHtml = coverage
      ? `<span>覆盖 ${esc(covered)}/${esc(required)}</span>`
      : "";
    const rigHtml = quality.hip_count !== undefined
      ? `<span>髋 ${esc(Number(quality.hip_count || 0))}</span>
         <span>腿长 ${esc(Number(quality.min_leg_length_px || 0))}px</span>
         <span>髋距 ${esc(Number(quality.max_leg_start_to_hip_px || 0))}px</span>`
      : "";
    const metrics = quality.frame_count || coverage
      ? `<div class="th-delivery-quality-metrics">
          ${coverageHtml}
          ${rigHtml}
          <span>脚-支撑 ${esc(Number(quality.max_foot_pedal_px || 0))}px</span>
          <span>腿/轮 ${esc(Number(quality.leg_wheel_overlap_px2 || 0))}px²</span>
          ${quality.frame_count ? `<span>${esc(Number(quality.frame_count || 0))} 帧</span>` : ""}
        </div>`
      : "";
    const qualityLabel = quality.status === "pass" ? "质量通过"
      : quality.status === "warn" ? "质量警告"
        : quality.status === "fail" ? "质量未通过"
          : quality.status === "unverified" ? "质量未验证" : "质量未知";
    const qualityScore = quality.status === "unverified"
      ? `未验证 · ${esc(covered)}/${esc(required)}`
      : `${esc(Number(quality.score || 0))}/100`;
    return `<div class="th-delivery-quality ${esc(quality.status || "")}" data-project-quality>
      ${headless ? "" : `<div class="th-delivery-quality-head">
        <b>${qualityLabel}</b>
        <span>${qualityScore}</span>
      </div>`}
      ${contact ? `<figure class="th-delivery-quality-frame"><a href="${esc(contact)}" target="_blank" rel="noopener"><img src="${esc(contact)}" alt="确定性帧采样：${esc(Number(quality.frame_count || 0))} 帧" loading="lazy"></a></figure>` : ""}
      ${metrics}
      ${qualityIssues.length ? `<ul>${qualityIssues.map(issue => `<li>${esc(issue)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }

  /* 交付物必须出现在对话里，而不是只藏在右侧 rail。代码任务通常只有
     文件列表；生图/生视频/截图任务还要能在最终回复中直接看到媒体。 */
  function projectDeliveryHtml(trace) {
    const changes = trace?.project?.filesChanged || [];
    if (!changes.length) return "";
    const runId = String(trace?.task?.id || "").replace(/^task-/, "");
    const media = changes
      .map(file => {
        const extension = String(file.path || "").split(".").pop().toLowerCase();
        const kind = MEDIA_TYPES[extension];
        return kind && runId ? { ...file, kind, extension } : null;
      })
      .filter(Boolean);
    const mediaHtml = media.map(file => {
      const url = `/api/runs/${encodeURIComponent(runId)}/workspace-media?path=${encodeURIComponent(file.path)}`;
      const label = `<span>${esc(file.path)}</span><small>${esc(file.status)}</small>`;
      if (file.kind === "video") {
        return `<figure class="th-delivery-item"><video class="th-delivery-media" controls preload="metadata" src="${esc(url)}"></video><figcaption>${label}</figcaption></figure>`;
      }
      return `<figure class="th-delivery-item"><a href="${esc(url)}" target="_blank" rel="noopener"><img class="th-delivery-media" src="${esc(url)}" alt="${esc(file.path)}" loading="lazy"></a><figcaption>${label}</figcaption></figure>`;
    }).join("");
    const htmlFiles = runId
      ? changes.filter(file => HTML_TYPES.has(String(file.path || "").split(".").pop().toLowerCase()))
      : [];
    const htmlHtml = htmlFiles.map(file =>
      `<button type="button" class="th-delivery-open" data-project-html="${esc(file.path)}" data-run-id="${esc(runId)}"><span>${esc(file.path)}</span><small>预览动画</small></button>`
    ).join("");
    const filesHtml = changes
      .filter(file => !media.some(item => item.path === file.path)
        && !htmlFiles.some(item => item.path === file.path))
      .slice(0, 12)
      .map(file => `<span class="th-delivery-file"><b>${esc(file.status)}</b>${esc(file.path)}</span>`)
      .join("");
    const qualityHtml = qualityBlockHtml(trace?.project?.quality, runId);
    return `<section class="th-delivery" data-project-delivery aria-label="交付物">
      <div class="th-delivery-head"><span>交付物</span><span>${changes.length} 个文件</span><span class="grow"></span><span class="th-delivery-isolated">隔离副本</span></div>
      ${mediaHtml ? `<div class="th-delivery-media-grid">${mediaHtml}</div>` : ""}
      ${htmlHtml ? `<div class="th-delivery-html">${htmlHtml}</div>` : ""}
      ${filesHtml ? `<div class="th-delivery-files">${filesHtml}</div>` : ""}
      ${qualityHtml}
    </section>`;
  }

  /* Failed/stopped runs still leave a real artifact on disk. Show what exists
     and what the last deterministic probe said, without pretending it is a
     delivered result. This is the failure surface the trace alone cannot give. */
  function projectArtifactStateHtml(trace) {
    if (!isProjectTrace(trace)) return "";
    const project = trace?.project || {};
    const quality = project.quality;
    const changes = Array.isArray(project.filesChanged) ? project.filesChanged : [];
    if (!quality && !changes.length) return "";
    const runId = String(trace?.task?.id || "").replace(/^task-/, "");
    const primary = changes.find(file => /(?:^|\/)index\.html$/.test(String(file.path || "")));
    const orderedChanges = primary ? [primary, ...changes.filter(file => file !== primary)] : changes;
    const files = orderedChanges.slice(0, 8).map(file => {
      const role = primary ? (file === primary ? "主产物" : "辅助文件") : (file.status || "文件");
      return `<span class="th-artifact-file"><b>${esc(role)}</b>${esc(file.path || "")}${primary ? `<small>${esc(file.status || "")}</small>` : ""}</span>`;
    }).join("");
    const more = changes.length > 8 ? `<span class="th-artifact-more">+${changes.length - 8}</span>` : "";
    const covered = Array.isArray(quality?.coverage?.covered) ? quality.coverage.covered.length : 0;
    const required = Array.isArray(quality?.coverage?.required) ? quality.coverage.required.length : 0;
    const qualityFacts = quality ? [
      quality.status === "unverified" ? "质量未检验" : `质量 ${Number(quality.score || 0)}/100`,
      quality.coverage ? `覆盖 ${covered}/${required}` : "",
    ].filter(Boolean) : [];
    const qualityStatusClass = ["fail", "warn", "unverified"].includes(quality?.status)
      ? quality.status : "";
    const qualityDetails = quality
      ? `<details class="th-artifact-quality ${qualityStatusClass}" data-artifact-quality>
          <summary><span class="th-artifact-quality-facts">${qualityFacts.map(fact => `<span>${esc(fact)}</span>`).join("")}</span><span class="th-artifact-quality-action">验收详情</span></summary>
          ${qualityBlockHtml(quality, runId, { headless: true })}
        </details>`
      : `<p class="th-artifact-state-note">尚未检查 · 未生成质量报告</p>`;
    return `<section class="th-artifact-state" data-artifact-state>
      <div class="th-artifact-state-head">
        <span class="th-artifact-state-kicker">产物状态</span>
        <b>${changes.length ? "文件已写入，未验收" : "文件未写入，未验收"}</b>
      </div>
      <p class="th-artifact-state-note">运行未完成；文件只写入隔离副本，当前产物尚未验收。</p>
      ${files ? `<div class="th-artifact-files">${files}${more}</div>` : ""}
      ${qualityDetails}
    </section>`;
  }

  return { projectAttachBody, projectComposerHtml, projectContextHtml, projectTabsHtml, projectOutcomeHtml, projectResultHtml, projectFilesHtml, projectDeliveryHtml, projectArtifactStateHtml, diffHtml };
}

export function projectDraftRows(project) {
  if (!project.path.trim()) return "";
  return `<div class="intake-row"><span>项目</span><b>${esc(project.path.split("/").pop() || "本地项目")}</b><span class="intake-scale-note">${esc(project.path)} · 只改隔离副本</span></div>
    <div class="intake-row"><span>验证</span><b>${esc(project.verification || "未填写")}</b><span class="intake-scale-note">最终由 Runtime 独立复跑</span></div>`;
}
