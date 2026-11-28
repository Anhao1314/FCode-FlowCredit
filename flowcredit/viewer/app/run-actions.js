"use strict";

/* 运行会话内的操作员动作（自 app.js 拆分切片 3c 迁入）。
 *
 * 审批决定、问题回答、计划/预算指令——都作用于当前会话（activeSession），
 * 都以"就地重绘线程 + toast 反馈"收尾，API 失败时回滚乐观状态。
 * 线程补丁由 thread-surface 的 paintThread 提供；审批决定身份沿用
 * task-thread 的 decisionForPending（投影与 API 共用同一份构造）。
 */

import { decisionForPending, lastBudget } from "./task-thread.js";

export function createRunActions(deps) {
  const d = deps;

  /* 审批与提问都在线程里就地重绘：节点由统一投影生成，这里只触发一次补丁。 */
  function paintApproval() {
    const s = d.activeSession();
    if (!s || !s.trace) return;
    d.paintThread(s, s.trace, { live: !(s.finished || (s.trace.terminal && s.trace.terminal.status)) });
  }

  async function answerQuestion(skip) {
    const s = d.activeSession();
    if (!s || !s.pending || s.pending.kind !== "question" || s.answering) return;
    const pending = s.pending;
    const input = document.querySelector("[data-question-input]");
    const text = skip ? "" : String(input ? input.value : "").trim();
    if (!skip && !text) { await answerQuestion(true); return; }
    s.answering = true;
    paintApproval();
    try {
      const response = await d.fetchImpl(`/api/runs/${encodeURIComponent(s.id)}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(skip
          ? { questionId: pending.id, skip: true }
          : { questionId: pending.id, text }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      s.answering = false;
      d.toast(skip ? "已跳过 · 模型自行判断" : "已提交回答 · 运行继续");
    } catch (error) {
      s.answering = false;
      paintApproval();
      d.toast("回答未送达：" + error.message);
    }
  }

  async function decideApproval(allow, remember = false) {
    const s = d.activeSession();
    if (!s || !s.pending || s.pending.kind === "question" || s.deciding) return;
    const pending = s.pending;
    s.deciding = decisionForPending(pending, allow, remember);
    paintApproval();
    try {
      const response = await d.fetchImpl(`/api/runs/${encodeURIComponent(s.id)}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvalId: pending.id, allow, remember }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      d.toast(allow ? (remember ? "本会话已授权 · 运行继续" : "已允许 · 运行继续") : "已拒绝 · 运行将终止");
    } catch (error) {
      s.deciding = null;
      paintApproval();
      d.toast("决定未送达：" + error.message);
    }
  }

  /* ------------------------------------- 计划/预算指令与 API */

  /* 计划与预算的编辑态都进同一条线程；这里只触发一次条目补丁。 */
  function paintPlan() {
    const s = d.activeSession();
    if (!s || !s.trace) return;
    d.paintThread(s, s.trace, { live: !(s.finished || (s.trace.terminal && s.trace.terminal.status)) });
  }

  function paintBudget() {
    paintPlan();
  }

  async function queuePlan(action, todoId) {
    const s = d.activeSession();
    if (!s || !s.running) return;
    const note = action === "skip" ? "操作员跳过" : "操作员提前";
    try {
      const response = await d.fetchImpl(`/api/runs/${encodeURIComponent(s.id)}/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, todoId, note }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      const label = action === "skip" ? `已排队：跳过 ${todoId}` : `已排队：提前 ${todoId}`;
      s.planQueued = { label, events: (s.trace.events || []).length };
      s.planConfirm = null;
      paintPlan();
      d.toast(label + " · 下一轮生效");
    } catch (error) {
      d.toast("计划指令未送达：" + error.message);
    }
  }

  async function queueBudget() {
    const s = d.activeSession();
    if (!s || !s.running) return;
    const read = key => Number((document.querySelector(`[data-budget="${key}"]`) || {}).value || 0);
    const limits = lastBudget(s.trace) ? lastBudget(s.trace).limits : {};
    const payload = {
      maxCalls: read("maxCalls"),
      maxReads: read("maxReads"),
      maxCostMicrousd: read("maxCostMicrousd"),
      maxTokens: read("maxTokens"),
      requestId: (globalThis.crypto?.randomUUID?.() || `budget-${Date.now()}-${Math.random().toString(16).slice(2)}`),
    };
    if (
      !payload.maxCalls || payload.maxCalls < (limits.max_calls || 0) ||
      !payload.maxReads || payload.maxReads < (limits.max_reads || 0) ||
      !payload.maxCostMicrousd || payload.maxCostMicrousd < (limits.max_cost_microusd || 0) ||
      !payload.maxTokens || payload.maxTokens < (limits.max_tokens || 0)
    ) {
      d.toast("上限只能调高，且必须是整数");
      return;
    }
    try {
      const response = await d.fetchImpl(`/api/runs/${encodeURIComponent(s.id)}/budget`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      s.budgetEditing = false;
      s.budgetQueued = { events: (s.trace.events || []).length };
      paintBudget();
      d.toast("预算调整已排队 · 下一轮生效");
    } catch (error) {
      d.toast("预算调整未送达：" + error.message);
    }
  }

  return {
    paintApproval, answerQuestion, decideApproval,
    paintPlan, paintBudget, queuePlan, queueBudget,
  };
}
