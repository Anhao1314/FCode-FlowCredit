import test from "node:test";
import assert from "node:assert/strict";

import {
  ZONES, chatRowsHtml, chatSectionHtml,
  pinnedSectionHtml, projectSectionHtml, quickStartHtml,
  runRowsHtml, statusDotHtml, taskSectionHtml, zoneOf, zoneSwitchHtml,
} from "../core/side-nav.js";

test("功能导航已撤除:治理台走 Governance 区切换器,不再有侧栏条目", async () => {
  const mod = await import("../core/side-nav.js");
  for (const key of ["SIDEBAR_NAV", "NAV_GROUPS", "navItemsHtml", "groupedNavHtml",
    "MORE_NAV", "reservedSlots"]) {
    assert.equal(mod[key], undefined, `${key} 不应再导出（导航/预留均已清理）`);
  }
  // 两区对话面仍在:Work/Governance 的切换器是唯一侧栏入口
  assert.deepEqual(ZONES.map(z => z.label), ["Work", "Governance"]);
  assert.deepEqual(ZONES.map(z => z.land), ["chat", "gov"]);
  const html = zoneSwitchHtml("gov");
  assert.match(html, /data-open="gov"/, "治理台经 Governance 区着陆");
});

test("S2.1 zoneOf:视图归属区,非区视图返回 null(切换器不闪)", () => {
  assert.equal(zoneOf("gov"), "gov");
  assert.equal(zoneOf("audit"), "gov");
  assert.equal(zoneOf("reviews"), "gov");
  for (const view of ["chat", "ledger", "run", "scheduled", "intake", "empty"]) {
    assert.equal(zoneOf(view), "work", `${view} 属工作区`);
  }
  for (const view of ["settings", "canvas", undefined, ""]) {
    assert.equal(zoneOf(view), null, `${view} 不属任何区`);
  }
});

test("S2.1 zoneSwitchHtml:两键带 data-open 着陆与 aria-pressed 点亮", () => {
  assert.deepEqual(ZONES.map(z => z.key), ["work", "gov"]);
  // R1:两区的着陆点都是各自的对话面——工作区=新对话(chat),治理区=治理台
  assert.deepEqual(ZONES.map(z => z.land), ["chat", "gov"]);
  const html = zoneSwitchHtml("gov");
  assert.match(html, /data-zone="work" data-open="chat"/);
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

test("Projects 段：标题可折叠 + ＋动作 + 真实空态", () => {
  const empty = projectSectionHtml([], {});
  assert.match(empty, /data-section-toggle="projects"/);
  assert.match(empty, /data-proj-add/);
  assert.match(empty, /section-empty/);
  const filled = projectSectionHtml([{ id: "prj-1", name: "恒星" }], { countOf: () => 3, filter: "prj-1" });
  assert.match(filled, /data-proj="prj-1"/);
  assert.match(filled, /class="smeta">3</);
  assert.match(filled, /active/);
  assert.equal((filled.match(/section-empty/g) || []).length, 0, "有项目就不该有空态");
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

test("section 标题与折叠按钮在任务段带上 ＋ 与计数", () => {
  const html = taskSectionHtml(4);
  assert.match(html, /data-section-toggle="tasks"/);
  assert.match(html, /data-task-count>4</);
  assert.match(html, /data-new-task/);
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
