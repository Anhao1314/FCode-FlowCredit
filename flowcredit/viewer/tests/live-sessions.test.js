import test from "node:test";
import assert from "node:assert/strict";

import { createLiveSessions } from "../app/live-sessions.js";

/* 会话机桩测试:用假 EventSource 驱动 snapshot/append/done/failed 事件,
   断言连接状态推进、前台/后台分流、终态收敛与乐观态清理。
   渲染类依赖(render/liveApply/renderMain…)全部用记录器替换。 */

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.last = this;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, data) {
    for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) });
  }
  fireOpen() { this.onopen && this.onopen(); }
  fireError() { this.onerror && this.onerror(); }
  close() { this.closed = true; }
}

function makeEl() {
  return { hidden: false, innerHTML: "", textContent: "", className: "", dataset: {}, title: "" };
}

function setup({ active = null } = {}) {
  const calls = { liveApply: [], render: [], renderMain: [], notify: [], toast: [],
    renderTabs: 0, renderNav: 0, refreshRuns: [], setComposerRunning: [],
    paintThread: [], paintApproval: 0, paintQueuedMsgs: 0, loadReviewProfile: [],
    loadProjectDelivery: [], setChainBadge: [], renderRail: [], updateStatusline: 0,
    openReport: 0, leaveChat: 0 };
  const sessions = new Map();
  const store = { sessionId: null, key: null, trace: null, check: null };
  const cache = new Map();
  const deps = {
    sessions, store, cache,
    viewGate: { begin() {} },
    activeSession: () => active === "SESSION" ? sessions.get(store.sessionId) : active,
    leaveChat: () => { calls.leaveChat += 1; },
    setLedgerOpen: () => {},
    renderNav: () => { calls.renderNav += 1; },
    renderTabs: () => { calls.renderTabs += 1; },
    renderMain: (html, meta) => calls.renderMain.push({ html, meta }),
    render: (trace, title) => calls.render.push({ trace, title }),
    refreshRuns: opts => calls.refreshRuns.push(opts),
    liveApply: (s, data) => calls.liveApply.push({ s, data }),
    liveShell: () => {},
    paintThread: (s, trace) => calls.paintThread.push({ s, trace }),
    paintApproval: () => { calls.paintApproval += 1; },
    paintQueuedMsgs: () => { calls.paintQueuedMsgs += 1; },
    setComposerRunning: v => calls.setComposerRunning.push(v),
    notify: (t, b) => calls.notify.push({ t, b }),
    toast: m => calls.toast.push(m),
    updateStatusline: () => { calls.updateStatusline += 1; },
    shownTrace: () => null,
    openReport: () => { calls.openReport += 1; },
    loadReviewProfile: t => calls.loadReviewProfile.push(t),
    loadProjectDelivery: t => calls.loadProjectDelivery.push(t),
    verifyChain: async () => ({ ok: true }),
    setChainBadge: c => calls.setChainBadge.push(c),
    renderRail: (t, c) => calls.renderRail.push({ t, c }),
    shortObjective: t => String(t).slice(0, 8),
    tplTitle: k => "通用委托",
  };
  const live = createLiveSessions(deps);
  globalThis.document = {
    getElementById: id => (els[id] ||= makeEl()),
    querySelector: () => null,
  };
  const els = {};
  return { live, sessions, store, cache, calls, els };
}

test("openLive:新会话走 loading 态并接流;store.key 指向运行", () => {
  const { live, sessions, store, calls } = setup({ active: null });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-1", "核对口径", "full");
  assert.equal(sessions.size, 1);
  assert.equal(store.key, "run:run-1");
  assert.equal(store.sessionId, "run-1");
  assert.match(calls.renderMain[0].html, /等待 Runtime 启动/);
  assert.equal(calls.renderMain[0].meta.mode, "loading");
  assert.deepEqual(calls.setComposerRunning, [true]);
  assert.match(FakeEventSource.last.url, /\/api\/runs\/run-1\/stream$/);
  assert.equal(calls.leaveChat, 1, "进入运行前先离开对话语境");
});

test("snapshot 帧:前台会话走 liveApply,finished 时收敛终态", () => {
  const { live, sessions, store, calls } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-2", "任务", "full");
  const s = sessions.get("run-2");
  s.queuedMsgs = [{ state: "queued", text: "插话" }];
  const es = FakeEventSource.last;
  es.fireOpen();
  es.emit("snapshot", { snapshot: { events: [{ at: "t" }], terminal: { status: "succeeded" } }, finished: true });
  assert.equal(calls.liveApply.length, 1, "前台帧走 liveApply 状态机");
  assert.equal(s.finished, true, "终态收敛");
  assert.equal(s.running, false);
  assert.equal(s.es, null, "终态后关流");
  assert.equal(es.closed, true);
  assert.equal(s.queuedMsgs[0].state, "draft", "未送达消息落草稿");
  assert.match(calls.toast.at(-1), /已保留为草稿/);
  assert.ok(calls.refreshRuns.some(o => o && o.force), "终态刷新运行列表");
});

