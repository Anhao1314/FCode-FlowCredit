import test from "node:test";
import assert from "node:assert/strict";

import { createPages } from "../app/pages.js";

/* 极简 DOM/localStorage 桩：只实现 pages.js 用到的接口。
   renderMain 被桩成记录器——断言的是"渲染了什么"，不是"怎么渲染"。 */

function makeEl(init = {}) {
  const el = {
    hidden: false,
    innerHTML: "",
    textContent: "",
    value: "",
    listeners: {},
    dataset: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    ...init,
  };
  return el;
}

function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
    _store: store,
  };
}

test("renderReviews:四类判断卡 + 审计处置区段并列渲染", async () => {
  const toasts = [];
  const els = {
    "tb-title": makeEl(), "tb-chips": makeEl(), "composer-ta": makeEl(),
  };
  const storage = makeLocalStorage();
  const seq = { value: 1 };
  const renderCalls = [];
  globalThis.document = {
    body: makeEl(), documentElement: makeEl(),
    getElementById: id => els[id] || null,
    querySelectorAll: () => [], createElement: () => makeEl(),
  };
  globalThis.localStorage = storage;

  const pages = createPages({
    renderMain: (html, meta) => renderCalls.push({ html, meta }),
    setFoot() {},
    toast: m => toasts.push(m),
    getViewGateSequence: () => seq.value,
    getScenarios: () => [],
    fetchImpl: async () => ({ json: async () => ({
      counts: { accept: 3, needs_evidence: 1, qualified: 0, redo: 2 },
      recent: [{ decision: "accept", note: "口径核对完成", at: "2026-11-06" }],
      audit_dispositions: [
        { audit_id: "evox-1", finding_id: "F-1", disposition: "accepted_risk",
          category: "AGI-07" },
        { audit_id: "evox-1", finding_id: "F-2", disposition: null },
      ],
    }) }),
  });

  await pages.renderReviews();
  const html = renderCalls.at(-1).html;
  for (const card of ["接受判断", "需要补材料", "保留意见", "退回重做"]) {
    assert.match(html, new RegExp(card));
  }
  assert.match(html, /口径核对完成/, "最近判断行");
  assert.match(html, /审计处置/, "审计处置区段并列");
  assert.match(html, /接受风险/, "处置标签走 DISPOSITION_LABELS");
  assert.ok(!html.includes("F-2"), "无处置的发现不进区段");
});

test("renderReviews:epoch 过期后不渲染旧响应（陈旧守卫）", async () => {
  const els = {
    "tb-title": makeEl(), "tb-chips": makeEl(), "composer-ta": makeEl(),
  };
  const seq = { value: 1 };
  const renderCalls = [];
  globalThis.document = {
    body: makeEl(), documentElement: makeEl(),
    getElementById: id => els[id] || null,
    querySelectorAll: () => [], createElement: () => makeEl(),
  };
  globalThis.localStorage = makeLocalStorage();

  const pages = createPages({
    renderMain: (html, meta) => renderCalls.push({ html, meta }),
    setFoot() {},
    toast() {},
    getViewGateSequence: () => seq.value,
    getScenarios: () => [],
    // fetch 返回前用户切走视图 → 世代号作废
    fetchImpl: async () => { seq.value = 99; return { json: async () => ({ counts: {} }) }; },
  });

  await pages.renderReviews();
  assert.equal(renderCalls.length, 1, "只有 loading 态，陈旧响应不渲染");
});

