import test from "node:test";
import assert from "node:assert/strict";

import { composerMode, isDirectConversation, workSubmissionRoute } from "../core/composer-mode.js";

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

test("Work 入口让问候变体、普通问答和歧义输入先进入真实 Chat", () => {
  for (const text of [
    "早上好呀", "什么是闭包？", "解释一下这个函数", "今天天气如何", "你会写脚本吗",
    "怎么写一个脚本", "查一个事实", "帮我介绍你自己", "你好世界", "", "   ",
    "How do I fix this parser?", "Explain this function",
  ]) {
    assert.equal(workSubmissionRoute(text), "chat", text);
  }
});

test("只有明确执行或交付意图才进入 Work，包括问候后的混合任务", () => {
  for (const text of [
    "你好，帮我写一个脚本", "谢谢，继续修改 report.md", "运行 npm test", "审计这个目录",
    "修一个 bug", "整理项目结构", "调研并整理成 research.md", "为 FCode 设计动画角色",
    "读取 src/app.js", "Create a landing page", "Fix the parser and run tests",
    "怎么修这个问题？请直接修改", "如何部署这个项目？请直接执行",
  ]) {
    assert.equal(workSubmissionRoute(text), "work", text);
  }
});

test("斜杠命令保持既有 Work 命令路径", () => {
  assert.equal(workSubmissionRoute("/账本"), "work");
  assert.equal(workSubmissionRoute("/report run-1"), "work");
});
