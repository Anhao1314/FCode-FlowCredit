import test from "node:test";
import assert from "node:assert/strict";

import { runModelFields } from "../core/model-choice.js";

const catalog = [
  { key: "mock", supportsEffort: false },
  { key: "kimi", supportsEffort: true },
  { key: "deepseek", supportsEffort: false },
];

test("mock 不发 model/effort", () => {
  assert.deepEqual(runModelFields({ key: "mock", effort: "high" }, catalog), {});
  assert.deepEqual(runModelFields({}, catalog), {});
});

test("支持思考强度的模型带上档位(缺省 medium)", () => {
  assert.deepEqual(runModelFields({ key: "kimi", effort: "high" }, catalog),
    { model: "kimi", effort: "high" });
  assert.deepEqual(runModelFields({ key: "kimi" }, catalog),
    { model: "kimi", effort: "medium" });
});

test("不支持 effort 的模型不发 effort——避免服务端 400", () => {
  // 回归：本地档位来自上一次选择的 kimi，切换到 deepseek 后不能沿用
  assert.deepEqual(runModelFields({ key: "deepseek", effort: "high" }, catalog),
    { model: "deepseek" });
});

test("目录未加载时不发 effort", () => {
  assert.deepEqual(runModelFields({ key: "deepseek", effort: "high" }, []),
    { model: "deepseek" });
  assert.deepEqual(runModelFields({ key: "kimi", effort: "high" }, null),
    { model: "kimi" });
});