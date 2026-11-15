import test from "node:test";
import assert from "node:assert/strict";

import { createRunActions } from "../app/run-actions.js";
import { decisionForPending } from "../app/task-thread.js";

/* 会话动作的桩测试:断言乐观状态、API 载荷与失败回滚,
   不渲染真实线程(paintThread 记录调用即可)。 */

function makeInput(value = "") {
  return { value, listeners: {} };
}

function setup({ session = null, fetchImpl = async () => ({ ok: true, json: async () => ({}) }),
                 inputs = {}, budgetInputs = null } = {}) {
  const toasts = [];
  const patches = [];
  const s = session || {};
  const actions = createRunActions({
    activeSession: () => s,
    paintThread: (sess, trace, opts) => patches.push({ sess, opts }),
    toast: m => toasts.push(m),
    fetchImpl,
  });
  globalThis.document = {
    querySelector: sel => {
      if (sel === "[data-question-input]") return inputs.question || null;
      if (sel.startsWith("[data-budget=")) {
        if (!budgetInputs) return null;
        const key = /\[data-budget="([^"]+)"\]/.exec(sel)[1];
        return budgetInputs[key] || null;
      }
      return null;
    },
  };
  return { actions, toasts, patches, s };
}

test("decisionForPending:决定身份由投影与 API 共用", () => {
  assert.deepEqual(decisionForPending({ id: "ap-9" }, true, true),
    { allow: true, remember: true, pendingId: "ap-9" });
  assert.deepEqual(decisionForPending(null, false), { allow: false, remember: false, pendingId: "" });
});

test("decideApproval:乐观置 deciding,成功后 toast,身份与 API 载荷一致", async () => {
  const posts = [];
  const { actions, toasts, patches, s } = setup({
    session: { id: "run-1", pending: { id: "ap-1", kind: "approval" }, trace: { events: [] } },
    fetchImpl: async (url, init) => {
      posts.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({}) };
    },
  });
  await actions.decideApproval(true, true);
  assert.deepEqual(posts[0].body, { approvalId: "ap-1", allow: true, remember: true });
  assert.match(posts[0].url, /\/api\/runs\/run-1\/decision$/);
  assert.match(toasts.at(-1), /本会话已授权/);
  assert.ok(patches.length >= 1, "决定前后补丁线程");
  assert.equal(s.pending.id, "ap-1", "会话不被动作改写");
});

test("decideApproval:API 失败 → deciding 回滚 + 错误 toast", async () => {
  const { actions, toasts, s } = setup({
    session: { id: "run-1", pending: { id: "ap-1", kind: "approval" } },
    fetchImpl: async () => ({ ok: false, json: async () => ({ error: "RUNTIME_DOWN" }) }),
  });
  await actions.decideApproval(true);
  assert.equal(s.deciding, null, "乐观状态回滚");
  assert.match(toasts.at(-1), /决定未送达：RUNTIME_DOWN/);
});

test("decideApproval:question 类 pending 不走审批;deciding 期间拒绝重入", async () => {
  let calls = 0;
  const { actions } = setup({
    session: { id: "run-1", pending: { id: "q-1", kind: "question" }, deciding: { pendingId: "x" } },
    fetchImpl: async () => { calls += 1; return { ok: true, json: async () => ({}) }; },
  });
  await actions.decideApproval(true);
  assert.equal(calls, 0, "问题用 answerQuestion,不用 decision");
});

test("answerQuestion:空文本自动转为跳过;成功清除 answering", async () => {
  const posts = [];
  const { actions, toasts, s } = setup({
    session: { id: "run-2", pending: { id: "q-1", kind: "question" } },
    inputs: { question: makeInput("  ") },
    fetchImpl: async (url, init) => {
      posts.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({}) };
    },
  });
  await actions.answerQuestion(false);
  assert.deepEqual(posts[0].body, { questionId: "q-1", skip: true }, "空白输入转跳过");
  assert.equal(s.answering, false);
  assert.match(toasts.at(-1), /已跳过/);
});

