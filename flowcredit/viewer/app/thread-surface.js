"use strict";

/* 统一任务线程的壳层接线（自 app.js 拆分切片 3b 迁入）。
 *
 * 投影在 app/task-thread.js（纯函数）、审批卡在 app/approval-card.js（纯函数）；
 * 这里是两者与 app 的接缝：产品语言注入（结论/判断面/预算表/运行动作）、
 * 条目级 DOM 打补丁与线程容器挂载。deps 注入 app 侧渲染器与状态读取，
 * 使接线本身可用桩测试；纯投影的语义由 task-thread 自己的测试覆盖。
 */

import { esc, stripTags } from "../core/format.js";
import {
  changedThreadKeys, formatElapsed, lastBudget, projectTaskThread, threadItemHtmlMap,
} from "./task-thread.js";
import { isProjectTrace } from "./project-work.js";
import {
  isWriteLike, approvalActionTitle,
  approvalCommandHtml, approvalDiffHtml, approvalImpactHtml, approvalComputerHtml,
} from "./approval-card.js";

export function createThreadSurface(deps) {
  const d = deps;

  function observationSummary(observation) {
    if (!observation || !observation.tool) return "";
    try {
      return stripTags((d.describe(observation) || [])[0] || "");
    } catch { return ""; }
  }

  function threadCtx() {
    return {
      isWriteLike,
      approvalTitle: approvalActionTitle,
      approvalWhy: pending => (pending.policy === "every-step"
        ? "逐步审批：本次运行的每次工具调用都需要你确认。"
        : "关键步骤：这个动作会改变结论或宣告任务受阻，执行前需要你确认。"),
      approvalPreviewHtml: pending => {
        if (!isWriteLike(pending.tool)) return "";
        if (pending.preview && pending.preview.computer) return approvalComputerHtml(pending.preview);
        if (pending.preview && pending.preview.command !== undefined) return approvalCommandHtml(pending.preview);
        if (pending.preview && (pending.preview.diff !== undefined || pending.preview.error)) return approvalDiffHtml(pending.preview);
        return approvalImpactHtml(pending.arguments || {});
      },
    };
  }

  function artifactsOf(result) {
    return Array.isArray(result.results)
      ? result.results.filter(entry => entry && entry.path)
      : (result.path ? [result] : []);
  }

  /* 最终回复只使用结果中真实存在的总结字段；没有总结时说明可验证事实。 */
  function answerTextFor(trace) {
    const terminal = trace.terminal || {};
    const result = terminal.result || {};
    const supplied = [result.answer, result.final_response, result.summary]
      .find(value => typeof value === "string" && value.trim());
    if (supplied) return supplied.trim();
    if (isProjectTrace(trace)) {
      const count = (trace.project?.filesChanged || []).length;
      return `已在隔离副本中完成修改${count ? `，共变更 ${count} 个文件` : ""}；原目录未改动。`;
    }
    if (result.change_bp !== undefined) return d.resultConclusion(result);
    const artifacts = artifactsOf(result);
    if (artifacts.length === 1) return `已生成 \`${artifacts[0].path}\`，并通过 Runtime 文件状态复核。`;
    if (artifacts.length > 1) return `已生成 ${artifacts.length} 个交付文件，并通过 Runtime 文件状态复核。`;
    return "任务已完成，结果通过 Runtime 独立校验。";
  }

  function answerDetailTitleFor(trace) {
    if (isProjectTrace(trace)) return "代码变更与验证";
    const result = (trace.terminal || {}).result || {};
    if (result.change_bp !== undefined) return "指标、引用与判断";
    return "验证与签名信息";
  }

  function answerDetailFor(trace) {
    const terminal = trace.terminal || {};
    const result = terminal.result || {};
    if (terminal.status !== "succeeded") return d.failureJudgmentHtml(trace, terminal);
    if (isProjectTrace(trace)) return d.projectWork.projectResultHtml(trace) || "";
    if (result.change_bp !== undefined) return d.judgmentSurfaceHtml(trace, result);
    const artifacts = artifactsOf(result);
    if (artifacts.length) return d.generalResultHtml(trace, result, artifacts);
    return "";
  }

  /* 交付物紧随回答展示；完整签名与结构化结果仍可展开复核。 */
  function answerDeliveryFor(trace) {
    if ((trace.terminal || {}).status !== "succeeded") return "";
    if (isProjectTrace(trace)) return d.projectWork.projectDeliveryHtml(trace) || "";
    const artifacts = artifactsOf((trace.terminal || {}).result || {});
    if (!artifacts.length) return "";
    return `<div class="th-artifacts" aria-label="交付文件">${artifacts.map(entry => `<div class="th-artifact">
      <span class="th-artifact-mark" aria-hidden="true">↗</span>
      <code class="th-artifact-path">${esc(entry.path)}</code>
      <span class="th-artifact-meta">${Number(entry.bytes || 0).toLocaleString("en-US")} B${entry.commit ? ` · ${esc(entry.commit)}` : ""}</span>
      <span class="th-artifact-state">文件状态已复核</span>
    </div>`).join("")}</div>`;
  }

  function threadOptions(trace, session) {
    const running = !!(session && session.running && !session.finished);
    const budget = lastBudget(trace);
    return {
      planEditable: running,
      planEditing: !!(session && session.planEditing),
      planConfirm: (session && session.planConfirm) || null,
      planQueued: session && session.planQueued ? session.planQueued.label : "",
      summaryOf: observation => observationSummary(observation),
      blockKindOf: round => d.resultBlockKind(round),
      scenarioLabel: key => d.tplTitle(String(key)),
      budgetHtml: budget
        ? d.renderMeters(trace, {
          editable: running,
          editing: !!(session && session.budgetEditing),
          queued: !!(session && session.budgetQueued),
          live: running,
        })
        : "",
      answerText: source => answerTextFor(source),
      answerDeliveryHtml: source => answerDeliveryFor(source),
      answerDetailTitle: source => answerDetailTitleFor(source),
      answerDetailHtml: source => answerDetailFor(source),
      // Successful answers are a conversation, not an admin panel: no report/
      // diagnostics/cost chips in the thread. Failures keep their recovery
      // actions, and the rail still carries the full evidence.
      answerActionsHtml: source => ((source.terminal || {}).status === "succeeded"
        ? "" : d.runActionsHtml(source)),
      failureText: terminal => d.failureConclusion(terminal.failure || {}),
      statusArtifactHtml: source => {
        const terminal = source.terminal || {};
        if (!terminal.status || terminal.status === "succeeded") return "";
        return isProjectTrace(source)
          ? d.projectWork.projectArtifactStateHtml(source) || "" : "";
      },
      failureDetailHtml: source => d.failureJudgmentHtml(source, source.terminal || {}),
    };
  }

  function liveStateOf(s) {
    return {
      running: !!(s && s.running && !s.finished),
      finished: !!(s && (s.finished || (s.trace && s.trace.terminal && s.trace.terminal.status))),
      pending: (s && s.pending) || null,
      deciding: (s && s.deciding) || null,
      answering: !!(s && s.answering),
      error: (s && s.lastError) || "",
      terminal: (s && s.trace && s.trace.terminal) || {},
    };
  }

  /* 条目级打补丁：只有内容真的变了的条目才替换节点，其余保持原样。
     展开状态、details 的 open、焦点与滚动都不因无关的 SSE 帧被重置。 */
  function carryDisclosure(previous, next) {
    if (!previous || !next) return;
    if (previous.dataset.user === "1" && previous.dataset.open !== undefined) {
      next.dataset.open = previous.dataset.open;
      const toggle = next.querySelector("[data-thread-toggle]");
      if (toggle) toggle.setAttribute("aria-expanded", previous.dataset.open);
      next.dataset.user = "1";
    }
    if (previous.dataset.threadItem === "plan" && previous.dataset.user === "1") {
      next.open = previous.open;
      next.dataset.user = "1";
    }
    const keep = new Map();
    previous.querySelectorAll("details[data-keep]").forEach(node => keep.set(node.dataset.keep, node.open));
    next.querySelectorAll("details[data-keep]").forEach(node => {
      if (keep.has(node.dataset.keep)) node.open = keep.get(node.dataset.keep);
    });
  }

  function patchThread(container, items, ctx, cache) {
    if (!container) return;
    const htmlMap = threadItemHtmlMap(items, ctx);
    const changed = new Set(changedThreadKeys(cache, htmlMap));
    const existing = new Map();
    for (const child of [...container.children]) {
      if (child.dataset.threadKey) existing.set(child.dataset.threadKey, child);
    }
    const ordered = [];
    for (const item of items) {
      const html = htmlMap.get(item.key);
      let node = existing.get(item.key);
      if (!node) {
        const holder = document.createElement("div");
        holder.innerHTML = html;
        node = holder.firstElementChild;
      } else if (changed.has(item.key)) {
        const holder = document.createElement("div");
        holder.innerHTML = html;
        const fresh = holder.firstElementChild;
        carryDisclosure(node, fresh);
        node.replaceWith(fresh);
        node = fresh;
      }
      cache.set(item.key, html);
      existing.delete(item.key);
      ordered.push(node);
    }
    for (const stale of existing.values()) stale.remove();
    let cursor = container.firstElementChild;
    for (const node of ordered) {
      if (node === cursor) {
        cursor = cursor.nextElementSibling;
        continue;
      }
      container.insertBefore(node, cursor);
    }
  }

  function threadCacheFor(s) {
    if (s) {
      if (!s.threadCache) s.threadCache = new Map();
      return s.threadCache;
    }
    const store = d.getStore();
    if (!store.threadCache) store.threadCache = new Map();
    return store.threadCache;
  }

  let clockTimer = null;

  function paintThreadClock() {
    const clock = document.querySelector('[data-thread-clock][data-live="1"]');
    if (!clock) {
      if (clockTimer) clearInterval(clockTimer);
      clockTimer = null;
      return;
    }
    const startedAt = Number(clock.dataset.startedAt);
    if (Number.isFinite(startedAt)) clock.textContent = formatElapsed(Date.now() - startedAt);
    if (!clockTimer) clockTimer = setInterval(paintThreadClock, 1000);
  }

  function paintThread(s, trace, { live = false } = {}) {
    const container = document.getElementById("thread");
    if (!container || !trace) return null;
    const source = trace;
    const liveState = live ? liveStateOf(s) : {
      running: !!(s && s.running && !s.finished),
      finished: true,
      pending: (s && s.pending) || null,
      deciding: (s && s.deciding) || null,
      answering: !!(s && s.answering),
      terminal: source.terminal || {},
    };
    const { items } = projectTaskThread(
      source,
      live ? Object.assign({}, liveState, { now: Date.now() }) : liveState,
      threadOptions(source, s),
    );
    patchThread(container, items, threadCtx(), threadCacheFor(s));
    const terminalStatus = ((source.terminal || liveState.terminal || {}) || {}).status || "";
    if (container.dataset && container.classList && !container.dataset.processTouched) {
      container.classList.toggle("hide-tools", !!terminalStatus);
    }
    if (live) paintThreadClock();
    return items;
  }

  function threadMountHtml() {
    return `<section class="task-thread" id="thread" data-thread aria-label="任务线程"></section><div id="queued-slot" class="th-queued"></div>`;
  }

  function projectRunHtml(trace, { live = false } = {}) {
    const active = d.currentWorkTab();
    return `<div class="project-work-layout" data-project-work="${esc(trace.task?.id || "")}">
      <div class="work-view-tabs" data-work-tabs role="tablist" aria-label="项目任务视图">${d.projectWork.projectTabsHtml(trace, active)}</div>
      <section class="work-pane" data-work-pane="activity">
        ${d.projectWork.projectContextHtml(trace)}
        ${threadMountHtml()}
      </section>
    </div>`;
  }

  return {
    observationSummary, threadCtx, artifactsOf, answerTextFor, answerDeliveryFor,
    answerDetailTitleFor, answerDetailFor, threadOptions, liveStateOf, carryDisclosure,
    patchThread, threadCacheFor, paintThread, threadMountHtml, projectRunHtml,
  };
}
