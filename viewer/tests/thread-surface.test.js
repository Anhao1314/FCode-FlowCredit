import test from "node:test";
import assert from "node:assert/strict";

import { createThreadSurface } from "../app/thread-surface.js";

/* 极简 DOM 桩：只覆盖 patchThread 用到的接口。
   断言"补丁语义"——未变条目复用节点、变化条目换新并保留展开态、
   消失条目被移除——而不是渲染结果本身（投影由 task-thread 测试覆盖）。 */

function fakeNode(key = "", attrs = {}) {
  return {
    dataset: { threadKey: key, ...attrs },
    querySelector: () => null,
    querySelectorAll: () => [],
    open: false,
    removed: false,
    replacedWith: null,
    remove() {
      this.removed = true;
      const c = this._container;
      if (c) c.children = c.children.filter(x => x !== this);
    },
    replaceWith(n) {
      this.replacedWith = n;
      const c = this._container;
      if (c) {
        n._container = c; // 与 DOM 一致:替换节点继承容器成员身份
        c.children.splice(c.children.indexOf(this), 1, n);
      }
    },
  };
}

function fakeContainer() {
  const container = {
    children: [],
    get firstElementChild() { return this.children[0] || null; },
    insertBefore(node, cursor) {
      node._container = this;
      // 与 DOM 一致:insertBefore 对已在容器内的节点是"移动"而不是复制
      const at = cursor ? this.children.indexOf(cursor) : -1;
      const cur = this.children.indexOf(node);
      if (cur >= 0) this.children.splice(cur, 1);
      const at2 = cursor ? this.children.indexOf(cursor) : -1;
      if (at2 < 0) this.children.push(node);
      else this.children.splice(at2, 0, node);
    },
  };
  return container;
}

function makeSurface(overrides = {}) {
  const calls = { describe: 0, resultBlockKind: 0, tplTitle: 0, renderMeters: 0,
    judgmentSurfaceHtml: 0, failureJudgmentHtml: 0, generalResultHtml: 0,
    runActionsHtml: 0, failureConclusion: 0, resultConclusion: 0 };
  const store = {};
  const surface = createThreadSurface({
    describe: () => { calls.describe += 1; return ["读取文件 a.md"]; },
    resultConclusion: () => { calls.resultConclusion += 1; return "毛利上升 120bp"; },
    failureConclusion: () => { calls.failureConclusion += 1; return "运行失败"; },
    resultBlockKind: () => { calls.resultBlockKind += 1; return "metric"; },
    renderMeters: () => { calls.renderMeters += 1; return "<div class='meters'></div>"; },
    judgmentSurfaceHtml: () => { calls.judgmentSurfaceHtml += 1; return "<section class='judgment'></section>"; },
    failureJudgmentHtml: () => { calls.failureJudgmentHtml += 1; return "<div class='fj'></div>"; },
    generalResultHtml: () => { calls.generalResultHtml += 1; return "<div class='gr'></div>"; },
    runActionsHtml: () => { calls.runActionsHtml += 1; return "<button>导出</button>"; },
    tplTitle: key => { calls.tplTitle += 1; return "通用委托"; },
    currentWorkTab: () => "activity",
    projectWork: {
      projectTabsHtml: () => "<div class='tabs'></div>",
      projectContextHtml: () => "<div class='ctx'></div>",
      projectResultHtml: () => "<div class='diff'></div>",
      projectFilesHtml: () => "<div class='files'></div>",
    },
    getStore: () => store,
    ...overrides,
  });
  return { surface, calls, store };
}

test("artifactsOf:results 数组过滤出带 path 的条目;单对象回退自身", () => {
  const { surface } = makeSurface();
  assert.deepEqual(
    surface.artifactsOf({ results: [{ path: "a.md" }, null, { nope: 1 }, { path: "b.md" }] }),
    [{ path: "a.md" }, { path: "b.md" }]);
  assert.deepEqual(surface.artifactsOf({ path: "only.md" }), [{ path: "only.md" }]);
  assert.deepEqual(surface.artifactsOf({}), []);
});

test("answerTextFor:项目任务/指标/文件产物/兜底 四路产品语言", () => {
  const { surface } = makeSurface();
  assert.match(surface.answerTextFor({
    task: { binding: { spec: { project: { path: "/repo" } } } },
    project: { filesChanged: [1, 2] },
  }), /已在隔离副本中完成修改，共变更 2 个文件/);
  assert.equal(
    surface.answerTextFor({ terminal: { result: { change_bp: 120 } } }),
    "毛利上升 120bp", "change_bp 走注入的 resultConclusion");
  assert.match(
    surface.answerTextFor({ terminal: { result: { results: [{ path: "a.md" }] } } }),
    /1 个文件通过 Runtime 文件状态复核/);
  assert.match(surface.answerTextFor({ terminal: { result: {} } }), /Runtime 独立校验/);
});

