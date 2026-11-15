import test from "node:test";
import assert from "node:assert/strict";

import {
  targetKey, targetLabel, aggregateTargets, filterFindings,
  govSurfaceSwitchHtml, govSummaryHtml, findingsToolbarHtml, findingsTableHtml, findingDetailHtml,
} from "../core/gov-workbench.js";

const findings = [
  { finding_id: "F-2", category: "AGI-07", severity: "medium", root_cause_group: "entitlement-overreach" },
  { finding_id: "F-1", category: "VIBE-02", severity: "high", root_cause_group: "magic-value-sprawl" },
  { finding_id: "F-3", category: "AGI-05", severity: "low", root_cause_group: "runtime-npm-install", operator_disposition: "repaired" },
];

test("target:身份取 target，标签取路径末段", () => {
  assert.equal(targetKey({ target: "/repo/app" }), "/repo/app");
  assert.equal(targetKey({ dir: "demo-1" }), "demo-1");
  assert.equal(targetLabel({ target: "/Applications/EvoX.app/" }), "EvoX.app");
  assert.equal(targetLabel({ target: "/repo/fixtures/demo-target" }), "demo-target");
  assert.equal(targetLabel({}), "审计记录");
});

test("聚合:同一目标只留一行，最近一次 dir 胜出并累计运行次数", () => {
  const rows = aggregateTargets([
    { dir: "evox-2026-11-08-aaaaaa", audit_id: "old", target: "/Applications/EvoX.app", findings: 9, severity: { high: 4 }, unhandled: 9 },
    { dir: "evox-2026-11-09-bbbbbb", audit_id: "new", target: "/Applications/EvoX.app", findings: 2, severity: { high: 1 }, unhandled: 1, regression: { status: "fixed" } },
    { dir: "demo-2026-11-09-cccccc", audit_id: "demo", target: "/repo/demo-target", findings: 0 },
  ]);
  assert.equal(rows.length, 2);
  const evox = rows.find(row => row.label === "EvoX.app");
  assert.equal(evox.dir, "evox-2026-11-09-bbbbbb");
  assert.equal(evox.auditId, "new");
  assert.equal(evox.runs, 2);
  assert.equal(evox.findings, 2);
  assert.deepEqual(evox.severity, { high: 1 });
  assert.equal(evox.unhandled, 1);
  assert.deepEqual(evox.regression, { status: "fixed" });
  // 新 dir 排前
  assert.equal(rows[0].label, "EvoX.app");
});

test("聚合:目标之间按最近日期排序，不被目标 slug 字母序带偏", () => {
  const rows = aggregateTargets([
    { dir: "aaa-2026-11-08-111111", target: "/repo/aaa", findings: 1 },
    { dir: "zzz-2026-11-10-222222", target: "/repo/zzz", findings: 1 },
    { dir: "mmm-2026-11-09-333333", target: "/repo/mmm", findings: 1 },
  ]);
  assert.deepEqual(rows.map(row => row.label), ["zzz", "mmm", "aaa"]);
});

test("筛选:严重度 + 未处置 + 搜索，按高危到低危排序", () => {
  assert.deepEqual(filterFindings(findings, {}).map(f => f.finding_id), ["F-1", "F-2", "F-3"]);
  assert.deepEqual(filterFindings(findings, { severity: "medium" }).map(f => f.finding_id), ["F-2"]);
  assert.deepEqual(filterFindings(findings, { unhandled: true }).map(f => f.finding_id), ["F-1", "F-2"]);
  assert.deepEqual(filterFindings(findings, { query: "AGI-0" }).map(f => f.finding_id), ["F-2", "F-3"]);
  assert.deepEqual(filterFindings(findings, { query: "MAGIC" }).map(f => f.finding_id), ["F-1"]);
  assert.equal(filterFindings(findings, { query: "no-such" }).length, 0);
});

test("治理面切换:治理台/团队/工作台/审计台四个入口，当前面始终点亮", () => {
  const dialog = govSurfaceSwitchHtml("dialog");
  assert.match(dialog, /role="tablist"/);
  assert.match(dialog, /data-gov-mode="dialog" aria-selected="true"/);
  assert.match(dialog, /data-gov-mode="team" aria-selected="false"/);
  assert.match(dialog, /data-gov-mode="workbench" aria-selected="false"/);
  assert.match(dialog, /data-open="audit" aria-selected="false"/);
  assert.match(dialog, /审计台<\/button>/);
  assert.match(dialog, /治理台<\/button>/);
  assert.match(dialog, /团队<\/button>/);
  const workbench = govSurfaceSwitchHtml("workbench");
  assert.match(workbench, /data-gov-mode="dialog" aria-selected="false"/);
  assert.match(workbench, /data-gov-mode="workbench" aria-selected="true"/);
  const audit = govSurfaceSwitchHtml("audit");
  assert.match(audit, /data-open="audit" aria-selected="true"/, "审计台视图点亮自己的入口");
  assert.doesNotMatch(audit, /data-gov-mode="(dialog|team|workbench)" aria-selected="true"/,
    "审计台是独立视图，不能同时点亮其它面");
});

