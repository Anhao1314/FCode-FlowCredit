import test from "node:test";
import assert from "node:assert/strict";

import {
  actorBadge, capabilityChips, groupFindings, mapOutcome, outcomeBadge,
  renderBundle, renderBundles, summarizeBundle,
} from "../audit/audit-view.js";

test("四值结果各有唯一的 UI 语气,未知值不猜测", () => {
  assert.equal(mapOutcome("finding").tone, "ok");
  assert.equal(mapOutcome("not_reproduced").tone, "dim");
  assert.equal(mapOutcome("invalid_poc").tone, "bad");
  assert.equal(mapOutcome("not_vulnerable_under_scope").tone, "dim");
  assert.equal(mapOutcome("dismissed").label, "dismissed", "未冻结的值原样显示,不美化");
  assert.equal(mapOutcome(undefined).label, "未知");
});

test("发现按根因归并,空账本给出空数组", () => {
  const groups = groupFindings([
    { finding_id: "F-1", root_cause_group: "approval-memory" },
    { finding_id: "F-2", root_cause_group: "approval-memory" },
    { finding_id: "F-3", root_cause_group: "plaintext-secret" },
  ]);
  assert.deepEqual(groups.map((g) => g.group), ["approval-memory", "plaintext-secret"]);
  assert.equal(groups[0].items.length, 2);
  assert.deepEqual(groupFindings([]), []);
});

test("UI 不显示强于证据的状态:candidate 行永远不是 ok 徽章", () => {
  for (const outcome of ["not_reproduced", "invalid_poc", "not_vulnerable_under_scope"]) {
    const badge = outcomeBadge(outcome);
    assert.ok(!badge.includes("at-ok"), `${outcome} 不得渲染为已验证`);
  }
  assert.ok(outcomeBadge("finding").includes("at-ok"));
});

test("esc 纪律:标题与值中的 HTML 元字符被转义", () => {
  const html = renderBundle(
    {
      audit_id: 'x" onload="alert(1)',
      target: { identity: "<script>alert(2)</script>", manifest_sha256: "a".repeat(64) },
      findings: [], poc_evidence_refs: [], toolchain: {},
    },
    [],
  );
  assert.ok(!html.includes("<script>alert(2)"), "target 身份必须被转义");
  assert.ok(!html.includes('onload="alert(1)"'), "audit_id 必须被转义");
});

test("能力清单:未授予的能力带 deny 样式", () => {
  const chips = capabilityChips([
    { kind: "filesystem_write", scope: "temp", granted: true },
    { kind: "network", scope: "none", granted: false },
  ]);
  assert.ok(chips.includes("at-cap-deny"), "未授予能力必须有 deny 视觉");
  assert.ok(!chips.includes("<script"));
});

test("bundle 摘要:manifest 只露出前 16 位", () => {
  const sum = summarizeBundle({
    audit_id: "a", target: { identity: "t", manifest_sha256: "f".repeat(64) },
    findings: [{}], poc_evidence_refs: ["1", "2"], toolchain: { ruleset_version: 3 },
  });
  assert.equal(sum.manifest, "f".repeat(16) + "…");
  assert.equal(sum.findings, 1);
  assert.equal(sum.evidence, 2);
  assert.equal(sum.ruleset, 3);
});

test("actor 徽章:已知角色着色,未知角色回落 operator 且转义", () => {
  assert.ok(actorBadge("host").includes("at-actor-host"));
  assert.ok(actorBadge("triage·shadow").includes("at-actor-operator"), "未知角色安全回落");
  assert.ok(!actorBadge('<img>').includes("<img"));
});

test("空账本与空列表给出可读空态,而不是空白", () => {
  const html = renderBundles([]);
  assert.ok(html.includes("还没有审计包"));
  const ledger = renderBundle({ audit_id: "a", target: {}, findings: [], poc_evidence_refs: [] }, []);
  assert.ok(ledger.includes("没有晋升的发现"));
});
