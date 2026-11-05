import test from "node:test";
import assert from "node:assert/strict";
import {
  actionCardHtml,
  mirrorFrom,
  runMirrorHtml,
  verdictOf,
} from "../core/run-card.js";

const proposal = {
  id: "a1",
  role: "assistant",
  status: "ok",
  text: "我建议这样查。",
  action: {
    goal: "查周三上海到杭州的高铁",
    mode: "research",
    sources: ["https://example.com/a"],
    reason: "需要实时车次",
  },
};

test("the proposal card shows the goal, mode and sources, escaped", () => {
  const html = actionCardHtml({ ...proposal, action: { ...proposal.action, goal: "<查票>" } });
  assert.ok(html.includes("&lt;查票&gt;"));
  assert.ok(!html.includes("<查票>"));
  assert.ok(html.includes("https://example.com/a"));
  assert.ok(html.includes("模型理由：需要实时车次"));
  assert.ok(html.includes('data-run-mode="research"'));
  assert.ok(html.includes('aria-pressed="true"'));
  assert.ok(html.includes("data-run-start=\"a1\""));
  assert.ok(html.includes("data-run-cancel=\"a1\""));
  assert.ok(html.includes("确认后才会创建运行"));
});

test("a script proposal carries no sources field", () => {
  const html = actionCardHtml({
    ...proposal,
    action: { goal: "写一个脚本整理文件", mode: "script" },
  });
  assert.ok(!html.includes("data-run-sources"));
  assert.ok(html.includes('data-run-mode="script"'));
});

test("the card renders the operator's unsaved edits, not the model's wording", () => {
  const html = actionCardHtml(proposal, {
    draft: "用户改写后的目标",
    mode: "script",
    sources: "https://typed.example",
  });
  assert.ok(html.includes("用户改写后的目标"));
  assert.ok(!html.includes("查周三上海到杭州的高铁"));
  assert.ok(!html.includes("data-run-sources"));
  assert.ok(!html.includes("https://example.com/a"));
});

test("a confirm in flight disables the card and a failure is shown in place", () => {
  const busy = actionCardHtml(proposal, { busy: true });
  assert.ok(busy.includes("正在创建运行…"));
  assert.equal((busy.match(/ disabled/g) || []).length >= 5, true);
  const failed = actionCardHtml(proposal, { error: "a research delegation needs spec.sources" });
  assert.ok(failed.includes("needs spec.sources"));
});

test("the proposal card reports how much conversation travels with it", () => {
  const html = actionCardHtml({ ...proposal, context: { messages: 4 } });
  assert.ok(html.includes("携带本对话 4 条"));
  assert.ok(!actionCardHtml(proposal).includes("携带本对话"));
});

test("verdict turns one observation into one token", () => {
  assert.equal(verdictOf({ tool: "write_file", result: { changed: true } }), "已写入");
  assert.equal(verdictOf({ tool: "write_file", result: { changed: false } }), "无变化");
  assert.equal(verdictOf({ tool: "run_command", result: { exit_code: 0 } }), "退出码 0");
  assert.equal(verdictOf({ tool: "run_command", result: { exit_code: 2 } }), "退出码 2");
  assert.equal(verdictOf({ tool: "read_file", decision: { allowed: false } }), "未获授权");
  assert.equal(verdictOf({ tool: "report_blocked", result: { blocked: true } }), "受阻");
  assert.equal(verdictOf({ tool: "x", result: { failure: { code: "TOOL_FAILED" } } }), "TOOL_FAILED");
  assert.equal(verdictOf({ tool: "ls_workspace", result: {} }), "完成");
});

const snapshot = {
  rounds: [
    { index: 1, observation: { tool: "write_file", result: { changed: true } } },
    { index: 2, observation: { tool: "run_command", result: { exit_code: 0 } } },
    { index: 3, observation: { tool: "edit_file", result: { changed: true } } },
    { index: 4, observation: { tool: "read_file", result: { bytes: 12 } } },
  ],
  terminal: {
    status: "succeeded",
    result: { verified: true, results: [{ path: "script.py", bytes: 318 }] },
  },
};

test("the mirror keeps the last three rounds and the run's own facts", () => {
  const mirror = mirrorFrom(snapshot, { status: "running", cost: { microusd: 1240 } });
  assert.deepEqual(mirror.rows.map(row => row.n), [2, 3, 4]);
  assert.equal(mirror.rounds, 4);
  assert.deepEqual(mirror.artifacts, [{ path: "script.py", bytes: 318 }]);
  assert.equal(mirror.verified, true);
  assert.equal(mirror.status, "running");
});

test("a live status beats the terminal one, and a failure survives", () => {
  const mirror = mirrorFrom(snapshot, { status: "stopped", error: "operator stopped" });
  assert.equal(mirror.status, "stopped");
  assert.equal(mirror.error, "operator stopped");
  const failed = mirrorFrom(
    { rounds: [], terminal: { status: "failed", failure: { code: "BUDGET_EXCEEDED", message: "轮次用尽" } } },
    {},
  );
  assert.equal(failed.failure.code, "BUDGET_EXCEEDED");
});

