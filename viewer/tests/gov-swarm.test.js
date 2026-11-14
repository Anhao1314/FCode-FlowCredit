import test from "node:test";
import assert from "node:assert/strict";

import {
  govSwarmFormHtml, govSwarmCardHtml, govSwarmListHtml,
} from "../core/gov-swarm.js";

const swarm = {
  id: "sw-1", name: "蜂群 · 2 个目标", status: "running", max_concurrency: 2,
  counts: { total: 3, queued: 1, running: 1, done: 1, failed: 0, rejected: 0, finished: 1 },
  runs: [
    { run_id: "r1", target: "/Applications/EvoX.app", status: "done",
      findings: 5, evidence: 5, audit_id: "evox-1" },
    { run_id: "r2", target: "fixtures/demo-target", status: "running" },
    { run_id: "r3", target: "fixtures/evox", status: "queued" },
  ],
};

test("蜂群表单:多行目标、并发上限与取消语义提示", () => {
  const html = govSwarmFormHtml();
  assert.match(html, /id="gov-swarm-targets"/);
  assert.match(html, /id="gov-swarm-concurrency"/);
  assert.match(html, /<option value="2" selected>2<\/option>/);
  assert.match(html, /id="gov-swarm-target-md"/);
  assert.match(html, /id="gov-swarm-osv-snapshot"/);
  assert.match(html, /每行一个/);
  assert.match(html, /取消只停止排队/);
});

test("蜂群卡:进度、并发状态、目标运行图与工作台出口", () => {
  const html = govSwarmCardHtml(swarm);
  assert.match(html, /data-swarm-id="sw-1"/);
  assert.match(html, /1 \/ 3 完成/);
  assert.match(html, /1 执行中/);
  assert.match(html, /1 排队/);
  assert.match(html, /aria-valuenow="1"/);
  assert.match(html, /EvoX\.app/);
  assert.match(html, /5 发现/);
  assert.match(html, /data-gov-hist="evox-1"/);
  assert.match(html, /data-gov-swarm-cancel="sw-1"/);
  // 没有失败/取消目标时不给重试入口
  assert.doesNotMatch(html, /data-gov-swarm-retry/);
});

test("蜂群卡:失败目标可重试，取消后不再显示取消按钮", () => {
  const failed = {
    ...swarm, status: "cancelled",
    counts: { total: 2, queued: 0, running: 0, done: 1, failed: 1, rejected: 0, finished: 2 },
    runs: [
      { run_id: "r1", target: "a", status: "done", findings: 1, evidence: 1, audit_id: "a-1" },
      { run_id: "r2", target: "b", status: "failed", error: "target not found" },
    ],
  };
  const html = govSwarmCardHtml(failed);
  assert.match(html, /data-gov-swarm-retry="sw-1"/);
  assert.doesNotMatch(html, /data-gov-swarm-cancel/);
  assert.match(html, /target not found/);
  assert.match(html, /已取消/);
});

test("蜂群卡:所有动态文本转义，空列表有引导", () => {
  const html = govSwarmCardHtml({
    ...swarm, name: "<img src=x>", status: "running",
    runs: [{ run_id: "r1", target: "<script>x</script>", status: "failed", error: "<b>bad</b>" }],
  });
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img"), false);
  assert.match(html, /script&gt;/);
  assert.match(govSwarmListHtml([]), /还没有蜂群/);
});