test("定时任务:服务端同步——旧草稿迁移/保存 POST/删除 POST", async () => {
  const storage = makeLocalStorage();
  // 预置旧版 localStorage 草稿:首次打开应一次性迁移上送
  storage.setItem("fa.scheduled", JSON.stringify([
    { objective: "旧草稿：核对口径", template: "full", approval: "off", frequency: "每天", time: "08:00" },
  ]));
  const els = {
    "tb-title": makeEl(), "tb-chips": makeEl(),
    "sch-objective": makeEl({ value: "" }),
    "sch-template": makeEl({ value: "full" }),
    "sch-approval": makeEl({ value: "consequential" }),
    "sch-frequency": makeEl({ value: "每天" }),
    "sch-time": makeEl({ value: "09:00" }),
    "sch-save": makeEl(),
  };
  const schList = makeEl();
  let delButtons = [];
  const renderCalls = [];
  const posts = [];
  let serverSchedules = [];  // 服务端状态桩

  globalThis.document = {
    body: makeEl(), documentElement: makeEl(),
    getElementById: id => els[id] || null,
    querySelector: sel => (sel === ".sch-list" ? schList : null),
    querySelectorAll: sel => (sel === "[data-sch-del]" ? delButtons : []),
    createElement: () => makeEl(),
  };
  globalThis.localStorage = storage;

  const toasts = [];
  const pages = createPages({
    renderMain: html => renderCalls.push(html),
    setFoot() {},
    toast: m => toasts.push(m),
    getViewGateSequence: () => 1,
    getScenarios: () => [["full", "完整研究"]],
    fetchImpl: async (url, init) => {
      const u = String(url);
      if (u === "/api/schedules" && (init === undefined || !init.method)) {
        return { ok: true, json: async () => ({ schedules: serverSchedules }) };
      }
      if (u === "/api/schedules" && init && init.method === "POST") {
        const body = JSON.parse(init.body);
        posts.push({ url: u, body });
        if (body.objective !== undefined && body.id) {
          // 更新:按 id 覆盖
          serverSchedules = serverSchedules.map(x => (x.id === body.id ? { ...x, ...body } : x));
        } else if (body.objective !== undefined) {
          // 新建/迁移:服务端分配 id
          serverSchedules.push({ id: "sch-" + (serverSchedules.length + 1), next: "每天 08:00", ...body });
        }
        return { ok: true, json: async () => ({ schedule: body }) };
      }
      if (u === "/api/schedules/delete" && init && init.method === "POST") {
        const body = JSON.parse(init.body);
        posts.push({ url: u, body });
        serverSchedules = serverSchedules.filter(x => x.id !== body.id);
        return { ok: true, json: async () => ({ ok: true }) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    },
  });

  // 首次渲染:迁移旧草稿(POST 上送) + 拉取列表
  await pages.renderScheduledPage();
  const migratePost = posts.find(p2 => p2.body.objective === "旧草稿：核对口径");
  assert.ok(migratePost, "旧草稿已 POST 上送");
  assert.equal(storage.getItem("fa.scheduled"), null, "迁移后清掉本机键");
  assert.match(schList.innerHTML, /旧草稿：核对口径/, "列表渲染服务端条目");
  assert.match(schList.innerHTML, /每天 08:00/, "下次触发描述来自服务端");

  // 目标为空 → toast 拦截,不发请求
  const postsBefore = posts.length;
  els["sch-save"].listeners.click[0]();
  assert.deepEqual(toasts,
    ["已迁移 1 条旧草稿到本机调度器", "先描述定时任务的目标"],
    "迁移提示与空目标拦截按序出现");
  assert.equal(posts.length, postsBefore, "空目标不发起请求");

  // 填目标 → 保存 → POST 载荷与服务端回填
  els["sch-objective"].value = "每天核对口径";
  els["sch-time"].value = "09:30";
  await els["sch-save"].listeners.click[0]();
  await new Promise(r => setTimeout(r, 5)); // 保存后的重渲染是 fire-and-forget
  const savePost = posts.find(p2 => p2.body.objective === "每天核对口径");
  assert.ok(savePost, "保存发起了 POST");
  assert.equal(savePost.body.time, "09:30");
  assert.equal(savePost.body.approval, "consequential");
  assert.match(schList.innerHTML, /每天核对口径/);

  // 删除按钮 → POST /delete → 服务端移除
  const target = serverSchedules.find(x => x.objective === "每天核对口径");
  const delBtn = makeEl({ dataset: { schDel: target.id } });
  delBtn.addEventListener = (type, fn) => { delBtn._fn = fn; };
  delButtons = [delBtn];
  await pages.renderScheduledPage();
  await delBtn._fn();
  await new Promise(r => setTimeout(r, 5));
  assert.ok(posts.some(p2 => p2.url === "/api/schedules/delete" && p2.body.id === target.id));
  assert.equal(serverSchedules.some(x => x.id === target.id), false);
});

/* Slice B：排程模型快照。目录来自共享 modelCatalog，初始值 = 共享默认；
   保存显式提交 model（可见 effort 时附带）；列表行显示已固定的模型。
   依赖缺席时退回 mock——这是回放/桩环境的诚实降级。 */

const CATALOG = [
  { key: "mock", available: true, label: "offline-mock", model: "mock", supportsEffort: false },
  { key: "glm", available: true, label: "GLM-5.3-Flash", model: "glm-5.3-flash", supportsEffort: true },
  { key: "kimi", available: true, label: "Kimi 云端", model: "kimi-k2.6", supportsEffort: false },
];

function scheduleHarness({ serverSchedules = [], catalog = CATALOG, sharedDefault = "glm" } = {}) {
  const els = {
    "tb-title": makeEl(), "tb-chips": makeEl(), "composer-ta": makeEl(),
    "sch-objective": makeEl({ value: "" }),
    "sch-template": makeEl({ value: "full" }),
    "sch-approval": makeEl({ value: "consequential" }),
    "sch-model": makeEl({ value: "" }),
    "sch-effort-label": makeEl({ hidden: true }),
    "sch-effort": makeEl({ value: "" }),
    "sch-frequency": makeEl({ value: "每天" }),
    "sch-time": makeEl({ value: "09:00" }),
    "sch-save": makeEl(),
  };
  const schList = makeEl();
  const renderCalls = [];
  const posts = [];
  const toasts = [];
  let delButtons = [];
  globalThis.document = {
    body: makeEl(), documentElement: makeEl(),
    getElementById: id => els[id] || null,
    querySelector: sel => (sel === ".sch-list" ? schList : null),
    querySelectorAll: sel => (sel === "[data-sch-del]" ? delButtons : []),
    createElement: () => makeEl(),
  };
  globalThis.localStorage = makeLocalStorage();
  const pages = createPages({
    renderMain: html => renderCalls.push(html),
    setFoot() {},
    toast: m => toasts.push(m),
    getViewGateSequence: () => 1,
    getScenarios: () => [["full", "完整研究"]],
    getModelCatalog: () => catalog,
    getSharedModelDefault: () => sharedDefault,
    effortLevels: () => ["low", "medium", "high"],
    effortLabel: key => ({ low: "低", medium: "中", high: "高" }[key] || key || ""),
    fetchImpl: async (url, init) => {
      const u = String(url);
      if (u === "/api/schedules" && (init === undefined || !init.method)) {
        return { ok: true, json: async () => ({ schedules: serverSchedules }) };
      }
      if (u === "/api/schedules" && init && init.method === "POST") {
        const body = JSON.parse(init.body);
        posts.push({ url: u, body });
        serverSchedules.push({ id: "sch-x", next: "每天 09:00", enabled: true, ...body });
        return { ok: true, json: async () => ({ schedule: body }) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    },
  });
  return { pages, els, schList, posts, toasts, renderCalls };
}

test("定时任务:模型选项来自共享目录,初始选中共享默认并标注", async () => {
  const { pages, els, renderCalls } = scheduleHarness();
  await pages.renderScheduledPage();
  const formHtml = renderCalls.at(-1);
  assert.match(formHtml, /value="glm" selected/, "共享默认被选中");
  assert.match(formHtml, /GLM-5.3-Flash（共享默认）/);
  assert.match(formHtml, /value="kimi"/, "目录里其它可用模型都在");
  assert.equal(els["sch-model"].value, "glm", "select 值显式落共享默认");
  assert.equal(els["sch-effort-label"].hidden, false, "supportsEffort 模型显示档位");
  assert.match(formHtml, /id="sch-effort"[\s\S]*?value="high">高/);
});

test("定时任务:不支持 effort 的模型隐藏档位,保存 body 不带 effort 字段", async () => {
  const { pages, els, posts } = scheduleHarness({ sharedDefault: "kimi" });
  await pages.renderScheduledPage();
  assert.equal(els["sch-effort-label"].hidden, true, "kimi 不支持 effort");
  els["sch-objective"].value = "每日报表";
  els["sch-model"].value = "kimi";
  await els["sch-save"].listeners.click[0]();
  await new Promise(r => setTimeout(r, 5));
  const savePost = posts.find(p => p.body.objective === "每日报表");
  assert.ok(savePost, "保存发起 POST");
  assert.equal(savePost.body.model, "kimi");
  assert.ok(!("effort" in savePost.body), "不支持 effort 时字段整体缺席");
});

test("定时任务:保存 body 携带 model 与 effort 快照", async () => {
  const { pages, els, posts } = scheduleHarness();
  await pages.renderScheduledPage();
  els["sch-objective"].value = "每日盯盘";
  els["sch-model"].value = "glm";
  els["sch-effort"].value = "high";
  await els["sch-save"].listeners.click[0]();
  await new Promise(r => setTimeout(r, 5));
  const savePost = posts.find(p => p.body.objective === "每日盯盘");
  assert.ok(savePost);
  assert.equal(savePost.body.model, "glm");
  assert.equal(savePost.body.effort, "high");
});

test("定时任务:列表行显示已固定的模型,旧条目显示 mock", async () => {
  const { pages, schList } = scheduleHarness({
    serverSchedules: [
      { id: "s1", objective: "旧排程", template: "full", approval: "off", next: "每天 08:00", enabled: true },
      { id: "s2", objective: "新排程", template: "full", approval: "off", model: "glm", effort: "high", next: "每天 09:00", enabled: true },
    ],
  });
  await pages.renderScheduledPage();
  const html = schList.innerHTML;
  assert.match(html, /模型 mock/, "迁移/旧排程如实显示 mock，不假装跟随默认");
  assert.match(html, /模型 glm · 高/, "新排程显示固定的模型与档位");
});

test("定时任务:目录缺席时退回 mock 选项(桩/回放降级)", async () => {
  const { pages, els, posts, renderCalls } = scheduleHarness({ catalog: [], sharedDefault: "" });
  await pages.renderScheduledPage();
  assert.match(renderCalls.at(-1), /value="mock">离线 Mock/);
  assert.equal(els["sch-model"].value, "mock");
  els["sch-objective"].value = "离线排程";
  await els["sch-save"].listeners.click[0]();
  await new Promise(r => setTimeout(r, 5));
  const savePost = posts.find(p => p.body.objective === "离线排程");
  assert.equal(savePost.body.model, "mock");
});
