/* 审计台（发现账本）的纯渲染层 + 数据接线。

   四条 UI 原则在这里落地（与 mockup 一致）：
   1. UI 不显示强于链上证据的状态——徽章只看 poc-evidence 的 outcome；
   2. 证据优先于叙述——发现行展开即证据链，不写形容词；
   3. 执行者可见——actor 徽章（host/triage/operator）随事件渲染；
   4. 目标能力清单前置——每个发现展示声明过的能力边界。

   与 app.js 的关系：这是独立模块、独立页面（audit.html），不向 god file
   添加一行。纯函数（mapOutcome / outcomeBadge / groupFindings /
   summarizeBundle / renderBundle / render* 系列）全部导出，node --test
   直接覆盖；init() 只做 fetch 与事件接线。 */

import { esc } from "../core/format.js";

const OUTCOME_MAP = {
  finding: { label: "已验证", tone: "ok" },
  not_reproduced: { label: "未复现(保留)", tone: "dim" },
  invalid_poc: { label: "证据无效", tone: "bad" },
  not_vulnerable_under_scope: { label: "范围内不成立", tone: "dim" },
  // renderBundle 内部哨兵:finding 引用的 poc 证据读取失败/不存在。
  __evidence_missing__: { label: "证据缺失", tone: "bad" },
};

// 操作员处置的单一标签来源:审计台下拉与工作台判断记录页共用。
export const DISPOSITION_LABELS = {
  "": "— 无 —",
  accepted_risk: "接受风险",
  false_positive_disputed: "误报争议",
  wont_fix: "不修复",
};

// 统一壳的审计 surface 外壳:bundle 列表 + 详情双栏,由 app.js 的
// renderAudit() 注入 #main 后调用 init()。独立 audit.html 已退役,
// 这两个 id 只在这里出现一次。
export function auditShellHtml() {
  return `<div class="at-wrap">
    <aside class="at-side" id="at-bundles"><div class="at-none dim">加载中…</div></aside>
    <main class="at-main" id="at-main">
      <div class="at-none dim">选择左侧一次审计,查看它的发现与证据。</div>
    </main>
  </div>`;
}

export function mapOutcome(outcome) {
  return OUTCOME_MAP[outcome] || { label: outcome || "未知", tone: "dim" };
}

