import test from "node:test";
import assert from "node:assert/strict";
import { modeForDeepLink, workPromptsHtml } from "../core/work-surface.js";

test("Work suggestions are explicit drafts, never automatic run actions", () => {
  const html = workPromptsHtml();
  assert.equal((html.match(/data-stage-prompt=/g) || []).length, 3);
  assert.match(html, /改造一个代码项目/);
  assert.doesNotMatch(html, /data-run-start|data-act="start"/);
});

test("Opening scene exposes three useful Work starting points", () => {
  const html = workPromptsHtml();
  assert.match(html, /研究并整理结论/);
  assert.match(html, /交付一份工作成果/);
  assert.match(html, /aria-label="任务建议"/);
});

test("run and trace deep links select Work without changing ordinary startup", () => {
  assert.equal(modeForDeepLink("?run=run-123"), "work");
  assert.equal(modeForDeepLink("?trace=%2Ftraces%2Ffull.json"), "work");
  assert.equal(modeForDeepLink("?run=run-123&desktop=1"), "work");
  assert.equal(modeForDeepLink("?chat=chat-123"), null);
  assert.equal(modeForDeepLink(""), null);
});

test("auditHashTarget:#/audit 与 #/audit/<dir> 解析,其余 hash 为 null", async () => {
  const { auditHashTarget } = await import("../core/work-surface.js");
  assert.equal(auditHashTarget("#/audit"), "");
  assert.equal(auditHashTarget("#/audit/evox-2026-11-03"), "evox-2026-11-03");
  assert.equal(auditHashTarget(""), null);
  assert.equal(auditHashTarget("#/ledger"), null);
  assert.equal(auditHashTarget("#/audit/"), null, "空 dir 不合法");
  assert.equal(auditHashTarget("#/audit/..%2Fetc"), null, "越界值拒绝");
  assert.equal(auditHashTarget("#/audit/UPPER"), null, "目录白名单是小写");
});
