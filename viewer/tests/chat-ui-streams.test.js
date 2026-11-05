import test from "node:test";
import assert from "node:assert/strict";

import { createChatUI } from "../app/chat-ui.js";
import { parseSseFrame } from "../core/chat-format.js";
import { chatRowsHtml } from "../core/side-nav.js";

const encoder = new TextEncoder();
const tick = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

function fakeDocument() {
  const elements = new Map();
  const make = id => ({
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    title: "",
    hidden: false,
    disabled: false,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    appendChild() {},
    insertBefore() {},
    remove() {},
    replaceWith() {},
    contains() { return false; },
  });
  const get = id => {
    if (!elements.has(id)) elements.set(id, make(id));
    return elements.get(id);
  };
  globalThis.document = {
    getElementById: get,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => make("created"),
    addEventListener() {},
    body: { classList: { add() {}, remove() {}, toggle() {} } },
  };
  return { get };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function controlledStream() {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  const control = {
    signal: null,
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    bind(signal) {
      control.signal = signal || null;
      if (signal) {
        signal.addEventListener("abort", () => {
          try {
            controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
          } catch { /* already settled */ }
        });
      }
    },
    send(event) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
    },
    close() {
      try { controller.close(); } catch { /* already settled */ }
    },
  };
  return control;
}

function harness() {
  fakeDocument();
  globalThis.requestAnimationFrame = callback => callback();
  const chat = {
    active: false, current: null, threads: [], streaming: false, abort: null,
    attachments: [], editing: null, paintTimer: null, limit: 50, total: 0,
    deleteArmed: null, deleteTimer: null, cards: new Map(), mirrors: new Map(),
  };
  let chatThreads = [];
  let epoch = 0;
  const toasts = [];
  const renderCalls = [];
  const sendControls = [];
  const serverThreads = new Map();
  const threadDeferreds = new Map();
  const threadReads = [];
  const threadListRequests = [];
  const deleteRequests = [];
  let listPayload = { threads: [], total: 0 };
  let threadListHandler = null;

  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.startsWith("/api/chat/send")) {
      const control = controlledStream();
      sendControls.push(control);
      control.bind(options.signal);
      return control.response;
    }
    if (target.startsWith("/api/chat/threads?")) {
      const parsed = new URL(target, "http://fcode.local");
      const request = {
        url: target,
        limit: Number(parsed.searchParams.get("limit")),
        offset: Number(parsed.searchParams.get("offset")),
      };
      threadListRequests.push(request);
      if (threadListHandler) return threadListHandler(request);
      return jsonResponse(listPayload);
    }
    const match = target.match(/^\/api\/chat\/threads\/([^/?]+)$/);
    if (match && options.method === "DELETE") {
      const id = decodeURIComponent(match[1]);
      deleteRequests.push(id);
      return jsonResponse({ id, archived: true });
    }
    if (match) {
      const id = decodeURIComponent(match[1]);
      threadReads.push(id);
      if (threadDeferreds.has(id)) {
        const value = await threadDeferreds.get(id).promise;
        // 同一个 deferred 可能被两代回读调用；Response body 只能读一次，
        // 数据对象则每次生成新的 Response。
        return value instanceof Response ? value : jsonResponse(value);
      }
      return jsonResponse(serverThreads.get(id) || { id, title: id, messages: [] });
    }
    return jsonResponse({ error: `unexpected ${target}` }, 404);
  };

  const ui = createChatUI({
    chat,
    chatModelChoice: { key: "mock" },
    esc: value => String(value),
    chatSectionHtml: () => "",
    chatRowsHtml,
    isChatPinned: () => false,
    toast: message => toasts.push(message),
    intake: { active: false },
    store: { key: null, trace: null },
    activeSession: () => null,
    detachLive() {},
    setChromeState() {},
    closeIntake() {},
    closeSettingsPage() {},
    renderNav() {},
    renderMain(html, meta) { renderCalls.push({ html, meta }); },
    stageEmptyHtml: () => "",
    autosize() {},
    paintSections() {},
    selectRun() {},
    prepareCardRuns: async () => {},
    aiAvatarHtml: () => "",
    chatMessageHtml: message => `<div data-msg-id="${message.id}">${message.text}</div>`,
    withRunStatus: message => message,
    chatCardHtml: () => "",
    nearMainBottom: () => true,
    scrollMainBottom() {},
    hydrateDoodles() {},
    parseSseFrame,
    renderMarkdown: text => String(text),
    copyText: async () => true,
    startIntake() {},
    threadOrigin: () => ({}),
    adoptDefaultChatModel() {},
    loadChatModels() {},
    getHealth: () => ({ live: true }),
    getChatThreads: () => chatThreads,
    setChatThreads: value => { chatThreads = value; },
    getChatConfig: () => ({}),
    getSettingsOpen: () => false,
    setLedgerOpen() {},
    getViewEpoch: () => epoch,
    invalidateView: () => { epoch += 1; },
  });

  return {
    ui, chat, toasts, renderCalls, sendControls, serverThreads, threadDeferreds, threadReads,
    threadListRequests, deleteRequests,
    setThreadList(payload) {
      listPayload = payload;
      threadListHandler = null;
    },
    setThreadListHandler(handler) { threadListHandler = handler; },
    threads() { return chatThreads; },
  };
}

