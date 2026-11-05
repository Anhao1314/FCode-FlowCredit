"use strict";

/* 判断记录视图与提交控制器。共享运行状态由 app.js 显式注入，避免第二套 store。 */
export function createReviewUI(deps) {
  const {
    esc, verifyChain, REVIEW_DECISIONS, REVIEW_DECISION, REVIEW_LABEL, REVIEW_COUNT_LABEL,
    store, cache, activeSession, currentRun, shownTrace, runTraceUrl, setChainBadge, toast,
  } = deps;
  const reviewStates = new Map();
  const reviewProfiles = new Map();
  const reviewTraceRefreshes = new Map();

  /* 判断只属于当前打开的运行；静态 trace 没有可写入的 run id。 */
  function reviewRunId() {
    const key = store.key || "";
    if (!key.startsWith("run:")) return null;
    const run = currentRun();
    if (run && run.id) return run.id;
    const s = activeSession();
    if (s && s.id && key === "run:" + s.id) return s.id;
    return null;
  }

  function reviewStateFor(runId) {
    if (!reviewStates.has(runId)) {
      reviewStates.set(runId, { decision: "", reason: "", sending: false, error: "", recorded: null, appending: false });
    }
    return reviewStates.get(runId);
  }

  function recordedReview(trace, runId) {
    const state = reviewStates.get(runId);
    if (state && state.appending) return null;
    if (state && state.recorded) return state.recorded;
    const events = (trace && trace.events) || [];
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i];
      if (!event || event.type !== "review_decided") continue;
      const detail = (event.payload && event.payload.detail) || (event.payload || {});
      return {
        decision: detail.decision || "",
        reason: detail.reason || "",
        reviewer: detail.reviewer || "reviewer",
        sequence: event.sequence !== undefined ? event.sequence : detail.sequence,
      };
    }
    return null;
  }

  function reviewReasonId(runId) {
    return "review-reason-" + String(runId).replace(/[^a-zA-Z0-9_-]/g, "-");
  }

  function reviewPanelElement(runId) {
    return [...document.querySelectorAll("[data-review-panel]")]
      .find(panel => panel.dataset.reviewRun === runId) || null;
  }

  function reviewProfileParams(trace) {
    const task = (trace && trace.task) || {};
    const binding = task.binding || {};
    const terminal = (trace && trace.terminal) || {};
    return {
      module: binding.module || "",
      template: binding.scenario || "",
      outcome: terminal.status || "",
    };
  }

  function reviewProfileKey(params) {
    return JSON.stringify([params.module, params.template, params.outcome]);
  }

  function truncateReviewText(value, max) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    const chars = Array.from(text);
    return chars.length > max ? chars.slice(0, max).join("") + "…" : text;
  }

  function latestRecentReview(recent) {
    let best = null;
    let bestKey = -Infinity;
    for (const item of recent) {
      if (!item) continue;
      const sequence = Number(item.sequence);
      const at = Date.parse(item.at);
      const key = Number.isFinite(sequence) ? sequence : (Number.isFinite(at) ? at : -Infinity);
      if (!best || key > bestKey) {
        best = item;
        bestKey = key;
      }
    }
    return best;
  }

  function reviewProfileHtml(data) {
    const counts = data.counts || {};
    const countText = REVIEW_DECISIONS
      .map(item => `${REVIEW_COUNT_LABEL[item.key]} <b>${Number(counts[item.key] || 0)}</b>`)
      .join(" · ");
    const suggested = data.suggested && REVIEW_DECISION[data.suggested.decision] ? data.suggested : null;
    const recent = Array.isArray(data.recent) ? data.recent : [];
    const latest = latestRecentReview(recent);
    let latestDecision = "";
    let latestReason = "";
    if (latest) {
      latestDecision = latest.decision || "";
      latestReason = latest.reason || "";
    } else if (suggested) {
      latestDecision = suggested.decision || "";
      latestReason = suggested.last_reason || "";
    }
    const label = REVIEW_LABEL[latestDecision] || latestDecision;
    const reasonText = truncateReviewText(latestReason, 72);
    const writable = !!reviewRunId();
    return `<div class="rp-head">
      <span class="rp-kicker">REVIEW PROFILE</span>
      <span class="rp-title">你的历史判断</span>
      <span class="rp-total">同类 <b>${Number(data.total) || 0}</b> 次</span>
    </div>
    <div class="rp-counts">${countText}</div>
    ${label ? `<div class="rp-last"><span class="rp-last-k">上次：</span>${esc(label)}${reasonText ? ` · <span class="rp-reason" title="${esc(latestReason)}">${esc(reasonText)}</span>` : ""}</div>` : ""}
    ${writable && suggested ? `<div class="rp-actions"><button type="button" class="review-apply" data-review-apply="${esc(suggested.decision)}" data-review-apply-reason="${esc(suggested.last_reason || "")}" title="只选中这个判断并填入上次理由，不会自动提交">采用上次判断</button></div>` : ""}`;
  }

  function paintReviewProfile(key, data) {
    const el = document.querySelector("[data-review-profile]");
    if (!el || el.dataset.reviewProfileKey !== key) return;
    if (!data || !(Number(data.total) > 0)) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.innerHTML = reviewProfileHtml(data);
    el.hidden = false;
  }

  function loadReviewProfile(trace, options = {}) {
    const el = document.querySelector("[data-review-profile]");
    if (!el) return;
    const params = reviewProfileParams(trace);
    if (!params.module && !params.template) {
      el.hidden = true;
      return;
    }
    const key = reviewProfileKey(params);
    el.dataset.reviewProfileKey = key;
    let entry = reviewProfiles.get(key);
    if (!entry) {
      entry = { data: null, promise: null, token: 0 };
      reviewProfiles.set(key, entry);
    }
    if (entry.data) paintReviewProfile(key, entry.data);
    if (entry.promise && !options.force) return;
    const token = ++entry.token;
    const query = new URLSearchParams();
    if (params.module) query.set("module", params.module);
    if (params.template) query.set("template", params.template);
    if (params.outcome) query.set("outcome", params.outcome);
    const promise = fetch(`/api/review-profile?${query.toString()}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then(response => (response.ok ? response.json() : null))
      .then(data => {
        if (entry.token !== token) return;
        entry.promise = null;
        if (!data || typeof data !== "object" || !(Number(data.total) > 0)) {
          entry.data = { total: 0, counts: {}, recent: [], suggested: null };
          paintReviewProfile(key, entry.data);
          return;
        }
        entry.data = data;
        paintReviewProfile(key, data);
      })
      .catch(() => {
        if (entry.token === token) entry.promise = null;
      });
    entry.promise = promise;
  }

  function reviewRecordedHtml(recorded) {
    const sequence = recorded.sequence === undefined || recorded.sequence === null ? "—" : recorded.sequence;
    const label = REVIEW_LABEL[recorded.decision] || recorded.decision || "判断记录";
    return `<div class="review-recorded" role="status">
      <span class="review-stamp">已记录</span>
      <span class="rr-sep">·</span>
      <span class="rr-line">${esc(label)} <span class="rr-sep">·</span> #${esc(sequence)}</span>
      <span class="rr-meta">${esc(recorded.reviewer || "reviewer")}</span>
      <button type="button" class="review-append" data-review-append title="在签名链末尾追加一次判断，不覆盖这次记录">追加判断</button>
      ${recorded.reason ? `<p class="rr-reason">${esc(recorded.reason)}</p>` : ""}
    </div>`;
  }

  function reviewPanelInnerHtml(trace, runId) {
    const recorded = recordedReview(trace, runId);
    if (recorded) return reviewRecordedHtml(recorded);
    const state = reviewStateFor(runId);
    const selected = REVIEW_DECISION[state.decision] || null;
    const disabled = state.sending ? " disabled" : "";
    const reason = String(state.reason || "");
    const choices = REVIEW_DECISIONS.map(item => {
      const active = item.key === state.decision;
      return `<button type="button" class="review-choice${active ? " selected" : ""}" data-review-decision="${esc(item.key)}" aria-pressed="${active ? "true" : "false"}"${disabled}>
        <span class="rc-label">${esc(item.label)}</span>
        <span class="rc-hint">${esc(item.hint)}</span>
      </button>`;
    }).join("");
    const reasonHtml = selected
      ? `<div class="review-reason">
          <div class="review-reason-head">
            <label class="review-reason-label" for="${reviewReasonId(runId)}">判断理由</label>
            <span class="${selected.requiresReason ? "review-required" : "review-optional"}">${selected.requiresReason ? "必填" : "可选"}</span>
          </div>
          <textarea class="review-textarea" id="${reviewReasonId(runId)}" data-review-reason rows="2" maxlength="1000"
            placeholder="${esc(selected.placeholder)}"${disabled}>${esc(reason)}</textarea>
          <div class="review-foot">
            <span class="review-error" data-review-error role="alert"${state.error ? "" : " hidden"}>${esc(state.error || "")}</span>
            <span class="grow"></span>
            <span class="review-count" data-review-count>${reason.length} 字</span>
            <button type="button" class="a-btn strong" data-review-submit${disabled || (selected.requiresReason && !reason.trim()) ? " disabled" : ""}>${state.sending ? "记录中…" : "记录判断"}</button>
          </div>
        </div>`
      : `<p class="review-prompt">先选择你的判断，再写理由。</p>`;
    return `<div class="review-head">
      <span class="review-kicker">REVIEW · 判断记录</span>
      <span class="review-hint">${state.appending ? "追加判断 · 写入签名链" : "reviewer · 写入签名链"}</span>
    </div>
    <div class="review-choices" role="group" aria-label="判断决定">${choices}</div>
    ${reasonHtml}`;
  }

  function reviewPanelHtml(trace) {
    const runId = reviewRunId();
    if (!runId) {
      return `<div class="review review-readonly">
        <div class="review-head">
          <span class="review-kicker">REVIEW · 判断记录</span>
          <span class="review-hint">只读</span>
        </div>
        <p>只读回放 · 没有可写入的运行 id，判断不会进入签名链。</p>
      </div>`;
    }
    return `<div class="review" data-review-panel data-review-run="${esc(runId)}">${reviewPanelInnerHtml(trace, runId)}</div>`;
  }

  function paintReviewPanel(runId) {
    const panel = reviewPanelElement(runId);
    if (!panel) return;
    const trace = shownTrace() || {};
    panel.innerHTML = reviewPanelInnerHtml(trace, runId);
  }

  function chooseReviewDecision(runId, decision) {
    if (!runId || !REVIEW_DECISION[decision]) return;
    const state = reviewStateFor(runId);
    if (state.sending || (state.recorded && !state.appending)) return;
    state.decision = decision;
    state.error = "";
    paintReviewPanel(runId);
    const panel = reviewPanelElement(runId);
    const textarea = panel && panel.querySelector("[data-review-reason]");
    if (textarea) textarea.focus();
  }

  function startAppendReview(runId) {
    if (!runId) return;
    const state = reviewStateFor(runId);
    if (state.sending) return;
    state.appending = true;
    state.recorded = null;
    state.decision = "";
    state.reason = "";
    state.error = "";
    paintReviewPanel(runId);
    const panel = reviewPanelElement(runId);
    const first = panel && panel.querySelector("[data-review-decision]");
    if (first) first.focus({ preventScroll: true });
  }

  function applyReviewSuggestion(runId, decision, reason) {
    if (!runId || !REVIEW_DECISION[decision]) return;
    const state = reviewStateFor(runId);
    if (state.sending) return;
    const trace = shownTrace() || {};
    if (!state.appending && recordedReview(trace, runId)) {
      state.appending = true;
      state.recorded = null;
    }
    state.decision = decision;
    state.reason = String(reason || "");
    state.error = "";
    paintReviewPanel(runId);
    const panel = reviewPanelElement(runId);
    const textarea = panel && panel.querySelector("[data-review-reason]");
    if (textarea) {
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    }
  }

  function syncReviewDraft(runId, panel, input) {
    const state = reviewStateFor(runId);
    state.reason = input.value;
    state.error = "";
    const selected = REVIEW_DECISION[state.decision];
    const submit = panel.querySelector("[data-review-submit]");
    if (submit) submit.disabled = !!state.sending || !!(selected && selected.requiresReason && !input.value.trim());
    const count = panel.querySelector("[data-review-count]");
    if (count) count.textContent = `${input.value.length} 字`;
    const error = panel.querySelector("[data-review-error]");
    if (error && !error.hidden) {
      error.hidden = true;
      error.textContent = "";
    }
  }

  function reviewErrorText(data, status) {
    const raw = data && (data.error || data.message || data.detail);
    if (typeof raw === "string" && raw.trim()) return raw.trim();
    if (raw) {
      try { return JSON.stringify(raw); } catch { return String(raw); }
    }
    return `HTTP ${status}`;
  }

  function waitForReviewTraceRefresh(runId) {
    return reviewTraceRefreshes.get(runId) || null;
  }

  function refreshReviewedTrace(runId) {
    if (!runId) return Promise.resolve(null);
    const promise = fetch(runTraceUrl(runId), { cache: "no-store" })
      .then(response => (response.ok ? response.json() : null))
      .then(trace => {
        if (!trace || !Array.isArray(trace.events)) return null;
        cache.set("run:" + runId, trace);
        if (store.key !== "run:" + runId) return trace;
        store.trace = trace;
        const s = activeSession();
        if (s && s.id === runId) s.trace = trace;
        verifyChain(trace).then(check => {
          if (store.key !== "run:" + runId) return;
          store.check = check;
          setChainBadge(check);
        }).catch(() => { /* 链验证失败不阻断判断记录 */ });
        return trace;
      })
      .catch(() => null)
      .finally(() => {
        if (reviewTraceRefreshes.get(runId) === promise) reviewTraceRefreshes.delete(runId);
      });
    reviewTraceRefreshes.set(runId, promise);
    return promise;
  }

  async function submitReview(runId) {
    if (!runId) return;
    const state = reviewStateFor(runId);
    const decision = REVIEW_DECISION[state.decision];
    if (!decision || state.sending || (state.recorded && !state.appending)) return;
    const reason = String(state.reason || "").trim();
    if (decision.requiresReason && !reason) {
      state.error = "请填写判断理由：说明缺什么材料、保留什么，或为什么退回。";
      paintReviewPanel(runId);
      const panel = reviewPanelElement(runId);
      const textarea = panel && panel.querySelector("[data-review-reason]");
      if (textarea) textarea.focus();
      return;
    }
    state.sending = true;
    state.error = "";
    paintReviewPanel(runId);
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: state.decision, reason, reviewer: "reviewer" }),
      });
      let data = {};
      try { data = await response.json(); } catch { /* 非 JSON 错误也走可读提示 */ }
      if (!response.ok || (data && data.ok === false)) {
        throw new Error(reviewErrorText(data, response.status));
      }
      const event = data.event || {};
      const sequence = data.sequence !== undefined ? data.sequence : event.sequence;
      state.recorded = {
        decision: data.decision || state.decision,
        reason,
        reviewer: "reviewer",
        sequence,
      };
      state.sending = false;
      state.appending = false;
      state.decision = "";
      state.reason = "";
      state.error = "";
      paintReviewPanel(runId);
      loadReviewProfile(shownTrace() || {}, { force: true });
      refreshReviewedTrace(runId);
      const label = REVIEW_LABEL[state.recorded.decision] || state.recorded.decision;
      toast(`已记录 · ${label} · #${sequence === undefined || sequence === null ? "—" : sequence}`);
    } catch (error) {
      state.sending = false;
      state.error = `记录失败：${error && error.message ? error.message : "网络错误"}`;
      paintReviewPanel(runId);
    }
  }

  return {
    reviewRunId, loadReviewProfile, reviewPanelHtml, chooseReviewDecision,
    startAppendReview, applyReviewSuggestion, syncReviewDraft, submitReview,
    waitForReviewTraceRefresh,
  };
}