export function groupFindings(findings) {
  const groups = new Map();
  for (const finding of findings || []) {
    const key = finding.root_cause_group || finding.finding_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return [...groups.entries()].map(([group, items]) => ({ group, items }));
}

export function summarizeBundle(bundle) {
  const target = bundle.target || {};
  const toolchain = bundle.toolchain || {};
  const artifact = target.artifact_sha256 || "";
  return {
    auditId: bundle.audit_id || "—",
    target: target.identity || "—",
    manifest: (target.manifest_sha256 || "").slice(0, 16) + "…",
    artifact: artifact ? artifact.slice(0, 16) + "…" : "—",
    findings: (bundle.findings || []).length,
    evidence: (bundle.poc_evidence_refs || []).length,
    reaEvidence: (bundle.rea_evidence_refs || []).length,
    reaVersion: (toolchain.rea || {}).version || "—",
    ruleset: toolchain.ruleset_version || "—",
    uncovered: (bundle.uncovered_surfaces || []).length,
  };
}

export function outcomeBadge(outcome) {
  const mapped = mapOutcome(outcome);
  return `<span class="at-badge at-${mapped.tone}">${esc(mapped.label)}</span>`;
}

export function actorBadge(actor) {
  if (!actor) return "";
  const known = ["host", "triage", "analyst", "operator"];
  const name = String(actor);
  const cls = known.includes(name) ? name : "operator";
  return `<span class="at-actor at-actor-${cls}">${esc(name)}</span>`;
}

export function capabilityChips(capabilities) {
  return (capabilities || []).map((cap) => {
    const granted = cap.granted !== false;
    return `<span class="at-cap ${granted ? "" : "at-cap-deny"}">`
      + `${esc(cap.kind || "?")}·${esc(cap.scope || "")}</span>`;
  }).join("");
}

export function bundleItemHtml(entry, selected) {
  const sel = selected ? " at-sel" : "";
  return `<button class="at-bitem${sel}" data-dir="${esc(entry.dir)}">`
    + `<span class="at-bid">${esc(entry.audit_id || entry.dir)}</span>`
    + `<span class="at-bmeta">${esc(String(entry.findings ?? 0))} 发现 · `
    + `${esc(String(entry.evidence ?? 0))} 证据`
    + (entry.rea_evidence ? ` · ${esc(String(entry.rea_evidence))} REA` : "")
    + `</span></button>`;
}

export function renderGoDeps(facts) {
  const goOp = ((facts || {}).operations || {}).inspect_go_binary;
  const deps = goOp && goOp.facts && Array.isArray(goOp.facts.dependencies) ? goOp.facts.dependencies : [];
  if (!deps.length) return "";
  const rows = deps.map((dep) => {
    const pre = dep.prerelease ? ' <span class="at-badge at-warn">pre-release</span>' : "";
    return `<tr><td class="at-mono">${esc(dep.path || "—")}</td><td class="at-mono">${esc(dep.version || "—")}</td><td>${pre}</td></tr>`;
  }).join("");
  return `<details class="at-expand"><summary>Go 依赖表 (${deps.length})</summary>
    <table class="at-table"><thead><tr><th>path</th><th>version</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  </details>`;
}

export function renderEntitlementDiff(facts) {
  const host = (facts || {}).host || {};
  const entitlements = Array.isArray(host.entitlements) ? host.entitlements : [];
  const declared = Array.isArray(host.declared_features) ? host.declared_features : [];
  if (!entitlements.length && !declared.length) return "";
  const allKeys = [...new Set([...entitlements, ...declared])].sort();
  const rows = allKeys.map((key) => {
    const hasEnt = entitlements.includes(key);
    const hasDecl = declared.includes(key);
    const status = hasEnt && hasDecl
      ? '<span class="at-badge at-ok">declared</span>'
      : hasEnt ? '<span class="at-badge at-bad">undeclared</span>'
      : '<span class="at-badge at-dim">declared only</span>';
    return `<tr><td class="at-mono">${esc(key)}</td><td>${status}</td></tr>`;
  }).join("");
  return `<details class="at-expand"><summary>Entitlements 对照表 (${entitlements.length} observed / ${declared.length} declared)</summary>
    <table class="at-table"><thead><tr><th>key</th><th>status</th></tr></thead><tbody>${rows}</tbody></table>
  </details>`;
}

export function renderSidecarDetail(facts) {
  const sidecars = ((facts || {}).host || {}).sidecars;
  if (!Array.isArray(sidecars) || !sidecars.length) return "";
  const rows = sidecars.map((s) => {
    const flags = [];
    if ((s.listener_without_auth || []).length) flags.push(`<span class="at-badge at-bad">未授权 listener ×${s.listener_without_auth.length}</span>`);
    if ((s.secret_like || []).length) flags.push(`<span class="at-badge at-warn">secret-like ×${s.secret_like.length}</span>`);
    if ((s.npm_install || []).length) flags.push(`<span class="at-badge at-warn">npm ×${s.npm_install.length}</span>`);
    if ((s.dangerous_calls || []).length) flags.push(`<span class="at-badge at-dim">dangerous_calls ×${s.dangerous_calls.length}</span>`);
    const flagHtml = flags.length ? flags.join(" ") : '<span class="at-badge at-ok">clean</span>';
    return `<tr><td>${esc(s.name)}</td><td>${esc(String(s.source_files ?? 0))} src / ${esc(String(s.test_files ?? 0))} test</td><td>${flagHtml}</td><td>${s.runtime_bundled ? "runtime bundled" : "—"}</td></tr>`;
  }).join("");
  return `<details class="at-expand"><summary>Sidecar 详情 (${sidecars.length})</summary>
    <table class="at-table"><thead><tr><th>name</th><th>files</th><th>flags</th><th>runtime</th></tr></thead><tbody>${rows}</tbody></table>
  </details>`;
}

export function renderReaFacts(facts) {
  if (!facts || typeof facts !== "object") return "";
  const target = facts.target || {};
  const host = facts.host || {};
  const operations = Object.entries(facts.operations || {});
  const entitlements = Array.isArray(host.entitlements) ? host.entitlements : [];
  const declared = Array.isArray(host.declared_features) ? host.declared_features : [];
  const extra = entitlements.filter((item) => !declared.includes(item));
  const sidecars = Array.isArray(host.sidecars) ? host.sidecars : [];
  const validation = host.library_validation || null;
  const validationVerdict = validation ? ({
    not_applicable: "不适用",
    no_static_justification: "静态加载链未发现必要性",
    requires_review: "存在需复核的加载路径",
    unknown: "存在未解析的加载路径",
  }[validation.verdict] || validation.verdict) : "";
  const validationRow = validation && validation.status === "observed"
    ? `<div class="at-kv"><span class="k">library validation</span><span class="v">`
      + `team ${esc(validation.main_team_id || "—")} · loaded ${esc(String(validation.loaded_count ?? 0))}`
      + ` · system ${esc(String(validation.system_count ?? 0))}`
      + ` · non-team ${esc(String((validation.non_team_signed || []).length))}`
      + ` · unresolved ${esc(String((validation.unresolved || []).length))}`
      + ` · ${esc(validationVerdict)}</span></div>`
    : "";
  const sidecarRows = sidecars.map((sidecar) => {
    const flags = [];
    if ((sidecar.listener_without_auth || []).length) {
      flags.push(`未授权 listener ${(sidecar.listener_without_auth || []).length}`);
    }
    if ((sidecar.secret_like || []).length) {
      flags.push(`secret-like ${(sidecar.secret_like || []).length}`);
    }
    if ((sidecar.npm_install || []).length) {
      flags.push(`npm install/exec ${(sidecar.npm_install || []).length}`);
    }
    const summary = `${sidecar.source_files || 0} source · ${sidecar.test_files || 0} test`
      + (flags.length ? ` · ${flags.join(" · ")}` : "");
    return `<div class="at-rea-op">
      <span class="at-mono">${esc(sidecar.name || "?")}</span>
      <span class="at-rea-bits">${esc(summary)}</span>
    </div>`;
  }).join("");
  const opRows = operations.map(([name, op]) => {
    const value = (op || {}).facts || {};
    const bits = [];
    if (value.go_version) bits.push(`Go ${value.go_version}`);
    if ((value.module || {}).path) bits.push(value.module.path);
    if (Array.isArray(value.dependencies)) bits.push(`${value.dependencies.length} deps`);
    if (typeof value.entry_count === "number") bits.push(`${value.entry_count} entries`);
    if ((value.statistics || {}).relevant_files != null) {
      bits.push(`${value.statistics.relevant_files} files`);
    }
    return `<div class="at-rea-op">
      <span class="at-mono">${esc(name)}</span>
      <span class="at-mono dim">${esc((op || {}).evidence_id || "")}</span>
      <span class="at-rea-bits">${esc(bits.join(" · ") || "—")}</span>
    </div>`;
  }).join("");
  const extras = extra.length
    ? `<div class="at-rea-extra">未声明 entitlement: ${extra.map((item) => `<code>${esc(item)}</code>`).join(" ")}</div>`
    : `<div class="at-rea-ok">entitlements 均在声明范围内</div>`;
  return `<section class="at-rea-facts">
    <h2>REA 事实</h2>
    <div class="at-kv"><span class="k">artifact</span><span class="v mono">${esc(target.artifact_sha256 || "—")}</span></div>
    <div class="at-kv"><span class="k">operations</span><span class="v">${esc(operations.map(([name]) => name).join(", ") || "—")}</span></div>
    ${opRows}
    <div class="at-kv"><span class="k">entitlements</span><span class="v">${esc(entitlements.join(", ") || "—")}</span></div>
    <div class="at-kv"><span class="k">declared</span><span class="v">${esc(declared.join(", ") || "—")}</span></div>
    ${extras}
    ${validationRow}
    ${sidecarRows ? `<div class="at-kv"><span class="k">sidecars</span><span class="v">${esc(sidecars.map((item) => item.name || "?").join(", "))}</span></div>${sidecarRows}` : ""}
    ${renderGoDeps(facts)}
    ${renderEntitlementDiff(facts)}
    ${renderSidecarDetail(facts)}
  </section>`;
}

export function renderReaEvidence(bundle, dir = "") {
  const refs = bundle.rea_evidence_refs || [];
  if (!refs.length) return "";
  const rows = refs.map((ref) => {
    const filename = String(ref.path || "").split("/").pop();
    if (!filename) {
      // ref 缺 path 时无法构造合法 URL——如实显示而不是生成死链。
      return `<li>${esc(ref.operation || "?")} <span class="at-mono dim">路径缺失</span></li>`;
    }
    const href = `/api/audit/rea/${encodeURIComponent(dir)}/${encodeURIComponent(filename)}`;
    return `<li><a href="${esc(href)}" target="_blank" rel="noopener">${esc(ref.operation || filename)}</a>`
      + ` <span class="at-mono dim">${esc(String(ref.evidence_id || "").slice(0, 18))}…</span></li>`;
  }).join("");
  return `<section class="at-rea"><h2>REA 证据</h2><ul class="at-rea-list">${rows}</ul></section>`;
}

export function renderUncovered(bundle) {
  const items = bundle.uncovered_surfaces || [];
  if (!items.length) return "";
  const rows = items.map((item) => `<li><strong>${esc(item.surface || "?")}</strong>`
    + ` <span class="dim">${esc(item.reason || "")}</span></li>`).join("");
  return `<section class="at-uncovered"><h2>未覆盖面</h2><ul>${rows}</ul></section>`;
}

export function renderAuthorization(bundle) {
  const auth = bundle.authorization || {};
  const target = bundle.target || {};
  const toolchain = bundle.toolchain || {};
  const rea = toolchain.rea || {};
  const rows = [
    ["operator", auth.operator || "—"],
    ["scope", auth.scope || "—"],
    ["granted_at", auth.granted_at || "—"],
  ];
  if (auth.time_window) rows.push(["time_window", auth.time_window]);
  const targetRows = [
    ["identity", target.identity || "—"],
    ["version", target.version || "—"],
    ["manifest_sha256", (target.manifest_sha256 || "").slice(0, 16) + "…"],
  ];
  if (target.artifact_sha256) {
    targetRows.push(["artifact_sha256", target.artifact_sha256.slice(0, 16) + "…"]);
  }
  if (target.profile) targetRows.push(["profile", target.profile]);
  const kv = (rows) => rows.map(([k, v]) =>
    `<div class="at-kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`
  ).join("");
  const reaRows = [];
  if (rea.version) reaRows.push(["REA version", rea.version]);
  if (rea.facts_schema) reaRows.push(["facts schema", rea.facts_schema]);
  if (toolchain.ruleset_version) reaRows.push(["ruleset", "v" + toolchain.ruleset_version]);
  if (toolchain.replayer) reaRows.push(["replayer", toolchain.replayer]);
  return `<section class="at-auth">
    <h2>授权与范围</h2>
    ${kv(rows)}
    <div class="at-auth-target">
      <h3>目标</h3>
      ${kv(targetRows)}
    </div>
    ${reaRows.length ? `<div class="at-auth-toolchain">
      <h3>工具链</h3>
      ${kv(reaRows)}
    </div>` : ""}
  </section>`;
}

export function renderDisposition(finding, dir) {
  const current = finding.operator_disposition || "";
  const options = ["", "accepted_risk", "false_positive_disputed", "wont_fix"]
    .map((value) => {
      const selected = value === current ? " selected" : "";
      return `<option value="${esc(value)}"${selected}>${esc(DISPOSITION_LABELS[value] || value)}</option>`;
    })
    .join("");
  return `<div class="at-disp" data-finding="${esc(finding.finding_id || "")}" data-dir="${esc(dir)}">
    <label class="at-disp-label">operator</label>
    <select class="at-disp-select" data-finding="${esc(finding.finding_id || "")}">
      ${options}
    </select>
  </div>`;
}

export function renderChain(chain) {
  if (!chain || typeof chain !== "object") return "";
  const events = chain.events || [];
  if (!events.length) return "";
  const status = chain.chain_intact
    ? `<span class="at-badge at-ok">🔗 链完整</span>${
        chain.verification === "link-only"
          ? ' <span class="at-badge at-dim">仅链接校验</span>' : ""}`
    : `<span class="at-badge at-bad">⚠ 链断裂</span>${
        chain.tamper ? ` <span class="at-mono dim">${esc(String(chain.tamper).slice(0, 120))}</span>` : ""}`;
  const rows = events.map((event) => {
    const detail = event.payload && typeof event.payload === "object"
      ? Object.entries(event.payload)
          .filter(([k]) => k !== "body_hash")
          .map(([k, v]) => `<span class="at-chain-k">${esc(k)}</span> ${esc(typeof v === "string" ? v.slice(0, 120) : JSON.stringify(v).slice(0, 120))}`)
          .join(" · ")
      : "";
    return `<div class="at-chain-ev">
      <span class="at-chain-seq">${esc(String(event.sequence))}</span>
      <span class="at-chain-type">${esc(event.type)}</span>
      <span class="at-chain-detail">${detail}</span>
      <span class="at-chain-hash at-mono dim">${esc(String(event.hash || "").slice(0, 12))}…</span>
    </div>`;
  }).join("");
  return `<section class="at-chain">
    <h2>Provenance chain ${status} <span class="dim at-mono">(${events.length} events)</span></h2>
    <div class="at-chain-list">${rows}</div>
  </section>`;
}

export function renderSpecGap(report) {
  if (!report || typeof report !== "object") return "";
  const judgments = report.judgments || [];
  if (!judgments.length) return "";
  const conforms = judgments.filter((j) => j.verdict === "conforms");
  const undetermined = judgments.filter((j) => j.verdict === "undetermined");
  const rows = judgments.map((j) => {
    const tone = j.verdict === "conforms" ? "ok" : "dim";
    const badge = `<span class="at-badge at-${tone}">${esc(j.verdict)}</span>`;
    const detail = j.verdict === "conforms"
      ? (j.basis ? `<span class="dim">${esc((j.basis.doc_refs || []).join(", "))}</span>` : "")
      : (j.missing_spec ? `<span class="dim">${esc(j.missing_spec.question || "")}</span>` : "");
    return `<div class="at-sg-row">
      <span class="at-mono">${esc(j.judgment_id)}</span>
      <span class="at-sg-subject">${esc(j.subject)}</span>
      ${badge}
      <span class="at-sg-detail">${detail}</span>
    </div>`;
  }).join("");
  return `<section class="at-sg">
    <h2>Spec-gap report
      <span class="at-badge at-ok">${conforms.length} conforms</span>
      ${undetermined.length ? `<span class="at-badge at-dim">${undetermined.length} undetermined</span>` : ""}
    </h2>
    <div class="at-sg-list">${rows}</div>
  </section>`;
}

export function renderRegression(reg) {
  if (!reg || typeof reg !== "object") return "";
  const has = (key) => Array.isArray(reg[key]) && reg[key].length > 0;
  if (!has("known") && !has("new") && !has("fixed") && !has("regressed")) return "";
  const badge = (label, tone) => `<span class="at-badge at-${tone}">${esc(label)}</span>`;
  const section = (key, label, tone) => {
    if (!has(key)) return "";
    const items = reg[key].map((f) =>
      `<span class="at-reg-item">${esc(f.category || "?")} · ${esc(f.root_cause_group || "?")}</span>`
    ).join(" ");
    return `<div class="at-reg-row"><span class="at-reg-label">${badge(label, tone)}</span> ${items}</div>`;
  };
  const prior = reg.prior_audit_id
    ? `<div class="at-kv"><span class="k">prior audit</span><span class="v">${esc(reg.prior_audit_id)}</span></div>`
    : "";
  return `<section class="at-reg">
    <h2>回归对比</h2>
    ${prior}
    ${section("known", "known", "dim")}
    ${section("new", "new", "warn")}
    ${section("fixed", "fixed", "ok")}
    ${section("regressed", "regressed", "bad")}
  </section>`;
}

export function renderBundle(bundle, records, options = {}) {
  const sum = summarizeBundle(bundle);
  const byId = new Map((records || []).map((r) => [r.poc_id, r]));
  const dir = options.dir || "";
  const findingRows = (bundle.findings || []).map((f) => {
    const ref = (f.poc_refs || [])[0];
    const rec = ref ? byId.get(ref) : null;
    // 证据缺失必须如实显示为"证据缺失",不能兜底成"已验证"
    // (原则 1:UI 不显示强于链上证据的状态)。
    const replay = (rec || {}).replay || {};
    const runs = replay.runs;
    const runsText = typeof runs === "string" ? runs : JSON.stringify(runs || {});
    const outcome = rec ? (replay.outcome || "finding") : "__evidence_missing__";
    return `<div class="at-finding" data-poc="${esc(ref || "")}">
      <div class="at-frow">
        <span class="at-mark at-mark-ok"></span>
        <span class="at-ftitle">${esc(f.finding_id)} · ${esc(f.root_cause_group)}</span>
        <span class="at-cat">${esc(f.category)}</span>
        <span class="at-sev">${esc(f.severity)}</span>
        ${outcomeBadge(outcome)}
        ${renderDisposition(f, dir)}
      </div>
      <div class="at-fdetail">
        <div class="at-kv"><span class="k">目标</span><span class="v">${esc(sum.target)}</span></div>
        <div class="at-kv"><span class="k">目标哈希</span><span class="v">${esc(sum.manifest)}</span></div>
        <div class="at-kv"><span class="k">oracle</span><span class="v">${esc((rec || {}).oracle ? rec.oracle.spec || "—" : "—")}</span></div>
        <div class="at-kv"><span class="k">负对照</span><span class="v">${esc((rec || {}).negative_control ? rec.negative_control.result || "—" : "—")}</span></div>
        <div class="at-caps">${capabilityChips(rec ? (rec.capability_manifest || {}).declared : undefined)}</div>
        <div class="at-kv"><span class="k">重放</span><span class="v">${
          esc(runsText)
        } · 阈值 ${esc(replay.threshold || "—")}</span></div>
      </div>
    </div>`;
  }).join("");

  const pendingRows = (records || [])
    .filter((r) => r.poc_id && (r.replay || {}).outcome !== "finding")
    .map((r) => {
      const outcome = (r.replay || {}).outcome;
      return `<div class="at-cand">
        <span class="at-mark at-mark-dim"></span>
        <span class="at-ftitle">${esc(r.poc_id)} · ${esc(r.rule_id)}</span>
        ${outcomeBadge(outcome)}
      </div>`;
    }).join("");

  const reaFacts = options.reaFacts || null;
  return `
  <div class="at-bhead">
    <h1>发现账本</h1>
    <span class="at-mono dim">${esc(sum.auditId)}</span>
    <span class="at-badge at-ok">🔗 证据集 ${esc(String(sum.evidence))} 条</span>
    ${sum.reaEvidence ? `<span class="at-badge at-dim">REA ${esc(String(sum.reaEvidence))} 条</span>` : ""}
  </div>
  <div class="at-bmeta2">
    <span class="at-mono">目标 ${esc(sum.target)}</span>
    <span class="at-mono dim">manifest ${esc(sum.manifest)}</span>
    ${sum.artifact !== "—" ? `<span class="at-mono dim">artifact ${esc(sum.artifact)}</span>` : ""}
    ${sum.reaVersion !== "—" ? `<span class="at-mono dim">REA v${esc(String(sum.reaVersion))}</span>` : ""}
    <span class="at-mono dim">规则集 v${esc(String(sum.ruleset))}</span>
  </div>
  ${renderAuthorization(bundle)}
  <div class="at-ledger">
    ${findingRows || '<div class="at-none dim">本包内没有晋升的发现</div>'}
    ${pendingRows}
  </div>
  ${renderReaEvidence(bundle, dir)}
  ${renderReaFacts(reaFacts)}
  ${renderUncovered(bundle)}
  ${options.chain ? renderChain(options.chain) : ""}
  ${options.specGap ? renderSpecGap(options.specGap) : ""}
  ${options.regression ? renderRegression(options.regression) : ""}
  <div class="at-note dim">UI 只显示证据支持的状态:徽章来自 poc-evidence 的 outcome,不来自任何人的声称。</div>`;
}

export function renderBundles(entries) {
  return (entries || []).map((e) => bundleItemHtml(e, false)).join("")
    || '<div class="at-none dim">还没有审计包——先运行 python -m flowaudit.cli audit &lt;target&gt;</div>';
}

export async function init({ fetchImpl = fetch, root = document, host = null,
                             onSelect = null } = {}) {
  // 统一壳传 host=#main 元素;host 未给时回退 root(document)。
  const scope = host || root;
  const side = scope.querySelector("#at-bundles");
  const main = scope.querySelector("#at-main");
  if (!side || !main) return;
  // 并发守卫:快速连点两个 bundle 时,只有最新一次点击允许写 main。
  let requestSeq = 0;
  let payload;
  try {
    const res = await fetchImpl("/api/audit/bundles");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch {
    side.innerHTML = '<div class="at-none dim">无法连接控制台 API</div>';
    return;
  }
  side.innerHTML = renderBundles(payload.bundles);
  side.querySelectorAll(".at-bitem").forEach((el) => {
    el.addEventListener("click", async () => {
      const seq = ++requestSeq;
      side.querySelectorAll(".at-bitem").forEach((b) => b.classList.remove("at-sel"));
      el.classList.add("at-sel");
      const dir = el.getAttribute("data-dir");
      // 壳侧挂钩:统一壳用它写 #/audit/<dir>;独立用法不传即无感。
      if (onSelect) { try { onSelect(dir); } catch { /* hash 写失败不阻断渲染 */ } }
      let bundle;
      try {
        const res = await fetchImpl(`/api/audit/bundle/${encodeURIComponent(dir)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        bundle = await res.json();
      } catch (err) {
        if (seq === requestSeq) {
          main.innerHTML = `<div class="at-none">审计包加载失败( ${
            esc(String((err && err.message) || err))
          } )——包目录可能已被移除或服务不可用。</div>`;
        }
        return;
      }
      if (seq !== requestSeq) return;
      const refs = bundle.poc_evidence_refs || [];
      // 证据读取失败的条目被丢弃而不是伪装成证据;对应 finding 会显示
      // "证据缺失"徽章(原则 1:UI 不显示强于链上证据的状态)。
      const records = (await Promise.all(refs.map(async (ref) => {
        try {
          const r = await fetchImpl(
            `/api/audit/evidence/${encodeURIComponent(dir)}/${encodeURIComponent(ref)}`);
          if (!r.ok) return null;
          const doc = await r.json();
          return doc && typeof doc === "object" && doc.poc_id ? doc : null;
        } catch {
          return null;
        }
      }))).filter(Boolean);
      if (seq !== requestSeq) return;
      const current_dispositions = {};
      for (const fd of bundle.findings || []) {
        current_dispositions[fd.finding_id] = fd.operator_disposition || "";
      }
      let reaFacts = null;
      const factsPath = (bundle.rea_facts_ref || {}).path;
      if (typeof factsPath === "string" && factsPath) {
        const filename = factsPath.split("/").pop();
        try {
          const response = await fetchImpl(
            `/api/audit/rea/${encodeURIComponent(dir)}/${encodeURIComponent(filename)}`,
          );
          if (response.ok) reaFacts = await response.json();
        } catch {
          reaFacts = null;
        }
      }
      let chain = null;
      try {
        const chainRes = await fetchImpl(`/api/audit/chain/${encodeURIComponent(dir)}`);
        if (chainRes.ok) chain = await chainRes.json();
      } catch { chain = null; }
      let specGap = null;
      try {
        const sgRes = await fetchImpl(`/api/audit/spec-gap/${encodeURIComponent(dir)}`);
        if (sgRes.ok) specGap = await sgRes.json();
      } catch { specGap = null; }
      let regression = null;
      try {
        const regRes = await fetchImpl(`/api/audit/regression/${encodeURIComponent(dir)}`);
        if (regRes.ok) regression = await regRes.json();
      } catch { regression = null; }
      if (seq !== requestSeq) return;
      main.innerHTML = renderBundle(bundle, records, { dir, reaFacts, chain, specGap, regression });
      main.querySelectorAll(".at-disp-select").forEach((select) => {
        select.addEventListener("change", async () => {
          const findingId = select.getAttribute("data-finding");
          const disposition = select.value;
          // 处置是操作员决定,必须给出可见结果:成功"已记录",失败回滚并显示原因。
          let status = select.parentElement.querySelector(".at-disp-status");
          if (!status) {
            status = document.createElement("span");
            status.className = "at-disp-status";
            select.parentElement.appendChild(status);
          }
          status.textContent = "记录中…";
          try {
            const res = await fetchImpl(
              `/api/audit/disposition/${encodeURIComponent(dir)}/${encodeURIComponent(findingId)}`,
              { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ disposition }) },
            );
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              throw new Error(err.error || `HTTP ${res.status}`);
            }
            current_dispositions[findingId] = disposition;
            status.textContent = "已记录";
            // 处置已落链:立即刷新链区段,操作员应该马上看到自己的事件进链,
            // 而不是等下次开包才发现。
            try {
              const chainRes = await fetchImpl(`/api/audit/chain/${encodeURIComponent(dir)}`);
              if (chainRes.ok) {
                const chainEl = main.querySelector(".at-chain");
                if (chainEl) chainEl.outerHTML = renderChain(await chainRes.json());
              }
            } catch { /* 链刷新失败不影响处置结果 */ }
          } catch (err) {
            select.value = current_dispositions[findingId] || "";
            status.textContent = `记录失败:${String((err && err.message) || err)}`;
          }
        });
      });
    });
  });
}

/* 自动启动守卫已随独立 audit.html 退役:统一壳由 app.js 的 renderAudit()
   在 surface 渲染后显式调用 init({ host, onSelect });纯函数导入无副作用,
   node --test 直接覆盖。 */
