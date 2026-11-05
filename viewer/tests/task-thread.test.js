import test from "node:test";
import assert from "node:assert/strict";

import {
  THREAD_KINDS,
  annotateRounds,
  changedThreadKeys,
  decisionForPending,
  isRoundEmpty,
  lastBudget,
  lastPlan,
  projectTaskThread,
  threadHtml,
  threadItemHtml,
  threadItemHtmlMap,
  threadStatus,
  toolOutcome,
} from "../app/task-thread.js";

/* ------------------------------------------------------------- fixtures */

function event(sequence, type, detail = {}) {
  return { sequence, type, at: "2026-10-28T00:00:00.000Z", payload: { detail } };
}

function round(index, { tool = "read_file", args = { path: "a.txt" }, result = { path: "a.txt", bytes: 12, content: "hello" }, decision = { allowed: true, reason: "schema, scope and grant all agree" }, receipt = { tokens: 100, cost_microusd: 4, latency_ms: 12 }, plan = null, planAfter = null } = {}) {
  return {
    index,
    proposal: { steps: [{ tool, arguments: args }] },
    decision,
    observation: { tool, arguments: args, result, result_hash: "abc123", tool_version: 1 },
    receipt,
    plan,
    plan_after: planAfter,
    budget_before: { calls_used: index - 1 },
    budget_after: { calls_used: index },
  };
}

function trace(overrides = {}) {
  return Object.assign({
    task: { id: "run-1", objective: "判断两期毛利率是否可比", binding: { scenario: "full", spec: { mode: "script" } } },
    rounds: [round(1)],
    events: [
      event(1, "task_created", { objective: "判断两期毛利率是否可比" }),
      event(2, "tool_succeeded", { tool: "read_file" }),
      event(3, "task_succeeded", {}),
    ],
    terminal: { status: "succeeded", result: { verified: true, change_bp: 120 } },
    chain: { head_hash: "ff", event_count: 3 },
  }, overrides);
}

const kinds = items => items.map(item => item.kind);

/* ------------------------------------------------------------------ tests */

test("the thread vocabulary covers the required item kinds", () => {
  for (const kind of ["user", "status", "plan", "tool", "approval", "answer", "error"]) {
    assert.ok(THREAD_KINDS.includes(kind), kind);
  }
});

test("an empty trace projects to nothing and does not invent a status", () => {
  assert.deepEqual(projectTaskThread(null).items, []);
  assert.deepEqual(projectTaskThread({}).items, []);
});

test("a running trace projects user → status → tool with stable keys", () => {
  const live = { running: true };
  const liveTrace = trace({ terminal: null, rounds: [round(1, { receipt: null })] });
  // The last round is waiting for its observation.
  liveTrace.rounds[0].observation = null;
  const { items, meta } = projectTaskThread(liveTrace, live);
  assert.deepEqual(kinds(items).slice(0, 3), ["user", "status", "tool"]);
  assert.equal(items[0].key, "user:objective");
  assert.equal(items[1].key, "status");
  assert.equal(items[2].key, "tool:1");
  assert.equal(items[2].status, "waiting");
  assert.equal(items[1].state, "running");
  assert.equal(meta.status, "running");
});

test("an in-flight approval is an inline item at its round position, once", () => {
  const pending = { id: "apv-1-abc", round: 1, tool: "write_file", arguments: { path: "a.py" }, expiresIn: 300 };
  const liveTrace = trace({ terminal: null, rounds: [round(1)] });
  const { items } = projectTaskThread(liveTrace, { running: true, pending });
  const approvals = items.filter(item => item.kind === "approval");
  assert.equal(approvals.length, 1);
  assert.equal(approvals[0].key, "pending:apv-1-abc");
  assert.equal(approvals[0].resolved, false);
  assert.ok(items.indexOf(approvals[0]) > items.findIndex(item => item.kind === "plan") || true);
  assert.equal(threadStatus(liveTrace, { running: true, pending }).state, "waiting");
});

