import test from "node:test";
import assert from "node:assert/strict";

import {
  ZONES, chatRowsHtml, chatSectionHtml, govHistorySectionHtml,
  pinnedSectionHtml, projectSectionHtml, quickStartHtml,
  runRowsHtml, runSectionHtml, statusDotHtml, zoneOf, zoneSwitchHtml,
} from "../core/side-nav.js";

test("功能导航已撤除:治理台走 Governance 区切换器,不再有侧栏条目", async () => {
  const mod = await import("../core/side-nav.js");
  for (const key of ["SIDEBAR_NAV", "NAV_GROUPS", "navItemsHtml", "groupedNavHtml",
    "MORE_NAV", "reservedSlots"]) {
    assert.equal(mod[key], undefined, `${key} 不应再导出（导航/预留均已清理）`);
  }
  // 两区对话面仍在:Work/Governance 的切换器是唯一侧栏入口
  assert.deepEqual(ZONES.map(z => z.label), ["Work", "Governance"]);
  assert.deepEqual(ZONES.map(z => z.land), ["work", "gov"]);
  const html = zoneSwitchHtml("gov");
  assert.match(html, /data-open="gov"/, "治理台经 Governance 区着陆");
});

test("S2.1 zoneOf:视图归属区,非区视图返回 null(切换器不闪)", () => {
  assert.equal(zoneOf("gov"), "gov");
  assert.equal(zoneOf("audit"), "gov");
  assert.equal(zoneOf("reviews"), "gov");
  for (const view of ["work", "chat", "ledger", "run", "scheduled", "intake", "empty"]) {
    assert.equal(zoneOf(view), "work", `${view} 属工作区`);
  }
  for (const view of ["settings", "canvas", undefined, ""]) {
    assert.equal(zoneOf(view), null, `${view} 不属任何区`);
  }
});

test("S2.1 zoneSwitchHtml:两键带 data-open 着陆与 aria-pressed 点亮", () => {
  assert.deepEqual(ZONES.map(z => z.key), ["work", "gov"]);
  // R1:两区的着陆点都是各自的对话面——工作区=新对话(chat),治理区=治理台
  assert.deepEqual(ZONES.map(z => z.land), ["work", "gov"]);
  const html = zoneSwitchHtml("gov");
  assert.match(html, /data-zone="work" data-open="work"/);
  assert.match(html, /data-zone="gov" data-open="gov" title="[^"]*" aria-pressed="true"/);
  assert.match(html, /data-zone="work"[^>]*aria-pressed="false"/);
  assert.equal((html.match(/<button/g) || []).length, 2);
});

test("置顶段只在真的置顶过东西时出现，任务行复用运行行", () => {
  assert.equal(pinnedSectionHtml([], {}), "");
  assert.equal(pinnedSectionHtml(null, {}), "");
  const compact = pinnedSectionHtml([{ id: "run-1", title: "恒星毛利率", meta: "12:03" }], {});
  assert.match(compact, /置顶/);
  assert.match(compact, /data-section-toggle="pinned"/);
  assert.match(compact, /data-run="run-1"/);

  const reused = pinnedSectionHtml([{ id: "run-1" }], { rowHtml: r => `<div data-run="${r.id}" data-menu="1"></div>` });
  assert.match(reused, /data-menu="1"/, "置顶的任务行保留行内菜单（能取消置顶）");

  const chats = pinnedSectionHtml([{ id: "t1", title: "周三" }], { kind: "chat", pinnedOf: () => true });
  assert.match(chats, /data-chat-id="t1"/);
  assert.match(chats, /data-chat-pin="t1"/);
  assert.match(chats, /取消置顶/);
});

test("Projects 段：标题可折叠 + ＋动作，项目展开后嵌运行", () => {
  const empty = projectSectionHtml([], {});
  assert.match(empty, /data-section-toggle="projects"/);
  assert.match(empty, /data-proj-add/);
  assert.match(empty, /还没有项目/);
  const runs = [1, 2, 3].map(n => ({
    id: `r${n}`, projectId: "prj-1", status: "succeeded", attention: "",
  }));
  const filled = projectSectionHtml([{ id: "prj-1", name: "恒星", path: "/tmp/x" }], {
    runs, filter: "prj-1", expanded: new Set(["prj-1"]),
    rowHtml: run => `<div data-nested="${run.id}"></div>`,
  });
  assert.match(filled, /data-proj-toggle="prj-1"/);
  assert.match(filled, /class="smeta">3</);
  assert.match(filled, /is-active/);
  assert.match(filled, /data-nested="r1"/);
  assert.match(filled, /data-proj-run="prj-1"/);
  assert.match(filled, /data-proj-edit="prj-1"/);
});

test("任务/对话列表交给调用方决定行渲染，空态文案是 Kimi 的原话", () => {
  assert.match(runRowsHtml([], {}), /任务会出现在这里/);
  const rows = runRowsHtml([1, 2], { rowHtml: n => `<div data-run="${n}"></div>` });
  assert.equal((rows.match(/data-run=/g) || []).length, 2);

  assert.match(chatRowsHtml([], {}), /还没有对话/);
  const chats = chatRowsHtml([{ id: "t1", title: "周三高铁票" }], { activeId: "t1", total: 5, pinnedOf: () => false });
  assert.match(chats, /data-chat-id="t1"/);
  assert.match(chats, /data-chat-pin="t1"/);
  assert.match(chats, /data-chat-delete="t1"/);
  assert.match(chats, /显示全部 · 还有 4 条/);
});

test("对话行置顶状态写回文案与标志", () => {
  const pinned = chatRowsHtml([{ id: "t9", title: "x" }], { pinnedOf: id => id === "t9" });
  assert.match(pinned, /取消置顶/);
  assert.match(pinned, /★/);
});

