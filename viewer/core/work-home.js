"use strict";

import { esc } from "./format.js";

/* Work 首页的纯渲染层：项目卡 + 运行看板。
   不读 DOM、不发请求；控制器把服务端快照传进来，这里只负责把事实画对。
   所有动态文本必须过 esc；状态用文字与 data-*，不用 emoji。 */

const STATUS_TEXT = {
  running: "进行中",
  succeeded: "已完成",
  failed: "失败",
  denied: "拒绝",
  stopped: "已停止",
  recovery_required: "待恢复",
  error: "错误",
};

const ATTENTION_TEXT = {
  approval: "等待审批",
  recovery: "待恢复",
  failed: "失败",
  denied: "拒绝",
};

export function statusText(status) {
  return STATUS_TEXT[status] || status || "未知";
}

export function attentionText(run) {
  return ATTENTION_TEXT[run && run.attention] || "";
}

export function relativeTime(value, now = Date.now()) {
  const when = new Date(value || "");
  if (Number.isNaN(when.getTime())) return "";
  const seconds = Math.max(0, Math.round((now - when.getTime()) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} 天前`;
  return when.toISOString().slice(0, 10);
}

function costText(run) {
  const micro = Number(run && run.cost && run.cost.costMicrousd);
  if (!Number.isFinite(micro) || micro <= 0) return "";
  return `$${(micro / 1_000_000).toFixed(3)}`;
}

export function boardColumnOf(run) {
  if (!run) return "recent";
  if (run.status === "running") return "running";
  if (run.attention) return "attention";
  return "recent";
}

export function filterRuns(runs, selectedProjectId) {
  const list = Array.isArray(runs) ? runs : [];
  if (!selectedProjectId) return list;
  if (selectedProjectId === "__none__") {
    return list.filter(run => !run.projectId);
  }
  return list.filter(run => run.projectId === selectedProjectId);
}

export function projectStats(project, runs) {
  const mine = (runs || []).filter(run => run.projectId === project.id);
  return {
    runs: mine.length,
    running: mine.filter(run => run.status === "running").length,
    attention: mine.filter(run => !!run.attention).length,
    succeeded: mine.filter(run => run.status === "succeeded").length,
  };
}

function projectCardHtml(project, runs, selectedProjectId) {
  const stats = projectStats(project, runs);
  const active = project.id === selectedProjectId ? " is-active" : "";
  return '<article class="project-card' + active + '" data-project-card="' + esc(project.id) + '">'
    + '<button type="button" class="project-card-main" data-project-open="' + esc(project.id) + '">'
    + '<span class="project-card-name">' + esc(project.name) + "</span>"
    + '<span class="project-card-path mono" title="' + esc(project.path) + '">' + esc(project.path) + "</span>"
    + "</button>"
    + '<div class="project-card-stats">'
    + '<span><b>' + stats.runs + "</b> 运行</span>"
    + (stats.running ? '<span class="is-live"><b>' + stats.running + "</b> 进行中</span>" : "")
    + (stats.attention ? '<span class="is-attention"><b>' + stats.attention + "</b> 待处理</span>" : "")
    + (stats.succeeded ? '<span><b>' + stats.succeeded + "</b> 完成</span>" : "")
    + "</div>"
    + '<div class="project-card-actions">'
    + '<button type="button" class="project-card-run" data-project-run="' + esc(project.id) + '">新运行</button>'
    + '<button type="button" class="project-card-edit" data-project-edit="' + esc(project.id) + '">设置</button>'
    + "</div>"
    + "</article>";
}

export function projectFormHtml(project) {
  const editing = !!project;
  const title = editing ? "项目设置" : "新建项目";
  return '<form class="work-project-form" data-work-project-form>'
    + '<div class="work-form-head"><h2>' + title + "</h2><button type=\"button\" class=\"ghost\" data-work-project-cancel>取消</button></div>"
    + (editing ? '<p class="work-form-path mono">' + esc(project.path) + "</p>" : "")
    + '<label>名称<input name="name" required maxlength="80" value="' + esc(editing ? project.name : "") + '" placeholder="例如：flowcredit"></label>'
    + (editing ? "" : '<label>目录<input name="path" required placeholder="/absolute/path/to/project"></label>')
    + '<label>验证命令<input name="verification" maxlength="300" value="' + esc(editing ? project.verification : "") + '" placeholder="pytest -q（可留空，启动运行前再填）"></label>'
    + '<p class="work-form-hint">项目目录必须是绝对路径且存在；验证命令在独立终端执行，用于运行结束后的验收。</p>'
    + '<div class="work-form-actions">'
    + '<button type="submit" class="primary">' + (editing ? "保存" : "创建项目") + "</button>"
    + (editing ? '<button type="button" class="danger" data-project-archive="' + esc(project.id) + '">归档项目</button>' : "")
    + "</div>"
    + "</form>";
}

function runCardHtml(run, projectsById) {
  const project = run.projectId ? projectsById.get(run.projectId) : null;
  const label = run.name || run.objective || run.id;
  const meta = [
    project ? project.name : (run.projectId ? "未知项目" : "未归属"),
    run.rounds ? run.rounds + " 轮" : "",
    costText(run),
    relativeTime(run.created),
  ].filter(Boolean).join(" · ");
  const attention = attentionText(run);
  return '<button type="button" class="board-run" data-work-run="' + esc(run.id) + '" data-state="' + esc(boardColumnOf(run)) + '">'
    + '<span class="board-run-top">'
    + '<span class="board-run-name">' + esc(label) + "</span>"
    + '<span class="board-run-status">' + esc(attention || statusText(run.status)) + "</span>"
    + "</span>"
    + '<span class="board-run-meta">' + esc(meta) + "</span>"
    + "</button>";
}

function columnHtml(key, title, runs, projectsById) {
  return '<section class="board-col" data-board-col="' + key + '">'
    + '<h3>' + title + '<span class="board-col-count">' + runs.length + "</span></h3>"
    + (runs.length
      ? runs.map(run => runCardHtml(run, projectsById)).join("")
      : '<div class="board-empty">' + (key === "running" ? "没有进行中的运行"
        : key === "attention" ? "没有待处理事项" : "还没有已完成的运行") + "</div>")
    + "</section>";
}

export function boardHtml({ projects, runs, selectedProjectId }) {
  const list = filterRuns(runs, selectedProjectId);
  const projectsById = new Map((projects || []).map(project => [project.id, project]));
  const grouped = { running: [], attention: [], recent: [] };
  for (const run of list) grouped[boardColumnOf(run)].push(run);
  grouped.recent.sort((a, b) => String(b.created).localeCompare(String(a.created)));
  grouped.recent = grouped.recent.slice(0, 20);
  const filters = '<button type="button" class="board-filter' + (!selectedProjectId ? " is-active" : "") + '" data-work-filter="">全部</button>'
    + '<button type="button" class="board-filter' + (selectedProjectId === "__none__" ? " is-active" : "") + '" data-work-filter="__none__">未归属</button>'
    + (projects || []).map(project =>
      '<button type="button" class="board-filter' + (selectedProjectId === project.id ? " is-active" : "")
      + '" data-work-filter="' + esc(project.id) + '">' + esc(project.name) + "</button>").join("");
  return '<section class="work-board">'
    + '<header class="board-head"><div class="board-filters">' + filters + "</div>"
    + '<span class="board-total">' + list.length + " 个运行</span></header>"
    + '<div class="board-columns">'
    + columnHtml("running", "进行中", grouped.running, projectsById)
    + columnHtml("attention", "待处理", grouped.attention, projectsById)
    + columnHtml("recent", "最近完成", grouped.recent, projectsById)
    + "</div></section>";
}

export function workHomeHtml({
  projects = [],
  runs = [],
  selectedProjectId = "",
  creating = false,
  editingProject = null,
  error = "",
  legacyCount = 0,
} = {}) {
  const projectRows = projects.map(project =>
    projectCardHtml(project, runs, selectedProjectId)).join("");
  const selected = projects.find(project => project.id === selectedProjectId);
  return '<div class="work-home">'
    + '<header class="work-home-head">'
    + '<div><div class="work-kicker">WORK</div><h1>工作</h1>'
    + '<p>项目、运行与待处理事项，集中在一个工作台。</p></div>'
    + '<div class="work-home-actions">'
    + '<button type="button" class="ghost" data-work-refresh>刷新</button>'
    + '<button type="button" class="primary" data-work-new-project>新建项目</button>'
    + "</div></header>"
    + (error ? '<div class="work-home-error" role="alert">' + esc(error) + "</div>" : "")
    + (legacyCount
      ? '<div class="work-home-notice" data-work-legacy>'
        + "<span>检测到 " + legacyCount + " 个旧的本地项目标签。它们没有目录路径，已停用；请创建一个带目录的项目。</span>"
        + '<button type="button" class="ghost" data-work-legacy-dismiss>知道了</button>'
        + "</div>"
      : "")
    + (creating || editingProject ? projectFormHtml(editingProject) : "")
    + '<section class="work-projects" aria-label="项目">'
    + (projectRows || '<div class="work-empty"><h2>还没有项目</h2><p>创建一个项目，把运行按目录归档；项目和运行都会持久化在服务端。</p><button type="button" class="primary" data-work-new-project>新建项目</button></div>')
    + "</section>"
    + (selected ? '<div class="work-selected"><span class="work-selected-label">当前项目</span><b>' + esc(selected.name) + "</b>"
      + '<button type="button" class="ghost" data-work-filter="">查看全部</button></div>' : "")
    + boardHtml({ projects, runs, selectedProjectId })
    + "</div>";
}