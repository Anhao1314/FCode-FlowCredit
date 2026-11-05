"use strict";

/* 运行证据的纯展示层：事件、链、工具契约、工具结果与预算。
   轮次/计划的主界面已迁到 app/task-thread.js；这里保留的渲染器
   仍被统一线程（工具详情、预算明细）与证据面板复用。 */
export function createRunEvidence(deps) {
  const {
    esc, pct, pp, num, shortHash, doodleSvg, lastBudget,
    EVENT_DOT, HUMAN_EVENTS, REVIEW_LABEL, ICON, FAILURE_REASONS, evExpanded,
    getRailFilter, getRailTab, getToolContracts,
  } = deps;
  const MACHINE_EVENTS = new Set([
    "model_started", "plan_validated", "plan_updated", "execution_started",
    "policy_requested", "policy_decided", "auto_action", "execution_resumed", "tool_started",
  ]);
  const TOOL_DOODLE_ICON = {
    list_documents: "list", read_document: "doc", normalize_units: "table",
    reconcile_basis: "link", check_restatement: "refresh-cw", calculate_margin: "gauge",
    report_blocked: "flag", ingest_file: "upload", search_corpus: "search",
    read_slice: "file-text", working_paper_append: "pen", fetch_source: "globe",
    search_cases: "search", read_case: "file-text", query_facts: "database",
    ls_workspace: "layout-grid", read_file: "book-open", write_file: "report",
    edit_file: "tool", run_command: "gate", fetch_url: "link",
  };

  function setTabCount(name, value) {
    const el = document.querySelector(`[data-count="${name}"]`);
    if (el) el.textContent = value === "" ? "" : String(value);
  }

  function railEmptyHtml() {
    return `<div class="rail-empty-doc">${doodleSvg("file-text")}<div class="t">案卷尚未展开</div><div class="dim">从左侧选择一条委托，或运行一个新的任务</div></div>`;
  }

  function eventItemHtml(e) {
    const human = HUMAN_EVENTS.has(e.type);
    const detail = summariseEvent(e);
    const mark = human ? `<span class="ev-human">人</span>` : `<span class="ev-mark ${EVENT_DOT[e.type] || ""}"></span>`;
    return `<div class="ev-item${human ? " human" : ""}"><div class="ev-line">${mark}<span class="ev-type">${esc(e.type)}</span><span class="grow"></span><span class="ev-ref" data-copy="${esc(e.hash || "")}" title="点击复制完整哈希">#${e.sequence} · ${esc(shortHash(e.hash))}</span></div>${detail ? `<div class="ev-detail">${detail}</div>` : ""}</div>`;
  }

  function renderEvents(trace) {
    const all = trace.events || [];
    const byRound = new Map();
    for (const event of all) {
      const round = Number((event.payload || {}).detail?.round) || 0;
      if (!byRound.has(round)) byRound.set(round, []);
      byRound.get(round).push(event);
    }
    const railFilter = getRailFilter();
    const chips = [["key", "关键"], ["all", "全部"], ["human", "人工"]]
      .map(([key, label]) => `<button class="rf${railFilter === key ? " active" : ""}" data-rail-filter="${key}">${label}</button>`).join("");
    let html = `<div class="rail-filters">${chips}</div>`;
    const rounds = [...byRound.keys()].sort((a, b) => a - b);
    for (const round of rounds) {
      const events = byRound.get(round);
      const human = events.filter(e => HUMAN_EVENTS.has(e.type));
      const machines = events.filter(e => MACHINE_EVENTS.has(e.type));
      const shown = railFilter === "human" ? human : events.filter(e => railFilter === "all" || !MACHINE_EVENTS.has(e.type));
      const expandable = railFilter === "key" && machines.length > 0;
      const expanded = evExpanded.has(round);
      if (!shown.length && !expandable) continue;
      html += `<div class="ev-round">${round === 0 ? "任务" : "第 " + round + " 轮"}</div>`;
      if (expanded) {
        html += shown.concat(machines).sort((a, b) => a.sequence - b.sequence).map(eventItemHtml).join("");
        html += `<button class="ev-more" data-ev-group="${round}">收起过程 · ${machines.length} 条</button>`;
      } else {
        html += shown.map(eventItemHtml).join("");
        if (expandable) html += `<button class="ev-more" data-ev-group="${round}">过程 ${machines.length} 条 ▸</button>`;
      }
    }
    if (!html.includes("ev-item")) html += `<div class="rail-empty">${railFilter === "human" ? "这条轨迹里还没有人工决定" : "没有可显示的事件"}</div>`;
    return html;
  }

  function renderChain(trace, check) {
    const events = trace.events || [];
    const chain = trace.chain || {};
    const ok = !!(check && check.ok);
    const shield = doodleSvg("shield-check", "draw");
    const head = chain.head_hash || "";
    return `<div class="seal ${ok ? "ok" : "bad"}"><div class="seal-head">${shield}<span>${check && check.live ? "签名中" : ok ? "全链验证通过" : "链验证失败"}</span><span class="grow"></span><span class="seal-count">${events.length} 事件</span></div>${!ok && check && check.reason ? `<div class="seal-reason">${esc(check.reason)}</div>` : ""}<div class="seal-row"><span class="k">链头</span><span class="seal-hash" data-copy="${esc(head)}" title="点击复制">${esc(head)}</span></div><div class="seal-row"><span class="k">算法</span><span>${esc(chain.algorithm || "sha256 · canonical json")}</span></div></div><div class="rail-sec-title">链接明细</div><div class="chain-list">${events.map(e => `<div class="link-row"><span class="n">#${e.sequence}</span><span class="h">${esc(shortHash(e.hash))}</span><span class="arrow">←</span><span class="p">${esc(shortHash(e.prev_hash))}${e.prev_hash ? "" : " (genesis)"}</span></div>`).join("")}</div>`;
  }

  function renderRaw(trace) {
    const lines = JSON.stringify(trace, null, 2).split("\n").map((line, index) => `<span class="raw-line"><i>${index + 1}</i>${esc(line)}</span>`).join("");
    return `<div class="raw-head"><button class="rf" data-copy-raw>复制原文</button><button class="rf" data-download-raw>下载 trace</button></div><pre class="raw-pre">${lines}</pre>`;
  }

  function renderRail(trace, check) {
    const body = document.getElementById("rail-body");
    if (!trace) { body.innerHTML = railEmptyHtml(); return; }
    const railTab = getRailTab();
    for (const button of document.querySelectorAll("#rail-tabs button")) button.classList.toggle("active", button.dataset.tab === railTab);
    const events = trace.events || [];
    const count = document.getElementById("rail-count");
    if (count) count.textContent = events.length ? `${events.length} 事件` : "";
    setTabCount("events", events.length);
    const contracts = getToolContracts();
    setTabCount("tools", contracts ? contracts.length : "");
    setTabCount("chain", events.length);
    if (railTab === "events") { body.innerHTML = renderEvents(trace); return; }
    if (railTab === "tools") { body.innerHTML = renderTools(trace); return; }
    if (railTab === "chain") { body.innerHTML = renderChain(trace, check); return; }
    body.innerHTML = renderRaw(trace);
  }

  function renderTools(trace) {
    const task = trace.task || {};
    const allowed = new Set(task.allowed_tools || []);
    const granted = new Set((task.model_grant || {}).allowed_tools || []);
    const usage = new Map();
    const touch = name => {
      if (!usage.has(name)) usage.set(name, { calls: 0, denied: 0, tokens: 0, cost: 0, latency: 0 });
      return usage.get(name);
    };
    for (const round of trace.rounds || []) {
      const denied = (round.decision || {}).allowed === false;
      const tool = (round.observation || {}).tool || (round.decision || {}).tool || (((round.proposal || {}).steps || [])[0] || {}).tool;
      if (!tool) continue;
      const stat = touch(tool);
      stat.calls += 1;
      if (denied) stat.denied += 1;
      const receipt = round.receipt || {};
      if (receipt.tokens !== undefined) stat.tokens += Number(receipt.tokens) || 0;
      if (receipt.cost_microusd !== undefined) stat.cost += Number(receipt.cost_microusd) || 0;
      if (receipt.latency_ms !== undefined) stat.latency += Number(receipt.latency_ms) || 0;
    }
    const contracts = getToolContracts();
    if (!contracts) return `<div class="rail-empty">正在加载工具契约…</div>`;
    const activeRound = [...(trace.rounds || [])].reverse().find(round => !round.observation && (round.decision || {}).allowed !== false);
    const activeTool = activeRound ? (((activeRound.proposal || {}).steps || [])[0] || {}).tool || (activeRound.decision || {}).tool : null;
    const rows = contracts.slice().sort((a, b) => {
      const ua = usage.get(a.name), ub = usage.get(b.name);
      if ((ub ? ub.calls : 0) !== (ua ? ua.calls : 0)) return (ub ? ub.calls : 0) - (ua ? ua.calls : 0);
      if (allowed.has(b.name) !== allowed.has(a.name)) return allowed.has(b.name) ? 1 : -1;
      return a.name.localeCompare(b.name);
    }).map(contract => {
      const stat = usage.get(contract.name);
      const inScope = allowed.has(contract.name);
      const inGrant = granted.has(contract.name);
      const effect = contract.effect || "read";
      const usageText = stat ? `${stat.calls} 次${stat.denied ? " · 拒绝 " + stat.denied : ""}${stat.tokens ? " · " + num(stat.tokens) + " tokens" : ""}${stat.cost ? " · " + num(stat.cost) + " µUSD" : ""}${stat.latency ? " · " + num(stat.latency) + " ms" : ""}` : "未使用";
      const iconName = TOOL_DOODLE_ICON[contract.name];
      const icon = iconName ? `<span class="tr-ic">${doodleSvg(iconName, contract.name === activeTool)}</span>` : "";
      return `<div class="tool-row${inScope ? "" : " out-of-scope"}" title="${esc(contract.summary || "")}"><div class="tr-head">${icon}<span class="tr-name">${esc(contract.name)}</span><span class="eff eff-${esc(effect)}">${esc(effect)}</span><span class="tr-ver">v${esc(contract.version || "?")}</span><span class="grow"></span><span class="tr-scope">${inScope ? (inGrant ? "允许 · 已授权" : "允许 · 未授权") : "不在任务范围"}</span></div><div class="tr-usage">${esc(usageText)}</div></div>`;
    }).join("");
    return `<div class="rail-tools"><span class="rf-hint">${allowed.size} 个允许 · ${granted.size} 个授权 · 契约来自注册表</span></div>${rows}`;
  }

  function summariseEvent(e) {
    if (e.type === "review_decided") {
      const detail = (e.payload && e.payload.detail) || (e.payload || {});
      const label = REVIEW_LABEL[detail.decision] || detail.decision || "判断记录";
      return detail.reason ? `${esc(label)} · ${esc(detail.reason)}` : esc(label);
    }
    const d = e.payload && e.payload.detail;
    if (!d || Object.keys(d).length === 0) return "";
    if (e.type === "tool_started" || e.type === "tool_succeeded") return `${esc(d.tool || "")} ${d.arguments && Object.keys(d.arguments).length ? "(" + Object.entries(d.arguments).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(", ") + ")" : "()"}`;
    if (e.type === "model_started") return `round ${esc(d.round)}`;
    if (e.type === "policy_requested") return `${esc((d.packet && d.packet.candidates || []).length)} 个候选 · packet ${esc(shortHash(d.packet_hash))}…`;
    if (e.type === "policy_decided") return `选择 ${esc(d.candidate_id || "—")} · packet ${esc(shortHash(d.packet_hash))}…`;
    if (e.type === "auto_action") return `${esc((d.step || {}).tool || "")} · 无需模型调用`;
    if (e.type === "memory_recalled") return `参考 ${esc(d.count || (d.hits || []).length)} 则旧案 · ${esc(((d.hits || [])[0] || {}).run_id || "")}`;
    if (e.type === "model_unknown") return esc(d.cause || "");
    if (e.type === "model_reconciled") return `receipt ${esc(shortHash(d.receipt_ref))}…`;
    if (e.type === "plan_updated") { const p = d.plan || {}; const done = (p.todos || []).filter(t => t.status === "done").length; return `revision ${esc(p.revision)} · ${done}/${(p.todos || []).length} settled`; }
    if (e.type === "plan_validated") return d.plan && d.plan.steps ? esc(d.plan.steps.map(s => s.id + ":" + s.tool).join(" ")) : "";
    if (e.type === "approval_requested") {
      const args = d.arguments && Object.keys(d.arguments).length ? "(" + Object.entries(d.arguments).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(", ") + ")" : "";
      return `${esc(d.tool)} ${args} · ${esc(d.policy)}`;
    }
    if (e.type === "approval_granted") return `${esc(d.tool)} · ${esc(d.by || "operator")}${d.remember ? " · 本运行免询" : ""}`;
    if (e.type === "approval_denied") return `${esc(d.tool)} · ${esc(d.code || "OPERATOR_DENIED")}`;
    if (e.type === "approval_aborted") return `${esc(d.tool)} · ${esc(d.code || "ABORTED")}`;
    if (e.type === "plan_steered") {
      const p = d.plan || {};
      const what = Array.isArray(d.added) && d.added.length ? `${esc(d.action)} ${esc(d.added.join(", "))}` : `${esc(d.action)} ${esc(d.todo_id || "")}`.trim();
      const note = [d.by === "model" ? "模型提议" : "", d.note || ""].filter(Boolean).map(esc).join(" · ");
      return `revision ${esc(p.revision)} · ${what}${note ? " · " + note : ""}`;
    }
    if (e.type === "budget_adjusted") {
      const before = d.before || {}, after = d.after || {};
      return `calls ${esc(before.max_calls)}→${esc(after.max_calls)} · µUSD ${esc(before.max_cost_microusd)}→${esc(after.max_cost_microusd)}`;
    }
    if (e.type === "control_rejected") return `${esc(d.kind)} · ${esc(d.reason)}`;
    if (e.type === "task_resumed") return `by ${esc(d.by || "operator")}${d.limits ? " · calls " + esc(d.limits.max_calls) : ""}`;
    if (e.type === "operator_message") return `第 ${esc(d.round)} 轮前 · ${esc(d.text || "")}`;
    return esc(JSON.stringify(d).slice(0, 120));
  }

  function tokenSplit(trace) {
    let prompt = 0, cached = 0, total = 0, known = false;
    for (const r of (trace.rounds || [])) {
      const receipt = r.receipt || {};
      const meta = receipt.metadata || {};
      total += Number(receipt.tokens || 0);
      if (meta.usage_prompt_tokens !== undefined && meta.usage_prompt_tokens !== null) {
        known = true;
        prompt += Number(meta.usage_prompt_tokens) || 0;
        cached += Number(meta.cached_tokens) || 0;
      }
    }
    return { prompt, cached, total, completion: known ? Math.max(0, total - prompt) : null, known };
  }

  function tokenSplitHtml(trace) {
    const t = tokenSplit(trace);
    if (!t.total) return "";
    if (!t.known) return `<div class="tok-split unknown"><span class="tok-note">tokens ${num(t.total)} · 当前收据未含 token 分类（offline-mock 不产生 usage_prompt_tokens）</span></div>`;
    return `<div class="tok-split"><div class="tok-row"><span class="k">prompt</span><span class="v">${num(t.prompt)}</span></div><div class="tok-row"><span class="k">cached</span><span class="v">${num(t.cached)}</span><span class="note">缓存命中按输入价计费（v1）</span></div><div class="tok-row"><span class="k">completion</span><span class="v">${num(t.completion)}</span></div></div>`;
  }

  function activeRoundDelta(trace) {
    const b = lastBudget(trace);
    if (!b) return null;
    const rounds = trace.rounds || [];
    let settled = 0;
    for (let i = rounds.length - 1; i >= 0; i--) {
      const r = rounds[i];
      if (r.receipt || (r.decision && r.decision.allowed === false)) {
        settled = Number((r.budget_after || {}).tokens_used ?? 0);
        break;
      }
      if (i === 0) settled = Number((r.budget_before || {}).tokens_used ?? 0);
    }
    const delta = Number(b.tokens_used || 0) - settled;
    return delta > 0 ? delta : null;
  }

  function renderMeters(trace, opts = {}) {
    const b = lastBudget(trace);
    if (!b) return "";
    const lim = b.limits || {};
    const editable = !!opts.editable;
    const editing = editable && !!opts.editing;
    const active = opts.live ? activeRoundDelta(trace) : null;
    const meters = [["calls", b.calls_used, lim.max_calls, ""], ["reads", b.reads_used, lim.max_reads, ""], ["µUSD", b.cost_used_microusd, lim.max_cost_microusd, ""], ["tokens", b.tokens_used, lim.max_tokens, "m-tokens"]];
    return `<div class="card meters${editing ? " editing" : ""}"><div class="card-head"><span class="card-title">${ICON.lock} 预算</span>${opts.queued ? `<span class="m-queued">已排队 · 下一轮生效</span>` : ""}<span class="grow"></span>${meters.map(([label, used, max, cls]) => {
      const ratio = max ? Math.min(1, (used || 0) / max) : 0;
      const hot = ratio >= .95 ? " over" : ratio >= .7 ? " hot" : "";
      const isActive = active !== null && label === "tokens";
      return `<span class="meter${hot}${cls ? " " + cls : ""}"><span class="m-k">${label}</span><span class="m-bar"><i style="width:${Math.round(ratio * 100)}%"></i></span><span class="m-v">${num(used || 0)}/${num(max || 0)}</span>${isActive ? `<span class="m-active">本轮 +${num(active)}</span>` : ""}</span>`;
    }).join("")}${editable ? `<button class="p-edit${editing ? " active" : ""}" data-budget-edit>${editing ? "取消" : "调整"}</button>` : ""}</div>${tokenSplitHtml(trace)}${editing ? budgetForm(lim) : ""}</div>`;
  }

  function budgetForm(lim) {
    return `<div class="budget-edit"><label>calls <input type="number" data-budget="maxCalls" min="${lim.max_calls}" value="${lim.max_calls}" inputmode="numeric"></label><label>reads <input type="number" data-budget="maxReads" min="${lim.max_reads}" value="${lim.max_reads}" inputmode="numeric"></label><label>µUSD <input type="number" data-budget="maxCostMicrousd" min="${lim.max_cost_microusd}" value="${lim.max_cost_microusd}" inputmode="numeric"></label><span class="hint">只允许调高 · 下一轮生效</span><span class="grow"></span><button class="a-btn" data-budget-cancel>取消</button><button class="a-btn strong" data-budget-apply>提交调整</button></div>`;
  }

  function kvDetail(rows) {
    const body = rows.filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`).join("");
    return body ? `<div class="t-detail"><div class="kv">${body}</div></div>` : "";
  }

  function alertDetail(kind, title, sub) {
    return `<div class="t-detail"><div class="alert ${kind}"><div class="a-title">${esc(title)}</div>${sub ? `<div class="a-sub">${esc(sub)}</div>` : ""}</div></div>`;
  }

  const TOOL_RENDERERS = {
    list_documents(obs) {
      const docs = (obs.result || {}).documents || [];
      if (!docs.length) return "";
      return `<div class="t-detail">${docs.map(d => `<div class="doc-row"><span class="id">${esc(d.id)}</span><span class="meta2">${esc(d.kind || "")} · ${esc(d.period || "")}</span><span class="grow"></span><span class="meta2">${esc(d.unit_label || d.unit || "")} · ${esc(d.basis || "")}</span></div>`).join("")}</div>`;
    },
    read_document(obs) {
      const r = obs.result || {};
      const body = String(r.body || "");
      return `<div class="t-detail"><div class="doc-row"><span class="id">${esc(r.id || "")}</span><span class="meta2">${esc(r.title || "")}</span></div>${body ? `<pre class="doc-body">${esc(body)}</pre>` : ""}</div>`;
    },
    normalize_units(obs) {
      const r = obs.result || {};
      return kvDetail([["换算", `${r.source_unit} → ${r.target_unit}`], ["系数", `×${num(r.factor)}`], ["毛利（元）", num(r.gross_profit_cny)]]);
    },
    reconcile_basis(obs) {
      const r = obs.result || {};
      if (r.reconciled === false) return alertDetail("warn", "无法桥接口径", r.reason || "没有公布的桥接说明");
      return kvDetail([["期间", r.period], ["口径", `${r.from_basis} → ${r.to_basis}`], ["差异", `${r.delta >= 0 ? "+" : ""}${r.delta}`], ["说明", r.reason || ""]]);
    },
    check_restatement(obs) {
      const r = obs.result || {};
      if (!r.restated) return kvDetail([["重述", "无重述记录"]]);
      return kvDetail([["原值 → 重述", `${num(r.original_gross_profit)} → ${num(r.restated_gross_profit)}`], ["差异", `${r.delta >= 0 ? "+" : ""}${r.delta}`], ["说明", r.reason || ""]]);
    },
    calculate_margin(obs) {
      const r = obs.result || {};
      if (!r.computed) return alertDetail("bad", "无法计算", (r.blockers || []).join("；"));
      return `<div class="t-detail"><div class="t-metrics"><div><div class="k">FY2024</div><div class="v">${pct(r.margin_bp_2024)}</div></div><div><div class="k">FY2025</div><div class="v">${pct(r.margin_bp_2025)}</div></div><div><div class="k">变化</div><div class="v ${r.change_bp >= 0 ? "up" : "down"}">${pp(r.change_bp)}</div></div></div></div>`;
    },
    report_blocked(obs) { return alertDetail("bad", "声明受阻", (obs.result || {}).reason || ""); },
    ls_workspace(obs) {
      const entries = (obs.result || {}).entries || [];
      if (!entries.length) return `<div class="t-detail"><div class="doc-row"><span class="id">（空工作区）</span></div></div>`;
      return `<div class="t-detail">${entries.map(entry => `<div class="doc-row"><span class="id">${esc(entry.path)}</span><span class="grow"></span><span class="meta2">${num(entry.bytes)} B</span></div>`).join("")}</div>`;
    },
    read_file(obs) {
      const r = obs.result || {};
      const content = String(r.content || "");
      return `<div class="t-detail"><div class="doc-row"><span class="id">${esc(r.path || "")}</span><span class="grow"></span><span class="meta2">${num(r.bytes)} B · sha ${esc(shortHash(r.sha256))}…</span></div>${content ? `<pre class="doc-body">${esc(content.slice(0, 4000))}</pre>` : ""}</div>`;
    },
    write_file(obs) {
      const r = obs.result || {};
      return kvDetail([["文件", r.path], ["状态", r.changed ? "已写入" : "内容未变化"], ["大小", `${num(r.bytes)} B`], ["写入哈希", `${shortHash(r.sha256_after)}…`], ["git commit", r.commit || "—"]]);
    },
    edit_file(obs) {
      const r = obs.result || {};
      return kvDetail([["文件", r.path], ["状态", r.changed ? "唯一锚点已替换" : "内容未变化"], ["大小", `${num(r.bytes)} B`], ["写入哈希", `${shortHash(r.sha256_after)}…`], ["git commit", r.commit || "—"]]);
    },
    run_command(obs) {
      const r = obs.result || {};
      return `${kvDetail([["命令", r.command], ["退出码", r.exit_code], ["耗时", `${num(r.duration_ms)} ms`]])}<div class="t-detail"><pre class="doc-body">${esc(String(r.output || "(no output)").slice(0, 4000))}</pre></div>`;
    },
    fetch_url(obs) {
      const r = obs.result || {};
      return `<div class="t-detail"><div class="doc-row"><span class="id">${esc(r.url || "")}</span><span class="grow"></span><span class="meta2">HTTP ${esc(r.status)} · ${num(r.bytes)} B</span></div><div class="doc-row"><span class="meta2">${esc(r.title || "")} · content_truth ${esc(r.content_truth || "not-verified")}</span></div>${r.text ? `<pre class="doc-body">${esc(String(r.text).slice(0, 2400))}</pre>` : ""}</div>`;
    },
  };

  function genericToolDetail(obs) {
    const r = obs.result || {};
    if (!r || typeof r !== "object" || Object.keys(r).length === 0) return "";
    const flat = [], nested = [];
    for (const [k, v] of Object.entries(r)) {
      if (v === null || v === undefined || v === "") continue;
      if (typeof v === "object") nested.push([k, v]); else flat.push([k, String(v)]);
    }
    const kv = kvDetail(flat);
    const extra = nested.length ? `<details class="raw"><summary>嵌套字段</summary><pre>${esc(JSON.stringify(Object.fromEntries(nested), null, 2))}</pre></details>` : "";
    if (!kv && !extra) return `<div class="t-detail"><pre class="doc-body">${esc(JSON.stringify(r, null, 2))}</pre></div>`;
    return (kv || "") + extra;
  }

  function renderToolDetail(obs, kind = "brief") {
    if (kind === "diff") return writeDiffHtml(obs);
    if (kind === "removed") return removedResultHtml(obs);
    if (kind === "qa") return askAnswerHtml(obs);
    return (TOOL_RENDERERS[obs.tool] || genericToolDetail)(obs);
  }

  function askAnswerHtml(obs) {
    const result = obs.result || {};
    const question = String(result.asked || (obs.arguments || {}).question || "");
    const why = String((obs.arguments || {}).why || "");
    const answered = !!result.answered;
    const answer = answered ? String(result.answer || "") : "";
    const note = answered ? "" : `<div class="qa-note">${result.timed_out ? "等待超时" : "操作员未回答"} · ${esc(result.reason || "模型据此自行判断")}</div>`;
    return `<div class="qa-block"><div class="qa-line q"><span class="qa-who">问操作员</span><span class="qa-text">${esc(question)}</span></div>${why ? `<div class="qa-why">${esc(why)}</div>` : ""}${answered ? `<div class="qa-line a"><span class="qa-who">回答</span><span class="qa-text">${esc(answer)}</span></div>` : note}</div>`;
  }

  const RESULT_BLOCK_KINDS = {
    list_documents: "brief", read_document: "brief", check_restatement: "brief", reconcile_basis: "brief",
    normalize_units: "brief", calculate_margin: "brief", working_paper_append: "diff", collect_source: "diff",
    write_document: "diff", report_blocked: "removed", ls_workspace: "brief", read_file: "brief",
    write_file: "diff", edit_file: "diff", run_command: "brief", fetch_url: "brief", ask_operator: "qa",
  };

  function resultBlockKind(round) {
    if (round.decision && round.decision.allowed === false) return "removed";
    const obs = round.observation || {};
    const step = ((round.proposal || {}).steps || [])[0] || {};
    const tool = obs.tool || (round.decision || {}).tool || step.tool || "";
    return RESULT_BLOCK_KINDS[tool] || "brief";
  }

  function writeDiffHtml(obs) {
    const args = obs.arguments || {}, result = obs.result || {};
    const target = ["target", "doc_id", "document_id", "section", "path"].map(k => args[k]).find(v => v !== undefined && v !== null && v !== "") || result.target || result.doc_id || "(目标未声明)";
    const added = result.content || result.appended || result.summary || result.text || "";
    const rest = Object.entries(result).filter(([k, v]) => !["content", "appended", "summary", "text", "target", "doc_id"].includes(k) && v !== null && v !== undefined && v !== "");
    return `<div class="t-detail block-diff"><div class="diff-head">目标 · <code>${esc(String(target))}</code></div>${added ? `<div class="diff-line add"><span class="sign">+</span><span class="body">${esc(String(added))}</span></div>` : ""}${kvDetail(rest)}</div>`;
  }

  function removedResultHtml(obs) {
    const r = obs.result || {};
    const reason = r.reason || r.message || r.blocked || "";
    return `<div class="t-detail block-removed"><div class="removed-line">该调用未产生可采信证据 · 结果弱化呈现</div>${reason ? `<div class="removed-why">${esc(String(reason))}</div>` : ""}</div>`;
  }

  function resultConclusion(result) {
    if (!result || result.change_bp === undefined) return "";
    if (result.comparable === false) return "两期数据口径不可比，不能给出变化结论。";
    const chg = result.change_bp;
    const direction = chg > 0 ? "上升" : chg < 0 ? "下降" : "持平";
    const amount = chg === 0 ? "" : ` ${(Math.abs(chg) / 100).toFixed(2)} 个百分点`;
    return `两期合并毛利率可比：FY2024 ${pct(result.margin_bp_2024)}，FY2025 ${pct(result.margin_bp_2025)}，${direction}${amount}。`;
  }

  function failureConclusion(failure) {
    const reason = FAILURE_REASONS[failure.code] || failure.message || "运行未能完成";
    return `未能给出结论：${reason}。`;
  }

  return {
    renderRail, renderMeters, tokenSplitHtml, renderToolDetail, resultBlockKind,
    resultConclusion, failureConclusion,
  };
}
