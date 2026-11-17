import test from "node:test";
import assert from "node:assert/strict";

import { createIntake } from "../app/intake.js";

/* 委托入口的桩测试:clarifier 往返、快照回退、偏好持久化、
   草稿路由(通用/财务/项目)与开始委托的守卫。 */

function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    _store: store,
  };
}

function makeEl(init = {}) {
  return {
    hidden: false, innerHTML: "", textContent: "", value: "", placeholder: "",
    dataset: {}, disabled: false, listeners: {}, style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    focus() {}, addEventListener() {},
    ...init,
  };
}

function setup({ health = { live: true }, fetchImpl, tplKey = "full", templates,
                 project = "", verification = "", storage = null } = {}) {
  const els = {};
  const calls = { renderMain: [], renderMode: [], renderAttachPanel: 0, openLive: [],
    toast: [], autosize: 0, leaveChat: 0, setComposerRunning: [], refreshRuns: [],
    closeSettingsPage: [], clearProjectContext: 0 };
  const fetchCalls = [];
  const defaultFetch = async (url, init) => {
    fetchCalls.push({ url, body: init ? JSON.parse(init.body) : null });
    if (String(url).startsWith("/api/intake")) {
      const body0 = JSON.parse(init.body);
      return { ok: true, json: async () => ({
        reply: "收到，需要确认两件事。",
        thought: "理解任务",
        // 与真实 clarifier 一致:在已有 spec 上累积
        spec: { ...(body0.spec || {}), intent: "compare",
          ...(body0.choice && body0.choice.field === "output" ? { output: body0.choice.value } : {}) },
        choices: [{ field: "output", value: "memo", label: "一页结论", description: "审计底稿" }],
        task: null,
      }) };
    }
    return { ok: true, json: async () => ({ id: "run-new", template: "full" }) };
  };
  const intakeDeps = {
    APPROVAL_MODES: [["consequential", "关键审批"], ["all", "逐步审批"], ["off", "自动"]],
    store: { key: "chat:1" },
    attach: {
      open: true, project: { path: project, verification }, budget: null,
      sources: [], sourcesDraft: "", mcp: { servers: [], loaded: false },
      scope: { size: 0, clear() { this.size = 0; } }, corpus: [1, 2],
    },
    approvalTitle: m => ({ consequential: "关键审批", all: "逐步审批", off: "自动" }[m] || m),
    isGeneralTemplate: () => true,
    renderAttachPanel: () => { calls.renderAttachPanel += 1; },
    delegationSpecExtras: () => ({}),
    clearProjectContext: () => { calls.clearProjectContext += 1; },
    autosize: () => { calls.autosize += 1; },
    hydrateDoodles: () => {},
    renderMode: m => calls.renderMode.push(m),
    renderMain: (html, meta) => calls.renderMain.push({ html, meta }),
    stageEmptyHtml: () => "<div class='stage-empty'></div>",
    select: () => {},
    activeSession: () => null,
    openLive: (id, objective, template, model) => calls.openLive.push({ id, objective, template, model }),
    refreshRuns: opts => calls.refreshRuns.push(opts),
    setComposerRunning: v => calls.setComposerRunning.push(v),
    toast: m => calls.toast.push(m),
    getHealth: () => health,
    getTemplates: () => templates || [
      { key: "full", title: "完整研究：读年报并核对", limits: { max_calls: 12, max_reads: 20, max_cost_microusd: 20000 } },
    ],
    getTplKey: () => tplKey,
    getApprovalMode: () => "consequential",
    getModelChoice: () => ({ key: "kimi-k2.6", effort: "medium" }),
    getModelCatalog: () => [{ key: "kimi-k2.6", supportsEffort: true }],
    getModelBudget: () => ({ template: "", models: null }),
    setModelBudget: () => {},
    getSettingsOpen: () => false,
    closeSettingsPage: (...a) => calls.closeSettingsPage.push(a),
    leaveChat: () => { calls.leaveChat += 1; },
    fetchImpl: fetchImpl || defaultFetch,
  };
  globalThis.document = {
    getElementById: id => (els[id] ||= makeEl()),
    querySelector: sel => (sel === "[data-intake-start]" ? els["intake-start-btn"] || null : null),
    createElement: () => makeEl(),
  };
  globalThis.localStorage = storage || makeLocalStorage();
  const intake = createIntake(intakeDeps);
  els["composer-ta"] = makeEl();
  els["intake-start-btn"] = makeEl();
  return { intake, deps: intakeDeps, els, calls, fetchCalls };
}

