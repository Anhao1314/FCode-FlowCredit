import test from "node:test";
import assert from "node:assert/strict";

import {
  attentionText,
  boardColumnOf,
  boardHtml,
  filterRuns,
  projectStats,
  relativeTime,
  statusText,
  workHomeHtml,
} from "../core/work-home.js";

const PROJECT = {
  id: "prj-a",
  name: "FlowCredit",
  path: "/tmp/flowcredit",
  verification: "pytest -q",
};

const RUNS = [
  { id: "r-run", status: "running", attention: "", projectId: "prj-a",
    objective: "正在修 pricing", rounds: 2, created: new Date().toISOString() },
  { id: "r-fail", status: "failed", attention: "failed", projectId: "prj-a",
    objective: "失败的迁移", rounds: 5, created: new Date().toISOString() },
  { id: "r-ok", status: "succeeded", attention: "", projectId: "",
    objective: "完成的研究", rounds: 3, created: new Date().toISOString(),
    cost: { costMicrousd: 12500 } },
];

test("statusText/attentionText:状态用文字,不用 emoji", () => {
  assert.equal(statusText("running"), "进行中");
  assert.equal(statusText("recovery_required"), "待恢复");
  assert.equal(attentionText({ attention: "approval" }), "等待审批");
  assert.equal(attentionText({ attention: "" }), "");
});

test("relativeTime:分钟/小时/日期", () => {
  const now = Date.parse("2026-11-11T12:00:00Z");
  assert.equal(relativeTime("2026-11-11T11:59:30Z", now), "刚刚");
  assert.equal(relativeTime("2026-11-11T11:30:00Z", now), "30 分钟前");
  assert.equal(relativeTime("2026-11-11T09:00:00Z", now), "3 小时前");
  assert.equal(relativeTime("2026-11-11T00:00:00Z", now), "12 小时前");
  assert.equal(relativeTime("", now), "");
});

test("boardColumnOf:running/attention/recent 三分", () => {
  assert.equal(boardColumnOf({ status: "running" }), "running");
  assert.equal(boardColumnOf({ status: "failed", attention: "failed" }), "attention");
  assert.equal(boardColumnOf({ status: "succeeded", attention: "" }), "recent");
});

test("filterRuns:全部/项目/未归属", () => {
  assert.equal(filterRuns(RUNS, "").length, 3);
  assert.deepEqual(filterRuns(RUNS, "prj-a").map(run => run.id), ["r-run", "r-fail"]);
  assert.deepEqual(filterRuns(RUNS, "__none__").map(run => run.id), ["r-ok"]);
});

test("projectStats:运行中与待处理计数", () => {
  assert.deepEqual(projectStats(PROJECT, RUNS), {
    runs: 2, running: 1, attention: 1, succeeded: 0,
  });
});

test("workHomeHtml:项目卡 + 三列看板 + 数据属性", () => {
  const html = workHomeHtml({
    projects: [PROJECT], runs: RUNS, selectedProjectId: "prj-a",
  });
  assert.match(html, /class="work-home"/);
  assert.match(html, /FlowCredit/);
  assert.match(html, /\/tmp\/flowcredit/);
  assert.match(html, /data-project-open="prj-a"/);
  assert.match(html, /data-project-run="prj-a"/);
  assert.match(html, /data-board-col="running"/);
  assert.match(html, /data-board-col="attention"/);
  assert.match(html, /data-board-col="recent"/);
  assert.match(html, /data-work-run="r-run"/);
  assert.match(html, /data-work-run="r-fail"/);
  // 选中项目后看板只显示该项目；未归属的完成运行不出现
  assert.doesNotMatch(html, /data-work-run="r-ok"/);
});

test("workHomeHtml:空态与旧本地项目提示", () => {
  const empty = workHomeHtml({ projects: [], runs: [] });
  assert.match(empty, /还没有项目/);
  assert.match(empty, /data-work-new-project/);
  const legacy = workHomeHtml({ projects: [], runs: [], legacyCount: 2 });
  assert.match(legacy, /旧的本地项目标签/);
  assert.match(legacy, /data-work-legacy-dismiss/);
});

test("workHomeHtml:所有动态文本转义", () => {
  const html = workHomeHtml({
    projects: [{
      id: "prj-x", name: '<img src=x onerror=alert(1)>',
      path: '/tmp/"><script>alert(2)</script>', verification: "",
    }],
    runs: [{
      id: "r-x", status: "failed", attention: "failed", projectId: "prj-x",
      objective: "<script>alert(3)</script>", created: "2026-11-11T00:00:00Z",
    }],
  });
  assert.doesNotMatch(html, /<script|<img/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;script&gt;/);
});

test("boardHtml:按列渲染并保留运行链接", () => {
  const html = boardHtml({ projects: [PROJECT], runs: RUNS, selectedProjectId: "" });
  assert.match(html, /进行中/);
  assert.match(html, /待处理/);
  assert.match(html, /最近完成/);
  assert.match(html, /data-work-run="r-ok"/);
  assert.match(html, /\$0\.013/);
});