test("answerDetailFor:按 trace 形态路由到对应渲染器", () => {
  const { surface, calls } = makeSurface();
  assert.equal(surface.answerDetailFor({ terminal: { status: "failed" } }),
    "<div class='fj'></div>");
  assert.equal(surface.answerDetailFor({
    task: { binding: { spec: { project: { path: "/repo" } } } },
    terminal: { status: "succeeded" },
  }), "<div class='diff'></div>", "项目任务 → projectWork.projectResultHtml");
  assert.equal(
    surface.answerDetailFor({ terminal: { status: "succeeded", result: { change_bp: 1 } } }),
    "<section class='judgment'></section>");
  assert.equal(
    surface.answerDetailFor({ terminal: { status: "succeeded", result: { path: "x" } } }),
    "<div class='gr'></div>");
  assert.equal(surface.answerDetailFor({ terminal: { status: "succeeded", result: {} } }), "");
  assert.equal(calls.failureJudgmentHtml, 1);
  assert.equal(calls.judgmentSurfaceHtml, 1);
  assert.equal(calls.generalResultHtml, 1);
});

test("threadOptions:预算/场景/结论钩子全部路由到注入的 app 渲染器", () => {
  const { surface, calls } = makeSurface();
  const trace = {
    task: { objective: "核对口径", binding: { scenario: "general" } },
    rounds: [{ budget_after: { calls_used: 3, cost_used_microusd: 40,
      limits: { max_calls: 10 } } }], events: [],
    terminal: { status: "succeeded", result: {} },
  };
  const options = surface.threadOptions(trace, { running: true });
  assert.equal(options.planEditable, true);
  assert.equal(options.scenarioLabel("general"), "通用委托");
  assert.ok(options.budgetHtml.includes("meters"), "有预算 → renderMeters 注入");
  assert.equal(options.answerText(trace), surface.answerTextFor(trace));
  assert.equal(options.blockKindOf({}), "metric");
  assert.equal(calls.renderMeters, 1);
  // 无预算 → budgetHtml 为空且不触发 renderMeters
  const options2 = surface.threadOptions({ rounds: [], events: [] }, null);
  assert.equal(options2.budgetHtml, "");
  assert.equal(calls.renderMeters, 1);
});

test("liveStateOf:running/finished/pending/deciding/answering/error 从会话读取", () => {
  const { surface } = makeSurface();
  const terminal = { status: "failed" };
  const state = surface.liveStateOf({
    running: true, pending: { id: "p1" }, deciding: { id: "p1" },
    answering: true, lastError: "boom", trace: { terminal: {} },
  });
  assert.deepEqual(state, {
    running: true, finished: false, pending: { id: "p1" }, deciding: { id: "p1" },
    answering: true, error: "boom", terminal: {},
  });
  const done = surface.liveStateOf({ running: true, trace: { terminal } });
  assert.equal(done.finished, true, "trace 有终态即视为结束");
  assert.deepEqual(surface.liveStateOf(null).running, false);
});

test("threadCtx:审批文案按档位与 preview 形态路由", () => {
  const { surface } = makeSurface();
  const ctx = surface.threadCtx();
  assert.match(ctx.approvalWhy({ policy: "every-step" }), /逐步审批/);
  assert.match(ctx.approvalWhy({ policy: "consequential" }), /关键步骤/);
  assert.equal(ctx.approvalPreviewHtml({ tool: "read_file" }), "", "只读工具不展开影响区");
  assert.match(ctx.approvalPreviewHtml({ tool: "run_command", preview: { command: "ls" } }),
    /apv-cmdline/);
  assert.match(ctx.approvalPreviewHtml({ tool: "write_file", preview: { diff: "+x", additions: 1, removals: 0 } }),
    /diff-line add/);
  assert.match(ctx.approvalPreviewHtml({ tool: "working_paper_append", arguments: { target: "wp" } }),
    /apv-impact/);
  assert.equal(ctx.isWriteLike("write_file"), true);
});

