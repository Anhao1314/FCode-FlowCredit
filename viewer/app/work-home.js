"use strict";

import { workHomeHtml } from "../core/work-home.js";

/* Work 首页控制器：项目快照 + 运行列表 → 看板。
   只拥有这一页的展示状态；项目与运行的事实都在服务端，本地不缓存第二份。 */

const LEGACY_KEY = "fa.projects";
const LEGACY_DISMISSED = "fa.workLegacyDismissed";

export function createWorkHome({
  paint,
  onOpenRun,
  onNewRun,
  onLoaded = () => {},
  toast = () => {},
  fetchImpl = (...args) => fetch(...args),
  confirmImpl = message => window.confirm(message),
}) {
  const state = {
    projects: [],
    runs: [],
    selectedProjectId: "",
    creating: false,
    editingProject: null,
    error: "",
    loading: false,
  };
  let timer = null;
  let bound = false;

  function legacyCount() {
    if (localStorage.getItem(LEGACY_DISMISSED) === "1") return 0;
    try {
      const projects = JSON.parse(localStorage.getItem(LEGACY_KEY) || "[]");
      return Array.isArray(projects) ? projects.length : 0;
    } catch {
      return 0;
    }
  }

  function paintNow() {
    paint(workHomeHtml({
      projects: state.projects,
      runs: state.runs,
      selectedProjectId: state.selectedProjectId,
      creating: state.creating,
      editingProject: state.editingProject,
      error: state.error,
      legacyCount: legacyCount(),
    }));
  }

  async function json(path, options) {
    const response = await fetchImpl(path, {
      cache: "no-store",
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function load({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    if (!silent) {
      state.error = "";
      paintNow();
    }
    try {
      const [projectsPayload, runsPayload] = await Promise.all([
        json("/api/projects"),
        json("/api/runs"),
      ]);
      state.projects = projectsPayload.projects || [];
      state.runs = Array.isArray(runsPayload) ? runsPayload : [];
      if (state.selectedProjectId
          && state.selectedProjectId !== "__none__"
          && !state.projects.some(project => project.id === state.selectedProjectId)) {
        state.selectedProjectId = "";
      }
      onLoaded();
      if (document.getElementById("main")?.dataset.view === "work") paintNow();
    } catch (error) {
      state.error = "工作台加载失败：" + error.message;
      if (document.getElementById("main")?.dataset.view === "work") paintNow();
    } finally {
      state.loading = false;
    }
  }

  function open() {
    state.creating = false;
    state.editingProject = null;
    state.error = "";
    paint(workHomeHtml({
      projects: state.projects,
      runs: state.runs,
      selectedProjectId: state.selectedProjectId,
      creating: false,
      editingProject: null,
      error: "",
      legacyCount: legacyCount(),
    }));
    load();
    if (!timer) {
      timer = setInterval(() => {
        if (document.getElementById("main")?.dataset.view !== "work") return;
        if (state.creating || state.editingProject) return;
        load({ silent: true });
      }, 5000);
    }
  }

  async function submitProject(form) {
    const data = new FormData(form);
    const payload = {
      name: String(data.get("name") || "").trim(),
      verification: String(data.get("verification") || "").trim(),
    };
    const editing = state.editingProject;
    try {
      if (editing) {
        await json("/api/projects/update", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: editing.id, ...payload }),
        });
      } else {
        payload.path = String(data.get("path") || "").trim();
        const project = await json("/api/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        state.selectedProjectId = project.id;
      }
      state.creating = false;
      state.editingProject = null;
      await load();
      toast(editing ? "项目已保存" : "项目已创建");
    } catch (error) {
      state.error = error.message;
      paintNow();
    }
  }

  async function archiveProject(projectId) {
    const project = state.projects.find(item => item.id === projectId);
    if (!project) return;
    if (!confirmImpl(`归档项目「${project.name}」？历史运行会保留。`)) return;
    try {
      await json("/api/projects/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: projectId }),
      });
      if (state.selectedProjectId === projectId) state.selectedProjectId = "";
      state.editingProject = null;
      await load();
      toast("项目已归档");
    } catch (error) {
      state.error = error.message;
      paintNow();
    }
  }

  function startRun(projectId) {
    const project = state.projects.find(item => item.id === projectId);
    if (!project) return;
    if (!String(project.verification || "").trim()) {
      state.editingProject = project;
      state.creating = false;
      state.error = "这个项目还没有验证命令，先补一条再启动运行。";
      paintNow();
      return;
    }
    onNewRun(project);
  }

  function bind() {
    if (bound) return;
    bound = true;
    document.addEventListener("click", event => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (document.getElementById("main")?.dataset.view !== "work") return;
      if (target.closest("[data-work-new-project]")) {
        state.creating = true;
        state.editingProject = null;
        state.error = "";
        paintNow();
        return;
      }
      if (target.closest("[data-work-project-cancel]")) {
        state.creating = false;
        state.editingProject = null;
        paintNow();
        return;
      }
      if (target.closest("[data-work-refresh]")) {
        load();
        return;
      }
      if (target.closest("[data-work-legacy-dismiss]")) {
        localStorage.setItem(LEGACY_DISMISSED, "1");
        paintNow();
        return;
      }
      const filter = target.closest("[data-work-filter]");
      if (filter) {
        state.selectedProjectId = filter.dataset.workFilter || "";
        paintNow();
        return;
      }
      const openProject = target.closest("[data-project-open]");
      if (openProject) {
        state.selectedProjectId = openProject.dataset.projectOpen;
        paintNow();
        return;
      }
      const newRun = target.closest("[data-project-run]");
      if (newRun) {
        startRun(newRun.dataset.projectRun);
        return;
      }
      const edit = target.closest("[data-project-edit]");
      if (edit) {
        state.editingProject = state.projects.find(
          project => project.id === edit.dataset.projectEdit) || null;
        state.creating = false;
        state.error = "";
        paintNow();
        return;
      }
      const archive = target.closest("[data-project-archive]");
      if (archive) {
        archiveProject(archive.dataset.projectArchive);
        return;
      }
      const run = target.closest("[data-work-run]");
      if (run) {
        onOpenRun(run.dataset.workRun);
      }
    });
    document.addEventListener("submit", event => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (!form.matches("[data-work-project-form]")) return;
      event.preventDefault();
      submitProject(form);
    });
  }

  function beginCreate() {
    state.creating = true;
    state.editingProject = null;
    state.error = "";
    paintNow();
  }

  return { open, refresh: load, bind, state, beginCreate };
}