async function startSend(h, text = "hello") {
  h.ui.newChat();
  document.getElementById("composer-ta").value = text;
  const running = h.ui.sendChatFromComposer();
  await tick();
  return { running, control: h.sendControls[h.sendControls.length - 1] };
}

const threadIds = list => list.map(thread => thread.id);

function listPage(threads, total, offset, limit = threads.length) {
  return jsonResponse({ threads, total, offset, limit });
}

async function seedThreads(h, threads, total = threads.length) {
  h.setThreadList({ threads, total });
  const result = await h.ui.loadChatThreads(true);
  assert.equal(result.applied, true, "初始 reset 应成功");
  return h.threads();
}

async function archiveThread(h, threadId) {
  const button = { textContent: "", classList: { add() {} } };
  h.ui.handleChatDelete(threadId, button); // 第一次点击进入确认态
  h.ui.handleChatDelete(threadId, button); // 第二次点击真正发送 DELETE
  await tick(10);
  assert.ok(h.deleteRequests.includes(threadId), `DELETE ${threadId} 应真正发出`);
}

async function startLivePlaceholder(h, text, threadId) {
  h.ui.newChat();
  document.getElementById("composer-ta").value = text;
  const running = h.ui.sendChatFromComposer();
  await tick();
  const control = h.sendControls[h.sendControls.length - 1];
  control.send({ type: "start", thread_id: threadId, thread_title: text });
  await tick();
  return { running, control };
}

const chatNavHtml = () => document.getElementById("chat-nav").innerHTML;

function moreCount() {
  const match = chatNavHtml().match(/显示全部 · 还有 (\d+) 条/);
  return match ? Number(match[1]) : 0;
}

const hasMoreButton = () => chatNavHtml().includes("data-chat-more");

test("Chat A 生成中切到 Work：流不中止，delta 继续写入 A", async () => {
  const h = harness();
  const { running, control } = await startSend(h);
  const threadA = h.chat.current;
  assert.ok(threadA);

  control.send({ type: "start", thread_id: "chat-a", thread_title: "A", assistant_id: "m-a" });
  await tick();
  control.send({ type: "delta", text: "A1" });
  await tick();
  assert.equal(threadA.messages.at(-1).text, "A1");

  h.ui.leaveChat();
  assert.equal(h.chat.current, null);
  assert.equal(control.signal.aborted, false, "切视图不能中止网络流");

  control.send({ type: "delta", text: "A2" });
  await tick();
  assert.equal(threadA.messages.at(-1).text, "A1A2");

  control.send({ type: "done", message: { id: "m-a", text: "A final" } });
  control.close();
  await running;
  assert.equal(threadA.messages.at(-1).status, "ok");
});

test("A 生成中打开 B：A 的 delta/done 不出现在 B；后台完成后返回 A 显示最终回答", async () => {
  const h = harness();
  const { running, control } = await startSend(h);
  const threadA = h.chat.current;
  control.send({ type: "start", thread_id: "chat-a", thread_title: "A" });
  await tick();
  control.send({ type: "delta", text: "A partial" });
  await tick();

  h.serverThreads.set("chat-b", {
    id: "chat-b",
    title: "B",
    messages: [{ id: "b-u", role: "user", text: "B question", status: "ok" }],
  });
  await h.ui.openChat("chat-b");
  assert.equal(h.chat.current.id, "chat-b");

  control.send({ type: "delta", text: "+more" });
  control.send({ type: "done", message: { id: "m-a", text: "A final" } });
  await tick();
  assert.equal(threadA.messages.at(-1).text, "A final");
  assert.deepEqual(h.chat.current.messages.map(m => m.id), ["b-u"], "B 不接收 A 的事件");
  assert.equal(h.chat.current.messages[0].text, "B question");

  h.serverThreads.set("chat-a", {
    id: "chat-a",
    title: "A",
    messages: [
      { id: "a-u", role: "user", text: "hello", status: "ok" },
      { id: "m-a", role: "assistant", text: "A final", status: "ok" },
    ],
  });
  control.close();
  await running;
  await h.ui.openChat("chat-a");
  assert.equal(h.chat.current.id, "chat-a");
  assert.equal(h.chat.current.messages.at(-1).text, "A final");
  assert.equal(h.chat.streaming, false);
});