test("patchThread:未变条目复用节点,变化条目换新且保留用户展开态,消失条目移除", () => {
  const { surface } = makeSurface();
  globalThis.document = { createElement: () => {
    const holder = { firstElementChild: null };
    Object.defineProperty(holder, "innerHTML", {
      set(html) {
        // 与真实 DOM 对齐:节点身份由 html 里的 data-thread-key 决定
        const m = /data-thread-key="([^"]*)"/.exec(html);
        const node = fakeNode(m ? m[1] : "");
        node.html = html;
        holder.firstElementChild = node;
      },
    });
    return holder;
  } };
  const container = fakeContainer();
  const cache = new Map();
  const mkItems = text => [
    { key: "op-1", kind: "operator", text, round: 1 },
    { key: "op-2", kind: "operator", text: "固定", round: 2 },
  ];

  // 首次:两节点按序进入容器
  surface.patchThread(container, mkItems("第一次"), surface.threadCtx(), cache);
  assert.equal(container.children.length, 2);
  const [n1, n2] = container.children;
  assert.equal(cache.get("op-1").includes("第一次"), true);

  // 第二次:内容没变 → 复用原节点,无 replaceWith/remove
  surface.patchThread(container, mkItems("第一次"), surface.threadCtx(), cache);
  assert.equal(container.children.length, 2);
  assert.ok(container.children[0] === n1 && container.children[1] === n2, "节点身份保持");
  assert.equal(n1.replacedWith, null);
  assert.equal(n1.removed, false);

  // 第三次:k1 文本变化 → 换新节点;用户展开态( dataset.user/open )被搬进新节点
  n1.dataset.user = "1";
  n1.dataset.open = "true";
  surface.patchThread(container, mkItems("第二次"), surface.threadCtx(), cache);
  const fresh = n1.replacedWith;
  assert.ok(fresh, "变化的条目生成了新节点");
  assert.equal(fresh.dataset.open, "true", "用户展开状态跨补丁保留");
  assert.equal(fresh.dataset.user, "1");
  assert.ok(fresh.html.includes("第二次"), "新节点承载新内容");

  // 第四次:只留 k2 → k1 系(已被替换的旧节点)不再参与,k2 复用
  surface.patchThread(container, [mkItems("第二次")[1]], surface.threadCtx(), cache);
  assert.equal(container.children.length, 1);
});

test("paintThread:容器缺失返回 null;正常路径投影进容器并把缓存落到 store", () => {
  const { surface, store } = makeSurface();
  globalThis.document = { getElementById: () => null, createElement: () => ({}) };
  assert.equal(surface.paintThread(null, { rounds: [] }), null, "无容器直接返回");
  assert.equal(surface.paintThread(null, null), null, "无 trace 直接返回");

  const container = fakeContainer();
  let holderNode = null;
  globalThis.document = {
    getElementById: id => (id === "thread" ? container : null),
    createElement: () => {
      const holder = { firstElementChild: null };
      Object.defineProperty(holder, "innerHTML", {
        set(html) { holderNode = fakeNode(); holderNode.html = html; holder.firstElementChild = holderNode; },
      });
      return holder;
    },
  };
  const trace = {
    task: { objective: "核对口径", binding: { scenario: "general" } },
    rounds: [], events: [],
    terminal: { status: "succeeded", result: {} },
  };
  const items = surface.paintThread(null, trace);
  assert.ok(Array.isArray(items) && items.length >= 2, "至少投影出 user + status 条目");
  assert.ok(store.threadCache instanceof Map, "回放缓存落在 store");
  assert.ok(store.threadCache.size >= 2);
});

test("threadCacheFor:会话缓存与会话缺失时的 store 缓存各自独立", () => {
  const { surface, store } = makeSurface();
  const s = {};
  const a = surface.threadCacheFor(s);
  a.set("x", "1");
  assert.equal(surface.threadCacheFor(s), a, "同一会话拿到同一份缓存");
  const b = surface.threadCacheFor(null);
  assert.notEqual(a, b);
  assert.equal(surface.threadCacheFor(null), b, "store 缓存幂等");
  assert.equal(store.threadCache, b);
});

test("projectRunHtml:项目任务三视图按当前 tab 展开,线程挂在 activity 面内", () => {
  const { surface } = makeSurface();
  const trace = { task: { id: "task-9" } };
  const html = surface.projectRunHtml(trace);
  assert.ok(html.includes('data-project-work="task-9"'));
  assert.ok(html.includes('data-work-pane="activity"'));
  assert.ok(html.includes('id="thread"'), "activity 面内挂任务线程");
  assert.ok(html.includes('id="changes-slot"'));
  assert.ok(html.includes('data-work-pane="files"'));
  const files = surface.projectRunHtml({ task: { id: "t" }, projectWorkspace: true });
  assert.ok(files.includes("files"), "有工作区时渲染文件面板内容");
});
