import test from "node:test";
import assert from "node:assert/strict";

import { composerMode } from "../core/composer-mode.js";

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

import { isCasualGreeting } from "../core/composer-mode.js";

test("纯问候分流：短问候→对话，带任务意图→任务管线", () => {
  for (const text of ["你好", "您好！", "hello", "hi there?".slice(0, 2), "在吗", "谢谢~", "test", "你好。"]) {
    assert.equal(isCasualGreeting(text), true, text);
  }
  for (const text of ["你好，帮我审计 fixtures/demo-target", "修一个 bug", "整理项目结构", "你好呀，顺便看看这个文件的内容很长很长很长"]) {
    assert.equal(isCasualGreeting(text), false, text);
  }
  assert.equal(isCasualGreeting(""), false);
});
