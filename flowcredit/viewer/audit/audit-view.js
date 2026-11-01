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
  return {
    auditId: bundle.audit_id || "—",
    target: target.identity || "—",
    manifest: (target.manifest_sha256 || "").slice(0, 16) + "…",
    findings: (bundle.findings || []).length,
    evidence: (bundle.poc_evidence_refs || []).length,
    ruleset: (bundle.toolchain || {}).ruleset_version || "—",
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
    + `${esc(String(entry.evidence ?? 0))} 证据</span></button>`;
}

export function renderBundle(bundle, records) {
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

  return `
  <div class="at-bhead">
    <h1>发现账本</h1>
    <span class="at-mono dim">${esc(sum.auditId)}</span>
    <span class="at-badge at-ok">🔗 证据集 ${esc(String(sum.evidence))} 条</span>
  </div>
  <div class="at-bmeta2">
    <span class="at-mono">目标 ${esc(sum.target)}</span>
    <span class="at-mono dim">manifest ${esc(sum.manifest)}</span>
    <span class="at-mono dim">规则集 v${esc(String(sum.ruleset))}</span>
  </div>
  <div class="at-ledger">
    ${findingRows || '<div class="at-none dim">本包内没有晋升的发现</div>'}
    ${pendingRows}
  </div>
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
      main.innerHTML = renderBundle(bundle, records);
    });
  });
}

// 页面入口:仅当作为页面脚本加载时自动接线;node --test 导入纯函数时跳过。
if (typeof document !== "undefined" && document.getElementById("at-bundles")) {
  init();
}
