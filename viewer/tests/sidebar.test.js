import test from "node:test";
import assert from "node:assert/strict";

import { createSidebar } from "../app/sidebar.js";

/* 极简 DOM 桩：只实现 sidebar.js 用到的接口（getElementById/querySelector(All)、
   classList、dataset、style、addEventListener、localStorage）。
   这是 P2 技术债补的第一批侧栏 DOM 测试——此前只有纯函数测试。 */

function makeClassList(el) {
  const set = new Set();
  return {
    add: c => set.add(c),
    remove: c => set.delete(c),
    contains: c => set.has(c),
    toggle: (c, force) => {
      const want = force === undefined ? !set.has(c) : !!force;
      if (want) set.add(c); else set.delete(c);
      return want;
    },
    _set: set,
  };
}

function makeEl(init = {}) {
  const el = {
    hidden: false,
    innerHTML: "",
    scrollTop: 0,
    scrollWidth: 0,
    clientWidth: 0,
    dataset: {},
    listeners: {},
    style: {
      props: {},
      setProperty(k, v) { this.props[k] = v; },
      removeProperty(k) { delete this.props[k]; },
    },
    attributes: {},
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k]; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    querySelector(sel) { return (this.children && this.children[sel]) || null; },
    querySelectorAll() { return []; },
    ...init,
  };
  el.classList = makeClassList(el);
  return el;
}

function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    _store: store,
  };
}

function makeDoc(elements) {
  const all = [];
  const match = sel => {
    // 仅支持 ".nav-item[data-open]" 一类精确匹配
    if (sel === ".nav-item[data-open]") {
      return all.filter(el => el._navItem);
    }
    return [];
  };
  const doc = {
    body: makeEl(),
    documentElement: makeEl(),
    getElementById: id => elements[id] || null,
    querySelector: sel => {
      if (sel === ".sidebar") return elements.sidebar || null;
      return null;
    },
    querySelectorAll: sel => match(sel),
    createElement: () => makeEl(),
    _registerNavItems: items => { all.push(...items); },
  };
  return doc;
}

function harness(overrides = {}) {
  const els = {
    "side-nav-items": makeEl(),
    "more-nav": makeEl(),
    "nav-more": makeEl(),
    "chat-pinned": makeEl(),
    "side-quickstart": makeEl(),
    "main": makeEl({ dataset: { view: "reviews" } }),
    sidebar: makeEl(),
  };
  const doc = makeDoc(els);
  const navItems = [
    makeEl({ dataset: { open: "ledger" }, _navItem: true }),
    makeEl({ dataset: { open: "reviews" }, _navItem: true }),
    makeEl({ dataset: { open: "audit" }, _navItem: true }),
  ];
  doc._registerNavItems(navItems);

  globalThis.document = doc;
  globalThis.localStorage = makeLocalStorage();

  const state = {
    ledgerOpen: false,
    runs: [],
    threads: [],
    catalog: [],
    projects: [],
    counts: { ledger: 3 },
    pinned: new Set(),
    ...overrides,
  };

  const sidebar = createSidebar({
    renderChats() {},
    renderProjects() {},
    renderRuns() {},
    getRunHistory: () => state.runs,
    getProjects: () => state.projects,
    onNewProject: () => { state.newProjectRequested = true; },
    onAssignProject: (runId, projectId) => {
      state.assignments = [...(state.assignments || []), [runId, projectId]];
    },
    getChatThreads: () => state.threads,
    getModelCatalog: () => state.catalog,
    getLedgerOpen: () => state.ledgerOpen,
    getNavCounts: () => state.counts,
    isChatPinned: id => state.pinned.has(id),
    stateClass: status => (status === "succeeded" ? "ok" : "bad"),
    hydrateDoodles() {},
    getActiveKey: () => state.activeKey || null,
    doodleSvg: () => "<svg></svg>",
    tplTitle: key => key || "",
    runLabel: run => run.name || run.objective || run.id,
  });

  return { sidebar, els, doc, navItems, state, storage: globalThis.localStorage };
}

test("renderSidebarNav:功能导航已撤除,不再写 nav/more 宿主", () => {
  const { sidebar, els } = harness();
  sidebar.renderSidebarNav();
  // 导航宿主整体撤除:渲染不再写任何 nav 内容(治理台由区切换器直达)
  assert.equal(els["side-nav-items"].innerHTML, "", "nav 宿主不再被写入");
  assert.equal(els["more-nav"].innerHTML, "", "more-nav 不再被写入");
});

test("paintNavActive:普通视图按 data-open 高亮,账本特例只在 run 保留", () => {
  const { sidebar, els, navItems, state } = harness();
  // view=reviews：只有 reviews 高亮
  els.main.dataset.view = "reviews";
  sidebar.paintNavActive();
  assert.equal(navItems[1].classList.contains("active"), true);
  assert.equal(navItems[0].classList.contains("active"), false);

  // view=run + ledgerOpen：账本保持高亮（从账本点开运行的态）
  els.main.dataset.view = "run";
  state.ledgerOpen = true;
  sidebar.paintNavActive();
  assert.equal(navItems[0].classList.contains("active"), true);
  assert.equal(navItems[2].classList.contains("active"), false);

  // 离开账本进审计：账本不再高亮（此前一个 bug 就是这里）
  els.main.dataset.view = "audit";
  state.ledgerOpen = true;
  sidebar.paintNavActive();
  assert.equal(navItems[0].classList.contains("active"), false);
  assert.equal(navItems[2].classList.contains("active"), true);
});

