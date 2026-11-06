"use strict";

/* 判断记录与定时任务两个 surface 的渲染与本地状态。
 *
 * 自 app.js 抽离（P2 切片 C）——两个页面共享同一形态：
 * page-head + 内容区 + renderMain({mode})，数据一个来自 API、一个来自
 * 本机 localStorage。依赖经 deps 注入（renderMain/setFoot/toast/
 * getViewGateSequence），测试用桩替换。
 *
 * 判断记录里的「审计处置」区段与 run 判断语义不同（三档处置 vs 四类
 * 判断），并列展示不并计数；标签单一来源 = audit-view 的
 * DISPOSITION_LABELS。
 */

import { esc } from "../core/format.js";
import { DISPOSITION_LABELS } from "../audit/audit-view.js";

export function createPages(deps) {
  const d = deps;

  /* ------------------------------------------------------------ 判断记录 */

  async function renderReviews() {
    const epoch = d.getViewGateSequence();
    document.getElementById("tb-title").textContent = "判断记录";
    document.getElementById("tb-chips").innerHTML = "";
    d.setFoot("判断记录 · 只读汇总", "");
    const ta = document.getElementById("composer-ta");
    if (ta) ta.value = "";
    d.renderMain('<div class="empty">读取判断记录…</div>', { mode: "reviews" });
    let profile;
    try {
      const r = await d.fetchImpl("/api/review-profile", { cache: "no-store" });
      profile = await r.json();
    } catch (e) {
      if (epoch !== d.getViewGateSequence()) return;
      d.renderMain('<div class="empty">无法加载判断记录：' + esc(e.message) + '</div>',
        { mode: "reviews" });
      return;
    }
    if (epoch !== d.getViewGateSequence()) return;
    const cards = [
      ["接受判断", profile.counts.accept], ["需要补材料", profile.counts.needs_evidence],
      ["保留意见", profile.counts.qualified], ["退回重做", profile.counts.redo],
    ];
    const rows = (profile.recent || []).map(item => '<div class="rev-row">'
      + '<span class="rev-dec">' + esc(item.decision || item.decision_label || "—") + '</span>'
      + '<span class="rev-what">' + esc(item.reason || item.note || item.objective || "") + '</span>'
      + '<span class="rev-at">' + esc(item.at || item.created || "") + '</span></div>').join("");
    d.renderMain(
      '<section class="page-head"><p class="eyebrow">REVIEW PROFILE</p>'
      + '<h1 class="page-title">判断记录</h1>'
      + '<p class="page-meta">你在各次委托判断面里做出的选择，按时间聚合。判断以标签随签名链记录，此处只作汇总展示。</p></section>'
      + '<div class="rev-cards">'
      + cards.map(pair => '<div class="rev-card"><span class="rev-num">' + (pair[1] ?? 0) + '</span><span class="rev-k">' + pair[0] + '</span></div>').join("")
      + '</div>'
      + ((profile.recent || []).length ? '<div class="rev-list">' + rows + '</div>'
        : '<div class="empty">还没有判断记录——在任一运行的「判断面」里做出选择后，这里会开始积累。</div>')
      + auditDispositionSection(profile.audit_dispositions),
      { mode: "reviews" });
  }

  /* 审计处置区段:与 run 判断语义不同(三档处置 vs 四类判断),并列展示
     不并计数。标签单一来源 = audit-view 的 DISPOSITION_LABELS。 */
  function auditDispositionSection(entries) {
    const list = Array.isArray(entries) ? entries.filter(e => e && e.disposition) : [];
    const rows = list.map(item => '<div class="rev-row">'
      + '<span class="rev-dec">' + esc(DISPOSITION_LABELS[item.disposition] || item.disposition) + '</span>'
      + '<span class="rev-what">' + esc(item.category || "") + ' · ' + esc(item.finding_id || "")
      + (item.audit_id ? ' <span class="rev-at">→ 审计台 ' + esc(item.audit_id) + '</span>' : '')
      + '</span></div>').join("");
    if (!list.length) return "";
    return '<section class="page-head" style="margin-top:18px"><p class="eyebrow">AUDIT DISPOSITIONS</p>'
      + '<h2 class="page-title" style="font-size:16px">审计处置</h2>'
      + '<p class="page-meta">你在审计台里对发现做出的三档处置（接受风险 / 误报争议 / 不修复），按审计聚合。</p></section>'
      + '<div class="rev-list">' + rows + '</div>';
  }

  /* ------------------------------------------------------------ 定时任务 */

  /* 调度器已接入运行时侧：排程保存在本机服务的 scheduler.sqlite，到点经
     与手创运行完全相同的流水线自动创建（门控/预算/收据/签名链）。
     旧版 localStorage 草稿在首次打开本页时一次性迁移上送。 */

  const SCHEDULE_POST = { method: "POST", headers: { "Content-Type": "application/json" } };

  async function fetchSchedules() {
    const r = await d.fetchImpl("/api/schedules", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()).schedules || [];
  }

  function scheduleRows(entries) {
    if (!entries.length) return '<div class="empty">还没有排程——填好上面的表单保存即可。</div>';
    return entries.map(t => `<div class="sch-row">
      <div class="sch-main"><b>${esc(t.objective || "（未命名）")}</b>
      <span class="sch-meta">${esc(t.template || "full")} · ${esc(t.approval || "consequential")} · ${esc(t.next || `${t.frequency} ${t.time || ""}`)}</span></div>
      <span class="sch-badge">${esc(t.enabled
        ? (t.last_status === "running" ? "上次已触发 · 运行中"
          : t.last_status ? `上次 ${esc(String(t.last_status))}` : "已排程")
        : "已暂停")}</span>
      <button class="row-more" data-sch-toggle="${esc(t.id)}" title="${t.enabled ? "暂停" : "恢复"}">${t.enabled ? "⏸" : "▶"}</button>
      <button class="row-more sch-del" data-sch-del="${esc(t.id)}" title="删除">×</button></div>`).join("");
  }

  /* 旧版 localStorage 草稿一次性迁移到服务端；迁移后清掉本机键。 */
  async function migrateLegacyDrafts() {
    let legacy = [];
    try { legacy = JSON.parse(localStorage.getItem("fa.scheduled") || "[]"); } catch { legacy = []; }
    if (!Array.isArray(legacy) || !legacy.length) return 0;
    let moved = 0;
    for (const draft of legacy) {
      if (!draft || !draft.objective) continue;
      try {
        const r = await d.fetchImpl("/api/schedules", { ...SCHEDULE_POST,
          body: JSON.stringify({
            objective: draft.objective, template: draft.template || "full",
            approval: draft.approval || "consequential",
            frequency: draft.frequency || "每天", time: draft.time || "09:00",
          }) });
        if (r.ok) moved += 1;
      } catch { /* 单条迁移失败不阻断其余 */ }
    }
    localStorage.removeItem("fa.scheduled");
    return moved;
  }

  async function renderScheduledPage() {
    const epoch = d.getViewGateSequence();
    document.getElementById("tb-title").textContent = "定时任务";
    document.getElementById("tb-chips").innerHTML = "";
    d.setFoot("定时任务 · 本机调度器", "");
    const ta = document.getElementById("composer-ta");
    if (ta) ta.value = "";
    let moved = 0;
    try {
      moved = await migrateLegacyDrafts();
    } catch { /* 迁移失败按 0 处理 */ }
    if (epoch !== d.getViewGateSequence()) return;
    d.renderMain(
      '<section class="page-head"><p class="eyebrow">SCHEDULED</p>'
      + '<h1 class="page-title">定时任务</h1>'
      + '<p class="page-meta">把重复的委托排上日程。调度器已接入运行时侧：到点自动创建运行，'
      + '审批档位与门控照常生效。排程保存在本机服务，重启后仍在。</p></section>'
      + '<div class="sch-form">'
      + '<label>目标<textarea id="sch-objective" rows="2" placeholder="例如：每天早上核对昨日新增的口径变更"></textarea></label>'
      + '<div class="sch-grid">'
      + '<label>模板<select id="sch-template">' + d.getScenarios().map(pair => '<option value="' + pair[0] + '">' + pair[1] + '</option>').join("") + '</select></label>'
      + '<label>审批档位<select id="sch-approval"><option value="consequential">关键审批</option><option value="all">逐步审批</option><option value="off">自动</option></select></label>'
      + '<label>频率<select id="sch-frequency"><option>每天</option><option>工作日</option><option>每周</option></select></label>'
      + '<label>时间<input id="sch-time" type="time" value="09:00"></label>'
      + '</div><div class="sch-actions">'
      + '<span class="sch-honest">每周 = 与创建日相同的星期几；到点创建的运行可在账本里看到。</span>'
      + '<button id="sch-save">保存排程</button></div></div>'
      + '<div class="sch-list"><div class="empty">读取排程…</div></div>',
      { mode: "scheduled" });
    if (moved) d.toast(`已迁移 ${moved} 条旧草稿到本机调度器`);

    let entries = [];
    try {
      entries = await fetchSchedules();
    } catch (e) {
      if (epoch !== d.getViewGateSequence()) return;
      document.querySelector(".sch-list").innerHTML =
        '<div class="empty">读取排程失败：' + esc(e.message) + '</div>';
      return;
    }
    if (epoch !== d.getViewGateSequence()) return;
    document.querySelector(".sch-list").innerHTML = scheduleRows(entries);

    const save = document.getElementById("sch-save");
    if (save) save.addEventListener("click", async () => {
      const objective = document.getElementById("sch-objective").value.trim();
      if (!objective) { d.toast("先描述定时任务的目标"); return; }
      const body = {
        objective,
        template: document.getElementById("sch-template").value,
        approval: document.getElementById("sch-approval").value,
        frequency: document.getElementById("sch-frequency").value,
        time: document.getElementById("sch-time").value || "09:00",
      };
      try {
        const r = await d.fetchImpl("/api/schedules", { ...SCHEDULE_POST, body: JSON.stringify(body) });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.error || `HTTP ${r.status}`);
        }
        d.toast("排程已保存 · 到点自动创建运行");
        renderScheduledPage();
      } catch (e) {
        d.toast("保存失败：" + e.message);
      }
    });
    for (const btn of document.querySelectorAll("[data-sch-del]")) {
      btn.addEventListener("click", async () => {
        await d.fetchImpl("/api/schedules/delete", { ...SCHEDULE_POST, body: JSON.stringify({ id: btn.dataset.schDel }) });
        renderScheduledPage();
      });
    }
    for (const btn of document.querySelectorAll("[data-sch-toggle]")) {
      btn.addEventListener("click", async () => {
        const entry = entries.find(t => t.id === btn.dataset.schToggle);
        await d.fetchImpl("/api/schedules/toggle", { ...SCHEDULE_POST,
          body: JSON.stringify({ id: btn.dataset.schToggle, enabled: !(entry && entry.enabled) }) });
        renderScheduledPage();
      });
    }
  }

  return { renderReviews, renderScheduledPage };
}