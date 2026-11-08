import test from "node:test";
import assert from "node:assert/strict";

import { composerMode } from "../core/composer-mode.js";

/* ---------- composerMode：S1 分态后 submit dispatch 的唯一真相 ---------- */

test("四个模式各自解析", () => {
  assert.equal(composerMode({ intakeActive: true }), "intake");
  assert.equal(composerMode({ chatActive: true }), "chat");
  assert.equal(composerMode({ sessionRunning: true }), "running");
  assert.equal(composerMode({}), "work");
});

test("优先级 intake > chat > running > work（与既有行为逐条一致）", () => {
  assert.equal(composerMode({ intakeActive: true, chatActive: true, sessionRunning: true }), "intake");
  assert.equal(composerMode({ chatActive: true, sessionRunning: true }), "chat");
  assert.equal(composerMode({ sessionRunning: true }), "running");
});

test("空参与全 false 都回落 work", () => {
  assert.equal(composerMode(), "work");
  assert.equal(composerMode({ intakeActive: false, chatActive: false, sessionRunning: false }), "work");
});
