import test from "node:test";
import assert from "node:assert/strict";

import {
  SSE_PHASE, connectionPresentation, initialConnection, reduceConnection,
} from "../core/sse-status.js";

test("初始状态是正在连接", () => {
  const state = initialConnection();
  assert.equal(state.phase, SSE_PHASE.CONNECTING);
  assert.equal(connectionPresentation(state).pill, "连接中…");
});

test("连接后收到 snapshot 才算同步正常", () => {
  let state = initialConnection();
  state = reduceConnection(state, { type: "opened" });
  assert.equal(state.phase, SSE_PHASE.CONNECTING);
  state = reduceConnection(state, { type: "snapshot" });
  assert.equal(state.phase, SSE_PHASE.CONNECTED);
  assert.equal(connectionPresentation(state).pill, "已同步");
});

test("已连接后 error：显示重连，但保留 running（不产生 failed）", () => {
  let state = reduceConnection(initialConnection(), { type: "snapshot" });
  state = reduceConnection(state, { type: "error", readyState: 0, error: "network" });
  assert.equal(state.phase, SSE_PHASE.RECONNECTING);
  assert.equal(state.terminal, "");
  const view = connectionPresentation(state);
  assert.equal(view.showBanner, true);
  assert.match(view.banner, /连接中断，正在重连/);
  assert.doesNotMatch(view.banner, /失败/);
});

test("error 后收到 snapshot：恢复同步并清除断线提示", () => {
  let state = reduceConnection(initialConnection(), { type: "error", readyState: 0 });
  state = reduceConnection(state, { type: "snapshot" });
  assert.equal(state.phase, SSE_PHASE.CONNECTED);
  assert.equal(connectionPresentation(state).showBanner, false);
});

test("EventSource 放弃重连（readyState=2）显示离线", () => {
  let state = reduceConnection(initialConnection(), { type: "error", readyState: 2 });
  assert.equal(state.phase, SSE_PHASE.OFFLINE);
  assert.equal(connectionPresentation(state).pill, "离线");
  state = reduceConnection(state, { type: "snapshot" });
  assert.equal(state.phase, SSE_PHASE.CONNECTED, "服务恢复后仍能以 snapshot 重新对齐");
});

test("done 之后是真实终态；后续 error 不再显示重连", () => {
  let state = reduceConnection(initialConnection(), { type: "snapshot" });
  state = reduceConnection(state, { type: "done" });
  assert.equal(state.phase, SSE_PHASE.FINISHED);
  assert.equal(state.terminal, "done");
  state = reduceConnection(state, { type: "error", readyState: 0 });
  assert.equal(state.phase, SSE_PHASE.FINISHED);
  assert.equal(connectionPresentation(state).showBanner, false);
  assert.equal(connectionPresentation(state).pill, "已结束");
});

test("手动 close：不显示错误，也不后台重连", () => {
  let state = reduceConnection(initialConnection(), { type: "snapshot" });
  state = reduceConnection(state, { type: "close" });
  assert.equal(state.phase, SSE_PHASE.CLOSED);
  assert.equal(connectionPresentation(state).showPill, false);
  state = reduceConnection(state, { type: "error", readyState: 0 });
  assert.equal(state.phase, SSE_PHASE.CLOSED);
});

test("服务端 failed 是真实终态，与连接 error 区分", () => {
  let state = reduceConnection(initialConnection(), { type: "error", readyState: 0 });
  state = reduceConnection(state, { type: "failed", error: "runtime crashed" });
  assert.equal(state.phase, SSE_PHASE.FINISHED);
  assert.equal(state.terminal, "failed");
  assert.equal(state.error, "runtime crashed");
});