test("摘要:身份/审计 id/计数/未处置/session 与四个动作", () => {
  const html = govSummaryHtml({
    dir: "evox-2026-11-09-bbbbbb",
    bundle: {
      audit_id: "evox-2026-11-09-bbbbbb",
      target: { identity: "/Applications/EvoX.app", profile: "artifact" },
      sessions: [{ session_ref: "rea-sess-1" }],
    },
    findings,
  });
  assert.match(html, /EvoX\.app/);
  assert.match(html, /\/Applications\/EvoX\.app/);
  assert.match(html, /evox-2026-11-09-bbbbbb/);
  assert.match(html, /3 发现/);
  assert.match(html, /高 1 · 中 1 · 低 1/);
  assert.match(html, /2 未处置/);
  assert.match(html, /rea-sess-1/);
  assert.match(html, /data-gov-redig="evox-2026-11-09-bbbbbb"/);
  assert.match(html, /data-gov-chain="evox-2026-11-09-bbbbbb"/);
  assert.match(html, /data-gov-open-bundle="evox-2026-11-09-bbbbbb"/);
});

test("摘要:无记录时给空态，不伪造目标", () => {
  const html = govSummaryHtml({ dir: "", bundle: null, findings: [] });
  assert.match(html, /还没有审计记录/);
  assert.doesNotMatch(html, /data-gov-redig/);
});

test("筛选条:激活档位、未处置勾选、搜索值与计数宿主", () => {
  const html = findingsToolbarHtml(
    { severity: "high", unhandled: true, query: '<script>"x"' },
    { visible: 1, total: 3 },
  );
  assert.match(html, /data-gov-sev="high" aria-pressed="true"/);
  assert.match(html, /data-gov-sev="all" aria-pressed="false"/);
  assert.match(html, /data-gov-unhandled checked/);
  assert.match(html, /value="&lt;script&gt;&quot;x&quot;"/);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /id="gov-findings-count">1 \/ 3<\/span>/);
});

test("发现表:行带 finding 锚点、处置 pill、当前选中态与筛选后计数", () => {
  const html = findingsTableHtml("d", findings, { severity: "all" }, "F-2");
  assert.equal((html.match(/data-gov-finding=/g) || []).length, 3);
  assert.match(html, /data-gov-finding="F-2"/);
  assert.match(html, /gov-finding-row active/);
  assert.match(html, /已修复/);
  assert.match(html, /未处置/);
  assert.match(html, /sev-high/);
  assert.match(html, /role="list"/);
  assert.match(html, /<div class="gov-findings-head" aria-hidden="true">/);
  assert.match(html, /<span>严重度<\/span><span>编号<\/span><span>类别<\/span><span>根因<\/span><span>处置<\/span>/);
  const empty = findingsTableHtml("d", findings, { query: "nothing" });
  assert.match(empty, /没有匹配的发现/);
});

test("发现表:文本转义（类别/根因来自 bundle 事实）", () => {
  const html = findingsTableHtml("d", [{
    finding_id: 'F-"1"', category: "<img>", severity: "high", root_cause_group: "<script>x</script>",
  }]);
  assert.equal(html.includes("<img>"), false);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;img&gt;/);
  assert.match(html, /&lt;script&gt;/);
});

test("详情:证据引用 + 五档处置 + 修复按钮 + 关闭", () => {
  const finding = {
    ...findings[0],
    poc_refs: ["poc/one.json", "poc/two.json"],
    operator_disposition: "accepted_risk",
    root_cause_note: "operator note",
  };
  const html = findingDetailHtml("d", finding, { repairable: true });
  assert.match(html, /data-gov-detail-close/);
  assert.match(html, /poc\/one\.json/);
  assert.match(html, /operator note/);
  assert.match(html, /data-dir="d" data-finding="F-2"/);
  assert.match(html, /<option value="accepted_risk" selected>/);
  assert.equal((html.match(/<option/g) || []).length, 5);
  assert.match(html, /data-gov-repair="F-2" data-dir="d"/);
  assert.match(html, /data-gov-open-bundle="d"/);
  assert.match(html, /data-gov-fix-submit/);
  assert.match(html, /data-gov-fix-delegate/);
  assert.match(html, /gov-fix-verification/);
  assert.match(html, /gov-fix-patch/);
  // artifact 目标不可修
  const artifact = findingDetailHtml("d", finding, { repairable: false });
  assert.doesNotMatch(artifact, /data-gov-repair/);
  assert.doesNotMatch(artifact, /data-gov-fix-submit/);
  assert.doesNotMatch(artifact, /gov-fix-patch/);
});

test("详情:未选中时给空态", () => {
  assert.match(findingDetailHtml("d", null), /选择一条发现/);
});