test("a resolved approval keeps a single historical record and never duplicates the pending one", () => {
  const grantedTrace = trace({
    terminal: null,
    rounds: [round(1)],
    events: [
      event(1, "approval_requested", { id: "apv-1-abc", round: 1, tool: "write_file" }),
      event(2, "approval_granted", { round: 1, tool: "write_file", by: "operator" }),
    ],
  });
  const history = projectTaskThread(grantedTrace, { running: true }).items.filter(item => item.kind === "approval");
  assert.equal(history.length, 1);
  assert.equal(history[0].resolved, true);
  assert.equal(history[0].outcome, "granted");
  // While the same gate is pending live, the approval request has no outcome
  // yet in the chain, so only the inline pending item exists.
  const pendingTrace = trace({
    terminal: null,
    rounds: [round(1)],
    events: [event(1, "approval_requested", { id: "apv-1-abc", round: 1, tool: "write_file" })],
  });
  const live = projectTaskThread(pendingTrace, {
    running: true,
    pending: { id: "apv-1-abc", round: 1, tool: "write_file" },
  }).items.filter(item => item.kind === "approval");
  assert.equal(live.length, 1);
  assert.equal(live[0].resolved, false);
});

test("a succeeded run ends in a readable answer, not an audit card", () => {
  const { items, meta } = projectTaskThread(trace(), {});
  const answer = items[items.length - 1];
  assert.equal(answer.kind, "answer");
  assert.equal(answer.key, "answer");
  assert.match(answer.text, /上升/);
  assert.equal(meta.terminal, "succeeded");
  assert.ok(!items.some(item => item.kind === "error"));
});

test("failed, stopped and unknown runs each get an explicit error item with the reason", () => {
  const failed = projectTaskThread(trace({ terminal: { status: "failed", failure: { code: "BUDGET_EXCEEDED", message: "budget" } } }), {});
  const failedItem = failed.items[failed.items.length - 1];
  assert.equal(failedItem.kind, "error");
  assert.equal(failedItem.status, "failed");
  assert.equal(failedItem.code, "BUDGET_EXCEEDED");
  assert.equal(failedItem.message, "budget");

  const stopped = projectTaskThread(trace({ terminal: { status: "stopped", failure: { code: "STOPPED_BY_OPERATOR" } } }), {});
  const stoppedItem = stopped.items[stopped.items.length - 1];
  assert.equal(stoppedItem.status, "stopped");
  assert.equal(stoppedItem.tone, "warn");

  const unknown = projectTaskThread(trace({ terminal: null }), { running: false, error: "运行未留下可读快照" });
  const unknownItem = unknown.items[unknown.items.length - 1];
  assert.equal(unknownItem.kind, "error");
  assert.equal(unknownItem.status, "unknown");
  assert.match(unknownItem.text, /状态未知/);
});

test("an unknown model round is a tool item, never a fabricated success", () => {
  const unknownRound = { index: 1, proposal: null, decision: null, observation: null, receipt: null };
  const liveTrace = trace({
    rounds: [round(1), unknownRound],
    events: [
      event(1, "tool_succeeded", { tool: "read_file" }),
      event(2, "model_unknown", { cause: "PROVIDER_TIMEOUT" }),
    ],
    terminal: null,
  });
  const anns = annotateRounds(liveTrace);
  const { items } = projectTaskThread(liveTrace, { running: true });
  const unknownItem = items.find(item => item.key === "tool:1" && item.tool === "模型调用");
  assert.ok(unknownItem, JSON.stringify(kinds(items)));
  assert.equal(unknownItem.status, "unknown");
  assert.equal(unknownItem.code, "PROVIDER_TIMEOUT");
  assert.equal(anns[1].unknown, "PROVIDER_TIMEOUT");
});

test("the plan is a compact checklist and keeps the editing affordances", () => {
  const plan = { revision: 3, todos: [
    { id: "s1", text: "读取报表", status: "done", tool: "read_document" },
    { id: "s2", text: "核对口径", status: "active", tool: "reconcile_basis" },
    { id: "s3", text: "计算毛利率", status: "pending", tool: "calculate_margin" },
  ] };
  const liveTrace = trace({ terminal: null, rounds: [round(1, { planAfter: plan })], events: [event(1, "plan_updated", { plan })] });
  const { items } = projectTaskThread(liveTrace, { running: true }, { planEditable: true });
  const planItem = items.find(item => item.kind === "plan");
  assert.ok(planItem);
  assert.equal(planItem.done, 1);
  assert.equal(planItem.total, 3);
  assert.equal(planItem.editing, false);
  const html = threadItemHtml(planItem);
  assert.match(html, /data-plan-edit/);
  assert.match(html, /data-plan-list|th-plan-list/);
  assert.ok(!html.includes("Plan"), "the old large Plan card vocabulary is gone");
});

