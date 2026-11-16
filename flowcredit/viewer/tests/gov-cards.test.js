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

test("处置下拉:五选项(含已修复),当前处置选中,空串即清除", () => {
  const html = findingsCardHtml("d", [
    { finding_id: "F-1", severity: "low", operator_disposition: "accepted_risk" },
  ]);
  // 有处置时:该档选中,空串不选中
  assert.match(html, /<option value="accepted_risk" selected>/);
  assert.doesNotMatch(html, /<option value="" selected>/);
  assert.doesNotMatch(html, /<option value="wont_fix" selected>/);
  assert.equal((html.match(/<option/g) || []).length, 5);
  assert.match(html, /<option value="repaired">已修复<\/option>/);
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
  // P2:发现卡可回频道定位同一条运行
  assert.match(html, /data-gov-run-by-audit="demo-2026-11-08"/);
  assert.match(html, /频道 →/);
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

/* ---------- G3 链验证卡与判断卡 ---------- */

import { chainCardHtml, judgmentsCardHtml } from "../core/gov-cards.js";

const intactChain = {
  chain_intact: true, verification: "kernel", count: 3,
  events: [
    { type: "task_created", payload: { audit_id: "x" }, hash: "48078bd3fec9" },
    { type: "candidate_proposed", payload: { candidate_id: "C-0001" }, hash: "fd2a9c14c8bc" },
    { type: "finding_promoted", payload: { finding_id: "F-POC-0001" }, hash: "fa146fba01f8" },
  ],
};

test("链卡:kernel 完整态 + 事件计数与末尾事件行", () => {
  const html = chainCardHtml("demo-2026", intactChain);
  assert.match(html, /🔗 链完整/);
  assert.match(html, /3 事件 · kernel/);
  assert.match(html, /3 · finding_promoted/);
  assert.match(html, /fa146fba01/, "事件哈希截前 10 位");
  assert.match(html, /data-gov-open-bundle="demo-2026"/);
  assert.match(html, /data-gov-run-by-audit="demo-2026"/, "链卡可回频道定位运行");
});

test("链卡:断裂带篡改证据,link-only 如实标注弱校验", () => {
  const broken = chainCardHtml("d", {
    chain_intact: false, verification: "kernel", tamper: "event 4 hash mismatch",
    events: [{ type: "task_created" }, { type: "finding_promoted" }],
  });
  assert.match(broken, /链断裂/);
  assert.match(broken, /event 4 hash mismatch/);
  const weak = chainCardHtml("d", { chain_intact: true, verification: "link-only", events: [] });
  assert.match(weak, /仅链接校验/);
  assert.doesNotMatch(weak, /wcard-row/);
});

test("链卡:payload 只取前三个键名,防长对象刷屏", () => {
  const html = chainCardHtml("d", {
    chain_intact: true, verification: "kernel",
    events: [{ type: "t", payload: { a: 1, b: 2, c: 3, d: 4, e: 5 } }],
  });
  assert.match(html, /t · a,b,c/);
  assert.doesNotMatch(html, /,d/);
});

test("判断卡:四类计数并列审计处置,不并计数", () => {
  const html = judgmentsCardHtml({
    counts: { accept: 2, needs_evidence: 0, qualified: 1, redo: 0 },
    recent: [{ decision: "接受判断", reason: "口径核对完成", at: "16:09" }],
    audit_dispositions: [{ disposition: "accepted_risk", category: "AGI-07", finding_id: "F-POC-0001" }],
  });
  assert.match(html, /接受判断 2 · 需要补材料 0 · 保留意见 1 · 退回重做 0/);
  assert.match(html, /口径核对完成/);
  assert.match(html, /接受风险/);
  assert.match(html, /AGI-07 · F-POC-0001/);
  assert.match(html, /data-open="reviews"/);
  assert.match(html, /data-gov-channel/, "判断卡可打开统一运行流");
});

test("判断卡:无任何记录时渲染空态", () => {
  const html = judgmentsCardHtml({ counts: {}, recent: [], audit_dispositions: [] });
  assert.match(html, /还没有判断记录/);
  const broken = judgmentsCardHtml(null);
  assert.match(broken, /还没有判断记录/);
});

test("链卡与判断卡对事实文本转义", () => {
  const chain = chainCardHtml("d", { chain_intact: false, tamper: "<b>tamper</b>", events: [] });
  assert.equal(chain.includes("<b>tamper</b>"), false);
  assert.match(chain, /&lt;b&gt;tamper/);
  const judged = judgmentsCardHtml({ counts: {}, recent: [{ decision: "<i>x</i>" }], audit_dispositions: [] });
  assert.equal(judged.includes("<i>x</i>"), false);
});


/* ---------- S1 修洞闭环:派修复 / 重新挖洞 ---------- */

test("发现行带「派 agent 修复」按钮,artifact 目标不出现", () => {
  const source = findingsCardHtml("d", [{ finding_id: "F-1", severity: "high" }], { repairable: true });
  assert.match(source, /data-gov-repair="F-1" data-dir="d"/);
  assert.match(source, /派 agent 修复/);
  const artifact = findingsCardHtml("d", [{ finding_id: "F-1", severity: "high" }], { repairable: false });
  assert.doesNotMatch(artifact, /data-gov-repair/);
});

test("发现卡头带「重新挖洞」按钮(验证修复的闭环入口)", () => {
  const html = findingsCardHtml("demo-2026", findings);
  assert.match(html, /data-gov-redig="demo-2026"/);
  assert.match(html, /重新挖洞 →/);
});
