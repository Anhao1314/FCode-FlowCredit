/* FCode 审计报告生成器。
 *
 * 输入一条带终态的 trace，输出一份自包含的 HTML 工作底稿：内联样式、
 * 零依赖、可离线打开、可直接打印。报告的定位是"结构化决策辅助文档"：
 * 证据明细全部来自哈希链事件，链头与验证结论写在完整性声明里，
 * 免责声明固定在页脚——这个项目讲信任，报告自己先诚实。
 *
 * 刻意不 import app.js 的任何东西：报告必须是独立产物，控制台重构
 * 不应影响已签发报告的格式。
 */
"use strict";

window.FCReport = (function () {

  /* ------------------------------------------------------------- helpers */

  const esc = value => String(value == null ? "" : value).replace(/[&<>"]/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
  const num = n => Number(n || 0).toLocaleString("en-US");
  const shortHash = h => String(h || "").slice(0, 16);
  const pct = bp => (bp / 100).toFixed(2) + "%";
  const pp = bp => (bp >= 0 ? "+" : "") + (bp / 100).toFixed(2) + " pp";

  const HUMAN_LABELS = {
    approval_requested: "审批请求",
    approval_granted: "审批批准",
    approval_denied: "审批拒绝",
    approval_aborted: "审批中止",
    plan_steered: "计划修订",
    budget_adjusted: "预算调整",
    control_rejected: "指令被拒",
    task_resumed: "续跑",
    operator_message: "操作员消息",
  };

  const TOOL_LABELS = {
    list_documents: "列出资料", read_document: "读取资料", normalize_units: "单位归一",
    reconcile_basis: "口径桥接", check_restatement: "重述核对", calculate_margin: "毛利率计算",
    report_blocked: "受阻声明",
    ls_workspace: "列出工作区", read_file: "读取文件", write_file: "写入文件",
    edit_file: "编辑文件", run_command: "执行命令", fetch_url: "抓取网页",
  };

  const REVIEW_LABELS = {
    accept: "接受判断",
    needs_evidence: "需要补材料",
    qualified: "保留意见",
    redo: "退回重做",
  };

  function flatRows(result) {
    const rows = [];
    for (const [k, v] of Object.entries(result || {})) {
      if (v === null || v === undefined || v === "") continue;
      rows.push(`<tr><td>${esc(k)}</td><td>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`);
    }
    return rows.join("");
  }

  function kvRows(rows) {
    return rows.filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  }

  /* ----------------------------------------------------------- sections */

  function conclusionSection(trace) {
    const terminal = trace.terminal || {};
    const result = terminal.result || {};
    const failure = terminal.failure || {};
    if (terminal.status === "succeeded" && result.change_bp !== undefined) {
      return `<h2>一、结论</h2>
      <p class="answer">${esc(resultConclusion(result))}</p>
      <table class="metrics">
        <tr><td>FY2024 毛利率</td><td class="v">${pct(result.margin_bp_2024)}</td></tr>
        <tr><td>FY2025 毛利率</td><td class="v">${pct(result.margin_bp_2025)}</td></tr>
        <tr><td>变化</td><td class="v">${pp(result.change_bp)}</td></tr>
      </table>
      <p class="cite">口径 ${esc(result.basis || "—")} · ${esc(result.consolidation || "—")} · ${result.restatement_applied ? "已应用 FY2024 重述" : "无重述"}</p>`;
    }
    if (terminal.status === "succeeded") {
      const artifacts = Array.isArray(result.results)
        ? result.results.filter(entry => entry && entry.path)
        : (result.path ? [result] : []);
      if (artifacts.length) {
        const truth = (artifacts.find(entry => entry.content_truth) || {}).content_truth || "runtime-verified-file-state";
        return `<h2>一、结论</h2>
        <p class="answer">通用委托已完成；${artifacts.length} 个文件通过 Runtime 文件状态复核。</p>
        <table class="metrics">
          ${artifacts.map(entry => `<tr><td>${esc(entry.path)}</td><td class="v">${num(entry.bytes)} B</td></tr>`).join("")}
        </table>
        <p class="cite">复核方式 ${esc(result.verification || "runtime-recomputed")} · 内容真实性 ${esc(truth)}</p>`;
      }
      return `<h2>一、结论</h2><p class="answer">任务已完成，结果通过 Runtime 独立复算校验。</p>`;
    }
    const reasons = {
      BUDGET_EXCEEDED: "预算已耗尽", ROUND_LIMIT: "达到轮次上限", OPERATOR_DENIED: "操作员拒绝了关键工具调用",
      INSUFFICIENT_EVIDENCE: "证据不足，Runtime 拒绝给出未经证明的数字", PLAN_BLOCKED: "计划被阻塞",
      PLAN_EXHAUSTED: "计划已没有可执行步骤", STOPPED_BY_OPERATOR: "操作员停止了运行",
    };
    return `<h2>一、结论</h2>
    <p class="answer">未能给出结论：${esc(reasons[failure.code] || failure.message || "运行未完成")}。</p>
    <p class="cite">失败码 ${esc(failure.code || "—")}${failure.message ? " · " + esc(failure.message) : ""}</p>`;
  }

  function resultConclusion(result) {
    if (result.comparable === false) return "两期数据口径不可比，不能给出变化结论。";
    const chg = result.change_bp;
    const direction = chg > 0 ? "上升" : chg < 0 ? "下降" : "持平";
    const amount = chg === 0 ? "" : ` ${(Math.abs(chg) / 100).toFixed(2)} 个百分点`;
    return `两期合并毛利率可比：FY2024 ${pct(result.margin_bp_2024)}，FY2025 ${pct(result.margin_bp_2025)}，${direction}${amount}。`;
  }

  function budgetSection(trace) {
    const rounds = trace.rounds || [];
    let budget = null;
    for (let i = rounds.length - 1; i >= 0; i--) {
      const b = rounds[i].budget_after;
      if (b && b.calls_used !== undefined) { budget = b; break; }
    }
    if (!budget) return "";
    const lim = budget.limits || {};
    return `<h2>二、成本账单</h2>
    <table>
      <tr><td>模型调用 calls</td><td>${num(budget.calls_used)} / ${num(lim.max_calls || 0)}</td></tr>
      <tr><td>资料读取 reads</td><td>${num(budget.reads_used)} / ${num(lim.max_reads || 0)}</td></tr>
      <tr><td>成本 µUSD</td><td>${num(budget.cost_used_microusd)} / ${num(lim.max_cost_microusd || 0)}</td></tr>
      <tr><td>tokens</td><td>${num(budget.tokens_used)} / ${num(lim.max_tokens || 0)}</td></tr>
    </table>
    <p class="cite">预算在每次调用发送前扣减；超时与未知结果同样计费，不重发。</p>`;
  }

  function roundRows(trace) {
    const rounds = trace.rounds || [];
    const events = trace.events || [];
    const humanByRound = new Map();
    for (const e of events) {
      if (!HUMAN_LABELS[e.type]) continue;
      const d = (e.payload || {}).detail || {};
      const round = Number(d.round) || 0;
      if (!humanByRound.has(round)) humanByRound.set(round, []);
      humanByRound.get(round).push(e);
    }
    const humanHead = round => (humanByRound.get(round) || []).map(e => {
      const d = (e.payload || {}).detail || {};
      let note = "";
      if (e.type === "operator_message") note = d.text || "";
      else if (e.type === "approval_granted" || e.type === "approval_denied" || e.type === "approval_requested") note = `${d.tool || ""} · ${d.by || ""}${d.remember ? " · 本运行免询" : ""}`;
      else if (e.type === "budget_adjusted") note = `calls ${(d.before || {}).max_calls}→${(d.after || {}).max_calls}`;
      else if (e.type === "plan_steered") {
        const added = Array.isArray(d.added) && d.added.length ? d.added.join(", ") : d.todo_id || "";
        note = `${d.action || ""} ${added}${d.by === "model" ? " · 模型提议" : ""}`.trim();
      }
      else if (e.type === "task_resumed") note = `by ${d.by || "operator"}`;
      else note = d.reason || d.code || "";
      return `<div class="human"><span class="htag">${esc(HUMAN_LABELS[e.type])}</span> ${esc(note)}</div>`;
    }).join("");

    return rounds.map(round => {
      const obs = round.observation || {};
      const decision = round.decision || {};
      const denied = decision.allowed === false;
      const step = (round.proposal && round.proposal.steps && round.proposal.steps[0]) || {};
      const tool = obs.tool || decision.tool || step.tool || "—";
      const args = obs.arguments || step.arguments || {};
      const argStr = Object.keys(args).length
        ? "(" + Object.entries(args).map(([k, v]) => `${k}=${v}`).join(", ") + ")" : "";
      const empty = !round.proposal && !round.decision && !round.observation && !round.receipt;
      const receipt = round.receipt || {};
      const receiptBits = [
        receipt.todo_id ? `todo ${receipt.todo_id}` : "",
        receipt.tokens !== undefined ? `tokens ${num(receipt.tokens)}` : "",
        receipt.cost_microusd !== undefined ? `${num(receipt.cost_microusd)} µUSD` : "",
        receipt.latency_ms !== undefined ? `${receipt.latency_ms} ms` : "",
      ].filter(Boolean).join(" · ");

      let verdict;
      if (denied) verdict = `<span class="badge bad">DENIED</span>`;
      else if (empty) verdict = `<span class="badge warn">TERMINATED</span>`;
      else if (!round.observation) verdict = `<span class="badge warn">UNKNOWN</span>`;
      else verdict = `<span class="badge ok">PASS</span>`;

      let preview = "";
      if (!empty && round.observation && round.observation.result) {
        preview = `<table class="kv">${flatRows(round.observation.result)}</table>`;
      } else if (denied) {
        preview = `<p class="deny">${esc(decision.code || "DENIED")} · ${esc(decision.reason || "")}</p>`;
      } else if (empty) {
        preview = `<p class="deny">任务在此终结——没有提议落进签名链。</p>`;
      }

      return `<div class="round">
        <div class="rhead"><span class="rno">#${esc(round.index)}</span>
          <span class="rtool">${esc(TOOL_LABELS[tool] || tool)} <span class="rargs">${esc(tool)}${esc(argStr)}</span></span>
          ${verdict}</div>
        ${preview}
        ${receiptBits ? `<p class="receipt">${esc(receiptBits)}</p>` : ""}
        ${humanHead(round.index)}
      </div>`;
    }).join("");
  }

  function processSection(trace) {
    return `<h2>三、执行过程与证据</h2>
    <p class="cite">共 ${esc((trace.rounds || []).length)} 轮。每轮的输出字段均来自签名链事件；人的决定以蓝色标签随轮记录。</p>
    ${roundRows(trace)}`;
  }

  function humanSection(trace) {
    const humans = (trace.events || []).filter(e => HUMAN_LABELS[e.type]);
    if (!humans.length) return `<h2>四、人工决定</h2><p class="cite">本次运行没有人工介入，全程按主机策略自动执行。</p>`;
    return `<h2>四、人工决定</h2><table>
      <tr><th>#</th><th>决定</th><th>说明</th></tr>
      ${humans.map(e => {
        const d = (e.payload || {}).detail || {};
        let note = "";
        if (e.type === "operator_message") note = d.text || "";
        else if (e.type === "approval_granted") note = `允许 ${d.tool || ""}${d.remember ? "（本运行免询）" : ""}`;
        else if (e.type === "approval_denied") note = `拒绝 ${d.tool || ""} · ${d.code || ""}`;
        else if (e.type === "approval_requested") note = `${d.tool || ""} 等待授权`;
        else if (e.type === "budget_adjusted") note = `calls ${(d.before || {}).max_calls}→${(d.after || {}).max_calls}`;
        else if (e.type === "plan_steered") {
          const added = Array.isArray(d.added) && d.added.length ? d.added.join(", ") : d.todo_id || "";
          note = `${d.action || ""} ${added}${d.by === "model" ? " · 模型提议" : ""}`.trim();
        }
        else note = d.reason || d.code || "";
        return `<tr><td>${esc(e.sequence)}</td><td>${esc(HUMAN_LABELS[e.type])}</td><td>${esc(note)}</td></tr>`;
      }).join("")}
    </table>`;
  }

  function latestReview(trace) {
    const events = trace.events || [];
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i];
      if (!event || event.type !== "review_decided") continue;
      return {
        event,
        detail: (event.payload && event.payload.detail) || (event.payload || {}),
      };
    }
    return null;
  }

  function reviewSection(trace) {
    const review = latestReview(trace);
    if (!review) return "";
    const { event, detail } = review;
    const label = REVIEW_LABELS[detail.decision] || detail.decision || "判断记录";
    const sequence = event.sequence === undefined || event.sequence === null ? "—" : event.sequence;
    const hash = event.hash || "—";
    return `<h2>五、判断记录</h2>
    <div class="review">
      <div class="rhead">
        <span class="rtag">${esc(label)}</span>
        <span class="rmeta">${esc(detail.reviewer || "reviewer")} · 事件 #${esc(sequence)}</span>
      </div>
      ${detail.reason ? `<p class="rreason">${esc(detail.reason)}</p>` : `<p class="rreason empty">未填写理由</p>`}
      <p class="rhash">hash ${esc(hash)}</p>
    </div>
    <p class="cite">判断以追加事件写入签名链，不改变运行终态；此处为最近一次判断。</p>`;
  }

  function chainSection(trace, check, meta, sectionNo) {
    const chain = trace.chain || {};
    const events = trace.events || [];
    const ok = !!(check && check.ok);
    return `<h2>${esc(sectionNo || "五")}、完整性声明</h2>
    <table>
      <tr><td>哈希算法</td><td>${esc(chain.algorithm || "sha256 · canonical json")}</td></tr>
      <tr><td>事件总数</td><td>${esc(events.length)}</td></tr>
      <tr><td>链头 hash</td><td class="mono">${esc(chain.head_hash || (check && check.head) || "—")}</td></tr>
      <tr><td>验证结论</td><td class="${ok ? "ok" : "bad"}">${ok
        ? `浏览器对全部 ${esc(events.length)} 个事件重算 SHA-256 链：通过`
        : `验证失败：${esc((check && check.reason) || "未知原因")}`}</td></tr>
      <tr><td>生成时间</td><td>${esc(meta.generatedAt || "")}</td></tr>
      <tr><td>运行标识</td><td class="mono">${esc(meta.runId || "—")}</td></tr>
    </table>
    <h3>事件附录</h3>
    <table class="appendix">
      <tr><th>#</th><th>类型</th><th>hash</th></tr>
      ${events.map(e => `<tr><td>${esc(e.sequence)}</td><td>${esc(e.type)}</td><td class="mono">${esc(shortHash(e.hash))}…</td></tr>`).join("")}
    </table>`;
  }

  /* ---------------------------------------------------------------- build */

  function build(trace, meta) {
    const task = trace.task || {};
    const binding = task.binding || {};
    const meta_ = meta || {};
    const title = meta_.title || task.objective || "FCode 工作底稿";
    const reviewHtml = reviewSection(trace);
    return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>${esc(title)} · FCode 工作底稿</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 40px 48px 64px; max-width: 860px; margin-inline: auto;
    font: 14px/1.7 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: #1c1c1c; background: #fff; }
  h1 { font-size: 21px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 30px 0 8px; padding-top: 14px; border-top: 2px solid #1c1c1c; }
  h3 { font-size: 13px; margin: 18px 0 6px; }
  .kicker { font-size: 11px; letter-spacing: .12em; color: #6b7280; text-transform: uppercase; margin-bottom: 18px; }
  .answer { font-size: 15px; font-weight: 600; margin: 8px 0; }
  .cite { font-size: 12px; color: #6b7280; margin: 6px 0; }
  .deny { font-size: 12.5px; color: #b91c1c; margin: 6px 0; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 8px 0; }
  td, th { text-align: left; padding: 5px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
  td:first-child { color: #6b7280; width: 38%; }
  .metrics td:first-child { width: 60%; }
  .metrics .v, td.v { font-weight: 700; font-variant-numeric: tabular-nums; }
  .mono { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; word-break: break-all; }
  .round { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 14px; margin: 10px 0; page-break-inside: avoid; }
  .rhead { display: flex; align-items: baseline; gap: 10px; }
  .rno { font-family: ui-monospace, monospace; font-size: 12px; color: #6b7280; }
  .rtool { font-weight: 600; flex: 1; }
  .rargs { font-weight: 400; color: #6b7280; font-size: 12px; font-family: ui-monospace, monospace; }
  .badge { font-size: 10.5px; font-weight: 700; padding: 1px 8px; border-radius: 999px; letter-spacing: .04em; }
  .badge.ok { background: #dcfce7; color: #166534; }
  .badge.bad { background: #fee2e2; color: #991b1b; }
  .badge.warn { background: #fef3c7; color: #92400e; }
  .kv td:first-child { width: 42%; }
  .receipt { font-size: 11.5px; color: #6b7280; margin: 6px 0 0; }
  .human { font-size: 12px; margin-top: 6px; color: #1e40af; }
  .htag { background: #dbeafe; color: #1e40af; border-radius: 4px; padding: 0 6px; font-size: 10.5px; font-weight: 600; }
  .review { padding: 10px 14px; border: 1px dashed #d1d5db; border-radius: 8px; margin: 8px 0; page-break-inside: avoid; }
  .review .rhead { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .review .rtag { border: 1px solid #1c1c1c; border-radius: 3px; padding: 0 7px; font-size: 10.5px; font-weight: 600; letter-spacing: .06em; }
  .review .rmeta { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: #6b7280; }
  .review .rreason { font-size: 13px; line-height: 1.65; margin: 7px 0 0; }
  .review .rreason.empty { color: #6b7280; }
  .review .rhash { font-family: ui-monospace, Menlo, monospace; font-size: 10.5px; color: #6b7280; margin: 6px 0 0; word-break: break-all; }
  .appendix td, .appendix th { padding: 3px 8px; font-size: 11px; }
  .footer { margin-top: 36px; padding-top: 12px; border-top: 1px solid #e5e7eb;
    font-size: 11px; color: #6b7280; line-height: 1.8; }
  @media print { body { padding: 24px; } .round { page-break-inside: avoid; } }
</style></head><body>
  <div class="kicker">FCode · 委托工作底稿</div>
  <h1>${esc(title)}</h1>
  <p class="cite">主体 ${esc(task.principal || "—")} · 委托 ${esc(task.id || "—")} · 场景 ${esc(binding.scenario || binding.corpus || "—")} · 授权 ${esc((task.model_grant || {}).grant_id || "—")} r${esc((task.model_grant || {}).revision || "")}</p>
  ${conclusionSection(trace)}
  ${budgetSection(trace)}
  ${processSection(trace)}
  ${humanSection(trace)}
  ${reviewHtml}
  ${chainSection(trace, meta_.check, meta_, reviewHtml ? "六" : "五")}
  <div class="footer">
    本文档由 FCode 运行时生成，是结构化决策辅助工作底稿，不构成法定审计意见、投资建议或信贷决策。
    证据明细与执行过程全部来自任务签名链；完整性声明中的链头哈希可用于独立验证本文档所引内容未被篡改——
    在控制台载入对应 trace JSON 即自动重算全链。金额、公司与资料如为合成演示数据，已在任务绑定中标注；抓取的网页内容如实标注 not-verified。
  </div>
</body></html>`;
  }

  return { build };
})();
