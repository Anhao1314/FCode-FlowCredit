import test from "node:test";
import assert from "node:assert/strict";
import { createReviewUI } from "../app/review.js";

test("report consumers can await the in-flight reviewed trace refresh", async () => {
  const trace = { task: { id: "task-run-1" }, events: [], terminal: { status: "succeeded" } };
  const cache = new Map();
  const store = { key: "run:run-1", trace: null, check: null };
  const session = { id: "run-1", trace: null };
  let releaseTrace;
  const decision = {
    key: "accept", label: "接受", hint: "", requiresReason: false, placeholder: "",
  };

  globalThis.document = {
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  globalThis.fetch = (url) => {
    if (url === "/api/runs/run-1/review") {
      return Promise.resolve({
        ok: true,
        json: async () => ({ decision: "accept", sequence: 52, event: { sequence: 52 } }),
      });
    }
    if (url === "/api/runs/run-1/trace") {
      return new Promise(resolve => {
        releaseTrace = () => resolve({ ok: true, json: async () => trace });
      });
    }
    throw new Error("unexpected fetch: " + url);
  };

  const ui = createReviewUI({
    esc: String,
    verifyChain: async () => ({ ok: true, events: 0 }),
    REVIEW_DECISIONS: [decision],
    REVIEW_DECISION: { accept: decision },
    REVIEW_LABEL: { accept: "接受" },
    REVIEW_COUNT_LABEL: { accept: "接受" },
    store,
    cache,
    activeSession: () => session,
    currentRun: () => ({ id: "run-1" }),
    shownTrace: () => store.trace || trace,
    runTraceUrl: id => `/api/runs/${id}/trace`,
    setChainBadge: () => {},
    toast: () => {},
  });

  assert.equal(ui.waitForReviewTraceRefresh("run-1"), null);
  ui.chooseReviewDecision("run-1", "accept");
  await ui.submitReview("run-1");

  const pending = ui.waitForReviewTraceRefresh("run-1");
  assert.ok(pending instanceof Promise, "the report must see the refresh promise");
  assert.equal(cache.has("run:run-1"), false, "the deferred trace has not arrived yet");

  releaseTrace();
  await pending;
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(cache.get("run:run-1"), trace);
  assert.strictEqual(store.trace, trace);
  assert.strictEqual(session.trace, trace);
  assert.equal(ui.waitForReviewTraceRefresh("run-1"), null, "settled refreshes are removed");
});