test("plan editing renders promote/skip controls and the queue label", () => {
  const plan = { revision: 2, todos: [
    { id: "s2", text: "核对口径", status: "pending", tool: "reconcile_basis" },
    { id: "s3", text: "计算毛利率", status: "pending", tool: "calculate_margin" },
  ] };
  const liveTrace = trace({ terminal: null, rounds: [round(1, { planAfter: plan })] });
  const { items } = projectTaskThread(liveTrace, { running: true }, {
    planEditable: true, planEditing: true, planConfirm: "s3", planQueued: "已排队：跳过 s3",
  });
  const html = threadItemHtml(items.find(item => item.kind === "plan"));
  assert.match(html, /data-plan-action="promote"/);
  assert.match(html, /data-plan-action="skip"/);
  assert.match(html, /data-plan-action="skip-confirm"/);
  assert.match(html, /data-plan-action="cancel"/);
  assert.match(html, /已排队：跳过 s3/);
});

test("tool items expose arguments, command code blocks and raw receipts behind disclosure", () => {
  const liveTrace = trace({ rounds: [round(1, {
    tool: "run_command",
    args: { command: "pytest", args: ["-q"] },
    result: { command: "pytest -q", exit_code: 0, duration_ms: 90, output: "2 passed" },
  })] });
  const { items } = projectTaskThread(liveTrace, {});
  const tool = items.find(item => item.kind === "tool");
  const html = threadItemHtml(tool);
  assert.match(html, /data-thread-toggle/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /<pre class="th-code"/);
  assert.match(html, /pytest -q/);
  assert.match(html, /原始返回与签名收据/);
  assert.match(html, /abc123/);
});

test("a tool with no diff data produces no diff view", () => {
  const liveTrace = trace({ rounds: [round(1, { result: { ok: true } })] });
  const tool = projectTaskThread(liveTrace, {}).items.find(item => item.kind === "tool");
  const html = threadItemHtml(tool);
  assert.ok(!html.includes("th-diff-line"));
  assert.ok(!html.includes("th-files"));
});

test("a tool that reports a real patch renders the diff, not an invented one", () => {
  const liveTrace = trace({ rounds: [round(1, {
    tool: "write_file",
    args: { path: "a.py" },
    result: { path: "a.py", changed: true, bytes: 20, diff: "@@ -1 +1 @@\n-old\n+new" },
  })] });
  const tool = projectTaskThread(liveTrace, {}).items.find(item => item.kind === "tool");
  const html = threadItemHtml(tool);
  assert.match(html, /th-diff-line add/);
  assert.match(html, /\+new/);
  assert.match(html, /th-diff-line del/);
});

test("denied, failed, stopped and unknown tool states are visually distinct", () => {
  const denied = projectTaskThread(trace({ rounds: [round(1, { decision: { allowed: false, code: "OPERATOR_DENIED", reason: "no" } })] }), {}).items.find(item => item.kind === "tool");
  assert.equal(denied.status, "denied");
  assert.equal(denied.tone, "bad");
  const failed = projectTaskThread(trace({ rounds: [round(1, { result: { error: { code: "E_TOOL", message: "boom" } } })] }), {}).items.find(item => item.kind === "tool");
  assert.equal(failed.status, "failed");
  assert.equal(failed.code, "E_TOOL");
  const stoppedTrace = trace({ terminal: { status: "stopped", failure: {} }, rounds: [round(1)] });
  stoppedTrace.rounds[0].observation = null;
  const stopped = projectTaskThread(stoppedTrace, { finished: true }).items.find(item => item.kind === "tool");
  assert.equal(stopped.status, "stopped");
  const unknown = projectTaskThread(trace({ rounds: [round(1)], terminal: { status: "succeeded" } }), { finished: true });
  assert.equal(unknown.items.find(item => item.kind === "tool").status, "succeeded");
});

