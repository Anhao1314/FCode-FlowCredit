import test from "node:test";
import assert from "node:assert/strict";
test("run and trace deep links select Work without changing ordinary startup", () => {
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
