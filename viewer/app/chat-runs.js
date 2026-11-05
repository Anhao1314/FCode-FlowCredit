"use strict";

import { isExecutionProposal } from "../core/run-card.js";

/* 对话内委托卡与运行镜像。对话消息仍由 app.js 管理，这里只处理提案到运行的边界。 */
export function createChatRuns(deps) {
  const {
    esc, actionCardHtml, runMirrorHtml, mirrorFrom, stripTags, describe, chat, modelChoice,
    getApprovalMode, getRunHistory, renderChat, loadChatThreads, toast, applyDelta, setUiMode, openLive,
    modelSupportsEffort,
  } = deps;
  const TERMINAL_STATUS = ["succeeded", "failed", "stopped"];
  const rowDescription = observation => stripTags((describe(observation) || [])[0] || "");
  const cssEscape = value => (window.CSS && window.CSS.escape
    ? window.CSS.escape(String(value))
    : String(value).replace(/["\\]/g, "\\$&"));

  function chatCardHtml(message) {
    // The run mirror is anchored by the server's chat_run_started projection,
    // not by the proposal: keep it even when the action is absent.
    if (message.run) return runMirrorHtml(message, mirrorView(message), { run: message.run });
    if (!isExecutionProposal(message.action)) return "";
    const state = chat.cards.get(message.id) || {};
    if (state.dismissed) {
      return `<div class="run-card dismissed" data-card="${esc(message.id)}" data-run-card="${esc(message.id)}">
        <span class="run-card-hint">已忽略这个提案</span>
        <span class="grow"></span>
        <button type="button" class="run-act" data-run-reconsider="${esc(message.id)}">重新考虑</button>
      </div>`;
    }
    return actionCardHtml(message, {
      draft: state.draft,
      mode: state.mode,
      sources: state.sources,
      busy: state.busy,
      error: state.error,
      approval: getApprovalMode(),
    });
  }

  function cardState(messageId) {
    if (!chat.cards.has(messageId)) chat.cards.set(messageId, {});
    return chat.cards.get(messageId);
  }

  function readCardDraft(messageId) {
    const node = document.querySelector(`[data-run-card="${cssEscape(messageId)}"]`);
    if (!node) return;
    const state = cardState(messageId);
    const goal = node.querySelector("[data-run-goal]");
    const sources = node.querySelector("[data-run-sources]");
    if (goal) state.draft = goal.value;
    if (sources) state.sources = sources.value;
  }

  function paintCard(messageId) {
    const message = chatMessageById(messageId);
    if (!message) return;
    const node = document.querySelector(`[data-run-card="${cssEscape(messageId)}"]`);
    if (!node) { renderChat(); return; }
    const holder = document.createElement("div");
    holder.innerHTML = chatCardHtml(message);
    const next = holder.firstElementChild;
    if (next) node.replaceWith(next);
  }

  function chatMessageById(messageId) {
    const thread = chat.current;
    if (!thread || !thread.messages) return null;
    return thread.messages.find(item => item.id === messageId) || null;
  }

  function parseSourceLines(text) {
    return String(text || "").split(/[\n,，\s]+/).map(item => item.trim()).filter(Boolean);
  }

  async function startCardRun(messageId) {
    const message = chatMessageById(messageId);
    if (!message || !isExecutionProposal(message.action) || message.run) return;
    readCardDraft(messageId);
    const state = cardState(messageId);
    if (state.busy) return;
    const goal = String(state.draft !== undefined ? state.draft : message.action.goal || "").trim();
    const mode = state.mode || message.action.mode || "script";
    const sources = mode === "research"
      ? parseSourceLines(state.sources !== undefined ? state.sources : (message.action.sources || []).join("\n"))
      : [];
    if (goal.length < 5 || goal.length > 600) {
      state.error = "目标需要 5 到 600 个字";
      paintCard(messageId);
      return;
    }
    if (mode === "research" && (sources.length < 1 || sources.length > 5)) {
      state.error = "研究方式需要 1 到 5 个 http(s) 来源";
      paintCard(messageId);
      return;
    }
    state.busy = true;
    state.error = "";
    paintCard(messageId);
    // The work-model preference is not the chat model: an effort level must not
    // ride along for models that do not support it (the mock refuses "medium").
    const chosenModel = modelChoice.key || "mock";
    const effort = chosenModel === "mock"
      ? ""
      : (modelSupportsEffort ? (modelSupportsEffort(chosenModel) ? (modelChoice.effort || "") : "") : "");
    try {
      const response = await fetch(`/api/chat/threads/${encodeURIComponent(chat.current.id)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messageId, goal, mode, sources,
          approval: getApprovalMode(),
          model: chosenModel,
          effort,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const run = payload.run || {};
      message.run = { id: run.id, goal, mode, at: new Date().toISOString() };
      chat.cards.delete(messageId);
      seedMirror(messageId, run);
      paintCard(messageId);
      loadChatThreads();
      toast(`已开始运行 ${run.id}`);
    } catch (error) {
      state.busy = false;
      state.error = error.message;
      paintCard(messageId);
    }
  }

  function dismissCard(messageId) {
    const state = cardState(messageId);
    state.dismissed = true;
    paintCard(messageId);
  }

  function reconsiderCard(messageId) {
    const state = cardState(messageId);
    state.dismissed = false;
    paintCard(messageId);
  }

  function withRunStatus(message) {
    if (!message.run || !message.run.id) return message;
    const view = mirrorView(message);
    if (!view.status) return message;
    return Object.assign({}, message, { run: Object.assign({}, message.run, { status: view.status }) });
  }

  function mirrorView(message) {
    const state = chat.mirrors.get(message.id);
    const run = message.run || {};
    const live = Object.assign(
      { finished: ["succeeded", "failed", "stopped"].includes(run.status) },
      state ? state.live : {},
    );
    return mirrorFrom(state ? state.trace : null, live, rowDescription);
  }

  async function prepareCardRuns(thread) {
    const anchored = (thread.messages || []).filter(message => message.run && message.run.id);
    await Promise.all(anchored.map(async message => {
      if (chat.mirrors.has(message.id)) return;
      const runId = message.run.id;
      const [record, trace] = await Promise.all([
        fetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" })
          .then(response => response.ok ? response.json() : null).catch(() => null),
        // The trace is what the mirror actually shows (rounds + terminal);
        // the history record alone would leave it saying "waiting" forever.
        fetch(`/api/runs/${encodeURIComponent(runId)}/trace`, { cache: "no-store" })
          .then(response => response.ok ? response.json() : null).catch(() => null),
      ]);
      if (chat.current !== thread) return;
      const merged = Object.assign({ id: runId }, record || {}, trace && trace.terminal ? trace : {});
      if (trace && Array.isArray(trace.rounds) && trace.rounds.length) {
        merged.rounds = trace.rounds;
        merged.events = trace.events || merged.events;
        merged.terminal = trace.terminal || merged.terminal;
      }
      seedMirror(message.id, merged);
    }));
  }

  function seedMirror(messageId, run) {
    const entry = chat.mirrors.get(messageId) || { runId: run.id, trace: null, live: {}, es: null };
    const terminal = run.terminal || {};
    const status = run.status || terminal.status || "";
    entry.runId = run.id;
    // A fetched trace is the mirror's own fact set; keep it for rendering.
    if (!entry.trace && Array.isArray(run.rounds)) entry.trace = run;
    entry.live = Object.assign({}, entry.live, {
      status: status || entry.live.status || "",
      error: run.error || (terminal.failure || {}).message || entry.live.error || "",
      cost: run.cost || entry.live.cost || null,
      finished: TERMINAL_STATUS.includes(status) || entry.live.finished,
    });
    chat.mirrors.set(messageId, entry);
    attachMirror(messageId, entry);
    return entry;
  }

  function attachMirror(messageId, entry) {
    if (!entry || entry.es || !entry.runId) return;
    const es = new EventSource(`/api/runs/${encodeURIComponent(entry.runId)}/stream`);
    entry.es = es;
    const absorb = payload => {
      if (payload.snapshot) entry.trace = payload.snapshot;
      entry.live = Object.assign({}, entry.live, {
        status: payload.status || entry.live.status,
        error: payload.error || "",
        pending: payload.pending || null,
        cost: payload.cost || entry.live.cost || null,
        finished: !!payload.finished,
      });
      const deciding = entry.live.deciding;
      if (deciding && (!entry.live.pending || entry.live.pending.id !== deciding.pendingId)) {
        entry.live.deciding = null;
      }
      paintCard(messageId);
    };
    es.addEventListener("snapshot", event => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      absorb(data);
      if (data.finished) closeMirror(entry, data);
    });
    es.addEventListener("append", event => {
      let delta;
      try { delta = JSON.parse(event.data); } catch { return; }
      if (entry.trace) applyDelta(entry.trace, delta);
      absorb({ status: delta.status, error: delta.error, pending: delta.pending, finished: !!delta.finished });
      if (delta.finished) closeMirror(entry, delta);
    });
    es.addEventListener("done", event => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* empty */ }
      absorb(Object.assign({}, entry.live, { status: data.status, error: data.error, finished: true }));
      closeMirror(entry, data);
    });
    es.addEventListener("failed", event => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* empty */ }
      if (!TERMINAL_STATUS.includes(entry.live.status)) entry.live.error = data.error || "运行未留下可读快照";
      entry.live.finished = true;
      closeMirror(entry, data);
    });
  }

  function closeMirror(entry, data) {
    if (entry.es) {
      entry.es.close();
      entry.es = null;
    }
    entry.live.finished = true;
    if (data && data.status) entry.live.status = data.status;
    paintCardFor(entry);
  }

  function paintCardFor(entry) {
    for (const [messageId, candidate] of chat.mirrors) {
      if (candidate === entry) paintCard(messageId);
    }
  }

  async function decideFromCard(messageId, pendingId, allow) {
    const message = chatMessageById(messageId);
    const entry = chat.mirrors.get(messageId);
    if (!message || !message.run || !entry || entry.live.deciding) return;
    entry.live.deciding = { allow, pendingId };
    paintCard(messageId);
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(message.run.id)}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvalId: pendingId, allow, remember: false }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      toast(allow ? "已允许 · 运行继续" : "已拒绝 · 运行将终止");
    } catch (error) {
      entry.live.deciding = null;
      entry.live.error = error.message;
      paintCard(messageId);
      toast("决定未送达：" + error.message);
    }
  }

  async function stopFromCard(runId) {
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      toast("已在轮次边界请求停止");
    } catch (error) {
      toast("停止请求未送达：" + error.message);
    }
  }

  async function openRunFromCard(runId) {
    const known = getRunHistory().find(run => run.id === runId);
    if (known) {
      setUiMode("work");
      openLive(known.id, known.objective, known.template);
      return;
    }
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const run = await response.json();
      setUiMode("work");
      openLive(run.id, run.objective, run.template);
    } catch (error) {
      toast("打开运行失败：" + error.message);
    }
  }

  return {
    chatCardHtml, cardState, readCardDraft, paintCard, startCardRun, dismissCard, reconsiderCard,
    withRunStatus, prepareCardRuns, decideFromCard, stopFromCard, openRunFromCard,
  };
}
