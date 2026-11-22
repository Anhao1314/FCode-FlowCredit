import test from "node:test";
import assert from "node:assert/strict";

import {
  botStatusLabel, botStatusClass,
  govBotSummaryHtml, govBotCardHtml, govBotRosterHtml,
  govBotDispatchHtml, govBotRunHtml, govBotChannelHtml,
  govBotRoutineFormHtml, govBotRoutinesHtml,
  govBotSettingsFormHtml, govBotSkillsHtml, govBotDetailHtml, govTriageBlock,
  govAgentAvatarHtml, govHomeCrewHtml,
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
  {
    id: "analyst", name: "分析员", role: "Triage Analyst", initial: "析",
    charter: "读取已完成审计，给出优先级；只提议，不裁决。", status: "idle",
    counts: { runs: 2, findings: 0, chain_failures: 0 },
    current: null,
    last: { run_id: "a0", target: "demo-2026", audit_id: "demo-2026", status: "done" },
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

test("团队摘要:角色数、执行中、待批准、运行数与 Phase C 标注", () => {
  const html = govBotSummaryHtml(bots);
  assert.match(html, /<b>3<\/b> 个常驻角色/);
  assert.match(html, /<b>1<\/b> 执行中/);
  assert.match(html, /<b>6<\/b> 次运行/);
  assert.match(html, /<b>0<\/b> 个排程/);
  assert.match(html, /Phase C/);
  assert.match(html, /确定性管线/);
});

test("Bot 卡片:头像、职责、运行态、最近目标与派活入口", () => {
  const html = govBotCardHtml(bots[0]);
  assert.match(html, /data-bot-id="auditor"/);
  assert.match(html, /class="ai-avatar ai-avatar--thinking is-thinking gov-agent-avatar gov-agent-avatar--auditor"/);
  assert.match(html, /data-ai-state="thinking"/);
  assert.match(html, /aria-label="审计员"/);
  assert.match(html, /data-agent-role="auditor"/);
  assert.match(html, /gov-agent-symbol--auditor/);
  assert.match(html, /gov-agent-status-dot/);
  assert.doesNotMatch(html, /ai-eye/);
  assert.match(html, /REA Auditor/);
  assert.match(html, /执行中/);
  assert.match(html, /demo-target/);
  assert.match(html, /3 次运行 · 11 发现/);
  assert.match(html, /data-gov-bot-pick="auditor"/);
  assert.match(html, /data-gov-bot-open="auditor"/);
  // 空闲 Bot 显示最近一次，不伪造当前任务
  const idle = govBotCardHtml(bots[1]);
  assert.match(idle, /最近/);
  assert.match(idle, /完成/);
  assert.doesNotMatch(idle, /正在执行/);
});

test("子代理角色符号:三种职责、状态与身份转义", () => {
  for (const bot of bots) {
    const html = govAgentAvatarHtml("ok", 32, bot);
    assert.match(html, new RegExp(`data-agent-role="${bot.id}"`));
    assert.match(html, new RegExp(`gov-agent-symbol--${bot.id}`));
    assert.match(html, /data-ai-state="ok"/);
    assert.match(html, /gov-agent-status-dot/);
    assert.doesNotMatch(html, /ai-eye/);
  }
  const escaped = govAgentAvatarHtml("unknown", 32, { id: "unknown", name: '<img>' });
  assert.match(escaped, /data-ai-state="idle"/);
  assert.match(escaped, /data-agent-role="auditor"/);
  assert.equal(escaped.includes('<img>'), false);
  assert.match(escaped, /&lt;img&gt;/);
});

test("治理首页角色编组:FCode 主伙伴与三种子代理都不伪造运行态", () => {
  const html = govHomeCrewHtml();
  const crew = html.slice(html.indexOf('<div class="gov-home-crew"'));
  assert.match(html, /class="gov-home-companion"/);
  assert.match(html, /ai-avatar-mascot/);
  assert.match(html, /class="gov-home-companion-label">FCODE/);
  assert.equal((crew.match(/class="gov-home-agent"/g) || []).length, 3);
  assert.equal((crew.match(/data-ai-state="idle"/g) || []).length, 3);
  for (const role of ["auditor", "verifier", "analyst"]) {
    assert.match(crew, new RegExp(`data-agent-role="${role}"`));
  }
  assert.match(crew, /审计员/);
  assert.match(crew, /复核员/);
  assert.match(crew, /分析员/);
  assert.doesNotMatch(html, /is-thinking|ai-avatar-ring/);
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
  assert.match(auditor, /<option value="analyst">分析员/);
  assert.match(auditor, /目标路径/);
  assert.match(auditor, /id="gov-bot-target-md"/, "可选目标描述文件");
  assert.match(auditor, /id="gov-bot-osv-snapshot"/, "可选 OSV 快照");
  assert.match(auditor, /fixtures\/evox\/target\.md/);
  const verifier = govBotDispatchHtml(bots, "verifier");
  assert.match(verifier, /<option value="verifier" selected>复核员/);
  assert.match(verifier, /审计 id（留空 = 最新一次）/);
  const analyst = govBotDispatchHtml(bots, "analyst");
  assert.match(analyst, /<option value="analyst" selected>分析员/);
  assert.match(analyst, /审计 id（留空 = 最新一次）/);
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
  assert.match(html, /class="ai-avatar ai-avatar--idle gov-agent-avatar gov-agent-avatar--auditor"/);
  assert.match(html, /data-ai-state="idle"/);
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

test("待批准状态:卡片与运行消息都给审批出口，不伪造完成", () => {
  const bot = {
    ...bots[0], status: "pending",
    current: null,
    pending: { run_id: "rp", target: "/repo/demo", status: "pending", trigger: "routine" },
    counts: { ...bots[0].counts, pending: 1 },
  };
  const card = govBotCardHtml(bot);
  assert.match(card, /待批准/);
  assert.match(card, /等待操作员批准/);
  const run = {
    run_id: "rp", bot_id: "auditor", target: "/repo/demo", status: "pending",
    trigger: "routine", started_at: "2026-11-10T09:00:00",
  };
  const message = govBotRunHtml(bot, run);
  assert.match(message, /data-gov-run-approve="rp"/);
  assert.match(message, /data-gov-run-reject="rp"/);
  assert.doesNotMatch(message, /data-gov-skill-run/);
});

test("完成运行有「教一次」；拒绝运行没有完成按钮", () => {
  const done = govBotRunHtml(bots[0], {
    run_id: "rd", bot_id: "auditor", target: "x", status: "done", audit_id: "a",
  });
  assert.match(done, /data-gov-skill-run="rd"/);
  assert.match(done, /教一次/);
  const captured = govBotRunHtml({
    ...bots[0], skills: [{ id: "sk-1", source_run_id: "rd" }],
  }, {
    run_id: "rd", bot_id: "auditor", target: "x", status: "done", audit_id: "a",
  });
  assert.doesNotMatch(captured, /data-gov-skill-run/);
  const rejected = govBotRunHtml(bots[0], {
    run_id: "rr", bot_id: "auditor", target: "x", status: "rejected",
  });
  assert.match(rejected, /操作员拒绝执行/);
  assert.doesNotMatch(rejected, /data-gov-skill-run/);
});

test("Bot 设置表单:每日上限、审批开关与今日用量", () => {
  const bot = { ...bots[0], runs_today: 3, settings: { daily_run_limit: 8, routine_approval: true } };
  const html = govBotSettingsFormHtml(bot);
  assert.match(html, /id="gov-bot-daily-limit" type="number" min="1" max="50" value="8"/);
  assert.match(html, /id="gov-bot-routine-approval" type="checkbox" checked/);
  assert.match(html, /今日已用 3 \/ 8/);
  assert.match(html, /data-bot-id="auditor"/);
});

test("技能列表:空态、来源运行与删除；文本转义", () => {
  assert.match(govBotSkillsHtml({ skills: [] }), /还没有技能/);
  const html = govBotSkillsHtml({ skills: [{
    id: "sk-1", name: "<img>", target: "<script>", source_run_id: "bot-1", version: 1,
  }] });
  assert.equal(html.includes("<img>"), false);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /data-gov-skill-delete="sk-1"/);
  assert.match(html, /data-gov-skill-schedule="sk-1"/);
  assert.match(html, /来源 bot-1/);
});

test("Bot 详情:动画头像、职责、设置、技能、排程与最近运行", () => {
  const bot = {
    ...bots[0], status: "pending",
    settings: { daily_run_limit: 8, routine_approval: true },
    runs_today: 2,
    skills: [{ id: "sk-1", name: "扫描 demo", target: "demo", source_run_id: "r1", version: 1 }],
    routines: [{
      id: "rt-1", bot_id: "auditor", target: "demo", frequency: "每天",
      time: "09:00", enabled: true, next: "每天 09:00",
    }],
    counts: { runs: 3, findings: 5, routines: 1, pending: 1 },
  };
  const html = govBotDetailHtml(bot, [{
    run_id: "rp", bot_id: "auditor", target: "demo", status: "pending", trigger: "routine",
  }]);
  assert.match(html, /class="ai-avatar ai-avatar--pending gov-agent-avatar gov-agent-avatar--auditor"/);
  assert.match(html, /data-ai-state="pending"/);
  assert.match(html, /ai-avatar-ring is-pending/);
  assert.match(html, /职责与权限/);
  assert.match(html, /预算与审批/);
  assert.match(html, /gov-bot-settings-form/);
  assert.match(html, /扫描 demo/);
  assert.match(html, /每天 09:00/);
  assert.match(html, /data-gov-run-approve="rp"/);
  assert.match(html, /data-gov-bot-back/);
});

test("分析员:只读优先级，不显示教一次与 AI 解读", () => {
  const html = govBotRunHtml(bots[2], {
    run_id: "a1", bot_id: "analyst", target: "demo-2026", audit_id: "demo-2026",
    status: "done", findings: 0, evidence: 0, triage_mode: "manual",
    triage: {
      mode: "manual", confidence: "high",
      priorities: [{ finding_id: "F-1", action: "reaudit", why: "先看审批记忆" }],
    },
    started_at: "2026-11-14T09:00:00",
  });
  assert.match(html, /@分析员 分析 demo-2026/);
  assert.match(html, /1 条优先级/);
  assert.match(html, /只提议，不裁决/);
  assert.match(html, /data-gov-open-bundle="demo-2026"/);
  assert.doesNotMatch(html, /data-gov-skill-run/, "分析员不产生技能");
  assert.doesNotMatch(html, /data-gov-explain/, "分析员不再套一层模型解读");
});

test("分析员设置与排程:无 triage/审批开关，不出现在排程 Bot 选项里", () => {
  const settings = govBotSettingsFormHtml(bots[2]);
  assert.doesNotMatch(settings, /gov-bot-triage-mode/);
  assert.doesNotMatch(settings, /gov-bot-routine-approval/);
  assert.match(settings, /分析调用计入上限/);
  const routine = govBotRoutineFormHtml(bots, "auditor");
  assert.doesNotMatch(routine, /value="analyst"/);
  const detail = govBotDetailHtml(bots[2], []);
  assert.doesNotMatch(detail, /<h3>技能<\/h3>/);
  assert.doesNotMatch(detail, /<h3>排程<\/h3>/);
});

test("团队频道按时间正序，并对失败文本转义", () => {
  const html = govBotChannelHtml([
    { run_id: "new", bot_id: "verifier", target: "b", status: "done", audit_id: "b",
      chain_intact: true, started_at: "2026-01-02T09:00:00" },
    { run_id: "old", bot_id: "auditor", target: "<img>", status: "failed",
      error: "<script>x</script>", started_at: "2026-01-01T09:00:00" },
  ], bots);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img>"), false);
  assert.match(html, /&lt;script&gt;/);
  assert.ok(html.indexOf("&lt;img&gt;") < html.indexOf("@复核员"));
  assert.match(govBotChannelHtml([], bots), /团队频道还是空的/);
});

test("团队频道:待批准置顶，其余按时间正序", () => {
  const html = govBotChannelHtml([
    { run_id: "r-new", bot_id: "auditor", target: "/repo/new", status: "done",
      audit_id: "a-new", findings: 1, evidence: 1, started_at: "2026-01-02T09:00:00" },
    { run_id: "r-pending", bot_id: "auditor", target: "/repo/pending", status: "pending",
      started_at: "2026-01-01T09:00:00" },
    { run_id: "r-old", bot_id: "auditor", target: "/repo/old", status: "done",
      audit_id: "a-old", findings: 1, evidence: 1, started_at: "2026-01-01T08:00:00" },
  ], bots);
  assert.ok(html.indexOf('data-gov-run-card="r-pending"') < html.indexOf('data-gov-run-card="r-old"'));
  assert.ok(html.indexOf('data-gov-run-card="r-old"') < html.indexOf('data-gov-run-card="r-new"'));
});

test("对话手动 run:来源标记、操作员口吻与同卡定位属性", () => {
  const html = govBotRunHtml(bots[0], {
    run_id: "gov-abc123", bot_id: "auditor", source: "console", trigger: "manual",
    target: "/Applications/EvoX.app", status: "done", audit_id: "evox-1",
    findings: 5, evidence: 5, started_at: "2026-01-02T09:00:00",
  });
  assert.match(html, /@操作员 挖洞/);
  assert.match(html, /gov-run-source">对话/);
  assert.match(html, /data-gov-run-card="gov-abc123"/);
  assert.match(html, /审计台 →/);
  // 团队手动派活仍显示「团队」，与对话入口区分
  const team = govBotRunHtml(bots[0], {
    run_id: "bot-1", bot_id: "auditor", source: "team", trigger: "manual",
    target: "fixtures/demo-target", status: "done", audit_id: "demo-1",
    findings: 3, evidence: 3, started_at: "2026-01-02T09:00:00",
  });
  assert.match(team, /gov-run-source">团队/);
  assert.doesNotMatch(team, /@操作员/);
});

test("AI 解读：非裁决标签显式、正文转义、done 运行带入口", () => {
  const explanations = {
    new: {
      run_id: "new", model: "glm-5.3-flash", tokens: 1234,
      prompt_sha256: "abc123", text: "发现 fp-1 的依据是<审计包>证据<script>bad</script>。",
    },
  };
  const html = govBotChannelHtml([
    { run_id: "new", bot_id: "verifier", target: "b", status: "done", audit_id: "b", chain_intact: true },
  ], bots, explanations);
  assert.match(html, /AI 解读/);
  assert.match(html, /非裁决/);
  assert.match(html, /不进审计包/);
  assert.match(html, /glm-5\.3-flash/);
  assert.doesNotMatch(html, /<script>bad/);
  assert.match(html, /&lt;审计包&gt;/);
  // done + audit_id 的运行在频道里带解读入口
  assert.match(html, /data-gov-explain="new"/);
  // 没有解读的运行不渲染块
  const bare = govBotChannelHtml([
    { run_id: "old", bot_id: "auditor", target: "b", status: "done", audit_id: "a2" },
  ], bots, explanations);
  assert.doesNotMatch(bare, /gov-explain-block="old"/);
  // done 但没有 audit_id：没有解读入口
  const noBundle = govBotRunHtml(bots[0], {
    run_id: "r9", bot_id: "auditor", target: "t", status: "done", audit_id: "",
  });
  assert.doesNotMatch(noBundle, /data-gov-explain/);
});
test("triage 块:影子/生效/失败三态，active 才显示待批准复扫", () => {
  assert.equal(govTriageBlock({}), "", "没有 triage 字段的旧运行不渲染");

  const pending = govTriageBlock({ triage_status: "pending" });
  assert.match(pending, /正在生成跟进优先级/);

  const failed = govTriageBlock({ triage_status: "error", triage_error: "provider down" });
  assert.match(failed, /provider down/);
  assert.match(failed, /不改变任何证据/);

  const plan = {
    summary: "先复核审批记忆。",
    confidence: "high",
    focus_categories: ["AGI-02"],
    priorities: [{ finding_id: "F-POC-0001", why: "门控根因", action: "reaudit" }],
    model: "fake-triage",
    tokens: 123,
    cost_microusd: 45,
  };
  const shadow = govTriageBlock({ triage_status: "done", triage_mode: "shadow", triage: plan });
  assert.match(shadow, /影子 · 仅记录/);
  assert.match(shadow, /F-POC-0001/);
  assert.match(shadow, /门控根因/);
  assert.doesNotMatch(shadow, /待批准复扫/);

  const active = govTriageBlock({
    triage_status: "done", triage_mode: "active", triage_run_id: "bot-child",
    triage: plan,
  });
  assert.match(active, /提议复扫（待批准）/);
  assert.match(active, /bot-child/);
  assert.match(active, /批准后才执行/);
});

test("Bot 设置:审计员有跟进策略档位，复核员没有", () => {
  const auditor = govBotSettingsFormHtml(bots[0]);
  assert.match(auditor, /id="gov-bot-triage-mode"/);
  assert.match(auditor, /value="shadow"/);
  assert.match(auditor, /value="active"/);
  const verifier = govBotSettingsFormHtml(bots[1]);
  assert.doesNotMatch(verifier, /triage-mode/);
});