test("A、B 同时生成时各自的停止只中止对应线程", async () => {
  const h = harness();
  const first = await startSend(h, "A");
  const threadA = h.chat.current;
  first.control.send({ type: "start", thread_id: "chat-a", thread_title: "A" });
  await tick();

  h.serverThreads.set("chat-b", { id: "chat-b", title: "B", messages: [] });
  await h.ui.openChat("chat-b");
  document.getElementById("composer-ta").value = "B";
  const runningB = h.ui.sendChatFromComposer();
  await tick();
  const second = h.sendControls[h.sendControls.length - 1];
  second.send({ type: "start", thread_id: "chat-b", thread_title: "B" });
  await tick();

  h.ui.stopChat();
  await tick();
  assert.equal(second.signal.aborted, true, "B 被停止");
  assert.equal(first.control.signal.aborted, false, "A 不受影响");
  assert.equal(threadA.messages.at(-1).status, "streaming");

  first.control.send({ type: "done", message: { id: "m-a", text: "A done" } });
  first.control.close();
  second.close();
  await Promise.allSettled([first.running, runningB]);
});

test("主动停止当前线程：cancelled + 保留部分文本，且不弹失败 toast", async () => {
  const h = harness();
  const { running, control } = await startSend(h);
  const threadA = h.chat.current;
  control.send({ type: "start", thread_id: "chat-a", thread_title: "A" });
  await tick();
  control.send({ type: "delta", text: "部分回答" });
  await tick();

  h.serverThreads.set("chat-a", {
    id: "chat-a",
    title: "A",
    messages: [
      { id: "a-u", role: "user", text: "hello", status: "ok" },
      { id: "m-a", role: "assistant", text: "部分回答", status: "cancelled" },
    ],
  });
  h.ui.stopChat();
  await tick(350);
  assert.equal(threadA.messages.at(-1).status, "cancelled");
  assert.equal(threadA.messages.at(-1).text, "部分回答");
  assert.equal(h.toasts.some(message => String(message).includes("对话失败")), false);
  await running;
});

test("正常 done 后立即发送下一条：旧回读不丢第二轮 optimistic 消息", async () => {
  const h = harness();
  const first = await startSend(h, "first");
  const threadA = h.chat.current;
  first.control.send({
    type: "start", thread_id: "chat-a", thread_title: "A",
    assistant_id: "m1", user: { id: "u1", text: "first" },
  });
  await tick();
  first.control.send({ type: "delta", text: "first answer" });
  await tick();

  // 第一轮收尾的服务端回读先挂起，模拟它慢于用户的下一条发送。
  const oldRead = deferred();
  h.threadDeferreds.set("chat-a", oldRead);
  first.control.send({ type: "done", message: { id: "m1", text: "first answer" } });
  first.control.close();
  await tick(20);

  document.getElementById("composer-ta").value = "second";
  const running2 = h.ui.sendChatFromComposer();
  await tick();
  const second = h.sendControls[h.sendControls.length - 1];
  assert.ok(second, "第二轮请求已发出");
  assert.equal(threadA.messages.filter(m => m.role === "user").at(-1).text, "second");
  assert.equal(threadA.messages.at(-1).status, "streaming", "第二轮 optimistic assistant 已出现");
  assert.ok(h.threadReads.includes("chat-a"), "陈旧回读请求已经发出，确实会进入 response.ok 分支");

  // 旧回读带着只有第一轮的服务端快照返回：新一代已开始，必须放弃。
  oldRead.resolve(jsonResponse({
    id: "chat-a",
    title: "A",
    messages: [
      { id: "u1", role: "user", text: "first", status: "ok" },
      { id: "m1", role: "assistant", text: "first answer", status: "ok" },
    ],
  }));
  await tick(20);
  assert.equal(threadA.messages.filter(m => m.role === "user").at(-1).text, "second", "第二轮用户消息不能丢");
  assert.equal(threadA.messages.at(-1).status, "streaming", "旧回读不能覆盖第二轮 assistant");

  // 第二轮自己的回读换成最终快照，验证正常收尾仍能原地合并。
  h.threadDeferreds.delete("chat-a");
  h.serverThreads.set("chat-a", {
    id: "chat-a",
    title: "A",
    messages: [
      { id: "u1", role: "user", text: "first", status: "ok" },
      { id: "m1", role: "assistant", text: "first answer", status: "ok" },
      { id: "u2", role: "user", text: "second", status: "ok" },
      { id: "m2", role: "assistant", text: "second answer", status: "ok" },
    ],
  });
  second.send({
    type: "start", thread_id: "chat-a", assistant_id: "m2",
    user: { id: "u2", text: "second" },
  });
  await tick();
  second.send({ type: "delta", text: "second answer" });
  await tick();
  assert.equal(threadA.messages.at(-1).text, "second answer", "第二轮 delta 仍写入当前线程");
  second.send({ type: "done", message: { id: "m2", text: "second answer" } });
  second.close();
  await Promise.all([first.running, running2]);

  assert.equal(h.chat.current, threadA, "线程对象没有被替换");
  assert.deepEqual(
    threadA.messages.filter(m => m.role === "assistant").map(m => m.text),
    ["first answer", "second answer"],
    "两轮回答都保留且顺序正确",
  );
  assert.equal(threadA.messages.at(-1).status, "ok");
  assert.equal(new Set(threadA.messages.map(m => m.id)).size, threadA.messages.length, "消息 id 不重复");
});

