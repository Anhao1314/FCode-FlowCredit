"use strict";

import { applyChatEvent, createChatStreamRegistry } from "../core/chat-streams.js";

/* /audit <target> 斜杠命令:在唯一对话框里触发审计管线。
   解析是纯函数,node --test 直接覆盖;执行走 POST /api/audit/run,
   轮询 /api/audit/jobs/<id>,结果指向发现账本(audit.html)。 */
export function parseAuditCommand(text) {
  const match = /^\s*\/audit\s+(\S[\s\S]*)$/.exec(text || "");
  return match ? match[1].trim() : null;
}

export function auditStatusText(job) {
  if (!job) return "审计任务不存在";
  if (job.status === "running") return `⏳ 审计进行中(job ${job.job_id})…完成后在审计台查看发现`;
  if (job.status === "done") {
    return `✅ 审计完成:${job.findings} 条发现、${job.evidence ?? "—"} 条证据。发现账本:/audit.html`;
  }
  if (job.status === "failed") return `❌ 审计失败:${job.error || "unknown"}`;
  return `审计状态:${job.status}`;
}

/* 对话线程、消息流与模式切换。运行镜像和 Provider 设置分别在独立模块。 */
export function createChatUI(deps) {
  const d = deps;
  const { chat, chatModelChoice } = d;
  const streams = createChatStreamRegistry();
  let openChatSeq = 0;
  let pendingSeq = 0;
  // 侧栏分页状态：baselineVersion 只在 reset 开始/结束时推进；append 必须
  // 绑定发起时的基线版本。serverRows 只统计已接受的服务端行，不含
  // mergeLiveThreads() 插入的本地占位行。
  let resetSeq = 0;
  let resetInFlight = 0;
  let baselineVersion = 0;
  let serverRows = 0;
  let serverIds = new Set();
  let appendInFlight = null;
  // 归档成功的线程不再允许 mergeLiveThreads 把仍在流式的占位行带回侧栏。
  const archivedIds = new Set();

  // 同一线程可能连续多轮：optimistic 消息 id 必须唯一，否则停止后立刻
  // 发下一条时两个 pending-assistant 会争同一个 DOM 节点。
  const pendingId = prefix => `${prefix}-${Date.now()}-${++pendingSeq}`;

  const streamFor = thread => streams.get(thread);
  // “可见”必须同时要求对话视图仍在前台：切到 Work 时 chat.current 可能
  // 仍保留（便于切回），但后台流不得改标题或消息 DOM。
  const visible = thread => chat.active && !!thread && chat.current === thread;

  /* 本地正在流式更新的线程不能被旧的服务端列表快照回滚：保留本地标题，
     服务端还没列出时先补一行，流结束后再以服务端事实为准。 */
  function mergeLiveThreads(list) {
    for (const entry of streams.entries()) {
      const thread = entry.thread;
      if (!thread || !thread.id || archivedIds.has(thread.id)) continue;
      const index = list.findIndex(item => item.id === thread.id);
      if (index < 0) {
        list.unshift({
          id: thread.id,
          title: thread.title || "新对话",
          updated_at: new Date().toISOString(),
        });
      } else if (thread.title && list[index].title !== thread.title) {
        list[index] = { ...list[index], title: thread.title };
      }
    }
    return list;
  }

  function commitThreadList(next, total) {
    if (Number.isFinite(Number(total))) chat.total = Number(total);
    d.setChatThreads(next);
    chat.threads = next;
    renderChats();
  }

  async function fetchThreadPage(offset) {
    const response = await fetch(
      `/api/chat/threads?limit=${chat.limit}&offset=${offset}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data || !Array.isArray(data.threads)) throw new Error("对话列表响应格式不正确");
    return data;
  }

  async function loadChatThreads(reset = true) {
    const health = d.getHealth();
    if (!health || !health.live) return { skipped: true };

    if (reset) {
      const requestId = ++resetSeq;
      resetInFlight = requestId;
      // reset 开始即结束旧基线：此前发出的 append 都不能再写。成功或失败
      // 收尾时再推进一次，让 reset 期间启动的 append 同样失效。
      baselineVersion += 1;
      try {
        const data = await fetchThreadPage(0);
        if (requestId !== resetSeq) return { stale: true };
        baselineVersion += 1;
        resetInFlight = 0;
        serverRows = data.threads.length;
        serverIds = new Set(data.threads.filter(thread => thread && thread.id).map(thread => thread.id));
        const next = mergeLiveThreads(data.threads.filter(thread => thread && !archivedIds.has(thread.id)));
        commitThreadList(next, data.total);
        return { applied: true, total: chat.total, serverRows };
      } catch (error) {
        if (requestId !== resetSeq) return { stale: true, error };
        baselineVersion += 1;
        resetInFlight = 0;
        return { applied: false, error };
      }
    }

    if (appendInFlight) return { skipped: true, reason: "append-in-flight" };
    const token = { version: baselineVersion, offset: serverRows };
    appendInFlight = token;
    try {
      const data = await fetchThreadPage(token.offset);
      // 响应前后都校验基线；reset 在途时也不接受任何 append。
      if (token !== appendInFlight || token.version !== baselineVersion || resetInFlight) {
        return { stale: true, offset: token.offset };
      }
      const current = d.getChatThreads();
      const known = new Set(current.map(thread => thread.id));
      const page = data.threads.filter(thread => thread && thread.id && !archivedIds.has(thread.id));
      const additions = page.filter(thread => !known.has(thread.id));
      serverRows += data.threads.length;
      for (const thread of page) serverIds.add(thread.id);
      const next = mergeLiveThreads(current.concat(additions));
      commitThreadList(next, data.total);
      return { applied: true, offset: token.offset, serverRows };
    } catch (error) {
      if (token !== appendInFlight || token.version !== baselineVersion || resetInFlight) {
        return { stale: true, offset: token.offset, error };
      }
      return { applied: false, offset: token.offset, error };
    } finally {
      if (appendInFlight === token) appendInFlight = null;
    }
  }

  function renderChats() {
    const nav = document.getElementById("chat-nav");
    if (!nav) return;
    const threads = d.getChatThreads();
    const head = document.getElementById("chat-chats");
    if (head) head.innerHTML = d.chatSectionHtml(threads.length);
    const list = threads.filter(thread => !d.isChatPinned(thread.id));
    // 只把服务端未加载行算作“还有 N 条”：本地流式占位不参与，
    // pinned 行已包含在 serverRows 里。
    const remaining = Math.max(0, (Number(chat.total) || 0) - serverRows);
    nav.innerHTML = d.chatRowsHtml(list, {
      activeId: chat.current ? chat.current.id : null,
      total: chat.total,
      remaining,
      pinnedOf: d.isChatPinned,
    });
  }

  function handleChatDelete(threadId, el) {
    if (chat.deleteArmed !== threadId) {
      chat.deleteArmed = threadId;
      el.textContent = "确认";
      el.classList.add("armed");
      clearTimeout(chat.deleteTimer);
      chat.deleteTimer = setTimeout(() => {
        chat.deleteArmed = null;
        chat.deleteTimer = null;
        renderChats();
      }, 3000);
      return;
    }
    chat.deleteArmed = null;
    clearTimeout(chat.deleteTimer);
    (async () => {
      try {
        const response = await fetch(`/api/chat/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        // 只有真正计入过 serverRows 的服务端行才扣减分页位置；纯本地
        // 流式占位不在服务端列表里，扣减会让下一页跳过真实记录。
        const loadedFromServer = serverIds.delete(threadId);
        if (loadedFromServer) {
          serverRows = Math.max(0, serverRows - 1);
          chat.total = Math.max(0, chat.total - 1);
        }
        if (threadId) archivedIds.add(threadId);
        // 归档改变了服务端列表：在途 reset/append 的基线全部失效，避免
        // 旧响应把刚归档的线程重新带回。
        baselineVersion += 1;
        resetSeq += 1;
        resetInFlight = 0;
        appendInFlight = null;
        const next = d.getChatThreads().filter(thread => thread.id !== threadId);
        d.setChatThreads(next);
        chat.threads = next;
        if (chat.current && chat.current.id === threadId) newChat();
        renderChats();
        d.toast("对话已从列表移除（事件链保留）");
      } catch (error) { d.toast("删除失败：" + error.message); }
    })();
  }

  /* 离开视图只清理“投影”（绘制定时器与 DOM 状态），网络流仍归线程所有。 */
  function clearVisiblePaint() {
    const entry = streamFor(chat.current);
    if (entry && entry.paintTimer) {
      clearTimeout(entry.paintTimer);
      entry.paintTimer = null;
    }
    if (chat.paintTimer) { clearTimeout(chat.paintTimer); chat.paintTimer = null; }
  }

  function syncActiveStream() {
    const entry = streamFor(chat.current);
    chat.streaming = !!entry;
    chat.abort = entry ? entry.controller : null;
    paintSendState();
  }

  function leaveChat() {
    clearVisiblePaint();
    const wasActive = chat.active, wasStreaming = chat.streaming;
    chat.active = false;
    chat.streaming = false;
    chat.abort = null;
    chat.editing = null;
    chat.current = null;
    chat.attachments = [];
    renderChatAttachChips();
    document.getElementById("app")?.classList.remove("chat-mode");
    paintChatChrome(false);
    if (wasActive || wasStreaming) restoreSendButton();
  }

  function paintChatChrome(on) {
    const show = (id, visible) => {
      const el = document.getElementById(id);
      if (el) el.style.display = visible ? "" : "none";
    };
    show("chain-badge", !on);
    show("rail-toggle", !on);
    show("approval-btn", !on);
    show("composer-open", !on);
    const attach = document.getElementById("chat-attach");
    if (attach) { attach.hidden = !on; attach.style.display = on ? "" : "none"; }
    const runChip = document.getElementById("run-chip");
    if (runChip && on) runChip.hidden = true;
    if (on) {
      const panel = document.getElementById("attach-panel");
      if (panel) panel.hidden = true;
      const open = document.getElementById("composer-open");
      if (open) open.setAttribute("aria-expanded", "false");
      document.getElementById("composer-box")?.classList.remove("open-attach");
    }
  }

  function renderChatHead() {
    const title = chat.current && chat.current.title ? chat.current.title : "新对话";
    document.getElementById("tb-title").textContent = title;
    document.getElementById("tb-chips").innerHTML = `<span class="chip">对话</span>`;
  }

  function detachChatChrome() {
    clearVisiblePaint();
    chat.active = false;
    chat.streaming = false;
    chat.abort = null;
    chat.editing = null;
    chat.attachments = [];
    renderChatAttachChips();
    document.getElementById("app")?.classList.remove("chat-mode");
    paintChatChrome(false);
    restoreSendButton();
  }

  function openWorkHome() {
    d.invalidateView();
    openChatSeq += 1;
    detachChatChrome();
    if (d.intake.active) d.closeIntake(false);
    if (d.getSettingsOpen()) d.closeSettingsPage(false);
    d.setLedgerOpen(false);
    if (d.activeSession()) d.detachLive();
    d.setChromeState({ rail: false });
    d.store.key = null;
    d.store.trace = null;
    chat.active = false;
    chat.streaming = false;
    chat.editing = null;
    d.renderNav();
    d.renderMain(d.stageEmptyHtml(d.WORK_STAGE_NOTE, { work: true }), { mode: "empty" });
    paintChatChrome(false);
    document.getElementById("tb-title").textContent = "FCode";
    document.getElementById("tb-chips").innerHTML = "";
    const ta = document.getElementById("composer-ta");
    if (ta) { ta.value = ""; d.autosize(ta); ta.placeholder = d.WORK_PLACEHOLDER; ta.focus(); }
    renderChatAttachChips();
    paintSendState();
  }

  function switchToChat() {
    if (chat.current && (chat.current.messages || []).length) {
      chat.active = true;
      chat.editing = null;
      d.renderNav();
      renderChat({ forceBottom: true });
      renderChatHead();
      syncActiveStream();
      paintChatChrome(true);
      const ta = document.getElementById("composer-ta");
      if (ta) { ta.value = ""; d.autosize(ta); ta.placeholder = "给 FCode 发消息…"; }
      paintSendState();
    } else newChat();
  }

  function setUiMode(mode) {
    d.invalidateView();
    openChatSeq += 1;
    if (d.getUiMode() !== mode) {
      d.setUiModeValue(mode);
      try { localStorage.setItem("fa.uiMode", mode); } catch { /* quota */ }
    }
    d.setSidebarMode(mode);
    const app = document.getElementById("app");
    app.classList.toggle("mode-chat", mode === "chat");
    app.classList.toggle("mode-work", mode === "work");
    const chatTab = document.getElementById("mode-chat"), workTab = document.getElementById("mode-work");
    if (chatTab) {
      chatTab.classList.toggle("active", mode === "chat");
      chatTab.setAttribute("aria-selected", mode === "chat" ? "true" : "false");
    }
    if (workTab) {
      workTab.classList.toggle("active", mode === "work");
      workTab.setAttribute("aria-selected", mode === "work" ? "true" : "false");
    }
    const paneChat = document.getElementById("pane-chat"), paneWork = document.getElementById("pane-work");
    if (paneChat) paneChat.hidden = mode !== "chat";
    if (paneWork) paneWork.hidden = mode !== "work";
    const chrome = d.modeChrome(mode);
    const newLabel = document.getElementById("new-label");
    if (newLabel) newLabel.textContent = chrome.newLabel;
    const flavor = document.getElementById("side-flavor");
    if (flavor) { flavor.textContent = chrome.flavor; flavor.hidden = !chrome.flavor; }
    const newBtn = document.getElementById("new-run");
    if (newBtn) newBtn.title = chrome.newTitle;
    d.renderNav();
    d.paintSections();
    if (mode === "chat") {
      if (chat.active && document.getElementById("main")?.dataset.view === "empty") return;
      if (chat.current && (chat.current.messages || []).length) switchToChat();
      else if (!chat.active) newChat();
    } else {
      const view = document.getElementById("main")?.dataset.view;
      if (view === "empty" && !chat.active) {
        d.renderMain(d.stageEmptyHtml(d.WORK_STAGE_NOTE, { work: true }), { mode: "empty" });
        const ta = document.getElementById("composer-ta");
        if (ta) ta.placeholder = d.WORK_PLACEHOLDER;
        return;
      }
      if (d.activeSession()) d.selectRun(d.activeSession().id); else openWorkHome();
    }
  }

  function resetForChat() {
    if (d.intake.active) d.closeIntake(false);
    if (d.getSettingsOpen()) d.closeSettingsPage(false);
    d.setLedgerOpen(false);
    if (d.activeSession()) d.detachLive();
    d.setChromeState({ rail: false });
    d.store.key = null;
    d.store.trace = null;
    chat.active = true;
    chat.current = null;
    chat.streaming = false;
    chat.editing = null;
    chat.attachments = [];
    d.renderNav();
  }

  function newChat() {
    d.invalidateView();
    openChatSeq += 1;
    leaveChat();
    resetForChat();
    d.renderMain(d.stageEmptyHtml("向 FCode 提问，或让 FCode 帮你整理思路", { chat: true }), { mode: "empty" });
    paintChatChrome(true);
    document.getElementById("tb-title").textContent = "FCode";
    document.getElementById("tb-chips").innerHTML = "";
    const ta = document.getElementById("composer-ta");
    if (ta) { ta.value = ""; d.autosize(ta); ta.placeholder = "给 FCode 发消息…"; ta.focus(); }
    renderChatAttachChips();
    paintSendState();
    d.adoptDefaultChatModel(d.getChatConfig() && d.getChatConfig().default_model);
    d.loadChatModels();
  }

  async function openChat(threadId) {
    d.invalidateView();
    const ticket = ++openChatSeq;
    const epoch = d.getViewEpoch();
    const stillCurrent = () => ticket === openChatSeq && epoch === d.getViewEpoch();
    leaveChat();
    resetForChat();

    // 该线程仍在后台生成：直接投影线程缓存，不重新请求旧快照，也不重复发起回答。
    const live = streams.getByThreadId(threadId);
    if (live && live.thread) {
      chat.current = live.thread;
      chat.active = true;
      paintChatChrome(true);
      renderChatHead();
      renderChat({ forceBottom: true });
      syncActiveStream();
      d.renderNav();
      const ta = document.getElementById("composer-ta");
      if (ta) { ta.value = ""; d.autosize(ta); ta.placeholder = "给 FCode 发消息…"; ta.focus(); }
      paintSendState();
      return;
    }

    d.renderMain(`<div class="empty">加载对话…</div>`, { mode: "loading" });
    try {
      const response = await fetch(`/api/chat/threads/${encodeURIComponent(threadId)}`, { cache: "no-store" });
      if (!stillCurrent()) return;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const thread = await response.json();
      if (!stillCurrent()) return;
      chat.current = thread;
      await d.prepareCardRuns(chat.current);
      if (!stillCurrent()) return;
    } catch (error) {
      if (!stillCurrent()) return;
      d.toast("对话加载失败：" + error.message);
      newChat();
      return;
    }
    paintChatChrome(true);
    renderChatHead();
    renderChat({ forceBottom: true });
    d.renderNav();
    const ta = document.getElementById("composer-ta");
    if (ta) { ta.value = ""; d.autosize(ta); ta.placeholder = "给 FCode 发消息…"; ta.focus(); }
    paintSendState();
  }

  function renderChat({ forceBottom = false } = {}) {
    // 后台线程/镜像流不得把对话内容画到当前其他页面。
    if (!chat.active) return;
    const thread = chat.current;
    if (!thread || !(thread.messages || []).length) {
      d.renderMain(`<div class="chat-empty">${d.aiAvatarHtml("idle", 64)}<div class="chat-empty-mark">FCode</div><p>开始一段对话。对话只回答，不执行工具；需要读写文件或运行命令时，用「委托运行」。</p></div>`, { mode: "chat" });
      return;
    }
    const stick = forceBottom || d.nearMainBottom();
    d.renderMain(`<div class="chat-thread" aria-live="polite">${thread.messages.map(message => d.chatMessageHtml(d.withRunStatus(message), chat.editing, d.chatCardHtml(message))).join("")}</div>`, { mode: "chat" });
    if (stick) requestAnimationFrame(d.scrollMainBottom);
  }

  function renderChatAttachChips() {
    const slot = document.getElementById("chat-attach-chips");
    if (!slot) return;
    slot.hidden = !chat.attachments.length;
    slot.innerHTML = chat.attachments.map((file, index) => `<span class="chat-file">${d.esc(file.name)}<button class="chat-file-x" type="button" data-chat-attach-remove="${index}" title="移除">×</button></span>`).join("");
  }

  async function handleChatFiles(event) {
    const files = [...(event.target.files || [])];
    for (const file of files) {
      if (chat.attachments.length >= 3) { d.toast("最多 3 个附件"); break; }
      if (file.size > 64 * 1024) { d.toast(`${file.name} 超过 64 KiB`); continue; }
      let text;
      try { text = await file.text(); } catch { d.toast(`${file.name} 读取失败`); continue; }
      if (text.includes("\u0000")) { d.toast(`${file.name} 不是纯文本文件`); continue; }
      chat.attachments.push({ name: file.name, text });
    }
    event.target.value = "";
    renderChatAttachChips();
    paintSendState();
  }

  function paintSendState() {
    const btn = document.getElementById("send");
    if (!btn || !chat.active) return;
    if (chat.streaming) {
      btn.disabled = false;
      btn.classList.add("stop");
      btn.title = "停止生成";
      btn.setAttribute("aria-label", "停止生成");
      btn.innerHTML = `<span class="stop-glyph"></span>`;
      return;
    }
    btn.classList.remove("stop");
    btn.title = "发送消息";
    btn.setAttribute("aria-label", "发送消息");
    btn.innerHTML = `<span data-doodle="arrow-up"></span>`;
    d.hydrateDoodles(btn);
    const ta = document.getElementById("composer-ta");
    const hasText = !!(ta && ta.value.trim());
    const health = d.getHealth();
    btn.disabled = !(health && health.live) || (!hasText && !chat.attachments.length);
  }

  function restoreSendButton() {
    const btn = document.getElementById("send");
    if (!btn) return;
    btn.classList.remove("stop");
    btn.title = "发送任务";
    btn.setAttribute("aria-label", "发送任务");
    btn.innerHTML = `<span data-doodle="arrow-up"></span>`;
    d.hydrateDoodles(btn);
    const health = d.getHealth();
    btn.disabled = !(health && health.live);
  }

  function setChatStreaming(on) {
    chat.streaming = on;
    if (on) {
      const chip = document.getElementById("run-chip");
      if (chip) chip.hidden = true;
    }
    paintSendState();
  }

  function stopChat() {
    const entry = streamFor(chat.current);
    if (!entry) return;
    try { entry.controller.abort(); } catch { /* settled */ }
  }

  function scheduleChatPaint(entry) {
    if (!entry || !visible(entry.thread)) return;
    entry.paintAssistant = entry.assistant;
    if (entry.paintTimer) return;
    entry.paintTimer = setTimeout(() => {
      entry.paintTimer = null;
      if (!visible(entry.thread)) return;
      paintStreamingAssistant(entry.assistant);
    }, 70);
  }

  function paintStreamingAssistant(assistant) {
    if (!assistant || !chat.current) return;
    if (!(chat.current.messages || []).some(message => message === assistant)) return;
    const node = document.querySelector(`.chat-msg[data-msg-id="${String(assistant.id)}"]`);
    if (!node) { renderChat(); return; }
    const stick = d.nearMainBottom();
    const body = node.querySelector(".chat-md");
    if (body) body.innerHTML = d.renderMarkdown(assistant.text);
    const typing = node.querySelector(".chat-typing");
    if (typing && assistant.text) typing.remove();
    let wait = node.querySelector(".chat-wait");
    if (assistant.waiting) {
      if (!wait) {
        wait = document.createElement("div");
        wait.className = "chat-wait";
        const actions = node.querySelector(".chat-msg-actions");
        node.insertBefore(wait, actions || null);
      }
      wait.textContent = `等待 ${assistant.waiting} 秒额度窗口…`;
    } else if (wait) wait.remove();
    if (stick) d.scrollMainBottom();
  }

  /* 把服务端线程合并回仍在前台的同一个线程对象，而不是替换它。
     已匹配 id 的消息以服务端为准；本地 optimistic 消息（尤其刚发出的下一条）
     保留，服务端多出来的旧消息插在 optimistic 尾部之前。 */
  function mergeServerThread(thread, server) {
    if (!thread || !server) return;
    if (Array.isArray(server.messages)) {
      const used = new Set();
      const byId = new Map(server.messages.filter(msg => msg && msg.id).map(msg => [msg.id, msg]));
      const merged = [];
      for (const local of thread.messages || []) {
        const direct = local && local.id ? byId.get(local.id) : null;
        if (direct && !used.has(direct.id)) {
          merged.push(direct);
          used.add(direct.id);
          continue;
        }
        // 服务端可能给 pending 消息分配了新 id；同角色同文本视为同一条。
        const pending = String((local && local.id) || "").startsWith("pending-");
        const match = pending
          ? server.messages.find(msg => msg && !used.has(msg.id)
            && msg.role === local.role
            && String(msg.text || "") === String(local.text || ""))
          : null;
        if (match) {
          merged.push(match);
          used.add(match.id);
        } else {
          merged.push(local);
        }
      }
      const extras = server.messages.filter(msg => msg && !used.has(msg.id));
      if (extras.length) {
        const tail = merged.findIndex(msg => String((msg && msg.id) || "").startsWith("pending-"));
        if (tail >= 0) merged.splice(tail, 0, ...extras);
        else merged.push(...extras);
      }
      thread.messages = merged;
    }
    for (const key of ["id", "title", "model", "budget", "events", "chain_head", "created_at", "updated_at"]) {
      if (server[key] !== undefined && server[key] !== null && server[key] !== "") thread[key] = server[key];
    }
  }

  function pushOptimisticAnswer() {
    chat.current.messages.push({ id: pendingId("pending-assistant"), role: "assistant", text: "", reasoning: "", status: "streaming", usage: {}, error: {} });
  }

  async function sendChatFromComposer() {
    if (chat.streaming || streamFor(chat.current)) return;
    const health = d.getHealth();
    if (!health || !health.live) { d.toast("回放模式：启动 server.py 后才能对话"); return; }
    const ta = document.getElementById("composer-ta");
    const text = (ta.value || "").trim();
    const attachments = chat.attachments.map(file => ({ name: file.name, text: file.text }));
    if (!text && !attachments.length) return;
    ta.value = "";
    d.autosize(ta);
    chat.attachments = [];
    renderChatAttachChips();
    const threadId = chat.current && chat.current.id ? chat.current.id : null;
    const model = chatModelChoice.key || "mock";
    if (!chat.current) chat.current = { id: null, title: (text || attachments[0].name || "新对话").slice(0, 30), messages: [] };
    if (!chat.current.messages) chat.current.messages = [];
    chat.current.messages.push({ id: pendingId("pending-user"), role: "user", text, attachments: attachments.map(file => ({ name: file.name, bytes: file.text.length })), status: "ok" });
    pushOptimisticAnswer();
    renderChatHead();
    renderChat({ forceBottom: true });
    setChatStreaming(true);
    const auditTarget = parseAuditCommand(text);
    if (auditTarget) {
      // 单一对话框的审计入口:斜杠命令 → 服务器跑审计管线 → 卡片更新状态。
      const assistant = chat.current.messages[chat.current.messages.length - 1];
      try {
        const response = await fetch("/api/audit/run", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target: auditTarget }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        assistant.text = auditStatusText({ status: "running", job_id: data.job_id });
        renderChat({ forceBottom: true });
        for (let poll = 0; poll < 90; poll++) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          const jobRes = await fetch(`/api/audit/jobs/${encodeURIComponent(data.job_id)}`);
          const job = await jobRes.json();
          if (job.status !== "running") { assistant.text = auditStatusText(job); break; }
        }
      } catch (error) {
        assistant.text = `❌ 审计启动失败:${error.message}`;
      }
      assistant.status = "ok";
      setChatStreaming(false);
      renderChat({ forceBottom: true });
      return;
    }
    await streamChat("/api/chat/send", { threadId, text, attachments, model });
  }

  async function streamChat(url, payload) {
    const threadRef = chat.current;
    const assistant = threadRef && threadRef.messages ? threadRef.messages[threadRef.messages.length - 1] : null;
    if (!assistant || assistant.role !== "assistant") { setChatStreaming(false); return; }
    if (streamFor(threadRef)) return;
    const controller = new AbortController();
    const entry = streams.begin(threadRef, { controller, assistant });
    syncActiveStream();
    try {
      const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: controller.signal });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      if (!response.body) throw new Error("浏览器没有拿到流式响应");
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = d.parseSseFrame(frame);
          // 事件总是写入它所属的线程；是否绘制由 visible() 决定。
          if (event) handleChatEvent(event, assistant, threadRef, entry);
        }
      }
    } catch (error) {
      if (error.name === "AbortError") {
        entry.aborted = true;
        assistant.status = "cancelled";
        if (!assistant.text) assistant.text = "";
      } else {
        assistant.status = "error";
        assistant.error = { code: "CHAT_FAILED", message: error.message };
        if (visible(threadRef)) d.toast("对话失败：" + error.message);
      }
    } finally {
      const wasVisible = visible(threadRef);
      const generation = entry.generation;
      // 回读属于发起它的那一代回答。同一线程只要开始了新流（例如 done 后
      // 立刻发下一条），旧回读就放弃，不得覆盖新一轮的 optimistic 消息。
      const sameGeneration = () => visible(threadRef)
        && streams.generationFor(threadRef) === generation
        && !streamFor(threadRef);
      streams.end(entry);
      if (assistant.status === "streaming") {
        assistant.status = "error";
        assistant.error = { code: "CHAT_STREAM_ENDED", message: "连接在回答完成前结束" };
      }
      if (wasVisible) syncActiveStream();
      if (entry.aborted && wasVisible) await new Promise(resolve => setTimeout(resolve, 250));
      // 后台回读不得覆盖当前会话；只有仍显示这个线程、且没有新一代流时才合并。
      if (wasVisible && threadRef.id && sameGeneration()) {
        try {
          const response = await fetch(`/api/chat/threads/${encodeURIComponent(threadRef.id)}`, { cache: "no-store" });
          if (response.ok) {
            const thread = await response.json();
            if (sameGeneration() && thread && Array.isArray(thread.messages)) {
              mergeServerThread(threadRef, thread);
            }
          }
        } catch { /* 保留乐观视图 */ }
      }
      if (visible(threadRef)) {
        renderChat();
        renderChatHead();
      }
      await loadChatThreads();
    }
  }

  function handleChatEvent(event, assistant, threadRef, entry) {
    if (!threadRef) return;
    const outcome = applyChatEvent(threadRef, assistant, event);
    if (!visible(threadRef)) {
      if (outcome.render) renderChats();
      return;
    }
    if (outcome.paint) scheduleChatPaint(entry);
    if (outcome.render) {
      if (outcome.head) renderChatHead();
      renderChat();
    }
  }

  function beginChatEdit(messageId) {
    chat.editing = messageId;
    renderChat();
    requestAnimationFrame(() => document.getElementById("chat-edit-input")?.focus());
  }

  function cancelChatEdit() { chat.editing = null; renderChat(); }

  function saveChatEdit(messageId) {
    if (!chat.current || !chat.current.id || streamFor(chat.current)) return;
    const input = document.getElementById("chat-edit-input");
    const text = (input && input.value || "").trim();
    if (!text) { d.toast("消息不能为空"); return; }
    const index = chat.current.messages.findIndex(message => message.id === messageId);
    if (index < 0) return;
    chat.editing = null;
    chat.current.messages = chat.current.messages.slice(0, index);
    chat.current.messages.push({ id: pendingId("pending-user"), role: "user", text, attachments: [], status: "ok" });
    pushOptimisticAnswer();
    renderChat({ forceBottom: true });
    setChatStreaming(true);
    streamChat(`/api/chat/threads/${encodeURIComponent(chat.current.id)}/edit`, { messageId, text, model: chatModelChoice.key || "mock" });
  }

  function regenerateChat(messageId) {
    if (!chat.current || !chat.current.id || streamFor(chat.current)) return;
    const index = chat.current.messages.findIndex(message => message.id === messageId);
    if (index < 0) return;
    chat.current.messages = chat.current.messages.slice(0, index);
    pushOptimisticAnswer();
    renderChat({ forceBottom: true });
    setChatStreaming(true);
    streamChat(`/api/chat/threads/${encodeURIComponent(chat.current.id)}/regenerate`, { messageId, model: chatModelChoice.key || "mock" });
  }

  function copyChatMessage(messageId) {
    if (!chat.current) return;
    const message = chat.current.messages.find(item => item.id === messageId);
    if (!message) return;
    d.copyText(message.text).then(ok => d.toast(ok ? "已复制回答" : "复制失败"));
  }

  function delegateChatMessage(messageId) {
    if (!chat.current) return;
    const message = chat.current.messages.find(item => item.id === messageId);
    if (!message || message.role !== "user") return;
    d.startIntake(message.text, { origin: d.threadOrigin(chat.current) });
  }

  return {
    loadChatThreads, renderChats, handleChatDelete, leaveChat, paintChatChrome, renderChatHead,
    detachChatChrome, openWorkHome, switchToChat, setUiMode, newChat, openChat, renderChat,
    renderChatAttachChips, handleChatFiles, paintSendState, restoreSendButton, setChatStreaming,
    stopChat, sendChatFromComposer, streamChat, beginChatEdit, cancelChatEdit, saveChatEdit,
    regenerateChat, copyChatMessage, delegateChatMessage,
  };
}
