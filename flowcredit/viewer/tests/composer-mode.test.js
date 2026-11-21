import test from "node:test";
import assert from "node:assert/strict";

import { composerMode, isDirectConversation } from "../core/composer-mode.js";

/* ---------- composerMode：S1 分态后 submit dispatch 的唯一真相 ---------- */

test("五个模式各自解析", () => {
  assert.equal(composerMode({ intakeActive: true }), "intake");
  assert.equal(composerMode({ govActive: true }), "gov");
  assert.equal(composerMode({ chatActive: true }), "chat");
  assert.equal(composerMode({ sessionRunning: true }), "running");
  assert.equal(composerMode({}), "work");
});

test("优先级 intake > gov > chat > running > work", () => {
  assert.equal(composerMode({ intakeActive: true, govActive: true, chatActive: true, sessionRunning: true }), "intake");
  assert.equal(composerMode({ govActive: true, chatActive: true, sessionRunning: true }), "gov");
  assert.equal(composerMode({ chatActive: true, sessionRunning: true }), "chat");
  assert.equal(composerMode({ sessionRunning: true }), "running");
});

test("空参与全 false 都回落 work", () => {
  assert.equal(composerMode(), "work");
  assert.equal(composerMode({ intakeActive: false, chatActive: false, sessionRunning: false }), "work");
});

test("明确的纯对话短输入直接回答，包括标点与表情变体", () => {
  for (const text of ["你好", " 你好！ ", "Hello 👋", "谢谢", "你是谁？", "What can you do?"]) {
    assert.equal(isDirectConversation(text), true, text);
  }
});

test("包含任务目标或附加要求的输入不被问候前缀截走", () => {
  for (const text of [
    "你好，帮我写一个脚本", "谢谢，继续修改 report.md", "你好世界", "帮我介绍你自己",
    "你能做什么就做什么，并写入 result.md", "", "   ", "运行 npm test",
  ]) {
    assert.equal(isDirectConversation(text), false, text);
  }
});