test("主动停止后立即发送下一条：旧回读不丢第二轮 optimistic 消息", async () => {
  const h = harness();
  const first = await startSend(h, "first");
  const threadA = h.chat.current;
  first.control.send({
    type: "start", thread_id: "chat-a", thread_title: "A",
    assistant_id: "m1", user: { id: "u1", text: "first" },
  });
  await tick();
  first.control.send({ type: "delta", text: "partial" });
  await tick();

  const oldRead = deferred();
  h.threadDeferreds.set("chat-a", oldRead);
  h.ui.stopChat();
  await tick(20);

  document.getElementById("composer-ta").value = "second";
  const running2 = h.ui.sendChatFromComposer();
  await tick();
  const second = h.sendControls[h.sendControls.length - 1];
  assert.ok(second, "停止后仍能立即发送下一条");
  assert.equal(threadA.messages.filter(m => m.role === "user").at(-1).text, "second");
  assert.equal(threadA.messages.at(-1).status, "streaming");

  // 停止回读有 250ms 等待；等旧收尾走完并返回陈旧快照。
  await tick(300);
  assert.equal(h.threadReads.includes("chat-a"), false, "新一轮先开始时，旧停止回读不应再发出");
  oldRead.resolve(jsonResponse({
    id: "chat-a",
    title: "A",
    messages: [
      { id: "u1", role: "user", text: "first", status: "ok" },
      { id: "m1", role: "assistant", text: "partial", status: "cancelled" },
    ],
  }));
  await tick(20);
  assert.equal(threadA.messages.filter(m => m.role === "user").at(-1).text, "second");
  assert.equal(threadA.messages.at(-1).status, "streaming", "旧停止回读不能覆盖新一轮 optimistic assistant");

  second.send({
    type: "start", thread_id: "chat-a", assistant_id: "m2",
    user: { id: "u2", text: "second" },
  });
  await tick();
  second.send({ type: "delta", text: "second answer" });
  await tick();
  assert.equal(threadA.messages.at(-1).text, "second answer", "第二轮 delta 仍写入当前线程");
  second.send({ type: "done", message: { id: "m2", text: "second answer" } });
  second.close();
  await Promise.all([first.running, running2]);

  assert.equal(h.chat.current, threadA);
  assert.deepEqual(
    threadA.messages.filter(m => m.role === "assistant").map(m => m.text),
    ["partial", "second answer"],
    "停止的部分文本与第二轮回答都保留",
  );
  assert.equal(threadA.messages.at(-1).status, "ok", "第二轮 done 仍落在当前线程");
});

test("新线程尚无正式 id 时切换，start/done 不串线；生成中返回会重接同一回答", async () => {
  const h = harness();
  const { running, control } = await startSend(h);
  const threadA = h.chat.current;
  assert.equal(threadA.id, null);

  h.ui.leaveChat();
  control.send({ type: "start", thread_id: "chat-real", thread_title: "正式标题" });
  await tick();
  assert.equal(threadA.id, "chat-real");

  // 仍在生成时按正式 id 返回：必须投影现有线程，而不是重新发一次回答。
  await h.ui.openChat("chat-real");
  assert.equal(h.chat.current, threadA);
  assert.equal(h.chat.streaming, true);
  const before = threadA.messages.length;

  h.serverThreads.set("chat-real", {
    id: "chat-real",
    title: "正式标题",
    messages: [
      { id: "u-1", role: "user", text: "hello", status: "ok" },
      { id: "m-1", role: "assistant", text: "完整回答", status: "ok" },
    ],
  });
  control.send({ type: "delta", text: "完整" });
  control.send({ type: "done", message: { id: "m-1", text: "完整回答" } });
  control.close();
  await running;
  assert.equal(threadA.messages.length, before, "没有重复追加回答");
  assert.equal(threadA.messages.at(-1).text, "完整回答");
  assert.equal(h.chat.current.id, "chat-real");
  assert.equal(h.chat.streaming, false);
});

test("流式期间刷新侧栏：保留本地线程，且不覆盖当前其他会话", async () => {
  const h = harness();
  const { running, control } = await startSend(h);
  const threadA = h.chat.current;
  control.send({ type: "start", thread_id: "chat-a", thread_title: "本地标题" });
  await tick();

  h.serverThreads.set("chat-b", { id: "chat-b", title: "B", messages: [] });
  await h.ui.openChat("chat-b");
  h.setThreadList({ threads: [{ id: "chat-other", title: "其它" }], total: 1 });
  await h.ui.loadChatThreads(true);

  assert.equal(h.chat.current.id, "chat-b", "侧栏刷新不能切换当前会话");
  assert.ok(h.threads().some(item => item.id === "chat-a"), "仍在流式更新的线程不能被旧列表吞掉");
  assert.equal(h.threads().find(item => item.id === "chat-a").title, "本地标题");

  control.send({ type: "done", message: { id: "m-a", text: "A done" } });
  control.close();
  await running;
  assert.equal(threadA.messages.at(-1).status, "ok");
});