test("toggleSection:折叠状态写 fa.sections 并投影 body[data-hide] 与 aria", () => {
  const { sidebar, doc, storage } = harness();
  const btn = makeEl({ dataset: { sectionToggle: "chats" } });
  const toggleBtn = { ...btn, setAttribute: btn.setAttribute };
  // querySelectorAll 固定返回该按钮
  doc.querySelectorAll = sel => (sel === "[data-section-toggle]" ? [toggleBtn] : []);

  sidebar.toggleSection(toggleBtn);
  assert.equal(doc.body.dataset.hide, "chats");
  assert.equal(toggleBtn.getAttribute("aria-expanded"), "false");
  assert.equal(JSON.parse(storage.getItem("fa.sections")).join(","), "chats");

  sidebar.toggleSection(toggleBtn);
  assert.equal(doc.body.dataset.hide, "");
  assert.equal(toggleBtn.getAttribute("aria-expanded"), "true");
});

test("renderPinned:只渲染对话置顶,空置顶不生成内容", () => {
  const { sidebar, els, state } = harness();
  state.threads = [{ id: "t1", title: "置顶对话" }];
  state.pinned = new Set(["t1"]);
  sidebar.renderPinned();
  assert.match(els["chat-pinned"].innerHTML, /置顶对话/);

  state.pinned = new Set();
  sidebar.renderPinned();
  assert.equal(els["chat-pinned"].innerHTML, "");
});

test("paintScrollMasks:scrollTop>1 才点亮顶部遮罩", () => {
  const { sidebar, doc } = harness();
  const mask = makeEl();
  const list = makeEl({ scrollTop: 0 });
  list.querySelector = sel => (sel === ".side-scroll-top-mask" ? mask : null);
  doc.querySelectorAll = sel => (sel === ".side-list" ? [list] : []);

  sidebar.paintScrollMasks();
  assert.equal(mask.classList.contains("is-visible"), false);

  list.scrollTop = 40;
  sidebar.paintScrollMasks();
  assert.equal(mask.classList.contains("is-visible"), true);
});
test("renderRuns:项目运行嵌在项目下，平坦列表只留未归属", () => {
  const { sidebar, els, state } = harness();
  const tasks = makeEl();
  els["chat-tasks"] = tasks;
  const now = new Date().toISOString();
  state.projects = [{ id: "prj-a", name: "项目甲" }];
  state.runs = [
    { id: "r1", status: "succeeded", created: now, template: "general", rounds: 2,
      objective: "A", pinned: false, projectId: "prj-a" },
  ];
  sidebar.renderRuns();
  assert.doesNotMatch(tasks.innerHTML, /r1/, "项目运行不再重复进平坦列表");
  assert.match(tasks.innerHTML, /未归属运行/);

  state.runs = [
    { id: "r2", status: "succeeded", created: now, template: "general", rounds: 1,
      objective: "未归属", pinned: false, projectId: "" },
  ];
  sidebar.renderRuns();
  assert.match(tasks.innerHTML, /r2/);
  assert.match(tasks.innerHTML, /今天/);

  // 没有项目时，平坦列表承载全部历史
  state.projects = [];
  state.runs = [{ id: "r3", status: "succeeded", created: now, template: "general",
    rounds: 1, objective: "全部", pinned: false, projectId: "" }];
  sidebar.renderRuns();
  assert.match(tasks.innerHTML, /r3/);
  assert.match(tasks.innerHTML, /任务/);

  state.runs = [];
  sidebar.renderRuns();
  assert.match(tasks.innerHTML, /任务会出现在这里/);
});

test("createProject/assignRunProject:侧栏表单与服务端动作", () => {
  const { sidebar, els, state } = harness();
  const host = makeEl();
  els["chat-projects"] = host;
  sidebar.createProject("项目乙");
  assert.match(host.innerHTML, /data-proj-form/, "新建项目在侧栏内联表单");
  state.runs = [{
    id: "r9", status: "succeeded", created: new Date().toISOString(),
    template: "general", rounds: 1, objective: "x", projectId: "prj-9",
  }];
  assert.equal(sidebar.getRunProject("r9"), "prj-9");
  sidebar.assignRunProject("r9", "prj-10");
  assert.deepEqual(state.assignments, [["r9", "prj-10"]]);
  assert.equal(localStorage.getItem("fa.projects"), null,
    "侧栏不再写本地项目标签");
});

test("renderProjects:过滤态与运行计数来自服务端快照", () => {
  const { sidebar, els, state } = harness();
  const host = makeEl();
  els["chat-projects"] = host;
  state.projects = [{ id: "prj-a", name: "含运行项目" }];
  state.runs = [
    { id: "r1", status: "succeeded", created: new Date().toISOString(),
      template: "general", rounds: 1, objective: "a", projectId: "prj-a" },
    { id: "r2", status: "running", created: new Date().toISOString(),
      template: "general", rounds: 1, objective: "b", projectId: "prj-a" },
  ];
  sidebar.renderProjects();
  assert.match(host.innerHTML, /含运行项目/);
  assert.match(host.innerHTML, />2</);
});
