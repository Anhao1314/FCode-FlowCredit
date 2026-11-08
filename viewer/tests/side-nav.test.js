import test from "node:test";
import assert from "node:assert/strict";

import {
  MORE_NAV, NAV_GROUPS, SIDEBAR_NAV, ZONES, chatRowsHtml, chatSectionHtml,
  groupedNavHtml, navItemsHtml, pinnedSectionHtml, projectSectionHtml, quickStartHtml, reservedSlots,
  runRowsHtml, statusDotHtml, taskSectionHtml, zoneOf, zoneSwitchHtml,
} from "../core/side-nav.js";

test("统一侧栏:5 个功能面分两组排布,不再拆两套名单", () => {
  const nav = navItemsHtml(SIDEBAR_NAV, {});
  for (const key of ["gov", "audit", "ledger", "reviews", "scheduled"]) {
    assert.match(nav, new RegExp(`data-open="${key}"`), `${key} 要在第一屏可见`);
  }
  // 适配重建后不存在第二套名单:功能面不重复出现在「更多」里。
  const more = navItemsHtml(MORE_NAV, {});
  for (const key of ["gov", "audit", "ledger", "reviews", "scheduled"]) {
    assert.equal((more.match(new RegExp(`data-open="${key}"`, "g")) || []).length, 0,
      `${key} 不得再藏进更多`);
  }
  assert.equal(SIDEBAR_NAV.length, 5);
  assert.equal(SIDEBAR_NAV.filter(item => item.reserved).length, 0);
  assert.match(nav, /data-doodle="shield-check"/, "审计台用盾牌图标");
  assert.equal(SIDEBAR_NAV[0].open, "gov", "治理台打头(治理区对话面)");
});

test("预留槽位带出处与落地条件,全部收进「更多」", () => {
  const more = navItemsHtml(MORE_NAV, {});
  const reserved = reservedSlots();
  assert.deepEqual(Object.keys(reserved).sort(), ["apps", "canvas", "plugins", "remoteControl"]);
  for (const [key, slot] of Object.entries(reserved)) {
    assert.ok(slot.kimi, `${key} 要写明对应 Kimi 的哪一项`);
    assert.ok(slot.needs, `${key} 要写明落地需要什么`);
    assert.match(more, new RegExp(`data-open="${key}"`), `${key} 要出现在更多里`);
  }
  // 可点（不是 disabled）+ 带「预留」标记
  assert.match(more, /class="nav-item nav-item--reserved" type="button" data-open="canvas" data-reserved="1"/);
  assert.match(more, /<span class="nav-reserved" aria-hidden="true">预留<\/span>/);
  assert.equal((more.match(/<button/g) || []).length, 4);
  assert.equal(MORE_NAV.length, 4);
});

test("导航条目的图标与计数按 Kimi 的行结构渲染", () => {
  const html = navItemsHtml(SIDEBAR_NAV, { scheduled: 7 });
  assert.match(html, /class="nav-item"/);
  assert.match(html, /data-doodle="calendar"/);
  assert.match(html, /<span class="side-count">7<\/span>/);
  assert.equal((html.match(/<button/g) || []).length, SIDEBAR_NAV.length);
});

test("S2 两区分组:工作区与治理区,恰好覆盖全部功能面", () => {
  // 分组恰好覆盖:每个功能面进且只进一个区,登记遗漏会被兜底渲染暴露
  const covered = NAV_GROUPS.flatMap(group => group.open);
  assert.equal(new Set(covered).size, covered.length, "一个功能面不得同时进两个区");
  assert.deepEqual([...covered].sort(), SIDEBAR_NAV.map(item => item.open).sort());
  assert.deepEqual(NAV_GROUPS.map(group => group.label), ["工作区", "治理区"]);
});

test("groupedNavHtml 按区渲染标签与行,计数随行", () => {
  const html = groupedNavHtml(SIDEBAR_NAV, NAV_GROUPS, { ledger: 12 });
  const groups = html.match(/class="nav-group"/g) || [];
  assert.equal(groups.length, 2);
  assert.match(html, /<div class="nav-group-label">工作区<\/div>/);
  assert.match(html, /<div class="nav-group-label">治理区<\/div>/);
  // 治理区在第二组:审计台的位置要晚于工作区标签
  assert.ok(html.indexOf("nav-group-label\">工作区") < html.indexOf("data-open=\"audit\""));
  assert.match(html, /data-open="ledger"[\s\S]*?<span class="side-count">12<\/span>/);
  assert.equal((html.match(/<button/g) || []).length, SIDEBAR_NAV.length);
});

test("groupedNavHtml 兜底:未登记进任何区的条目仍渲染,不静默消失", () => {
  const extra = [...SIDEBAR_NAV, { open: "future", label: "新面", icon: "cube" }];
  const html = groupedNavHtml(extra, NAV_GROUPS, {});
  assert.match(html, /data-open="future"/);
  assert.equal((html.match(/<button/g) || []).length, extra.length);
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
  assert.deepEqual(ZONES.map(z => z.land), ["ledger", "gov"]);
  const html = zoneSwitchHtml("gov");
  assert.match(html, /data-zone="work" data-open="ledger"/);
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
