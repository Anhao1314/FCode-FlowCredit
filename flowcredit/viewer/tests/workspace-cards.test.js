import test from "node:test";
import assert from "node:assert/strict";

import { ledgerCardHtml, statusCardHtml, scheduledCardHtml } from "../core/workspace-cards.js";
import { chatMessageHtml } from "../core/chat-view.js";

const runs = [
  { id: "r1", objective: "修复定价报告的 bug", status: "succeeded", rounds: 7, cost: { costMicrousd: 45407 }, source: "manual", created: "2026-11-08T09:00:00Z" },
  { id: "r2", name: "每天早上核对日报", status: "running", rounds: 2, source: "scheduled", created: "2026-11-08T08:00:00Z" },
];

test("账本卡:总数与累计成本 + 最近运行行,来源转中文", () => {
  const html = ledgerCardHtml(runs, {});
  assert.match(html, /2 次运行 · 累计 45,407 µUSD/);
  assert.match(html, /修复定价报告的 bug/);
  assert.match(html, /7 轮 · 45,407 µ · 手动/);
  assert.match(html, /data-open="ledger"/);
  assert.match(html, /完整账本/);
});

test("账本卡:created 倒序取最近 5 条,空态引导对话", () => {
  const many = Array.from({ length: 8 }, (_, i) => ({
    id: "r" + i, objective: "任务" + i, created: `2026-11-0${8 - Math.floor(i / 2)}T0${i % 9}:00:00Z`,
  }));
  const html = ledgerCardHtml(many, {});
  assert.equal((html.match(/class="wcard-row"/g) || []).length, 5);
  const empty = ledgerCardHtml([], {});
  assert.match(empty, /还没有委托运行/);
  assert.doesNotMatch(empty, /wcard-meta">[^<]*\S/);
});

test("状态卡:只列 running,空态不装样子", () => {
  const html = statusCardHtml(runs);
  assert.match(html, /进行中的运行/);
  assert.match(html, /每天早上核对日报/);
  assert.equal((html.match(/class="wcard-row"/g) || []).length, 1);
  assert.match(statusCardHtml(runs.filter(r => r.status !== "running")), /当前没有进行中的运行/);
});

test("定时卡:排程行带下次触发,暂停标记,空态跳页面", () => {
  const html = scheduledCardHtml([
    { objective: "核对日报", next: "明天 09:00", enabled: true },
    { objective: "周报汇总", frequency: "每周", time: "周一 09:00", enabled: false },
  ]);
  assert.match(html, /核对日报/);
  assert.match(html, /明天 09:00/);
  assert.match(html, /周报汇总/);
  assert.match(html, /已暂停/);
  assert.match(html, /data-open="scheduled"/);
  assert.match(scheduledCardHtml([]), /还没有排程/);
});

test("卡片对用户可见文本转义(目标来自模型/用户输入)", () => {
  const html = ledgerCardHtml([{ id: "r", objective: '<img src=x>', status: "succeeded", created: "2026-11-08T00:00:00Z" }], {});
  assert.equal(html.includes("<img"), false);
  assert.match(html, /&lt;img/);
});

test("chatMessageHtml:wcard 分支渲染 cardHtml,不落入 user/assistant 分支", () => {
  const html = chatMessageHtml({ id: "w1", role: "wcard", cardHtml: '<div class="wcard">卡</div>' });
  assert.match(html, /class="chat-msg wcard-msg"/);
  assert.match(html, /class="wcard">卡</);
  assert.doesNotMatch(html, /chat-bubble|chat-act/);
});