test("打开 Chat A、B 乱序返回：最终始终是 B，A 的失败不覆盖 B", async () => {
  const h = harness();
  const a = deferred();
  const b = deferred();
  h.threadDeferreds.set("chat-a", a);
  h.threadDeferreds.set("chat-b", b);
  h.serverThreads.set("chat-a", { id: "chat-a", title: "A", messages: [] });
  h.serverThreads.set("chat-b", { id: "chat-b", title: "B", messages: [] });

  const openingA = h.ui.openChat("chat-a");
  const openingB = h.ui.openChat("chat-b");
  b.resolve(jsonResponse({ id: "chat-b", title: "B", messages: [{ id: "b1", role: "user", text: "B", status: "ok" }] }));
  await tick();
  assert.equal(h.chat.current && h.chat.current.id, "chat-b");

  a.resolve(jsonResponse({ id: "chat-a", title: "A", messages: [{ id: "a1", role: "user", text: "A", status: "ok" }] }));
  await Promise.all([openingA, openingB]);
  assert.equal(h.chat.current.id, "chat-b");
  assert.deepEqual(h.chat.current.messages.map(m => m.id), ["b1"]);
  assert.equal(h.toasts.length, 0);
});

test("A 在 B 显示后失败：不弹错误、不把 B 替换成 A 的错误页", async () => {
  const h = harness();
  const a = deferred();
  const b = deferred();
  h.threadDeferreds.set("chat-a", a);
  h.threadDeferreds.set("chat-b", b);

  const openingA = h.ui.openChat("chat-a");
  const openingB = h.ui.openChat("chat-b");
  b.resolve(jsonResponse({ id: "chat-b", title: "B", messages: [] }));
  await tick();
  a.reject(new Error("A network failed"));
  await Promise.all([openingA, openingB]);

  assert.equal(h.chat.current.id, "chat-b");
  assert.equal(h.toasts.some(message => String(message).includes("对话加载失败")), false);
  assert.equal(h.renderCalls.some(call => String(call.html).includes("无法加载")), false);
});
test("reset 先、append 后：append 基于旧列表返回后不能覆盖新基线", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "old-a", title: "Old-A" },
    { id: "old-b", title: "Old-B" },
  ], 5);

  const resetRead = deferred();
  const appendRead = deferred();
  const requests = [];
  h.setThreadListHandler(request => {
    requests.push(request);
    return request.offset === 0 ? resetRead.promise : appendRead.promise;
  });

  const resetRun = h.ui.loadChatThreads(true);
  const appendRun = h.ui.loadChatThreads(false);
  assert.deepEqual(requests.map(r => [r.limit, r.offset]), [[2, 0], [2, 2]],
    "append 捕获旧基线的服务端行数，而不是等待 reset");

  resetRead.resolve(listPage([
    { id: "new-a", title: "New-A" },
    { id: "new-b", title: "New-B" },
  ], 4, 0, 2));
  await tick();
  assert.deepEqual(threadIds(h.threads()), ["new-a", "new-b"]);
  assert.equal(h.chat.total, 4);

  appendRead.resolve(listPage([{ id: "old-c", title: "Old-C" }], 5, 2, 2));
  const [resetResult, appendResult] = await Promise.all([resetRun, appendRun]);
  assert.equal(resetResult.applied, true);
  assert.equal(appendResult.stale, true, "旧 append 必须被丢弃");
  assert.deepEqual(threadIds(h.threads()), ["new-a", "new-b"], "不能把旧页拼回新基线");
  assert.equal(h.chat.total, 4, "旧 append 的 total 也不能回写");
});

test("append 先、reset 后：旧 append 最后返回也不能写入", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "old-a", title: "Old-A" },
    { id: "old-b", title: "Old-B" },
  ], 5);

  const appendRead = deferred();
  const resetRead = deferred();
  const requests = [];
  h.setThreadListHandler(request => {
    requests.push(request);
    return request.offset === 2 ? appendRead.promise : resetRead.promise;
  });

  const appendRun = h.ui.loadChatThreads(false);
  const resetRun = h.ui.loadChatThreads(true);
  assert.deepEqual(requests.map(r => [r.limit, r.offset]), [[2, 2], [2, 0]]);

  resetRead.resolve(listPage([
    { id: "new-a", title: "New-A" },
    { id: "new-b", title: "New-B" },
  ], 4, 0, 2));
  await tick();
  assert.deepEqual(threadIds(h.threads()), ["new-a", "new-b"]);

  appendRead.resolve(listPage([{ id: "old-c", title: "Old-C" }], 5, 2, 2));
  const [appendResult, resetResult] = await Promise.all([appendRun, resetRun]);
  assert.equal(resetResult.applied, true);
  assert.equal(appendResult.stale, true);
  assert.deepEqual(threadIds(h.threads()), ["new-a", "new-b"]);
  assert.equal(h.chat.total, 4);
});

