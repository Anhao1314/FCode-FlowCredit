"use strict";

/* Chat 流按线程拥有生命周期：控制器、assistant 与绘制定时器都挂在
   thread 的流条目上，切换视图只销毁“投影”，不中止网络流。 */

export function createChatStreamRegistry() {
  const keys = new WeakMap();
  const generations = new WeakMap();
  const streams = new Map();
  let localSeq = 0;

  function keyFor(thread) {
    if (!thread || typeof thread !== "object") return null;
    let key = keys.get(thread);
    if (!key) {
      key = thread.id ? `id:${thread.id}` : `local:${++localSeq}`;
      keys.set(thread, key);
    }
    return key;
  }

  return {
    keyFor,
    get(thread) {
      const key = keyFor(thread);
      return key ? streams.get(key) || null : null;
    },
    getByThreadId(threadId) {
      if (!threadId) return null;
      for (const entry of streams.values()) {
        if (entry.thread && entry.thread.id === threadId) return entry;
      }
      return null;
    },
    begin(thread, { controller, assistant }) {
      const key = keyFor(thread);
      const generation = (generations.get(thread) || 0) + 1;
      generations.set(thread, generation);
      const entry = {
        key,
        generation,
        thread,
        controller,
        assistant,
        aborted: false,
        paintTimer: null,
      };
      streams.set(key, entry);
      return entry;
    },
    /* 回读等异步收尾必须绑定发起时的回答世代：同一线程只要开始了
       新流，旧世代的回读就不得再写线程或消息。 */
    generationFor(thread) {
      if (!thread || typeof thread !== "object") return 0;
      return generations.get(thread) || 0;
    },
    end(entry) {
      if (!entry) return;
      if (entry.paintTimer) {
        clearTimeout(entry.paintTimer);
        entry.paintTimer = null;
      }
      if (streams.get(entry.key) === entry) streams.delete(entry.key);
    },
    entries() {
      return [...streams.values()];
    },
  };
}

/* 把一条 Chat SSE 事件应用到它所属的线程；调用方负责决定是否绘制。 */
export function applyChatEvent(threadRef, assistant, event) {
  if (!threadRef || !assistant || !event) return {};
  switch (event.type) {
    case "start": {
      if (!event.thread_id) return {};
      threadRef.id = event.thread_id;
      if (event.thread_title) threadRef.title = event.thread_title;
      if (event.user) {
        const pending = [...threadRef.messages].reverse()
          .find(message => message.role === "user" && String(message.id).startsWith("pending-user-"));
        if (pending) Object.assign(pending, event.user);
      }
      if (event.assistant_id) assistant.id = event.assistant_id;
      return { render: true, head: true };
    }
    case "wait":
      assistant.waiting = Number(event.seconds) || 0;
      return { paint: true };
    case "reasoning":
      assistant.reasoning = (assistant.reasoning || "") + String(event.text || "");
      return { paint: true };
    case "delta":
      assistant.text = (assistant.text || "") + String(event.text || "");
      // 有序 blocks：与文本交错的工具卡靠它渲染（旧消息没有 blocks）。
      if (!Array.isArray(assistant.blocks)) assistant.blocks = [];
      const lastBlock = assistant.blocks[assistant.blocks.length - 1];
      if (lastBlock && lastBlock.type === "text") lastBlock.text += String(event.text || "");
      else assistant.blocks.push({ type: "text", text: String(event.text || "") });
      return { paint: true };
    case "tool_call": {
      if (!Array.isArray(assistant.blocks)) assistant.blocks = [];
      const call = {
        type: "tool", call_id: event.call_id, name: event.name,
        arguments: event.arguments || "{}", round: event.round,
        status: "running",
      };
      assistant.blocks.push(call);
      if (!Array.isArray(assistant.tool_calls)) assistant.tool_calls = [];
      assistant.tool_calls.push(call);
      return { render: true };
    }
    case "tool_result": {
      if (!Array.isArray(assistant.blocks)) assistant.blocks = [];
      for (const block of assistant.blocks) {
        if (block.type !== "tool" || block.call_id !== event.call_id) continue;
        block.status = event.status || "ok";
        if (event.error) block.error = event.error;
        if (event.result !== undefined) block.result = event.result;
        if (event.result_hash) block.result_hash = event.result_hash;
        if (event.trust) block.trust = event.trust;
      }
      return { render: true };
    }
    case "done":
      if (event.message) {
        assistant.id = event.message.id || assistant.id;
        assistant.text = event.message.text || assistant.text;
        assistant.reasoning = event.message.reasoning || assistant.reasoning;
        assistant.usage = event.message.usage || {};
        assistant.receipt_ref = event.message.receipt_ref || "";
        if (Array.isArray(event.message.blocks)) assistant.blocks = event.message.blocks;
        if (Array.isArray(event.message.tool_calls)) assistant.tool_calls = event.message.tool_calls;
        assistant.status = "ok";
      }
      assistant.waiting = 0;
      return { render: true };
    case "error":
      if (event.thread_id && !threadRef.id) threadRef.id = event.thread_id;
      assistant.status = "error";
      assistant.error = { code: event.code || "CHAT_FAILED", message: event.message || "对话失败" };
      if (event.partial) assistant.text = event.partial;
      return { render: true };
    case "stopped":
      assistant.status = "cancelled";
      return { render: true };
    default:
      return {};
  }
}