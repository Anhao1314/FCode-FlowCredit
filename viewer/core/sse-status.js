"use strict";

/* Work 运行流的连接状态机。它只描述“前端与运行流之间的连接”，
   与任务终态分离：EventSource 的 error 不能伪造成 failed。 */

export const SSE_PHASE = Object.freeze({
  CONNECTING: "connecting",
  CONNECTED: "connected",
  RECONNECTING: "reconnecting",
  OFFLINE: "offline",
  FINISHED: "finished",
  CLOSED: "closed",
});

const SETTLED = new Set([SSE_PHASE.FINISHED, SSE_PHASE.CLOSED]);
const EVENTSOURCE_CLOSED = 2;

export function initialConnection(phase = SSE_PHASE.CONNECTING) {
  return { phase, transport: "connecting", error: "", terminal: "" };
}

export function reduceConnection(state, action = {}) {
  const current = state || initialConnection();
  switch (action.type) {
    case "opened":
      if (SETTLED.has(current.phase)) return current;
      return { ...current, transport: "open" };
    case "error": {
      if (SETTLED.has(current.phase)) return current;
      const offline = action.readyState === EVENTSOURCE_CLOSED;
      return {
        ...current,
        phase: offline ? SSE_PHASE.OFFLINE : SSE_PHASE.RECONNECTING,
        transport: offline ? "closed" : "retrying",
        error: String(action.error || ""),
      };
    }
    case "snapshot":
    case "append": {
      if (SETTLED.has(current.phase)) return current;
      if (current.phase === SSE_PHASE.CONNECTED && !current.error) return current;
      // 只有重新收到权威数据（新连接的首帧是 snapshot）才算恢复同步。
      return { ...current, phase: SSE_PHASE.CONNECTED, transport: "open", error: "" };
    }
    case "done":
      if (current.phase === SSE_PHASE.CLOSED) return current;
      return { ...current, phase: SSE_PHASE.FINISHED, transport: "closed", terminal: "done", error: "" };
    case "failed":
      if (current.phase === SSE_PHASE.CLOSED) return current;
      return {
        ...current,
        phase: SSE_PHASE.FINISHED,
        transport: "closed",
        terminal: "failed",
        error: String(action.error || ""),
      };
    case "close":
      if (SETTLED.has(current.phase)) return current;
      return { ...current, phase: SSE_PHASE.CLOSED, transport: "closed", error: "" };
    default:
      return current;
  }
}

export function connectionPresentation(state) {
  const phase = (state && state.phase) || SSE_PHASE.CONNECTING;
  switch (phase) {
    case SSE_PHASE.CONNECTED:
      return { showPill: true, pill: "已同步", tone: "ok", showBanner: false, banner: "" };
    case SSE_PHASE.RECONNECTING:
      return {
        showPill: true,
        pill: "重连中",
        tone: "warn",
        showBanner: true,
        banner: "连接中断，正在重连；后台任务状态暂未同步",
      };
    case SSE_PHASE.OFFLINE:
      return {
        showPill: true,
        pill: "离线",
        tone: "bad",
        showBanner: true,
        banner: "无法连接运行状态；后台任务可能仍在继续，恢复后会以服务端快照重新同步",
      };
    case SSE_PHASE.FINISHED:
      return { showPill: true, pill: "已结束", tone: "", showBanner: false, banner: "" };
    case SSE_PHASE.CLOSED:
      return { showPill: false, pill: "", tone: "", showBanner: false, banner: "" };
    case SSE_PHASE.CONNECTING:
    default:
      return { showPill: true, pill: "连接中…", tone: "", showBanner: false, banner: "" };
  }
}