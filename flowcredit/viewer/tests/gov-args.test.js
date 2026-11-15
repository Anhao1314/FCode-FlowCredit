import test from "node:test";
import assert from "node:assert/strict";

import { parseGovAuditArgs } from "../core/gov-args.js";

test("挖洞参数:纯目标原样保留", () => {
  assert.deepEqual(parseGovAuditArgs("/Applications/EvoX.app"), {
    target: "/Applications/EvoX.app", targetMd: "", osvSnapshot: "",
  });
});

test("挖洞参数:--target-md 空格与等号两种写法", () => {
  const spaced = parseGovAuditArgs(
    "/Applications/EvoX.app --target-md fixtures/evox/target.md");
  assert.equal(spaced.target, "/Applications/EvoX.app");
  assert.equal(spaced.targetMd, "fixtures/evox/target.md");
  const equals = parseGovAuditArgs(
    "/Applications/EvoX.app --target-md=fixtures/evox/target.md");
  assert.equal(equals.target, "/Applications/EvoX.app");
  assert.equal(equals.targetMd, "fixtures/evox/target.md");
});

test("挖洞参数:两个 flag 同时出现，目标里的空格不被拆开", () => {
  const parsed = parseGovAuditArgs(
    "/Applications/My App.app --osv-snapshot work/audit/osv.json --target-md fixtures/x.md");
  assert.equal(parsed.target, "/Applications/My App.app");
  assert.equal(parsed.targetMd, "fixtures/x.md");
  assert.equal(parsed.osvSnapshot, "work/audit/osv.json");
});

test("挖洞参数:只有 flag 时目标为空；未知 flag 留在目标里交给后端拒绝", () => {
  assert.equal(parseGovAuditArgs("--target-md fixtures/x.md").target, "");
  const unknown = parseGovAuditArgs("/Applications/EvoX.app --depth 3");
  assert.equal(unknown.target, "/Applications/EvoX.app --depth 3");
});