test("startIntake:空文本进入引导消息并渲染 intake 面", () => {
  const { intake, calls } = setup();
  intake.startIntake("");
  assert.equal(intake.intake.active, true);
  assert.equal(calls.leaveChat, 1);
  assert.deepEqual(calls.renderMode, ["intake"]);
  intake.renderIntake();
  const html = calls.renderMain.at(-1).html;
  assert.match(html, /还差一点信息/, "通用模板的 intake 是任务确认");
  assert.match(html, /先说说你想做什么/, "把 clarifier 的追问放在确认页里");
  assert.doesNotMatch(html, /intake-msg/, "通用模板不再渲染聊天线程");
  assert.match(intake.intake.messages[0].text, /先说说你想做什么/);
});

test("renderIntake:通用委托的 readyTask 渲染任务引用与草稿，不渲染对话气泡", () => {
  const { intake, calls } = setup();
  intake.intake.active = true;
  intake.intake.messages = [
    { role: "user", text: "把 top_k_words 实现掉" },
    { role: "assistant", text: "已就绪。", choices: [] },
  ];
  intake.intake.readyTask = { objective: "把 top_k_words 实现掉", template: "full", approval: "off", spec: { mode: "script" } };
  intake.renderIntake();
  const html = calls.renderMain.at(-1).html;
  assert.match(html, /任务确认/);
  assert.match(html, /确认后开始/);
  assert.match(html, /把 top_k_words 实现掉/);
  assert.match(html, /intake-draft/);
  assert.doesNotMatch(html, /intake-msg/);
});

test("startIntake direct:任务就绪后自动创建运行，不显示聊天线程", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url: String(url), body: init && init.body ? JSON.parse(init.body) : null });
    if (String(url).startsWith("/api/intake")) {
      return { ok: true, json: async () => ({
        reply: "已就绪。",
        thought: "",
        spec: { mode: "script" },
        choices: [],
        task: { objective: "写个脚本", template: "full", approval: "off", spec: { mode: "script" } },
      }) };
    }
    return { ok: true, json: async () => ({ id: "run-direct", template: "full" }) };
  };
  const { intake, calls } = setup({ fetchImpl, tplKey: "full" });
  intake.startIntake("写个脚本", null, { direct: true });
  await new Promise(r => setTimeout(r, 750));
  const intakeCall = seen.find(entry => entry.url === "/api/intake");
  const runCall = seen.find(entry => entry.url === "/api/runs");
  assert.ok(intakeCall, "先走 clarifier");
  assert.ok(runCall, "就绪后创建运行");
  assert.deepEqual(calls.openLive[0], { id: "run-direct", objective: "写个脚本", template: "full", model: "kimi-k2.6" });
  assert.equal(intake.intake.active, false, "直接开跑后离开 intake");
  assert.ok(!calls.renderMain.some(entry => /正在准备任务|任务确认/.test(entry.html)), "direct 提交不渲染准备页或确认页");
  assert.ok(!calls.renderMode.includes("intake"), "direct 提交不切换到 intake 页");
});

test("startIntake direct:motion 任务无来源也立即创建运行，不出确认页", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url: String(url), body: init && init.body ? JSON.parse(init.body) : null });
    if (String(url).startsWith("/api/intake")) {
      return { ok: true, json: async () => ({
        reply: "动效任务已就绪。", thought: "", spec: { mode: "research" }, choices: [],
        task: {
          objective: "单文件 HTML/SVG/CSS 动画", template: "general-research",
          approval: "consequential",
          spec: { mode: "research", fastProfile: "motion-render" },
        },
      }) };
    }
    return { ok: true, json: async () => ({ id: "run-motion", template: "general-research" }) };
  };
  const { intake, calls } = setup({ fetchImpl, tplKey: "general-research" });
  intake.startIntake("单文件 HTML/SVG/CSS 动画", null, { direct: true });
  await new Promise(r => setTimeout(r, 750));
  assert.ok(seen.some(entry => entry.url === "/api/runs"), "motion 不受 URL 守卫拦截");
  assert.equal(calls.openLive.length, 1);
  assert.ok(!calls.renderMain.some(entry => /任务确认/.test(entry.html)), "不出确认页");
});

