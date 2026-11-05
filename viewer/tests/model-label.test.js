import test from "node:test";
import assert from "node:assert/strict";

import { modelLabel, resolveModelLabel, traceModelLabel } from "../core/model-label.js";

const kimiTrace = { model: { provider: "kimi", model: "kimi-k2.6" } };
const mockTrace = { model: { provider: "mock", model: "mock" } };

test("Kimi trace 显示真实模型，不重复 provider", () => {
  assert.equal(traceModelLabel(kimiTrace), "kimi-k2.6");
  assert.equal(modelLabel({ provider: "kimi", model: "kimi-k2.6" }), "kimi-k2.6");
});

test("mock trace 仍显示 mock", () => {
  assert.equal(traceModelLabel(mockTrace), "mock");
});

test("provider 与 model 不重复时保留两者，且缺字段时不编造", () => {
  assert.equal(modelLabel({ provider: "openai", model: "gpt-4o" }), "openai/gpt-4o");
  assert.equal(modelLabel({ provider: "local" }), "local");
  assert.equal(modelLabel({ model: "qwen3-4b" }), "qwen3-4b");
  assert.equal(modelLabel({}), "");
  assert.equal(modelLabel(null), "");
});

test("trace 模型优先于运行信息和新选择", () => {
  assert.equal(resolveModelLabel({
    trace: kimiTrace,
    runModel: "deepseek-chat",
    selectedModel: "fcode-qwen3-4b",
  }), "kimi-k2.6");
});

test("运行结束后改选模型不会回写历史运行展示", () => {
  const finished = { ...kimiTrace, terminal: { status: "succeeded" } };
  const before = resolveModelLabel({ trace: finished, selectedModel: "deepseek-chat" });
  const after = resolveModelLabel({ trace: finished, selectedModel: "fcode-qwen3-4b" });
  assert.equal(before, "kimi-k2.6");
  assert.equal(after, "kimi-k2.6");
});

test("没有 trace 时才退到本次确认的选择；全无信息时返回空字符串", () => {
  assert.equal(resolveModelLabel({ trace: null, runModel: "kimi-k2.6" }), "kimi-k2.6");
  assert.equal(resolveModelLabel({ trace: null, selectedModel: "deepseek-chat" }), "deepseek-chat");
  assert.equal(resolveModelLabel({}), "");
});