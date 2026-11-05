import test from "node:test";
import assert from "node:assert/strict";

import { createChatRuns } from "../app/chat-runs.js";
import { actionCardHtml, isExecutionProposal, messageHasProposal, mirrorFrom, runMirrorHtml } from "../core/run-card.js";
import { esc } from "../core/format.js";

/* The card renderer needs the app's chat state, so the test drives the real
   factory with the real card renderers and an in-memory chat object. */
function harness(messages = []) {
  const chat = { current: { id: "chat-1", messages }, cards: new Map(), mirrors: new Map() };
  const runs = createChatRuns({
    esc,
    actionCardHtml,
    runMirrorHtml,
    mirrorFrom,
    stripTags: value => [String(value || "")],
    describe: () => [],
    chat,
    modelChoice: { key: "mock", effort: "" },
    getApprovalMode: () => "consequential",
    getRunHistory: () => [],
    renderChat: () => {},
    loadChatThreads: () => {},
    toast: () => {},
    applyDelta: () => {},
    setUiMode: () => {},
    openLive: () => {},
  });
  return { runs, chat };
}

const proposal = {
  id: "a1",
  role: "assistant",
  status: "ok",
  text: "我建议这样查。",
  action: { goal: "查周三上海到杭州的高铁", mode: "research", sources: ["https://example.com/a"] },
};

test("a plain assistant message renders no card at all", () => {
  const { runs } = harness([{ id: "a0", role: "assistant", status: "ok", text: "普通回答" }]);
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", text: "普通回答" }), "");
});

test("an empty action object renders no card (the old blank-card bug)", () => {
  const { runs } = harness();
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: {} }), "");
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: null }), "");
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: [] }), "");
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: { goal: "" } }), "");
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: { note: "no goal" } }), "");
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: "proposal" }), "");
});

test("a legal proposal still renders the compact proposal", () => {
  const { runs } = harness([proposal]);
  const html = runs.chatCardHtml(proposal);
  assert.ok(html.includes("data-run-card=\"a1\""));
  // The compact next-step line carries the goal; the editable form is behind
  // a disclosure, but every control the confirm endpoint needs is present.
  assert.ok(html.includes("run-card-next"));
  assert.ok(html.includes("下一步"));
  assert.ok(html.includes("data-run-start=\"a1\""));
  assert.ok(html.includes("data-run-goal"));
  assert.ok(html.includes("https://example.com/a"));
});

test("a run-anchored message still mirrors the run without an action", () => {
  const { runs } = harness();
  const message = {
    id: "a2",
    role: "assistant",
    text: "已开始。",
    run: { id: "run-9", goal: "整理文件", mode: "script", status: "running" },
  };
  const html = runs.chatCardHtml(message);
  assert.ok(html.includes("data-run-card=\"a2\""));
  assert.ok(html.includes("data-run=\"run-9\""));
  assert.ok(html.includes("整理文件"));
  assert.ok(!html.includes("data-run-start"));
});

test("a dismissed proposal renders the reconsider row, a dismissed plain message does not", () => {
  const { runs, chat } = harness([proposal]);
  chat.cards.set("a1", { dismissed: true });
  assert.ok(runs.chatCardHtml(proposal).includes("重新考虑"));
  chat.cards.set("a0", { dismissed: true });
  assert.equal(runs.chatCardHtml({ id: "a0", role: "assistant", action: {} }), "");
});

test("the proposal predicate is the single source of truth", () => {
  assert.equal(isExecutionProposal({ goal: "写一个脚本整理文件" }), true);
  assert.equal(isExecutionProposal({ goal: "写一个脚本整理文件", mode: "script" }), true);
  assert.equal(isExecutionProposal({ goal: "查资料", mode: "research", sources: ["https://a"] }), true);
  assert.equal(isExecutionProposal({ goal: "查资料", mode: "bogus" }), false);
  assert.equal(isExecutionProposal({}), false);
  assert.equal(isExecutionProposal(null), false);
  assert.equal(messageHasProposal({ action: { goal: "有目标" } }), true);
  assert.equal(messageHasProposal({}), false);
});