test("startIntake direct:open choices 后无来源也自动创建一次运行", async () => {
  let intakeCalls = 0;
  let runCalls = 0;
  const fetchImpl = async (url) => {
    if (String(url) === "/api/intake") {
      intakeCalls += 1;
      return { ok: true, json: async () => (intakeCalls === 1 ? {
        reply: "请选择。", thought: "", spec: { mode: "open" }, task: null,
        choices: [{ field: "delivery", value: "yes", label: "保留", description: "继续" }],
      } : {
        reply: "已就绪。", thought: "", spec: { mode: "open" }, choices: [],
        task: { objective: "开放编码任务", template: "full", approval: "off", spec: { mode: "open" } },
      }) };
    }
    if (String(url) === "/api/runs") runCalls += 1;
    return { ok: true, json: async () => ({ id: "run-open-direct", template: "full" }) };
  };
  const { intake, calls } = setup({ fetchImpl, tplKey: "full" });
  intake.startIntake("开放编码任务", null, { direct: true });
  await new Promise(r => setTimeout(r, 720));
  assert.equal(intakeCalls, 1);
  assert.equal(intake.intake.choices.length, 1);

  intake.intakeChoose("delivery", "yes", "保留");
  intake.intakeChoose("delivery", "yes", "保留");
  await new Promise(r => setTimeout(r, 800));
  assert.equal(intakeCalls, 2, "在途的重复选择必须被忽略");
  assert.equal(runCalls, 1, "choices 后 ready 必须自动建一次运行");
  assert.equal(calls.openLive.length, 1);
  assert.equal(intake.intake.active, false);
});

test("startIntake:带文本 → 快照 + clarifier 请求回填草稿选项", async () => {
  const { intake, calls, fetchCalls } = setup();
  intake.startIntake("核对两期毛利");
  await new Promise(r => setTimeout(r, 720));
  assert.equal(fetchCalls[0].url, "/api/intake");
  assert.equal(fetchCalls[0].body.message, "核对两期毛利");
  assert.equal(intake.intake.spec.intent, "compare");
  assert.equal(intake.intake.messages.length, 2);
  assert.equal(intake.intake.messages[1].role, "assistant");
  assert.ok(intake.intake.snapshots.length === 1, "进入前快照");
  assert.ok(calls.renderMain.at(-1).html.includes("intake-draft"), "草稿渲染进主区");
});

test("intakeRequest:通用委托把审批档位并入 spec 一起送出", async () => {
  const { intake, fetchCalls } = setup({ tplKey: "full" });
  intake.intake.active = true;
  await intake.intakeRequest("调研任务", null);
  assert.equal(fetchCalls[0].body.spec.approval, "consequential",
    "通用委托的自主度由输入框档位决定,随请求送出");
  assert.equal(fetchCalls[0].body.template, "full");
});

test("intakeChoose/intakeFreeText/intakeBack:选项入链,上一步恢复快照", async () => {
  const { intake } = setup();
  intake.intake.active = true;
  intake.intake.spec = { intent: "compare" };
  intake.intakeChoose("output", "memo", "一页结论");
  await new Promise(r => setTimeout(r, 720));
  assert.equal(intake.intake.spec.output, "memo");
  const before = JSON.stringify(intake.intake.spec);
  intake.intakeFreeText("换个说法");
  await new Promise(r => setTimeout(r, 720));
  assert.ok(intake.intake.snapshots.length >= 2);
  intake.intakeBack();
  assert.equal(JSON.stringify(intake.intake.spec), before, "回退到上一步的 spec");
});

