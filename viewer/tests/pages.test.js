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

test("定时任务:保存草稿/删除/空态,全部落 fa.scheduled", async () => {
  const storage = makeLocalStorage();
  const els = {
    "tb-title": makeEl(), "tb-chips": makeEl(),
    "sch-objective": makeEl({ value: "" }),
    "sch-template": makeEl({ value: "general" }),
    "sch-approval": makeEl({ value: "consequential" }),
    "sch-frequency": makeEl({ value: "每天" }),
    "sch-time": makeEl({ value: "09:00" }),
    "sch-save": makeEl(),
  };
  let delButtons = [];
  const renderCalls = [];
  globalThis.document = {
    body: makeEl(), documentElement: makeEl(),
    getElementById: id => els[id] || null,
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
    getScenarios: () => [["general", "通用委托"]],
    fetchImpl: async () => ({ json: async () => ({}) }),
  });

  // 首次渲染：空态
  pages.renderScheduledPage();
  assert.match(renderCalls.at(-1), /还没有定时任务草稿/);

  // 目标为空 → toast 拦截
  els["sch-save"].listeners.click[0]();
  assert.deepEqual(toasts, ["先描述定时任务的目标"]);

  // 填目标 → 保存 → 落 localStorage + 重渲染出该行
  els["sch-objective"].value = "每天核对口径";
  els["sch-save"].listeners.click[0]();
  const stored = JSON.parse(storage.getItem("fa.scheduled"));
  assert.equal(stored.length, 1);
  assert.equal(stored[0].objective, "每天核对口径");
  assert.match(renderCalls.at(-1), /每天核对口径/);

  // 删除按钮按 data-sch-del 索引移除
  els["sch-objective"].value = "第二条";
  els["sch-save"].listeners.click[0]();
  const delBtn = makeEl({ dataset: { schDel: "0" } });
  delBtn.addEventListener = (type, fn) => { delBtn._fn = fn; };
  delButtons = [delBtn];
  pages.renderScheduledPage();
  delBtn._fn();
  assert.equal(JSON.parse(storage.getItem("fa.scheduled")).length, 1);
  assert.equal(JSON.parse(storage.getItem("fa.scheduled"))[0].objective, "第二条");
});