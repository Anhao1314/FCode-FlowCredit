import test from "node:test";
import assert from "node:assert/strict";

import {
  actorBadge, capabilityChips, groupFindings, mapOutcome, outcomeBadge,
  renderAuthorization, renderBundle, renderBundles, renderChain, renderGoDeps, renderEntitlementDiff, renderSidecarDetail, renderReaEvidence, renderReaFacts, renderRegression, renderSpecGap,
  renderUncovered, summarizeBundle,
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

test("REA 摘要:artifact、版本、证据数与未覆盖面来自 bundle", () => {
  const sum = summarizeBundle({
    audit_id: "a",
    target: { identity: "t", manifest_sha256: "f".repeat(64), artifact_sha256: "e".repeat(64) },
    findings: [], poc_evidence_refs: [],
    rea_evidence_refs: [{ evidence_id: "ev_a" }],
    toolchain: { ruleset_version: 3, rea: { version: "6.8.0" } },
    uncovered_surfaces: [{ surface: "dynamic", reason: "static only" }],
  });
  assert.equal(sum.artifact, "e".repeat(16) + "…");
  assert.equal(sum.reaVersion, "6.8.0");
  assert.equal(sum.reaEvidence, 1);
  assert.equal(sum.uncovered, 1);
});

test("REA 证据面板:链接按 bundle 的相对路径生成并转义", () => {
  const html = renderReaEvidence({
    rea_evidence_refs: [{
      evidence_id: "ev_" + "a".repeat(64),
      operation: "inspect_go_binary",
      path: "rea-evidence/inspect_go_binary.json",
    }],
  }, 'audit"dir');
  assert.ok(html.includes("inspect_go_binary"));
  assert.ok(html.includes("/api/audit/rea/audit%22dir/inspect_go_binary.json"));
  assert.ok(!html.includes('audit"dir'));
});

test("REA 事实:列出未声明 entitlement,且不把声明外内容当已证明", () => {
  const html = renderReaFacts({
    target: { artifact_sha256: "e".repeat(64) },
    operations: {
      inspect_go_binary: {
        evidence_id: "ev_a",
        facts: { go_version: "go1.26.10", module: { path: "example/app" },
                 dependencies: [{}, {}] },
      },
    },
    host: {
      entitlements: ["audio-input", "disable-library-validation"],
      declared_features: ["audio-input"],
    },
  });
  assert.ok(html.includes("go1.26.10"));
  assert.ok(html.includes("2 deps"));
  assert.ok(html.includes("disable-library-validation"));
  assert.ok(html.includes("未声明 entitlement"));
  assert.ok(html.includes("audio-input"));
});

test("REA 事实:sidecar 摘要展示未授权 listener 与 secret 计数", () => {
  const html = renderReaFacts({
    target: { artifact_sha256: "e".repeat(64) },
    operations: {},
    host: {
      entitlements: [], declared_features: [],
      sidecars: [{
        name: "demo-bridge", source_files: 3, test_files: 2,
        listener_without_auth: [{ file: "bridge.ts", line: 1 }],
        secret_like: [], npm_install: [],
      }],
    },
  });
  assert.ok(html.includes("demo-bridge"));
  assert.ok(html.includes("3 source"));
  assert.ok(html.includes("未授权 listener 1"));
});

test("REA 事实:library validation 判定来自静态加载链事实", () => {
  const html = renderReaFacts({
    target: { artifact_sha256: "e".repeat(64) },
    operations: {},
    host: {
      entitlements: [], declared_features: [],
      library_validation: {
        status: "observed", entitlement_present: true, main_team_id: "TEAM",
        loaded_count: 22, system_count: 22, non_team_signed: [], unresolved: [],
        verdict: "no_static_justification",
      },
    },
  });
  assert.ok(html.includes("library validation"));
  assert.ok(html.includes("TEAM"));
  assert.ok(html.includes("静态加载链未发现必要性"));
});

test("未覆盖面:非空时逐条渲染,空时不生成区块", () => {
  assert.equal(renderUncovered({}), "");
  const html = renderUncovered({
    uncovered_surfaces: [{ surface: "sidecar", reason: "not collected" }],
  });
  assert.ok(html.includes("sidecar") && html.includes("not collected"));
});

test("授权卡:operator/scope/granted_at 和目标信息来自 bundle", () => {
  const html = renderAuthorization({
    authorization: { operator: "alice", scope: "static audit", granted_at: "2026-11-01T00:00:00Z",
                     time_window: "2026-11-01 → 2026-11-02" },
    target: { identity: "/tmp/app", version: "1.0.0",
              manifest_sha256: "a".repeat(64), artifact_sha256: "b".repeat(64),
              profile: "artifact" },
    toolchain: { ruleset_version: 6, replayer: "flowaudit.poc.replay/v1",
                 rea: { version: "6.8.0", facts_schema: "fcode.rea-facts/v1" } },
  });
  assert.ok(html.includes("alice"), "operator");
  assert.ok(html.includes("static audit"), "scope");
  assert.ok(html.includes("2026-11-01T00:00:00Z"), "granted_at");
  assert.ok(html.includes("2026-11-01 → 2026-11-02"), "time_window");
  assert.ok(html.includes("/tmp/app"), "target identity");
  assert.ok(html.includes("1.0.0"), "target version");
  assert.ok(html.includes("a".repeat(16) + "…"), "manifest short");
  assert.ok(html.includes("b".repeat(16) + "…"), "artifact short");
  assert.ok(html.includes("artifact"), "profile");
  assert.ok(html.includes("6.8.0"), "REA version");
  assert.ok(html.includes("v6"), "ruleset version");
  assert.ok(html.includes("授权与范围"), "section heading");
});

test("provenance chain:事件列表和链完整性状态", () => {
  const html = renderChain({
    chain_intact: true, count: 3,
    events: [
      { sequence: 1, type: "task_created", payload: { audit_id: "x" },
        prev_hash: null, hash: "a".repeat(64), at: "2026-11-01T00:00:00Z" },
      { sequence: 2, type: "candidate_proposed", payload: { candidate_id: "C-0001" },
        prev_hash: "a".repeat(64), hash: "b".repeat(64), at: "2026-11-01T00:00:01Z" },
      { sequence: 3, type: "finding_promoted", payload: { finding_id: "F-POC-0001" },
        prev_hash: "b".repeat(64), hash: "c".repeat(64), at: "2026-11-01T00:00:02Z" },
    ],
  });
  assert.ok(html.includes("task_created"), "event type");
  assert.ok(html.includes("finding_promoted"), "promoted event");
  assert.ok(html.includes("C-0001"), "candidate_id");
  assert.ok(html.includes("链完整"), "chain integrity badge");
  assert.ok(html.includes("3 events"), "event count");
});

test("provenance chain:断裂时显示警告", () => {
  const html = renderChain({
    chain_intact: false, count: 2,
    events: [
      { sequence: 1, type: "task_created", payload: {}, prev_hash: null, hash: "a".repeat(64), at: "" },
      { sequence: 2, type: "candidate_proposed", payload: {}, prev_hash: "WRONG", hash: "b".repeat(64), at: "" },
    ],
  });
  assert.ok(html.includes("链断裂"), "tamper warning");
});

test("spec-gap report:conforms 与 undetermined 判定", () => {
  const html = renderSpecGap({
    report_id: "spec-gap-demo",
    judgments: [
      { judgment_id: "SG-0001", subject: "workspace-write 下写 /tmp",
        verdict: "conforms", basis: { doc_refs: ["README.md:46"] } },
      { judgment_id: "SG-0002", subject: "session 归属校验",
        verdict: "undetermined",
        missing_spec: { question: "是否校验 session 归属？", searched_refs: ["docs/"], result: "未找到规范" } },
    ],
  });
  assert.ok(html.includes("SG-0001"), "judgment id");
  assert.ok(html.includes("conforms"), "conforms badge");
  assert.ok(html.includes("undetermined"), "undetermined badge");
  assert.ok(html.includes("workspace-write"), "subject");
  assert.ok(html.includes("1 conforms"), "conforms count");
  assert.ok(html.includes("1 undetermined"), "undetermined count");
});

test("spec-gap report:空报告不渲染", () => {
  assert.equal(renderSpecGap({ judgments: [] }), "");
  assert.equal(renderSpecGap(null), "");
});

test("Go 依赖表:展示依赖列表和 pre-release 标记", () => {
  const html = renderGoDeps({
    operations: { inspect_go_binary: { facts: { dependencies: [
      { path: "example/rc", version: "v1.0.0-rc1", prerelease: true },
      { path: "example/stable", version: "v1.2.3", prerelease: false },
    ]}}},
  });
  assert.ok(html.includes("example/rc"), "dep path");
  assert.ok(html.includes("pre-release"), "prerelease badge");
  assert.ok(html.includes("v1.2.3"), "stable version");
});

test("entitlements 对照表:undeclared 高亮", () => {
  const html = renderEntitlementDiff({
    host: { entitlements: ["audio", "library-validation"],
            declared_features: ["audio"] },
  });
  assert.ok(html.includes("audio"), "entitlement key");
  assert.ok(html.includes("undeclared"), "undeclared badge");
  assert.ok(html.includes(">declared<"), "declared badge");
});

test("sidecar 详情表:展示 flags", () => {
  const html = renderSidecarDetail({
    host: { sidecars: [
      { name: "demo-bridge", source_files: 3, test_files: 2, runtime_bundled: false,
        listener_without_auth: [{}], secret_like: [], npm_install: [], dangerous_calls: [] },
    ]},
  });
  assert.ok(html.includes("demo-bridge"), "sidecar name");
  assert.ok(html.includes("未授权 listener"), "unauth flag");
});

test("回归对比:known/new/fixed/regressed 四桶展示", () => {
  const html = renderRegression({
    prior_audit_id: "prior-001",
    known: [{ category: "AGI-07", root_cause_group: "entitlement" }],
    new: [{ category: "AGI-05", root_cause_group: "supply-chain" }],
    fixed: [{ category: "AGI-03", root_cause_group: "secret-leak" }],
    regressed: [{ category: "AGI-02", root_cause_group: "approval" }],
  });
  assert.ok(html.includes("known"), "known badge");
  assert.ok(html.includes("new"), "new badge");
  assert.ok(html.includes("fixed"), "fixed badge");
  assert.ok(html.includes("regressed"), "regressed badge");
  assert.ok(html.includes("prior-001"), "prior audit id");
  assert.ok(html.includes("entitlement"), "known finding detail");
});

test("回归对比:空数据不渲染", () => {
  assert.equal(renderRegression({ known: [], new: [], fixed: [], regressed: [] }), "");
  assert.equal(renderRegression(null), "");
});

test("空账本与空列表给出可读空态,而不是空白", () => {
  const html = renderBundles([]);
  assert.ok(html.includes("还没有审计包"));
  const ledger = renderBundle({ audit_id: "a", target: {}, findings: [], poc_evidence_refs: [] }, []);
  assert.ok(ledger.includes("没有晋升的发现"));
});