test("answerQuestion:文本回答 POST 全文;失败回滚 answering", async () => {
  const posts = [];
  const { actions, toasts, s } = setup({
    session: { id: "run-2", pending: { id: "q-2", kind: "question" } },
    inputs: { question: makeInput("口径为 CNY") },
    fetchImpl: async (url, init) => {
      posts.push(JSON.parse(init.body));
      return { ok: false, status: 500, json: async () => ({}) };
    },
  });
  await actions.answerQuestion(false);
  assert.deepEqual(posts[0], { questionId: "q-2", text: "口径为 CNY" });
  assert.equal(s.answering, false, "失败回滚");
  assert.match(toasts.at(-1), /回答未送达/);
});

test("queuePlan:运行中才能排队,排队态带事件计数,下一轮生效提示", async () => {
  const posts = [];
  const { actions, toasts, s } = setup({
    session: { id: "run-3", running: true, trace: { events: [1, 2, 3] }, planConfirm: { x: 1 } },
    fetchImpl: async (url, init) => {
      posts.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({}) };
    },
  });
  await actions.queuePlan("skip", "t4");
  assert.deepEqual(posts[0].body, { action: "skip", todoId: "t4", note: "操作员跳过" });
  assert.deepEqual(s.planQueued, { label: "已排队：跳过 t4", events: 3 });
  assert.equal(s.planConfirm, null, "排队后清除确认态");
  assert.match(toasts.at(-1), /下一轮生效/);
});

test("queueBudget:上限只许调高;违规不发起请求", async () => {
  let calls = 0;
  const { actions, toasts } = setup({
    session: {
      id: "run-4", running: true,
      // lastBudget 从 trace.rounds[].budget_after 读既有上限
      trace: { events: [], rounds: [{ budget_after: { calls_used: 1,
        limits: { max_calls: 5, max_reads: 10, max_cost_microusd: 100 } } }] },
    },
    budgetInputs: {
      maxCalls: makeInput("1"), maxReads: makeInput("5"), maxCostMicrousd: makeInput("10"),
    },
    fetchImpl: async () => { calls += 1; return { ok: true, json: async () => ({}) }; },
  });
  await actions.queueBudget();
  assert.equal(calls, 0, "低于既有上限的调整不得发起请求");
  assert.match(toasts.at(-1), /上限只能调高/);
});

test("queueBudget:全部高于现有上限 → POST 载荷与排队态", async () => {
  const posts = [];
  const session = {
    id: "run-5", running: true, budgetEditing: true,
    trace: { events: [1], rounds: [{ budget_after: { calls_used: 1, limits: {
      max_calls: 5, max_reads: 10, max_cost_microusd: 100 } } }] },
  };
  const { actions, toasts, s } = setup({
    session,
    budgetInputs: {
      maxCalls: makeInput("8"), maxReads: makeInput("12"), maxCostMicrousd: makeInput("200"),
    },
    fetchImpl: async (url, init) => {
      posts.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({}) };
    },
  });
  await actions.queueBudget();
  assert.deepEqual(posts[0].body, { maxCalls: 8, maxReads: 12, maxCostMicrousd: 200 });
  assert.equal(s.budgetEditing, false);
  assert.deepEqual(s.budgetQueued, { events: 1 });
  assert.match(toasts.at(-1), /预算调整已排队/);
});

test("paintApproval/paintPlan:无会话或无 trace 时不补丁,live 标志跟随终态", () => {
  const { actions, patches } = setup({ session: null });
  actions.paintApproval();
  actions.paintPlan();
  assert.equal(patches.length, 0);
  const { actions: a2, patches: p2 } = setup({
    session: { trace: { terminal: { status: "succeeded" } }, finished: false },
  });
  a2.paintApproval();
  assert.deepEqual(p2[0].opts, { live: false }, "已有终态的会话按回放补丁");
  const { actions: a3, patches: p3 } = setup({
    session: { trace: { events: [] }, running: true },
  });
  a3.paintPlan();
  assert.deepEqual(p3[0].opts, { live: true });
});
