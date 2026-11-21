import test from "node:test";
import assert from "node:assert/strict";

import { isAuditHash, parseHash, runHashFor } from "../app/run-route.js";

test("runHashFor 用 #/run/ 前缀并对 id 做 URL 编码", () => {
  assert.equal(runHashFor("abc"), "#/run/abc");
  assert.equal(runHashFor("run-20261120-010238-2132"), "#/run/run-20261120-010238-2132");
  assert.equal(runHashFor("a b/c?d&e=f"), "#/run/a%20b%2Fc%3Fd%26e%3Df");
  assert.equal(runHashFor(""), "#/run/");
});

test("parseHash 的 run 形状：#/run/<非空 id> 判为 run", () => {
  assert.deepEqual(parseHash("#/run/abc"), { kind: "run", id: "abc" });
  assert.deepEqual(parseHash("#/run/run-20261120-010238-2132"), {
    kind: "run",
    id: "run-20261120-010238-2132",
  });
});

test("parseHash 还原 URL 编码的 run id", () => {
  assert.deepEqual(parseHash("#/run/a%20b%2Fc%3Fd%26e%3Df"), { kind: "run", id: "a b/c?d&e=f" });
  assert.deepEqual(parseHash("#/run/%E4%B8%AD%E6%96%87"), { kind: "run", id: "中文" });
});

test("runHashFor 与 parseHash 互为逆运算", () => {
  const ids = ["abc", "run-1", "a b/c?d&e=f", "中文", "x%y"];
  for (const id of ids) {
    assert.deepEqual(parseHash(runHashFor(id)), { kind: "run", id });
  }
});

test("parseHash 的 audit 形状：#/audit 与 #/audit/<dir> 优先判为 audit", () => {
  assert.deepEqual(parseHash("#/audit"), { kind: "audit", dir: "" });
  assert.deepEqual(parseHash("#/audit/bundles"), { kind: "audit", dir: "bundles" });
  assert.deepEqual(parseHash("#/audit/run-20261120-010238-2132"), {
    kind: "audit",
    dir: "run-20261120-010238-2132",
  });
  assert.deepEqual(parseHash("#/audit/a%20b"), { kind: "audit", dir: "a b" });
});

test("audit 优先于 run 字样", () => {
  assert.equal(parseHash("#/audit/run/abc").kind, "audit");
  assert.deepEqual(parseHash("#/audit/run/abc"), { kind: "audit", dir: "run/abc" });
});

test("parseHash 的非法 run 形状：#/run/ 与畸形 hash 判 invalid", () => {
  assert.deepEqual(parseHash("#/run/"), { kind: "run", id: "", invalid: true });
  assert.deepEqual(parseHash("#/run"), { kind: "run", id: "", invalid: true });
  assert.deepEqual(parseHash("#/run/   "), { kind: "run", id: "", invalid: true });
  assert.deepEqual(parseHash("#/run/%E0%A4%A"), { kind: "run", id: "", invalid: true });
  assert.equal(parseHash("#/run/").invalid, true);
});

test("parseHash 的空 hash 与其他形状判为 home", () => {
  assert.deepEqual(parseHash(""), { kind: "home" });
  assert.deepEqual(parseHash("#"), { kind: "home" });
  assert.deepEqual(parseHash("#/other"), { kind: "home" });
  assert.deepEqual(parseHash("#/dashboard/settings"), { kind: "home" });
  assert.deepEqual(parseHash("#auditx"), { kind: "home" });
  assert.deepEqual(parseHash(undefined), { kind: "home" });
  assert.deepEqual(parseHash(null), { kind: "home" });
});

test("isAuditHash 只对 audit 形状返回 true", () => {
  assert.equal(isAuditHash("#/audit"), true);
  assert.equal(isAuditHash("#/audit/bundles"), true);
  assert.equal(isAuditHash("#/audit/a%20b"), true);
  assert.equal(isAuditHash("#/run/abc"), false);
  assert.equal(isAuditHash("#/run/"), false);
  assert.equal(isAuditHash(""), false);
  assert.equal(isAuditHash("#/other"), false);
  assert.equal(isAuditHash(undefined), false);
});