test("section 标题与折叠按钮在任务段只带计数，不再有第二个新建入口", () => {
  const html = runSectionHtml("未归属运行", 4);
  assert.match(html, /data-section-toggle="tasks"/);
  assert.match(html, /data-task-count>4</);
  assert.doesNotMatch(html, /data-new-task/);
  assert.doesNotMatch(html, /新建任务/);
  assert.match(chatSectionHtml(2), /data-section-toggle="chats"/);
});

test("快速开始：三步由真实状态推导，全部完成后收敛，dismissed 后不再出现", () => {
  const fresh = quickStartHtml({});
  assert.match(fresh, /快速开始/);
  assert.match(fresh, /0\/3/);
  assert.equal((fresh.match(/quickstart-task"/g) || []).length, 3);
  assert.match(fresh, /data-quickstart-close/);

  const partial = quickStartHtml({ providerReady: true, firstRunDone: true });
  assert.match(partial, /2\/3/);
  assert.equal((partial.match(/quickstart-task done/g) || []).length, 2);

  const done = quickStartHtml({ providerReady: true, firstRunDone: true, ledgerSeen: true });
  assert.match(done, /3\/3/);
  assert.match(done, /quickstart-done-text/);

  assert.equal(quickStartHtml({ dismissed: true }), "");
});

test("状态点沿用治理事实的类名", () => {
  assert.equal(statusDotHtml("ok"), '<span class="smark ok" aria-hidden="true"></span>');
  assert.match(statusDotHtml("bad"), /smark bad/);
});

test("渲染对 HTML 做转义（标题来自模型/用户输入）", () => {
  const html = chatRowsHtml([{ id: "t1", title: '<img src=x onerror="alert(1)">' }], {});
  assert.equal(html.includes("<img"), false);
  assert.match(html, /&lt;img/);
});


/* ---------- 分区历史：治理区审计记录（Kimi 式按日期分组） ---------- */

function localDay(offset) {
  const d = new Date();
  const t = new Date(d.getFullYear(), d.getMonth(), d.getDate() + offset);
  const pad = n => String(n).padStart(2, "0");
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
}

test("治理历史:按今天/昨天/更早分组,行带发现数与审计 id", () => {
  const html = govHistorySectionHtml([
    { dir: `evox.app-${localDay(0)}-aaaaaa`, audit_id: "evox.app-today", target: "/Applications/EvoX.app", findings: 5 },
    { dir: `demo-${localDay(-1)}-bbbbbb`, audit_id: "demo-y", target: "/repo/fixtures/demo-target", findings: 2 },
    { dir: `old-${localDay(-9)}-cccccc`, audit_id: "old-x", target: "/tmp/Old.app", findings: 0 },
  ]);
  assert.match(html, /<span>今天<\/span>/);
  assert.match(html, /<span>昨天<\/span>/);
  assert.match(html, /<span>更早<\/span>/);
  assert.match(html, /data-gov-hist="evox\.app-\d{4}-\d{2}-\d{2}-aaaaaa"[^>]*title="evox.app-today"/);
  assert.match(html, /<span class="slabel">EvoX.app<\/span><span class="smeta">5 发现<\/span>/);
  assert.match(html, /demo-target/);
  assert.match(html, /审计记录/);
  assert.match(html, /data-gov-hist-nav/);
});

test("治理历史:同一目标只一行，指向最近一次并汇总高危/运行次数", () => {
  const older = `evox-${localDay(-1)}-aaaaaa`;
  const latest = `evox-${localDay(0)}-bbbbbb`;
  const html = govHistorySectionHtml([
    { dir: older, audit_id: "evox-old", target: "/Applications/EvoX.app", findings: 9, severity: { high: 4 }, unhandled: 9 },
    { dir: latest, audit_id: "evox-new", target: "/Applications/EvoX.app", findings: 2, severity: { high: 1 }, unhandled: 1 },
  ]);
  assert.equal((html.match(/data-gov-hist=/g) || []).length, 1, "一个 target 只渲染一行");
  assert.match(html, new RegExp(`data-gov-hist="${latest}"[^>]*title="evox-new · 2 次审计"`));
  assert.doesNotMatch(html, new RegExp(`data-gov-hist="${older}"`));
  assert.match(html, /<span class="slabel">EvoX.app<\/span><span class="smeta">高1 · 2 发现<\/span>/);
  assert.match(html, /<span class="side-count">1<\/span>/, "分组计数按目标数，不按运行数");
});

test("治理历史:空列表给引导,不发假行", () => {
  const html = govHistorySectionHtml([]);
  assert.match(html, /还没有治理记录/);
  assert.doesNotMatch(html, /data-gov-hist=/);
});

test("治理历史:团队运行段已撤除——那份事实只住在团队频道侧栏里", () => {
  const html = govHistorySectionHtml([]);
  assert.doesNotMatch(html, /团队运行/);
  assert.doesNotMatch(html, /data-gov-bot-open=/);
});

test("治理历史:审计行带 ruleset 版本,不把版本藏进 title", () => {
  const html = govHistorySectionHtml([{
    dir: "demo-2026-11-10-aaaaaa", audit_id: "demo", target: "/repo/demo",
    findings: 2, severity: { high: 2 }, ruleset_version: 10,
  }]);
  assert.match(html, /高2 · 2 发现 · v10/);
  assert.match(html, /ruleset v10/);
});

test("治理历史:目标名与 title 转义(来自 bundle 事实)", () => {
  const html = govHistorySectionHtml([
    { dir: "x", audit_id: "<b>id</b>", target: "/a/<script>.app", findings: 1 },
  ]);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;b&gt;id/);
});