test("快速重复加载更多：不并发同一 offset，下一页基于最新服务端行数", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);

  const pageRead = deferred();
  let appendCalls = 0;
  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => {
    appendCalls += 1;
    return pageRead.promise;
  });

  const firstRun = h.ui.loadChatThreads(false);
  const duplicate = await h.ui.loadChatThreads(false);
  assert.equal(duplicate.skipped, true, "同页 append 在途时应直接跳过");
  assert.equal(appendCalls, 1, "不能对同一页发起不可控并发");
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 2]],
  );

  pageRead.resolve(listPage([
    { id: "s3", title: "S3" },
    { id: "s4", title: "S4" },
  ], 4, 2, 2));
  assert.equal((await firstRun).applied, true);
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2", "s3", "s4"]);

  h.setThreadListHandler(() => listPage([{ id: "s5", title: "S5" }], 5, 4, 2));
  const next = await h.ui.loadChatThreads(false);
  assert.equal(next.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 2], [2, 4]],
    "下一页 offset 由已接受的服务端行数推进",
  );
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2", "s3", "s4", "s5"]);
  assert.equal(new Set(threadIds(h.threads())).size, 5, "追加页不能重复 id");
});

test("本地流式占位不增加服务端 offset，且 reset 不替换当前会话", async () => {
  const h = harness();
  h.chat.limit = 2;
  const { running, control } = await startSend(h, "live");
  const liveThread = h.chat.current;
  control.send({ type: "start", thread_id: "live-1", thread_title: "Live" });
  await tick();

  const before = h.threadListRequests.length;
  h.setThreadList({
    threads: [
      { id: "s1", title: "S1" },
      { id: "s2", title: "S2" },
    ],
    total: 4,
  });
  const resetResult = await h.ui.loadChatThreads(true);
  assert.equal(resetResult.applied, true);
  assert.deepEqual(threadIds(h.threads()), ["live-1", "s1", "s2"]);
  assert.equal(h.chat.current, liveThread, "侧栏 reset 不能替换当前会话");

  h.setThreadListHandler(() => listPage([
    { id: "s3", title: "S3" },
    { id: "s4", title: "S4" },
  ], 4, 2, 2));
  const appendResult = await h.ui.loadChatThreads(false);
  assert.equal(appendResult.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 0], [2, 2]],
    "append offset 只能是服务端行数 2，不能把本地占位算成 3",
  );
  assert.deepEqual(threadIds(h.threads()), ["live-1", "s1", "s2", "s3", "s4"]);
  assert.equal(new Set(threadIds(h.threads())).size, 5, "流式线程仍保留且唯一");
  assert.equal(h.chat.current, liveThread);

  h.setThreadList({ threads: [], total: 0 });
  control.send({ type: "done", message: { id: "live-m", text: "done" } });
  control.close();
  await running;
});

test("append 失败保留当前列表，后续 reset 仍能建立新基线", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 3);

  h.setThreadListHandler(() => jsonResponse({ error: "boom" }, 500));
  const failed = await h.ui.loadChatThreads(false);
  assert.equal(failed.applied, false);
  assert.equal(failed.stale, undefined);
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2"], "失败不能清空或改写列表");
  assert.equal(h.chat.total, 3);
  assert.equal(h.toasts.length, 0, "侧栏失败不弹无关提示");

  h.setThreadList({
    threads: [
      { id: "new-a", title: "New-A" },
      { id: "new-b", title: "New-B" },
    ],
    total: 2,
  });
  const resetResult = await h.ui.loadChatThreads(true);
  assert.equal(resetResult.applied, true);
  assert.deepEqual(threadIds(h.threads()), ["new-a", "new-b"]);
  assert.equal(h.chat.total, 2);
});

test("reset 失败保留旧列表，后续 append 仍使用旧基线的服务端 offset", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);

  const before = h.threadListRequests.length;
  h.setThreadListHandler(request => (
    request.offset === 0
      ? jsonResponse({ error: "boom" }, 500)
      : listPage([{ id: "s3", title: "S3" }], 4, 2, 2)
  ));
  const failed = await h.ui.loadChatThreads(true);
  assert.equal(failed.applied, false);
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2"]);
  assert.equal(h.chat.total, 4);
  assert.equal(h.toasts.length, 0);

  const appended = await h.ui.loadChatThreads(false);
  assert.equal(appended.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 0], [2, 2]],
    "reset 失败后，offset 仍是旧基线已接受的服务端行数",
  );
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2", "s3"]);
  assert.equal(h.chat.total, 4);
});