test("the mirror borrows the viewer's own row description", () => {
  const mirror = mirrorFrom(snapshot, {}, observation => `desc:${observation.tool}`);
  assert.equal(mirror.rows[0].detail, "desc:run_command");
  const plain = mirrorFrom(snapshot);
  assert.equal(plain.rows[0].detail, "");
});

test("the mirror renders progress, the approval gate and the artifacts", () => {
  const mirror = mirrorFrom(snapshot, {
    status: "running",
    cost: { microusd: 1240 },
    pending: { id: "p1", round: 4, tool: "write_file", arguments: { path: "script.py" }, expiresIn: 30 },
  });
  const html = runMirrorHtml({ id: "a1", action: proposal.action }, mirror, { run: { id: "run-1", goal: "查周三高铁" } });
  assert.ok(html.includes("研究运行"));
  assert.ok(html.includes("运行中"));
  assert.ok(html.includes("查周三高铁"));
  assert.ok(html.includes("#4"));
  assert.ok(html.includes("退出码 0"));
  assert.ok(html.includes("script.py"));
  assert.ok(html.includes("链已校验"));
  assert.ok(html.includes("1,240 µUSD"));
  assert.ok(html.includes("需要授权"));
  assert.ok(html.includes("write_file(path=script.py)"));
  assert.ok(html.includes('data-run-allow="p1"'));
  assert.ok(html.includes('data-run-deny="p1"'));
  assert.ok(html.includes('data-run-stop="run-1"'));
  assert.ok(html.includes('data-run-open="run-1"'));
});

test("a finished run drops the stop button and shows the outcome", () => {
  const mirror = mirrorFrom(snapshot, { status: "succeeded", finished: true, cost: { microusd: 980 } });
  const html = runMirrorHtml({ id: "a1", action: { mode: "script" } }, mirror, { run: { id: "run-1" } });
  assert.ok(html.includes("已完成"));
  assert.ok(!html.includes("data-run-stop"));
  assert.ok(html.includes('data-run-open="run-1"'));
  assert.ok(!html.includes("需要授权"));
});

test("a run without rounds yet still renders, and its failure is shown", () => {
  const html = runMirrorHtml(
    { id: "a1", action: { mode: "script" } },
    mirrorFrom({ rounds: [], terminal: { status: "failed", failure: { code: "BUDGET_EXCEEDED", message: "轮次用尽" } } }, { finished: true }),
    { run: { id: "run-1" } },
  );
  // A finished run with no rounds must not claim it is still waiting.
  assert.ok(html.includes("这次运行没有留下可读的轮次"));
  assert.ok(!html.includes("等待 Runtime 的第一个轮次…"));
  assert.ok(html.includes("BUDGET_EXCEEDED"));
  assert.ok(html.includes("轮次用尽"));
  assert.ok(html.includes("失败"));
});
test("a round that has not executed yet is waiting, not done", () => {
  const snapshot = {
    rounds: [
      { index: 1, proposal: { steps: [{ tool: "write_file" }] }, decision: { allowed: true } },
      { index: 2, proposal: { steps: [{ tool: "run_command" }] }, decision: { allowed: false } },
    ],
  };
  const mirror = mirrorFrom(snapshot, {}, () => "should not be called");
  assert.deepEqual(mirror.rows.map(row => [row.tool, row.verdict, row.detail]), [
    ["write_file", "等待", ""],
    ["run_command", "未获授权", ""],
  ]);
});

test("the mirror labels the run with the mode the operator approved", () => {
  const mirror = mirrorFrom({ rounds: [], terminal: { status: "running" } }, { status: "running" });
  const html = runMirrorHtml(
    { id: "a1", action: { goal: "写脚本", mode: "research" } },
    mirror,
    { run: { id: "run-1", goal: "写脚本", mode: "script" } },
  );
  assert.ok(html.includes("脚本运行"));
  assert.ok(!html.includes("研究运行"));
});

test("a submitted decision only disables the gate it was submitted for", () => {
  const base = runMirrorHtml(
    { id: "a1", action: { mode: "script" } },
    {
      rows: [], rounds: 1, artifacts: [], status: "running", finished: false,
      deciding: { allow: true, pendingId: "p1" },
      pending: { id: "p1", round: 2, tool: "write_file", arguments: { path: "a.py" }, expiresIn: 30 },
    },
    { run: { id: "run-1" } },
  );
  assert.ok(base.includes("已允许 · 等待继续…"));
  assert.ok(base.includes("disabled"));
  // 下一个门控（新 id）必须立刻可点：否则一次批准会把后面所有轮次锁死
  const next = runMirrorHtml(
    { id: "a1", action: { mode: "script" } },
    {
      rows: [], rounds: 2, artifacts: [], status: "running", finished: false,
      deciding: { allow: true, pendingId: "p1" },
      pending: { id: "p2", round: 3, tool: "run_command", arguments: { command: "python3" }, expiresIn: 30 },
    },
    { run: { id: "run-1" } },
  );
  assert.ok(!next.includes("已允许 · 等待继续…"));
  assert.ok(!next.includes("disabled"));
  assert.ok(next.includes("30 秒后按默认处理"));
});
