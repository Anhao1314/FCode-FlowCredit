"use strict";

/* 会话生命周期与运行流连接（自 app.js 拆分切片 3d 迁入）。
 *
 * 一条运行 = 一个会话对象（sessions Map）+ 一条 EventSource 流。
 * 连接状态由 core/sse-status 的纯 reducer 管理：快照/append/done 推进，
 * 断流只降级展示、绝不伪造任务终态。前台会话走 liveApply 状态机（app 侧
 * 注入），后台会话只 stash 状态与标签点。finishSession 是唯一把会话
 * 置为终态的地方：关流、清乐观态、队列消息落草稿、刷新运行列表。
 */

import { esc } from "../core/format.js";
import { connectionPresentation, initialConnection, reduceConnection } from "../core/sse-status.js";

export function createLiveSessions(deps) {
  const d = deps;

  function createSession(id, objective, template, confirmedModel = "") {
    return {
      id, objective, template, confirmedModel, es: null, trace: null, lastData: null,
      connection: initialConnection(),
      running: true, finished: false, failed: false, lastError: "",
      eventCount: 0, resultLoaded: false, threadCache: null,
      pending: null, pendingDeadline: 0, deciding: null, answering: false,
      planEditing: false, planConfirm: null, planQueued: null,
      budgetEditing: false, budgetQueued: null,
      autoReport: false, queuedMsgs: [],
    };
  }

  function tabTitle(s) {
    return s.objective ? d.shortObjective(s.objective) : d.tplTitle(s.template);
  }

  function openLive(id, objective, template, confirmedModel = "") {
    d.viewGate.begin();
    d.leaveChat();
    d.setLedgerOpen(false);
    const fresh = !d.sessions.has(id);
    if (fresh) d.sessions.set(id, createSession(id, objective, template, confirmedModel));
    const s = d.sessions.get(id);
    if (confirmedModel && !s.confirmedModel) s.confirmedModel = confirmedModel;
    d.store.sessionId = s.id;
    d.store.key = "run:" + id;
    d.renderNav();
    d.renderTabs();
    if (fresh) {
      d.setComposerRunning(true);
      document.getElementById("tb-title").textContent = d.tplTitle(template);
      document.getElementById("tb-chips").innerHTML = "";
      document.getElementById("rail-body").innerHTML = "";
      d.renderMain(`<div class="empty">任务已提交，等待 Runtime 启动…</div>`, { mode: "loading", scroll: "top" });
      paintConnection(s);
      attachStream(s);
    } else {
      repaintSession(s);
    }
  }

  function hideConnectionStatus() {
    const pill = document.getElementById("conn-pill");
    if (pill) pill.hidden = true;
    const banner = document.getElementById("conn-banner");
    if (banner) banner.hidden = true;
  }

  function paintConnection(s) {
    if (!s || d.activeSession() !== s) return;
    const view = connectionPresentation(s.connection || initialConnection());
    const pill = document.getElementById("conn-pill");
    if (pill) {
      pill.hidden = !view.showPill;
      pill.dataset.tone = view.tone || "";
      const text = document.getElementById("conn-pill-text");
      if (text) text.textContent = view.pill;
      pill.title = view.banner || (view.showPill ? "运行流连接状态" : "");
    }
    const banner = document.getElementById("conn-banner");
    if (banner) {
      banner.hidden = !view.showBanner;
      banner.dataset.tone = view.tone || "";
      const text = document.getElementById("conn-banner-text");
      if (text) text.textContent = view.banner;
    }
  }

  function setConnection(s, action) {
    if (!s) return;
    s.connection = reduceConnection(s.connection || initialConnection(), action);
    paintConnection(s);
  }

  /* 增量帧：只合并变化部分，渲染复用现有 liveApply（字符串 diff 保持不变） */
  function applyDelta(trace, delta) {
    if (Array.isArray(delta.events) && delta.events.length) {
      trace.events = (trace.events || []).concat(delta.events);
    }
    if (Array.isArray(delta.rounds) && delta.rounds.length) {
      const start = Math.max(0, delta.roundStart || 0);
      trace.rounds = (trace.rounds || []).slice(0, start).concat(delta.rounds);
    }
    if (delta.terminal) trace.terminal = delta.terminal;
    if (delta.chain) trace.chain = delta.chain;
    if (delta.task) trace.task = delta.task;
    if (delta.model) trace.model = delta.model;
  }

  function attachStream(s) {
    const es = new EventSource(`/api/runs/${encodeURIComponent(s.id)}/stream`);
    s.es = es;
    es.onopen = () => {
      if (s.es !== es || s.finished || s.failed) return;
      setConnection(s, { type: "opened" });
    };
    es.onerror = () => {
      // EventSource 的 error 只说明连接断了；浏览器会自动重连。任务状态保持
      // 最后已知值，绝不在这里伪造 failed。
      if (s.es !== es || s.finished || s.failed) return;
      setConnection(s, { type: "error", readyState: es.readyState });
    };
    es.addEventListener("snapshot", event => {
      if (s.failed || s.finished) return;
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      setConnection(s, { type: "snapshot" });
      if (d.activeSession() !== s) { stashSessionUpdate(s, data); return; }
      d.liveApply(s, data);
      if (data.finished) finishSession(s, data);
    });
    es.addEventListener("append", event => {
      if (s.failed || s.finished || !s.trace) return;
      let delta;
      try { delta = JSON.parse(event.data); } catch { return; }
      setConnection(s, { type: "append" });
      applyDelta(s.trace, delta);
      const data = {
        snapshot: s.trace,
        pending: delta.pending || null,
        queue: delta.queue,
        finished: !!delta.finished,
        status: delta.status,
        tracePath: delta.tracePath,
        error: delta.error,
      };
      if (d.activeSession() !== s) { stashSessionUpdate(s, data); return; }
      d.liveApply(s, data);
      if (data.finished) finishSession(s, data);
    });
    es.addEventListener("done", event => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* empty */ }
      setConnection(s, { type: "done" });
      if (d.activeSession() !== s) {
        stashSessionUpdate(s, Object.assign({}, s.lastData || {}, { finished: true, status: data.status, error: data.error }));
        return;
      }
      finishSession(s, data);
    });
    es.addEventListener("failed", event => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* empty */ }
      setConnection(s, { type: "failed", error: data.error });
      if (s.es) { s.es.close(); s.es = null; }
      s.failed = true;
      s.running = false;
      s.lastError = String(data.error || "Runtime 未返回任何快照");
      d.refreshRuns({ force: true });
      if (d.activeSession() !== s) {
        d.renderTabs();
        d.notify("FCode · 运行失败", s.lastError.slice(0, 120));
        return;
      }
      if (s.trace) {
        // 有快照就保留线程，把“没有可读快照”的原因写进显式错误条目，
        // 不伪造 failed、也不清空已经看到的工具结果。
        d.paintThread(s, s.trace);
      } else {
        d.renderMain(
          `<div class="empty">运行失败：${esc(s.lastError)}
          <div class="actions" style="justify-content:center"><button class="a-btn" data-act="retry">重试</button></div>
        </div>`,
          { mode: "error" }
        );
      }
      d.setComposerRunning(false);
      d.notify("FCode · 运行失败", s.lastError.slice(0, 120));
    });
  }

  /* 后台会话：只更新状态与标签点，不动 DOM */
  function stashSessionUpdate(s, data) {
    if (data && data.snapshot) s.trace = data.snapshot;
    if (data) s.lastData = data;
    if (data && data.finished && !s.finished && !s.failed) finishSession(s, data);
    else d.renderTabs();
  }

  /* 激活一个已有会话：从缓存状态重建画面 */
  function repaintSession(s) {
    if (!s.trace) {
      d.renderMain(`<div class="empty">任务已提交，等待 Runtime 启动…</div>`, { mode: "loading" });
      return;
    }
    if (s.finished || (s.trace.terminal && s.trace.terminal.status)) {
      d.setComposerRunning(false);
      d.store.trace = s.trace;
      d.render(s.trace, tabTitle(s));
      return;
    }
    d.setComposerRunning(true);
    d.liveShell(s.trace);
    s.threadCache = new Map();
    s.eventCount = 0;
    d.liveApply(s, s.lastData || { snapshot: s.trace, pending: null, finished: false });
    paintConnection(s);
  }

  function finishSession(s, data) {
    if (s.finished) { if (d.activeSession() !== s) d.renderTabs(); return; }
    setConnection(s, { type: "done" });
    s.finished = true;
    s.running = false;
    s.pending = null;
    s.deciding = null;
    s.answering = false;
    if (s.es) { s.es.close(); s.es = null; }
    s.queueEditingId = null;
    let draftCount = 0;
    s.queuedMsgs.forEach(m => {
      if (m.state === "queued") { m.state = "draft"; m.steered = false; draftCount += 1; }
    });
    const trace = s.trace || (s.lastData && s.lastData.snapshot) || null;
    s.trace = trace;
    d.refreshRuns({ force: true });
    const status = (trace && trace.terminal && trace.terminal.status) || (data && data.status) || "?";
    if (d.activeSession() !== s) {
      d.renderTabs();
      d.notify(
        status === "succeeded" ? "FCode · 运行完成" : `FCode · 运行结束（${status}）`,
        `${tabTitle(s)} · 后台完成`
      );
      return;
    }
    d.paintApproval();
    d.paintQueuedMsgs();
    d.setComposerRunning(false);
    if (trace) {
      if (trace.terminal && trace.terminal.status) {
        d.paintThread(s, trace);
        d.loadReviewProfile(trace);
        d.loadProjectDelivery(trace);
      }
      d.cache.set("run:" + s.id, trace);
      d.verifyChain(trace).then(check => {
        if (d.activeSession() !== s) return;
        d.store.check = check;
        d.setChainBadge(check);
        d.renderRail(trace, check);
      });
      const rounds = (trace.rounds || []).length;
      d.notify(
        status === "succeeded" ? "FCode · 运行完成" : `FCode · 运行结束（${status}）`,
        `${d.shortObjective(s.objective || "")} · ${rounds} 轮`
      );
    }
    d.renderTabs();
    if (d.activeSession() === s) {
      d.updateStatusline();
      d.paintQueuedMsgs();
      if (draftCount) d.toast(`未送达的 ${draftCount} 条已保留为草稿`);
    }
    if (s.autoReport && status === "succeeded" && d.activeSession() === s) {
      s.autoReport = false;
      setTimeout(() => { if (d.activeSession() === s && d.shownTrace()) d.openReport(); }, 300);
    }
  }

  function detachLive() {
    /* 切走：会话保留在后台继续流式，不再渲染 */
    d.store.sessionId = null;
    d.setComposerRunning(false);
    d.renderTabs();
  }

  function closeSessionTab(id) {
    const s = d.sessions.get(id);
    if (!s) return;
    setConnection(s, { type: "close" });
    if (s.es) { s.es.close(); s.es = null; }
    d.sessions.delete(id);
    if (d.store.sessionId === id) {
      d.store.sessionId = null;
      d.setComposerRunning(false);
      d.renderMain(`<div class="empty">标签已关闭。运行仍在服务器上继续，可从左侧运行记录重新打开。</div>`, { mode: "run" });
      document.getElementById("chain-badge").className = "chain-pill";
      document.getElementById("chain-badge").innerHTML = `<span class="dot"></span><span>未加载</span>`;
    }
    const rest = [...d.sessions.values()];
    if (rest.length) openLive(rest[rest.length - 1].id, rest[rest.length - 1].objective, rest[rest.length - 1].template);
    else { d.renderNav(); d.renderTabs(); }
    d.refreshRuns();
  }

  return {
    createSession, tabTitle, openLive,
    hideConnectionStatus, paintConnection, setConnection,
    applyDelta, attachStream, stashSessionUpdate, repaintSession,
    finishSession, detachLive, closeSessionTab,
  };
}