test("归档已加载服务端记录后，offset 从 2 调整为 1，下一页不跳记录", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);

  assert.equal(moreCount(), 2, "归档前剩余 s3/s4 两条");
  assert.ok(hasMoreButton());
  await archiveThread(h, "s1");
  assert.deepEqual(threadIds(h.threads()), ["s2"]);
  assert.equal(h.chat.total, 3, "服务端 total 同步减 1");
  assert.equal(moreCount(), 2, "归档一条已加载记录后，剩余仍是 s3/s4 两条");
  assert.ok(hasMoreButton());

  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => listPage([
    { id: "s3", title: "S3" },
    { id: "s4", title: "S4" },
  ], 3, 1, 2));
  const appended = await h.ui.loadChatThreads(false);
  assert.equal(appended.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 1]],
    "归档一条已加载记录后，服务端 offset 从 2 调到 1",
  );
  assert.deepEqual(threadIds(h.threads()), ["s2", "s3", "s4"], "下一页不能跳过 s3");
  assert.equal(h.chat.total, 3);
  assert.equal(moreCount(), 0, "服务端记录全部加载后剩余为 0");
  assert.equal(hasMoreButton(), false);
});

test("归档纯本地流式占位不扣减服务端 offset，且不会重新出现", async () => {
  const h = harness();
  h.chat.limit = 2;
  const { running, control } = await startSend(h, "live");
  const liveThread = h.chat.current;
  control.send({ type: "start", thread_id: "live-1", thread_title: "Live" });
  await tick();

  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 2);
  assert.deepEqual(threadIds(h.threads()), ["live-1", "s1", "s2"]);
  assert.equal(moreCount(), 0, "服务端 total 2 已全部加载");
  assert.equal(hasMoreButton(), false);

  await archiveThread(h, "live-1");
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2"]);
  assert.equal(h.chat.total, 2, "纯本地占位不在服务端 total 里，不能扣减");
  assert.equal(moreCount(), 0, "归档本地占位不改变剩余数");
  assert.equal(hasMoreButton(), false);

  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => listPage([{ id: "s3", title: "S3" }], 3, 2, 2));
  const appended = await h.ui.loadChatThreads(false);
  assert.equal(appended.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 2]],
    "删除纯本地占位后 offset 保持 2",
  );
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2", "s3"]);
  assert.equal(h.chat.total, 3);

  h.setThreadList({
    threads: [
      { id: "s1", title: "S1" },
      { id: "s2", title: "S2" },
    ],
    total: 2,
  });
  const reset = await h.ui.loadChatThreads(true);
  assert.equal(reset.applied, true);
  assert.deepEqual(threadIds(h.threads()), ["s1", "s2"], "归档的流式占位不能被 mergeLiveThreads 带回");

  h.setThreadList({ threads: [], total: 0 });
  control.send({ type: "done", message: { id: "live-m", text: "done" } });
  control.close();
  await running;
  assert.equal(h.chat.current, null);
});

test("归档使在途 reset 失效，旧列表不会带回已归档线程", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);

  assert.equal(moreCount(), 2);
  const resetRead = deferred();
  h.setThreadListHandler(() => resetRead.promise);
  const resetRun = h.ui.loadChatThreads(true);
  await tick();

  await archiveThread(h, "s1");
  assert.deepEqual(threadIds(h.threads()), ["s2"]);
  assert.equal(h.chat.total, 3);
  assert.equal(moreCount(), 2);

  resetRead.resolve(listPage([
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4, 0, 2));
  const resetResult = await resetRun;
  assert.equal(resetResult.stale, true, "归档后旧 reset 必须失效");
  assert.deepEqual(threadIds(h.threads()), ["s2"], "已归档线程不能重新出现");
  assert.equal(h.chat.total, 3);
  assert.equal(moreCount(), 2, "旧 reset 不能把剩余数恢复成归档前的值");

  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => listPage([{ id: "s3", title: "S3" }], 3, 1, 2));
  const appended = await h.ui.loadChatThreads(false);
  assert.equal(appended.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 1]],
  );
  assert.deepEqual(threadIds(h.threads()), ["s2", "s3"]);
  assert.equal(moreCount(), 1);
});