test("snapshot 帧:后台会话只 stash,不动主区", () => {
  const { live, sessions, calls } = setup({ active: null });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-3", "后台任务", "full");
  const s = sessions.get("run-3");
  calls.liveApply.length = 0;
  FakeEventSource.last.emit("snapshot", { snapshot: { events: [1] }, finished: false });
  assert.equal(calls.liveApply.length, 0, "后台不渲染");
  assert.deepEqual(s.trace, { events: [1] }, "状态照常推进");
  assert.ok(calls.renderTabs >= 1, "标签点更新");
});

test("append 帧:delta 合并进 trace,pending 透传", () => {
  const { live, sessions, calls } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-4", "任务", "full");
  const s = sessions.get("run-4");
  s.trace = { events: [], rounds: [] };
  FakeEventSource.last.emit("append", {
    events: [{ at: "t2" }], rounds: [{ round: 1 }], roundStart: 0,
    pending: { id: "ap-1", kind: "approval" },
  });
  assert.equal(s.trace.events.length, 1);
  assert.equal(s.trace.rounds.length, 1);
  assert.deepEqual(calls.liveApply.at(-1).data.pending, { id: "ap-1", kind: "approval" });
});

test("applyDelta:roundStart 截断重写,terminal/task/model 覆盖", () => {
  const { live } = setup();
  const trace = { events: [1], rounds: [{ round: 1 }, { round: 2 }], task: { old: 1 } };
  live.applyDelta(trace, { rounds: [{ round: 2, fixed: true }], roundStart: 1,
    terminal: { status: "succeeded" }, task: { id: "t" }, model: "m" });
  assert.deepEqual(trace.rounds, [{ round: 1 }, { round: 2, fixed: true }],
    "roundStart 保留之前的轮次,之后的整段重写");
  assert.deepEqual(trace.terminal, { status: "succeeded" });
  assert.deepEqual(trace.task, { id: "t" });
  assert.equal(trace.model, "m");
  assert.deepEqual(trace.events, [1], "无 events 增量时保持原样");
});

test("failed 帧:置失败但绝不伪造终态;无快照时渲染显式错误页", () => {
  const { live, sessions, calls } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-5", "任务", "full");
  const s = sessions.get("run-5");
  FakeEventSource.last.emit("failed", { error: "RUNTIME CRASH" });
  assert.equal(s.failed, true);
  assert.equal(s.running, false);
  assert.equal(s.trace, null, "没有快照就没有线程");
  assert.match(calls.renderMain.at(-1).html, /运行失败：RUNTIME CRASH/);
  assert.equal(calls.renderMain.at(-1).meta.mode, "error");
  assert.match(calls.notify[0].t, /运行失败/);
});

test("finishSession:前台带终态快照 → 链验证/判断面/通知全部触发", async () => {
  const { live, sessions, store, calls, cache } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-6", "完成任务", "full");
  const s = sessions.get("run-6");
  const trace = { events: [], terminal: { status: "succeeded", result: {} } };
  s.trace = trace;
  s.es = FakeEventSource.last;
  live.finishSession(s, {});
  assert.equal(s.finished, true);
  assert.deepEqual(cache.get("run:run-6"), trace, "终态快照进缓存");
  assert.match(calls.notify[0].t, /运行完成/);
  await new Promise(r => setTimeout(r, 0));
  assert.ok(calls.setChainBadge.length >= 1, "链徽章随链验证刷新");
});

test("closeSessionTab:删会话、关流;关的是当前标签时主区给提示", () => {
  const { live, sessions, store, calls } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-7", "任务", "full");
  const s = sessions.get("run-7");
  const es = s.es;
  live.closeSessionTab("run-7");
  assert.equal(sessions.has("run-7"), false);
  assert.equal(es.closed, true);
  assert.equal(store.sessionId, null);
  assert.match(calls.renderMain.at(-1).html, /标签已关闭/);
  assert.ok(calls.refreshRuns.length >= 1);
});

test("detachLive:只清当前指针,会话继续在后台跑", () => {
  const { live, store, calls } = setup({ active: null });
  store.sessionId = "run-9";
  live.detachLive();
  assert.equal(store.sessionId, null);
  assert.deepEqual(calls.setComposerRunning, [false]);
});

test("connection:opened/snapshot/done 推进,断流 error 不伪造终态", () => {
  const { live, sessions } = setup({ active: "SESSION" });
  globalThis.EventSource = FakeEventSource;
  live.openLive("run-8", "任务", "full");
  const s = sessions.get("run-8");
  const es = FakeEventSource.last;
  es.fireOpen();
  assert.equal(s.connection.transport, "open", "opened 只推进传输层");
  es.emit("snapshot", { snapshot: { events: [] } });
  assert.equal(s.connection.phase, "connected", "首帧快照才算同步");
  es.fireError();
  assert.equal(s.connection.phase, "reconnecting", "断流进入重连,不是任务失败");
  assert.notEqual(s.connection.phase, "finished");
  assert.equal(s.running, true, "任务状态保持最后已知值");
  es.emit("done", {});
  assert.equal(s.connection.phase, "finished");
  assert.equal(s.connection.terminal, "done");
});