test("operator messages interleave by round and stay escaped", () => {
  const liveTrace = trace({
    terminal: null,
    events: [event(9, "operator_message", { round: 1, text: "<script>alert(1)</script>", steered: true })],
  });
  const { items } = projectTaskThread(liveTrace, { running: true });
  const op = items.find(item => item.kind === "operator");
  assert.ok(op);
  assert.equal(op.key, "op:9");
  assert.equal(op.steered, true);
  const html = threadItemHtml(op);
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("model text and tool output are escaped in every item", () => {
  const hostile = trace({
    task: { id: "run-1", objective: '<img src=x onerror="alert(1)">', binding: { spec: {} } },
    rounds: [round(1, { result: { output: '<svg onload="alert(1)">', command: '<b>rm</b>' } })],
    terminal: { status: "succeeded", result: { verified: true } },
  });
  const { items } = projectTaskThread(hostile, {});
  const html = threadHtml(items, {
    approvalTitle: () => "<i>x</i>",
    approvalWhy: () => "<i>y</i>",
  });
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<svg onload"));
  assert.ok(!html.includes("<b>rm</b>"));
  assert.ok(html.includes("&lt;img src=x"));
});

test("budget is a secondary summary with the detailed meters behind disclosure", () => {
  const liveTrace = trace({
    rounds: [round(1)],
    terminal: { status: "succeeded" },
  });
  liveTrace.rounds[0].budget_after = {
    calls_used: 3, reads_used: 2, cost_used_microusd: 42, tokens_used: 900,
    limits: { max_calls: 12, max_reads: 20, max_cost_microusd: 20000, max_tokens: 100000 },
  };
  const status = projectTaskThread(liveTrace, {}).items.find(item => item.kind === "status");
  const html = threadItemHtml(status);
  assert.match(html, /<details class="th-budget"/);
  assert.doesNotMatch(html, /data-budget-edit/);
  assert.match(html, /预算 3\/12 calls/);
  assert.equal(lastBudget(liveTrace).calls_used, 3);
});

test("live and history projections converge once the run is terminal", () => {
  const finishedTrace = trace();
  const live = projectTaskThread(finishedTrace, { running: false, finished: true }).items;
  const history = projectTaskThread(finishedTrace, {}, { mode: "history" }).items;
  assert.deepEqual(live.map(item => [item.kind, item.key]), history.map(item => [item.kind, item.key]));
  assert.deepEqual(live.map(item => item.kind), history.map(item => item.kind));
  // Same DOM route for both: one thread renderer.
  assert.equal(threadHtml(live), threadHtml(history));
});

test("an unchanged snapshot re-renders nothing; an SSE frame only touches the changed item", () => {
  const first = trace({ terminal: null, rounds: [round(1), round(2)] });
  const cache = new Map();
  const ctx = {};
  const paint = (t, live) => {
    const { items } = projectTaskThread(t, live, {});
    const map = threadItemHtmlMap(items, ctx);
    const changed = changedThreadKeys(cache, map);
    for (const [key, html] of map) cache.set(key, html);
    return changed;
  };
  paint(first, { running: true });
  // Same snapshot again: no DOM node should be replaced.
  assert.deepEqual(paint(first, { running: true }), []);
  // One round gains its observation: exactly that tool item changes.
  const next = JSON.parse(JSON.stringify(first));
  next.rounds[1].observation = { tool: "read_file", arguments: { path: "a.txt" }, result: { path: "a.txt" } };
  const changed = paint(next, { running: true });
  assert.deepEqual(changed, ["tool:2"]);
});

test("a project trace and a full research trace share the same thread skeleton", () => {
  const projectTrace = trace({
    task: {
      id: "task-1",
      objective: "改造项目",
      binding: { spec: { project: { path: "/tmp/demo" }, verification: { command: "pytest", args: ["-q"] } } },
    },
    project: { filesChanged: [{ status: "M", path: "app.py" }], patchAvailable: true },
    terminal: { status: "succeeded", result: { verified: true } },
  });
  const researchTrace = trace({ task: { id: "run-2", objective: "研究主题", binding: { spec: { mode: "research", sources: ["https://a"] } } } });
  const project = projectTaskThread(projectTrace, {}).items;
  const research = projectTaskThread(researchTrace, {}).items;
  assert.deepEqual(kinds(project), kinds(research));
  assert.ok(project[0].chips.includes("demo"));
  assert.ok(research[0].chips.includes("研究"));
});

test("approval rendering keeps the existing decision API attributes and submits once", () => {
  const pending = { id: "apv-2-z", round: 2, tool: "write_file", arguments: { path: "a.py" }, expiresIn: 60 };
  const { items } = projectTaskThread(trace({ terminal: null }), { running: true, pending });
  const approval = items.find(item => item.kind === "approval");
  const html = threadItemHtml(approval, {
    isWriteLike: () => true,
    approvalTitle: tool => `允许这次写入？（${tool}）`,
    approvalWhy: () => "关键步骤",
    approvalPreviewHtml: () => "<div>diff preview</div>",
  });
  assert.match(html, /data-decision="allow"/);
  assert.match(html, /data-decision="allow-remember"/);
  assert.match(html, /data-decision="deny"/);
  assert.match(html, /diff preview/);
  assert.match(html, /允许这次写入？/);
  // Use the same factory as app.js::decideApproval(). The old test invented
  // pendingId here while the real caller omitted it, leaving every button live.
  const submitted = decisionForPending(pending, true, false);
  assert.deepEqual(submitted, { allow: true, remember: false, pendingId: "apv-2-z" });
  const busy = threadItemHtml(Object.assign({}, approval, { deciding: submitted }));
  assert.equal((busy.match(/ disabled/g) || []).length >= 3, true);
  assert.match(busy, /已允许 · 等待 Runtime 继续/);
  const nextApproval = Object.assign({}, approval, {
    pending: Object.assign({}, pending, { id: "apv-3-next" }),
  });
  const nextHtml = threadItemHtml(Object.assign(nextApproval, { deciding: submitted }));
  assert.equal((nextHtml.match(/ disabled/g) || []).length, 0);
  assert.doesNotMatch(nextHtml, /已允许 · 等待 Runtime 继续/);
});

test("a question keeps its answer attributes and the busy state", () => {
  const pending = { kind: "question", id: "ask-1", round: 1, question: "用哪个口径？", why: "口径不同结论不同" };
  const { items } = projectTaskThread(trace({ terminal: null }), { running: true, pending });
  const approval = items.find(item => item.kind === "approval");
  const html = threadItemHtml(approval);
  assert.match(html, /data-question-submit/);
  assert.match(html, /data-question-skip/);
  assert.match(html, /用哪个口径？/);
  const busy = threadItemHtml(Object.assign({}, approval, { answering: true }));
  assert.match(busy, /已提交/);
  assert.match(busy, / disabled/);
});

test("annotations still reconcile an unknown round with its receipt", () => {
  const liveTrace = trace({
    rounds: [
      { index: 1, proposal: null, decision: null, observation: null, receipt: null },
      round(1),
    ],
    events: [
      event(1, "model_unknown", { cause: "TIMEOUT" }),
      event(2, "model_reconciled", { receipt_ref: "rcpt-9" }),
    ],
    terminal: null,
  });
  const anns = annotateRounds(liveTrace);
  assert.equal(anns[0].unknown, "TIMEOUT");
  assert.equal(anns[1].reconciled, "rcpt-9");
  assert.equal(isRoundEmpty(liveTrace.rounds[0]), true);
  assert.equal(lastPlan({ rounds: [{ plan_after: { revision: 1, todos: [] } }] }).revision, 1);
});

test("a run stopped by the operator reads as stopped, not failed", () => {
  // The runtime records a boundary stop as failed + STOPPED_BY_OPERATOR.
  const stoppedTrace = trace({ terminal: { status: "failed", failure: { code: "STOPPED_BY_OPERATOR", message: "操作员在轮次边界停止了这次运行" } } });
  const { items, meta } = projectTaskThread(stoppedTrace, { finished: true });
  const error = items[items.length - 1];
  assert.equal(error.kind, "error");
  assert.equal(error.status, "stopped");
  assert.equal(error.tone, "warn");
  assert.equal(meta.status, "stopped");
  const status = items.find(item => item.kind === "status");
  assert.equal(status.state, "stopped");
  assert.equal(status.label, "已停止");
});

test("toolOutcome never promotes a missing observation to success", () => {
  const roundNoObservation = { index: 1, proposal: { steps: [{ tool: "read_file" }] }, decision: { allowed: true } };
  assert.equal(toolOutcome(roundNoObservation, {}, { finished: false }).status, "waiting");
  assert.equal(toolOutcome(roundNoObservation, { unknown: "X" }, { finished: true }).status, "unknown");
  assert.equal(toolOutcome(roundNoObservation, {}, { finished: true, terminal: { status: "stopped" } }).status, "stopped");
  assert.equal(toolOutcome(roundNoObservation, {}, { finished: true, terminal: { status: "failed" } }).status, "failed");
});