test("归档使在途 append 失效，旧页不会带回已归档线程", async () => {
  const h = harness();
  h.chat.limit = 2;
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);

  assert.equal(moreCount(), 2);
  const appendRead = deferred();
  h.setThreadListHandler(() => appendRead.promise);
  const appendRun = h.ui.loadChatThreads(false);
  await tick();

  await archiveThread(h, "s1");
  assert.deepEqual(threadIds(h.threads()), ["s2"]);
  assert.equal(h.chat.total, 3);
  assert.equal(moreCount(), 2);

  appendRead.resolve(listPage([
    { id: "s1", title: "S1" },
    { id: "s3", title: "S3" },
  ], 4, 2, 2));
  const appendResult = await appendRun;
  assert.equal(appendResult.stale, true, "归档后旧 append 必须失效");
  assert.deepEqual(threadIds(h.threads()), ["s2"], "旧页不能写回列表");
  assert.equal(h.chat.total, 3);
  assert.equal(moreCount(), 2, "旧 append 不能把剩余数恢复成归档前的值");

  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => listPage([{ id: "s3", title: "S3" }], 3, 1, 2));
  const next = await h.ui.loadChatThreads(false);
  assert.equal(next.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 1]],
  );
  assert.deepEqual(threadIds(h.threads()), ["s2", "s3"]);
  assert.equal(moreCount(), 1);
});

test("剩余数按服务端未加载行计算：2 条本地占位 + 2 条服务端行、total 4", async () => {
  const h = harness();
  h.chat.limit = 2;
  const first = await startLivePlaceholder(h, "Live A", "live-a");
  const second = await startLivePlaceholder(h, "Live B", "live-b");

  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);
  assert.equal(h.threads().length, 4, "侧栏有 2 条本地占位 + 2 条服务端行");
  assert.equal(moreCount(), 2, "剩余数只算服务端未加载的 2 条");
  assert.ok(hasMoreButton());

  h.setThreadList({ threads: [], total: 0 });
  first.control.send({ type: "done", message: { id: "live-a-m", text: "done" } });
  first.control.close();
  second.control.send({ type: "done", message: { id: "live-b-m", text: "done" } });
  second.control.close();
  await Promise.all([first.running, second.running]);
});

test("服务端已加载记录等于 total 时，本地占位不产生“还有”入口", async () => {
  const h = harness();
  h.chat.limit = 2;
  const live = await startLivePlaceholder(h, "Live", "live-1");
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 2);

  assert.equal(h.threads().length, 3);
  assert.equal(moreCount(), 0, "total 2 已全部加载，本地占位不增加剩余数");
  assert.equal(hasMoreButton(), false, "不能因本地占位显示虚假的加载入口");

  h.setThreadList({ threads: [], total: 0 });
  live.control.send({ type: "done", message: { id: "live-m", text: "done" } });
  live.control.close();
  await live.running;
});

test("归档纯本地占位不改变服务端剩余数", async () => {
  const h = harness();
  h.chat.limit = 2;
  const live = await startLivePlaceholder(h, "Live", "live-1");
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 4);
  assert.equal(moreCount(), 2);

  await archiveThread(h, "live-1");
  assert.equal(h.chat.total, 4, "本地占位归档不扣服务端 total");
  assert.equal(moreCount(), 2, "本地占位归档不改变服务端剩余数");

  const before = h.threadListRequests.length;
  h.setThreadListHandler(() => listPage([{ id: "s3", title: "S3" }], 4, 2, 2));
  const appended = await h.ui.loadChatThreads(false);
  assert.equal(appended.applied, true);
  assert.deepEqual(
    h.threadListRequests.slice(before).map(r => [r.limit, r.offset]),
    [[2, 2]],
  );
  assert.equal(moreCount(), 1, "追加一条服务端记录后剩余数从 2 变 1");
  assert.ok(hasMoreButton());

  h.setThreadList({ threads: [], total: 0 });
  live.control.send({ type: "done", message: { id: "live-m", text: "done" } });
  live.control.close();
  await live.running;
});

test("追加下一页后剩余数按服务端累计行数重算，本地占位不参与", async () => {
  const h = harness();
  h.chat.limit = 2;
  const live = await startLivePlaceholder(h, "Live", "live-1");
  await seedThreads(h, [
    { id: "s1", title: "S1" },
    { id: "s2", title: "S2" },
  ], 5);
  assert.equal(moreCount(), 3, "5 - 2 = 3，本地占位不计入");

  h.setThreadListHandler(() => listPage([
    { id: "s3", title: "S3" },
    { id: "s4", title: "S4" },
  ], 5, 2, 2));
  const firstPage = await h.ui.loadChatThreads(false);
  assert.equal(firstPage.applied, true);
  assert.equal(moreCount(), 1, "再加载 2 条后剩余 1 条");
  assert.ok(hasMoreButton());

  h.setThreadListHandler(() => listPage([{ id: "s5", title: "S5" }], 5, 4, 2));
  const lastPage = await h.ui.loadChatThreads(false);
  assert.equal(lastPage.applied, true);
  assert.equal(moreCount(), 0, "服务端全部加载后剩余为 0");
  assert.equal(hasMoreButton(), false, "本地占位存在也不能显示加载入口");

  h.setThreadList({ threads: [], total: 0 });
  live.control.send({ type: "done", message: { id: "live-m", text: "done" } });
  live.control.close();
  await live.running;
});