test("startIntakeRun:调研类无来源被拦;有 readyTask 才创建运行并接流", async () => {
  const ok = setup();
  ok.intake.intake.active = true;
  ok.intake.intake.readyTask = { objective: "调研 X", template: "full", approval: "off", spec: { mode: "research" } };
  await ok.intake.startIntakeRun();
  assert.match(ok.calls.toast[0], /至少需要 1 个来源/);
  assert.equal(ok.calls.openLive.length, 0, "守卫拦截后不创建运行");

  const withSource = setup();
  withSource.deps.attach.sources = ["https://example.com/a"];
  withSource.intake.intake.active = true;
  withSource.intake.intake.readyTask = {
    objective: "调研 X", template: "full", approval: "off",
    spec: { output: "memo" },
  };
  await withSource.intake.startIntakeRun();
  assert.deepEqual(withSource.fetchCalls.at(-1).body, {
    objective: "调研 X", template: "full", approval: "off",
    spec: { output: "memo" },
    model: "kimi-k2.6", effort: "medium",
  });
  assert.deepEqual(withSource.calls.openLive[0], { id: "run-new", objective: "调研 X", template: "full", model: "kimi-k2.6" });
  assert.equal(withSource.intake.intake.active, false, "受理后离开 intake");
  assert.equal(withSource.deps.attach.scope.size, 0, "来源清空");
  assert.match(withSource.calls.toast[0], /委托已受理/);
});

test("startIntakeRun:项目模式必须带独立验证命令", async () => {
  const s = setup({ project: "/repo", verification: "" });
  s.deps.attach.sources = ["https://example.com/a"];
  s.intake.intake.active = true;
  s.intake.intake.readyTask = { objective: "改代码", template: "full", approval: "off", spec: { mode: "script" } };
  await s.intake.startIntakeRun();
  assert.match(s.calls.toast[0], /独立验证命令/);
  assert.equal(s.calls.openLive.length, 0);
});

test("startIntakeRun:项目目录失效时清绑定并保留任务草稿", async () => {
  const s = setup({
    project: "/deleted/project", verification: "pytest -q",
    fetchImpl: async () => ({
      ok: false, status: 400,
      json: async () => ({ code: "PROJECT_NOT_FOUND", error: "project directory is not readable" }),
    }),
  });
  s.intake.intake.active = true;
  s.intake.intake.readyTask = {
    objective: "保留这条任务", template: "full", approval: "off", spec: { mode: "open" },
  };
  await s.intake.startIntakeRun();
  assert.equal(s.calls.clearProjectContext, 1);
  assert.equal(s.calls.openLive.length, 0, "失效项目不得进入运行线程");
  assert.equal(s.intake.intake.active, true);
  assert.equal(s.intake.intake.readyTask.objective, "保留这条任务");
  assert.match(s.calls.toast[0], /重新选择项目.*任务内容已保留/);
  assert.equal(s.els["intake-start-btn"].disabled, false);
});

test("偏好持久化:受理成功后写入 fa.intake,下次徽章显示'上次选择'", async () => {
  const s = setup();
  s.deps.attach.sources = ["https://example.com/a"];
  s.intake.intake.active = true;
  s.intake.intake.readyTask = { objective: "调研 X", template: "full", approval: "off", spec: { output: "memo" } };
  await s.intake.startIntakeRun();
  const stored = JSON.parse(globalThis.localStorage.getItem("fa.intake"));
  assert.equal(stored.output, "memo");
  // 新实例读同一份 localStorage → 徽章命中
  const s2 = setup({ storage: globalThis.localStorage });
  const badge = s2.intake.intakeBadge({ field: "output", value: "memo", label: "一页结论", badge: "" });
  assert.equal(badge, "上次选择");
  const other = s2.intake.intakeBadge({ field: "output", value: "trace", label: "详细过程", badge: "" });
  assert.equal(other, "");
});

test("refreshModelBudget:health 未就绪不发请求;模板变化才拉取", async () => {
  let fetches = 0;
  const s = setup({
    health: { live: false },
    fetchImpl: async () => { fetches += 1; return { ok: true, json: async () => ({ models: [] }) }; },
  });
  s.intake.refreshModelBudget();
  assert.equal(fetches, 0, "回放模式不拉模型预算");
});

test("renderIntake:非激活态直接返回,不写主区", () => {
  const s = setup();
  s.intake.renderIntake();
  assert.equal(s.calls.renderMain.length, 0);
});
