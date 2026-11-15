import test from "node:test";
import assert from "node:assert/strict";

import { createRunEvidence } from "../app/run-evidence.js";

const esc = value => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const evidence = createRunEvidence({
  esc,
  pct: value => String(value),
  pp: value => String(value),
  num: value => String(Math.round(Number(value) || 0)),
  shortHash: value => String(value).slice(0, 8),
  doodleSvg: () => "",
  lastBudget: trace => trace.budget || null,
  EVENT_DOT: {},
  HUMAN_EVENTS: new Set(),
  REVIEW_LABEL: {},
  ICON: { lock: "L" },
  FAILURE_REASONS: {},
  evExpanded: () => false,
  getRailFilter: () => "all",
  getRailTab: () => "events",
  getToolContracts: () => [],
  setRailMode: () => {},
});

const classifiedTrace = () => ({
  rounds: [{
    receipt: {
      tokens: 1000,
      metadata: {
        usage_prompt_tokens: 700,
        usage_completion_tokens: 300,
        cached_tokens: 200,
        cache_creation_tokens: 50,
        prompt_prefix_bp: 2500,
      },
    },
    budget_after: { tokens_used: 1000 },
  }],
  budget: {
    calls_used: 1, reads_used: 1, cost_used_microusd: 4, tokens_used: 1025,
    limits: { max_calls: 12, max_reads: 20, max_cost_microusd: 20000, max_tokens: 100000 },
  },
});

test("tokenSplitHtml: input_other/cache_read/cache_creation/output/active 五分类", () => {
  const html = evidence.tokenSplitHtml(classifiedTrace(), { active: 25 });
  for (const label of ["input_other", "cache_read", "cache_creation", "output", "active", "prefix"]) {
    assert.match(html, new RegExp(`>${label}<`), `${label} 应出现在分类行`);
  }
  assert.match(html, />450</, "input_other = prompt - cache_read - cache_creation");
  assert.match(html, />200</);
  assert.match(html, />50</);
  assert.match(html, />300</);
  assert.match(html, />\+25</, "active 是当前轮增量");
  assert.match(html, /25\.0%/);
});

test("tokenSplitHtml: 没有 usage 元数据时如实说明，不伪造分类", () => {
  const html = evidence.tokenSplitHtml({ rounds: [{ receipt: { tokens: 42, metadata: {} } }] });
  assert.match(html, /tok-split unknown/);
  assert.match(html, /当前收据未含 token 分类/);
  assert.doesNotMatch(html, /cache_read/);
});

test("renderMeters: live 模式把当前轮增量传入 token 分类", () => {
  const html = evidence.renderMeters(classifiedTrace(), { live: true });
  assert.match(html, /class="tok-split"/);
  assert.match(html, />\+25</);
  assert.match(html, /m-active/);
});

test("resultBlockKind: 拒绝优先、计划调用与结构化计划结果映射 todo", () => {
  assert.equal(evidence.resultBlockKind({ decision: { allowed: false }, observation: { tool: "write_file" } }), "removed");
  assert.equal(evidence.resultBlockKind({ observation: { tool: "submit_plan" } }), "todo");
  assert.equal(evidence.resultBlockKind({
    observation: { tool: "custom_tool", result: { plan: { todos: [{ id: "a" }] } } },
  }), "todo");
  assert.equal(evidence.resultBlockKind({ observation: { tool: "read_file" } }), "brief");
});

test("renderToolDetail(todo): 计划事项按状态渲染且全文转义", () => {
  const html = evidence.renderToolDetail({
    tool: "plan_extension",
    result: {
      plan: {
        revision: 3,
        todos: [
          { id: "scene", text: "<script>alert(1)</script>", status: "pending", tool: "edit_file" },
          { id: "styles", text: "补齐样式", status: "done" },
        ],
      },
    },
  }, "todo");
  assert.match(html, /block-todo/);
  assert.match(html, /计划变更 · rev 3/);
  assert.match(html, /1\/2 settled/);
  assert.match(html, /todo-row pending/);
  assert.match(html, /todo-row done/);
  assert.match(html, /edit_file/);
  assert.ok(!html.includes("<script>alert(1)</script>"));
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});
