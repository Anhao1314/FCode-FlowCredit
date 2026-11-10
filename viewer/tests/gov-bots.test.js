import test from "node:test";
import assert from "node:assert/strict";

import {
  botStatusLabel, botStatusClass,
  govBotSummaryHtml, govBotCardHtml, govBotRosterHtml,
  govBotDispatchHtml, govBotRunHtml, govBotChannelHtml,
  govBotRoutineFormHtml, govBotRoutinesHtml,
} from "../core/gov-bots.js";

const bots = [
  {
    id: "auditor", name: "审计员", role: "REA Auditor", initial: "审",
    charter: "对目标执行离线确定性审计。", status: "running",
    counts: { runs: 3, findings: 11, chain_failures: 0 },
    current: { run_id: "r1", target: "/repo/fixtures/demo-target", status: "running" },
    last: { run_id: "r1", target: "/repo/fixtures/demo-target", status: "running" },
  },
  {
    id: "verifier", name: "复核员", role: "Chain Verifier", initial: "核",
    charter: "验证签名链与回归对比。", status: "idle",
    counts: { runs: 1, findings: 2, chain_failures: 0 },
    current: null,
    last: { run_id: "r0", target: "demo-target-2026", status: "done" },
  },
];

test("Bot 状态文案与语气", () => {
  assert.equal(botStatusLabel("running"), "执行中");
  assert.equal(botStatusLabel("done"), "完成");
  assert.equal(botStatusLabel("queued"), "排队");
  assert.equal(botStatusClass("running"), "live");
  assert.equal(botStatusClass("done"), "ok");
  assert.equal(botStatusClass("failed"), "bad");
  assert.equal(botStatusClass("idle"), "idle");
});

test("团队摘要:角色数、执行中、运行数与 Phase A 标注", () => {
  const html = govBotSummaryHtml(bots);
  assert.match(html, /<b>2<\/b> 个常驻角色/);
  assert.match(html, /<b>1<\/b> 执行中/);
  assert.match(html, /<b>4<\/b> 次运行/);
  assert.match(html, /<b>0<\/b> 个排程/);
  assert.match(html, /Phase B/);
  assert.match(html, /确定性管线/);
});

test("Bot 卡片:头像、职责、运行态、最近目标与派活入口", () => {
  const html = govBotCardHtml(bots[0]);
  assert.match(html, /data-bot-id="auditor"/);
  assert.match(html, /class="bot-avatar bot-avatar-auditor is-running"/);
  assert.match(html, /aria-label="审计员 · 执行中"/);
  assert.match(html, /REA Auditor/);
  assert.match(html, /执行中/);
  assert.match(html, /demo-target/);
  assert.match(html, /3 次运行 · 11 发现/);
  assert.match(html, /data-gov-bot-pick="auditor"/);
  // 空闲 Bot 显示最近一次，不伪造当前任务
  const idle = govBotCardHtml(bots[1]);
  assert.match(idle, /最近/);
  assert.match(idle, /完成/);
  assert.doesNotMatch(idle, /正在执行/);
});

test("花名册空态不伪造 Bot", () => {
  const html = govBotRosterHtml([]);
  assert.match(html, /团队还没有编制/);
  assert.doesNotMatch(html, /data-bot-id/);
});

test("派活表单:角色选项、审计员默认目标提示、复核员可留空", () => {
  const auditor = govBotDispatchHtml(bots, "auditor");
  assert.match(auditor, /<option value="auditor" selected>审计员/);
  assert.match(auditor, /<option value="verifier">复核员/);
  assert.match(auditor, /目标路径/);
  const verifier = govBotDispatchHtml(bots, "verifier");
  assert.match(verifier, /<option value="verifier" selected>复核员/);
  assert.match(verifier, /审计 id（留空 = 最新一次）/);
});

