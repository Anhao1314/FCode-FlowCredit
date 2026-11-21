import test from "node:test";
import assert from "node:assert/strict";

import {
  govActionCardHtml,
  govChatMessageHtml,
  govChatRunCardHtml,
  govChatTeamRailHtml,
  govChatThreadBarHtml,
} from "../core/gov-chat.js";

const bots = [
  { id: "auditor", name: "审计员", role: "REA Auditor", status: "pending",
    counts: { pending: 1 } },
  { id: "verifier", name: "复核员", role: "Chain Verifier", status: "idle" },
];

test("治理线程切换器列出历史并选中当前线程", () => {
  const html = govChatThreadBarHtml([
    { id: "chat-1", title: "审计跟进" },
    { id: "chat-2", title: "待批准" },
  ], "chat-2");
  assert.match(html, /<option value="chat-1">审计跟进<\/option>/);
  assert.match(html, /<option value="chat-2" selected>待批准<\/option>/);
  assert.match(html, /data-gov-chat-thread/);
});

test("用户消息是气泡，助手消息带模型和 Markdown", () => {
  const user = govChatMessageHtml({ id: "u1", role: "user", text: "<b>你好</b>" });
  assert.match(user, /gov-chat-bubble/);
  assert.doesNotMatch(user, /<b>你好<\/b>/);
  const assistant = govChatMessageHtml({
    id: "a1", role: "assistant", text: "**结论**", model: "deepseek-flash",
    status: "ok", usage: { total_tokens: 12 },
  });
  assert.match(assistant, /治理助手/);
  assert.match(assistant, /deepseek-flash/);
  assert.match(assistant, /<strong>结论<\/strong>/);
  assert.match(assistant, /12 tokens/);
});

test("gov_action 渲染成任务提案卡而不是直接执行", () => {
  const message = {
    id: "a2", role: "assistant", status: "ok", text: "我来安排。",
    action: {
      kind: "gov", bot_id: "auditor", action: "audit",
      target: "fixtures/demo-target", reason: "验证最近改动",
    },
  };
  const html = govChatMessageHtml(message);
  assert.match(html, /任务提案/);
  assert.match(html, /审计员 · 扫描/);
  assert.match(html, /fixtures\/demo-target/);
  assert.match(html, /data-gov-proposal-confirm="a2"/);
  assert.match(html, /data-gov-proposal-dismiss="a2"/);
  assert.doesNotMatch(html, /data-gov-run-card/);
  const dismissed = govActionCardHtml(message, { dismissed: true });
  assert.match(dismissed, /已忽略/);
  assert.match(dismissed, /data-gov-proposal-reconsider="a2"/);
});

test("已确认提案显示对话内运行卡：pending 可审批", () => {
  const message = {
    id: "a3", role: "assistant", status: "ok", text: "",
    action: { kind: "gov", bot_id: "auditor", action: "audit", target: "demo" },
    run: { id: "bot-1" },
  };
  const html = govChatMessageHtml(message, {
    bots,
    runsById: { "bot-1": {
      run_id: "bot-1", bot_id: "auditor", target: "fixtures/demo-target",
      status: "pending", findings: 0, evidence: 0,
    } },
  });
  assert.match(html, /gov-chat-run/);
  assert.match(html, /待批准/);
  assert.match(html, /data-gov-run-approve="bot-1"/);
  assert.match(html, /data-gov-run-reject="bot-1"/);
  assert.doesNotMatch(html, /data-gov-proposal-confirm/);
});

test("完成运行给工作台/审计库出口", () => {
  const html = govChatRunCardHtml({
    run_id: "bot-2", bot_id: "auditor", target: "demo", status: "done",
    findings: 3, evidence: 3, audit_id: "demo-2026",
  });
  assert.match(html, /3 发现/);
  assert.match(html, /demo-2026/);
  assert.match(html, /data-gov-hist="demo-2026"/);
  assert.match(html, /data-gov-open-bundle="demo-2026"/);
});

test("团队侧栏保留三个角色与待批准数量", () => {
  const html = govChatTeamRailHtml(bots, [
    { run_id: "p1", bot_id: "auditor", status: "pending", target: "demo" },
  ]);
  assert.doesNotMatch(html, />TEAM</);
  assert.match(html, /审计员/);
  assert.match(html, /复核员/);
  assert.match(html, /待批准/);
  assert.match(html, /data-gov-run-approve="p1"/);
});

test("解读消息渲染成结构化卡片：徽章 + 目标 chip + 边界标注，正文不加前缀", () => {
  const message = {
    id: "a-1", role: "assistant",
    text: "**一句话**：命中 OSV 已知漏洞依赖。\n\n**建议**：升级 Wails ≥ 2.13.0。",
    model: "deepseek-flash",
    usage: { total_tokens: 1522, cost_microusd: 473 },
    explain: { finding_id: "F-POC-0002", audit_id: "evox.app-2026-11-18-739ba7" },
  };
  const html = govChatMessageHtml(message);
  assert.match(html, /data-gov-explain-card="F-POC-0002"/);
  assert.ok(html.includes("gov-explain-badge"), "解读徽章");
  assert.ok(html.includes("非裁决 · 不进审计包"), "边界标注");
  assert.ok(html.includes("evox.app-2026-11-18-739ba7"), "包上下文");
  assert.ok(html.includes("<strong>一句话</strong>"), "加粗小标题");
  assert.ok(html.includes("1,522 tokens"), "诚实 tokens，不声称已签收据");
  assert.ok(!html.includes("已签收据"));
  assert.ok(!html.includes("gov-chat-msg-head"), "卡片不再重复助手头");
});

test("无 explain 元数据的消息保持普通助手气泡", () => {
  const html = govChatMessageHtml({
    id: "a-2", role: "assistant", text: "普通回复", status: "ok",
    model: "m", usage: { total_tokens: 5 },
  });
  assert.ok(html.includes("gov-chat-msg-head"));
  assert.ok(!html.includes("gov-chat-explain"));
});

test("探员候选卡：徽章与边界按 kind 渲染，不冒充 AI 解读", () => {
  const html = govChatMessageHtml({
    id: "a-9", role: "assistant",
    text: "**结论**：探员产出 8 条候选（shadow 落链，未晋升、未进 bundle）。",
    model: "deepseek-flash", usage: { total_tokens: 23055, cost_microusd: 7287 },
    explain: { finding_id: "", audit_id: "evox.app-2026-11-18-4c7593",
               kind: "hunt", candidate_count: 8 },
  });
  assert.ok(html.includes("探员候选"), "徽章如实标注");
  assert.ok(!html.includes("AI 解读"), "不冒充解读卡");
  assert.ok(html.includes("8 条候选"), "候选数展示");
  assert.ok(html.includes("shadow · 未晋升 · 不进审计包"), "shadow 边界标注");
  assert.ok(html.includes("23,055 tokens"));
});
