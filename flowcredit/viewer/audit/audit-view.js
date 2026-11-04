/* 审计台（发现账本）的纯渲染层 + 数据接线。

   四条 UI 原则在这里落地（与 mockup 一致）：
   1. UI 不显示强于链上证据的状态——徽章只看 poc-evidence 的 outcome；
   2. 证据优先于叙述——发现行展开即证据链，不写形容词；
   3. 执行者可见——actor 徽章（host/triage/operator）随事件渲染；
   4. 目标能力清单前置——每个发现展示声明过的能力边界。

   与 app.js 的关系：这是独立模块、独立页面（audit.html），不向 god file
   添加一行。纯函数（mapOutcome / groupFindings / summarizeBundle /
   renderLedger / renderBundle / renderEvidence）全部导出，node --test
   直接覆盖；init() 只做 fetch 与事件接线。 */

import { esc } from "../core/format.js";

const OUTCOME_MAP = {
  finding: { label: "已验证", tone: "ok" },
  not_reproduced: { label: "未复现(保留)", tone: "dim" },
  invalid_poc: { label: "证据无效", tone: "bad" },
  not_vulnerable_under_scope: { label: "范围内不成立", tone: "dim" },
};

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
  </section>`;
}

export function renderReaEvidence(bundle, dir = "") {
  const refs = bundle.rea_evidence_refs || [];
  if (!refs.length) return "";
  const rows = refs.map((ref) => {
    const filename = String(ref.path || "").split("/").pop();
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

export function renderBundle(bundle, records, options = {}) {
  const sum = summarizeBundle(bundle);
  const byId = new Map((records || []).map((r) => [r.poc_id, r]));
  const findingRows = (bundle.findings || []).map((f) => {
    const ref = (f.poc_refs || [])[0];
    const rec = byId.get(ref) || {};
    const outcome = (rec.replay || {}).outcome || "finding";
    return `<div class="at-finding" data-poc="${esc(ref || "")}">
      <div class="at-frow">
        <span class="at-mark at-mark-ok"></span>
        <span class="at-ftitle">${esc(f.finding_id)} · ${esc(f.root_cause_group)}</span>
        <span class="at-cat">${esc(f.category)}</span>
        <span class="at-sev">${esc(f.severity)}</span>
        ${outcomeBadge(outcome)}
      </div>
      <div class="at-fdetail">
        <div class="at-kv"><span class="k">目标</span><span class="v">${esc(sum.target)}</span></div>
        <div class="at-kv"><span class="k">目标哈希</span><span class="v">${esc(sum.manifest)}</span></div>
        <div class="at-kv"><span class="k">oracle</span><span class="v">${esc((rec.oracle || {}).spec || "—")}</span></div>
        <div class="at-kv"><span class="k">负对照</span><span class="v">${esc((rec.negative_control || {}).result || "—")}</span></div>
        <div class="at-caps">${capabilityChips((rec.capability_manifest || {}).declared)}</div>
        <div class="at-kv"><span class="k">重放</span><span class="v">${
          esc(JSON.stringify((rec.replay || {}).runs || {}))
        } · 阈值 ${esc((rec.replay || {}).threshold || "—")}</span></div>
      </div>
    </div>`;
  }).join("");

  const pendingRows = (records || [])
    .filter((r) => (r.replay || {}).outcome !== "finding")
    .map((r) => {
      const outcome = (r.replay || {}).outcome;
      return `<div class="at-cand">
        <span class="at-mark at-mark-dim"></span>
        <span class="at-ftitle">${esc(r.poc_id)} · ${esc(r.rule_id)}</span>
        ${outcomeBadge(outcome)}
      </div>`;
    }).join("");

  const reaFacts = options.reaFacts || null;
  const dir = options.dir || "";
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
  <div class="at-ledger">
    ${findingRows || '<div class="at-none dim">本包内没有晋升的发现</div>'}
    ${pendingRows}
  </div>
  ${renderReaEvidence(bundle, dir)}
  ${renderReaFacts(reaFacts)}
  ${renderUncovered(bundle)}
  <div class="at-note dim">UI 只显示证据支持的状态:徽章来自 poc-evidence 的 outcome,不来自任何人的声称。</div>`;
}

export function renderBundles(entries) {
  return (entries || []).map((e) => bundleItemHtml(e, false)).join("")
    || '<div class="at-none dim">还没有审计包——先运行 python -m flowaudit.cli audit &lt;target&gt;</div>';
}

export async function init({ fetchImpl = fetch, root = document } = {}) {
  const side = root.getElementById("at-bundles");
  const main = root.getElementById("at-main");
  let res;
  try {
    res = await fetchImpl("/api/audit/bundles");
  } catch {
    side.innerHTML = '<div class="at-none dim">无法连接控制台 API</div>';
    return;
  }
  const payload = await res.json();
  side.innerHTML = renderBundles(payload.bundles);
  side.querySelectorAll(".at-bitem").forEach((el) => {
    el.addEventListener("click", async () => {
      side.querySelectorAll(".at-bitem").forEach((b) => b.classList.remove("at-sel"));
      el.classList.add("at-sel");
      const dir = el.getAttribute("data-dir");
      const bundle = await (await fetchImpl(`/api/audit/bundle/${encodeURIComponent(dir)}`)).json();
      const refs = bundle.poc_evidence_refs || [];
      const records = await Promise.all(refs.map(async (ref) => {
        const r = await fetchImpl(`/api/audit/evidence/${encodeURIComponent(dir)}/${encodeURIComponent(ref)}`);
        return r.json();
      }));
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
      main.innerHTML = renderBundle(bundle, records, { dir, reaFacts });
    });
  });
}

// 页面入口:仅当作为页面脚本加载时自动接线;node --test 导入纯函数时跳过。
if (typeof document !== "undefined" && document.getElementById("at-bundles")) {
  init();
}