test("运行消息:审计员完成态带发现数、工作台与审计台出口", () => {
  const run = {
    run_id: "r2", bot_id: "auditor", target: "/repo/fixtures/demo-target",
    status: "done", audit_id: "demo-2026", findings: 2, evidence: 2,
    started_at: "2026-11-10T09:30:00", finished_at: "2026-11-10T09:30:03",
  };
  const html = govBotRunHtml(bots[0], run);
  assert.match(html, /@审计员 扫描 demo-target/);
  assert.match(html, /2 发现/);
  assert.match(html, /2 证据记录/);
  assert.match(html, /data-gov-hist="demo-2026"/);
  assert.match(html, /data-gov-open-bundle="demo-2026"/);
  assert.match(html, /09:30/);
});

test("运行消息:复核员完成态显示链完整与回归桶", () => {
  const run = {
    run_id: "r3", bot_id: "verifier", target: "demo-2026", status: "done",
    audit_id: "demo-2026", chain_intact: true, events: 12,
    regression: { new: 1, fixed: 2, regressed: 0 },
  };
  const html = govBotRunHtml(bots[1], run);
  assert.match(html, /@复核员 验证 demo-2026/);
  assert.match(html, /链完整/);
  assert.match(html, /12 事件/);
  assert.match(html, /new 1 \/ fixed 2 \/ regressed 0/);
});

test("运行消息:失败与运行中都不伪造完成", () => {
  const failed = govBotRunHtml(bots[0], {
    run_id: "r4", bot_id: "auditor", target: "x", status: "failed", error: "target not found",
  });
  assert.match(failed, /target not found/);
  assert.doesNotMatch(failed, /完成/);
  const running = govBotRunHtml(bots[0], {
    run_id: "r5", bot_id: "auditor", target: "x", status: "running",
  });
  assert.match(running, /正在执行/);
  assert.doesNotMatch(running, /data-gov-hist/);
});

test("排程表单:Bot/目标/频率/时间四字段，默认当前 Bot", () => {
  const html = govBotRoutineFormHtml(bots, "verifier");
  assert.match(html, /id="gov-routine-form"/);
  assert.match(html, /<option value="verifier" selected>复核员/);
  assert.match(html, /id="gov-routine-target"/);
  assert.match(html, /<option value="每天">每天<\/option>/);
  assert.match(html, /id="gov-routine-time" type="time" value="09:00"/);
  assert.match(html, /添加排程/);
});

test("排程列表:身份头像、下次触发、启停与删除", () => {
  const withRoutine = [{
    ...bots[0],
    routines: [{
      id: "rt-1", bot_id: "auditor", target: "fixtures/demo-target",
      frequency: "每天", time: "09:00", enabled: true,
      next: "每天 09:00", last_fired_at: "2026-11-10T09:00:00",
      last_status: "running",
    }],
  }];
  const html = govBotRoutinesHtml(withRoutine);
  assert.match(html, /data-routine-id="rt-1"/);
  assert.match(html, /class="bot-avatar bot-avatar-auditor is-idle"/);
  assert.match(html, /fixtures\/demo-target/);
  assert.match(html, /每天 09:00/);
  assert.match(html, /data-gov-routine-toggle="rt-1" checked/);
  assert.match(html, /data-gov-routine-delete="rt-1"/);
  assert.doesNotMatch(html, /running/);
});

test("排程列表空态与转义", () => {
  assert.match(govBotRoutinesHtml([]), /还没有定时排程/);
  const html = govBotRoutinesHtml([{
    ...bots[0],
    routines: [{
      id: "rt-2", bot_id: "auditor", target: "<img>", frequency: "每天",
      enabled: false, next: "<script>", last_status: "error: <bad>",
    }],
  }]);
  assert.equal(html.includes("<img>"), false);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;img&gt;/);
});

test("团队频道按时间正序，并对失败文本转义", () => {
  const html = govBotChannelHtml([
    { run_id: "new", bot_id: "verifier", target: "b", status: "done", audit_id: "b", chain_intact: true },
    { run_id: "old", bot_id: "auditor", target: "<img>", status: "failed", error: "<script>x</script>" },
  ], bots);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img>"), false);
  assert.match(html, /&lt;script&gt;/);
  assert.ok(html.indexOf("&lt;img&gt;") < html.indexOf("@复核员"));
  assert.match(govBotChannelHtml([], bots), /团队频道还是空的/);
});