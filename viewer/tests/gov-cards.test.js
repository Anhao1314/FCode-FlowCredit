import test from "node:test";
import assert from "node:assert/strict";

import { DISPOSITION_LABELS, findingsCardHtml } from "../core/gov-cards.js";
import { DISPOSITION_LABELS as FORWARDED } from "../audit/audit-view.js";

const findings = [
  { finding_id: "F-POC-0001", category: "AGI-07", severity: "high", root_cause_group: "entitlement-overreach" },
  { finding_id: "F-POC-0002", category: "AGI-05", severity: "medium", root_cause_group: "runtime-npm-install" },
  { finding_id: "F-POC-0003", category: "VIBE-02", severity: "low", root_cause_group: "magic-value-sprawl" },
];

test("发现处置卡:计数元数据 + 每条一行带类别与根因", () => {
  const html = findingsCardHtml("demo-2026-11-08", findings);
  assert.match(html, /3 发现 · 高 1 \/ 中 1 \/ 低 1/);
  assert.match(html, /F-POC-0001/);
  assert.match(html, /AGI-07/);
  assert.match(html, /entitlement-overreach/);
  assert.equal((html.match(/class="gov-finding"/g) || []).length, 3);
});

test("处置下拉:四选项,当前处置选中,空串即清除", () => {
  const html = findingsCardHtml("d", [
    { finding_id: "F-1", severity: "low", operator_disposition: "accepted_risk" },
  ]);
  // 有处置时:该档选中,空串不选中
  assert.match(html, /<option value="accepted_risk" selected>/);
  assert.doesNotMatch(html, /<option value="" selected>/);
  assert.doesNotMatch(html, /<option value="wont_fix" selected>/);
  assert.equal((html.match(/<option/g) || []).length, 4);
  // 无处置时:空串选中(= 清除)
  const clean = findingsCardHtml("d", [{ finding_id: "F-2", severity: "low" }]);
  assert.match(clean, /<option value="" selected>— 无 —<\/option>/);
});

test("空发现或空目录不渲染卡", () => {
  assert.equal(findingsCardHtml("d", []), "");
  assert.equal(findingsCardHtml("", findings), "");
  assert.equal(findingsCardHtml(null, findings), "");
});

test("处置下拉带 dir/finding 数据属性,深链按钮指向审计台", () => {
  const html = findingsCardHtml("demo-2026-11-08", findings);
  assert.match(html, /data-dir="demo-2026-11-08" data-finding="F-POC-0001"/);
  assert.match(html, /data-gov-open-bundle="demo-2026-11-08"/);
  assert.match(html, /查看证据/);
});

test("用户可见文本转义(根因与类别来自 bundle 事实)", () => {
  const html = findingsCardHtml("d", [
    { finding_id: "F-1", severity: "low", root_cause_group: '<script>x</script>' },
  ]);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("DISPOSITION_LABELS 单一来源迁移:audit-view 转发同一对象", () => {
  assert.equal(FORWARDED, DISPOSITION_LABELS);
  assert.equal(DISPOSITION_LABELS.accepted_risk, "接受风险");
});
