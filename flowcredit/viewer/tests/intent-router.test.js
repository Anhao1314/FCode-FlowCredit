import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { isDirectConversation, routeIntent, workSubmissionRoute } from "../core/intent-router.js";

const CASES = JSON.parse(
  readFileSync(new URL("../../fixtures/intent-router/cases.json", import.meta.url), "utf8"),
).cases;

/* ---------- 稳定契约 ---------- */

test("routeIntent 返回稳定契约：route/via/reason/certainty", () => {
  const decision = routeIntent("你好");
  assert.deepEqual(Object.keys(decision).sort(),
    ["certainty", "reason", "route", "via"]);
  assert.equal(decision.via, "deterministic");
  assert.equal(["high", "low"].includes(decision.certainty), true);
});

/* ---------- 与 Python 端共用的版本化 fixture ---------- */

test("共享 fixture（41 例）全部按预期分流，Python/JS 不漂移", () => {
  assert.ok(CASES.length >= 36, "fixture must stay large enough to pin the rules");
  const mismatched = [];
  for (const item of CASES) {
    const decision = routeIntent(item.message, item.context || {});
    if (decision.route !== item.expected_route) {
      mismatched.push(`${item.id}: expected ${item.expected_route}, got ${decision.route}`);
    }
  }
  assert.deepEqual(mismatched, []);
});

/* ---------- 上下文与低置信护栏 ---------- */

test("继续类输入：无上下文 clarify，有线程/项目上下文 work", () => {
  assert.equal(routeIntent("继续刚才那个修改").route, "clarify");
  assert.equal(routeIntent("继续刚才那个修改", { recent_work_thread: true }).route, "work");
  assert.equal(routeIntent("继续刚才那个修改", { has_project: true }).route, "work");
});

test("残余低置信桶永远不落 work（歧义不自动变 Run）", () => {
  for (const text of ["今天想看点别的", "随便看看", "嗯"]) {
    const decision = routeIntent(text);
    if (decision.certainty === "low") {
      assert.notEqual(decision.route, "work", text);
    }
  }
});

/* ---------- 旧两分法契约（问候不建 Run；明确任务直接开跑） ---------- */

test("workSubmissionRoute：问候与问句走 chat，明确任务走 work", () => {
  for (const text of ["你好", "什么是闭包？", "解释一下这个函数", "怎么写一个脚本", ""]) {
    assert.equal(workSubmissionRoute(text), "chat", text);
  }
  for (const text of ["帮我修复登录 bug", "运行 npm test", "/账本", "Fix the parser and run tests"]) {
    assert.equal(workSubmissionRoute(text), "work", text);
  }
});

test("isDirectConversation 保留问候变体契约", () => {
  assert.equal(isDirectConversation("你好！"), true);
  assert.equal(isDirectConversation("Hello 👋"), true);
  assert.equal(isDirectConversation("你好，帮我写一个脚本"), false);
});
