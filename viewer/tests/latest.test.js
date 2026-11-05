import test from "node:test";
import assert from "node:assert/strict";

import { createLatestGate, createRunsPoller, settleLatest } from "../core/latest.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test("新选择立即使旧选择失效", () => {
  const gate = createLatestGate();
  const a = gate.begin();
  const b = gate.begin();
  assert.equal(a.isCurrent(), false);
  assert.equal(b.isCurrent(), true);
  gate.invalidate();
  assert.equal(b.isCurrent(), false);
});

test("打开运行 A 再打开运行 B：B 先返回、A 后返回，最终只保留 B", async () => {
  const gate = createLatestGate();
  const applied = [];
  const first = deferred();
  const second = deferred();

  const a = gate.begin();
  const runA = settleLatest(a, () => first.promise).then(outcome => {
    if (outcome.stale) return;
    applied.push(outcome.value);
  });
  const b = gate.begin();
  const runB = settleLatest(b, () => second.promise).then(outcome => {
    if (outcome.stale) return;
    applied.push(outcome.value);
  });

  second.resolve("B");
  await tick();
  first.resolve("A");
  await Promise.all([runA, runB]);
  assert.deepEqual(applied, ["B"]);
});

test("A 在 B 已显示后失败：不把 B 替换成 A 的错误", async () => {
  const gate = createLatestGate();
  const first = deferred();
  const second = deferred();

  const a = gate.begin();
  const runA = settleLatest(a, () => first.promise);
  const b = gate.begin();
  const runB = settleLatest(b, () => second.promise);

  second.resolve("B");
  await tick();
  first.reject(new Error("A failed"));
  const [outcomeA, outcomeB] = await Promise.all([runA, runB]);
  assert.equal(outcomeA.stale, true);
  assert.equal(outcomeA.error.message, "A failed");
  assert.equal(outcomeB.stale, false);
  assert.equal(outcomeB.value, "B");
});

test("轮询串行化：上一次未结束时共享同一请求，不重复发起", async () => {
  let calls = 0;
  const pending = deferred();
  const refresh = createRunsPoller({
    fetchRuns: () => { calls += 1; return pending.promise; },
    apply: () => {},
  });
  const first = refresh();
  const second = refresh();
  assert.equal(calls, 1);
  pending.resolve([]);
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.applied, true);
  assert.equal(b.applied, true, "等待中的调用方共享同一次请求的结果");
  assert.equal(calls, 1);
  assert.equal((await refresh()).applied, true);
  assert.equal(calls, 2, "请求结束后可以发起下一轮");
});

test("强制刷新允许抢跑，旧响应不会回滚新列表", async () => {
  const first = deferred();
  const second = deferred();
  const applied = [];
  let calls = 0;
  const refresh = createRunsPoller({
    fetchRuns: () => (++calls === 1 ? first.promise : second.promise),
    apply: list => applied.push(list),
  });

  const oldRun = refresh();
  const newRun = refresh({ force: true });
  second.resolve(["new"]);
  await tick();
  first.resolve(["old"]);
  const [oldOutcome, newOutcome] = await Promise.all([oldRun, newRun]);
  assert.equal(newOutcome.applied, true);
  assert.equal(oldOutcome.stale, true);
  assert.deepEqual(applied, [["new"]]);
});

test("最新请求失败只报告一次，旧错误被丢弃", async () => {
  const first = deferred();
  const second = deferred();
  const errors = [];
  let calls = 0;
  const refresh = createRunsPoller({
    fetchRuns: () => (++calls === 1 ? first.promise : second.promise),
    apply: () => {},
    onError: error => errors.push(error.message),
  });

  const oldRun = refresh();
  const newRun = refresh({ force: true });
  second.reject(new Error("new failed"));
  await tick();
  first.reject(new Error("old failed"));
  const [oldOutcome, newOutcome] = await Promise.all([oldRun, newRun]);
  assert.equal(oldOutcome.stale, true);
  assert.equal(newOutcome.applied, false);
  assert.deepEqual(errors, ["new failed"]);
});