import test from "node:test";
import assert from "node:assert/strict";

import { INTENT_RULES, classifyIntent, isCasualGreeting } from "../core/intent.js";

/* 统一意图表：composer 输入 → 车道。声明式规则、按序求值、首中即停。 */

test("意图表：问候行在前，兜底 task 行殿后，lane 只有 chat/task", () => {
  assert.equal(INTENT_RULES[0].id, "casual-greeting");
  assert.equal(INTENT_RULES[0].lane, "chat");
  assert.equal(INTENT_RULES[INTENT_RULES.length - 1].id, "default-task");
  assert.equal(INTENT_RULES[INTENT_RULES.length - 1].lane, "task");
  assert.ok(INTENT_RULES.every(rule => typeof rule.test === "function"));
  assert.ok(INTENT_RULES.every(rule => ["chat", "task"].includes(rule.lane)));
});

test("classifyIntent:纯问候 → chat，带任务意图/空 → task", () => {
  for (const text of ["你好", "您好！", "hello", "在吗", "谢谢~"]) {
    const intent = classifyIntent(text);
    assert.equal(intent.lane, "chat", text);
    assert.equal(intent.rule, "casual-greeting");
  }
  for (const text of ["你好，帮我审计 fixtures/demo-target", "修一个 bug", "", "   "]) {
    const intent = classifyIntent(text);
    assert.equal(intent.lane, "task", text);
    assert.equal(intent.rule, "default-task");
  }
});

test("isCasualGreeting 从 intent.js 导出（composer-mode re-export 同源）", async () => {
  const composerMode = await import("../core/composer-mode.js");
  assert.equal(composerMode.isCasualGreeting, isCasualGreeting);
});
